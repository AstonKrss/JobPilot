/**
 * 岗哨前端。
 *
 * 刻意保持无框架：只有四个视图、一次请求对应一次渲染，没有需要跨组件共享的复杂状态。
 * 引入构建工具与状态库会让「改一行看效果」多出编译等待，对单用户本地工具是负收益。
 */

const state = {
  resumes: [],
  jobs: [],
  jobSource: '',      // '' = 全部平台；否则按 source 过滤
  jobSources: {},     // { boss: 12, liepin: 3, ... }

  // 最近一次匹配结果，供"岗位对比"取字段用。
  // 不重新请求接口：对比必须和当前列表看到的是同一批数据，否则数字对不上
  matches: null,
  // 勾选待对比的岗位 id。用 Set 而不是数组，避免重复添加
  compare: new Set(),

  pollTimer: null,
  // job_id -> { score, decision, detail_level }。为空表示没选简历、按时间排序
  jobScores: {},
};

/* ---------- 基础设施 ---------- */

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  const raw = await resp.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = null;
    }
  }
  if (!resp.ok) {
    // FastAPI 的 detail 可能是字符串，也可能是校验错误数组
    let msg = `HTTP ${resp.status}`;
    if (data && data.detail !== undefined) {
      msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } else if (raw) {
      msg = raw.slice(0, 200);
    }
    throw new Error(msg);
  }
  return data;
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = 'toast'; }, 3200);
}

function setStatus(id, text, kind = '') {
  const node = document.getElementById(id);
  node.textContent = text || '';
  node.className = 'status' + (kind ? ' ' + kind : '');
}

/** 按钮执行期间禁用并改文案，避免长任务被重复点击 */
async function withLoading(btn, label, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/* ---------- 视图切换 ---------- */

// 视图白名单。新增标签页时必须同步加进来，否则 activateView 会静默返回，
// 点 tab 没反应、hash 也恢复不了 —— 加「工作台」时就漏了，查了两轮才定位
const VIEWS = ['resumes', 'jobs', 'radar', 'match', 'workbench', 'review', 'chat'];

function activateView(name) {
  if (!VIEWS.includes(name)) return;
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.view === name);
  });
  document.querySelectorAll('.view').forEach((v) => {
    v.classList.toggle('active', v.id === 'view-' + name);
  });
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    activateView(tab.dataset.view);
    // 用 hash 记住当前位置：刷新后不会跳回第一个标签
    history.replaceState(null, '', `#${tab.dataset.view}`);
    if (tab.dataset.view === 'workbench') {
      fillWorkbenchSelects();
      loadWorkbenchStats();
    }
    if (tab.dataset.view === 'radar') {
      loadRadar();
      loadRadarPlan();
    }
  });
});

// 工作台入口：岗位列表和匹配卡片上的「工作台」按钮都走这里
document.addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-workbench]');
  if (!btn) return;
  const jobId = Number(btn.dataset.workbench) || 0;
  if (!jobId) return;
  fillWorkbenchSelects();
  document.getElementById('wb-job').value = String(jobId);
  activateView('workbench');
  history.replaceState(null, '', '#workbench');
  loadWorkbench(jobId);
});

// hash 视图恢复放到数据就绪之后：数据没到就切视图，工作台/建议页会是空壳

/* ---------- 简历 ---------- */

const STATUS_META = {
  done: ['ok', '已解析'],
  parsing: ['info', '解析中'],
  pending: ['', '排队中'],
  partial: ['warn', '部分完成'],
  failed: ['bad', '失败'],
};

function renderResumes() {
  const box = document.getElementById('resume-list');
  document.getElementById('resume-count').textContent =
    state.resumes.length ? `共 ${state.resumes.length} 份` : '';

  if (!state.resumes.length) {
    box.innerHTML = '<div class="empty">还没有简历，先上传一份 PDF。</div>';
    return;
  }

  box.innerHTML = state.resumes.map((r) => {
    const [cls, label] = STATUS_META[r.parse_status] || ['', r.parse_status];
    const kind = r.source_kind === 'scanned' ? '扫描件 OCR'
      : r.source_kind === 'text_layer' ? '文本层' : '类型未知';
    const pages = r.page_count ? `${r.page_count} 页` : '页数未知';
    const err = r.parse_error
      ? `<div class="item-meta" style="color:var(--danger)">${esc(r.parse_error)}</div>` : '';
    return `
      <div class="item">
        <div class="item-row">
          <div class="item-main">
            <div class="item-title">
              ${r.is_default ? '<span class="badge ok">主简历</span>' : ''}
              <span>${esc(r.file_name)}</span>
              <span class="badge ${cls}">${label}</span>
              ${r.has_profile ? '<span class="badge info">可匹配</span>' : ''}
            </div>
            <div class="item-meta">${kind} · ${pages} · #${r.id}</div>
            ${err}
          </div>
          <div class="item-actions">
            <button class="small" data-view-resume="${r.id}">查看</button>
            ${!r.is_default && r.has_profile ? `<button class="small" data-set-default="${r.id}">设为主简历</button>` : ''}
            <button class="small" data-reparse="${r.id}">重解析</button>
            <button class="small danger" data-delete="${r.id}">删除</button>
          </div>
        </div>
        <div class="resume-body" data-body="${r.id}"></div>
      </div>`;
  }).join('');

  box.querySelectorAll('[data-view-resume]').forEach((btn) => {
    btn.addEventListener('click', () => toggleResumeDetail(btn));
  });

  box.querySelectorAll('[data-set-default]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await withLoading(btn, '设置中', async () => {
        try {
          await api(`/api/resumes/${btn.dataset.setDefault}/default`, { method: 'PUT' });
          toast('已设为主简历：匹配、建议、定制、HR 回复都将默认用它');
          await loadResumes();
        } catch (e) { toast(e.message, true); }
      });
    });
  });

  box.querySelectorAll('[data-reparse]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await withLoading(btn, '提交中', async () => {
        try {
          await api(`/api/resumes/${btn.dataset.reparse}/reparse`, { method: 'POST' });
          toast('已提交重新解析');
          await loadResumes();
        } catch (e) { toast(e.message, true); }
      });
    });
  });

  box.querySelectorAll('[data-delete]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.delete;
      if (!confirm(`彻底删除简历 #${id}？数据库记录与磁盘上的原始 PDF 都会被删除，无法恢复。`)) return;
      await withLoading(btn, '删除中', async () => {
        try {
          const res = await api(`/api/resumes/${id}`, { method: 'DELETE' });
          toast(res.file_removed ? '已删除（含磁盘文件）' : '已删除');
          await loadResumes();
        } catch (e) { toast(e.message, true); }
      });
    });
  });
}

/**
 * 展开/收起简历详情。内容按需拉取：列表要轮询刷新，
 * 每次刷新都把所有人的画像一起带上会让轮询变重。
 */
async function toggleResumeDetail(btn) {
  const id = btn.dataset.viewResume;
  const card = btn.closest('.item');
  const body = card.querySelector(`[data-body="${id}"]`);
  if (!body) return;

  if (body.classList.contains('open')) {
    body.classList.remove('open');
    btn.textContent = '查看';
    return;
  }

  if (!body.dataset.loaded) {
    btn.disabled = true;
    btn.textContent = '读取中…';
    try {
      const detail = await api(`/api/resumes/${id}`);
      body.innerHTML = renderResumeDetail(detail);
      body.dataset.loaded = '1';
    } catch (e) {
      toast(e.message, true);
      btn.textContent = '查看';
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
  }
  body.classList.add('open');
  btn.textContent = '收起';
}

/** 画像 + 系统实际读到的原文。两者并排才能判断字段抽错是版面问题还是模型问题 */
function renderResumeDetail(detail) {
  const p = detail.profile;
  const fileLink = `<a class="jd-link" href="/api/resumes/${detail.id}/file" target="_blank" rel="noreferrer">打开原始 PDF ↗</a>`;

  if (!p) {
    return `
      <div class="jd-meta">${fileLink}</div>
      <div class="empty">还没有解析出画像（状态：${esc(detail.parse_status)}）。${detail.parse_error ? '原因：' + esc(detail.parse_error) : ''}</div>`;
  }

  const b = p.basic || {};
  const kv = [
    ['学历', b.edu_level],
    ['统招', b.edu_fulltime === true ? '是' : b.edu_fulltime === false ? '否' : '未标注'],
    ['学校', b.school],
    ['专业', b.major],
    ['毕业年份', b.graduation_year],
    ['工作年限', b.work_years != null ? `${b.work_years} 年` : null],
    ['当前城市', b.current_city],
  ].filter(([, v]) => v != null && v !== '');

  const skills = (p.skills || [])
    .map((s) => (typeof s === 'string' ? s : s.name))
    .filter(Boolean);

  const exps = (p.experiences || []).map((e) => `
    <div class="tl-item">
      <div class="tl-head">${esc([e.company, e.title].filter(Boolean).join(' · ') || '未命名经历')}</div>
      <div class="tl-meta">${esc([e.city, [e.start, e.end || '至今'].filter(Boolean).join(' → ')].filter(Boolean).join(' · '))}</div>
      ${(e.highlights || []).length
        ? `<ul class="tl-list">${e.highlights.map((h) => `<li>${esc(h)}</li>`).join('')}</ul>` : ''}
    </div>`).join('');

  const projects = (p.projects || []).map((x) => `
    <div class="tl-item">
      <div class="tl-head">${esc(x.name || '未命名项目')}${x.role ? ' · ' + esc(x.role) : ''}</div>
      ${(x.tech || []).length ? `<div class="tl-meta">${esc(x.tech.join(' / '))}</div>` : ''}
      ${(x.highlights || []).length
        ? `<ul class="tl-list">${x.highlights.map((h) => `<li>${esc(h)}</li>`).join('')}</ul>` : ''}
    </div>`).join('');

  return `
    <div class="jd-meta">${fileLink}</div>
    <div class="kv">
      ${kv.map(([k, v]) => `<div class="kv-item"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('')}
    </div>
    ${skills.length ? `<div class="sec-title">技能</div><div class="chips">${skills.map((s) => `<span class="chip">${esc(s)}</span>`).join('')}</div>` : ''}
    ${exps ? `<div class="sec-title">工作经历</div><div class="timeline">${exps}</div>` : ''}
    ${projects ? `<div class="sec-title">项目经历</div><div class="timeline">${projects}</div>` : ''}
    ${detail.extract_meta && detail.extract_meta.quality
      ? `<div class="sec-title">抽取质量</div>
         <div class="item-meta">策略 ${esc(detail.extract_meta.strategy || 'segments')} ·
           质量分 ${detail.extract_meta.quality.score} ·
           ${detail.extract_meta.quality.ok ? '通过' : '未通过（已自动换策略重试）'}</div>
         ${(detail.extract_meta.attempts || []).length > 1
            ? `<ul class="tl-list">${detail.extract_meta.attempts.map((a) => `<li>${
                esc(a.strategy)}${a.error ? '：' + esc(a.error) : '：质量分 ' + a.score}${a.detail ? '（' + esc(a.detail) + '）' : ''}</li>`).join('')}</ul>`
            : ''}`
      : ''}
    ${detail.text ? `<div class="sec-title">系统读到的原文（按阅读顺序还原）</div><pre class="raw-text">${esc(detail.text)}</pre>` : ''}
  `;
}

async function loadResumes() {
  try {
    const data = await api('/api/resumes');
    state.resumes = data.items || [];
    renderResumes();
    fillResumeSelects();
    schedulePoll();
  } catch (e) {
    toast(e.message, true);
  }
}

/** 有未完成的解析就 2 秒后重新拉列表，直到全部结束 */
function schedulePoll() {
  clearTimeout(state.pollTimer);
  const busy = state.resumes.some((r) => r.parse_status === 'parsing' || r.parse_status === 'pending');
  if (busy) {
    state.pollTimer = setTimeout(loadResumes, 2000);
  }
}

function fillResumeSelects() {
  const usable = state.resumes.filter((r) => r.has_profile);
  const targets = [
    ['match-resume', '选择简历'],
    ['review-resume', '选择简历'],
    ['job-sort-resume', '不排序'],
  ];
  for (const [id, placeholder] of targets) {
    const sel = document.getElementById(id);
    if (!sel) continue; // 岗位页的排序控件在旧版页面里可能不存在，跳过即可
    const prev = sel.value;
    sel.innerHTML = '<option value="">' + placeholder + '</option>' +
      usable.map((r) => `<option value="${r.id}">#${r.id} ${esc(r.file_name)}</option>`).join('');
    if (prev && usable.some((r) => String(r.id) === prev)) sel.value = prev;
  }

  // 匹配页默认选主简历：每次进来都要手动挑一份很烦，而"主简历"本来就是这层语义。
  // 只对匹配页做，建议页/排序控件保持空值（它们的语义是"可选"）。
  const matchSel = document.getElementById('match-resume');
  if (matchSel && !matchSel.value && usable.length) {
    const primary = usable.find((r) => r.is_default) || usable[0];
    matchSel.value = primary.id;
  }
}

document.getElementById('btn-upload').addEventListener('click', async (ev) => {
  const input = document.getElementById('resume-file');
  if (!input.files.length) { toast('先选择一个 PDF 或 DOCX 文件', true); return; }

  await withLoading(ev.currentTarget, '上传中', async () => {
    const form = new FormData();
    form.append('file', input.files[0]);
    setStatus('upload-status', '上传中…', 'busy');
    try {
      const res = await api('/api/resumes/upload', { method: 'POST', body: form });
      setStatus('upload-status',
        res.reused ? `已存在，复用 #${res.resume_id} 并重新解析` : `已接收 #${res.resume_id}，正在解析…`, 'busy');
      input.value = '';
      await loadResumes();
    } catch (e) {
      setStatus('upload-status', e.message, 'error');
    }
  });
});
/** 按岗位定制简历：调用后端改 DOCX，逐条展示改动与被拒原因 */
async function tailorForJob(btn) {
  const jobId = btn.dataset.tailor;
  const card = btn.closest('.match-card');
  const body = card.querySelector('.tailor-body');
  const resumeId = document.getElementById('match-resume').value;
  if (!resumeId) { toast('先选择一份简历', true); return; }

  body.classList.add('open');
  body.innerHTML = '<div class="tailor-loading">正在按岗位改写，约 20–40 秒…</div>';
  btn.disabled = true;
  try {
    const data = await api(`/api/resumes/${resumeId}/tailor/${jobId}`, { method: 'POST' });
    renderTailor(body, data);
    if (data.accepted > 0) {
      toast(`已生成定制版：${data.accepted} 处改动`);
    } else {
      toast(data.note || '模型判断无需改动');
    }
  } catch (e) {
    body.innerHTML = `<div class="tailor-empty" style="color:var(--danger)">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

// 三种来源的交代方式。用户最在意"是不是我原来那份"，所以必须说清
const ORIGIN_NOTE = {
  docx: '在你的原 Word 上只改文字，版式与样式未改动',
  pdf_converted: '原文件是 PDF，已按原版式转成 Word 后只改文字；极个别细节可能与原 PDF 有微小差异',
  rebuilt: '原 PDF 无法保版式转换，这份是按内容重建的，版式不是你的原版',
};

function renderTailor(box, data) {
  const diffs = (data.diff || []).filter((d) => d.accepted);
  const rejected = data.rejected || [];
  const originNote = ORIGIN_NOTE[data.origin] || ORIGIN_NOTE.rebuilt;
  const originWarn = data.origin === 'rebuilt';

  box.innerHTML = `
    <div class="tailor-panel">
      <div class="tailor-head">
        <span class="tailor-title">定制结果</span>
        <span class="badge ok">${data.accepted} 处改动</span>
        ${rejected.length ? `<span class="badge warn">${rejected.length} 处被拒</span>` : ''}
        <span class="count">¥${(data.cost_cny || 0).toFixed(4)}</span>
        ${data.download_url
          ? `<a class="tailor-download" href="${data.download_url}" target="_blank" rel="noreferrer">下载 Word ↓</a>`
          : ''}
      </div>
      <p class="tailor-note"${originWarn ? ' style="color:var(--danger)"' : ''}>${esc(originNote)}${data.summary ? ' · ' + esc(data.summary) : ''}</p>
      ${data.hintTotal && data.accepted < data.hintTotal ? `
        <p class="tailor-note" style="color:var(--text-2)">
          诊断共 ${data.hintTotal} 条，本次自动应用 ${data.accepted} 处。其余涉及时间线、项目名、期望城市、经历缺失，
          需要你先确认真实情况才能改 —— 程序不会替你编一段没发生过的经历，也不会替你决定去哪个城市。
        </p>` : ''}
      ${data.accepted === 0 && data.download_url
        ? `<div class="tailor-empty">这一版没有自动改动：${esc(data.note || '')}。文件已按原文输出，可以在 Word 里自己补。</div>`
        : ''}
      ${diffs.map((d) => `
        <div class="tailor-diff">
          <div class="diff-row"><span class="diff-tag">原文</span><span class="diff-before">${esc(d.before)}</span></div>
          <div class="diff-row"><span class="diff-tag after">改后</span><span class="diff-after">${esc(d.after)}</span></div>
          <div class="diff-why">${esc(d.reason || '')}${d.evidence ? ' · 依据：' + esc(d.evidence) : ''}</div>
        </div>`).join('')}
      ${rejected.length ? `
        <div class="tailor-rejected">
          <div class="sec-title">未采纳的改动（附原因）</div>
          ${rejected.map((r) => `<div class="rej-row"><span class="rej-why">${esc(r.why)}</span><span class="rej-text">${esc((r.text || '').slice(0, 60))}</span></div>`).join('')}
        </div>` : ''}
    </div>`;
}

/** 定制简历按钮同样用事件委托（卡片会整体重建） */
document.getElementById('match-result').addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-tailor]');
  if (!btn) return;
  tailorForJob(btn);
});

/* ---------- 岗位 ---------- */

/** 平台名与颜色：Boss 蓝、猎聘橙、智联绿、手动灰 */
const PLATFORM_META = {
  boss: { label: 'BOSS直聘', cls: 'boss' },
  liepin: { label: '猎聘', cls: 'liepin' },
  zhaopin: { label: '智联招聘', cls: 'zhaopin' },
  manual: { label: '手动粘贴', cls: 'manual' },
};

function platformBadge(source) {
  const meta = PLATFORM_META[source];
  if (!meta) return '';
  return `<span class="badge plat ${meta.cls}">${meta.label}</span>`;
}

function renderPlatformBar() {
  const bar = document.getElementById('platform-bar');
  const counts = state.jobSources || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const items = [
    ['', '全部平台', total],
    ...Object.entries(PLATFORM_META).map(([key, meta]) => [key, meta.label, counts[key] || 0]),
  ].filter(([key, , n]) => key !== '' || n > 0);

  bar.innerHTML = items.map(([key, label, n]) => `
    <button class="plat-chip ${state.jobSource === key ? 'active' : ''}" data-source="${key}">
      ${label}<span class="n">${n}</span>
    </button>`).join('');

  bar.querySelectorAll('[data-source]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.jobSource = btn.dataset.source;
      renderPlatformBar();
      renderJobs();
    });
  });
}

function renderJobs() {
  const box = document.getElementById('job-list');
  const scored = Object.keys(state.jobScores).length > 0;
  document.getElementById('job-count').textContent =
    state.jobs.length ? `共 ${state.jobs.length} 个` : '';

  // 有分数时排序：默认按分数降序（最直观），也可切成"通过优先"（与匹配页同口径）。
  // 分数是本地规则算的，切换简历可以即时重排，不花模型调用
  const ORDER = { pass: 0, confirm: 1, reject: 2 };
  const mode = document.getElementById('job-sort-mode').value;
  const visible = state.jobSource
    ? state.jobs.filter((j) => (j.source || 'manual') === state.jobSource)
    : state.jobs;

  const jobs = scored
    ? [...visible].sort((a, b) => {
      const sa = state.jobScores[a.id];
      const sb = state.jobScores[b.id];
      if (!sa && !sb) return b.id - a.id;
      if (!sa) return 1;
      if (!sb) return -1;
      if (mode === 'decision') {
        const da = ORDER[sa.decision] ?? 3;
        const db = ORDER[sb.decision] ?? 3;
        return da - db || (sb.score ?? 0) - (sa.score ?? 0);
      }
      return (sb.score ?? 0) - (sa.score ?? 0);
    })
    : visible;

  if (!jobs.length) {
    box.innerHTML = state.jobSource
      ? `<div class="empty">${PLATFORM_META[state.jobSource]?.label || state.jobSource} 下还没有岗位</div>`
      : '<div class="empty">还没有岗位，先粘贴一条 JD，或用插件从招聘网站抓取</div>';
  } else {
    box.innerHTML = jobs.map((j) => {
      const exp = j.exp_min != null
        ? `${j.exp_min}${j.exp_max != null ? '–' + j.exp_max : '+'} 年`
        : '年限未标';
      const must = (j.must_have_skills || []);
      // 技能用 chip 展示而不是拼成一行：一行几十个字很难扫。
      // 但抽取出的技能常是整句要求，所以 chip 要截断 + hover 看全文
      const chips = must.slice(0, 6)
        .map((s) => `<span class="chip chip-clip" title="${esc(s)}">${esc(s)}</span>`).join('');
      const more = must.length > 6 ? `<span class="chip">+${must.length - 6}</span>` : '';
      const sc = state.jobScores[j.id];
      const decide = sc
        ? `<span class="badge ${sc.decision === 'pass' ? 'ok' : sc.decision === 'reject' ? 'bad' : 'warn'}">${
          { pass: '通过', confirm: '待确认', reject: '否决' }[sc.decision] || sc.decision}</span>`
        : '';
      const scoreTag = sc && sc.score != null ? `<span class="score">${sc.score} 分</span>` : '';
      return `
        <div class="item">
          <div class="item-row">
            <div class="item-main">
              <div class="item-title">
                <span>${esc(j.title || '（未抽取标题）')}</span>
                ${decide}
                ${platformBadge(j.source || 'manual')}
                ${j.job_nature ? `<span class="badge">${esc(j.job_nature)}</span>` : ''}
                ${(j.graduate_years || []).length
                  ? `<span class="badge info">${j.graduate_years.join('/')} 届</span>` : ''}
                ${j.edu_level ? `<span class="badge">${esc(j.edu_level)}</span>` : ''}
                ${j.city ? `<span class="badge">${esc(j.city)}</span>` : ''}
                ${j.detail_level === 'full' ? '<span class="badge ok">完整 JD</span>'
                  : j.detail_level === 'digest' ? '<span class="badge warn">仅摘要</span>' : ''}
                ${scoreTag}
              </div>
              <div class="item-meta">${esc(exp)} · #${j.id}${j.url ? ' · <a class="jd-link" href="' + esc(j.url) + '" target="_blank" rel="noreferrer">原站链接 ↗</a>' : ''}</div>
              ${must.length
                ? `<div class="chips" style="margin-top:6px">${chips}${more}</div>`
                : `<div class="item-meta" style="margin-top:6px;color:var(--faint)">未列出必备技能</div>`}
            </div>
            <div class="item-actions">
              <button class="small" data-view-job="${j.id}">查看 JD</button>
              <button class="small" data-workbench="${j.id}">工作台</button>
            </div>
          </div>
          <div class="jd-body" data-job-body="${j.id}"></div>
        </div>`;
    }).join('');

    box.querySelectorAll('[data-view-job]').forEach((btn) => {
      btn.addEventListener('click', () => toggleJobDetail(btn));
    });
  }

  const sel = document.getElementById('review-job');
  const prev = sel.value;
  sel.innerHTML = '<option value="">不指定（普适体检）</option>' +
    state.jobs.map((j) => `<option value="${j.id}">#${j.id} ${esc(j.title || '未命名')}</option>`).join('');
  if (prev) sel.value = prev;
}

async function toggleJobDetail(btn) {
  const id = btn.dataset.viewJob;
  const card = btn.closest('.item');
  const body = card.querySelector(`[data-job-body="${id}"]`);
  if (!body) return;

  if (body.classList.contains('open')) {
    body.classList.remove('open');
    btn.textContent = '查看 JD';
    return;
  }

  if (!body.dataset.loaded) {
    btn.disabled = true;
    btn.textContent = '读取中…';
    try {
      const d = await api(`/api/jobs/${id}`);
      const p = d.profile || {};
      const chips = (list, label) => ((list || []).length
        ? `<div class="jd-skills"><span class="skill-label">${label}</span>${list
          .map((s) => `<span class="skill-chip">${esc(s)}</span>`).join('')}</div>`
        : '');
      // 卡片顶部已经列过必备技能（最多 6 条），这里只在被截断时补全，
      // 否则同一组 chip 会在同一张卡上出现两次
      const mustAll = p.must_have_skills || [];
      const mustExtra = mustAll.length > 6 ? chips(mustAll.slice(6), '必备（续）') : '';
      body.innerHTML = `
        <div class="jd-meta">
          <span class="badge ${d.detail_level === 'full' ? 'ok' : 'warn'}">
            ${d.detail_level === 'full' ? '完整 JD' : '仅列表摘要'}
          </span>
          ${d.url
            ? `<a class="jd-link" href="${esc(d.url)}" target="_blank" rel="noreferrer">在原网站打开 ↗</a>`
            : ''}
        </div>
        ${chips(p.nice_to_have_skills, '加分')}
        ${mustExtra}
        <pre class="jd-text">${esc(d.raw_text || '（没有保存 JD 原文）')}</pre>`;
      body.dataset.loaded = '1';
    } catch (e) {
      toast(e.message, true);
      btn.textContent = '查看 JD';
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
  }
  body.classList.add('open');
  btn.textContent = '收起';
}

async function loadJobs() {
  try {
    const data = await api('/api/jobs');
    state.jobs = data.items || [];
    state.jobSources = data.sources || {};
    renderPlatformBar();
    renderJobs();
    // 正在按匹配度排序时，新入库或重抽过的岗位需要重新算分
    const selected = document.getElementById('job-sort-resume').value;
    if (selected) await loadJobScores(selected);
  } catch (e) {
    toast(e.message, true);
  }
}

/** 拉一份「岗位 -> 分数」的轻量表并重排。纯本地规则，不花模型调用 */
async function loadJobScores(resumeId) {
  if (!resumeId) {
    state.jobScores = {};
    renderJobs();
    return;
  }
  try {
    const data = await api(`/api/resumes/${resumeId}/job-scores`);
    const map = {};
    for (const item of data.items || []) map[item.job_id] = item;
    state.jobScores = map;
    renderJobs();
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById('job-sort-resume').addEventListener('change', (ev) => {
  loadJobScores(ev.currentTarget.value);
});
document.getElementById('job-sort-mode').addEventListener('change', () => {
  renderJobs();
});

document.getElementById('btn-ingest').addEventListener('click', async (ev) => {
  const text = document.getElementById('jd-text').value.trim();
  if (text.length < 30) { toast('JD 太短，至少 30 个字符', true); return; }

  await withLoading(ev.currentTarget, '解析中…', async () => {
    setStatus('ingest-status', '抽取字段中，约 10–20 秒…', 'busy');
    try {
      const res = await api('/api/jobs/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: 'manual', raw_text: text }),
      });
      setStatus('ingest-status', `已入库 #${res.job_id}（¥${res.cost_cny}）`, 'done');
      document.getElementById('jd-text').value = '';
      await loadJobs();
    } catch (e) {
      setStatus('ingest-status', e.message, 'error');
    }
  });
});

/* ---------- 匹配 ---------- */

const DECISION_LABEL = { pass: '通过', confirm: '待确认', reject: '否决' };

/* ---------- 岗位横向对比 ---------- */

// 上限 4 个。不是界面限制，是承认"选太多等于没选" ——
// 求职者真实的纠结通常在两三个之间，列越宽越难权衡。
const CMP_MAX = 4;

function updateCmpBar() {
  const bar = document.getElementById('cmp-bar');
  if (!bar) return;
  const n = state.compare.size;
  bar.hidden = n === 0;
  const count = document.getElementById('cmp-count');
  if (count) count.textContent = String(n);
  const btn = document.getElementById('btn-cmp');
  if (btn) {
    btn.disabled = n < 2;
    btn.textContent = n < 2 ? '至少选 2 个' : `对比这 ${n} 个`;
  }
}

async function showCompare() {
  const panel = document.getElementById('cmp-panel');
  const items = (state.matches?.items || []).filter((i) => state.compare.has(String(i.job_id)));
  if (!panel || items.length < 2) return;

  panel.innerHTML = '<div class="panel"><div class="empty">加载中…</div></div>';
  // 薪资与城市不在匹配结果里（那层只关心决策），要单独取一次
  const details = await Promise.all(
    items.map((i) => api(`/api/jobs/${i.job_id}`).catch(() => null)),
  );

  const col = (fn) => items.map((item, idx) => `<td>${fn(item, idx)}</td>`).join('');
  const rows = [
    ['公司', col((i) => esc(i.company || '—'))],
    [
      '城市 / 薪资',
      col((i, idx) => {
        const d = details[idx] || {};
        const city = (d.profile && d.profile.city) || '未标注';
        const salary = d.salary ? ' · ' + d.salary : '';
        return esc(city) + esc(salary);
      }),
    ],
    [
      '结论',
      col((i) => {
        const cls = i.decision === 'pass' ? 'ok' : i.decision === 'reject' ? 'bad' : 'warn';
        return `<span class="badge ${cls}">${DECISION_LABEL[i.decision] || i.decision}</span>`;
      }),
    ],
    ['信息完整度', col((i) => (i.confidence != null ? Math.round(i.confidence * 100) + '%' : '—'))],
    ['参考分', col((i) => (i.score != null ? String(i.score) : esc(i.score_note || '未评分')))],
    [
      '缺口技能',
      col((i) => {
        const missed = i.missed_skills || [];
        return missed.length
          ? `<span class="missed">${esc(missed.join('、'))}</span>`
          : '<span class="ok-text">无</span>';
      }),
    ],
  ];

  panel.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <h2>岗位对比</h2>
        <button class="small" id="btn-cmp-close" style="margin-left:auto">收起</button>
      </div>
      <div class="cmp-wrap">
        <table class="cmp-table">
          <thead><tr><th>对比项</th>${items
            .map((i) => `<th>${esc(i.title || '未命名岗位')}</th>`)
            .join('')}</tr></thead>
          <tbody>${rows.map(([label, cells]) => `<tr><th>${label}</th>${cells}</tr>`).join('')}</tbody>
        </table>
      </div>
      <p class="hint">缺口一栏逐项列出。如果几列缺的是同一项，说明它是这批岗位的共性门槛。</p>
    </div>`;

  panel.querySelector('#btn-cmp-close').addEventListener('click', () => {
    panel.innerHTML = '';
  });
}

function renderMatches(data) {
  const box = document.getElementById('match-result');
  const counts = data.counts || {};
  // 供「岗位对比」取字段用。不重新请求接口 —— 对比必须和列表看到的是同一批数据
  state.matches = data;

  if (!data.items || !data.items.length) {
    box.innerHTML = '<div class="panel"><div class="empty">没有可显示的结果。若开启了「显示被否决」仍为空，可能是岗位库还没有数据。</div></div>';
    return;
  }

  const groups = [
    ['pass', '通过（信息完整，已排序）'],
    ['confirm', '待确认（信息缺失，不能直接判否）'],
    ['reject', '否决（硬约束不满足）'],
  ];

  box.innerHTML = `
    <div class="summary-bar">
      <div class="summary-item pass"><div class="n">${counts.pass || 0}</div><div class="t">通过</div></div>
      <div class="summary-item confirm"><div class="n">${counts.confirm || 0}</div><div class="t">待确认</div></div>
      <div class="summary-item reject"><div class="n">${counts.reject || 0}</div><div class="t">否决</div></div>
      <div class="summary-item"><div class="n">${data.total_jobs || 0}</div><div class="t">已比对岗位</div></div>
    </div>
    <div class="cmp-bar" id="cmp-bar" hidden>
      已选 <b id="cmp-count">0</b> 个岗位
      <button id="btn-cmp" class="small primary" disabled>至少选 2 个</button>
      <button id="btn-cmp-clear" class="small">清空</button>
    </div>
    ${data.cached
      ? `<p class="hint">这组条件的结果来自缓存（${formatWhen(data.cached_at)}算的，${data.elapsed_ms || 0}ms 命中）。
           简历重解析或岗位库有变化会自动重算。
           <button class="link-btn" id="btn-match-force">重新匹配</button></p>`
      : ''}
    ${groups.map(([key, title]) => {
        const items = data.items.filter((i) => i.decision === key);
        if (!items.length) return '';
        const cards = items.map((i) => `
              <div class="match-card ${key}">
                <div class="match-head">
                  <input type="checkbox" class="cmp-check" data-cmp="${i.job_id}"
                         ${state.compare.has(String(i.job_id)) ? 'checked' : ''}
                         title="加入对比（最多 ${CMP_MAX} 个）">
                  <span class="match-title">${esc(i.title || '未命名岗位')}</span>
                  <span class="badge ${key === 'pass' ? 'ok' : key === 'reject' ? 'bad' : 'warn'}">${DECISION_LABEL[key]}</span>
                  ${i.company ? `<span class="item-meta">${esc(i.company)}</span>` : ''}
                  ${i.detail_level === 'digest'
                    ? '<span class="badge warn">仅摘要·技能匹配不可信</span>' : ''}
                  ${i.confidence != null
                    ? `<span class="badge" title="岗位字段缺失：${esc((i.info_missing || []).join('、') || '无')}">信息 ${Math.round(i.confidence * 100)}%</span>`
                    : ''}
                  ${i.score != null
                    ? `<span class="score" title="参考分：依据岗位有限的几项字段算出，不代表录用概率">${i.score} 分</span>`
                    : `<span class="item-meta">${esc(i.score_note || '信息不足，未评分')}</span>`}
                </div>
                <ul class="reason-list">
                  ${(i.reasons || [])
                    // 缺口在下面单独用红字列出，这里跳过避免同一句话说两遍
                    .filter((r) => !r.startsWith('未覆盖的必备技能'))
                    .map((r) => `<li>${esc(r)}</li>`).join('')}
                </ul>
                ${(i.missed_skills || []).length
                  ? `<div class="item-meta" style="margin-top:6px">未覆盖的必备技能：<span class="missed">${esc(i.missed_skills.join('、'))}</span></div>`
                  : ''}
                <div class="jd-actions">
                  <button class="link-btn" data-jd="${i.job_id}" data-level="${esc(i.detail_level || 'unknown')}">
                    查看完整 JD${i.detail_level === 'digest' ? '（仅摘要）' : ''}
                  </button>
                  <span class="act-sep">·</span>
                  <button class="link-btn" data-tailor="${i.job_id}">按此岗位定制简历</button>
                  <span class="act-sep">·</span>
                  <button class="link-btn" data-workbench="${i.job_id}">工作台</button>
                </div>
                <div class="jd-body"></div>
                <div class="tailor-body"></div>
              </div>`).join('');

        // 否决项默认折叠：真实数据里否决常有十几二十条，全部展开会把
        // 「通过」挤到两屏之外，用户得先滚过一堆不相关的东西才能看到结果
        if (key === 'reject') {
          return `
          <div class="result-group">
            <details class="group-details">
              <summary class="group-title">${title} · ${items.length} 个（点击展开，含每条被卡的硬约束）</summary>
              ${cards}
            </details>
          </div>`;
        }
        return `
          <div class="result-group">
            <p class="group-title">${title}</p>
            ${cards}
          </div>`;
      }).join('')}`;

  // 缓存提示里的「重新匹配」是局部重算，不走顶部按钮，避免整页 loading
  const forceBtn = box.querySelector('#btn-match-force');
  if (forceBtn) forceBtn.addEventListener('click', () => runMatch(true));

  updateCmpBar();
}

/**
 * JD 展开用事件委托，不逐条绑监听：
 * 每次重新匹配都会整体重建 innerHTML，逐条绑的话监听会随旧节点一起丢掉。
 * 内容按需拉取 —— 一次匹配可能有几十个岗位，全量塞进首屏响应没意义。
 */
document.getElementById('match-result').addEventListener('change', (ev) => {
  const cb = ev.target.closest('[data-cmp]');
  if (!cb) return;
  const id = String(cb.dataset.cmp);
  if (cb.checked) {
    if (state.compare.size >= CMP_MAX) {
      cb.checked = false;
      toast(`最多同时对比 ${CMP_MAX} 个岗位`, true);
      return;
    }
    state.compare.add(id);
  } else {
    state.compare.delete(id);
  }
  updateCmpBar();
});

document.getElementById('match-result').addEventListener('click', (ev) => {
  if (ev.target.closest('#btn-cmp')) {
    showCompare();
    return;
  }
  if (ev.target.closest('#btn-cmp-clear')) {
    state.compare.clear();
    document.querySelectorAll('[data-cmp]').forEach((cb) => { cb.checked = false; });
    updateCmpBar();
    const panel = document.getElementById('cmp-panel');
    if (panel) panel.innerHTML = '';
  }
});

document.getElementById('match-result').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-jd]');
  if (!btn) return;
  const card = btn.closest('.match-card');
  const body = card.querySelector('.jd-body');
  if (!body) return;

  if (body.classList.contains('open')) {
    body.classList.remove('open');
    btn.textContent = btn.dataset.label || '查看完整 JD';
    return;
  
}

  if (!body.dataset.loaded) {
    const original = btn.textContent;
    btn.dataset.label = original;
    btn.disabled = true;
    btn.textContent = '读取中…';
    try {
      const detail = await api(`/api/jobs/${btn.dataset.jd}`);
      const level = detail.detail_level || btn.dataset.level || 'unknown';
      const levelText = { full: '完整 JD', digest: '仅列表摘要', unknown: '未知' }[level] || '未知';
      const p = detail.profile || {};
      const chips = (list) => (list || []).map((s) => `<span class="skill-chip">${esc(s)}</span>`).join('');
      body.innerHTML = `
        <div class="jd-meta">
          <span class="badge ${level === 'full' ? 'ok' : 'warn'}">${esc(levelText)}</span>
          ${detail.url
            ? `<a class="jd-link" href="${esc(detail.url)}" target="_blank" rel="noreferrer">在原网站打开 ↗</a>`
            : '<span class="item-meta">手动粘贴的岗位，没有原始链接</span>'}
        </div>
        ${(p.must_have_skills || []).length
          ? `<div class="jd-skills"><span class="skill-label">必备</span>${chips(p.must_have_skills)}</div>`
          : ''}
        ${(p.nice_to_have_skills || []).length
          ? `<div class="jd-skills"><span class="skill-label">加分</span>${chips(p.nice_to_have_skills)}</div>`
          : ''}
        <pre class="jd-text">${esc(detail.raw_text || '（没有保存 JD 原文）')}</pre>
      `;
      body.dataset.loaded = '1';
      btn.textContent = '收起 JD';
    } catch (e) {
      toast(e.message, true);
      btn.textContent = original;
    } finally {
      btn.disabled = false;
    }
  } else {
    btn.textContent = '收起 JD';
  }
  body.classList.add('open');
});

/** 放宽探索：把"全部被否"变成"差在哪、放宽哪一条能通" */
document.getElementById('btn-relax').addEventListener('click', async (ev) => {
  const resumeId = document.getElementById('match-resume').value;
  if (!resumeId) { toast('先选择一份已解析的简历', true); return; }

  await withLoading(ev.currentTarget, '计算中…', async () => {
    setStatus('match-status', '正在算放宽后的结果…', 'busy');
    try {
      const params = new URLSearchParams({
        accept_relocation: document.getElementById('match-relocation').checked,
        accept_outsourcing: document.getElementById('match-outsourcing').checked,
        accept_internship: document.getElementById('match-internship').checked,
        target_cities: document.getElementById('match-cities').value.trim(),
      });
      const data = await api(`/api/resumes/${resumeId}/relaxations?${params}`, { method: 'POST' });
      renderRelaxations(data);
      setStatus('match-status', '放宽方案已生成', 'done');
    } catch (e) {
      setStatus('match-status', e.message, 'error');
    }
  });
});

function renderRelaxations(data) {
  const box = document.getElementById('relax-result');
  const base = data.base || {};
  const advice = String(data.advice || '').split('\n').map((x) => x.trim()).filter(Boolean);
  const variants = data.variants || [];

  box.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <h2>放宽约束后</h2>
        <span class="count">当前：通过 ${base.pass || 0} · 待确认 ${base.confirm || 0} · 否决 ${base.reject || 0}</span>
      </div>
      ${advice.length
        ? `<div class="sg-block"><span class="label">建议</span>${advice.map((a) => `<div>${esc(a)}</div>`).join('')}</div>`
        : ''}
      <div class="list">
        ${variants.map((v) => `
          <div class="item">
            <div class="item-row">
              <div class="item-main">
                <div class="item-title">
                  <span>${esc(v.label)}</span>
                  <span class="badge ${v.pass_count > (base.pass || 0) ? 'ok' : ''}">通过 ${base.pass || 0} → ${v.pass_count}</span>
                  <span class="badge">新增可投 ${v.opened_total}</span>
                </div>
                ${(v.opened || []).length
                  ? `<ul class="reason-list">${v.opened.map((o) => `
                      <li>${esc(o.title)}（${esc(o.city || '城市未标')}，${o.score} 分）— 原先卡在：${esc((o.was_blocked_by || []).join('；') || '未记录')}</li>`).join('')}</ul>`
                  : '<div class="item-meta">放宽这一项没有变化 —— 它不是瓶颈</div>'}
              </div>
            </div>
          </div>`).join('')}
      </div>
      <p class="hint">放宽只是给你一张"如果……会怎样"的地图，正式匹配仍按原规则否决。</p>
    </div>`;
}

async function runMatch(force) {
  const resumeId = document.getElementById('match-resume').value;
  if (!resumeId) { toast('先选择一份已解析的简历', true); return; }
  const btn = document.getElementById('btn-match');

  await withLoading(btn, '匹配中…', async () => {
    setStatus('match-status', force ? '按当前条件重新计算…' : '过滤与排序中…', 'busy');
    try {
      const params = new URLSearchParams({
        include_rejected: document.getElementById('match-rejected').checked,
        accept_relocation: document.getElementById('match-relocation').checked,
        accept_outsourcing: document.getElementById('match-outsourcing').checked,
        accept_internship: document.getElementById('match-internship').checked,
        target_cities: document.getElementById('match-cities').value.trim(),
      });
      if (force) params.set('force', 'true');
      const data = await api(`/api/resumes/${resumeId}/matches?${params}`);
      const how = data.cached
        ? `命中缓存 · ${formatWhen(data.cached_at)}算的`
        : `实算 ${((data.elapsed_ms || 0) / 1000).toFixed(1)}s`;
      setStatus('match-status', `已比对 ${data.total_jobs} 个岗位 · ${how}`, 'done');
      renderMatches(data);
      loadAdvice(resumeId);
    } catch (e) {
      setStatus('match-status', e.message, 'error');
    }
  });
}

/* ---------- 搜索策略：把否决模式翻译成「该搜什么」 ---------- */

// 为什么放在匹配页而不是单独一页：
// 它的全部结论都建立在"刚跑出来的这批结果"上，换个页面看会失去上下文。
// 没有匹配记录时静默不占屏（接口会返回 409）。
async function loadAdvice(resumeId) {
  const box = document.getElementById('advice-box');
  if (!box) return;
  if (!resumeId) {
    box.innerHTML = '';
    return;
  }
  try {
    const d = await api(`/api/advice/search-strategy?resume_id=${resumeId}`);
    renderAdvice(box, d);
  } catch {
    box.innerHTML = '';
  }
}

function renderAdvice(box, d) {
  const diag = d.diagnosis || {};
  const terms = d.search_terms || [];
  const fill = d.fill_resume || [];
  const miss = d.change_direction || [];
  const trap = d.title_trap || {};
  // 「补技能解决不了」是个劝退型结论，用暖色；其余用主色
  const accent = /解决不了|方向不对/.test(diag.verdict || '') ? 'var(--warn)' : 'var(--primary)';

  box.innerHTML = `
    <div class="panel" style="border-left:3px solid ${accent}">
      <div class="panel-head">
        <h2>搜索策略</h2>
        <span class="badge warn">${esc(diag.verdict || '')}</span>
      </div>
      <p class="hint" style="margin-top:0">${esc(diag.reason || '')}</p>

      ${terms.length ? `
      <div style="margin-top:14px">
        <div class="sec-title">建议搜索词（点击复制）</div>
        <div class="row wrap" style="gap:8px;margin-top:6px">
          ${terms
            .map(
              (t) =>
                `<button class="small" data-copy="${esc(t.term)}" title="${esc(t.why)}">${esc(t.term)} ⧉</button>`,
            )
            .join('')}
        </div>
      </div>` : ''}

      ${trap.count ? `
      <div style="margin-top:14px">
        <div class="sec-title">要避开的标题词</div>
        <p class="hint" style="margin:2px 0 0">
          ${esc((trap.words || []).join(' · '))} —— ${trap.count} 个否决岗位的标题里带这些词
        </p>
      </div>` : ''}

      ${fill.length ? `
      <div style="margin-top:14px">
        <div class="sec-title">会但没写（改简历就能改善）</div>
        <ul class="reason-list">${fill
          .map(
            (x) =>
              `<li>${esc(x.skill)}<span class="item-meta"> — 简历里有 ${esc(
                (x.related || []).map((r) => r.name).join('、'),
              )}</span></li>`,
          )
          .join('')}</ul>
      </div>` : ''}

      ${miss.length ? `
      <div style="margin-top:14px">
        <div class="sec-title">够不着的（补，或避开这类岗位）</div>
        <ul class="reason-list">${miss.map((x) => `<li>${esc(x.skill)}</li>`).join('')}</ul>
      </div>` : ''}
    </div>`;

  box.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        toast(`已复制「${btn.dataset.copy}」，去招聘网站搜一下`);
      } catch {
        toast('复制失败，请手动选中', true);
      }
    });
  });
}

document.getElementById('btn-match').addEventListener('click', () => runMatch(false));

// 换简历时同步刷新策略：不同简历的否决模式完全不同，留着上一份的建议会误导
const matchResumeSel = document.getElementById('match-resume');
if (matchResumeSel) {
  matchResumeSel.addEventListener('change', () => loadAdvice(matchResumeSel.value));
}

/* ---------- 建议 ---------- */

const CATEGORY_LABEL = {
  resume_defect: '简历硬伤',
  jd_gap: '岗位要求缺证据',
  nice_gap: '加分项未体现',
};
const SEVERITY_LABEL = { high: '高', medium: '中', low: '低' };

/** 相对时间，用于标注"这份结果是什么时候算的" */
function formatWhen(iso) {
  if (!iso) return '上次';
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return '上次';
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return new Date(t).toLocaleDateString('zh-CN');
}

function renderReview(data, resumeId, jobId, fromCache) {
  const box = document.getElementById('review-result');
  if (!data.items || !data.items.length) {
    box.innerHTML = '<div class="panel"><div class="empty">没有产出建议。</div></div>';
    return;
  }

  // 只有定向模式才有岗位可改，也才有必要生成文件
  const canTailor = data.mode === 'targeted' && jobId && resumeId;
  // 刚算出来的显示轮次与耗时；从库里读回来的只能显示生成时间
  const meta = data.elapsed_s != null
    ? `${data.rounds} 轮 · 耗时 ${data.elapsed_s}s`
    : `${formatWhen(data.created_at)}生成`;

  box.innerHTML = `
    <div class="result-head">
      <div class="result-title">
        ${data.total} 条建议
        <span class="count">${meta}${data.flagged ? ` · ${data.flagged} 条未通过事实校验` : ''}</span>
      </div>
      <p class="hint">${data.mode === 'targeted'
        ? '定向改写：针对所选岗位。'
        : '普适体检：不针对具体岗位。'}标黄的条目其改写示例未通过事实校验，已如实保留原样。</p>
      ${fromCache ? '<p class="hint">这是上次生成的结果，没有重新调用模型。要更新点「重新生成」。</p>' : ''}
      ${canTailor ? `
        <div class="row wrap" style="margin-top:10px">
          <button class="primary" id="btn-review-tailor">按建议生成 Word 简历</button>
          <span class="hint" style="margin:0 0 0 10px">只自动应用「有原文依据」的措辞改动；时间线矛盾、项目名不一致这类必须你自己确认事实，程序不会替你改。</span>
        </div>` : ''}
    </div>
    ${data.items.map((s) => `
        <div class="suggestion ${s.fact_status === 'passed' ? 'passed' : 'flagged'}">
          <div class="sg-head">
            <span class="badge info">${CATEGORY_LABEL[s.category] || s.category}</span>
            <span class="badge ${s.severity === 'high' ? 'bad' : s.severity === 'medium' ? 'warn' : ''}">${SEVERITY_LABEL[s.severity] || s.severity}</span>
            <span class="item-meta">${esc(s.anchor || '')}</span>
            ${s.fact_status !== 'passed' ? '<span class="badge warn">未通过事实校验</span>' : ''}
          </div>
          <p class="sg-problem">${esc(s.problem)}</p>
          ${s.direction ? `<div class="sg-block"><span class="label">改写方向</span>${esc(s.direction)}</div>` : ''}
          ${s.example ? `<div class="sg-block"><span class="label">改写示例</span>${esc(s.example)}</div>` : ''}
          ${s.jd_requirement ? `<div class="sg-block"><span class="label">对应岗位要求</span>${esc(s.jd_requirement)}</div>` : ''}
          ${s.fact_issue ? `<div class="sg-block issue"><span class="label">未通过原因</span>${esc(s.fact_issue)}</div>` : ''}
        </div>`).join('')}
    <div id="review-tailor-box"></div>`;

  if (canTailor) {
    const btn = box.querySelector('#btn-review-tailor');
    const target = box.querySelector('#review-tailor-box');
    // 把上面这些诊断原样交给改写环节：不然用户看完 7 条问题点生成，
    // 实际改的却是另一套逻辑，两边对不上
    const hints = data.items.map((s) => {
      const where = s.anchor ? `【${s.anchor}】` : '';
      return `${where}${s.problem}${s.direction ? ' → ' + s.direction : ''}`;
    });
    btn.addEventListener('click', () => runReviewTailor(btn, target, resumeId, jobId, hints));
  }
}

/** 在建议面板里直接把改动落到文件上，免去"回匹配列表再找那个按钮" */
async function runReviewTailor(btn, box, resumeId, jobId, hints) {
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '生成中…';
  box.innerHTML = '<div class="tailor-loading">正在把改动写进 Word，约 20–40 秒…</div>';
  try {
    const data = await api(`/api/resumes/${resumeId}/tailor/${jobId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hints: hints || [] }),
    });
    renderTailor(box, { ...data, hintTotal: (hints || []).length });
    if (data.accepted > 0) toast(`已生成定制版：${data.accepted} 处改动`);
    else toast(data.note || '未做改动，已按原文输出一份 Word');
  } catch (e) {
    box.innerHTML = `<div class="tailor-empty" style="color:var(--danger)">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/**
 * 打开页面或换简历/岗位时，先看库里有没有上次的结果。
 * 一次生成 20–40 秒、约 ¥0.03，用户切来切去不该每次都重算。
 */
async function loadLatestReview() {
  const resumeId = document.getElementById('review-resume').value;
  const jobId = document.getElementById('review-job').value;
  const box = document.getElementById('review-result');
  const btn = document.getElementById('btn-review');

  if (!resumeId) {
    box.innerHTML = '';
    setStatus('review-status', '', '');
    btn.textContent = '生成建议';
    return;
  }

  try {
    const qs = jobId ? `?job_id=${jobId}` : '';
    const data = await api(`/api/resumes/${resumeId}/review/latest${qs}`);
    if (!data.exists) {
      box.innerHTML = '';
      setStatus('review-status', '这份简历还没生成过建议', '');
      btn.textContent = '生成建议';
      return;
    }
    if (data.stale) {
      // 简历重新解析过，旧建议指向的段落可能已经不存在了
      box.innerHTML = '';
      setStatus('review-status', '简历重新解析过，上次的建议已失效，需要重新生成', '');
      btn.textContent = '重新生成';
      return;
    }
    setStatus('review-status', `显示${formatWhen(data.created_at)}的结果，共 ${data.total} 条`, 'done');
    btn.textContent = '重新生成';
    renderReview(data, resumeId, jobId, true);
  } catch {
    // 读历史失败不该挡住主流程，静默即可
  }
}

document.getElementById('review-resume').addEventListener('change', loadLatestReview);
document.getElementById('review-job').addEventListener('change', loadLatestReview);

document.getElementById('btn-review').addEventListener('click', async (ev) => {
  const resumeId = document.getElementById('review-resume').value;
  if (!resumeId) { toast('先选择一份已解析的简历', true); return; }

  await withLoading(ev.currentTarget, '生成中…', async () => {
    setStatus('review-status', '生成建议并逐条核查事实，约 20–40 秒…', 'busy');
    try {
      const jobId = document.getElementById('review-job').value;
      const url = `/api/resumes/${resumeId}/review` + (jobId ? `?job_id=${jobId}` : '');
      const data = await api(url, { method: 'POST' });
      setStatus('review-status', `完成：${data.total} 条，耗时 ${data.elapsed_s}s`, 'done');
      ev.currentTarget.textContent = '重新生成';
      // 把 resumeId / jobId 传下去：定向模式下要能就地生成 Word，
      // 否则用户只能回匹配列表再找「按此岗位定制简历」那个按钮
      renderReview(data, resumeId, jobId, false);
    } catch (e) {
      setStatus('review-status', e.message, 'error');
    }
  });
});

/* ---------- 工作台：一个岗位的所有动作都在这一页 ---------- */

// 曾经这里是六个投递状态（候选/已投/沟通中/面试/Offer/不合适）。
// 实测一次都没被手动点过 —— 招聘平台自带投递记录，手动维护状态是纯负担。
// 现在只保留"已投 / 未投"，其余信息（有没有回应）由聊天记录自动带出来。

function fillWorkbenchSelects() {
  const jobSel = document.getElementById('wb-job');
  const resumeSel = document.getElementById('wb-resume');
  // 旧版 HTML（浏览器缓存）里没有工作台，这里必须提前退出。
  // 它在启动流程的 Promise.then 里被调用，抛错会变成未捕获的 Promise 异常，
  // 后面所有初始化（列表渲染）全部不执行 —— 用户看到的就是"数据全没有"
  if (!jobSel || !resumeSel) return;

  const prevJob = jobSel.value;
  jobSel.innerHTML = '<option value="">选择岗位</option>' +
    state.jobs.map((j) => `<option value="${j.id}">#${j.id} ${esc(j.title || '未命名')}</option>`).join('');
  if (prevJob) jobSel.value = prevJob;

  // 简历下拉：主简历排最前（后端已排序），没有显式设置时默认选第一份
  const prevResume = resumeSel.value;
  const usable = state.resumes.filter((r) => r.has_profile);
  resumeSel.innerHTML = usable.length
    ? usable.map((r) => `<option value="${r.id}">#${r.id} ${esc(r.file_name)}${r.is_default ? '（主）' : ''}</option>`).join('')
    : '<option value="0">（没有解析好的简历）</option>';
  if (prevResume && usable.some((r) => String(r.id) === prevResume)) resumeSel.value = prevResume;
}

async function loadWorkbench(jobId) {
  const body = document.getElementById('wb-body');
  const statusEl = document.getElementById('wb-status');
  const resumeSel = document.getElementById('wb-resume');
  const resumeId = Number(resumeSel.value) || null;

  statusEl.textContent = '加载中…';
  body.innerHTML = '<div class="panel"><div class="empty">加载中…</div></div>';
  try {
    const [job, matches, appRes, chatlogs, similar] = await Promise.all([
      api(`/api/jobs/${jobId}`),
      resumeId
        ? api(`/api/resumes/${resumeId}/matches?include_rejected=true&job_id=${jobId}`)
        : Promise.resolve(null),
      api(`/api/applications/by-job/${jobId}`),
      // 聊天记录按岗位关联：扩展同步时按标题匹配到了才有
      api(`/api/chat-logs?job_id=${jobId}`).catch(() => ({ items: [] })),
      // 同类岗位：标题向量找相似，带上它们各自的投递结果。
      // 这是全系统唯一能拿出的"同类经验"证据 —— 比一个算出来的分数更能改变决定
      api(`/api/workbench/similar/${jobId}?limit=3`).catch(() => null),
    ]);

    // 下一步建议：规则算的，秒回。传当前状态过去而不是让后端再查一遍，
    // 避免为了一个提示再跑一次匹配
    const matchItem = (matches && matches.items && matches.items[0]) || null;
    const chatItems = (chatlogs && chatlogs.items) || [];
    let nextStep = null;
    try {
      nextStep = await api('/api/workbench/next-step', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          job_id: jobId,
          match_decision: matchItem ? matchItem.decision : 'none',
          missed_skills: (matchItem && matchItem.missed_skills) || [],
          app_status: appRes.exists ? appRes.status : 'none',
          last_role: chatItems.length ? chatItems[chatItems.length - 1].role : null,
          chat_count: chatItems.length,
        }),
      });
    } catch { /* 建议算不出来不影响工作台其他部分 */ }

    statusEl.textContent = '';
    renderWorkbench(jobId, job, matches, appRes, chatlogs, nextStep, similar);
  } catch (e) {
    statusEl.textContent = e.message;
    body.innerHTML = '';
  }
}

function renderWorkbench(jobId, job, matches, appRes, chatlogs, nextStep, similar) {
  const body = document.getElementById('wb-body');
  const profile = (job && job.profile) || {};
  const basic = profile.basic || {};
  const meta = [
    profile.company || '',
    profile.city || '',
    job && job.salary,
    basic.edu_level ? `${basic.edu_level}${basic.edu_fulltime_required === false ? '' : ''}` : '',
  ].filter(Boolean).join(' · ');

  // 匹配结论：选了简历才有；没匹配结果是"还没算过"而不是"不匹配"
  const match = matches && matches.items.length ? matches.items[0] : null;
  const matchHtml = !matches
    ? '<div class="empty">先在上面选一份简历，才能看这份岗位跟你的匹配结论。</div>'
    : !match
      ? '<div class="empty">没有匹配结论 —— 该岗位可能缺少画像或被过滤。可到「匹配」页点开始。</div>'
      : `
        <div class="match-head">
          <span class="badge ${match.decision === 'pass' ? 'ok' : match.decision === 'reject' ? 'bad' : 'warn'}">${DECISION_LABEL[match.decision] || match.decision}</span>
          ${match.score != null ? `<span class="score">${match.score} 分</span>` : ''}
          ${matches.cached ? '<span class="item-meta">（缓存结论）</span>' : ''}
        </div>
        <ul class="reason-list">${(match.reasons || []).slice(0, 6).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
        ${(match.missed_skills || []).length
          ? `<div class="item-meta" style="margin-top:6px">未覆盖的必备技能：<span class="missed">${esc(match.missed_skills.join('、'))}</span></div>`
          : ''}`;

  // 投递状态只留「未投 / 已投」两个。
  // 六态（沟通中/面试/offer/不合适）实测一次都没被手动点过 ——
  // 手动维护状态是纯负担，而招聘平台自带投递记录。
  // 系统真正需要知道的只有"投了没"，其余信息由聊天记录自动带出来。
  const applied = appRes.exists && appRes.status !== 'candidate';
  const appliedLabel = applied
    ? `已投 · ${formatWhen(appRes.updated_at) || formatWhen(appRes.created_at)}`
    : '';

  // 缺口：这是"事实"，不是推测。有就列出来，没有就说全cover了
  const missed = (match && match.missed_skills) || [];
  const gapHtml = !match
    ? '<span class="item-meta">还没有匹配结论，无法判断缺口</span>'
    : missed.length
      ? `缺 <span class="missed">${esc(missed.join('、'))}</span>`
      : '<span class="ok-text">必备技能已全部覆盖</span>';

  // 同类岗位：全系统唯一能拿出的"同类经验"证据
  const simItems = (similar && similar.items) || [];
  const simHtml = !similar
    ? '<div class="item-meta">相似岗位暂时算不出来（语义层未就绪）</div>'
    : similar.reason
      ? `<div class="item-meta">${esc(similar.reason)}</div>`
      : !simItems.length
        ? '<div class="item-meta">库里还没有方向相近的岗位可比</div>'
        : `<div class="item-meta">找到 ${similar.total} 个方向相近的岗位，你投了 ${similar.applied} 个${
            similar.replied ? `，其中 <b>${similar.replied} 个有回应</b>` : '，都还没有回应'
          }</div>
        <ul class="reason-list">${simItems
          .map(
            (s) =>
              // 列出判断依据而不是只给一个百分比：百分比对用户没有意义，
              // 而"共同技能 3 项"是他能自己去核对的
              `<li>${esc(s.title)}（${s.overlap > 0 ? `共同技能 ${s.overlap} 项` : '标题高度一致'}）${
                s.replied ? ' · <b>有回应</b>' : s.status && s.status !== 'candidate' ? ' · 已投' : ' · 未投'
              }</li>`,
          )
          .join('')}</ul>`;

  body.innerHTML = `
    <div class="panel">
      <div class="item-row">
        <div class="item-main">
          <div class="item-title"><span class="match-title">${esc((job.profile && job.profile.title) || '未命名岗位')}</span></div>
          <div class="item-meta">${esc(meta)}</div>
          ${job.url ? `<a class="item-meta" href="${esc(job.url)}" target="_blank" rel="noreferrer">原站链接 ↗</a>` : ''}
        </div>
      </div>
    </div>
    ${nextStep ? `
    <div class="panel" style="border-left:3px solid ${
      nextStep.urgency === 'high' ? 'var(--primary)'
      : nextStep.urgency === 'warn' ? 'var(--warn)'
      : nextStep.urgency === 'muted' ? 'var(--line-strong)'
      : 'var(--line-strong)'}">
      <div class="sec-title">下一步</div>
      <div class="item-title" style="font-size:15px;margin-bottom:4px">${esc(nextStep.action)}</div>
      <div class="item-meta">${esc(nextStep.reason)}</div>
    </div>` : ''}
    <div class="panel">
      <div class="sec-title">匹配结论（简历 #${document.getElementById('wb-resume').value}）</div>
      ${matchHtml}
    </div>
    <div class="panel">
      <div class="sec-title">投递前判断</div>
      <div class="item-meta" style="margin-bottom:10px">技能缺口：${gapHtml}</div>
      ${simHtml}
      <div class="row" style="margin-top:14px">
        ${applied
          ? `<span class="badge ok">${esc(appliedLabel)}</span>
             <button class="small" data-wb-applied="0">撤销</button>`
          : `<button class="small primary" data-wb-applied="1">标记为已投</button>
             <span class="item-meta">点一下就够了，不用维护状态</span>`}
      </div>
    </div>
    <div class="panel">
      <div class="sec-title">定制简历</div>
      <p class="hint">按这个岗位改写你选的简历并生成 Word。PDF 来源会先转成 Word 保版式，只改文字。</p>
      <button id="btn-wb-tailor" class="primary">生成定制版 Word（约 20–40 秒）</button>
      <div id="wb-tailor-result" style="margin-top:10px"></div>
    </div>
    <div class="panel">
      <div class="sec-title">HR 沟通记录</div>
      ${(chatlogs.items || []).length
        ? `<ul class="reason-list">${(chatlogs.items || []).slice(-20).map((c) => `
            <li><b>${esc(c.role)}</b>：${esc(c.content.length > 120 ? c.content.slice(0, 120) + '…' : c.content)}</li>`).join('')}</ul>
           <p class="hint">由浏览器扩展自动同步，最后一次同步的整段对话都在这里。</p>`
        : '<div class="empty">还没有同步到这个岗位的聊天记录。在招聘网站的沟通页打开「帮我回复」面板，它会自动把最近消息同步过来。</div>'}
    </div>`;

  // 已投 / 撤销：只有一个动作。
  // 首次走 POST，已存在走 PATCH（接口幂等，重复点不会造出多条记录）
  body.querySelectorAll('[data-wb-applied]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const next = btn.dataset.wbApplied === '1';
      await withLoading(btn, '…', async () => {
        try {
          if (appRes.exists) {
            await api(`/api/applications/${appRes.id}`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ status: next ? 'applied' : 'candidate' }),
            });
          } else {
            await api('/api/applications', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ job_id: jobId, status: next ? 'applied' : 'candidate' }),
            });
          }
          toast(next ? '已标记为已投' : '已撤销');
          loadWorkbench(jobId);
          loadWorkbenchStats();
        } catch (e) { toast(e.message, true); }
      });
    });
  });

  const tailorBtn = body.querySelector('#btn-wb-tailor');
  tailorBtn.addEventListener('click', async () => {
    const resumeSel = document.getElementById('wb-resume');
    const rid = Number(resumeSel.value) || 0;
    if (!rid) { toast('先在上面选一份简历', true); return; }
    const resultBox = body.querySelector('#wb-tailor-result');
    await withLoading(tailorBtn, '生成中…', async () => {
      resultBox.innerHTML = '<div class="msg">正在按岗位改写并生成 Word，约 20–40 秒…</div>';
      try {
        const data = await api(`/api/resumes/${rid}/tailor/${jobId}`, { method: 'POST' });
        const originNote = data.origin === 'docx'
          ? '在你的原 Word 上只改文字，版式未动'
          : data.origin === 'pdf_converted'
            ? '原 PDF 已按原版式转成 Word，只改文字'
            : '原 PDF 无法保版式转换，此为重建版';
        resultBox.innerHTML = `
          <div class="item-meta">${esc(originNote)} · ${data.accepted} 处改动 · ¥${(data.cost_cny || 0).toFixed(4)}</div>
          ${data.download_url
            ? `<a class="link-btn" href="${data.download_url}" target="_blank" rel="noreferrer">下载 Word ↓</a>`
            : `<div class="item-meta" style="color:var(--danger)">${esc(data.note || '未生成')}</div>`}
          ${data.accepted === 0 && data.download_url ? '<div class="item-meta">内容与原文一致（没有需要自动改的地方）</div>' : ''}`;
      } catch (e) {
        resultBox.innerHTML = `<div class="item-meta" style="color:var(--danger)">${esc(e.message)}</div>`;
      }
    });
  });
}

async function loadWorkbenchStats() {
  const box = document.getElementById('wb-stats-box');
  if (!box) return;
  try {
    const s = await api('/api/applications/stats');
    // 没有任何投递记录时不占屏
    if (!s.tracked) { box.innerHTML = ''; return; }

    // 只给绝对数，不给"回复率"百分比。
    // 原因很实际：一轮求职通常只投十几家，样本不够时百分比会剧烈波动，
    // 而"投了 3 家、1 家有回应"是事实，"33% 回复率"是把噪音包装成结论。
    const notApplied = Math.max(s.tracked - s.applied, 0);
    box.innerHTML = `
      <div class="panel">
        <div class="sec-title">投递进展</div>
        <div class="summary-bar">
          <div class="summary-item"><div class="n">${s.applied}</div><div class="t">已投</div></div>
          <div class="summary-item pass"><div class="n">${s.replied}</div><div class="t">有回应</div></div>
          <div class="summary-item"><div class="n">${notApplied}</div><div class="t">未投</div></div>
        </div>
        ${s.by_source.length ? `
          <table style="width:100%;font-size:12px;border-collapse:collapse">
            <tr style="color:#6b7078;text-align:left">
              <th style="font-weight:500;padding:4px 8px">平台</th>
              <th style="font-weight:500;padding:4px 8px">已投</th>
              <th style="font-weight:500;padding:4px 8px">有回应</th>
            </tr>
            ${s.by_source.map((r) => `
              <tr style="border-top:1px solid #eef0f3">
                <td style="padding:4px 8px">${esc(r.label)}</td>
                <td style="padding:4px 8px">${r.total}</td>
                <td style="padding:4px 8px">${r.replied}</td>
              </tr>`).join('')}
          </table>
          <p class="hint">「有回应」指该岗位的聊天记录里出现过 HR 的消息，由浏览器扩展同步。</p>`
        : '<p class="hint">同步聊天记录后（在招聘网站沟通页打开「帮我回复」），这里会显示各平台有回应的岗位数。</p>'}
      </div>`;
  } catch {
    box.innerHTML = '';
  }
}

// 可选区块的绑定一律先判空：index.html 与新 app.js 版本不一致时
// （浏览器缓存了旧 HTML），直接 .addEventListener 会抛 TypeError，
// 整段脚本中断，页面变成"样式对但数据全空"——踩过一次，很难看出原因
const wbLoadBtn = document.getElementById('btn-wb-load');
if (wbLoadBtn) {
  wbLoadBtn.addEventListener('click', () => {
    const jobId = Number(document.getElementById('wb-job').value) || 0;
    if (!jobId) { toast('先选择岗位', true); return; }
    loadWorkbench(jobId);
  });
}
const wbResumeSel = document.getElementById('wb-resume');
if (wbResumeSel) {
  wbResumeSel.addEventListener('change', () => {
    const jobId = Number(document.getElementById('wb-job').value) || 0;
    if (jobId) loadWorkbench(jobId);
  });
}

/* ---------- 启动 ---------- */

/* ---------- 对话（ChatAgent，DeepSeek 式排布） ---------- */

const chatState = {
  conversationId: Number(localStorage.getItem('chatConversationId')) || null,
  sending: false,
  lastUserText: '',
};

function chatBox() {
  return document.getElementById('chat-msgs');
}

function scrollToBottom() {
  const el = document.getElementById('chat-scroll');
  if (el) el.scrollTop = el.scrollHeight;
}

function hideEmpty() {
  const el = document.getElementById('chat-empty');
  if (el) el.style.display = 'none';
}

function showEmpty() {
  const el = document.getElementById('chat-empty');
  if (el) el.style.display = '';
}

/** 清掉 Boss 链接里的跟踪参数，展示干净的投递地址 */
function cleanJobUrl(u) {
  try {
    const url = new URL(u);
    for (const key of [...url.searchParams.keys()]) {
      if (/^(pk|ka|lid|sessionid)$/i.test(key)) url.searchParams.delete(key);
    }
    return url.toString();
  } catch {
    return u;
  }
}

/** 转义后的文本里把 URL 变成可点的链接（回复模型偶尔仍会贴链接，兜底） */
function linkify(escaped) {
  return escaped.replace(/https?:\/\/[^\s<）)】，。]+/g, (raw) => {
    const href = cleanJobUrl(raw);
    return `<a href="${href}" target="_blank" rel="noreferrer">投递链接 ↗</a>`;
  });
}

function appendUser(text) {
  hideEmpty();
  const wrap = document.createElement('div');
  wrap.className = 'msg-user';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = esc(text);
  wrap.appendChild(bubble);
  chatBox().appendChild(wrap);
  scrollToBottom();
}

function appendAssistant() {
  const wrap = document.createElement('div');
  wrap.className = 'msg-assistant';
  wrap.innerHTML = `
    <div class="msg-head"><img class="msg-mark" src="./logo96.png" alt=""><span class="msg-role">岗哨</span></div>
    <div class="msg-body"></div>`;
  chatBox().appendChild(wrap);
  scrollToBottom();
  return { root: wrap, body: wrap.querySelector('.msg-body') };
}

/** 执行过程：进行中展开显示最新一步，完成后收成一行「已完成 N 步」 */
function traceHtml(steps, running) {
  if (!steps.length && !running) return '';
  const last = steps.length ? steps[steps.length - 1] : '准备中…';
  const summary = running
    ? `<span class="trace-spin"></span><span>${esc(last)}</span>`
    : `<span>已完成 ${steps.length} 步</span>`;
  return `<details class="chat-trace"${running ? ' open' : ''}>
    <summary>${summary}</summary>
    <ol>${steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ol>
  </details>`;
}

/** 岗位以紧凑卡片嵌在回复里，可点链接投递 */
function renderChatJobs(jobs) {
  if (!jobs || !jobs.length) return '';
  const label = { pass: '通过', confirm: '待确认', reject: '否决' };
  const cls = { pass: 'ok', confirm: 'warn', reject: 'bad' };
  return `<div class="chat-jobs">${jobs.map((j) => `
    <div class="chat-job">
      <div class="chat-job-head">
        <span class="job-name-text">${esc(j.title || '未命名岗位')}</span>
        <span class="badge ${cls[j.decision] || ''}">${label[j.decision] || j.decision}</span>
        ${j.score != null ? `<span class="score">${j.score} 分</span>` : ''}
        ${j.url ? `<a class="apply-link" href="${cleanJobUrl(j.url)}" target="_blank" rel="noreferrer">去投递 ↗</a>` : ''}
      </div>
      <div class="item-meta">
        ${esc([j.company, j.city].filter(Boolean).join(' · '))}
        ${j.semantic_sim != null ? ` · 语义相关 ${j.semantic_sim}` : ''}
      </div>
      ${(j.missed_skills || []).length
        ? `<div class="item-meta missed">缺：${esc(j.missed_skills.join('、'))}</div>` : ''}
    </div>`).join('')}</div>`;
}

function addMsgActions(ctx, reply) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions';
  const copy = document.createElement('button');
  copy.textContent = '复制';
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(reply || '');
      copy.textContent = '已复制';
      setTimeout(() => { copy.textContent = '复制'; }, 1500);
    } catch {
      copy.textContent = '复制失败';
    }
  });
  const retry = document.createElement('button');
  retry.textContent = '重新生成';
  retry.addEventListener('click', () => {
    if (!chatState.lastUserText || chatState.sending) return;
    sendChat(chatState.lastUserText);
  });
  bar.appendChild(copy);
  bar.appendChild(retry);
  ctx.body.appendChild(bar);
}


/** 渲染一条历史消息（刷新页面后恢复对话，不然等于没保存） */
function renderStoredMessage(msg) {
  if (msg.role === 'user') {
    appendUser(msg.content || '');
    return;
  }
  const ctx = appendAssistant();
  const meta = msg.meta || {};
  ctx.body.innerHTML =
    `<div class="msg-text">${linkify(esc(msg.content || '')).replace(/\n/g, '<br>')}</div>` +
    renderChatJobs(meta.jobs);
  addMsgActions(ctx, msg.content || '');
}

async function loadConversation(id) {
  try {
    const conv = await api(`/api/chat/${id}`);
    chatBox().innerHTML = '';
    hideEmpty();
    for (const msg of conv.messages || []) renderStoredMessage(msg);
    chatState.conversationId = conv.id;
    document.getElementById('chat-conv-id').textContent =
      `会话 #${conv.id}` + (conv.messages ? ` · ${conv.messages.length} 条` : '');
    if (!(conv.messages || []).length) showEmpty();
  } catch (e) {
    // 会话不存在（数据库被清过）时退回空状态，不要把用户卡在报错里
    chatState.conversationId = null;
    localStorage.removeItem('chatConversationId');
    document.getElementById('chat-conv-id').textContent = '新对话';
    showEmpty();
  }
}

async function toggleHistoryMenu() {
  const menu = document.getElementById('chat-history-menu');
  if (menu.classList.contains('open')) {
    menu.classList.remove('open');
    return;
  }
  menu.innerHTML = '<div class="hist-empty">读取中…</div>';
  menu.classList.add('open');
  try {
    const data = await api('/api/chat');
    const items = (data.items || []).slice(0, 15);
    if (!items.length) {
      menu.innerHTML = '<div class="hist-empty">还没有历史会话</div>';
      return;
    }
    menu.innerHTML = items.map((c) => `
      <button class="hist-item" data-conv="${c.id}">
        <span class="hist-title">${esc(c.title || '未命名会话')}</span>
        <span class="hist-meta">#${c.id} · ${c.messages} 条</span>
      </button>`).join('');
    menu.querySelectorAll('[data-conv]').forEach((btn) => {
      btn.addEventListener('click', () => {
        menu.classList.remove('open');
        loadConversation(Number(btn.dataset.conv));
      });
    });
  } catch (e) {
    menu.innerHTML = `<div class="hist-empty">${esc(e.message)}</div>`;
  }
}

async function sendChat(text) {
  if (chatState.sending) return;
  chatState.sending = true;
  chatState.lastUserText = text;
  document.getElementById('btn-chat-send').disabled = true;

  appendUser(text);
  const ctx = appendAssistant();

  const steps = [];
  const paint = (running) => {
    ctx.body.innerHTML = traceHtml(steps, running);
    scrollToBottom();
  };
  paint(true);

  try {
    const resp = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, conversation_id: chatState.conversationId }),
    });
    if (!resp.ok || !resp.body) throw new Error(`服务异常（${resp.status}）`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let finished = false;
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!frame.startsWith('data:')) continue;
        const evt = JSON.parse(frame.slice(5).trim());
        if (evt.type === 'step') {
          steps.push(evt.label);
          paint(true);
        } else if (evt.type === 'done') {
          finished = true;
          chatState.conversationId = evt.conversation_id;
          localStorage.setItem('chatConversationId', String(evt.conversation_id));
          document.getElementById('chat-conv-id').textContent = `会话 #${evt.conversation_id}`;
          const finalSteps = evt.steps || steps;
          ctx.body.innerHTML =
            `${traceHtml(finalSteps, false)}<div class="msg-text">${linkify(esc(evt.reply || '')).replace(/\n/g, '<br>')}</div>` +
            renderChatJobs(evt.jobs);
          addMsgActions(ctx, evt.reply || '');
          scrollToBottom();
        } else if (evt.type === 'error') {
          throw new Error(evt.message || '处理失败');
        }
      }
    }
  } catch (e) {
    // 流式不可用时退回一次性接口，保证功能不被传输层绑死
    try {
      const data = await api('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, conversation_id: chatState.conversationId }),
      });
      chatState.conversationId = data.conversation_id;
      localStorage.setItem('chatConversationId', String(data.conversation_id));
      document.getElementById('chat-conv-id').textContent = `会话 #${data.conversation_id}`;
      const finalSteps = data.steps || steps;
      ctx.body.innerHTML =
        `${traceHtml(finalSteps, false)}<div class="msg-text">${linkify(esc(data.reply || '')).replace(/\n/g, '<br>')}</div>` +
        renderChatJobs(data.jobs);
      addMsgActions(ctx, data.reply || '');
    } catch (e2) {
      ctx.body.innerHTML = `<div class="msg-text" style="color:var(--danger)">${esc(e2.message || e.message)}</div>`;
    }
  } finally {
    chatState.sending = false;
    document.getElementById('btn-chat-send').disabled = false;
    scrollToBottom();
  }
}

function autosizeInput(el) {
  el.style.height = 'auto';
  el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
}

document.getElementById('btn-chat-send').addEventListener('click', () => {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  autosizeInput(input);
  sendChat(text);
});

document.getElementById('chat-input').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    document.getElementById('btn-chat-send').click();
  }
});
document.getElementById('chat-input').addEventListener('input', (ev) => {
  autosizeInput(ev.currentTarget);
});

document.getElementById('btn-chat-new').addEventListener('click', () => {
  chatState.conversationId = null;
  chatState.lastUserText = '';
  localStorage.removeItem('chatConversationId');
  document.getElementById('chat-conv-id').textContent = '新对话';
  chatBox().innerHTML = '';
  document.getElementById('chat-history-menu').classList.remove('open');
  showEmpty();
  document.getElementById('chat-input').focus();
});

document.getElementById('btn-chat-history').addEventListener('click', (ev) => {
  ev.stopPropagation();
  toggleHistoryMenu();
});
document.addEventListener('click', (ev) => {
  const menu = document.getElementById('chat-history-menu');
  if (menu && !menu.contains(ev.target)) menu.classList.remove('open');
});

// 页面加载时恢复上一次的会话：会话是存库的，不该刷新就消失
if (chatState.conversationId) {
  loadConversation(chatState.conversationId);
}

document.getElementById('chat-samples').addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-sample]');
  if (btn) sendChat(btn.dataset.sample);
});

/* ---------- 发现（岗位雷达） ---------- */

// 公司分档。T0/T1 是"值得优先投入时间"的两档，T2 不是不能去，是需要单独核实，
// X 是外包/人力供应商 —— 去了简历增值低，所以单独用红色标出来
const TIER_META = {
  T0: { cls: 'tier-t0', label: 'T0 首选' },
  T1: { cls: 'tier-t1', label: 'T1 值得投' },
  T2: { cls: 'tier-t2', label: 'T2 保底' },
  X: { cls: 'tier-x', label: 'X 慎投' },
};

const GRADE_META = {
  reachable: { cls: 'ok', label: '可达' },
  stretch: { cls: 'warn', label: '勉强' },
  unknown: { cls: 'info', label: '待确认' },
  blocked: { cls: 'bad', label: '已排除' },
};

const radarState = { data: null, showBlocked: false };

async function loadRadar() {
  const box = document.getElementById('radar-list');
  if (!box) return;
  try {
    const q = radarState.showBlocked ? '?include_blocked=true&limit=200' : '?limit=80';
    const data = await api('/api/radar' + q);
    radarState.data = data;
    fillRadarPref(data.pref);
    renderRadarVerdict(data);
    renderRadarList(data);
  } catch (err) {
    box.innerHTML = `<div class="empty">雷达加载失败：${esc(err.message || err)}</div>`;
  }
}

function fillRadarPref(pref) {
  if (!pref) return;
  document.getElementById('radar-stage').value = pref.stage || 'internship';
  document.getElementById('radar-cities').value = (pref.cities || []).join(',');
  document.getElementById('radar-year').value = pref.graduation_year || '';
  document.getElementById('radar-edu').value = pref.edu_level || '本科';
  document.getElementById('radar-outsourcing').checked = !!pref.accept_outsourcing;
}

function renderRadarVerdict(data) {
  const box = document.getElementById('radar-verdict');
  if (!box) return;
  const v = data.verdict || { level: 'ok', text: '' };
  const counts = data.grade_counts || {};
  const chip = (n, label, cls) =>
    `<span class="stat-chip ${cls}"><b>${n}</b>${label}</span>`;
  const gaps = data.data_gaps || {};

  box.innerHTML = `
    <div class="panel verdict-panel ${v.level}">
      <div class="verdict-text">${esc(v.text)}</div>
      <div class="verdict-stats">
        ${chip(counts.reachable || 0, '个可达', 'ok')}
        ${chip(counts.stretch || 0, '个勉强', 'warn')}
        ${chip(counts.unknown || 0, '个待确认', 'info')}
        ${chip(counts.blocked || 0, '个已排除', 'bad')}
      </div>
      ${gaps.no_company
        ? `<div class="verdict-gap">其中 ${gaps.no_company} 个岗位没抓到公司名，无法评价公司质量；点开原站详情页重新抓一次就能补上。</div>`
        : ''}
    </div>`;
  document.getElementById('radar-count').textContent =
    `${data.total_jobs} 个岗位里留下 ${data.returned} 个`;
}

function renderRadarList(data) {
  const box = document.getElementById('radar-list');
  const items = data.items || [];
  if (!items.length) {
    box.innerHTML = '<div class="empty">没有一个岗位通过筛选。先看上面的「该搜什么」，换关键词补一批数据。</div>';
    return;
  }

  box.innerHTML = items.map((it) => {
    const g = GRADE_META[it.gate.grade] || GRADE_META.unknown;
    const tier = TIER_META[it.company_score.tier] || TIER_META.T2;
    const cp = it.company_profile || {};
    const companyLine = [
      it.company || '公司未知',
      cp.parent ? `${cp.parent}旗下` : '',
      cp.industry || '',
      cp.nature || '',
    ].filter(Boolean).join(' · ');

    const exp = it.exp_min != null
      ? `${it.exp_min}${it.exp_max != null ? '–' + it.exp_max : '+'} 年`
      : '';
    const years = (it.graduate_years || []).length
      ? `${it.graduate_years.join('/')} 届` : '';

    const why = [
      ...(it.gate.positives || []).map((t) => `<span class="why ok">✓ ${esc(t)}</span>`),
      ...(it.gate.cautions || []).map((t) => `<span class="why warn">! ${esc(t)}</span>`),
      ...(it.gate.blockers || []).map((t) => `<span class="why bad">× ${esc(t)}</span>`),
    ];

    // 公司加减分理由只取前两条：全列出来会把卡片撑爆，而用户真正需要的
    // 是"为什么这家公司在这个档位"，两条足够看出主要依据
    const companyWhy = (it.company_score.reasons || []).slice(0, 2)
      .map((t) => `<span class="why plain">${esc(t)}</span>`).join('');

    const skills = (it.must_have_skills || []).slice(0, 5)
      .map((s) => `<span class="chip chip-clip" title="${esc(s)}">${esc(s)}</span>`).join('');

    const m = it.match || {};
    const matchLine = m.must
      ? `<span class="match-line">必备技能命中 ${m.hit}/${m.must}${m.reliable ? '' : '（岗位信息不全，仅供参考）'}</span>`
      : '';

    return `
      <div class="item radar-item ${it.gate.grade}">
        <div class="item-row">
          <div class="radar-num">
            <b>${Math.round(it.radar_score)}</b>
            <span>雷达分</span>
          </div>
          <div class="item-main">
            <div class="item-title">
              <span>${esc(it.title || '（未命名）')}</span>
              <span class="badge ${g.cls}">${g.label}</span>
              <span class="badge ${tier.cls}">${tier.label} ${Math.round(it.company_score.score)}</span>
              ${it.job_nature ? `<span class="badge">${esc(it.job_nature)}</span>` : ''}
              ${it.city ? `<span class="badge">${esc(it.city)}</span>` : ''}
              ${it.edu_level ? `<span class="badge">${esc(it.edu_level)}</span>` : ''}
              ${years ? `<span class="badge info">${esc(years)}</span>` : ''}
              ${it.salary_raw ? `<span class="badge">${esc(it.salary_raw)}</span>` : ''}
            </div>
            <div class="item-meta">
              ${esc(companyLine)}
              ${cp.note ? ` · <span class="co-note">${esc(cp.note)}</span>` : ''}
            </div>
            <div class="radar-why">
              ${why.join('')}
              ${matchLine ? `<span class="why plain">${matchLine}</span>` : ''}
              ${companyWhy}
            </div>
            ${skills ? `<div class="chips" style="margin-top:8px">${skills}</div>` : ''}
            <div class="item-meta" style="margin-top:6px">
              #${it.job_id}${it.url ? ` · <a class="jd-link" href="${esc(it.url)}" target="_blank" rel="noreferrer">原站链接 ↗</a>` : ''}
              ${it.exp_min != null ? ` · 经验 ${esc(exp)}` : ''}
            </div>
          </div>
          <div class="item-actions">
            <button class="small" data-workbench="${it.job_id}">工作台</button>
          </div>
        </div>
      </div>`;
  }).join('');
}

async function loadRadarPlan() {
  const box = document.getElementById('radar-plan-body');
  if (!box) return;
  try {
    const p = await api('/api/radar/plan');
    renderRadarPlan(box, p);
  } catch (err) {
    box.innerHTML = `<div class="empty">搜索计划加载失败：${esc(err.message || err)}</div>`;
  }
}

function renderRadarPlan(box, p) {
  document.getElementById('radar-plan-stage').textContent = {
    internship: '实习优先',
    campus: '校招优先',
    both: '实习 + 校招',
  }[p.stage] || p.stage;

  const kws = (p.keywords || []).map((k) => `
    <button class="plan-chip" data-copy="${esc(k.q)}" title="${esc(k.why)}">
      ${esc(k.q)}
    </button>`).join('');

  const cos = (p.company_targets || []).map((c) => {
    const tier = TIER_META[c.tier] || TIER_META.T2;
    return `
      <div class="plan-co">
        <button class="co-name" data-copy="${esc(c.company)}">${esc(c.company)}</button>
        <span class="badge ${tier.cls}">${tier.label}</span>
        <span class="plan-co-meta">${esc(c.city || '')}${c.in_library ? ` · 库里 ${c.in_library} 条` : ' · 库里没有'}</span>
        <div class="plan-co-why">${esc(c.why)}</div>
      </div>`;
  }).join('');

  const f = p.filters || {};
  box.innerHTML = `
    <p class="hint" style="margin-top:0">点关键词或公司名复制，粘到招聘网站的搜索框。给的是词，不是链接 —— 平台的搜索参数一改版，拼好的链接会静默抓空。</p>
    <p class="hint">搜出结果后，把那个结果页的网址粘到扩展弹窗的「批量采集」里，它会翻几页把卡片抓进来（只抓摘要）。抓完回到这里点「重新扫描」，候选池会重排。</p>
    <div class="plan-section">
      <div class="plan-label">关键词</div>
      <div class="plan-chips">${kws || '<span class="hint">没有可用关键词</span>'}</div>
    </div>
    <div class="plan-section">
      <div class="plan-label">筛选条件</div>
      <div class="chips">
        <span class="chip">学历 ${esc(f.degree || '')}</span>
        <span class="chip">经验 ${esc(f.experience || '')}</span>
        ${f.graduate_year ? `<span class="chip">${f.graduate_year} 届</span>` : ''}
        ${(f.avoid || []).map((a) => `<span class="chip chip-avoid">避开：${esc(a)}</span>`).join('')}
      </div>
    </div>
    <div class="plan-section">
      <div class="plan-label">按公司直搜（命中率最高）</div>
      <div class="plan-companies">${cos || '<span class="hint">还没有可推荐的公司</span>'}</div>
    </div>
    <div class="plan-section">
      <div class="plan-label">操作要点</div>
      <ul class="plan-tips">${(p.tips || []).map((t) => `<li>${esc(t)}</li>`).join('')}</ul>
    </div>`;
}

async function saveRadarPref() {
  const btn = document.getElementById('btn-radar-save');
  const cities = document.getElementById('radar-cities').value
    .split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
  const year = Number(document.getElementById('radar-year').value) || null;
  const payload = {
    stage: document.getElementById('radar-stage').value,
    cities,
    graduation_year: year,
    edu_level: document.getElementById('radar-edu').value,
    accept_outsourcing: document.getElementById('radar-outsourcing').checked,
  };
  await withLoading(btn, '保存中…', async () => {
    await api('/api/radar/pref', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    setStatus('radar-status', '口径已更新', 'done');
    await loadRadar();
    await loadRadarPlan();
  });
}

document.addEventListener('click', (ev) => {
  const copyBtn = ev.target.closest('[data-copy]');
  if (!copyBtn) return;
  const text = copyBtn.dataset.copy;
  navigator.clipboard?.writeText(text).then(
    () => toast(`已复制「${text}」，粘到招聘网站搜索框`),
    () => toast('复制失败，手动选中复制即可', true),
  );
});

const radarShowBlocked = document.getElementById('radar-show-blocked');
if (radarShowBlocked) {
  radarShowBlocked.addEventListener('change', (ev) => {
    radarState.showBlocked = ev.currentTarget.checked;
    loadRadar();
  });
}
document.getElementById('btn-radar-save')?.addEventListener('click', saveRadarPref);
document.getElementById('btn-radar-refresh')?.addEventListener('click', () => {
  setStatus('radar-status', '');
  loadRadar();
});

async function checkHealth() {
  const node = document.getElementById('health');
  try {
    const data = await api('/health');
    if (!data.db) {
      node.textContent = '数据库不可用';
      node.className = 'health bad';
      node.title = '本地 Postgres 没起来，重启 start.bat 试试';
    } else if (!data.llm_configured) {
      node.textContent = '缺模型密钥';
      node.className = 'health bad';
      node.title = '在 .env 里填 DEEPSEEK_API_KEY 才能抽取和改写';
    } else {
      node.textContent = '本地就绪';
      node.className = 'health ok';
      node.title = '本地服务已连接 · 数据不出本机';
    }
  } catch {
    node.textContent = '服务未连接';
    node.className = 'health bad';
    node.title = '本地服务没响应，双击 start.bat 启动';
  }
}

// 构建标记：左下角只显示简短版本号，完整的构建标记放进 title。
// 之前把「2026-10-26a · 一键发现 + 消息监控」整行铺在侧栏底部，
// 看起来像调试输出漏到了界面上；但它确实有用 ——
// 改完样式后分不清"没生效"和"没刷新"时，靠它一眼确认。
const APP_VERSION = 'v0.11.0';
const BUILD_TAG = '2026-10-26b · 品牌与图标';
const buildTagEl = document.getElementById('build-tag');
if (buildTagEl) {
  buildTagEl.textContent = `${APP_VERSION} · 本地`;
  buildTagEl.title = `${BUILD_TAG} · 数据不出本机`;
}

checkHealth();

// 首次使用引导：讲清"不装扩展也能用"，只提示一次。
// 为什么要在界面里说这件事：扩展要开开发者模式加载，很多人到这一步就放弃了，
// 而实际上粘贴 JD 就能走完全流程。不说明的话，用户会以为这东西必须装扩展。
(function initOnboard() {
  const KEY = 'jobpilot-onboard-jobs-v1';
  const box = document.getElementById('jobs-onboard');
  const btn = document.getElementById('btn-onboard-ok');
  if (!box || !btn) return;
  try {
    if (localStorage.getItem(KEY) === '1') return;
  } catch {
    // 隐私模式禁用 localStorage 时照常显示，只是记不住"已读"
  }
  box.hidden = false;
  btn.addEventListener('click', () => {
    box.hidden = true;
    try {
      localStorage.setItem(KEY, '1');
    } catch {
      /* 存不了就算了 */
    }
  });
})();

// 视图恢复必须独立于数据加载：之前挂在 Promise.all().then() 里，
// 链条里任何一步抛错就整段不执行，用户点「工作台」看到的还是简历页，
// 而且控制台没有任何提示，完全查不出来。
const initialView = (location.hash || '').replace('#', '');
if (initialView) activateView(initialView);
if (initialView === 'radar') {
  loadRadar();
  loadRadarPlan();
}

loadResumes().then(fillWorkbenchSelects).catch(() => {});
loadJobs()
  .then(() => {
    fillWorkbenchSelects();
    if (initialView === 'workbench') loadWorkbenchStats();
  })
  .catch(() => {});
