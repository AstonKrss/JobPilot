/**
 * HR 消息监控 + 一键发现的离线测试。
 *
 * 这两件事都不好在真环境里验证：监控要等 HR 真的回消息，一键发现要跑几个关键词。
 * 但真正容易坏的恰恰不是"跑通"，而是：
 *   1) 未读徽章的识别 —— 平台把会话渲染出来再隐藏，隐藏的也会被选中，
 *      于是每次检查都报"有 5 个未读"，用户两天后就不看通知了
 *   2) 换搜索词 —— 直接改 input.value 不触发 React 的 onChange，
 *      表现是"按了搜索但结果没变"，而且控制台干干净净
 * 两件事都能用 jsdom + 合成 DOM 断言，所以放在这里。
 *
 * 用法：node scripts/check_monitor.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const EXT = path.join(ROOT, 'extension');

let pass = 0;
let fail = 0;
const ok = (m) => { console.log(`[PASS] ${m}`); pass += 1; };
const bad = (m, d) => { console.log(`[FAIL] ${m}${d ? ' — ' + d : ''}`); fail += 1; };

/** 统一的 chrome mock。onMessage.addListener 会把回调存下来供测试直接触发。 */
function makeChrome() {
  const listeners = [];
  return {
    listeners,
    api: {
      runtime: {
        id: 'test',
        getURL: (p) => p,
        sendMessage: async () => ({ ok: true, data: {} }),
        onMessage: { addListener: (fn) => listeners.push(fn) },
        lastError: null,
      },
      storage: { local: { get: async () => ({}), set: async () => ({}) } },
    },
  };
}

// ---------------------------------------------------------------- 聊天页
function bootChat(html) {
  const dom = new JSDOM(html, {
    url: 'https://www.zhipin.com/web/geek/chat',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;

  Object.defineProperty(w.HTMLElement.prototype, 'innerText', {
    get() { return this.textContent; },
    set(v) { this.textContent = v; },
  });
  // 可见性判断靠尺寸。隐藏元素宽度为 0 —— 真实浏览器里也是这样，
  // 而且隐藏是沿祖先继承的（父节点 display:none，子节点同样 0 尺寸），
  // mock 必须模拟这一点，否则"隐藏会话不算未读"这条等于没测
  const isHidden = (el) => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (/hidden|hide/.test(String(n.className || ''))) return true;
    }
    return false;
  };
  w.Element.prototype.getBoundingClientRect = function () {
    if (isHidden(this)) return { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0 };
    const badge = /unread|red-dot|reddot|badge/.test(String(this.className || ''));
    const size = badge ? 14 : 200;
    return { width: size, height: badge ? 14 : 40, top: 0, left: 0, right: size, bottom: 40, x: 0, y: 0 };
  };

  const chromeMock = makeChrome();
  w.chrome = chromeMock.api;
  w.__JOB_AGENT_TEST__ = true;
  w.eval(fs.readFileSync(path.join(EXT, 'chat.js'), 'utf8'));
  return { w, api: w.__jobAgentChatTest, listeners: chromeMock.listeners };
}

const CHAT_HTML = `<!DOCTYPE html><html><body>
<ul class="session-list">
  <li class="session-item"><span class="name">美云智数 · Java 后端实习生</span><i class="badge unread">2</i></li>
  <li class="session-item"><span class="name">海天味业 · 系统运维</span></li>
  <li class="session-item"><span class="name">库卡机器人 · 嵌入式</span><i class="red-dot unread"></i></li>
  <li class="session-item hidden"><span class="name">已隐藏的会话</span><i class="badge unread">9</i></li>
</ul>
<div class="chat-window"><div class="message-item myself">你好，我想投这个岗位</div></div>
<textarea class="chat-input"></textarea>
</body></html>`;

console.log('== 未读会话识别 ==');
{
  const { api } = bootChat(CHAT_HTML);
  const marks = api.unreadNodes();
  const count = marks.length;
  if (count === 2) ok('识别出 2 个未读会话（数字徽章 + 红点各一个）');
  else bad('识别出 2 个未读会话（数字徽章 + 红点各一个）', `实际 ${count}`);

  const hiddenHit = marks.some((m) => (m.node.textContent || '').includes('已隐藏的会话'));
  if (!hiddenHit) ok('隐藏会话不算未读（否则每次检查都会误报）');
  else bad('隐藏会话不算未读（否则每次检查都会误报）');

  const titles = marks.map((m) => (m.node.textContent || ''));
  if (titles.some((t) => t.includes('美云智数'))) ok('未读项定位到会话而不是徽章本身');
  else bad('未读项定位到会话而不是徽章本身', JSON.stringify(titles));

  const badges = marks.map((m) => m.badge);
  if (badges.includes('2') && badges.includes('●')) ok('徽章内容保留（数字 / 红点）');
  else bad('徽章内容保留（数字 / 红点）', JSON.stringify(badges));
}

// ---------------------------------------------------------------- 列表页
const LIST_HTML = `<!DOCTYPE html><html><body>
<header><div class="search-input">
  <input id="search_input" type="text" placeholder="搜索职位">
  <button class="search-btn">搜索</button>
</div></header>
<div class="job-list">
  <div class="job-card"><a href="/job_detail/aaa.html"><span class="job-name">Java 开发</span></a><div>佛山 · 10-15K</div></div>
  <div class="job-card"><a href="/job_detail/bbb.html"><span class="job-name">后端开发</span></a><div>广州 · 12-18K</div></div>
</div>
</body></html>`;

function bootContent() {
  const dom = new JSDOM(LIST_HTML, {
    url: 'https://www.zhipin.com/web/geek/job?query=Java',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  Object.defineProperty(w.HTMLElement.prototype, 'innerText', {
    get() { return this.textContent; },
    set(v) { this.textContent = v; },
  });
  w.Element.prototype.getBoundingClientRect = () => ({
    width: 200, height: 40, top: 0, left: 0, right: 200, bottom: 40, x: 0, y: 0,
  });
  const chromeMock = makeChrome();
  w.chrome = chromeMock.api;
  w.fetch = async () => ({ ok: true, json: async () => ({}) });
  w.eval(fs.readFileSync(path.join(EXT, 'lib.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(EXT, 'content.js'), 'utf8'));
  return { w, listeners: chromeMock.listeners };
}

function sendTo(listeners, msg) {
  return new Promise((resolve) => {
    let done = false;
    for (const fn of listeners) {
      fn(msg, {}, (resp) => {
        if (!done) { done = true; resolve(resp); }
      });
    }
    setTimeout(() => { if (!done) { done = true; resolve(null); } }, 500);
  });
}

(async () => {
  console.log('\n== 换搜索词 ==');
  const { w, listeners } = bootContent();
  const box = w.document.getElementById('search_input');

  const events = [];
  box.addEventListener('input', () => events.push('input'));
  box.addEventListener('change', () => events.push('change'));
  box.addEventListener('keydown', (e) => { if (e.key === 'Enter') events.push('enter'); });

  const resp = await sendTo(listeners, { type: 'search-keyword', keyword: 'Java 实习生' });

  if (resp && resp.ok) ok('search-keyword 返回成功');
  else bad('search-keyword 返回成功', JSON.stringify(resp));

  if (box.value === 'Java 实习生') ok('搜索框的值被改成目标关键词');
  else bad('搜索框的值被改成目标关键词', box.value);

  // 这一条最关键：只改 value 不派发事件，React/Vue 的 onChange 不会触发，
  // 按回车搜的还是旧词，而页面上看不出任何异常
  if (events.includes('input')) ok('派发了 input 事件（受控组件才会响应）');
  else bad('派发了 input 事件（受控组件才会响应）', JSON.stringify(events));

  if (events.includes('enter')) ok('派发了回车事件');
  else bad('派发了回车事件', JSON.stringify(events));

  console.log('\n== 搜索框缺失时的降级 ==');
  {
    const dom = new JSDOM('<html><body><div>没有搜索框的页面</div></body></html>', {
      url: 'https://www.zhipin.com/web/geek/job',
      runScripts: 'outside-only',
      pretendToBeVisual: true,
    });
    const w2 = dom.window;
    Object.defineProperty(w2.HTMLElement.prototype, 'innerText', {
      get() { return this.textContent; }, set(v) { this.textContent = v; },
    });
    w2.Element.prototype.getBoundingClientRect = () => ({
      width: 200, height: 40, top: 0, left: 0, right: 200, bottom: 40, x: 0, y: 0,
    });
    const cm = makeChrome();
    w2.chrome = cm.api;
    w2.fetch = async () => ({ ok: true, json: async () => ({}) });
    w2.eval(fs.readFileSync(path.join(EXT, 'lib.js'), 'utf8'));
    w2.eval(fs.readFileSync(path.join(EXT, 'content.js'), 'utf8'));

    const r = await sendTo(cm.listeners, { type: 'search-keyword', keyword: 'Java' });
    if (r && r.ok === false && /搜索框/.test(r.reason || '')) ok('找不到搜索框时给出可读原因');
    else bad('找不到搜索框时给出可读原因', JSON.stringify(r));
  }

  console.log('\n== background 接线 ==');
  {
    const bg = fs.readFileSync(path.join(EXT, 'background.js'), 'utf8');
    for (const type of ['discover-start', 'chat-now', 'chat-schedule', 'search-keyword']) {
      const re = new RegExp(`['"]${type}['"]`);
      if (re.test(bg) || fs.readFileSync(path.join(EXT, 'content.js'), 'utf8').includes(`'${type}'`)) {
        ok(`已接上 ${type}`);
      } else {
        bad(`已接上 ${type}`);
      }
    }
    if (/CHAT_ALARM/.test(bg) && /periodInMinutes/.test(bg)) ok('聊天监控注册了周期任务');
    else bad('聊天监控注册了周期任务');

    // 只有 HR 新说的才弹通知。混着自己发的消息弹，两次之后就没人点通知了
    if (/addedHr > 0/.test(bg)) ok('通知按 HR 新增条数触发，不是按总新增');
    else bad('通知按 HR 新增条数触发，不是按总新增');

    if (/chrome\.notifications\.onClicked/.test(bg)) ok('通知可点击跳回聊天页');
    else bad('通知可点击跳回聊天页');
  }

  console.log(`\n${pass}/${pass + fail} 通过`);
  process.exit(fail === 0 ? 0 : 1);
})();
