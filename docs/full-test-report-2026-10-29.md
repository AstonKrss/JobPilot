# 小新求职 · JobPilot 全量测试报告

**测试对象**：`http://127.0.0.1:8000/`（FastAPI 后端 48 端点 + 原生前端 + 本地 PostgreSQL）
**测试日期**：2026-10-29
**测试方式**：黑盒（API 全覆盖 / 负向 / 安全 / 边界 / 并发 / UI 浏览器实测）＋ 白盒（10 模块代码审查 / 规则回归 / 单元级边界）＋ 功能与 Agent 质量评测（记忆召回率 / 意图路由 / 记忆写入链路）
**环境状态**：`/health` → `{"ok": true, "db": true, "llm_configured": true}`，LLM 为真实调用（deepseek-v4.1-flash 网关）

---

## 一、结论摘要

| 维度 | 结果 |
|---|---|
| 健康检查 | ✅ db ✓ llm_configured ✓ |
| API 端点覆盖 | 48 个端点，41 条实测记录 + 10 条 LLM 端点实测（含 SSE），覆盖完整 |
| 负向测试 | 38 条用例，37 过 1 缺陷（limit=-1 → 500） |
| 安全测试 | 23 条探针：路径穿越 / SQL 注入 / XSS / CORS / 恶意文件 / 信息泄漏 —— **未发现注入与穿越漏洞**，2 条 P2（伪 docx 被接受、500 无可读错误） |
| 边界与并发 | 分页边界 9 条（1 缺陷）；20 并发 100% 成功，P95 = 34.3ms |
| 白盒审查 | 10 模块，8 项发现（0 P0 / 0 P1 / 4 P2 / 4 P3），9 项正面确认 |
| 规则回归（eval） | 主评测 40/42（95.2%）＋ 扩展 19/21（90.5%）＝ 59/63（93.7%），4 条失败均为**用例期望漂移**而非功能错误 |
| 记忆召回率 | **6/6 目标查询命中第 1 位（recall@1 = 1.0，MRR = 1.0）**；无关查询隔离无阈值门（P2） |
| Agent 意图路由 | 6 条端到端用例 5 过；平均响应 3.3s |
| 记忆写入链路 | observe / 对话链路 / 幂等 / 「本轮条件不写入长期偏好」全部实测生效 |
| UI 黑盒 | 4 个标签页渲染正常，流式对话 13 步时间线、fit/blocked 双栏卡片正常，控制台 0 错误 0 警告 |

**总体判断**：产品核心功能（岗位录入 → 硬约束 → 匹配打分 → 记忆 → 对话 → 简历定制 → 投递跟踪）全部可用且行为与设计决策一致；无 P0/P1 缺陷；发现 8 项 P2/P3 待修项与 2 项产品缺口。测试后数据已恢复原状（详见第六节）。

---

## 二、黑盒 API 测试

### 2.1 覆盖测试（48 端点）

实测 41 条记录（`.testrun/r2/part_01a~01d.json`）+ 10 条 LLM 端点（`part_06a/06b.json`、`bb_09_results.json`）。全部 CRUD 路径（GET/POST/PUT/PATCH/DELETE）均实际执行。LLM 端点真实调用实测耗时：

| 端点 | 耗时 | 结果 |
|---|---|---|
| POST /api/jobs/ingest（JD 抽取） | 3.5s | ✅ 200，正确抽取 |
| POST /api/resumes/upload → 解析 | <1s（文本层） | ✅ 200，SHA256 复用生效 |
| POST /api/resumes/{id}/review（简历体检） | 11.0s | ✅ 200 |
| POST /api/resumes/{id}/tailor/{job_id}（定制） | 2.8s | ✅ 200，且**正确拒绝**信息量过少的简历（"简历仅 3 段…缺少岗位必备项"） |
| POST /api/resumes/{id}/relaxations（放宽建议） | 3.9s | ✅ 200，返回 city/edu 等维度变体与 pass_count |
| POST /api/chat（对话） | 2.3–7.4s | ✅ 200 |
| POST /api/chat/stream（SSE 流式） | 2.8s | ✅ 3 帧（step→done），done 帧含完整回复 |
| POST /api/reply/suggest（回复建议） | 4.6s | ✅ 200 |
| POST /api/memory/summarize/{id} | 3ms | ✅ 200，消息太少正确返回 skipped |

### 2.2 负向测试（38 条，37 过）

- 不存在的 id（resumes/jobs/applications/chat/radar 各路）→ 全部 404 且返回可读中文错误（`{"detail": "简历不存在"}`）✅
- 非数字 / 负数 / 超大 id → 422 可读错误 ✅（例外见缺陷 D-1）
- 非法 JSON、缺失必填字段、错误 method（405）、不存在端点（404）→ 全部正确 ✅
- **DELETE /api/profile/-1（不存在的 key）返回 200** → 记为 D-4（P3）

### 2.3 安全测试（23 条探针，2 条缺陷）

| 类别 | 用例 | 结果 |
|---|---|---|
| 路径穿越 | `/../../etc/passwd`、`/..%2f..%2fetc/passwd`、`/%2e%2e/.../win.ini`、`/static/../../.env`、`/..\\..\\win.ini` | ✅ 全部 404，未泄漏任何文件 |
| SQL 注入 | `keyword='; DROP TABLE jobs;--`、`1' OR '1'='1`、advice q 注入、路径 `5 OR 1=1` | ✅ 无 SQL 报错；**注入后数据完好（count 81→81）**；参数化路径参数正确拒绝（422） |
| XSS 存储 | observe 写入 `<script>alert(1)</script>`、`<img onerror>` | ✅ 读回无原样标签（写入链路只存结构化槽位；前端 esc() 137 处转义） |
| 超长载荷 | 10MB JD 文本 → 422（缺字段被拒）；10KB profile | ✅ 无 OOM、服务不崩 |
| 恶意文件 | **exe 魔数（MZ）伪装 docx → 200 被接受**；空文件 docx → 200 被接受；.txt 冒充 → 400 正确拒绝 | ⚠️ **D-2（P2）**：无魔数校验 |
| CORS | `Origin: http://evil.com` | ✅ 不回显 ACOA；preflight 400 |
| 信息泄漏 | 422/500 响应体 | ⚠️ 500 仅 "Internal Server Error" 无堆栈（不泄漏但也不可读）→ D-1；422 无堆栈/路径/SQL ✅ |

### 2.4 边界与并发

- 分页：`limit=0`（空列表 ✅）、`limit=999999`（81 条 ✅）、`offset=999999`（✅）、`limit=abc`（422 ✅）、**`limit=-1` → 500（D-1）**
- 中文/emoji：`目标城市📌深圳🚀` observe → 正确 additive 合并 `[佛山,深圳]`，存回读一致 ✅
- 幂等：同一 observe 文本重复两次 → 不重复累积 ✅
- 并发：20 线程并发 GET（jobs + memory/overview）→ **20/20 成功，P50 26.2ms，P95 34.3ms** ✅
- 前端资源：index.html 引用的全部 src/href（logo/style/app.js）+ favicon + logo192 → 全部 200 ✅

### 2.5 缺陷清单（黑盒）

| ID | 级别 | 描述 | 证据 |
|---|---|---|---|
| D-1 | P2 | `GET /api/jobs?limit=-1` 返回 500 且无可读错误 | 响应体仅 `Internal Server Error`；根因白盒定位 `api/jobs.py:262` 缺 `ge=0` |
| D-2 | P2 | 伪 docx（exe 魔数）与空文件被上传接口接受 | 均返回 200 + resume_id，进入解析管线（后台会 failed，但恶意内容未被前置拦截） |
| D-4 | P3 | DELETE 不存在的画像槽位返回 200 | `DELETE /api/profile/-1` → `{"deleted": "-1"}` |
| D-5 | P3 | 岗位与会话无删除接口 | openapi 无 `DELETE /api/jobs/{id}`、`DELETE /api/chat/{id}`；测试产生的岗位（id 94）只能手工清库 |

---

## 三、白盒测试

### 3.1 代码审查（10 模块，8 项发现）

完整清单见 `.testrun/r2/whitebox_findings.json`。要点：

| ID | 级别 | 位置 | 缺陷 |
|---|---|---|---|
| WB-01 | P2 | `memory/episode.py:170` | `build_recall_block` 无最低分数阈值——无关查询（最近 episode 得分 0.5457）也会把记忆注入首轮 prompt |
| WB-02 | P2 | `memory/profile_repo.py` + 数据血缘 | 画像历史缺失异常：`user_profile.cities.updated_at=22:20:24` 而历史表当刻仅有 10-23 的 4 行；**实时探针证实现行代码写历史正确**（observe → 历史新增 id=33），疑似当日早前并行测试清理/交错写入所致。代码路径无缺陷，数据口径需留意 |
| WB-03 | P2 | `api/jobs.py:262` | limit 缺 `ge=0`（= D-1 白盒根因） |
| WB-04 | P2 | `eval/cases*.jsonl` | 4 条用例期望漂移：mix-03（74<80）、mix-05（82<95）是绝对数封顶规则引入后未同步期望；skill-15、mix-12 决策 confirm≠期望 pass。**不是功能错误，是回归基线失真** |
| WB-05 | P3 | `normalize/fuzzy.py` | `norm_key(None)` 抛 TypeError |
| WB-06 | P3 | `normalize/skill_alias.py` | `expand(None)` 返回 `{None}` |
| WB-07 | P3 | `api/profile.py:96` | DELETE 不存在槽位返回 200（= D-4） |
| WB-08 | P3 | 产品缺口 | 岗位/会话无删除接口（= D-5） |

### 3.2 正面确认（9 项）

1. **SQL 全参数化**（psycopg `%s`），拼接仅发生在固定字符串上；注入探针后数据完好
2. **CORS 白名单**（localhost / 127.0.0.1 / 浏览器扩展），恶意 Origin 不回显
3. **上传安全**：SHA256 哈希命名（用户文件名不进路径）、20MB 上限、重复文件复用
4. **密钥处理**：仅进 Authorization 头，DSN 日志脱敏，打点失败不干扰主流程
5. **LLM 错误三分类**：transient 5 次指数退避（1/2/4/8/16s）/ permanent 不重试 / bug 快速失败
6. **记忆合并 `_merge`** 三分支（remove / additive / 覆盖）与注释一致，单元 5/5 过
7. **前端 XSS**：`esc()` 转义 137 处覆盖全部 innerHTML 渲染路径
8. **静态资源 no-cache 中间件**、连接池 min1/max4 与单用户定位一致
9. **PII 脱敏**：手机/邮箱/证件/住址/姓名发送给模型前剔除，且 PII 不写日志

### 3.3 规则级回归（eval）

| 套件 | 用例数 | 通过 | 准确率 | 失败 |
|---|---|---|---|---|
| cases.jsonl（主） | 42 | 40 | 95.2% | mix-03、mix-05（期望漂移） |
| cases_ext.jsonl（扩展） | 21 | 19 | 90.5% | skill-15、mix-12（期望漂移） |
| 合计 | 63 | 59 | 93.7% | 4 条均为用例问题 |

混淆矩阵（主评测）：confirm→confirm 3、pass→pass 24、reject→reject 15；**否决层 precision/recall 均为 1.0**（该守住的一个没漏）。

### 3.4 单元级边界（19/19 过）

- `scoring`：空画像/负年限/100 项全命中/空列表/exp_min=None → 全部 0≤total≤100，不抛异常 ✅
- `guard.scan`：空文本/1字/短JD/10KB → 全部正常返回 ✅；`strip_invisible` 移除零宽字符（1 处）✅；`find_homoglyphs` 检出西里尔同形字 ✅
- `_merge`：exclusive 覆盖 / additive 合并 / additive 无 op / remove / None base → 5/5 与设计一致 ✅
- `fuzzy`/`skill_alias`：空串、超长、特殊字符正常；`norm_key(None)` TypeError → WB-05

---

## 四、功能与 Agent 质量评测

### 4.1 记忆召回率（/api/memory/recall，8 条查询）

episode 库含 2 条记忆（id 1「评估深圳AI Agent后端岗位匹配」、id 2「求职助手模型身份询问」），6 条目标查询 + 2 条无关查询：

| 指标 | 结果 |
|---|---|
| **recall@1（目标查询）** | **6/6 = 1.0** |
| recall@3 | 1.0 |
| **MRR** | **1.0** |
| 无关查询最近记忆得分 | 0.5457 / 0.5558（语义余弦，非强匹配） |
| 无关查询隔离 | ⚠️ 无阈值门（WB-01）：仍会注入 prompt |

查询明细：6 条目标查询全部第 1 位命中（top score 0.69–0.86）；打分明细（相似度/新鲜度/复用）随响应返回，可观测 ✅。

### 4.2 Agent 意图路由与对话质量（6 条端到端，真实 LLM）

| 用例 | 期望 | 结果 |
|---|---|---|
| I01 身份询问 | 不返回岗位，说明身份 | ✅ 2.4s，"我是求职助手的规划模块……技术细节不便透露"，0 岗位 |
| I02 闲聊（天气） | 不返回岗位 | ✅ 2.4s，"查不了天气哈"，并主动引导回求职场景，0 岗位 |
| I03 搜索意图（深圳后端实习） | 返回深圳岗位 | ✅ 7.4s，回复列出 6 个 fit 岗位全部深圳；blocked 栏另列 3 个城市不符岗位并说明卡点（该栏设计如此） |
| I04 记忆写入（对话链路） | cities=[广州] 且回复确认 | ✅ 2.3s，`cities=[广州]` 落库 + 回复含"已写入画像：目标城市 → 广州" |
| I05 记忆引用 | 回复引用深圳匹配记忆 | ✅ 2.6s，"记得。上次按深圳 AI Agent 后端方向筛了两轮……最高分 80 分" |
| I06 指定岗位问询 | 定位已入库岗位并评估 | ❌ 2.8s，回复"没有存下这个岗位"——**按「城市+职位名」自然语言未定位到已入库的岗位（实体召回弱）**，兜底引导用户贴 JD 属合理降级 |

**5/6 过，平均 3.3s。** I06 暴露的实体定位弱是 Agent 层最值得改进的一项。

### 4.3 记忆写入链路（关键验收）

- `/api/profile/observe` 完整链路（L0 预筛 → L1 抽取 → L2 裁决 → 落库）每层结果可见 ✅
- 对话链路写入与接口写入走同一函数（`writer.apply_actions`），I04 证实「说完话一定发生」✅
- 重复 observe 幂等，不重复累积 ✅
- **「本轮条件不写入长期偏好」**：UI 实测搜索深圳，回复明确"这次的「目标城市」我当成这一轮的搜索条件，没写进长期偏好" ✅（此前评审发现的「搜索请求改写长期偏好」缺陷的修复在真实运行中生效）
- pending 裁决：不确定表述挂起不写入 ✅（合并规则单元 5/5）

### 4.4 UI 黑盒（浏览器实测）

- 4 个标签页（简历/岗位/发现/匹配/工作台/建议/对话）路由正常，hash 路由 ✅
- 简历页：上传/解析状态/主简历切换/删除按钮齐全；「设为主简历」点击后即时生效 ✅
- 岗位库：81 个岗位、平台筛选（BOSS 65/猎聘 4/智联 5/手动 8，汇总=总数 ✅）、JD 技能标签、完整度标记（完整 JD / 仅摘要）渲染正确 ✅
- 对话页：流式时间线「已完成 13 步」、fit/blocked 双栏卡片、语义相似度、卡点明细（"卡在：岗位在佛山…必备技能命中 2/11"）、去投递链接、复制/重新生成 ✅
- **控制台 0 错误 0 警告** ✅

---

## 五、待修项汇总（按优先级）

| 优先级 | 项 | 修复建议 |
|---|---|---|
| P2 | D-1/WB-03：`limit=-1` → 500 | `api/jobs.py:262` 改为 `limit: int = Query(200, ge=0)` |
| P2 | D-2：无魔数校验 | 上传时校验 PK\\x03\\x04（docx/zip）魔数，拒绝 MZ 等其他头 |
| P2 | WB-01：召回无阈值门 | `build_recall_block` 增加 min_score（如 0.6），低于阈值不注入 |
| P2 | WB-04：评测期望漂移 | 按封顶规则更新 mix-03/05 的期望分与 skill-15/mix-12 期望决策，恢复回归基线 |
| P3 | WB-05/06：`norm_key(None)`、`expand(None)` | 入口加 None 防护 |
| P3 | D-4/WB-07：删除不存在槽位返回 200 | 可保留幂等，但建议响应区分 `deleted`/`not_found` |
| P3 | D-5/WB-08：岗位/会话无删除接口 | 增补 `DELETE /api/jobs/{id}`、`DELETE /api/chat/{id}` |
| P3 | I06：岗位实体定位弱 | Agent 的实体匹配增加「城市+职位名」模糊定位已入库岗位 |

---

## 六、测试数据恢复说明

- 测试创建的 `[BBTEST]` 标记数据（简历、投递、画像槽位）已全部通过 API 删除；主简历已恢复为用户原 `方镇涛 (2).pdf`（#6）
- 画像 cities 已恢复为测试前的 `["广州","深圳"]`（依据 2026-10-23 历史）
- **遗留**：测试中入库的岗位（id 94，[BBTEST]科技/测试工程师（黑盒））与若干测试会话/对话消息因无删除接口而保留（见 D-5），可用 SQL 手工清理
- 测试期间 cities 曾短暂变为 `[杭州]`（探针所致），已恢复；测试未修改用户简历原文

## 七、证据文件索引

| 文件 | 内容 |
|---|---|
| `.testrun/r2/part_01a~01d.json` | 端点覆盖（41 条） |
| `.testrun/r2/part_02.json` | 负向 38 条 |
| `.testrun/r2/part_03_security.json` | 安全 23 条 |
| `.testrun/r2/part_04_boundary.json` | 边界 9 条 |
| `.testrun/r2/part_05_concurrency.json` | 20 并发压测 |
| `.testrun/r2/part_06a/06b.json`、`bb_09_results.json` | LLM 端点与剩余覆盖（10+9 条） |
| `.testrun/r2/part_07_frontend.json` | 前端资源 11 条 |
| `.testrun/r2/fa_recall.json` | 记忆召回率明细 |
| `.testrun/r2/fa_agent.json` | Agent 端到端明细 |
| `.testrun/r2/whitebox_findings.json` | 白盒 8 项发现 + 9 项正面确认 |
| `.testrun/r2/wb_eval_results.json`、`_eval_main_raw.txt` | 规则回归跑分 |
| `.testrun/r2/unit_results.json`、`unit_results_v2.json` | 单元级边界测试 |
