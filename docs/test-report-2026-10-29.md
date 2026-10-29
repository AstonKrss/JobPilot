# 小新求职 · 全量测试报告

- **被测对象**：`http://127.0.0.1:8000/`（本地 FastAPI 服务 + 单页前端）
- **测试时间**：2026-10-29 20:08 – 20:22
- **数据库**：PostgreSQL `ragjob@127.0.0.1:5433`，测试前 80 岗位 / 5 简历 / 1 投递
- **结论**：主链路可用，**无阻断性故障**。发现 **5 个应尽快修的功能缺陷**、**8 个可诊断性问题**、**6 个测试工程与体验问题**。

---

## 一、结果总览

| 测试层 | 用例数 | 通过 | 失败 | 说明 |
|---|---:|---:|---:|---|
| 静态资源与首页结构 | 16 | 16 | 0 | HTML/CSS/JS/图标全部可达，7 页导航齐全 |
| 只读接口（全量 GET） | 39 | 39 | 0 | 36 个端点全部 200，含 3 个合理 404 |
| 数据一致性校验 | 29 | 26 | 3 | 2 项为测试假设错误，1 项为真实问题 |
| 异常与边界输入 | 74 | 61 | 13 | 8 个真问题，4 个为合理校验，1 个为脚本误判 |
| 写操作业务链路 | 41 | 36 | 5 | 3 个真问题，2 个为测试假设错误 |
| 改写与导出自检 | 8 | 8 | 0 | 生成 → 自检 → 下载 docx 全通 |
| 补充覆盖（3 个写接口） | 6 | 6 | 0 | 画像写入 / 公司画像 / 公司关联 |
| **接口层小计** | **213** | **192** | **21** | 剔除误判后约 **12 个真实缺陷** |
| 项目自带回归（Python） | 416 | 415 | 1 | 仅 `check_llm_cache.py` 失败（脚本自身问题） |
| 项目自带回归（JS，补 jsdom 后） | 139 | 139 | 0 | 不补依赖时 3 个脚本直接起不来 |
| **合计** | **768** | **746** | **22** | |

接口覆盖率：**55/55 = 100%**（55 个 HTTP 端点全部实际调用，含写接口的正向与失败路径）。

---

## 二、缺陷清单

### P1 — 应尽快修（功能错误 / 安全）

#### 1. `limit=-1` 触发 500，参数缺少下界校验

```
GET /api/jobs?limit=-1  →  500 Internal Server Error
```

- **根因**：`backend/app/api/jobs.py:262` 的 `limit: int = 200` 直接拼进 SQL `LIMIT`，PostgreSQL 对负数报 `LIMIT must not be negative`，未转成 4xx。
- **对比**：`limit=0` 返回 0 条、`limit=100000` 返回全部 80 条，说明只有负数未处理。
- **建议**：改成 `limit: int = Query(200, ge=1, le=1000)`，与 `followups` 的 `days`（已有 `ge=1` 校验）对齐。

#### 2. 同一岗位重复入库不去重

```
POST /api/jobs/ingest  {raw_text: <同一份 JD>, url: <同一 URL>}  ×3
→ 返回 job_id 86, 89, 90 —— 三条内容完全相同的岗位
```

- **根因**：抽取结果有缓存（`cache_tag="jd_extract"`，不重复花钱），但 `repo.create_job` 未按 `(source, url)` 或正文哈希去重。
- **影响**：雷达与匹配结果里同一岗位出现多次，岗位总数与统计失真；用户手动粘贴两次就会踩到。
- **建议**：`job` 表加唯一索引 `(source, coalesce(url,''), source_job_id)`，或入库前按正文 SHA256 查重，命中则返回既有 `job_id` 并标记 `reused: true`（与简历上传的语义一致）。

#### 3. L0 数据闸门与文档不符，非岗位文本会被收录

实测把一段 40 字闲聊贴进「岗位」页：

```
raw_text = "今天天气不错，随便说点什么。这是一个讨论午饭吃什么的段落，没有任何招聘信息。"
→ 200，入库为 job 88，title="未命名"，description 里模型自己写着"原文内容与招聘无关"
```

- **根因**：`backend/app/api/jobs.py:214` 的闸门只调 `_reject_non_job(title, text)`，而该函数只做两件事：标题含「公司介绍/招聘简章/关于我们」等词、标题为空。README 声明的另外两条 —— **正文 ≥ 100 字**、**至少抽到 2 个关键字段** —— 并未实现。
- **影响**：任何误抓页面、复制错内容都会变成一条「未命名」岗位，进匹配池变噪声。这正是 README 里说要避免的情况。
- **建议**：补上正文长度与关键字段计数两条件；或把模型的「与招聘无关」判定纳入闸门（模型已经在 description 里说出来了，只是代码没用这个信号）。

#### 4. 存储型 XSS：岗位 URL 无协议校验，可执行脚本

```
POST /api/jobs/ingest {url: "javascript:alert(document.domain)"}  → 200 入库
GET  /api/jobs/91                                                 → url 原样返回
前端 backend/static/app.js:1777  →  <a href="${cleanJobUrl(j.url)}">去投递 ↗</a>
```

- **根因**：`JobIngestRequest.url` 是裸 `str`，无校验；前端 `cleanJobUrl()` 用 `new URL(u)` 解析，`javascript:` 能解析成功并原样 `toString()` 返回，`catch` 分支更是直接返回原串。
- **影响**：点击「去投递」即执行。本地应用能读取全部简历、HR 聊天记录，且这些数据本就设计为"不出本机"，XSS 恰好能把它送出去。攻击面来自不可信 JD（手动粘贴一条伪造链接即可）。
- **建议**：后端入库时校验 `urlparse(url).scheme in {"http","https"}`，否则置 null；前端 `cleanJobUrl` 增加同样的白名单，不合法返回 `''`。

#### 5. 匹配页「接受外包」硬编码勾选，与用户偏好相反

```
backend/static/index.html:225  <input type="checkbox" id="match-outsourcing" checked> 接受外包
```

- **事实**：用户画像 `accept_outsourcing = {value: false, evidence: "不接受外包", modality: "exclusive"}`，雷达偏好 `accept_outsourcing = false`。匹配页却默认**勾上**了「接受外包」。
- **影响**：同一产品两个结论 —— 雷达页把外包岗排除，匹配页把它们放进结果。README 专门强调过"两处口径分开写，同一条岗位会显示两种结论"是要避免的事。
- **建议**：默认值改为读 `/api/profile` 或 `radar_pref`，不要在 HTML 里写死。

#### 6. 雷达城市偏好与用户画像完全独立，对话里说的话对雷达无效

```
/api/radar        pref.cities = ["佛山", "广州"]
/api/profile      cities      = ["广州", "深圳"]   （来源：对话里"深圳也可以"）
```

- **根因**：`radar_pref` 是独立表，`_load_pref()` 只读它；`_enrich_pref()` 只从主简历补 `graduation_year` 与 `edu_level`，**不合并画像的 cities**。全库只有 `PUT /api/radar/pref` 能改它。
- **影响**：用户在对话里说「深圳也可以」，画像记下了，但雷达仍然排除深圳岗位。记忆系统在这里断链了。
- **建议**：二选一并写进文档 —— 要么 `_enrich_pref` 在 `cities` 为空时回落到画像；要么明确"雷达城市是手动配置"，并在发现页提示"画像里的深圳未生效，点这里同步"。

---

### P2 — 可诊断性与一致性

| # | 现象 | 期望 | 根因/说明 |
|---|---|---|---|
| 7 | `GET /api/resumes/999999/matches` → **409**「该简历尚无可用画像」 | 404 | 把"简历不存在"和"存在但未解析"混成同一句话，用户无法判断该重传还是等解析 |
| 8 | `POST /api/resumes/999999/review` → **409** 同上 | 404 | 同上 |
| 9 | `POST /api/memory/summarize/999999` → **200** `{created:false}` | 404 | 对不存在的会话静默成功，调用方以为已处理 |
| 10 | `POST /api/workbench/next-step {job_id:999999}` → **200**「先跑一次匹配」 | 404 | 对不存在的岗位给出业务建议，会让用户去找一个不存在的东西 |
| 11 | `PATCH /api/applications/{id}` 只传 `note` → **422** | 200 | `ApplicationUpdate.status` 是必填，PATCH 语义应为部分更新；改备注必须连带传 status |
| 12 | `GET /api/jobs/ingest` → **422**（`job_id` 解析失败） | 405 | `/api/jobs/{job_id}` 注册在 `/api/jobs/ingest` 之前，路径被遮蔽；POST 正常，但方法不匹配时返回了误导性错误 |
| 13 | 上传 0 字节或非 PDF 内容 → **200** 并创建 `resume` 记录（随后 `parse_status=failed`） | 400 | 无文件头校验，留下需要用户手动删的脏记录。解析失败文案本身是清楚的（"请确认是未损坏的 PDF"），问题只在于记录已建 |
| 14 | `PUT /api/radar/pref {weight_company:-1}` / 三权重和 2.7 / `graduation_year:1800` → 全部 **200** | 400/422 | 无范围校验。负权重会产生负的雷达分，权重和不为 1 会让分数越过 0–100 |

---

### P3 — 测试工程与体验

| # | 问题 | 说明 |
|---|---|---|
| 15 | 3 个 JS 回归脚本在干净环境跑不起来 | `check_monitor.js` / `check_ui_boot.js` / `check_ui_radar.js` 都 `require('jsdom')`，但项目**没有 package.json**，README 又直接写 `node scripts/check_ui_boot.js`。实测报 `MODULE_NOT_FOUND`；补装 jsdom 后 96 个用例全过 |
| 16 | `check_llm_cache.py` 不可重复执行 | 脚本不清理自己的 `stage_snapshot(cache_tag='check_cache')`，第二次跑必然 `first.cached=true` → 判 FAIL，看起来像"缓存坏了"。首次运行才是有效的 |
| 17 | 窄视口（618px）导航被截断 | 7 个标签横排溢出，「对话」显示成「文」，需横向滚动才能看到，且无滚动提示。桌面宽度下正常 |
| 18 | 匹配页城市输入框 placeholder 写死「北京,杭州」 | 与实际偏好（佛山/广州）无关，容易被误读成默认值 |
| 19 | 建议页简历下拉不默认选主简历 | 匹配页、工作台都默认选中主简历，建议页是空选项，行为不一致 |
| 20 | PDF→DOCX 文字覆盖率 74% | 样本为 `data/uploads/21b47ee0….pdf`（李强简历），脚本自定阈值 90%，实际 74% 仍判"转换成功"。属于已知的转换质量问题，但阈值未参与结论 |

---

## 三、已验证正常的关键能力

这些是最容易出问题、这次专门压过的点：

- **雷达口径统一**：逐条对比 80 个岗位的 `/api/radar` 列表判定与 `/api/radar/job/{id}` 单条判定，`grade` 与 `radar_score` **100% 一致**，0 差异。README 强调的"两处共用 `_build_item`"确实做到了。
- **计数一致性**：`grade_counts` 之和 = `total_jobs`（80）；`matches.counts` 之和 = 80 且 `items = pass + confirm`；`applications.stats.tracked` = 列表 count；`by_source` 之和 = tracked；`jobs.sources` 之和 = 80。全部吻合。
- **分页与过滤**：`limit` 1/5/20/100000 均正确封顶；`source=boss` 过滤生效。
- **流式对话**：SSE 正确推送 12 个 `step` 事件（"理解你的需求…" → "扫描岗位库（82 个在库岗位）" → …）+ 1 个 `done`，全程 10.3s，`done` 带 `reply/jobs/slots/steps`。
- **改写链路**：定制生成 9.6s，导出自检 6 项（4 pass / 2 warn / 0 fail），下载文件为合法 docx（44KB，`PK\x03\x04` 头，python-docx 可读，28 段 2606 字）。
- **并发**：`/api/radar` ×10（max 196ms）、`/api/jobs` ×20（max 106ms）、`/api/matches` ×10（max 989ms），**零失败**。
- **项目自带回归**：JD 清洗 180、JD 闸门 50、ATS 自检 35、事实核查 15、API 26、雷达 56、匹配 20、该催谁/校准 25、PDF 版式 6 —— 共 413 项全过；扩展与 UI 的 139 项（补 jsdom 后）全过。
- **安全基线**：`esc()` 转义了 `& < > " '`，`innerHTML` 的 64 处插值逐块静态扫描，用户数据基本都在 `esc()` 内；路径穿越 `/api/resumes/6/file/../../app/main.py` → 404；SQL 注入探针 `1 OR 1=1` → 422。

---

## 四、测试数据与副作用（已全部还原）

测试期间产生的数据均已清理，复核基线一致：**80 岗位 / 5 简历 / 1 投递 / 默认简历 #6 / 雷达偏好原值**。

| 产生 | 处理 |
|---|---|
| 失败简历 #7、#8（空文件与假 PDF 上传） | API 删除 |
| 测试简历 #9（变体 docx） | API 删除，默认简历还原为 #6 |
| 岗位 #88（闲聊文本）、#89/#90（重复入库）、#91–93（XSS URL） | 直连数据库按 id 精确删除，含 `job_profile` 关联行 |
| 投递 #2、聊天记录 #12/#13、会话 #10（SSE 测试） | 删除 |
| 雷达偏好（权重/城市/年份边界测试） | 测后 PUT 回原值，已核对一致 |
| 画像 `accept_outsourcing` | 写入后按原值还原（`updated_at` 有更新，值未变） |
| `data/tailored/resume6-job19.docx` | 保留（改写链路的正常产物，可手动删） |

> 清理时 `match_run` 表无 `job_id` 列（跳过），未影响数据完整性。

---

## 五、建议的修复顺序

1. `limit` 参数下界（一行改动，防 500）
2. 岗位入库去重（防数据膨胀，影响所有下游统计）
3. URL 协议白名单（后端 + 前端各一处，防 XSS）
4. 匹配页「接受外包」默认值改为读偏好（消除两页结论矛盾）
5. L0 闸门补齐两条判据（防脏岗位）
6. 404 / 409 语义拆分（5 处，纯错误码）
7. 雷达城市与画像的联动策略（先定语义，再改代码）
8. 补 `package.json` 声明 jsdom，让 JS 回归在干净环境可跑

---

## 六、本次未覆盖

- **扫描件 OCR 路径**（1–2 分钟/份，未构造扫描件样本）
- **浏览器扩展的真实抓取**（需要登录态的 BOSS/猎聘/智联页面；仅跑了离线 DOM 快照回归）
- **多平台采集与 HR 消息监控的端到端**（依赖真实账号与浏览器常开）
- **PDF→DOCX 的多样本回归**（仅用 1 份样本测了覆盖率）
- **长时间稳定性 / 内存泄漏**（仅测了短时并发）

---

*测试脚本与原始结果留存在 `.testrun/`（`t1_readonly.py`、`t2_consistency.py`、`t3_negative.py`、`t4_write.py`、`t5_tailor.py` 及对应 `t*_results.json`），可重复执行。*
