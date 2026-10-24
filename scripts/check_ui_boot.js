/**
 * 界面启动冒烟测试：把 index.html + app.js 灌进 jsdom 跑一遍，确认「一加载就崩」不会发生。
 *
 * 为什么需要它：
 *   1) 往 index.html 加了工作台元素后，app.js 里直接
 *      document.getElementById('wb-resume').addEventListener —— 浏览器如果还缓存着
 *      旧 HTML，这行会抛 TypeError，整段脚本中断，用户看到的是
 *      「样式是旧的、列表全空、数据全没有」，而服务端一切正常，排查半天。
 *   2) 视图恢复一度挂在 Promise.all().then() 里，链条一断就停在简历页，
 *      用户点「工作台」看到的是简历页，控制台还没有任何提示。
 *
 * 检查四件事：
 *   1. 加载过程不抛错
 *   2. 关键元素都在
 *   3. 带 #workbench 启动时视图真的切过去、侧栏高亮同步
 *   4. 旧 HTML（没有工作台）+ 新 app.js 也不许崩
 *
 * 用法：node scripts/check_ui_boot.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const STATIC = path.join(ROOT, 'backend', 'static');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let pass = 0;
let fail = 0;
const ok = (m) => { console.log(`[PASS] ${m}`); pass += 1; };
const bad = (m, d) => { console.log(`[FAIL] ${m}${d ? ' — ' + d : ''}`); fail += 1; };

/** 用给定的 HTML 引导页面，返回 { errors, window } */
function boot(html, hash = '#resumes', apiData = null) {
  const dom = new JSDOM(html, {
    url: `http://127.0.0.1:8000/${hash}`,
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  const errors = [];

  // jsdom 不做布局，这几样缺失会让部分 UI 代码分支报错
  w.Element.prototype.getBoundingClientRect = () => ({
    width: 600, height: 400, top: 0, left: 0, right: 600, bottom: 400, x: 0, y: 0,
  });
  w.HTMLElement.prototype.scrollTo = () => {};
  w.scrollTo = () => {};

  // 默认所有接口返回空数据（只验证引导流程不崩）；
  // 传了 apiData 就按 URL 取 mock，用来验证具体视图的渲染
  w.fetch = async (url) => ({
    ok: true,
    status: 200,
    text: async () => JSON.stringify(apiData ? apiData(String(url)) : { count: 0, items: [] }),
  });

  w.addEventListener('error', (e) => errors.push(e.error ? e.error.message : e.message));
  w.addEventListener('unhandledrejection', (e) => errors.push('unhandledrejection: ' + e.reason));

  try {
    w.eval(fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'));
  } catch (e) {
    errors.push(`注入即抛错: ${e.name}: ${e.message}`);
  }
  return { errors, w };
}

const fullHtml = fs
  .readFileSync(path.join(STATIC, 'index.html'), 'utf8')
  .replace(/<script[\s\S]*?<\/script>/g, '');

(async () => {
  // ---------- 1. 完整 HTML ----------
  const full = boot(fullHtml);
  await sleep(300);
  if (!full.errors.length) ok('完整 HTML 引导无报错');
  else bad('完整 HTML 引导无报错', full.errors.slice(0, 3).join(' | '));

  const tag = full.w.document.getElementById('build-tag');
  if (tag && tag.textContent.trim()) ok(`页脚显示构建标记：${tag.textContent.trim()}`);
  else bad('页脚显示构建标记', '为空或元素缺失');

  for (const id of ['view-workbench', 'wb-job', 'wb-resume', 'wb-stats-box', 'btn-wb-load']) {
    if (full.w.document.getElementById(id)) ok(`工作台元素存在：${id}`);
    else bad(`工作台元素存在：${id}`, '缺失');
  }

  // ---------- 2. #workbench 启动 ----------
  const wb = boot(fullHtml, '#workbench');
  await sleep(250);
  const activeIds = [...wb.w.document.querySelectorAll('.view.active')].map((v) => v.id);
  if (activeIds.includes('view-workbench')) ok('带 #workbench 启动时切到工作台视图');
  else bad('带 #workbench 启动时切到工作台视图', `实际激活：${activeIds.join(',') || '(无)'}`);

  const activeTab = [...wb.w.document.querySelectorAll('.tab.active')].map((t) => t.dataset.view);
  if (activeTab.includes('workbench')) ok('侧边栏「工作台」标签高亮同步');
  else bad('侧边栏「工作台」标签高亮同步', `实际：${activeTab.join(',') || '(无)'}`);
  if (!wb.errors.length) ok('#workbench 启动无报错');
  else bad('#workbench 启动无报错', wb.errors.slice(0, 3).join(' | '));

  // ---------- 3. 旧 HTML（没有工作台）：不许崩 ----------
  const oldHtml = fullHtml
    .replace(/<section class="view" id="view-workbench">[\s\S]*?<\/section>/, '')
    .replace(/<div class="ver" id="build-tag"><\/div>/, '')
    .replace(/<button class="tab" data-view="workbench">工作台<\/button>/, '');

  const legacy = boot(oldHtml);
  await sleep(300);
  if (!legacy.errors.length) ok('旧 HTML + 新 app.js 不崩（缓存降级安全）');
  else bad('旧 HTML + 新 app.js 不崩', legacy.errors.slice(0, 3).join(' | '));

  // ---------- 4. 工作台渲染：投递前判断 ----------
  // 这块的历史：六态面板一次都没人点过，改成"技能缺口 + 同类岗位"后
  // 最怕的是渲染悄悄退化（面板空了但页面不报错）。所以断言具体内容。
  const mock = (url) => {
    if (url.includes('/api/jobs/')) {
      return {
        profile: { title: 'Java后端开发实习生', company: '测试公司', city: '深圳', basic: {} },
        salary: '200-250元/天',
        url: 'https://example.com/14',
      };
    }
    if (url.includes('/matches')) {
      return {
        cached: false,
        items: [{ decision: 'pass', score: 70, reasons: ['硬约束全部满足'], missed_skills: ['Kafka', 'Redis'], confidence: 0.83 }],
      };
    }
    if (url.includes('/applications/by-job/')) return { exists: false, job_id: 14 };
    if (url.includes('/chat-logs')) return { items: [] };
    if (url.includes('/workbench/similar/')) {
      return {
        total: 2,
        applied: 1,
        replied: 0,
        items: [
          { job_id: 5, title: '后端开发实习生', sim: 0.95, overlap: 2, status: 'applied', replied: false },
          { job_id: 6, title: 'Java', sim: 0.9, overlap: 0, status: null, replied: false },
        ],
      };
    }
    if (url.includes('/next-step')) return { action: '补 Kafka 再投', reason: '还差 2 项必备技能', urgency: 'normal' };
    if (url.includes('/api/advice/search-strategy')) {
      return {
        diagnosis: { verdict: '方向偏了，补技能解决不了', reason: '32 个否决里 25 个卡在必备技能' },
        summary: { total: 45, pass: 3, confirm: 10, reject: 32 },
        search_terms: [{ term: 'Java 开发工程师', why: '简历核心技能 + 通过岗位的共同方向' }],
        title_trap: { count: 2, words: ['资深', '高级'], examples: [] },
        fill_resume: [{ skill: 'Kafka', asked_by: 2, related: [{ name: 'RabbitMQ', similarity: 0.7 }] }],
        change_direction: [{ skill: '分布式事务', asked_by: 3 }],
        common_terms: ['开发工程师'],
        avoid_terms: ['资深'],
      };
    }
    return { count: 0, items: [] };
  };

  const wbRender = boot(fullHtml, '#workbench', mock);
  await sleep(200);
  // 注入一份简历再加载，否则匹配分支拿不到 resume_id
  const sel = wbRender.w.document.getElementById('wb-resume');
  const opt = wbRender.w.document.createElement('option');
  opt.value = '4';
  sel.appendChild(opt);
  sel.value = '4';
  try {
    await wbRender.w.eval('loadWorkbench(14)');
  } catch (e) {
    wbRender.errors.push('loadWorkbench 抛错: ' + e.message);
  }
  await sleep(200);

  const text = wbRender.w.document.getElementById('wb-body').textContent || '';
  const checks = [
    ['渲染出「投递前判断」面板', text.includes('投递前判断')],
    ['显示技能缺口（Kafka）', text.includes('Kafka')],
    ['显示同类岗位的共同技能依据', text.includes('共同技能 2 项')],
    ['同类岗位标注了已投', text.includes('已投')],
    ['有「标记为已投」按钮', !!wbRender.w.document.querySelector('[data-wb-applied="1"]')],
    ['旧的六态「投递状态」面板已移除', !text.includes('投递状态') && !wbRender.w.document.querySelector('[data-wb-status]')],
  ];
  for (const [name, passed] of checks) {
    if (passed) ok(name);
    else bad(name, '页面内容不符合预期');
  }
  if (!wbRender.errors.length) ok('工作台渲染无报错');
  else bad('工作台渲染无报错', wbRender.errors.slice(0, 3).join(' | '));

  // ---------- 5. 搜索策略面板 ----------
  // 它依赖"刚跑出来的匹配结果"，所以要单独验证渲染不崩、关键内容在
  try {
    await wbRender.w.eval('loadAdvice(6)');
  } catch (e) {
    wbRender.errors.push('loadAdvice 抛错: ' + e.message);
  }
  await sleep(150);
  const adviceText = (wbRender.w.document.getElementById('advice-box') || {}).textContent || '';
  const adviceChecks = [
    ['渲染出「搜索策略」面板', adviceText.includes('搜索策略')],
    ['显示诊断结论', adviceText.includes('方向偏了')],
    ['渲染出可复制的搜索词', !!wbRender.w.document.querySelector('[data-copy]')],
    ['显示「会但没写」分组', adviceText.includes('会但没写')],
    ['显示「够不着」分组', adviceText.includes('够不着')],
  ];
  for (const [name, passed] of adviceChecks) {
    if (passed) ok(name);
    else bad(name, '页面内容不符合预期');
  }

  // ---------- 6. 岗位横向对比 ----------
  // 勾选 → 对比条出现 → 点对比 → 表格渲染。这条链路上任何一环断了，
  // 用户看到的都是"勾了没反应"，比报错更难排查
  const cmpData = {
    counts: { pass: 2, confirm: 0, reject: 0 },
    total_jobs: 2,
    cached: false,
    items: [
      { job_id: 14, title: 'Java后端开发实习生', company: 'A 公司', decision: 'pass', score: 70, confidence: 0.8, missed_skills: [], reasons: [] },
      { job_id: 2, title: '高级后端工程师', company: 'B 公司', decision: 'pass', score: 60, confidence: 0.9, missed_skills: ['Kafka'], reasons: [] },
    ],
  };
  try {
    wbRender.w.eval(`renderMatches(${JSON.stringify(cmpData)})`);
  } catch (e) {
    wbRender.errors.push('renderMatches 抛错: ' + e.message);
  }
  await sleep(100);
  const boxes = [...wbRender.w.document.querySelectorAll('[data-cmp]')];
  boxes.slice(0, 2).forEach((b) => {
    b.checked = true;
    b.dispatchEvent(new wbRender.w.Event('change', { bubbles: true }));
  });
  const bar = wbRender.w.document.getElementById('cmp-bar');
  if (bar && !bar.hidden) ok('勾选后出现对比操作条');
  else bad('勾选后出现对比操作条', '条未显示');

  const cmpBtn = wbRender.w.document.getElementById('btn-cmp');
  if (cmpBtn && !cmpBtn.disabled) {
    cmpBtn.click();
    await sleep(250);
    const cmpText = (wbRender.w.document.getElementById('cmp-panel') || {}).textContent || '';
    const cmpChecks = [
      ['渲染出对比表格', cmpText.includes('岗位对比')],
      ['对比表含缺口行', cmpText.includes('缺口技能')],
      ['对比表含两个岗位标题', cmpText.includes('Java后端开发实习生') && cmpText.includes('高级后端工程师')],
      ['对比表带出薪资', cmpText.includes('200-250元/天')],
    ];
    for (const [name, passed] of cmpChecks) {
      if (passed) ok(name);
      else bad(name, '表格内容不符合预期');
    }
  } else {
    bad('选够 2 个后对比按钮可用', '按钮仍禁用');
  }

  console.log(`\n${pass}/${pass + fail} 通过`);
  process.exit(fail ? 1 : 0);
})();
