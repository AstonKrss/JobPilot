/**
 * 扩展弹窗：只做两件事 —— 显示后端是否可达、允许改后端地址。
 *
 * 真正的操作面板在页面里（content script 注入的悬浮面板），
 * 因为抓取与展示都需要直接接触页面 DOM，放在 popup 里还得来回传消息。
 * popup 每次打开都会被销毁重建，不适合承载需要保留状态的交互。
 */

const DEFAULT_BASE = 'http://127.0.0.1:8000';

function setStatus(text, kind) {
  const node = document.getElementById('status');
  node.textContent = text;
  node.className = 'status' + (kind ? ' ' + kind : '');
}

function callApi(base, path) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'api', path }, (resp) => {
      // 后端地址还没保存过时，background 会用默认值；这里只关心结果
      if (chrome.runtime.lastError || !resp) {
        resolve({ ok: false, error: '后台无响应' });
        return;
      }
      resolve(resp);
    });
  });
}

async function check() {
  setStatus('检查中…');
  const resp = await callApi(DEFAULT_BASE, '/health');
  if (!resp.ok) {
    setStatus(resp.error || '无法连接后端', 'bad');
    return;
  }
  const data = resp.data || {};
  if (!data.db) {
    setStatus('后端已连上，但数据库不可用', 'bad');
  } else if (!data.llm_configured) {
    setStatus('后端已连上，但未配置模型密钥', 'bad');
  } else {
    setStatus('就绪：数据库与模型均可用', 'ok');
  }
}

async function restore() {
  const stored = await chrome.storage.local.get('baseUrl');
  document.getElementById('base').value = stored.baseUrl || DEFAULT_BASE;
}

document.getElementById('save').addEventListener('click', async () => {
  const value = document.getElementById('base').value.trim().replace(/\/+$/, '');
  await chrome.storage.local.set({ baseUrl: value || DEFAULT_BASE });
  setStatus('已保存，重新检查…');
  await check();
});

restore().then(check);

// 页面右下角没出现按钮时的兜底：手动注入一次。
// 自动注入依赖 manifest 的 matches，平台换域名（zhaopin.cn）或 URL 结构变了就会漏。
// 这个按钮让用户不必等改代码才能用上，也让"不显示"有第二个出路
document.getElementById('inject').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    setStatus('找不到当前标签页', 'bad');
    return;
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: false },
      files: ['lib.js', 'content.js', 'chat.js'],
    });
    setStatus('已尝试注入。看页面右下角；仍然没有就点「诊断页面结构」把信息发我', 'ok');
    window.close();
  } catch (e) {
    setStatus(`注入失败：${e.message}（该页面可能不允许扩展运行）`, 'bad');
  }
});


/* ---------- 自动发现新岗位 ---------- */

const WATCH_KEYS = ['watchUrl', 'watchOn', 'watchLast'];

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderWatchLast(last) {
  const node = document.getElementById('watch-last');
  if (!node) return;
  if (!last || !last.at) {
    node.textContent = '还没扫过。';
    return;
  }
  if (last.note) {
    node.textContent = `上次 ${fmtTime(last.at)}：${last.note}`;
    return;
  }
  node.textContent = `上次 ${fmtTime(last.at)}：抓了 ${last.total || 0} 个，新增 ${last.added || 0} 个`;
}

async function restoreWatch() {
  const stored = await chrome.storage.local.get(WATCH_KEYS);
  document.getElementById('watch').value = stored.watchUrl || '';
  document.getElementById('watch-on').checked = Boolean(stored.watchOn);
  renderWatchLast(stored.watchLast);
}

document.getElementById('watch').addEventListener('change', async (ev) => {
  await chrome.storage.local.set({ watchUrl: ev.target.value.trim() });
});

document.getElementById('watch-on').addEventListener('change', async (ev) => {
  const enabled = ev.target.checked;
  const url = document.getElementById('watch').value.trim();
  if (enabled && !url) {
    ev.target.checked = false;
    setStatus('先填一个搜索结果页网址，再开启定时扫描', 'bad');
    return;
  }
  await chrome.storage.local.set({ watchUrl: url, watchOn: enabled });
  chrome.runtime.sendMessage({ type: 'watch-schedule', enabled });
  setStatus(enabled ? '已开启：每 2 小时扫一次（3 分钟后首次运行）' : '已关闭定时扫描', 'ok');
});

document.getElementById('watch-run').addEventListener('click', async () => {
  const url = document.getElementById('watch').value.trim();
  if (!url) {
    setStatus('先填一个搜索结果页网址', 'bad');
    return;
  }
  await chrome.storage.local.set({ watchUrl: url });
  setStatus('正在扫描，大约需要 20-40 秒…');
  chrome.runtime.sendMessage({ type: 'watch-now' }, (resp) => {
    if (chrome.runtime.lastError || !resp) {
      setStatus('扫描没跑起来，检查后端是否在运行', 'bad');
      return;
    }
    const r = (resp.result || {});
    if (r.skipped) {
      setStatus('扫描被跳过：先勾上「每 2 小时自动扫一次」', 'bad');
      return;
    }
    if (r.error) {
      setStatus(`扫描失败：${r.error}`, 'bad');
      return;
    }
    setStatus(`扫描完成：抓了 ${r.total || 0} 个，新增 ${r.added || 0} 个`, 'ok');
    chrome.storage.local.get('watchLast').then((s) => renderWatchLast(s.watchLast));
  });
});

restoreWatch();

/* ---------- 批量采集 ----------
   放在弹窗而不是网页 UI 里：网页在 127.0.0.1，不在扩展的内容脚本注入范围内，
   网页想直接驱动扩展需要 externally_connectable + 手填扩展 ID，多一步就多一个
   劝退点。弹窗本来就在浏览器里，天然能发消息给 background。 */
const crawlInput = document.getElementById('crawl');
const crawlPages = document.getElementById('crawl-pages');
const crawlBtn = document.getElementById('crawl-run');
const crawlLast = document.getElementById('crawl-last');

function renderCrawlLast(last) {
  if (!last) return;
  const when = new Date(last.at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  const line = `${when} · 翻 ${last.pages || 0}/${last.total || 0} 页 · 扫描 ${last.scanned || 0} 条 · 新增 ${last.added || 0} 个`;
  crawlLast.textContent = last.note ? `${line}\n${last.note}` : line;
  crawlLast.style.whiteSpace = 'pre-line';
}

if (crawlInput && crawlBtn) {
  chrome.storage.local.get(['crawlUrl', 'crawlPages', 'crawlLast']).then((s) => {
    if (s.crawlUrl) crawlInput.value = s.crawlUrl;
    if (s.crawlPages) crawlPages.value = s.crawlPages;
    renderCrawlLast(s.crawlLast);
  });

  crawlBtn.addEventListener('click', async () => {
    const url = crawlInput.value.trim();
    if (!url) {
      crawlLast.textContent = '先粘一个搜索结果页的网址';
      return;
    }
    await chrome.storage.local.set({ crawlUrl: url, crawlPages: crawlPages.value });
    crawlBtn.disabled = true;
    crawlBtn.textContent = '采集中…';
    crawlLast.textContent = '正在后台标签页里翻页，窗口会自己关掉，不要手动关它';
    chrome.runtime.sendMessage(
      { type: 'crawl-start', url, pages: Number(crawlPages.value) || 1 },
      (resp) => {
        crawlBtn.disabled = false;
        crawlBtn.textContent = '开始采集';
        if (chrome.runtime.lastError || !resp || !resp.ok) {
          crawlLast.textContent = '采集失败：' + (chrome.runtime.lastError?.message || '后台无响应');
          return;
        }
        renderCrawlLast({ ...resp.result, at: Date.now() });
      },
    );
  });
}
