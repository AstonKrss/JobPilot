/**
 * 真实页面 DOM 集成测试。
 *
 * 为什么需要它：content.js 的选择器全是猜的，猜错了表现是"插件不显示"或
 * "抓不到 JD"，而这两件事在开发机上根本看不出来 —— 之前智联就踩过：
 * 路径正则只认 /job_detail/，智联是 /jobdetail/，详情页被判成列表页。
 *
 * 做法：把线上页面 HTML 灌进 jsdom，按真实顺序注入 lib.js + content.js，
 * 然后检查 1) 悬浮球挂没挂出来 2) 页面类型判对没 3) JD 容器/卡片命中没。
 *
 * 不带参数时跑 scripts/fixtures/ 下的两份快照（2026-10-22 从线上保存）：
 *   zhilian_detail.html  www.zhaopin.com/jobdetail/CC697082780J40896898510.htm
 *   zhilian_sou.html     sou.zhaopin.com/?kw=后端开发
 * 也可以传自己的文件：node check_extension_dom.js <html> <url>
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
// expect 显式声明这份快照应该被判成什么类型。
// 不再靠 URL 正则猜：猎聘详情页是 /a/81058331.shtml，用 /job/\d+ 那种正则会判错，
// 一判错整个测试就失去意义。
const FIXTURES = {
  'zhilian_detail.html': {
    url: 'https://www.zhaopin.com/jobdetail/CC697082780J40896898510.htm',
    expect: 'detail',
  },
  'zhilian_sou.html': {
    url: 'https://sou.zhaopin.com/?kw=%E5%90%8E%E7%AB%AF',
    expect: 'list',
  },
  // 猎聘列表页是纯客户端渲染，真实 HTML 抓不到，这份是按用户诊断到的类名合成的
  'liepin_list.html': {
    url: 'https://www.liepin.com/zhaopin/?key=java',
    expect: 'list',
  },
  // 详情页结构来自用户现场诊断输出的 outerHTML（详见文件内注释）
  'liepin_detail.html': {
    url: 'https://www.liepin.com/a/81058331.shtml',
    expect: 'detail',
  },
};

function runOne(htmlPath, pageUrl, expected = null) {
  return new Promise((resolve) => {
    const dom = new JSDOM(fs.readFileSync(htmlPath, 'utf8'), {
      url: pageUrl,
      runScripts: 'outside-only',
      pretendToBeVisual: true,
    });
    const w = dom.window;

    // jsdom 的 innerText 基本是空实现，而 content.js 全靠它取文本
    Object.defineProperty(w.HTMLElement.prototype, 'innerText', {
      get() { return this.textContent; },
      set(v) { this.textContent = v; },
    });
    // 选择器逻辑里有可见性判断，jsdom 不做布局，给个非零尺寸
    w.Element.prototype.getBoundingClientRect = () => ({
      width: 600, height: 400, top: 0, left: 0, right: 600, bottom: 400, x: 0, y: 0,
    });

    w.chrome = {
      runtime: { id: 'test', getURL: (p) => p, sendMessage: async () => ({ ok: true }), lastError: null },
      storage: { local: { get: async () => ({}), set: async () => ({}) } },
    };
    w.fetch = async () => ({ ok: true, json: async () => ({}) });

    const runtimeErrors = [];
    w.addEventListener('error', (e) => runtimeErrors.push(String(e.message)));

    try {
      w.eval(fs.readFileSync(path.join(ROOT, 'extension/lib.js'), 'utf8'));
      w.eval(fs.readFileSync(path.join(ROOT, 'extension/content.js'), 'utf8'));
    } catch (e) {
      console.log(`[FAIL] ${path.basename(htmlPath)} 注入即抛错: ${e.name}: ${e.message}`);
      resolve(false);
      return;
    }

    setTimeout(() => {
      const hosts = [...w.document.documentElement.children].filter((el) => el.shadowRoot);
      if (!hosts.length) {
        console.log(`[FAIL] ${path.basename(htmlPath)} 悬浮球未挂载 —— 用户看到的正是"插件没反应"`);
        resolve(false);
        return;
      }
      const sh = hosts[0].shadowRoot;
      const uiText = sh.textContent || '';
      const isDetail = /抓取本岗位并匹配/.test(uiText);
      const isList = /批量抓取/.test(uiText);

      const diagBtn = sh.getElementById('act-diag');
      if (diagBtn) diagBtn.click();
      // 诊断结果写在 textarea.value 里，textContent 读不到
      const diagArea = sh.getElementById('diag-text');
      const diag = diagArea ? diagArea.value : '';

      const pick = (re) => (diag.match(re) || [''])[0];
      const jdLine = pick(/JD 容器:[^\n]*/);
      const cardLine = pick(/卡片选择器:[^\n]*/);
      const uniqLine = pick(/去重后岗位数:[^\n]*/);
      const firstTitle = pick(/首个岗位标题:[^\n]*/);
      const grabbedTitle = pick(/抓到标题:[^\n]*/);

      // 期望类型由 URL 推断：详情页要判成 detail 且 JD 命中；
      // 列表页要判成 list 且卡片命中。判错任何一边都是用户能直接看到的故障
      // 优先用快照配置里声明的期望类型；命令行手工跑时才退回 URL 推断
      const expectDetail = expected === 'detail'
        ? true
        : expected === 'list'
          ? false
          : /job_?detail|jobdetail|\/job\/\d+|\/a\/\d+\.s?html/i.test(pageUrl);
      const jdOk = /命中/.test(jdLine);
      const cardOk = /节点数=[1-9]/.test(cardLine);
      // 列表页还必须真的能解析出条目：卡片命中但链接取不到时 items 会是 0，
      // 用户点"批量抓取"什么都不会发生（智联卡片的 a[href*="jobdetail"] 就踩过）
      const listOk = /去重后岗位数: [1-9]/.test(uniqLine);

      // 详情页的标题会被拼在 JD 最前面一起送去抽取，脏了（混进城市/薪资）
      // 会污染整个岗位画像，而界面上看不出来 —— 必须在这里守住
      const titleOk = !expectDetail || (/抓到标题: [^\s]/.test(grabbedTitle) && !/混入/.test(grabbedTitle));

      console.log(`--- ${path.basename(htmlPath)}（期望 ${expectDetail ? 'detail' : 'list'}）`);
      console.log(`  平台:     ${pick(/平台:[^\n]*/) || '(未输出)'}`);
      console.log(`  页面类型: ${isDetail ? 'detail' : isList ? 'list' : '未知 ✗'}`);
      console.log(`  JD 容器:  ${jdLine || '(未输出)'}`);
      if (expectDetail) console.log(`  ${grabbedTitle || '抓到标题: (未输出)'}`);
      console.log(`  卡片:     ${cardLine || '(无)'}`);
      console.log(`  列表解析: ${uniqLine || '(无)'}${firstTitle ? ' | ' + firstTitle : ''}`);
      if (runtimeErrors.length) console.log(`  运行期错误: ${runtimeErrors.slice(0, 3).join(' | ')}`);

      const pass = expectDetail ? isDetail && jdOk && titleOk : isList && cardOk && listOk;
      console.log(`  结果: ${pass ? 'PASS' : 'FAIL'}`);
      resolve(pass);
    }, 400);
  });
}

(async () => {
  const args = process.argv.slice(2);
  const cases = args.length >= 2
    ? [[args[0], args[1], null]]
    : Object.entries(FIXTURES).map(([name, cfg]) => [
        path.join(ROOT, 'scripts', 'fixtures', name),
        cfg.url,
        cfg.expect,
      ]);

  let failed = 0;
  for (const [file, url, expect] of cases) {
    if (!fs.existsSync(file)) {
      console.log(`[SKIP] 找不到 ${file}`);
      continue;
    }
    const ok = await runOne(file, url, expect);
    if (!ok) failed += 1;
  }
  console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
  process.exit(failed ? 1 : 0);
})();
