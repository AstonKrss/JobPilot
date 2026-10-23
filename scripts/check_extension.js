/**
 * 扩展离线自检（不需要浏览器）。
 *
 * 为什么要有：改动 content.js / lib.js 之后，唯一的验证方式是"装上扩展去真实网站看"，
 * 而这一步在开发机上做不到 —— 结果就是"某个平台不显示插件"这类问题要等用户发现。
 * 这个脚本覆盖两件可以在本地断言的事：
 *   1) 顶层求值不抛错（TDZ、引用未声明常量等，一崩就是整个 UI 消失）
 *   2) 纯函数逻辑正确（岗位 ID 解析、打招呼话术分支）
 *
 * 只验证"能不能跑起来 + 纯逻辑"，不验证选择器是否命中真实 DOM ——
 * 后者用 check_extension_dom.js。
 *
 * 用法：node scripts/check_extension.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(ROOT, 'extension', f), 'utf8');

let pass = 0;
let fail = 0;
const ok = (name) => { pass++; console.log(`[PASS] ${name}`); };
const bad = (name, detail) => { fail++; console.log(`[FAIL] ${name}\n        ${detail}`); };
const eq = (name, actual, expected) => {
  if (JSON.stringify(actual) === JSON.stringify(expected)) ok(name);
  else bad(name, `期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
};

// ---------- 1. 顶层求值冒烟 ----------

function makeCtx(hostname) {
  const ctx = {
    location: { hostname, href: `https://${hostname}/jobdetail/x.htm`, pathname: '/jobdetail/x.htm' },
    document: {
      readyState: 'loading',           // 挡住 init，只验证顶层常量求值
      addEventListener: () => {},
      createElement: () => ({
        style: {}, appendChild() {}, setAttribute() {},
        attachShadow: () => ({ appendChild() {}, innerHTML: '' }),
      }),
      querySelector: () => null,
      querySelectorAll: () => [],
      getElementById: () => null,
      body: null, head: null,
      documentElement: { appendChild() {} },
    },
    chrome: {
      runtime: { id: 'test', getURL: (p) => p, sendMessage: async () => ({ ok: true }), onMessage: { addListener() {} }, lastError: null },
      storage: { local: { get: async () => ({}), set: async () => ({}) } },
    },
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    MutationObserver: class { observe() {} disconnect() {} },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    requestAnimationFrame: () => 0,
    URL, URLSearchParams,
    Event: class {}, CustomEvent: class {}, Node: class {},
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.self = ctx;
  vm.createContext(ctx);
  return ctx;
}

const HOSTS = ['www.zhipin.com', 'www.liepin.com', 'www.zhaopin.com', 'sou.zhaopin.com', 'm.zhaopin.com'];
for (const host of HOSTS) {
  for (const files of [['lib.js', 'content.js'], ['lib.js', 'chat.js']]) {
    const ctx = makeCtx(host);
    let err = null;
    for (const f of files) {
      try {
        vm.runInContext(read(f), ctx, { filename: f });
      } catch (e) {
        err = `${f}: ${e.name}: ${e.message}`;
        break;
      }
    }
    if (err) bad(`${files[files.length - 1]} @ ${host} 顶层求值`, err);
    else ok(`${files[files.length - 1]} @ ${host} 顶层求值`);
  }
}

// background.js 需要 chrome.runtime.onMessage
try {
  const ctx = makeCtx('www.zhipin.com');
  vm.runInContext(read('background.js'), ctx, { filename: 'background.js' });
  ok('background.js 顶层求值');
} catch (e) {
  bad('background.js 顶层求值', `${e.name}: ${e.message}`);
}

// ---------- 2. 纯函数：岗位 ID 解析 ----------

const libCtx = makeCtx('www.zhipin.com');
vm.runInContext(read('lib.js'), libCtx, { filename: 'lib.js' });
const L = libCtx.JobAgentLib;

eq('BOSS 岗位 ID', L.extractJobId('https://www.zhipin.com/job_detail/abc123XYZ.html'), 'abc123XYZ');
eq('智联岗位 ID', L.extractJobId('https://www.zhaopin.com/jobdetail/CC697082780J40896898510.htm'), 'CC697082780J40896898510');
eq('猎聘岗位 ID', L.extractJobId('https://www.liepin.com/job/1234567.shtml'), '1234567');
eq('猎聘老格式 ID', L.extractJobId('https://www.liepin.com/a/7654321.shtml'), '7654321');
eq('查询串兜底 ID', L.extractJobId('https://x.com/p?jobId=zz99'), 'zz99');
eq('非岗位页返回 null', L.extractJobId('https://www.zhipin.com/web/geek/chat'), null);

// ---------- 3. 纯函数：打招呼话术 ----------

const profile = { basic: { work_years: 6 }, skills: ['Python', 'Go', 'MySQL', 'Redis'] };

const aligned = L.buildGreeting({
  title: '后端开发工程师',
  profile,
  match: { missed_skills: [], skill_must_count: 3, skill_hit_count: 3 },
});
eq('对口岗位提技能', /Python、Go、MySQL、Redis/.test(aligned) && /技能我基本都有对应经验/.test(aligned), true);

const offDir = L.buildGreeting({
  title: '餐厅经理',
  profile,
  match: { missed_skills: ['门店运营', '人员排班', '食品安全'], skill_must_count: 3, skill_hit_count: 0 },
});
eq('不对口岗位不提技术栈', /Python|Go|MySQL/.test(offDir), false);
eq('不对口岗位只问到岗重点', /最看重哪方面/.test(offDir), true);

// 未知命中信息时不能瞎判方向，应按原有逻辑走
const unknown = L.buildGreeting({ title: '后端开发工程师', profile, match: {} });
eq('无计数信息时走通用话术', /工作经验/.test(unknown), true);

console.log(`\n${pass}/${pass + fail} 通过`);
process.exit(fail ? 1 : 0);
