/**
 * 后台代理：所有发往后端的请求都从这里走。
 *
 * 为什么不能让 content script 直接请求
 *   招聘网站是 https，而本地后端是 http://127.0.0.1:8000。
 *   从 https 页面里发起 http 请求属于混合内容，浏览器会直接拦截，请求根本到不了后端。
 *   service worker 有独立的执行上下文，不受页面混合内容策略约束，
 *   加上 manifest 里声明了 host_permissions，跨域请求才是可行的。
 *   这不是代码洁癖，是唯一能通的路径。
 */

const DEFAULT_BASE = 'http://127.0.0.1:8000';

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'api') return undefined;

  (async () => {
    try {
      const stored = await chrome.storage.local.get('baseUrl');
      const base = (stored.baseUrl || DEFAULT_BASE).replace(/\/+$/, '');
      const url = base + message.path;

      const init = { method: message.method || 'GET' };
      if (message.body !== undefined) {
        init.headers = { 'Content-Type': 'application/json' };
        init.body = JSON.stringify(message.body);
      }

      const resp = await fetch(url, init);
      const raw = await resp.text();
      let data = null;
      if (raw) {
        try {
          data = JSON.parse(raw);
        } catch {
          data = null;
        }
      }

      if (!resp.ok) {
        const detail = data && data.detail !== undefined ? data.detail : `HTTP ${resp.status}`;
        sendResponse({
          ok: false,
          error: typeof detail === 'string' ? detail : JSON.stringify(detail),
        });
        return;
      }
      sendResponse({ ok: true, data });
    } catch (err) {
      // 连不上本地服务是最常见的情况（后端没启动），要把话说清楚
      sendResponse({
        ok: false,
        error: '无法连接本地后端，请确认服务已启动（scripts/start-api.bat）',
      });
    }
  })();

  // 返回 true 表示会异步调用 sendResponse，否则通道会被提前关闭
  return true;
});

/* ---------- 自动发现新岗位 ----------
 *
 * 做法：定时开一个「后台标签页」打开用户配置的搜索结果页 → 让页面里的
 * content script 抓列表 → 逐条入库 → 统计新增 → 通知 → 关掉标签。
 *
 * 为什么必须用真实标签页而不是 fetch：
 *   这些平台是纯客户端渲染，fetch 回来的是空壳；而且登录态在标签页里才有。
 *   绕开这两点的唯一办法就是真的把页面打开一次。
 *
 * 代价必须说清楚（界面上也要写）：
 *   1. 每 2 小时会在后台开一个标签（不抢焦点），几十秒后自动关闭
 *   2. 需要保持登录，否则抓到 0 条 —— 不报错，只是没收获
 *   3. 只抓首屏卡片摘要（digest），完整 JD 仍要手动点开
 */

const WATCH_ALARM = 'watch-new-jobs';
const WATCH_PERIOD_MIN = 120;
const WATCH_BATCH = 20;

function guessSource(url) {
  const u = String(url || '').toLowerCase();
  if (u.includes('zhipin.com')) return 'boss';
  if (u.includes('liepin.com')) return 'liepin';
  if (u.includes('zhaopin.com') || u.includes('zhaopin.cn')) return 'zhaopin';
  return 'manual';
}

async function backendCall(path, method, body) {
  const stored = await chrome.storage.local.get('baseUrl');
  const base = (stored.baseUrl || DEFAULT_BASE).replace(/\/+$/, '');
  const init = { method: method || 'GET' };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(base + path, init);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/** 等标签页加载完，再额外给 1.2 秒让 SPA 把卡片渲染出来。 */
function waitForComplete(tabId, timeoutMs = 25000) {
  return new Promise((resolve) => {
    const started = Date.now();
    const timer = setInterval(async () => {
      try {
        const tab = await chrome.tabs.get(tabId);
        if (tab.status === 'complete') {
          clearInterval(timer);
          setTimeout(resolve, 1200);
          return;
        }
      } catch {
        clearInterval(timer);
        resolve();
        return;
      }
      if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        resolve();
      }
    }, 300);
  });
}

async function scanWatchPage(force = false) {
  const conf = await chrome.storage.local.get(['watchUrl', 'watchOn']);
  // force：用户点「立即扫一次」时不受定时开关限制 —— 否则会得到一个
  // "被跳过"的提示，而他明明刚点了按钮，这种体验最容易被当成坏了
  if ((!conf.watchOn && !force) || !conf.watchUrl) return { skipped: true };

  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: conf.watchUrl, active: false });
    tabId = tab.id;
    await waitForComplete(tabId);

    const listing = await chrome.tabs
      .sendMessage(tabId, { type: 'scrape-list', count: WATCH_BATCH })
      .catch(() => null);

    if (!listing || !listing.ok || !(listing.items || []).length) {
      await chrome.storage.local.set({
        watchLast: {
          at: Date.now(),
          added: 0,
          total: 0,
          note: '页面没解析出岗位（可能未登录，或平台改版了选择器）',
        },
      });
      return { added: 0, total: 0 };
    }

    let added = 0;
    for (const item of listing.items) {
      try {
        const res = await backendCall('/api/jobs/ingest', 'POST', {
          source: item.source || guessSource(conf.watchUrl),
          raw_text: item.raw_text,
          url: item.url,
          source_job_id: item.source_job_id,
          detail_level: 'digest',
        });
        if (res && res.is_new) added += 1;
      } catch {
        // 单条失败不影响整批：平台字段偶尔会缺，跳过即可
      }
    }

    await chrome.storage.local.set({
      watchLast: { at: Date.now(), added, total: listing.items.length },
    });

    if (added > 0) {
      chrome.notifications.create(`new-jobs-${Date.now()}`, {
        type: 'basic',
        iconUrl: 'icon128.png',
        title: `发现 ${added} 个新岗位`,
        message: '已经入库，去「匹配」页跑一次就能看到结论',
      });
    }
    return { added, total: listing.items.length };
  } catch (err) {
    await chrome.storage.local.set({
      watchLast: { at: Date.now(), added: 0, note: String(err && err.message ? err.message : err) },
    });
    return { added: 0, error: String(err) };
  } finally {
    if (tabId !== null) {
      chrome.tabs.remove(tabId).catch(() => {});
    }
  }
}

async function scheduleWatch(enabled) {
  await chrome.alarms.clear(WATCH_ALARM);
  if (!enabled) return;
  // delayInMinutes 给 3 分钟：装完扩展马上跑一次会撞上用户正在浏览的页面
  chrome.alarms.create(WATCH_ALARM, { delayInMinutes: 3, periodInMinutes: WATCH_PERIOD_MIN });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === WATCH_ALARM) scanWatchPage();
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message) return undefined;
  if (message.type === 'watch-now') {
    scanWatchPage(true).then((r) => sendResponse({ ok: true, result: r }));
    return true;
  }
  if (message.type === 'watch-schedule') {
    scheduleWatch(Boolean(message.enabled)).then(() => sendResponse({ ok: true }));
    return true;
  }
  return undefined;
});

// service worker 每次唤醒都会重新执行，所以要判断是否已经排过，
// 否则每醒一次就多一个 alarm（同名 alarm 会覆盖，但仍然重复创建）
chrome.alarms.get(WATCH_ALARM).then(async (existing) => {
  const conf = await chrome.storage.local.get('watchOn');
  if (conf.watchOn && !existing) {
    chrome.alarms.create(WATCH_ALARM, { delayInMinutes: 3, periodInMinutes: WATCH_PERIOD_MIN });
  }
});
