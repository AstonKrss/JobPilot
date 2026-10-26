/**
 * 发现页（岗位雷达）的渲染测试。
 *
 * 为什么单独一组：
 *   这一页的渲染函数完全依赖接口返回的结构（gate / company_score / verdict），
 *   后端一改字段名，前端会静默渲染出「undefined」而不是报错 ——
 *   浏览器控制台干干净净，页面上却全是空格子。
 *   这类问题只有把真实 payload 灌进去渲染一遍才看得出来。
 *
 * 用法：node scripts/check_ui_radar.js
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

const RADAR = {
  pref: {
    cities: ['佛山', '广州'],
    nearby_cities: ['广州'],
    graduation_year: 2028,
    edu_level: '本科',
    stage: 'internship',
    accept_outsourcing: false,
  },
  total_jobs: 45,
  returned: 2,
  grade_counts: { reachable: 1, stretch: 1, unknown: 0, blocked: 43 },
  offtopic: 11,
  blocked_hidden: 43,
  verdict: { level: 'warn', text: '可投 2 个，但没有一个来自你设为优先梯队的公司。' },
  data_gaps: { no_company: 3, digest_only: 1 },
  items: [
    {
      job_id: 101,
      title: 'Java 后端开发实习生',
      company: '美云智数',
      city: '佛山',
      salary_raw: '200-300元/天',
      job_nature: '实习',
      edu_level: '本科',
      exp_min: 0,
      exp_max: null,
      graduate_years: [2028],
      campus_type: '实习',
      detail_level: 'full',
      url: 'https://example.com/job/101',
      source: 'boss',
      must_have_skills: ['Java', 'Spring Boot', 'MySQL'],
      company_profile: {
        name: '美云智数',
        parent: '美的集团',
        industry: '工业软件',
        nature: '子公司',
        tags: ['美的系'],
        note: '美的集团旗下做制造业数字化的公司',
        source: 'seed',
      },
      company_score: {
        score: 86,
        tier: 'T0',
        label: '首选',
        reasons: ['美的集团旗下 +10：集团背书，行业认可度高', '工业软件方向 +14'],
      },
      gate: {
        grade: 'reachable',
        label: '可达',
        offtopic: false,
        distance: 1,
        distance_label: '同城',
        positives: ['佛山 在通勤范围内', '明确招 2028 届'],
        cautions: [],
        blockers: [],
        missing: [],
      },
      match: { score: 78, display_score: 78, reliable: true, decision: 'pass', hit: 3, must: 3, missed: [] },
      radar_score: 82.4,
    },
    {
      job_id: 102,
      title: '产品运营',
      company: null,
      city: null,
      job_nature: null,
      edu_level: null,
      exp_min: null,
      graduate_years: [],
      detail_level: 'digest',
      url: null,
      must_have_skills: [],
      company_profile: null,
      company_score: { score: 50, tier: 'T2', label: '保底', reasons: ['没有识别出公司，无法评价'] },
      gate: {
        grade: 'stretch',
        label: '勉强',
        offtopic: false,
        distance: 0.5,
        distance_label: '城市未标注',
        positives: [],
        cautions: ['全职岗，现阶段以实习为主'],
        blockers: [],
        missing: ['城市', '学历', '经验'],
      },
      match: {},
      radar_score: 41.0,
    },
  ],
};

const PLAN = {
  stage: 'internship',
  cities: ['佛山', '广州'],
  keywords: [
    { q: 'Java 实习生', why: '简历方向是 Java', priority: 1 },
    { q: '后端开发实习生', why: '方向词命中多', priority: 2 },
  ],
  company_targets: [
    { company: '美云智数', city: '佛山', tier: 'T0', score: 86, in_library: 0, why: '美的集团旗下' },
  ],
  filters: { degree: '本科', experience: '应届生 / 在校', graduate_year: 2028, avoid: ['1 年以上经验', '外包驻场'] },
  tips: ['先切成「最新」'],
};

function boot() {
  const html = fs
    .readFileSync(path.join(STATIC, 'index.html'), 'utf8')
    .replace(/<script[\s\S]*?<\/script>/g, '');
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8000/#radar',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  const errors = [];
  w.Element.prototype.getBoundingClientRect = () => ({
    width: 600, height: 400, top: 0, left: 0, right: 600, bottom: 400, x: 0, y: 0,
  });
  w.HTMLElement.prototype.scrollTo = () => {};

  w.fetch = async (url) => {
    const u = String(url);
    let data = { count: 0, items: [] };
    if (u.includes('/api/radar/plan')) data = PLAN;
    else if (u.includes('/api/radar')) data = RADAR;
    return { ok: true, status: 200, text: async () => JSON.stringify(data) };
  };
  w.navigator.clipboard = { writeText: async () => {} };

  w.addEventListener('error', (e) => errors.push(e.error ? e.error.message : e.message));
  w.addEventListener('unhandledrejection', (e) => errors.push('unhandledrejection: ' + e.reason));
  w.eval(fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'));
  return { w, errors };
}

(async () => {
  const { w, errors } = boot();
  await sleep(600);
  const doc = w.document;
  const text = (sel) => (doc.querySelector(sel)?.textContent || '').trim();

  if (!errors.length) ok('带 #radar 启动无报错');
  else bad('带 #radar 启动无报错', errors.slice(0, 3).join(' | '));

  const activeTab = doc.querySelector('.tab.active');
  if (activeTab && activeTab.dataset.view === 'radar') ok('侧栏高亮在「发现」');
  else bad('侧栏高亮在「发现」', activeTab ? activeTab.dataset.view : 'none');

  if (text('#radar-verdict').includes('可投 2 个')) ok('结论条渲染出后端给的结论');
  else bad('结论条渲染出后端给的结论', text('#radar-verdict').slice(0, 80));

  if (doc.querySelectorAll('.stat-chip').length === 4) ok('四个分档计数都渲染');
  else bad('四个分档计数都渲染', String(doc.querySelectorAll('.stat-chip').length));

  if (text('#radar-verdict').includes('没抓到公司名')) ok('数据缺口提示出现');
  else bad('数据缺口提示出现');

  const items = doc.querySelectorAll('.radar-item');
  if (items.length === 2) ok('候选池渲染出 2 条');
  else bad('候选池渲染出 2 条', String(items.length));

  if (text('.radar-item .item-title').includes('Java 后端开发实习生')) ok('岗位标题渲染');
  else bad('岗位标题渲染');

  if (text('.radar-item').includes('美的集团旗下')) ok('公司母公司与点评渲染');
  else bad('公司母公司与点评渲染');

  if (text('.radar-item').includes('T0')) ok('公司分档徽章渲染');
  else bad('公司分档徽章渲染');

  if (text('.radar-item').includes('2028')) ok('届别徽章渲染');
  else bad('届别徽章渲染');

  if (text('.radar-item').includes('明确招 2028 届')) ok('可达理由逐条渲染');
  else bad('可达理由逐条渲染');

  if (doc.querySelector('.radar-item .badge.tier-t0')) ok('T0 用了对应样式类');
  else bad('T0 用了对应样式类');

  if (!text('#radar-list').includes('undefined')) ok('没有 undefined 漏到界面上');
  else bad('没有 undefined 漏到界面上', text('#radar-list').match(/undefined.{0,20}/)?.[0] || '');

  if (!text('#radar-list').includes('NaN')) ok('没有 NaN 漏到界面上');
  else bad('没有 NaN 漏到界面上');

  // 搜索计划
  if (text('#radar-plan-body').includes('Java 实习生')) ok('搜索计划渲染关键词');
  else bad('搜索计划渲染关键词');

  if (doc.querySelectorAll('.plan-chip').length === 2) ok('关键词渲染成可点击 chips');
  else bad('关键词渲染成可点击 chips', String(doc.querySelectorAll('.plan-chip').length));

  if (text('#radar-plan-body').includes('美云智数')) ok('公司直搜渲染出来');
  else bad('公司直搜渲染出来');

  if (text('#radar-plan-body').includes('外包驻场')) ok('避开条件渲染出来');
  else bad('避开条件渲染出来');

  if (doc.getElementById('radar-stage').value === 'internship') ok('档位回填成实习优先');
  else bad('档位回填成实习优先');

  if (doc.getElementById('radar-year').value === '2028') ok('届别回填成 2028');
  else bad('届别回填成 2028');

  if (doc.getElementById('radar-cities').value === '佛山,广州') ok('城市回填');
  else bad('城市回填', doc.getElementById('radar-cities').value);

  // 点关键词复制：不应抛错
  doc.querySelector('.plan-chip').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await sleep(60);
  if (!errors.length) ok('点击关键词复制不抛错');
  else bad('点击关键词复制不抛错', errors.slice(0, 2).join(' | '));

  console.log(`\n${pass}/${pass + fail} 通过`);
  process.exit(fail === 0 ? 0 : 1);
})();
