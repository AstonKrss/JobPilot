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
