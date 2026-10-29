/**
 * 页面脚本：抓取 JD、调用本地后端、在页面上展示匹配点。
 *
 * 三条设计原则：
 * 1) 只搬原文，不抽字段 —— 字段抽取放后端，平台改版时炸的只是"能否找到容器"，
 *    而不是"字段全错"。插件升级需要用户重新加载扩展，代价比改后端高得多。
 * 2) 选择器全部是多候选 + 长度校验，全部失配时降级抓整页文本并标注 fallback，
 *    让用户知道这次抓的数据不干净。
 * 3) UI 放在 Shadow DOM 里，页面样式再凶也影响不到面板；高亮则反过来要小心，
 *    只改文本节点且有 try/catch 兜底 —— Boss 是框架渲染，动它的 DOM 有风险。
 */

(() => {
  'use strict';

  // 重复注入守卫。popup 里的「在当前页显示面板」会手动执行一次本脚本，
  // 若自动注入已经跑过，这里不拦就会挂出两个一模一样的悬浮球
  if (globalThis.__jobAgentContentReady) return;
  globalThis.__jobAgentContentReady = true;

  // ---------- 配置 ----------

  // ---------- 平台适配 ----------
  // 每个平台的类名都是构建产物，改版就会变，所以每个都给多个候选。
  // Boss 已实测；猎聘与智联的选择器是**基于公开结构的推测，未在登录态下验证**，
  // 所以面板上有「诊断页面结构」——抓不到时用它取证，再据此修选择器。
  const HOST = location.hostname.replace(/^www\./, '');
  const PLATFORMS = {
    'zhipin.com': {
      // 这里必须写字面量。之前写成 SOURCE，而 SOURCE 在文件后面才 const 声明，
      // 对象字面量求值时直接命中 TDZ，整个 content.js 顶层抛错、UI 一个都不出现。
      // 表现是"某个平台不显示插件"，很容易被误判成选择器不匹配。
      //
      // 值必须是 'boss' 而不是 'zhipin'：后端 job.source 有 CHECK 约束，
      // 只收 boss/liepin/zhaopin/manual。写成 zhipin 会让入库被数据库拒绝，
      // 而当时为了绕开这一点，ingestJob 里把 source 写死成 'boss' ——
      // 于是猎聘和智联抓来的岗位全被记成 Boss 的，平台筛选和雷达全错。
      source: 'boss',
      detailPath: /\/job_detail\//,
      jd: [
        '.job-detail-container',
        '.job-detail-box',
        '.job-sec-text',
        '.job-detail-section',
        '.job-detail',
        '[class*="job-sec"]',
        '[class*="jobDesc"]',
        '[class*="job-detail"]',
      ],
      title: ['.job-name', 'h1', '.job-banner .name', '[class*="job-title"]', '[class*="jobTitle"]'],
      cards: [
        '.job-card-wrap',
        '.job-card-box',
        '.job-card-wrapper',
        '.job-list-box > li',
        '[class*="job-card"]',
      ],
      cardLink: 'a[href*="job_detail"]',
      cardTitle: '.job-name',
    },
    'liepin.com': {
      source: 'liepin',
      // 猎聘详情：/job/123456.shtml，也有 /a/123456.shtml 的老格式
      detailPath: /\/(?:job|a)\/\d+\.s?html?/i,
      // 外层容器优先。实测同一个岗位：
      //   .job-intro-container        401 字（含「职位介绍」+「其他信息」里的
      //                               语言要求、行业要求 —— 这两项对判断匹配有用）
      //   [data-selector=...]         362 字（只有职位介绍正文，少了上面那段）
      // 所以 data-selector 不做首选，只作为类名改版后的兜底：
      // 它是猎聘给自家埋点用的标记，比构建出来的类名更抗改版。
      jd: [
        '.job-intro-container',
        '[data-selector="job-intro-content"]',
        '.job-description',
        '.job-detail-box',
        '[class*="job-intro"]',
        '[class*="job-description"]',
        '[class*="job-detail"]',
      ],
      // 详情页标题是 <div class="job-title-box"><div title="岗位名">岗位名</div></div>，
      // 不是 h1。用 [title] 属性取最准 —— 直接拿 [class*="job-title"] 会把
      // 同级的城市（【深圳-福田区】）和薪资一起读进来
      title: ['.job-title-box [title]', '.name-box .name', '.job-title-box h1', 'h1', '[class*="job-title"]'],
      // 以下类名于 2026-10-22 由用户在现场点「诊断页面结构」取证得到：
      // 列表页上 .jobCardPcContainer / .jobTitleBox / .jobCompanyInfoBox /
      // .jobCardRightBox 各出现 42 次（一页 42 个岗位）。
      // 之前猜的 .job-card-pc-container 是短横线写法，实际是驼峰 —— 全站 0 命中。
      cards: [
        '.jobCardPcContainer',
        '.job-card-pc-container',
        '[class*="jobCard"]',
        '[class*="job-card"]',
        'li[class*="card"]',
      ],
      cardLink: 'a[href*="/job/"], a[href*="/a/"]',
      cardTitle: '.jobTitleBox',
    },
    'zhaopin.com': {
      source: 'zhaopin',
      // 智联详情页实测为 /jobdetail/CC697082780J40896898510.htm（无下划线）
      detailPath: /\/jobdetail\//i,
      // 以下类名于 2026-10-22 从 www.zhaopin.com/jobdetail/CC697082780J40896898510.htm
      // 的线上 HTML 实测得到：正文在 .describtion-card__detail-content，
      // 技能标签在 .describtion-card__skills-item，标题是 summary-planes（复数）。
      // 之前写的 .describtion / .summary-plane__title 都是凭印象猜的，后者连复数都错
      jd: [
        '.describtion-card__detail-content',
        '.describtion-card',
        '.describtion-card__content-wrap',
        '.describtion',
        '[class*="describtion"]',
        '[class*="job-description"]',
        '[class*="job-detail"]',
      ],
      title: [
        '.summary-planes__title',
        '.summary-plane__title',
        '.job-summary h1',
        'h1',
        '[class*="job-title"]',
      ],
      // 列表卡片：joblist-box__item 是搜索页实测存在的类名（20 条/页）
      cards: ['.joblist-box__item', '.jobinfo', '[class*="joblist"] li', '[class*="job-card"]'],
      // 卡片里的详情链接是 http://www.zhaopin.com/jobdetail/xxx.htm（无下划线）
      cardLink: 'a[href*="jobdetail"]',
      cardTitle: '.jobinfo__name',
    },
  };

  const PLATFORM =
    Object.entries(PLATFORMS).find(([domain]) => HOST.endsWith(domain))?.[1] || PLATFORMS['zhipin.com'];
  const SOURCE = PLATFORM.source;

  const JD_CONTAINERS = PLATFORM.jd;
  const TITLE_SELECTORS = PLATFORM.title;
  const CARD_SELECTORS = PLATFORM.cards;
  // 卡片内的"进去看详情"链接与标题。三个平台写法不同，写死 BOSS 的
  // a[href*="job_detail"] 会让智联／猎聘的卡片全部匹配失败 ——
  // 表现为链接取不到、URL 退化成列表页地址、去重键重复，同一岗位反复入库
  const CARD_LINK = PLATFORM.cardLink || 'a[href*="job_detail"]';
  const CARD_TITLE = PLATFORM.cardTitle || '.job-name';

  const MIN_JD_CHARS = 120;
  // 批量条数由用户自己定，不再写死。上限仍要有个天花板：
  // 一次抓太多会让连续操作时间过长，中途任何页面变化都会打断它
  const DEFAULT_BATCH_COUNT = 10;
  const MAX_BATCH_COUNT = 30;
  // 两次操作之间的间隔。**不用固定值** —— 固定节奏本身就是最容易被识别的特征，
  // 真人的间隔是散乱的
  const CLICK_GAP_MIN = 1200;
  const CLICK_GAP_MAX = 2800;
  // 小概率插入一次长停顿，模拟"读一会儿再点下一个"，而不是机械地等 1.5 秒
  const LONG_PAUSE_CHANCE = 0.08;
  // 点击卡片后等详情渲染的上限。Boss 的详情是异步加载的，给 8 秒足够；
  // 超时就退回摘要入库，不无限等
  const DETAIL_WAIT_MS = 8000;

  const rand = (min, max) => min + Math.random() * (max - min);
  const randInt = (min, max) => Math.floor(rand(min, max + 1));

  // ---------- 自动收录 ----------
  // 用户在列表里点开一个岗位，右侧详情一换就把这份 JD 入库，
  // 不必再点一次"抓取"。每次收录会产生一次 LLM 抽取（约 ¥0.003），
  // 所以必须有去重、间隔与上限三道闸，不能点一下就抽一次。
  const AUTO_TICK_MS = 900;           // 轮询右侧面板的节奏
  const AUTO_STABLE_TICKS = 2;        // 连续两拍看到同一个岗位才算"停下来了"
  const AUTO_MIN_GAP_MS = 3000;       // 两次自动收录的最小间隔
  const AUTO_MAX_PER_PAGE = 40;       // 单页上限，防止长列表一路点下去烧钱
  // 列表页自动收录的单次上限。必须声明在这里而不是渲染代码附近：
  // renderActions 会把它插进模板字符串，而 renderActions 在挂载时就执行，
  // 声明晚了会命中 TDZ —— 整个 content.js 顶层抛错，UI 一个都不出现。
  // 之前就是因为 source: 'zhipin' 写成了 SOURCE，踩过同一个坑
  const AUTO_LIST_MAX = 12;
  const INGESTED_KEY = 'jobagent:auto-ingested';

  // 纯函数由 lib.js 提供（拆成独立文件才能脱离浏览器做单测）
  const { extractJobId, buildGreeting, cleanText, escapeRegExp } = globalThis.JobAgentLib;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ---------- 拟人化操作 ----------

  // 上一次鼠标位置：连续移动要有起点，每次都从屏幕外飞过来反而更假
  let lastPointer = { x: rand(200, 700), y: rand(200, 500) };

  function fireMouse(type, x, y, target) {
    const node = target || document.elementFromPoint(x, y) || document;
    node.dispatchEvent(
      new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        clientX: x,
        clientY: y,
        view: window,
      })
    );
  }

  /**
   * 按缓动曲线把鼠标移到元素上再点下去。
   *
   * 边界要说清楚：合成事件的 `isTrusted` 恒为 false，这是浏览器层面的硬痕迹，
   * 任何轨迹模拟都改不掉。所以这里做的不是"绕过检测"，而是让**节奏与轨迹**
   * 不呈现机器特征。真正保护账号的是低频、人在现场、不做平台协议禁止的批量行为。
   */
  async function humanClick(el) {
    const rect = el.getBoundingClientRect();
    if (rect.top < 0 || rect.bottom > window.innerHeight) {
      el.scrollIntoView({ block: 'center' });
      await sleep(rand(180, 380));
    }
    const box = el.getBoundingClientRect();
    // 落点不放在几何中心：真人不会每次都精准命中正中间
    const to = {
      x: box.left + box.width * rand(0.35, 0.65),
      y: box.top + box.height * rand(0.35, 0.65),
    };
    const from = lastPointer;
    const steps = randInt(12, 20);

    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const dist = Math.hypot(dx, dy) || 1;
    // 垂直于行进方向的弧度，避免一条笔直的线
    const bow = rand(-0.12, 0.12) * dist;
    const ctrl = {
      x: (from.x + to.x) / 2 - (dy / dist) * bow,
      y: (from.y + to.y) / 2 + (dx / dist) * bow,
    };
    // 先冲过目标一点点再回正：真人手会过冲
    const over = rand(2, 8);
    const overshoot = { x: to.x + (dx / dist) * over, y: to.y + (dy / dist) * over };

    for (let i = 1; i <= steps; i += 1) {
      const t = i / steps;
      let point;
      if (t <= 0.85) {
        const u = t / 0.85;
        const mt = 1 - u;
        point = {
          x: mt * mt * from.x + 2 * mt * u * ctrl.x + u * u * overshoot.x,
          y: mt * mt * from.y + 2 * mt * u * ctrl.y + u * u * overshoot.y,
        };
      } else {
        const u = (t - 0.85) / 0.15;
        point = {
          x: overshoot.x + (to.x - overshoot.x) * u,
          y: overshoot.y + (to.y - overshoot.y) * u,
        };
      }
      lastPointer = { x: point.x + rand(-1, 1), y: point.y + rand(-1, 1) };
      fireMouse('mousemove', lastPointer.x, lastPointer.y);
      // 中段快、两端慢：加减速靠步进延时体现，不是等距匀速
      const speed = Math.sin(Math.PI * t);
      await sleep(rand(6, 16) + (1 - speed) * 12);
    }

    lastPointer = to;
    fireMouse('mousemove', to.x, to.y, el);
    fireMouse('mouseover', to.x, to.y, el);
    await sleep(rand(140, 420));

    // 按下前后各有一小段抖动：手不是一根刚性杆
    fireMouse('mousemove', to.x + rand(-2, 2), to.y + rand(-2, 2), el);
    await sleep(rand(60, 150));
    fireMouse('mousedown', to.x, to.y, el);
    await sleep(rand(45, 120));
    fireMouse('mouseup', to.x, to.y, el);
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  }

  /** 两次操作之间的停顿：散乱，且小概率出现一次"读完再点"的长停顿 */
  async function humanPause() {
    const long = Math.random() < LONG_PAUSE_CHANCE;
    await sleep(long ? rand(3500, 6500) : rand(CLICK_GAP_MIN, CLICK_GAP_MAX));
  }

  // ---------- 抓取 ----------

  function pickContainer(selectors, minChars, root) {
    const scope = root || document;
    for (const selector of selectors) {
      const nodes = scope.querySelectorAll(selector);
      for (const node of nodes) {
        const text = cleanText(node.innerText);
        if (text.length >= minChars) return { node, text, selector };
      }
    }
    return null;
  }

  /**
   * JD 容器单独一套挑选逻辑：同一个选择器可能命中多层嵌套
   * （实测 .job-detail-container 里面还有 .job-detail-box / .job-detail-body），
   * 取"第一个够长的"会挑到内层、丢掉标题与薪资，所以同一优先级内取最长。
   */
  function pickJdContainer() {
    for (const selector of JD_CONTAINERS) {
      let best = null;
      for (const node of document.querySelectorAll(selector)) {
        const text = cleanText(node.innerText);
        if (text.length < MIN_JD_CHARS) continue;
        if (!best || text.length > best.text.length) best = { node, text, selector };
      }
      if (best) return best;
    }
    return null;
  }

  /** 元素是否真的在屏幕上。有的站点会在 DOM 里预渲染一个隐藏的详情区，
   *  文本读得到但用户看不见，不能把它当成"右侧详情面板" */
  function isVisible(node) {
    if (!node || !node.isConnected) return false;
    const rect = node.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return false;
    const style = window.getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  }

  function pageKind() {
    // 按平台各自的详情页路径判断。之前只认 /job_detail/，
    // 而智联是 /jobdetail/（没有下划线）—— 结果智联详情页被判成列表页，
    // 面板只显示"批量抓取"，用户点下去当然抓不到当前岗位
    const re = PLATFORM.detailPath || /job_?detail/i;
    return re.test(location.pathname) ? 'detail' : 'list';
  }

  // 存一份裁剪后的 DOM。
  //
  // 为什么需要：入库只存了 innerText，结果是「清洗规则改坏了吗」「选择器还准吗」
  // 这两个问题事后完全无法回答 —— 没有原料。库里 44 个岗位的 raw_html 全是空，
  // 想补一份真实页面快照做回归都补不出来。
  //
  // 为什么要裁：BOSS 详情页的 outerHTML 常带几百 KB 的 base64 头像与内联脚本，
  // 原样存单条就 2MB。去掉 script/style/svg 和 data: 图片后一般在 100KB 以内，
  // 足够还原 DOM 结构（回归测试最需要的是结构，不是图片）。
  function snapshotDom() {
    try {
      const clone = document.documentElement.cloneNode(true);
      clone.querySelectorAll('script, style, link, svg, noscript, iframe, canvas').forEach((n) => n.remove());
      clone.querySelectorAll('[src^="data:"]').forEach((n) => n.removeAttribute('src'));
      clone.querySelectorAll('[srcset]').forEach((n) => n.removeAttribute('srcset'));
      clone.querySelectorAll('[style]').forEach((n) => n.removeAttribute('style'));
      let html = clone.outerHTML;
      if (html.length > 400000) html = html.slice(0, 400000) + '\n<!-- truncated -->';
      return html;
    } catch {
      return null; // 存不下来不影响抓取本身
    }
  }

  function scrapeDetail() {
    const found = pickJdContainer();
    const titleNode = pickContainer(TITLE_SELECTORS, 2, document);
    const title = titleNode ? cleanText(titleNode.text).split('\n')[0] : document.title;

    // 找不到 JD 容器时降级抓整页，并标记 fallback：
    // 整页文本会带进导航、推荐位等噪声，抽取精度会下降，用户需要知道
    const body = found ? found.text : cleanText(document.body.innerText);
    return {
      kind: 'detail',
      url: location.href,
      source_job_id: extractJobId(location.href),
      title,
      raw_text: `${title}\n${body}`,
      fallback: !found,
      jd_node: found ? found.node : null,
    };
  }

  /**
   * 从卡片里取岗位名。
   *
   * 不能直接拿 CARD_TITLE 的整段 innerText：猎聘的 .jobTitleBox 里
   * `<a>Java开发实习生</a><span>急聘</span>` 两个都是行内元素，
   * innerText 不会换行，拼出来就是「Java开发实习生急聘」。
   * 优先取标题里的链接文本，取不到再退回节点文本的第一行。
   */
  function cardTitleOf(node) {
    const nameNode = node.querySelector(CARD_TITLE);
    if (!nameNode) return cleanText(node.innerText).split('\n')[0].trim();
    const linkNode = nameNode.querySelector('a');
    const raw = cleanText((linkNode || nameNode).innerText);
    return raw.split('\n')[0].trim();
  }

  function collectCards() {
    for (const selector of CARD_SELECTORS) {
      const nodes = Array.from(document.querySelectorAll(selector));
      if (nodes.length > 0) return { selector, nodes };
    }
    return { selector: null, nodes: [] };
  }

  function scrapeList(limit) {
    const cap = Math.min(Math.max(limit || DEFAULT_BATCH_COUNT, 1), MAX_BATCH_COUNT);
    const { nodes } = collectCards();
    const items = [];
    // Boss 列表页里同一个岗位可能出现多次（推荐位、虚拟列表复用节点），
    // 不去重会导致一份 JD 被提交两遍、批量名额被白白吃掉
    const seen = new Set();
    for (const node of nodes) {
      const link = node.querySelector(CARD_LINK);
      if (!link) continue;
      const text = cleanText(node.innerText);
      if (text.length < 10) continue;
      const key = extractJobId(link.href) || link.href;
      if (seen.has(key)) continue;
      seen.add(key);
      // 标题取卡片标题节点，比"整段文本第一行"稳：卡片首行可能是公司名或标签。
      // 猎聘的 .jobTitleBox 里常混着「急聘」「学生可投」这类角标，所以只取第一行
      const title = cardTitleOf(node);
      items.push({
        url: link.href,
        source_job_id: extractJobId(link.href),
        raw_text: text,
        title: title || text.split('\n')[0],
        key,
        // 保留 DOM 引用供批量点击；不外传，只在本页内使用
        node,
      });
      if (items.length >= cap) break;
    }
    return { kind: 'list', found: nodes.length, unique: items.length, items };
  }

  // ---------- 翻页（雷达的批量采集用） ----------
  //
  // 为什么靠点按钮而不是拼 URL
  //   各平台的分页参数（page / currentPage / offset）不一样，还可能带一次性签名。
  //   硬拼 URL 的成功率随时间下降，而且失败时表现为"抓到重复的首页"，
  //   看起来像去重坏了，实际是请求根本没翻页。
  //
  // 选择器按"语义"排优先级：aria-label / 文本 > 常见组件库类名 > 泛化的 next 类名。
  // 泛化类名放最后，因为它最容易误命中（比如"next-step 引导"）。
  const NEXT_SELECTORS = [
    'a[aria-label="下一页"]',
    'button[aria-label="下一页"]',
    '.ant-pagination-next',
    '.pager-next a',
    'li[class*="next"] a',
    'a[class*="next-page"]',
    'a[title="下一页"]',
    '[class*="pagination"] [class*="next"]',
  ];

  function isDisabled(el) {
    if (!el) return true;
    if (el.disabled) return true;
    if (String(el.getAttribute('aria-disabled')) === 'true') return true;
    const cls = String(el.className || '');
    return /disabled|is-disabled/.test(cls);
  }

  function findNextPage() {
    for (const sel of NEXT_SELECTORS) {
      const el = document.querySelector(sel);
      if (el && !isDisabled(el)) return el;
    }
    // 文本兜底：只认完全等于「下一页」的，避免命中「下一页：Java 工程师」这种标题
    const candidates = document.querySelectorAll('a, button, span[role="button"]');
    for (const el of candidates) {
      const text = (el.textContent || '').trim();
      if ((text === '下一页' || text === 'Next' || text === '下页') && !isDisabled(el)) {
        return el;
      }
    }
    return null;
  }

  /** 当前页的指纹：首页卡片的链接。翻页成功后它一定会变。 */
  function pageSignature() {
    const { nodes } = collectCards();
    let first = '';
    for (const node of nodes) {
      const link = node.querySelector(CARD_LINK);
      if (link && link.href) {
        first = link.href;
        break;
      }
    }
    return { href: location.href, first, count: nodes.length };
  }

  function clickNextPage() {
    const el = findNextPage();
    if (!el) return { ok: false, reason: '没有找到「下一页」按钮，可能已到最后一页' };
    try {
      el.scrollIntoView({ block: 'center' });
    } catch {
      /* 个别元素不支持 scrollIntoView，不影响点击 */
    }
    el.click();
    return { ok: true };
  }

  // ---------- 换搜索词（一键发现用） ----------
  //
  // 为什么在搜索框里打字，而不是拼搜索结果 URL
  //   各平台的搜索参数（query key、城市码、筛选位、一次性签名）拼错的表现是
  //   「打开了一个正常页面，但结果和你搜的词无关」—— 看起来像抓取坏了，
  //   实际是搜索没生效。在搜索框里打字是真人做的事，平台自己处理参数。
  //
  // 难点是 React/Vue 受控组件：直接改 input.value 不会触发框架的 onChange，
  // 状态不会更新，按回车搜的还是旧词。必须用原型上的 setter 赋值再补发 input 事件。
  const SEARCH_BOX_SELECTORS = [
    '#search_input',
    'input[placeholder*="搜索"]',
    'input[placeholder*="职位"]',
    'input[placeholder*="关键词"]',
    'input[type="search"]',
    '.search-input input',
    '[class*="search"] input[type="text"]',
    'input[name="query"]',
    'input[name="key"]',
    'input[name="keywords"]',
  ];

  function visible(el) {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 30 && rect.height > 10;
  }

  function findSearchBox() {
    for (const sel of SEARCH_BOX_SELECTORS) {
      const nodes = document.querySelectorAll(sel);
      for (const el of nodes) {
        if (visible(el) && !el.disabled && !el.readOnly) return el;
      }
    }
    return null;
  }

  function setInputValue(el, value) {
    // 原型 setter：绕过框架对 value 的劫持，否则 onChange 不触发
    const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function submitSearch(box) {
    // 三个事件都发：有的站只监听 keydown，有的监听 keypress，
    // 少发一个就会"没反应"，而排查时完全看不出差异
    for (const type of ['keydown', 'keypress', 'keyup']) {
      box.dispatchEvent(
        new KeyboardEvent(type, {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true,
        }),
      );
    }
  }

  function clickSearchButton() {
    const box = findSearchBox();
    const scope = box ? box.closest('form, [class*="search"], header, div') : document;
    const candidates = (scope || document).querySelectorAll('button, [role="button"], .search-btn, [class*="search-btn"]');
    for (const el of candidates) {
      const text = (el.textContent || '').trim();
      if (/^(搜索|搜一下|查询|Search)$/i.test(text) || /search-btn/.test(String(el.className || ''))) {
        if (visible(el)) {
          el.click();
          return { ok: true };
        }
      }
    }
    return { ok: false, reason: '没找到搜索按钮' };
  }

  function applySearch(keyword) {
    const kw = String(keyword || '').trim();
    if (!kw) return { ok: false, reason: '关键词为空' };
    const box = findSearchBox();
    if (!box) {
      return {
        ok: false,
        reason: '这个页面没找到搜索框。先把页面停在平台的搜索结果页再试，或直接用「批量采集」',
      };
    }
    box.focus();
    setInputValue(box, kw);
    submitSearch(box);
    return { ok: true, keyword: kw };
  }

  // ---------- 后端调用 ----------

  // 扩展被重新加载（改代码后点刷新）之后，已经打开的页面里留着的是旧脚本，
  // 它的 chrome.runtime 已经被销毁。此时任何 sendMessage 都会抛
  // "Extension context invalidated."——这不是网络问题，重试一万次也不会成功，
  // 唯一出路是刷新页面让新脚本注入。所以必须识别出来并直接说清楚。
  const RELOAD_HINT = '扩展已更新，本页还是旧脚本。请按 F5 刷新本页，再点一次按钮';

  function runtimeAlive() {
    try {
      return Boolean(chrome && chrome.runtime && chrome.runtime.id);
    } catch {
      return false;
    }
  }

  function isInvalidated(message) {
    return /context invalidated|message port closed|receiving end does not exist/i.test(
      String(message || '')
    );
  }

  function callApi(path, method = 'GET', body) {
    if (!runtimeAlive()) return Promise.reject(new Error(RELOAD_HINT));
    return new Promise((resolve, reject) => {
      const handle = (resp) => {
        let lastError = null;
        try {
          lastError = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
        } catch {
          lastError = 'invalidated';
        }
        if (lastError) {
          reject(new Error(isInvalidated(lastError) ? RELOAD_HINT : lastError));
          return;
        }
        if (!resp) {
          reject(new Error('后台无响应，请重新加载扩展'));
          return;
        }
        if (!resp.ok) {
          reject(new Error(resp.error));
          return;
        }
        resolve(resp.data);
      };
      try {
        chrome.runtime.sendMessage({ type: 'api', path, method, body }, handle);
      } catch (err) {
        // 扩展刚失效时 sendMessage 会同步抛错，而不是走 lastError
        reject(new Error(isInvalidated(err && err.message) ? RELOAD_HINT : String(err)));
      }
    });
  }

  let currentResumeId = null;
  let currentProfile = null;
  let batchCount = DEFAULT_BATCH_COUNT;

  /** 条数存在 chrome.storage.local：关掉页面下次还是你设的值 */
  async function loadBatchCount() {
    try {
      const saved = await chrome.storage.local.get({ batchCount: DEFAULT_BATCH_COUNT });
      batchCount = clampCount(saved.batchCount);
    } catch {
      /* 读不到就用默认值，不影响主流程 */
    }
  }

  function clampCount(value) {
    const n = Math.trunc(Number(value));
    if (!Number.isFinite(n)) return DEFAULT_BATCH_COUNT;
    return Math.min(Math.max(n, 1), MAX_BATCH_COUNT);
  }

  function setBatchCount(value) {
    batchCount = clampCount(value);
    try {
      chrome.storage.local.set({ batchCount });
    } catch {
      /* 存不进去也不该挡住抓取 */
    }
    return batchCount;
  }

  async function loadResume() {
    const data = await callApi('/api/resumes');
    const usable = (data.items || []).filter((r) => r.has_profile);
    if (!usable.length) throw new Error('本地库里还没有已解析的简历，先在界面里上传一份');
    currentResumeId = usable[0].id;
    const detail = await callApi(`/api/resumes/${currentResumeId}`);
    currentProfile = detail.profile;
    return usable[0];
  }

  // ---------- 面板（Shadow DOM 隔离） ----------

  let ui = null;

  function buildUi() {
    if (ui) return ui;
    const host = document.createElement('div');
    host.style.cssText =
      'position:fixed;right:20px;bottom:20px;z-index:2147483647;' +
      'font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;';
    document.documentElement.appendChild(host);
    const shadow = host.attachShadow({ mode: 'open' });
    shadow.innerHTML = `
      <style>
        * { box-sizing: border-box; }
        /* 悬浮球直接放品牌形象（内联 SVG）。
           不用 <img src="chrome-extension://...">：页面上下文加载扩展资源需要
           web_accessible_resources，多一项权限就多一个被平台 CSP 拦掉的可能 */
        .toggle {
          width: 52px; height: 52px; border-radius: 50%; border: 0; cursor: pointer;
          padding: 0; overflow: hidden; background: transparent;
          line-height: 0;
          box-shadow: 0 6px 18px rgba(80, 70, 200, .32);
          transition: transform .16s ease, box-shadow .16s ease;
        }
        .toggle img, .toggle svg { display: block; width: 100%; height: 100%; }
        .toggle:hover { transform: translateY(-2px); box-shadow: 0 10px 24px rgba(80, 70, 200, .38); }
        .toggle:active { transform: translateY(0); }
        .panel {
          display: none; width: 340px; max-height: 76vh; overflow: auto;
          background: #fff; border: 1px solid #e2e1dc; border-radius: 12px;
          padding: 14px; margin-bottom: 10px; font-size: 13px; color: #2c2c2a;
          box-shadow: 0 8px 28px rgba(0,0,0,.16);
        }
        .panel.show { display: block; }
        .head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
        .head b { font-size: 14px; font-weight: 500; }
        .ver { color: #9a9a94; font-weight: 400; font-size: 11px; }
        .close { margin-left: auto; border: 0; background: transparent; cursor: pointer; color: #6b6b66; font-size: 16px; }
        .row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
        button.act {
          flex: 1; min-width: 120px; padding: 8px 10px; border-radius: 8px; cursor: pointer;
          border: 1px solid #e2e1dc; background: #fff; font-size: 13px; color: #2c2c2a;
        }
        button.act.primary { background: #4f46e5; border-color: #4f46e5; color: #fff; }
        button.act:disabled { opacity: .55; cursor: not-allowed; }
        .meta { color: #6b6b66; font-size: 12px; margin-bottom: 8px; }
        .msg { font-size: 12px; margin: 6px 0; }
        .msg.err { color: #a32d2d; }
        .msg.ok { color: #0f6e56; }
        .block { background: #fafaf8; border-radius: 8px; padding: 8px 10px; margin-top: 8px; font-size: 12px; }
        .block .label { color: #6b6b66; display: block; margin-bottom: 3px; }
        .tag { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; margin: 2px 4px 2px 0; }
        .tag.ok { background: #e1f5ee; color: #0f6e56; }
        .tag.bad { background: #fcebeb; color: #a32d2d; }
        .greet { white-space: pre-wrap; line-height: 1.6; }
        .count-row {
          display: flex; align-items: center; gap: 6px; margin: 2px 0 8px;
          color: #6b6b66; font-size: 12px;
        }
        .count-row input {
          width: 56px; padding: 4px 6px; font-size: 12px; color: #2c2c2a;
          border: 1px solid #e2e1dc; border-radius: 6px; background: #fff;
        }
        .count-row .hint { color: #9a9a94; }
        .toggle-row {
          display: flex; align-items: center; gap: 6px;
          margin: 0 0 8px; font-size: 12px; color: #6b6b66; cursor: pointer;
        }
        .toggle-row input { margin: 0; accent-color: #4f46e5; }
        textarea {
          width: 100%; height: 190px; resize: vertical; padding: 6px;
          border: 1px solid #e2e1dc; border-radius: 6px; background: #fff;
          font-family: ui-monospace, Consolas, monospace; font-size: 11px;
          line-height: 1.45; color: #2c2c2a;
        }
      </style>
      <div class="panel" id="panel">
        <div class="head"><b>小新求职 <span class="ver" id="ver"></span></b><button class="close" id="close">×</button></div>
        <div class="meta" id="meta">初始化中…</div>
        <div class="row" id="actions"></div>
        <div class="msg" id="msg"></div>
        <div id="result"></div>
      </div>
      <button class="toggle" id="toggle" title="小新求职 · 点开看这个岗位能不能投">
        <img id="toggle-img" alt="" width="52" height="52">
        <svg id="toggle-fallback" viewBox="0 0 52 52" width="52" height="52" aria-hidden="true" hidden>
          <defs>
            <linearGradient id="gpBrand" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#635BFF"/>
              <stop offset="1" stop-color="#38BDF8"/>
            </linearGradient>
            <clipPath id="gpHead"><circle cx="26" cy="27" r="13"/></clipPath>
          </defs>
          <circle cx="26" cy="26" r="26" fill="url(#gpBrand)"/>
          <rect x="12" y="38" width="28" height="18" rx="6" fill="#e23a34"/>
          <circle cx="26" cy="27" r="13" fill="#ffdbbd"/>
          <g clip-path="url(#gpHead)">
            <rect x="10" y="11" width="32" height="12.2" fill="#1a1826"/>
            <rect x="13" y="22" width="1.9" height="5" fill="#1a1826"/>
            <rect x="37.1" y="22" width="1.9" height="5" fill="#1a1826"/>
          </g>
          <rect x="16.6" y="24.6" width="6" height="2.4" rx="1.2" fill="#1a1826"/>
          <rect x="29.4" y="24.6" width="6" height="2.4" rx="1.2" fill="#1a1826"/>
          <circle cx="21.6" cy="29.6" r="2" fill="#1a1826"/>
          <circle cx="30.4" cy="29.6" r="2" fill="#1a1826"/>
          <path d="M22.4 31.6q3.6 2.6 7.2 0" stroke="#1a1826" stroke-width="1.5" fill="none" stroke-linecap="round"/>
        </svg>
      </button>
    `;
    ui = {
      host,
      shadow,
      panel: shadow.getElementById('panel'),
      meta: shadow.getElementById('meta'),
      actions: shadow.getElementById('actions'),
      msg: shadow.getElementById('msg'),
      result: shadow.getElementById('result'),
    };

    shadow.getElementById('toggle').addEventListener('click', () => {
      ui.panel.classList.toggle('show');
    });

    // 悬浮球用图标原图（而不是手写的 SVG 近似）：扩展资源要在 manifest 里
    // 声明 web_accessible_resources，页面上下文才加载得到。
    // 个别站点 CSP 很严，会把它拦掉 —— 那时退回一张内联矢量图，
    // 至少不会变成一个空白圆球
    const toggleImg = shadow.getElementById('toggle-img');
    const toggleFallback = shadow.getElementById('toggle-fallback');
    if (toggleImg && chrome.runtime && chrome.runtime.id) {
      toggleImg.addEventListener('error', () => {
        toggleImg.hidden = true;
        if (toggleFallback) toggleFallback.hidden = false;
      }, { once: true });
      toggleImg.src = chrome.runtime.getURL('icon128.png');
    } else if (toggleImg && toggleFallback) {
      toggleImg.hidden = true;
      toggleFallback.hidden = false;
    }
    shadow.getElementById('close').addEventListener('click', () => {
      ui.panel.classList.remove('show');
    });
    // 把版本号显示出来：改完扩展后忘了刷新页面时，一眼就能看出跑的是旧脚本
    try {
      shadow.getElementById('ver').textContent = 'v' + chrome.runtime.getManifest().version;
    } catch {
      shadow.getElementById('ver').textContent = '';
    }
    return ui;
  }

  function say(text, kind = '') {
    const node = buildUi().msg;
    node.textContent = text || '';
    node.className = 'msg' + (kind ? ' ' + kind : '');
  }

  /** 拼 innerHTML 前转义：错误信息里可能带 < > & 或服务端返回的片段 */
  function escText(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /** 自动收录开关的绑定（列表页与详情页各有一份 DOM） */
  function wireAutoToggle(shadow) {
    const box = shadow.getElementById('auto-ingest');
    if (!box) return;
    box.addEventListener('change', () => {
      setAutoIngest(box.checked);
      say(
        box.checked
          ? '已开启自动收录：翻列表页会把当前页收进来，点开岗位会收完整 JD'
          : '已关闭自动收录（「批量抓取」仍然可以手动跑）',
      );
    });
  }

  function renderActions() {
    const kind = pageKind();
    const shadow = buildUi().shadow;

    if (kind === 'detail') {
      buildUi().actions.innerHTML = `
        <button class="act primary" id="act-detail">抓取本岗位并匹配</button>
        <label class="toggle-row">
          <input type="checkbox" id="auto-ingest" ${autoIngest ? 'checked' : ''}>
          自动收录（打开即入库，每个约 ¥0.003）
        </label>
        <button class="act" id="act-diag">诊断页面结构</button>
      `;
      shadow.getElementById('act-detail').addEventListener('click', onScrapeDetail);
      wireAutoToggle(shadow);
    } else {
      // 条数交给用户定：写死 10 的时候想多抓一点只能改代码
      buildUi().actions.innerHTML = `
        <button class="act primary" id="act-batch">批量抓取</button>
        <label class="count-row" for="batch-count">
          抓取条数
          <input type="number" id="batch-count" min="1" max="${MAX_BATCH_COUNT}" step="1" value="${batchCount}">
          <span class="hint">1–${MAX_BATCH_COUNT}</span>
        </label>
        <label class="toggle-row">
          <input type="checkbox" id="auto-ingest" ${autoIngest ? 'checked' : ''}>
          浏览即收录（进入新列表页自动抓当前页 ${AUTO_LIST_MAX} 条摘要，每条约 ¥0.003）
        </label>
        <button class="act" id="act-diag">诊断页面结构</button>
      `;
      shadow.getElementById('act-batch').addEventListener('click', onBatch);
      const input = shadow.getElementById('batch-count');
      input.addEventListener('change', () => {
        input.value = setBatchCount(input.value);
      });
      wireAutoToggle(shadow);
    }

    // 诊断按钮是刻意常驻的：有些站点开了反调试，右键检查会让页面直接失效，
    // 届时就只剩这条"不依赖开发者工具"的取证路径
    shadow.getElementById('act-diag').addEventListener('click', onDiagnose);
  }

  // ---------- 页面结构诊断 ----------

  /** 压掉空白并截断，够看清结构即可 */
  function snippet(node, limit = 1200) {
    const html = (node && node.outerHTML) || '';
    return html.replace(/\s+/g, ' ').slice(0, limit);
  }

  function diagnosticReport() {
    // 带上扩展版本：用户反馈"改了没用"时，第一件事是确认装的到底是哪一版
    const ver = (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || '?';
    const lines = [
      `小新求职诊断 v4 · 扩展 ${ver}`,
      `平台: ${SOURCE}`,
      `URL: ${location.href}`,
      `页面类型: ${pageKind()}`,
    ];

    const cards = collectCards();
    lines.push(`卡片选择器: ${cards.selector || '(未命中)'}  节点数=${cards.nodes.length}`);

    // 详情页还要报一下抓到的标题。标题会被拼在 JD 最前面一起送去抽取，
    // 抓错了（比如把同级的「【深圳-福田区】」「30-45k」带进来）会污染整个岗位画像，
    // 而这种错误从界面上完全看不出来 —— 只能靠诊断里这一行核对。
    if (pageKind() === 'detail') {
      const t = scrapeDetail().title || '';
      const dirty = /[【】]|\d+\s*-\s*\d+\s*k/i.test(t) ? '  ← 疑似混入了城市/薪资' : '';
      lines.push(`抓到标题: ${t || '(空)'}${dirty}`);
    }

    const list = scrapeList(batchCount);
    lines.push(`去重后岗位数: ${list.unique}（本次设定 ${batchCount} 条，上限 ${MAX_BATCH_COUNT}）`);
    if (list.items[0]) lines.push(`首个岗位标题: ${list.items[0].title}`);

    const jd = pickJdContainer();
    if (jd) {
      // 可见性是关键判据：隐藏的预渲染详情区读得到文本，但用户看不见，
      // 不能当成"右侧详情面板"，否则会误判成「左列表右详情」布局
      // 太短要提示：猎聘等站点长 JD 会折叠成「展开」按钮，没展开时只渲染前半段 ——
      // 这时抓到的要求不全，匹配结论会偏向"技能都符合"，方向正好反过来
      const shortHint = jd.text.length < 300 ? '  ← 偏短，检查页面上是否有「展开」按钮' : '';
      lines.push(`JD 容器: 命中 ${jd.selector} · ${jd.text.length} 字 · 可见=${isVisible(jd.node)}${shortHint}`);
    } else {
      lines.push(`JD 容器: 未命中（所有候选都不足 ${MIN_JD_CHARS} 字）`);
    }

    lines.push('候选容器明细（节点数 / 最长字数）:');
    for (const selector of JD_CONTAINERS) {
      const nodes = Array.from(document.querySelectorAll(selector));
      const longest = nodes.reduce(
        (max, n) => Math.max(max, cleanText(n.innerText).length),
        0
      );
      const anyVisible = nodes.some((n) => isVisible(n));
      lines.push(`  ${selector} -> ${nodes.length} / ${longest}${anyVisible ? ' · 有可见节点' : ''}`);
    }

    // 页面类名线索：选择器全不命中时，光知道"不命中"没用，得知道页面上到底有什么。
    // 这里把与岗位/卡片/消息相关的类名按出现次数列出来，拿到诊断就能直接写选择器
    lines.push('--- 页面类名线索（出现次数 >= 2，取前 15）---');
    const freq = new Map();
    const all = document.querySelectorAll('[class]');
    const cap = Math.min(all.length, 6000); // 超大页面只扫前 6000 个元素，避免诊断本身卡住
    for (let i = 0; i < cap; i += 1) {
      const raw = all[i].className;
      const cls = typeof raw === 'string' ? raw : '';
      if (!cls) continue;
      for (const token of cls.split(/\s+/)) {
        if (!token || !/job|card|item|position|detail|describ|msg|chat|bubble|session|contact/i.test(token)) continue;
        freq.set(token, (freq.get(token) || 0) + 1);
      }
    }
    const top = [...freq.entries()]
      .filter(([, n]) => n >= 2)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 15);
    if (top.length) {
      for (const [name, n] of top) lines.push(`  .${name} -> ${n}`);
    } else {
      lines.push('  （没有可用的类名线索，可能是纯 Canvas 或极端混淆的类名）');
    }

    if (jd) {
      lines.push(`命中容器文本前 100 字: ${jd.text.slice(0, 100).replace(/\n/g, ' / ')}`);
      lines.push('命中容器 outerHTML（前 1200 字）:');
      lines.push(snippet(jd.node));
    }
    if (cards.nodes[0]) {
      lines.push('第一张卡片 outerHTML（前 1200 字）:');
      lines.push(snippet(cards.nodes[0]));
    }
    return lines.join('\n');
  }

  function onDiagnose() {
    const box = buildUi().result;
    const report = diagnosticReport();
    box.innerHTML = `
      <div class="block">
        <span class="label">页面结构诊断（复制后发给我，用来改选择器）</span>
        <textarea id="diag-text" readonly></textarea>
        <button class="act" id="diag-copy" style="margin-top:8px">复制诊断信息</button>
      </div>
    `;
    const shadow = buildUi().shadow;
    const area = shadow.getElementById('diag-text');
    area.value = report;
    shadow.getElementById('diag-copy').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(report);
        say('诊断信息已复制', 'ok');
      } catch {
        area.select();
        say('已选中文本，请按 Ctrl+C 复制', 'err');
      }
    });
    say('诊断完成，点「复制诊断信息」', 'ok');
  }

  // ---------- 动作 ----------

  async function ensureReady() {
    if (currentResumeId) return;
    const resume = await loadResume();
    buildUi().meta.textContent = `简历 #${resume.id} · ${resume.file_name}`;
  }

  async function onScrapeDetail() {
    const btn = buildUi().shadow.getElementById('act-detail');
    btn.disabled = true;
    say('抓取页面…');
    try {
      await ensureReady();
      const scraped = scrapeDetail();
      if (scraped.fallback) {
        say('未找到 JD 容器，已降级抓整页文本（可能含无关内容）', 'err');
      }

      const ingest = await callApi('/api/jobs/ingest', 'POST', {
        source: SOURCE,
        raw_text: scraped.raw_text,
        // 详情页顺带存一份裁剪后的 DOM：日后平台改版时，能拿它复现"当时页面长什么样"，
        // 而不是只能凭诊断信息重新猜选择器
        raw_html: snapshotDom(),
        url: scraped.url,
        source_job_id: scraped.source_job_id,
        detail_level: scraped.fallback ? 'digest' : 'full',
      });
      say(`已入库 #${ingest.job_id}，开始匹配…`, 'ok');

      const params = new URLSearchParams({ include_rejected: 'true', target_cities: '' });
      const matches = await callApi(`/api/resumes/${currentResumeId}/matches?${params}`);
      const hit = (matches.items || []).find((i) => i.job_id === ingest.job_id);

      renderMatch(hit, ingest.profile, scraped);
    } catch (err) {
      say(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  function renderMatch(match, profile, scraped) {
    const box = buildUi().result;
    if (!match) {
      box.innerHTML = '<div class="block">匹配结果里没找到这个岗位，可能它被硬约束过滤掉了。</div>';
      return;
    }

    const decide = { pass: '通过', confirm: '待确认', reject: '否决' }[match.decision] || match.decision;
    const missed = match.missed_skills || [];
    const reasons = (match.reasons || []).slice(0, 6);

    box.innerHTML = `
      <div class="block">
        <span class="label">匹配结论</span>
        ${decide}${match.score != null ? ` · ${match.score} 分` : ''}
        ${missed.map((s) => `<span class="tag bad">缺 ${s}</span>`).join('')}
      </div>
      <div class="block">
        <span class="label">判断依据</span>
        ${reasons.map((r) => `<div>· ${r}</div>`).join('')}
      </div>
      <div class="block">
        <span class="label">打招呼话术（可复制）</span>
        <div class="greet" id="greet-text"></div>
        <button class="act" id="copy-greet" style="margin-top:8px">复制话术</button>
      </div>
    `;

    const greeting = buildGreeting({
      title: (profile && profile.title) || scraped.title,
      profile: currentProfile,
      match,
    });
    buildUi().shadow.getElementById('greet-text').textContent = greeting;

    buildUi().shadow.getElementById('copy-greet').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(greeting);
        say('话术已复制', 'ok');
      } catch {
        say('复制失败，请手动选中文本复制', 'err');
      }
    });

    // 高亮放最后且独立兜底：它是锦上添花，失败不能影响前面的结果
    if (scraped.jd_node) {
      try {
        const skills = (currentProfile.skills || [])
          .map((s) => (typeof s === 'string' ? s : s.name))
          .filter((s) => s && s.length >= 2);
        const count = highlightTerms(scraped.jd_node, skills);
        if (count) say(`已在页面上标出 ${count} 处技能命中`, 'ok');
      } catch {
        /* 页面是框架渲染，改动 DOM 可能失败；失败就静默跳过，不影响主流程 */
      }
    }
  }

  /** 当前页面上的 JD 文本，用于判断点击后详情是否真的换了 */
  function currentJd() {
    return pickJdContainer();
  }

  /** 通用轮询：fn 返回真值即结束 */
  async function waitFor(fn, timeoutMs, stepMs = 200) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const value = fn();
      if (value) return value;
      await sleep(stepMs);
    }
    return null;
  }

  /** 右侧详情面板当前显示的岗位名 */
  function panelJobName(found) {
    const jd = found || pickJdContainer();
    // BOSS 的标题就在 JD 容器里；智联的 JD 容器（.describtion-card）里没有标题，
    // 标题在页面顶部的 .summary-planes__title，所以要退到页面级选择器再找一轮
    const inJd = jd ? jd.node.querySelector(CARD_TITLE) : null;
    if (inJd) return cleanText(inJd.innerText);
    const inPage = pickContainer(TITLE_SELECTORS, 2, document);
    if (inPage) {
      const first = cleanText(inPage.text).split('\n')[0];
      if (first) return first;
    }
    if (!jd) return '';
    return (jd.text.split('\n')[0] || '').trim();
  }

  /** 岗位名比较：卡片标题可能比面板里的短（带后缀/编号），所以允许包含关系 */
  function namesMatch(a, b) {
    const x = cleanText(a || '').replace(/\s+/g, '');
    const y = cleanText(b || '').replace(/\s+/g, '');
    if (x.length < 2 || y.length < 2) return false;
    return x === y || x.includes(y) || y.includes(x);
  }

  /** 按岗位 key 重新定位卡片。列表重渲染后旧节点会失效，所以每次都要重查 */
  function cardNodeOf(key) {
    for (const node of collectCards().nodes) {
      const link = node.querySelector('a[href*="job_detail"]');
      if (!link) continue;
      if ((extractJobId(link.href) || link.href) === key) {
        // 点 li.job-card-box 而不是外层 wrap：Vue 的点击监听挂在卡片本身，
        // 而直接点 <a> 有可能真的执行跳转
        return node.querySelector('li.job-card-box') || node;
      }
    }
    return null;
  }

  function ingestJob({ rawText, url, sourceJobId, detailLevel }) {
    return callApi('/api/jobs/ingest', 'POST', {
      // 用当前平台的真实来源。写死 'boss' 会让猎聘/智联的岗位全部记成 Boss，
      // 平台筛选、雷达的来源标签、按平台统计都跟着错
      source: SOURCE,
      raw_text: rawText,
      url,
      source_job_id: sourceJobId,
      // 如实标注完整度：摘要被当成完整 JD，会让"没抓到技能要求"
      // 在匹配里变成"技能全不符"，结论正好相反
      detail_level: detailLevel,
    });
  }

  // ---------- 自动收录 ----------

  let autoIngest = true;
  let autoTimer = null;
  let batchBusy = false;
  let lastAutoName = '';
  let autoStable = 0;
  let lastIngestAt = 0;
  let autoCount = 0;
  let ingested = loadIngested();

  function loadIngested() {
    try {
      return new Set(JSON.parse(sessionStorage.getItem(INGESTED_KEY) || '[]'));
    } catch {
      return new Set();
    }
  }

  function markIngested(key) {
    ingested.add(key);
    try {
      sessionStorage.setItem(INGESTED_KEY, JSON.stringify([...ingested].slice(-200)));
    } catch {
      /* 存不进去就退化成"刷新后可能重复收录一次"，不影响正确性 */
    }
  }

  /** 按标题反查卡片——右侧详情面板本身不带 URL，只能拿标题去列表里对 */
  function cardByTitle(title) {
    const target = cleanText(title).replace(/\s+/g, '');
    if (target.length < 2) return null;
    for (const node of collectCards().nodes) {
      const nameNode = node.querySelector(CARD_TITLE);
      if (!nameNode) continue;
      const name = cardTitleOf(node).replace(/\s+/g, '');
      if (name && (name === target || target.includes(name) || name.includes(target))) {
        return node;
      }
    }
    return null;
  }

  /**
   * 入库后立刻问后端"这个岗位我能不能投"，把结论贴在面板上。
   *
   * 为什么必须在抓取现场给结论：等用户想起来去「发现」页翻，这个岗位多半
   * 已经关掉了。用户在浏览岗位时唯一想知道的就是这一句。
   *
   * 判定失败不影响入库结果，所以静默 —— 这只是附加信息，不该盖掉"已收录"。
   */
  async function showVerdict(jobId, integrity) {
    try {
      const v = await callApi(`/api/radar/job/${jobId}`, 'GET');
      if (!v || !v.advice) return;
      const kind = v.gate.grade === 'blocked' ? 'err'
        : v.gate.grade === 'reachable' ? 'ok' : '';
      const tier = v.company_score ? ` · ${v.company_score.tier}` : '';
      // JD 里有可疑内容时，警告要排在判定前面：判定说的是"值不值得投"，
      // 而可疑内容说的是"这份 JD 可能不是写给你看的"，
      // 后者的优先级更高 —— 一条被污染的 JD 抽出来的岗位要求本来就不该信
      const warn = integrity && integrity.level === 'warn' ? `⚠ ${integrity.summary} · ` : '';
      say(`${warn}${v.advice}${tier}`, warn ? 'err' : kind);
    } catch {
      /* 判定是附加信息，失败就保留"已收录"那条 */
    }
  }

  // ---------- 列表页自动收录 ----------
  //
  // 用户"在浏览岗位"时，插件应该顺手把当前页收进来，而不是等他点按钮。
  // 只抓当前页，不自动翻页 —— 翻页会打断他正在看的列表，也更容易被风控盯上。
  //
  // 每个列表 URL 只扫一次：SPA 在同一个 URL 下会因为筛选变化重渲染，
  // 不按 URL 去重的话，用户每改一次筛选就白跑一遍二十条 JD 抽取。
  const AUTO_LIST_KEY = 'jobagent:auto-list-url';
  const AUTO_LIST_MAX_TRIES = 20;    // 卡片没渲染出来时最多等多少拍（约 18 秒）
  let autoListBusy = false;
  let autoListScanned = new Set();
  let autoListTries = 0;

  function loadListScanned() {
    try {
      return new Set(JSON.parse(sessionStorage.getItem(AUTO_LIST_KEY) || '[]'));
    } catch {
      return new Set();
    }
  }
  autoListScanned = loadListScanned();

  async function autoScanList() {
    if (!autoIngest || autoListBusy || batchBusy || !runtimeAlive()) return;
    if (pageKind() !== 'list') return;
    const url = location.href.split('#')[0];
    if (autoListScanned.has(url)) return;

    // 卡片是异步渲染的：第一拍常常还是空的。这里先不登记 URL，
    // 空结果就下一拍再来，最多等 AUTO_LIST_MAX_TRIES 拍。
    // 过早登记会导致"进页面那一刻卡片没出来"就永久跳过这一页 —— 而且是静默的
    const list = scrapeList(AUTO_LIST_MAX);
    if (!list.items.length) {
      autoListTries += 1;
      if (autoListTries > AUTO_LIST_MAX_TRIES) {
        autoListScanned.add(url);
        try {
          sessionStorage.setItem(AUTO_LIST_KEY, JSON.stringify([...autoListScanned].slice(-30)));
        } catch {
          /* 存不了就算了 */
        }
      }
      return;
    }
    autoListTries = 0;

    autoListBusy = true;
    autoListScanned.add(url);
    try {
      sessionStorage.setItem(AUTO_LIST_KEY, JSON.stringify([...autoListScanned].slice(-30)));
    } catch {
      /* 存不了就在本次会话内存里去重 */
    }

    try {
      say(`列表页自动收录中：识别到 ${list.items.length} 个岗位…`);
      let added = 0;
      let seen = 0;
      for (const item of list.items) {
        if (!runtimeAlive()) {
          say(RELOAD_HINT, 'err');
          return;
        }
        const key = item.source_job_id || item.url;
        if (!key) continue;
        seen += 1;
        try {
          const res = await ingestJob({
            rawText: `${item.title || ''}\n${item.raw_text}`,
            url: item.url,
            sourceJobId: item.source_job_id,
            detailLevel: 'digest',
          });
          if (res && res.is_new) added += 1;
        } catch (err) {
          if (isInvalidated(err.message)) {
            stopAutoWatch();
            say(RELOAD_HINT, 'err');
            return;
          }
          // 单条失败继续：列表卡片偶尔缺字段，跳过即可
        }
      }
      say(`列表页已收录 ${added} 个新岗位（扫描 ${seen} 个）· 点「批量抓取」可抓完整 JD`, added ? 'ok' : '');
    } catch (err) {
      say(`列表页自动收录失败：${err.message}`, 'err');
    } finally {
      autoListBusy = false;
    }
  }

  async function autoIngestOnce(name) {
    const jd = pickJdContainer();
    if (!jd) return;
    // 详情区必须是可见的。有些页面会在 DOM 里预渲染一个隐藏的详情区，
    // 那是"用户还没点开"的内容，收进来等于白花钱
    if (!isVisible(jd.node)) return;

    const node = cardByTitle(name);
    const link = node ? node.querySelector(CARD_LINK) : null;
    const jobId = link ? extractJobId(link.href) : extractJobId(location.href);
    const key = jobId || name;
    if (ingested.has(key)) return;
    // 间隔不够就跳过这一拍，下拍还会再来，不需要排队
    if (Date.now() - lastIngestAt < AUTO_MIN_GAP_MS) return;
    if (autoCount >= AUTO_MAX_PER_PAGE) return;

    lastIngestAt = Date.now();
    try {
      const res = await ingestJob({
        rawText: `${name}\n${jd.text}`,
        url: link ? link.href : location.href,
        sourceJobId: jobId,
        detailLevel: 'full',
      });
      autoCount += 1;
      markIngested(key);
      if (!batchBusy) {
        say(`已自动收录 #${res.job_id} ${name}（共 ${autoCount} 个）`, 'ok');
        if (res.job_id) showVerdict(res.job_id, res.integrity);
      }
    } catch (err) {
      if (isInvalidated(err.message)) {
        stopAutoWatch();
        say(RELOAD_HINT, 'err');
        return;
      }
      // 失败要说出来：否则用户以为收进去了，事后发现库里没有
      say(`自动收录失败：${err.message}`, 'err');
    }
  }

  function autoTick() {
    if (!autoIngest || batchBusy || !runtimeAlive()) return;

    // 列表页先做一次整页收录，再走"详情面板逐条收录"的老路。
    // 两件事不冲突：整页收录拿的是摘要，右侧面板打开的那条能拿到完整 JD
    if (pageKind() === 'list') autoScanList();

    // 列表页看右侧面板，详情页看页面本身，两处都能取到当前岗位名
    const name = panelJobName();
    if (!name) return;

    if (name !== lastAutoName) {
      // 还在切换中，等它连续两拍不变再收，避免把渲染到一半的内容抓走
      lastAutoName = name;
      autoStable = 1;
      return;
    }
    autoStable += 1;
    if (autoStable === AUTO_STABLE_TICKS) autoIngestOnce(name);
  }

  function startAutoWatch() {
    if (autoTimer) return;
    autoTimer = setInterval(autoTick, AUTO_TICK_MS);
  }

  function stopAutoWatch() {
    if (autoTimer) clearInterval(autoTimer);
    autoTimer = null;
  }

  async function setAutoIngest(value) {
    autoIngest = Boolean(value);
    try {
      await chrome.storage.local.set({ autoIngest });
    } catch {
      /* 存不下就只在本次生效 */
    }
    if (autoIngest) startAutoWatch();
    else stopAutoWatch();
  }

  async function loadAutoSetting() {
    try {
      const saved = await chrome.storage.local.get({ autoIngest: true });
      autoIngest = Boolean(saved.autoIngest);
    } catch {
      /* 读不到就按默认开着 */
    }
  }

  async function onBatch() {
    const btn = buildUi().shadow.getElementById('act-batch');
    btn.disabled = true;
    // 批量跑的时候别让自动收录插队：两边都会调模型，消息也会互相覆盖
    batchBusy = true;
    try {
      // 先确认脚本还活着：否则会在 10 条里各失败一次，把"该刷新页面"
      // 这条唯一有用的信息淹在 10 行同样的报错里
      if (!runtimeAlive()) {
        say(RELOAD_HINT, 'err');
        return;
      }
      // 用户可能在输入框里改完直接点按钮，change 事件不一定先到，这里再读一次
      const countInput = buildUi().shadow.getElementById('batch-count');
      if (countInput) setBatchCount(countInput.value);

      await ensureReady();
      const list = scrapeList(batchCount);
      if (!list.items.length) {
        say('没有识别到岗位卡片，可能页面结构变了', 'err');
        return;
      }

      const targets = list.items.map((it) => ({
        key: it.source_job_id || it.url,
        url: it.url,
        source_job_id: it.source_job_id,
        title: it.title,
        digest: it.raw_text,
      }));

      // 右侧详情面板是否真的可见，决定两条路线各等多久：
      // 有可见面板 → 等它换成新内容（给足 8 秒）；
      // 没有 → 点了多半直接跳页，不该在"等右侧变化"上白等
      const firstJd = pickJdContainer();
      const sidePanel = Boolean(firstJd && isVisible(firstJd.node));
      const waitMs = sidePanel ? DETAIL_WAIT_MS : 3000;
      say(
        `识别到 ${targets.length} 个岗位，${sidePanel ? '右侧有详情面板，就地抓取' : '无可见详情面板，将逐个点开抓取'}`
      );

      const listUrl = location.href;
      let full = 0;
      let digest = 0;
      let jumped = 0;
      const failures = [];

      for (let i = 0; i < targets.length; i += 1) {
        const target = targets[i];
        const label = `[${i + 1}/${targets.length}] ${target.title}`;

        if (!runtimeAlive()) {
          say(RELOAD_HINT, 'err');
          return;
        }

        // 上一次点击可能把页面带到了详情页，先回列表再继续
        if (location.href !== listUrl || collectCards().nodes.length === 0) {
          history.back();
          const back = await waitFor(() => collectCards().nodes.length > 0, 6000);
          if (!back) {
            failures.push(`${label}：回不到列表页，已中断`);
            break;
          }
          await sleep(400);
        }

        const node = cardNodeOf(target.key);
        if (!node) {
          failures.push(`${label}：列表里找不到这张卡片，跳过`);
          continue;
        }

        const prevText = (currentJd() || {}).text || '';
        const urlBefore = location.href;

        // 右侧面板的判断必须靠"岗位名是否已是目标"，不能只靠"文本变了没有"：
        // 列表默认选中第一张卡，第一轮点的恰好就是它，点了不会有任何变化，
        // 只按"变化"判断会白等 8 秒然后把它误判成摘要 —— 而面板里就是完整 JD
        const panelReady = () => {
          const jdNow = pickJdContainer();
          if (!jdNow) return null;
          return namesMatch(panelJobName(jdNow), target.title) || jdNow.text !== prevText
            ? jdNow
            : null;
        };

        try {
          await humanClick(node);
        } catch {
          failures.push(`${label}：点击失败`);
          continue;
        }
        await sleep(rand(280, 560));

        // 有的版本把点击监听挂在 <a.job-name> 上，点 li 没反应。
        // 补一次点链接：如果它真的会跳转，下面的分支会正确按"跳页"处理
        if (location.href === urlBefore && !panelReady()) {
          const anchor = node.querySelector('a[href*="job_detail"]');
          if (anchor) {
            try {
              await humanClick(anchor);
            } catch {
              /* 点不动就算了，后面会退化成摘要 */
            }
            await sleep(rand(280, 560));
          }
        }

        let rawText = target.digest;
        let isFull = false;
        let url = target.url;

        if (location.href !== urlBefore) {
          // 这个列表页是「点击进入详情页」的布局，不是左列表右详情。
          // 那就就地抓完再退回去 —— 行为上等同于用户自己逐个点开看
          jumped += 1;
          url = location.href;
          const found =
            (await waitFor(
              () => {
                const now = pickJdContainer();
                return now && isVisible(now.node) ? now : null;
              },
              waitMs
            )) || pickJdContainer();
          if (found) {
            const titleNode = pickContainer(TITLE_SELECTORS, 2, document);
            const title = titleNode ? cleanText(titleNode.text).split('\n')[0] : target.title;
            rawText = `${title}\n${found.text}`;
            isFull = true;
          }
        } else {
          const found = panelReady() || (await waitFor(panelReady, waitMs));
          if (found) {
            rawText = `${target.title}\n${found.text}`;
            isFull = true;
          }
        }

        if (isFull) full += 1;
        else digest += 1;

        try {
          await ingestJob({
            rawText,
            url,
            sourceJobId: target.source_job_id,
            detailLevel: isFull ? 'full' : 'digest',
          });
          say(`${label} → ${isFull ? '完整 JD' : '仅摘要'}（${i + 1}/${targets.length}）`);
        } catch (err) {
          if (isInvalidated(err.message)) {
            say(RELOAD_HINT, 'err');
            return;
          }
          // 失败的那条不计入完整/摘要，改计入失败，避免统计虚高
          if (isFull) full -= 1;
          else digest -= 1;
          failures.push(`${label}：${err.message}`);
        }

        await humanPause();
      }

      const box = buildUi().result;
      box.innerHTML = `
        <div class="block">
          <span class="label">批量结果</span>
          完整 JD ${full} 条 · 仅摘要 ${digest} 条 · 失败 ${failures.length} 条
          ${failures.length ? failures.map((f) => `<div>· ${escText(f)}</div>`).join('') : ''}
        </div>
        <div class="block">
          <span class="label">说明</span>
          本页布局是${jumped ? '「点击进入详情页」，已逐个点开抓取后自动返回' : '「左列表右详情」，已就地读取右侧详情'}。
          标记为「仅摘要」的岗位没有抓到 JD 正文，匹配会偏宽松，可进入该岗位详情页再单独抓一次。
        </div>
      `;
      say(`批量完成：完整 ${full} 条，摘要 ${digest} 条，失败 ${failures.length} 条`, full ? 'ok' : 'err');
    } catch (err) {
      say(err.message, 'err');
    } finally {
      btn.disabled = false;
      batchBusy = false;
    }
  }

  // ---------- 关键词高亮 ----------

  /** 在容器内标记命中词。只改文本节点，遇到异常由调用方兜住 */
  function highlightTerms(container, terms) {
    if (!terms.length) return 0;
    const pattern = new RegExp(terms.map(escapeRegExp).join('|'), 'gi');
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const targets = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeValue && pattern.test(node.nodeValue)) {
        pattern.lastIndex = 0;
        targets.push(node);
      }
    }

    let count = 0;
    for (const node of targets) {
      const text = node.nodeValue;
      const frag = document.createDocumentFragment();
      let last = 0;
      pattern.lastIndex = 0;
      let m = pattern.exec(text);
      while (m) {
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        const mark = document.createElement('mark');
        mark.style.cssText = 'background:#e1f5ee;color:#0f6e56;border-radius:3px;padding:0 2px;';
        mark.textContent = m[0];
        frag.appendChild(mark);
        last = m.index + m[0].length;
        count += 1;
        m = pattern.exec(text);
      }
      frag.appendChild(document.createTextNode(text.slice(last)));
      if (node.parentNode) node.parentNode.replaceChild(frag, node);
    }
    return count;
  }

  // ---------- 启动 ----------

  /**
   * 当前页是不是"沟通/聊天"页。
   * 聊天页由 chat.js 负责（它要读对话、生成 HR 回复），
   * 这边再挂一个「批量抓取」既没用，还会和聊天面板叠在一起。
   * 判据：独立子域（c.liepin.com / i.zhaopin.com）或路径里带 chat/im。
   */
  function isChatContext() {
    const h = location.hostname;
    if (/^c\.liepin\.com$/.test(h) || /^i\.zhaopin\.com$/.test(h) || /^im\./.test(h)) return true;
    return /(?:^|[/?#_-])(?:im|chat|message|messages)(?:[/?#_-]|$)/i.test(
      location.pathname + location.search + location.hash
    );
  }

  async function init() {
    if (isChatContext()) return;
    buildUi();
    // 先读用户上次设的条数与开关，再渲染按钮，避免输入框闪一下变成默认值
    await loadBatchCount();
    await loadAutoSetting();
    renderActions();
    if (autoIngest) startAutoWatch();
    ensureReady()
      .catch((err) => say(err.message, 'err'))
      .finally(renderActions);
  }

  // 后台定时扫描时给本页发消息，复用同一套抓取逻辑。
  //
  // 为什么不让 background 自己注入脚本抓：选择器和去重逻辑全在这里，
  // 两处各写一份必然走样 —— 平台改版时改了一处忘了另一处，
  // 表现就是「手动能抓、自动抓不到」，极难排查。
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) return undefined;
    if (message.type === 'scrape-list') {
      try {
        const list = scrapeList(message.count || 20);
        sendResponse({ ok: true, items: list.items, unique: list.unique });
      } catch (err) {
        sendResponse({ ok: false, error: String(err && err.message ? err.message : err) });
      }
      return true;
    }
    if (message.type === 'page-signature') {
      try {
        sendResponse({ ok: true, ...pageSignature() });
      } catch (err) {
        sendResponse({ ok: false, error: String(err && err.message ? err.message : err) });
      }
      return true;
    }
    if (message.type === 'goto-next-page') {
      try {
        sendResponse(clickNextPage());
      } catch (err) {
        sendResponse({ ok: false, reason: String(err && err.message ? err.message : err) });
      }
      return true;
    }
    if (message.type === 'search-keyword') {
      try {
        sendResponse(applySearch(message.keyword));
      } catch (err) {
        sendResponse({ ok: false, reason: String(err && err.message ? err.message : err) });
      }
      return true;
    }
    if (message.type === 'click-search') {
      try {
        sendResponse(clickSearchButton());
      } catch (err) {
        sendResponse({ ok: false, reason: String(err && err.message ? err.message : err) });
      }
      return true;
    }
    return undefined;
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
