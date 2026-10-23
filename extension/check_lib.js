/**
 * lib.js 的离线测试（node 直接跑，不需要浏览器）。
 *
 * 覆盖范围与实际限制
 *   能测：URL 解析、话术拼装、文本清理 —— 这些是纯字符串逻辑，最容易写错也最容易验证。
 *   不能测：DOM 抓取与选择器命中。后者必须在真实页面里验证，
 *          这里不假装跑通，只保证"不碰 DOM 的那部分是对的"。
 *
 * 用法：node extension/check_lib.js
 */

require('./lib.js');

const { extractJobId, buildGreeting, cleanText, escapeRegExp } = globalThis.JobAgentLib;

let passed = 0;
let failed = 0;

function check(name, actual, expected) {
  const ok = Object.is(actual, expected) || JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed += 1;
    console.log('[PASS] ' + name);
  } else {
    failed += 1;
    console.log('[FAIL] ' + name);
    console.log('       期望: ' + JSON.stringify(expected));
    console.log('       实际: ' + JSON.stringify(actual));
  }
}

function checkContains(name, text, fragment) {
  const ok = typeof text === 'string' && text.includes(fragment);
  if (ok) {
    passed += 1;
    console.log('[PASS] ' + name);
  } else {
    failed += 1;
    console.log('[FAIL] ' + name);
    console.log('       期望包含: ' + fragment);
    console.log('       实际: ' + text);
  }
}

// ---- extractJobId ----
check(
  '详情页 URL 取 ID',
  extractJobId('https://www.zhipin.com/job_detail/abc123def.html'),
  'abc123def'
);
check(
  '详情页 URL 带查询参数',
  extractJobId('https://www.zhipin.com/job_detail/abc~def.html?ka=search_list_1'),
  'abc~def'
);
check(
  '查询串形式取 ID',
  extractJobId('https://www.zhipin.com/web/geek/job?jobId=xyz789'),
  'xyz789'
);
check('列表页 URL 无 ID', extractJobId('https://www.zhipin.com/web/geek/job'), null);
check('空值返回 null', extractJobId(null), null);

// ---- buildGreeting ----
const profile = {
  basic: { work_years: 6 },
  skills: [{ name: 'Go' }, { name: 'PostgreSQL' }, { name: 'Redis' }, { name: 'Kafka' }, { name: 'Vue' }],
};

const full = buildGreeting({ title: '高级后端工程师', profile, match: { missed_skills: [] } });
checkContains('话术含岗位名', full, '高级后端工程师');
checkContains('话术含年限', full, '6 年');
checkContains('话术含技能（只取前四个）', full, 'Go、PostgreSQL、Redis、Kafka');
check('话术不包含第五个技能', full.includes('Vue'), false);
checkContains('技能齐全时的表述', full, '基本都有对应经验');

const partial = buildGreeting({
  title: '后端',
  profile,
  match: { missed_skills: ['MySQL', 'Spring Boot'] },
});
checkContains('技能缺失时点名', partial, 'MySQL、Spring Boot');
checkContains('技能缺失时的表述', partial, '经验相对少一些');

const noYears = buildGreeting({ title: '实习', profile: { basic: {}, skills: [] }, match: {} });
checkContains('无年限时不报错', noYears, '主要技术栈');
check('空 profile 不抛异常', typeof noYears, 'string');

// ---- cleanText ----
check('压缩多余空行', cleanText('a\n\n\n\nb'), 'a\n\nb');
check('去掉行尾空格', cleanText('a   \nb'), 'a\nb');
check('nbsp 转普通空格', cleanText('a\u00a0b'), 'a b');
check('空值返回空串', cleanText(null), '');

// ---- escapeRegExp ----
check('转义 C++', escapeRegExp('C++'), 'C\\+\\+');
check('转义点号', escapeRegExp('a.b'), 'a\\.b');

console.log('\n' + passed + '/' + (passed + failed) + ' 通过');
console.log('说明：DOM 抓取与选择器命中未覆盖，需在真实 Boss 页面验证。');
process.exit(failed ? 1 : 0);
