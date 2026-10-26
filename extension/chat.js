/* 小新求职 · HR 沟通面板
 *
 * 只在聊天页注入。做三件事：
 *   1. 从当前会话里读出最近几条消息（HR 说什么、你回了什么）
 *   2. 让后端结合岗位与简历事实生成 3 条候选回复
 *   3. 你点一条 → 填进输入框，发送永远由你自己点
 *
 * 为什么不自动发送：回复代表本人在承诺薪资、到岗时间。程序代答说错，承担后果的是求职者。
 */
(() => {
  // 重复注入守卫，理由同 content.js：popup 手动注入与 manifest 自动注入会撞车
  if (globalThis.__jobAgentChatReady) return;
  globalThis.__jobAgentChatReady = true;

  const VERSION = '0.7.0';
  const API_TIMEOUT = 90000;
  // 聊天框是 SPA 异步渲染的，document_idle 时可能还没出来。
  // 之前是"注入即挂 UI"，换到现在按 URL 全站注入后，必须先等页面长出聊天框
  const MOUNT_WAIT_MS = 20000;

  // 各平台聊天页的消息容器与输入框。结构会变，所以每个都给多个候选
  const MESSAGE_SELECTORS = {
    'zhipin.com': [
      '.chat-message-list .message-item',
      '.chat-record-list .message-item',
      '[class*="message-item"]',
      '[class*="chat-message"]',
    ],
    'liepin.com': [
      '.im-chat-list .im-chat-item',
      '[class*="chat-item"]',
      '[class*="message-item"]',
    ],
    'zhaopin.com': [
      // 智联 IM 是 i.zhaopin.com/im（Vue + 网易云信），类名全是构建产物。
      // 实测 message-item / chat-item / chat-msg / msg-item 全为 0，
      // 所以这里补了更宽的候选；再不中就靠 readByLayout 的结构识别兜底
      '.chat-content .message-item',
      '[class*="message-item"]',
      '[class*="chat-item"]',
      '[class*="chat-msg"]',
      '[class*="msg-item"]',
      '[class*="message"]',
      '[class*="msg-content"]',
      '[class*="bubble"]',
    ],
  };
  const INPUT_SELECTORS = {
    'zhipin.com': ['#chat-input', '.chat-input', '[contenteditable="true"]'],
    'liepin.com': [
      '.im-input textarea',
      '.im-input [contenteditable="true"]',
      'textarea',
      // 实测 c.liepin.com 上连 textarea 都没有（可能是没点开会话，也可能
      // 输入区是富文本容器）。裸的 contenteditable 必须单独试一次，
      // 只测 .im-input 下的会漏
      '[contenteditable="true"]',
      '[contenteditable=""]',
    ],
    'zhaopin.com': [
      '.chat-input textarea',
      '.chat-input [contenteditable="true"]',
      'textarea',
      '[contenteditable="true"]',
    ],
  };
  const TITLE_SELECTORS = [
    // 智联聊天里的职位卡片实测类名（2026-10-22 从 sou.zhaopin.com 线上 HTML 确认）
    '.c-chat-job__title',
    '.chat-job-name',
    '[class*="job-name"]',
    '[class*="job-title"]',
    'h3',
  ];

  const host = (() => {
    const h = location.hostname.replace(/^www\./, '');
    // 智联有 zhaopin.cn 这个备用域，选择器与 zhaopin.com 一致，归一到同一张表
    return h.endsWith('zhaopin.cn') ? h.replace(/zhaopin\.cn$/, 'zhaopin.com') : h;
  })();
  const pick = (map) => {
    for (const [domain, list] of Object.entries(map)) {
      if (host.endsWith(domain)) return list;
    }
    return [];
  };

  let shadow = null;
  let busy = false;
  // 当前选中的简历 id。为 0 时后端会退回到"最新一份"——
  // 那是错简历的来源：实测用了示例简历，回复里冒出用户自己没有的大厂经历
  let resumeId = 0;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function buildUi() {
    if (shadow) return shadow;

    const hostEl = document.createElement('div');
    hostEl.id = 'job-agent-chat-host';
    hostEl.style.cssText = 'position:fixed;right:22px;bottom:96px;z-index:2147483000;';
    shadow = hostEl.attachShadow({ mode: 'open' });

    const style = document.createElement('style');
    style.textContent = `
      .fab {
        display:flex;align-items:center;gap:6px;
        background:#007aff;color:#fff;
        border:0;border-radius:999px;padding:10px 16px;font:13px system-ui;
        cursor:pointer;box-shadow:0 10px 24px -10px rgba(48,86,211,.8);
      }
      .fab:hover { filter:brightness(1.08); }
      .panel {
        display:none;position:absolute;right:0;bottom:52px;width:340px;
        background:#fff;border:1px solid #e7e8ec;border-radius:14px;
        box-shadow:0 18px 40px -18px rgba(24,26,32,.5);overflow:hidden;
        font:13px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:#1c1e21;
      }
      .panel.show { display:block; }
      .head { display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #eef0f3; }
      .head b { font-size:13px;font-weight:600; }
      .head .ver { color:#9ba0a8;font-size:11px; }
      .head .close { margin-left:auto;border:0;background:transparent;color:#9ba0a8;font-size:16px;cursor:pointer; }
      .resume-bar {
        display:flex;align-items:center;gap:8px;padding:7px 12px;
        border-bottom:1px solid #eef0f3;background:#fafbfd;
      }
      .resume-bar span { font-size:11.5px;color:#6b7078;white-space:nowrap; }
      .resume-bar select {
        flex:1;min-width:0;font:11.5px system-ui;padding:3px 6px;
        border:1px solid #dfe1e7;border-radius:6px;background:#fff;color:#1c1e21;
      }
      .body { padding:10px 12px;max-height:60vh;overflow:auto; }
      .hint { color:#6b7078;font-size:12px;margin:0 0 8px; }
      .card { border:1px solid #e7e8ec;border-radius:10px;padding:9px 11px;margin-bottom:8px;background:#fafbfd; }
      .card:hover { border-color:#c9ccd4;background:#fff; }
      .card .tag { font-size:10.5px;color:#0062cc;background:rgba(0,122,255,.10);border-radius:999px;padding:1px 8px; }
      .card .text { margin:6px 0 4px;font-size:12.5px;line-height:1.65;white-space:pre-wrap; }
      .card .why { font-size:11px;color:#9ba0a8; }
      .card .ops { display:flex;gap:12px;margin-top:6px; }
      .card .ops button { border:0;background:transparent;padding:0;color:#007aff;font-size:11.5px;cursor:pointer; }
      .msg { color:#6b7078;font-size:12px;padding:6px 0; }
      .msg.err { color:#b3372f; }
      .diag { font:11px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;max-height:180px;overflow:auto;background:#fafbfd;border:1px dashed #d8dae0;border-radius:8px;padding:8px;color:#6b7078; }
    `;
    shadow.appendChild(style);

    const fab = el('button', 'fab', '帮我回复');
    const panel = el('div', 'panel');
    panel.innerHTML = `
      <div class="head"><b>帮我回复</b><span class="ver">v${VERSION}</span>
        <button class="close" title="关闭">×</button></div>
      <div class="resume-bar">
        <span>用哪份简历</span>
        <select class="resume-sel"></select>
      </div>
      <div class="body"></div>`;
    shadow.appendChild(fab);
    shadow.appendChild(panel);

    const body = panel.querySelector('.body');
    const resumeSel = panel.querySelector('.resume-sel');
    fab.addEventListener('click', () => {
      panel.classList.toggle('show');
      if (panel.classList.contains('show')) {
        // 每次打开都刷一次：网页端可能刚上传了新简历
        loadResumeList(resumeSel);
        run(body);
      }
    });
    panel.querySelector('.close').addEventListener('click', () => panel.classList.remove('show'));
    resumeSel.addEventListener('change', async () => {
      resumeId = Number(resumeSel.value) || 0;
      try {
        await chrome.storage.local.set({ resumeId });
      } catch {
        /* 存不下就只在本次生效 */
      }
    });

    hostEl.style.setProperty('display', 'block');
    document.documentElement.appendChild(hostEl);
    setBody(body);
    return shadow;
  }

  let currentBody = null;
  function setBody(body) { currentBody = body; }
  function say(text, kind) {
    if (!currentBody) return;
    const box = el('div', 'msg' + (kind ? ' ' + kind : ''), text);
    currentBody.prepend(box);
  }

  // ---------- 读页面 ----------

  // 时间戳、已读状态、按钮、附件名。这些在页面上和消息混在一起，
  // 当成对话内容传给模型会让它误以为 HR 说过这些话
  const NOISE_LINE = [
    /^\d{1,2}:\d{2}$/,
    /^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$/,
    /^星期[一二三四五六日天]$/,
    /^(昨天|今天|前天)$/,
    /^(送达|已读|未读|发送中|撤回)$/,
    /^(发简历|换微信|查看详情|发微简历|继续沟通|举报)$/,
    /\.(pdf|docx?|jpe?g|png)$/i,
  ];

  function isNoiseLine(text) {
    const t = String(text).trim();
    if (!t) return true;
    if (t.length > 300) return true;
    return NOISE_LINE.some((re) => re.test(t));
  }

  function readBySelectors() {
    const out = [];
    for (const sel of pick(MESSAGE_SELECTORS)) {
      const nodes = document.querySelectorAll(sel);
      if (!nodes.length) continue;
      nodes.forEach((node) => {
        const text = (node.innerText || '').trim();
        if (!text) return;
        // 消息节点常把时间、状态混在一起，取最后一行作为正文
        const lines = text.split('\n').map((s) => s.trim()).filter((s) => s && !isNoiseLine(s));
        const content = lines[lines.length - 1] || '';
        if (content.length < 1) return;
        const self = /myself|self|right|is-me|mine/.test(node.className || '');
        out.push({ role: self ? '我' : 'HR', text: content.slice(0, 200) });
      });
      if (out.length) break;
    }
    return out;
  }

  /**
   * 结构兜底：不认任何类名，只认布局。
   *
   * 为什么需要：智联 IM 是 Vue + 网易云信，类名全是构建产物，实测
   * message-item / chat-item / chat-msg / msg-item 一个都不存在。
   * 猜类名这条路走不通，改成：
   *   1) 把发送框当锚点（textarea 是最稳的，各家都有）
   *   2) 向上找到"文本够多"的祖先，即聊天区域
   *   3) 在里面找可滚动的后代，那就是消息列表
   *   4) 逐层下钻到"有多个带文本子元素"的那一层，那些子元素就是消息项
   *   5) 用元素中心的横坐标相对容器中线判断谁说的
   */
  function findChatRegion(anchor) {
    // 阈值不能太高：会话消息少的时候（"已发简历" + "好的"）整个聊天区
    // 可能不到 200 字，取不到就会误报"读不到消息"。80 字足够把
    // 输入框那一小块排除掉，又不会漏掉短会话
    let node = anchor && anchor.parentElement;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      if ((node.innerText || '').trim().length > 80) return node;
    }
    return null;
  }

  function domDepth(el) {
    let d = 0;
    for (let n = el; n; n = n.parentElement) d++;
    return d;
  }

  function findScroller(region) {
    // 关键：要选**最深**的可滚动容器，而不是文本最长的。
    // 页面外层（#root / body）往往也可滚动且文本最多，选它会把头部、
    // 会话列表、发送区一起当成消息。消息列表一定是嵌套更深的那个
    let best = null;
    let bestDepth = -1;
    for (const el of region.querySelectorAll('*')) {
      if (el.scrollHeight <= el.clientHeight + 20) continue;
      if ((el.innerText || '').trim().length < 30) continue;
      const depth = domDepth(el);
      if (depth > bestDepth) {
        best = el;
        bestDepth = depth;
      }
    }
    if (best) return best;
    // 拿不到滚动尺寸时（例如布局还没算完），退而取"文本最多的直接子元素"。
    // 直接把 region 当消息列表是错的：region 里还包含发送区，
    // 逐层下钻的第一层会把整个消息列表当成一条消息
    const kids = [...region.children].filter((el) => (el.innerText || '').trim());
    kids.sort((a, b) => (b.innerText || '').length - (a.innerText || '').length);
    return kids[0] || null;
  }

  function digMessageItems(container, depth = 0) {
    if (!container || depth > 4) return [];
    const kids = [...container.children].filter((el) => (el.innerText || '').trim());
    if (kids.length >= 2) return kids;
    if (kids.length === 1) return digMessageItems(kids[0], depth + 1);
    return [];
  }

  function readByLayout() {
    const anchor = findInput();
    const region = findChatRegion(anchor);
    if (!region) return [];

    const scroller = findScroller(region);
    if (!scroller) return [];
    const items = digMessageItems(scroller);
    if (!items.length) return [];

    const box = scroller.getBoundingClientRect();
    const mid = box.left + box.width / 2;
    const out = [];
    for (const el of items) {
      const raw = (el.innerText || '').trim();
      if (!raw) continue;
      const lines = raw
        .split('\n')
        .map((s) => s.trim())
        .filter((s) => s && !isNoiseLine(s));
      if (!lines.length) continue;
      const text = lines.join(' ').slice(0, 200);
      // 时间戳和按钮已经滤掉，剩下的整块就是一条消息。
      // 左右位置判说话人：自己发的气泡靠右
      const rect = el.getBoundingClientRect();
      const center = rect.left + rect.width / 2;
      const self = rect.width > 0 && center > mid;
      out.push({ role: self ? '我' : 'HR', text });
    }
    return out;
  }

  function readMessages() {
    const bySelector = readBySelectors();
    if (bySelector.length) return bySelector.slice(-8);
    // 选择器全失配时不要直接报"读不到"，先按结构试一次
    return readByLayout().slice(-8);
  }

  /** 是否真的读到了对话，用于决定要不要给用户报错 */
  function hasMessages() {
    return readMessages().length > 0;
  }

  function readJobTitle() {
    for (const sel of TITLE_SELECTORS) {
      const node = document.querySelector(sel);
      const text = node && (node.innerText || '').trim();
      if (text && text.length <= 40) return text;
    }
    return '';
  }

  // 聊天页右侧通常挂着岗位卡片（标题 + 薪资 + 城市 + 部分 JD）。
  // 只传标题时后端拿不到岗位在要求什么，生成的回复会变成复读简历技能——
  // 实测在非技术岗上会直接写出"我主要做后端，Python/Go/MySQL"这种群发句子。
  const JOB_PANEL_SELECTORS = [
    // 智联：c-chat-job 是聊天窗口里的职位卡片（线上 HTML 实测存在）
    '.c-chat-job__content',
    '.c-chat-job',
    '[class*="job-info"]',
    '[class*="job-card"]',
    '[class*="job-detail"]',
    '[class*="position-info"]',
    '[class*="job-summary"]',
    '[class*="job-content"]',
  ];

  /**
   * 读当前岗位的正文。
   *
   * 关键约束：**内容必须包含当前岗位标题**。
   * 猎聘的沟通页右侧是岗位卡片，但页面底部还有一整片「猜你喜欢」推荐岗位
   * （实测混进来一个 35-65k 的资深 Java 岗），泛类名选择器会把它们一起圈进来，
   * 结果模型以为当前岗位是那个 35-65k 的，回复全跑偏。
   *
   * 拿不到标题时宁可不传：抓错比不抓更糟。
   */
  function readJobContext() {
    const title = readJobTitle();
    if (!title) return '';

    const cands = [];
    for (const sel of JOB_PANEL_SELECTORS) {
      for (const node of document.querySelectorAll(sel)) {
        const text = (node.innerText || '').trim();
        if (text.length < 12 || text.length > 1200) continue;
        if (!text.includes(title)) continue;
        cands.push(text);
      }
      if (cands.length) break;
    }
    if (!cands.length) return title;
    // 取最短的那个：它是最贴近岗位卡片的容器，不会把周边的推荐区一起圈进来
    cands.sort((a, b) => a.length - b.length);
    return cands[0].slice(0, 900);
  }

  function findInput() {
    for (const sel of pick(INPUT_SELECTORS)) {
      const node = document.querySelector(sel);
      if (node) return node;
    }
    return null;
  }

  function fillInput(text) {
    const node = findInput();
    if (!node) return false;
    if (node.tagName === 'TEXTAREA' || node.tagName === 'INPUT') {
      node.focus();
      const setter = Object.getOwnPropertyDescriptor(
        node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
        'value'
      ).set;
      setter.call(node, text);
      node.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    }
    if (node.isContentEditable) {
      node.focus();
      node.textContent = text;
      node.dispatchEvent(new InputEvent('input', { bubbles: true, data: text }));
      return true;
    }
    return false;
  }

  async function callApi(path, body, method) {
    // 扩展刚更新过时 chrome.runtime 已被销毁，sendMessage 会同步抛错（不是走 lastError）
    if (!chrome.runtime || !chrome.runtime.id) {
      throw new Error('扩展已更新，请按 F5 刷新本页');
    }
    let resp;
    try {
      resp = await chrome.runtime.sendMessage({
        type: 'api',
        path,
        method: method || 'POST',
        body,
      });
    } catch {
      throw new Error('扩展已更新，请按 F5 刷新本页');
    }
    if (!resp) throw new Error('后台无响应，请重新加载扩展');
    if (!resp.ok) throw new Error(resp.error || '请求失败');
    return resp.data;
  }

  /**
   * 拉简历列表填充下拉框。
   *
   * 之前扩展根本不传 resume_id，后端只能取"最新一份"。
   * 用户库里同时有示例简历和自己上传的，取到示例简历后，
   * 回复里就会出现他本人没有的经历 —— 发给 HR 等于自曝造假。
   */
  async function loadResumeList(sel) {
    try {
      const data = await callApi('/api/resumes', undefined, 'GET');
      const usable = (data.items || []).filter((r) => r.has_profile);
      if (!usable.length) {
        sel.innerHTML = '<option value="0">（还没有解析好的简历）</option>';
        resumeId = 0;
        return;
      }
      const stored = await chrome.storage.local.get('resumeId');
      const saved = Number(stored.resumeId) || 0;
      sel.innerHTML = usable
        .map((r) => `<option value="${r.id}">#${r.id} ${escapeHtml(r.file_name)}</option>`)
        .join('');
      const chosen = usable.some((r) => r.id === saved) ? saved : usable[0].id;
      sel.value = String(chosen);
      resumeId = chosen;
    } catch (e) {
      sel.innerHTML = `<option value="0">（读取失败：${escapeHtml(e.message)}）</option>`;
    }
  }

  // ---------- 主流程 ----------

  async function run(body) {
    if (busy) return;
    body.innerHTML = '<div class="msg">正在读聊天记录…</div>';

    const messages = readMessages();
    const title = readJobTitle();
    const jobText = readJobContext();
    if (!messages.length) {
      body.innerHTML = `
        <p class="hint">没读到聊天消息。先确认两件事：<br>
        1) 左侧会话列表里点开一个具体的 HR 会话（没点开时右侧聊天区是空的）；<br>
        2) 页面结构可能变了，点下面的诊断把信息发我。</p>
        <div class="diag">${escapeHtml(diagnose())}</div>`;
      return;
    }

    // 告诉用户这次是靠类名匹配还是靠结构识别读到的。
    // 结构识别是兜底路径，读出来的条数可能偏少，用户有权知道
    const viaLayout = readBySelectors().length === 0;
    const jobHint = title
      ? ` · ${escapeHtml(title)}${jobText ? ` · 岗位正文 ${jobText.length} 字` : ' · 未读到岗位正文'}`
      : '';
    const modeHint = viaLayout ? ' · 结构识别' : '';
    body.innerHTML = `<div class="msg">读到 ${messages.length} 条消息${modeHint}${jobHint}，生成中…</div>`;

    busy = true;
    try {
      // 同步聊天记录到后端（工作台要展示「跟这家 HR 聊到哪了」）。
      // 后端按 (平台, 岗位标题, 角色, 内容) 去重，重复同步不会翻倍。
      // 失败静默：同步失败不该挡住本次生成回复
      try {
        await callApi('/api/chat-logs/sync', {
          platform: location.hostname,
          page_url: location.href,
          job_title: title || null,
          messages: messages.map((m) => ({ role: m.role, text: m.text })),
        });
      } catch { /* 忽略同步失败 */ }

      const data = await callApi('/api/reply/suggest', {
        messages,
        job_title: title || null,
        job_text: jobText || null,
        resume_id: resumeId || null,
      });
      renderCandidates(body, data.candidates || [], data);
    } catch (err) {
      body.innerHTML = `<div class="msg err">${escapeHtml(err.message)}</div>`;
    } finally {
      busy = false;
    }
  }

  function renderCandidates(body, candidates, meta) {
    const label = { concise: '简洁', formal: '正式', clarify: '追问' };
    // 一定要显示用的是哪份简历：扩展没指定时后端取的是"最新一份"，
    // 很可能不是你想用的（实测用了示例简历，回复里冒出用户自己没有的大厂经历）
    const resumeLine = meta && meta.resume_name
      ? `<p class="hint">依据简历：<b>${escapeHtml(meta.resume_name)}</b>${
          /sample|示例|demo/i.test(meta.resume_name)
            ? ' —— 这是示例简历，不是你上传的那份，去网页端选对简历后重新生成'
            : ''
        }</p>`
      : '';
    body.innerHTML = `${resumeLine}<p class="hint">点「填入输入框」后自己确认再发送，程序不会替你发。</p>`;
    for (const c of candidates) {
      const card = el('div', 'card');
      card.innerHTML = `
        <span class="tag">${label[c.style] || c.style}</span>
        <div class="text">${escapeHtml(c.text)}</div>
        ${c.why ? `<div class="why">依据：${escapeHtml(c.why)}</div>` : ''}
        <div class="ops">
          <button data-act="fill">填入输入框</button>
          <button data-act="copy">复制</button>
        </div>`;
      card.querySelector('[data-act="fill"]').addEventListener('click', () => {
        const ok = fillInput(c.text);
        say(ok ? '已填入，确认后按回车发送' : '没找到输入框，请用「复制」手动粘贴', ok ? '' : 'err');
      });
      card.querySelector('[data-act="copy"]').addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(c.text);
          say('已复制');
        } catch {
          say('复制失败，请手动选中', 'err');
        }
      });
      body.appendChild(card);
    }
  }

  function diagnose() {
    const lines = [`诊断 v${VERSION}`, `URL: ${location.href}`];
    for (const sel of pick(MESSAGE_SELECTORS)) {
      const n = document.querySelectorAll(sel).length;
      lines.push(`消息候选 ${sel} -> ${n} 个`);
    }
    for (const sel of pick(INPUT_SELECTORS)) {
      lines.push(`输入框候选 ${sel} -> ${document.querySelector(sel) ? '命中' : '无'}`);
    }
    const first = document.querySelector(pick(MESSAGE_SELECTORS)[0] || '');
    if (first) {
      lines.push('首条消息 outerHTML（前 600 字）:');
      lines.push(first.outerHTML.slice(0, 600));
    }
    // 消息到底读不读得到，是"面板能用不能用"的分水岭。
    // 把两条路径各自的结果都打出来，一眼看出该修选择器还是修结构识别
    lines.push('--- 消息读取 ---');
    lines.push(`选择器路径: ${readBySelectors().length} 条`);
    const anchor = findInput();
    const region = findChatRegion(anchor);
    lines.push(`发送框: ${anchor ? anchor.tagName.toLowerCase() : '未找到'}`);
    // 输入框在 iframe 里时本页面查不到任何聊天节点，需要单独告诉用户
    const frames = document.querySelectorAll('iframe');
    lines.push(`同一文档内 iframe 数: ${frames.length}${frames.length ? '（聊天可能在 iframe 里）' : ''}`);
    lines.push(`contenteditable 元素: ${document.querySelectorAll('[contenteditable]').length}`);
    lines.push(`聊天区域: ${region ? `${region.tagName.toLowerCase()}.${String(region.className).slice(0, 60)}` : '未找到'}`);
    const scroller = region ? findScroller(region) : null;
    lines.push(
      `可滚动容器: ${
        scroller
          ? `${scroller.tagName.toLowerCase()}.${String(scroller.className).slice(0, 60)} / ${scroller.children.length} 个子元素`
          : '未找到'
      }`
    );
    const byLayout = readByLayout();
    lines.push(`结构识别路径: ${byLayout.length} 条`);
    if (byLayout.length) {
      lines.push('最近 3 条:');
      for (const m of byLayout.slice(-3)) lines.push(`  ${m.role}: ${m.text.slice(0, 60)}`);
    }
    // 岗位卡片没抓到就等于在"无岗位"状态下生成回复，HR 一眼看出是群发。
    // 所以把探测结果一起打出来，命中哪个容器能直接看出该加哪个选择器
    lines.push('--- 岗位容器探测 ---');
    lines.push(`标题选择器命中: ${readJobTitle() || '(无)'}`);
    for (const sel of JOB_PANEL_SELECTORS) {
      const n = document.querySelectorAll(sel).length;
      const len = n ? (document.querySelector(sel).innerText || '').trim().length : 0;
      lines.push(`岗位候选 ${sel} -> ${n} 个 / 首个体 ${len} 字`);
    }
    lines.push(`readJobContext 结果: ${readJobContext().length} 字`);
    return lines.join('\n');
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // 只在这两个条件之一成立时挂面板。全站注入后不能再"注入即挂"，
  // 否则每个岗位列表页右下角都会多一个聊天按钮
  function chatReady() {
    if (findInput()) return true;
    for (const sel of pick(MESSAGE_SELECTORS)) {
      if (document.querySelector(sel)) return true;
    }
    return false;
  }

  // URL 层面的兜底：即使聊天容器选择器全失配，只要地址像沟通页也要把面板挂出来。
  // 不挂的话用户看到的是"插件消失"，无从取证；挂出来点一次诊断就能定位选择器
  function looksLikeChatPage() {
    const h = location.hostname;
    // 猎聘沟通页是 c.liepin.com（路径就是 "/"，完全不带 chat 字样），
    // 智联是 i.zhaopin.com/im。这类独立子域是最可靠的信号
    if (/^c\.liepin\.com$/.test(h) || /^i\.zhaopin\.com$/.test(h) || /^im\./.test(h)) {
      return true;
    }
    const url = location.pathname + location.search + location.hash;
    return /(?:^|[/?#_-])(?:im|chat|message|messages|conversation|talk|contact)(?:[/?#_-]|$)/i.test(url);
  }

  /**
   * 回复自动检测：定时把当前会话同步到后端。
   *
   * 为什么要它：以前只在用户点「帮我回复」时才同步一次，
   * 结果工作台上的「HR 沟通记录」永远是上次手动点开时的样子 ——
   * HR 回了什么，除非你再点一次面板，否则系统不知道。
   *
   * 只读不写：只把页面上已有的消息发到本地后端，不点发送、不碰输入框，
   * 所以没有封号风险。60 秒一拍，对页面几乎无感。
   */
  let autoSyncTimer = null;
  let liveObserver = null;
  let liveDebounce = null;

  async function syncTick() {
    try {
      const messages = readMessages();
      const title = readJobTitle();
      // 没读到消息就跳过：可能是会话没选、也可能页面在切换中
      if (!messages.length) return;
      await callApi('/api/chat-logs/sync', {
        platform: location.hostname,
        page_url: location.href,
        job_title: title || null,
        messages: messages.map((m) => ({ role: m.role, text: m.text })),
      });
    } catch {
      // 定时任务失败不打扰用户：后端没启动、扩展刚更新都会走到这里
    }
  }

  /**
   * 实时同步：消息区一有变化就同步，而不是等下一个 60 秒。
   *
   * 为什么之前不够：HR 回了一句，用户在工作台上要最多等一分钟才看得到，
   * 而这个页面他可能只停留十几秒。60 秒轮询对"正在聊"的场景太慢。
   *
   * 保留 60 秒轮询不动：MutationObserver 在会话切换、虚拟列表复用节点时
   * 可能漏掉变化，定时那一拍是兜底。
   */
  function startLiveSync() {
    if (liveObserver) return;
    const anchor = findInput();
    const region = (anchor && findChatRegion(anchor)) || document.body;
    if (!region) return;
    liveObserver = new MutationObserver(() => {
      // 消息区一次性插入多条时会连续触发几十次，1.2 秒内的变化合并成一次
      clearTimeout(liveDebounce);
      liveDebounce = setTimeout(syncTick, 1200);
    });
    try {
      liveObserver.observe(region, { childList: true, subtree: true, characterData: true });
    } catch {
      // 观察失败就只靠定时轮询，不影响功能
      liveObserver = null;
    }
  }

  function startAutoSync() {
    if (autoSyncTimer) return;
    syncTick(); // 进来先同步一次，不用等第一个 60 秒
    autoSyncTimer = setInterval(syncTick, 60000);
    startLiveSync();
  }

  /* ---------- 后台扫描：未读检测 + 主动同步 ----------
   *
   * 之前只有"页面开着才同步"：HR 回了什么，除非你正好在聊天页，否则系统不知道，
   * 也不会提醒。而求职期真正要盯的恰恰是"有没有人回我"。
   *
   * 现在的做法：后台定时开一个标签页打开聊天页 → 抓未读会话 → 逐个点开读消息 →
   * 上报 → 关标签。只读不写，不点发送、不碰输入框。
   *
   * 必须说清的副作用：点开会话会把平台那边的未读标记清掉。
   * 这既是副作用也是目的 —— 消息已经进本地库了，红点留着反而重复提醒。
   */

  const UNREAD_SELECTORS = [
    '[class*="unread"]',
    '[class*="red-dot"]',
    '[class*="reddot"]',
    '[class*="badge-count"]',
    '[class*="msg-count"]',
    '[class*="chat-badge"]',
  ];

  /** 元素有没有实际尺寸。用它而不是 offsetParent：后者在部分容器里恒为 null。 */
  function hasSize(node) {
    const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : null;
    if (rect && (rect.width > 0 || rect.height > 0)) return rect;
    if (node.offsetWidth > 0 || node.offsetHeight > 0) {
      return { width: node.offsetWidth, height: node.offsetHeight };
    }
    return null;
  }

  /** 找会话列表里带未读标记的项。返回的是"会话项"，不是徽章本身。 */
  function unreadNodes() {
    const out = [];
    const seen = new Set();
    for (const sel of UNREAD_SELECTORS) {
      document.querySelectorAll(sel).forEach((node) => {
        // 平台会把所有会话渲染出来再隐藏，隐藏的那些没有红点，不能算未读
        const rect = hasSize(node);
        if (!rect) return;
        const text = (node.innerText || '').trim();
        const isNum = /^[1-9]\d?$/.test(text);
        const isDot = !text && rect.width <= 16 && rect.height <= 16;
        if (!isNum && !isDot) return;
        const item =
          node.closest('li, [class*="item"], [class*="session"], [class*="conversation"], [class*="list"] > *') ||
          node;
        // 同一个会话的徽章父子节点会命中多次，按最近的会话项去重
        if (seen.has(item)) return;
        seen.add(item);
        out.push({ node: item, badge: text || '●' });
      });
    }
    return out;
  }

  async function syncCurrent() {
    const messages = readMessages();
    const title = readJobTitle();
    if (!messages.length) return { messages: 0, saved: 0, added_hr: 0, title };
    const data = await callApi('/api/chat-logs/sync', {
      platform: location.hostname,
      page_url: location.href,
      job_title: title || null,
      messages: messages.map((m) => ({ role: m.role, text: m.text })),
    });
    return { messages: messages.length, title, ...data };
  }

  /**
   * 同步当前会话，再逐个点开未读会话。
   *
   * max 默认 5：一次点太多个会话，页面会明显卡，而且失败时不好定位是哪一个。
   * 深度扫描失败不应该让整次扫描失败 —— 至少当前会话的数据已经拿到了。
   */
  async function scanAndSync({ deep = true, max = 5 } = {}) {
    const result = { ok: true, conversations: 0, added_hr: 0, unread: 0, items: [], note: '' };

    const first = await syncCurrent().catch(() => null);
    if (first) {
      result.conversations += 1;
      result.added_hr += first.added_hr || 0;
      result.items.push(first);
    }

    if (!deep) return result;

    const marks = unreadNodes();
    result.unread = marks.length;
    for (const mark of marks.slice(0, max)) {
      try {
        const target = mark.node.querySelector('a, button, [role="button"]') || mark.node;
        target.click();
        // 会话切换是异步加载消息的，1.5 秒是实测下沿；再短会读到上一个会话的残留
        await new Promise((r) => setTimeout(r, 1500));
        const one = await syncCurrent();
        result.conversations += 1;
        result.added_hr += one.added_hr || 0;
        if (one.added_hr) result.items.push(one);
      } catch {
        // 单个会话失败不影响其他会话
      }
    }
    if (!marks.length) result.note = '当前页面没有检测到未读会话';
    return result;
  }

  function init() {
    if (document.getElementById('job-agent-chat-host')) return;
    if (chatReady() || looksLikeChatPage()) {
      buildUi();
      startAutoSync();
      return;
    }
    // 首屏可能还没渲染出聊天框，也可能用户是 SPA 路由从列表页点进"沟通"的
    // （此时脚本只注入过一次，DOM 变化不会再来）。所以用轮询而不是一次性判断：
    // 1.5 秒一拍，最多看 MOUNT_WAIT_MS。等不到就静默退出，不在非聊天页挂按钮
    const t0 = Date.now();
    const timer = setInterval(() => {
      if (document.getElementById('job-agent-chat-host') || chatReady() || looksLikeChatPage()) {
        clearInterval(timer);
        if (!document.getElementById('job-agent-chat-host')) buildUi();
        startAutoSync();
        return;
      }
      if (Date.now() - t0 > MOUNT_WAIT_MS) clearInterval(timer);
    }, 1500);
  }

  // 后台定时扫描时给本页发消息。聊天页可能一连开着好几个，
  // 每个都会响应，后台按会话汇总即可，不需要在这里做互斥。
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== 'sync-chat-now') return undefined;
    scanAndSync({ deep: Boolean(message.deep), max: message.max || 5 })
      .then((r) => sendResponse(r))
      .catch((err) => sendResponse({ ok: false, error: String(err && err.message ? err.message : err) }));
    return true;
  });

  // 测试钩子：只在 check_extension_chat.js 里打开。
  // 内部函数包在 IIFE 里，不暴露的话只能靠真实浏览器点击来验证，
  // 而结构识别这种逻辑恰恰最需要能离线断言
  if (globalThis.__JOB_AGENT_TEST__) {
    globalThis.__jobAgentChatTest = {
      readMessages, readByLayout, readBySelectors, diagnose, looksLikeChatPage, chatReady,
      unreadNodes,
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
