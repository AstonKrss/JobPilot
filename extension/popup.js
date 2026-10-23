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
