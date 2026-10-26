/**
 * 聊天页读取逻辑的离线测试。
 *
 * 背景：智联 IM 是 i.zhaopin.com/im（Vue + 网易云信），类名全是构建产物。
 * 实测 message-item / chat-item / chat-msg / msg-item 四个候选全为 0 个节点，
 * 光靠猜类名永远修不完。于是加了 readByLayout：以 textarea 为锚点向上找聊天区、
 * 再找可滚动容器、再逐层下钻取消息项，最后用横向位置判说话人。
 *
 * 这个文件用 jsdom 造两种页面验证它：
 *   1) 智联式：无语义类名，只能靠结构
 *   2) BOSS 式：有 .message-item，应优先走选择器路径，不被结构识别带偏
 *
 * 用法：node scripts/check_extension_chat.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
let pass = 0;
let fail = 0;
const ok = (n) => { pass++; console.log(`[PASS] ${n}`); };
const bad = (n, d) => { fail++; console.log(`[FAIL] ${n}\n        ${d}`); };

function boot(html, url = 'https://i.zhaopin.com/im?sessionId=abc') {
  const dom = new JSDOM(`<!DOCTYPE html><body>${html}</body>`, {
    url,
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;

  // jsdom 没有 layout：innerText 恒为空、scrollHeight 恒为 0。
  // innerText 要按块级元素补换行 —— 直接用 textContent 会把「正文」和「送达」
  // 粘成一行，噪声过滤就失效了（浏览器里它们是两个块）
  const BLOCK_TAG = /^(DIV|P|LI|UL|OL|SECTION|ARTICLE|H[1-6]|TR|TD|SPAN)$/;
  Object.defineProperty(w.HTMLElement.prototype, 'innerText', {
    get() {
      const walk = (n) => {
        if (n.nodeType === 3) return n.nodeValue;
        if (n.nodeType !== 1) return '';
        if (n.tagName === 'BR') return '\n';
        let s = '';
        for (const c of n.childNodes) s += walk(c);
        return BLOCK_TAG.test(n.tagName) && s.trim() ? s + '\n' : s;
      };
      return walk(this).replace(/\n{2,}/g, '\n');
    },
    set(v) { this.textContent = v; },
  });
  Object.defineProperty(w.HTMLElement.prototype, 'scrollHeight', {
    get() { return this.hasAttribute('data-scroll') ? 400 : 200; },
  });
  Object.defineProperty(w.HTMLElement.prototype, 'clientHeight', {
    get() { return 200; },
  });
  // 靠右 = 自己发的。用 class 里有没有 self 来模拟左右布局
  w.Element.prototype.getBoundingClientRect = function () {
    const self = /self/.test(this.className || '');
    return {
      left: self ? 300 : 20, width: 260, top: 0, height: 40,
      right: self ? 560 : 280, bottom: 40, x: self ? 300 : 20, y: 0,
    };
  };

  w.chrome = {
    runtime: {
      id: 'test',
      getURL: (p) => p,
      sendMessage: async () => ({ ok: true }),
      // 后台扫描会给聊天页发 sync-chat-now，所以 chat.js 必须能监听；
      // mock 里漏了它会让整个脚本在顶层求值时直接崩，看起来像"脚本写错了"
      onMessage: { addListener() {} },
      lastError: null,
    },
    storage: { local: { get: async () => ({}), set: async () => ({}) } },
  };
  w.__JOB_AGENT_TEST__ = true;
  w.eval(fs.readFileSync(path.join(ROOT, 'extension', 'chat.js'), 'utf8'));
  return w.__jobAgentChatTest;
}

// ---------- 1. 智联式结构：没有任何语义类名 ----------

const zhilianHtml = `
<div id="root">
  <div class="d1">
    <div class="d2">会话列表 刘瑞桓 詹女士 7</div>
    <div class="d3">
      <div class="d4">詹女士 美的集团 java后端工程师</div>
      <div class="d5" data-scroll="1">
        <div class="k1"><span class="t">16:29</span></div>
        <div class="k2"><div class="b">您好，我对贵司【java后端工程师】很感兴趣，请您看下我的简历</div></div>
        <div class="k3 self"><div class="b">谢谢，简历已经发过去了</div><span class="t">送达</span></div>
        <div class="k4"><div class="b">my-resume.pdf</div></div>
        <div class="k5"><div class="b">好，那我这边稍后将您的简历送去业务部门审核，有进展会通知您</div></div>
      </div>
      <div class="d6"><textarea></textarea><button>发送</button></div>
    </div>
  </div>
</div>`;

const api = boot(zhilianHtml);
const layout = api.readByLayout();

if (layout.length >= 2) ok(`智联式结构能读出消息（${layout.length} 条）`);
else bad('智联式结构能读出消息', `只读到 ${layout.length} 条内容`);

const texts = layout.map((m) => m.text).join(' | ');
if (/java后端工程师/.test(texts) && /送去业务部门审核/.test(texts)) ok('正文完整');
else bad('正文完整', `实际: ${texts.slice(0, 160)}`);

if (!/16:29|送达|\.pdf/.test(texts)) ok('时间戳/状态/附件名被过滤');
else bad('时间戳/状态/附件名被过滤', `实际: ${texts.slice(0, 160)}`);

const roles = layout.map((m) => m.role);
if (roles.includes('我') && roles.includes('HR')) ok(`能区分说话人（${roles.join(',')}）`);
else bad('能区分说话人', `实际: ${roles.join(',')}`);

// readMessages 在没有选择器命中时必须自动走结构识别
const msgs = api.readMessages();
if (msgs.length === layout.length) ok('readMessages 自动回退到结构识别');
else bad('readMessages 自动回退到结构识别', `readMessages=${msgs.length} layout=${layout.length}`);

// ---------- 2. BOSS 式结构：有选择器命中，不应走结构识别 ----------

const bossHtml = `
<div class="chat">
  <div class="chat-message-list">
    <div class="message-item myself"><div class="text">您好，我想了解一下这个岗位</div></div>
    <div class="message-item"><div class="text">你之前做过交易系统吗</div></div>
  </div>
  <div class="chat-input"><textarea></textarea></div>
</div>`;

const boss = boot(bossHtml);
const bySel = boss.readBySelectors();
if (bySel.length === 2) ok('BOSS 式结构走选择器路径（2 条）');
else bad('BOSS 式结构走选择器路径', `读到 ${bySel.length} 条`);
if (bySel.some((m) => m.role === '我')) ok('myself 类名判为自己');
else bad('myself 类名判为自己', JSON.stringify(bySel));

// ---------- 3. 空页面不应误报 ----------

const empty = boot('<div><textarea></textarea></div>');
const none = empty.readMessages();
if (none.length === 0) ok('空页面不误报消息');
else bad('空页面不误报消息', JSON.stringify(none));

// ---------- 4. 猎聘沟通页 c.liepin.com ----------
// 这是实测踩过的坑：路径就是 "/"，既没有 chat 字样，消息容器类名也不匹配，
// 结果 chat 面板根本不挂，用户在沟通页上什么都看不到

const liepinBlank = boot('<div id="app"></div>', 'https://c.liepin.com/?time=1792661737086');
if (liepinBlank.looksLikeChatPage()) ok('c.liepin.com 被识别为聊天页（即使路径只有 /）');
else bad('c.liepin.com 被识别为聊天页', 'looksLikeChatPage 返回 false，面板不会挂');

const liepinWithInput = boot(
  '<div class="im-wrap"><div class="im-body"></div><div class="im-input"><textarea></textarea></div></div>',
  'https://c.liepin.com/?time=123'
);
if (liepinWithInput.chatReady()) ok('c.liepin.com 有 textarea 时 chatReady 成立');
else bad('c.liepin.com 有 textarea 时 chatReady 成立', '未识别到输入框');

// ---------- 5. content.js 在聊天页不该挂岗位面板 ----------
// 实测在 c.liepin.com 上，content.js 挂了「批量抓取」——那不是岗位列表页，
// 点上也没用，还会和聊天面板叠在一起

function bootContent(html, url) {
  const dom = new JSDOM(`<!DOCTYPE html><body>${html}</body>`, {
    url,
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  const BLOCK_TAG = /^(DIV|P|LI|UL|OL|SECTION|ARTICLE|H[1-6]|TR|TD|SPAN)$/;
  Object.defineProperty(w.HTMLElement.prototype, 'innerText', {
    get() {
      const walk = (n) => {
        if (n.nodeType === 3) return n.nodeValue;
        if (n.nodeType !== 1) return '';
        if (n.tagName === 'BR') return '\n';
        let s = '';
        for (const c of n.childNodes) s += walk(c);
        return BLOCK_TAG.test(n.tagName) && s.trim() ? s + '\n' : s;
      };
      return walk(this).replace(/\n{2,}/g, '\n');
    },
    set(v) { this.textContent = v; },
  });
  w.Element.prototype.getBoundingClientRect = () => ({
    width: 600, height: 400, top: 0, left: 0, right: 600, bottom: 400, x: 0, y: 0,
  });
  w.chrome = {
    // content.js 现在会注册 scrape-list 监听（供后台定时扫描调用），
    // mock 少一个 onMessage 就会在注入时直接抛错，整套测试全挂
    runtime: {
      id: 'test', getURL: (p) => p, sendMessage: async () => ({ ok: true }),
      onMessage: { addListener() {} }, lastError: null,
    },
    storage: { local: { get: async () => ({}), set: async () => ({}) } },
  };
  w.eval(fs.readFileSync(path.join(ROOT, 'extension', 'lib.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(ROOT, 'extension', 'content.js'), 'utf8'));
  return w;
}

const chatWin = bootContent('<div id="app"></div>', 'https://c.liepin.com/?time=1');
const jobListWin = bootContent('<div id="app"></div>', 'https://www.liepin.com/zhaopin/?key=java');

setTimeout(() => {
  const shadowCount = (w) =>
    [...w.document.documentElement.children].filter((el) => el.shadowRoot).length;

  if (shadowCount(chatWin) === 0) ok('content.js 在猎聘沟通页不挂岗位面板');
  else bad('content.js 在猎聘沟通页不挂岗位面板', `挂了 ${shadowCount(chatWin)} 个`);

  if (shadowCount(jobListWin) === 1) ok('content.js 在猎聘列表页仍然正常挂载');
  else bad('content.js 在猎聘列表页仍然正常挂载', `挂了 ${shadowCount(jobListWin)} 个`);

  console.log(`\n${pass}/${pass + fail} 通过`);
  process.exit(fail ? 1 : 0);
}, 300);
