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

/* ---------- 批量采集（雷达用） ----------
 *
 * 输入是"用户自己搜好的一个结果页 URL"，输出是翻了 N 页拿到的一批卡片摘要。
 * 不做 URL 拼接：平台的搜索参数带一次性签名，硬拼出来的链接经常只是
 * 重复返回第一页 —— 表现成"抓了一堆重复岗位"，排查方向会被带偏。
 *
 * 每页之间靠 DOM 指纹判断翻页是否成功，而不是固定 sleep：
 * sleep 短了会重复提交同一页，长了整轮采集慢得离谱。
 */

const CRAWL_MAX_PAGES = 5;
const CRAWL_PER_PAGE = 30;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForPageChange(tabId, beforeFirst, timeoutMs = 12000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    await sleep(700);
    const sig = await chrome.tabs.sendMessage(tabId, { type: 'page-signature' }).catch(() => null);
    if (!sig || !sig.ok) continue;
    // 首条卡片变了就算翻页成功。有些平台是整页刷新，href 也会变，任一即可
    if (sig.first && sig.first !== beforeFirst) return true;
  }
  return false;
}

/** 抓当前页的卡片并入库。返回本页新增数与卡片数。 */
async function ingestPage(tabId, source, seenJobs) {
  const listing = await chrome.tabs
    .sendMessage(tabId, { type: 'scrape-list', count: CRAWL_PER_PAGE })
    .catch(() => null);
  if (!listing || !listing.ok || !(listing.items || []).length) {
    return { ok: false, cards: 0, added: 0 };
  }

  let added = 0;
  for (const item of listing.items) {
    const key = item.source_job_id || item.url;
    // 跨页去重。平台的推荐位会把同一岗位放到第 2、3 页，
    // 不去重的话一次采集能提交三遍同一份 JD，还会占掉后面的真实新岗位名额
    if (!key || seenJobs.has(key)) continue;
    seenJobs.add(key);
    try {
      const res = await backendCall('/api/jobs/ingest', 'POST', {
        source: item.source || source,
        raw_text: item.raw_text,
        url: item.url,
        source_job_id: item.source_job_id,
        detail_level: 'digest',
      });
      if (res && res.is_new) added += 1;
    } catch {
      // 单条失败不中断整批：平台字段偶尔缺，跳过即可
    }
  }
  return { ok: true, cards: listing.items.length, added };
}

/**
 * 在已经打开的标签页上翻页抓取。
 *
 * 抽成独立函数是因为现在有两个入口：手动「批量采集」和一键发现的多关键词循环。
 * 两处各写一份翻页逻辑，改了一处忘另一处，表现就是"手动能抓、一键发现抓不到"。
 */
async function crawlPagesOnTab(tabId, pages, source, onProgress) {
  const total = Math.max(1, Math.min(Number(pages) || 1, CRAWL_MAX_PAGES));
  const seenJobs = new Set();
  let added = 0;
  let scanned = 0;
  let donePages = 0;
  let note = '';

  for (let page = 1; page <= total; page += 1) {
    const res = await ingestPage(tabId, source, seenJobs);
    if (!res.ok) {
      note = page === 1
        ? '第一页就没解析出岗位：可能没登录，或平台改版了卡片选择器'
        : '这一页没有岗位，提前结束';
      break;
    }

    donePages = page;
    scanned += res.cards;
    added += res.added;
    if (onProgress) onProgress({ page, pages: total, added, scanned });

    // 这一页一条新的都没有，说明已经翻出了重复内容，没必要继续
    if (res.added === 0 && page > 1) {
      note = `第 ${page} 页全是已入库岗位，提前结束`;
      break;
    }
    if (page === total) break;

    const before = await chrome.tabs
      .sendMessage(tabId, { type: 'page-signature' })
      .catch(() => null);
    const next = await chrome.tabs
      .sendMessage(tabId, { type: 'goto-next-page' })
      .catch(() => null);
    if (!next || !next.ok) {
      note = (next && next.reason) || '翻页失败，已抓到的部分不受影响';
      break;
    }
    const changed = await waitForPageChange(tabId, before && before.ok ? before.first : '');
    if (!changed) {
      note = '点击下一页后页面没有变化，可能被登录墙拦住了';
      break;
    }
  }

  return { added, scanned, pages: donePages, total, note };
}

async function crawlSearch(startUrl, pages, onProgress) {
  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: startUrl, active: false });
    tabId = tab.id;
    await waitForComplete(tabId, 30000);
    const result = await crawlPagesOnTab(tabId, pages, guessSource(startUrl), onProgress);

    await chrome.storage.local.set({ crawlLast: { at: Date.now(), ...result, url: startUrl } });
    if (result.added > 0) {
      chrome.notifications.create(`crawl-${Date.now()}`, {
        type: 'basic',
        iconUrl: 'icon128.png',
        title: `批量采集：新增 ${result.added} 个岗位`,
        message: `翻了 ${result.pages} 页，扫描 ${result.scanned} 条。去「发现」页重新扫描一次雷达`,
      });
    }
    return result;
  } catch (err) {
    return { added: 0, scanned: 0, pages: 0, total: 0, note: String(err) };
  } finally {
    if (tabId !== null) chrome.tabs.remove(tabId).catch(() => {});
  }
}

/**
 * 一键发现：按后端算出的搜索计划，在同一个标签页里换关键词逐个搜索 + 翻页。
 *
 * 为什么不拼搜索结果 URL：各平台的搜索参数（query key、城市码、筛选位、
 * 一次性签名）拼错的表现是"打开了一个正常页面，但结果和你搜的词无关" ——
 * 看起来像抓取坏了，实际是搜索没生效，排查方向会被带偏。
 * 在搜索框里打字是真人做的事，参数由平台自己处理。
 *
 * 关键词数量默认 3 个：每个词要翻 2-3 页、每次大概 20-40 秒。
 * 轮着来 5 个词就是五分钟，用户会以为卡死。宁可少而快，多搜几次。
 */
async function discoverAll(startUrl, pages, maxKeywords = 3) {
  let plan;
  try {
    plan = await backendCall('/api/radar/plan', 'GET');
  } catch {
    return { added: 0, keywords: 0, note: '后端没响应，先启动本地服务（start.bat）' };
  }
  const keywords = (plan.keywords || []).slice(0, maxKeywords).map((k) => k.q).filter(Boolean);
  if (!keywords.length) {
    return { added: 0, keywords: 0, note: '搜索计划里没有关键词，先去「发现」页看看简历技能是否抽全' };
  }

  let tabId = null;
  let added = 0;
  let scanned = 0;
  const done = [];
  const notes = [];

  try {
    const tab = await chrome.tabs.create({ url: startUrl, active: false });
    tabId = tab.id;
    await waitForComplete(tabId, 30000);
    const source = guessSource(startUrl);

    for (const keyword of keywords) {
      const before = await chrome.tabs
        .sendMessage(tabId, { type: 'page-signature' })
        .catch(() => null);

      const applied = await chrome.tabs
        .sendMessage(tabId, { type: 'search-keyword', keyword })
        .catch(() => null);
      if (!applied || !applied.ok) {
        notes.push((applied && applied.reason) || `「${keyword}」搜索没能执行`);
        break;
      }

      let changed = await waitForPageChange(
        tabId, before && before.ok ? before.first : '', 15000,
      );
      if (!changed) {
        // 回车没生效时补一次点搜索按钮 —— 有的站只认按钮点击
        await chrome.tabs.sendMessage(tabId, { type: 'click-search' }).catch(() => null);
        changed = await waitForPageChange(
          tabId, before && before.ok ? before.first : '', 12000,
        );
      }
      if (!changed) {
        // 页面没变有两种可能：搜索没生效，或者这个词本来就搜不到新东西。
        // 无论哪种，把当前页抓一遍都不亏（可能是第一个词，页面本来就是它的结果）
        notes.push(`「${keyword}」搜索后页面没变化，抓到的是当前页内容`);
      }

      const res = await crawlPagesOnTab(tabId, pages, source);
      added += res.added;
      scanned += res.scanned;
      done.push({ keyword, added: res.added, scanned: res.scanned, pages: res.pages });
      if (res.note) notes.push(`「${keyword}」：${res.note}`);
    }

    const result = {
      added, scanned, keywords: done.length, done, total: keywords.length,
      note: notes.join('；'),
    };
    await chrome.storage.local.set({ discoverLast: { at: Date.now(), ...result, url: startUrl } });
    if (added > 0) {
      chrome.notifications.create(`discover-${Date.now()}`, {
        type: 'basic',
        iconUrl: 'icon128.png',
        title: `一键发现：新增 ${added} 个岗位`,
        message: `跑了 ${done.length} 个关键词，扫描 ${scanned} 条。去「发现」页重新扫描雷达`,
      });
    }
    return result;
  } catch (err) {
    return { added, scanned, keywords: done.length, done, total: keywords.length, note: String(err) };
  } finally {
    if (tabId !== null) chrome.tabs.remove(tabId).catch(() => {});
  }
}

/* ---------- HR 消息监控 ----------
 *
 * 后台定时开一个标签页打开聊天页 → 让 chat.js 抓未读会话 → 上报 → 关标签。
 *
 * 关键取舍：不开标签页就抓不到。
 *   平台的消息在登录态 + 客户端渲染之后的 DOM 里，没有可用的接口能直接拉。
 *   所以"监控"的真实代价是每隔一段时间开一个后台标签（不抢焦点，几十秒后关）。
 *   用户必须知道这一点，否则会在任务栏看到莫名闪现的标签页而以为中毒了。
 *
 * 另一个必须说清的：点开会话会清掉平台上的未读红点。
 *   消息已经进本地库了，红点留着只会重复提醒。
 */

const CHAT_ALARM = 'watch-chat';
const CHAT_PERIOD_MIN = 10;
// 通知点开后要跳到哪个页面。通知 id 与 URL 的映射只存在内存里，
// service worker 被回收就丢了 —— 所以点通知时如果没有映射，就退回到
// 打开用户配置的第一个聊天页
let chatUrlByNotify = {};

async function scanChatPages(force = false) {
  const conf = await chrome.storage.local.get(['chatUrls', 'chatOn']);
  const urls = (conf.chatUrls || []).map((u) => String(u || '').trim()).filter(Boolean);
  if ((!conf.chatOn && !force) || !urls.length) return { skipped: true };

  let addedHr = 0;
  let unread = 0;
  let conversations = 0;
  const titles = [];
  const samples = [];

  for (const url of urls.slice(0, 3)) {
    let tabId = null;
    try {
      const tab = await chrome.tabs.create({ url, active: false });
      tabId = tab.id;
      await waitForComplete(tabId, 30000);
      // 会话列表与消息体都是异步渲染的，等页面 complete 之后还要再给两秒
      await sleep(2500);
      const res = await chrome.tabs
        .sendMessage(tabId, { type: 'sync-chat-now', deep: true, max: 4 })
        .catch(() => null);
      if (!res || !res.ok) continue;
      addedHr += res.added_hr || 0;
      unread += res.unread || 0;
      conversations += res.conversations || 0;
      for (const item of res.items || []) {
        if (!item.added_hr) continue;
        if (item.title) titles.push(item.title);
        if (item.last_hr) samples.push(`${item.title || '会话'}：${item.last_hr}`);
      }
    } catch {
      // 单个聊天页失败不影响其他平台
    } finally {
      if (tabId !== null) chrome.tabs.remove(tabId).catch(() => {});
    }
  }

  const summary = {
    at: Date.now(),
    addedHr,
    unread,
    conversations,
    titles: titles.slice(0, 5),
    note: addedHr
      ? `HR 新增 ${addedHr} 条消息`
      : unread
        ? `有 ${unread} 个会话未读，但没读到新内容`
        : '没有新消息',
  };
  await chrome.storage.local.set({ chatLast: summary });

  if (addedHr > 0) {
    const id = `chat-${Date.now()}`;
    chatUrlByNotify[id] = urls[0];
    chrome.notifications.create(id, {
      type: 'basic',
      iconUrl: 'icon128.png',
      title: `HR 回了 ${addedHr} 条消息`,
      message: (samples[0] || titles[0] || '有新回复').slice(0, 120),
      priority: 2,
    });
    chrome.action.setBadgeBackgroundColor({ color: '#ff3b30' });
    chrome.action.setBadgeText({ text: String(Math.min(addedHr, 99)) });
  } else if (unread > 0) {
    const id = `chat-unread-${Date.now()}`;
    chatUrlByNotify[id] = urls[0];
    chrome.notifications.create(id, {
      type: 'basic',
      iconUrl: 'icon128.png',
      title: `有 ${unread} 个会话未读`,
      message: '点开看看；如果一直读不到内容，可能是平台改版了会话列表结构',
    });
  }

  return summary;
}

async function scheduleChat(enabled) {
  await chrome.alarms.clear(CHAT_ALARM);
  if (!enabled) {
    chrome.action.setBadgeText({ text: '' });
    return;
  }
  // 2 分钟后跑第一次：刚装完就开标签页会撞上用户正在看的页面
  chrome.alarms.create(CHAT_ALARM, { delayInMinutes: 2, periodInMinutes: CHAT_PERIOD_MIN });
}

chrome.notifications.onClicked.addListener((id) => {
  const url = chatUrlByNotify[id];
  if (url) chrome.tabs.create({ url, active: true });
  chrome.notifications.clear(id);
  chrome.action.setBadgeText({ text: '' });
});

async function scheduleWatch(enabled) {
  await chrome.alarms.clear(WATCH_ALARM);
  if (!enabled) return;
  // delayInMinutes 给 3 分钟：装完扩展马上跑一次会撞上用户正在浏览的页面
  chrome.alarms.create(WATCH_ALARM, { delayInMinutes: 3, periodInMinutes: WATCH_PERIOD_MIN });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === WATCH_ALARM) scanWatchPage();
  if (alarm.name === CHAT_ALARM) scanChatPages();
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
  if (message.type === 'crawl-start') {
    crawlSearch(message.url, message.pages).then((r) => sendResponse({ ok: true, result: r }));
    return true;
  }
  if (message.type === 'crawl-last') {
    chrome.storage.local.get('crawlLast').then((s) => sendResponse({ ok: true, last: s.crawlLast || null }));
    return true;
  }
  if (message.type === 'discover-start') {
    discoverAll(message.url, message.pages, message.maxKeywords || 3)
      .then((r) => sendResponse({ ok: true, result: r }));
    return true;
  }
  if (message.type === 'discover-last') {
    chrome.storage.local.get('discoverLast').then((s) => sendResponse({ ok: true, last: s.discoverLast || null }));
    return true;
  }
  if (message.type === 'chat-now') {
    scanChatPages(true).then((r) => sendResponse({ ok: true, result: r }));
    return true;
  }
  if (message.type === 'chat-schedule') {
    scheduleChat(Boolean(message.enabled)).then(() => sendResponse({ ok: true }));
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

// 聊天监控的闹钟同样要恢复：MV3 的 service worker 会被回收，
// alarm 虽然持久化，但重新装扩展/改配置后靠这段把状态接回来
chrome.alarms.get(CHAT_ALARM).then(async (existing) => {
  const conf = await chrome.storage.local.get('chatOn');
  if (conf.chatOn && !existing) {
    chrome.alarms.create(CHAT_ALARM, { delayInMinutes: 2, periodInMinutes: CHAT_PERIOD_MIN });
  }
});
