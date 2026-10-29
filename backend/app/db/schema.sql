-- 求职 Agent 数据表结构（M1：简历/岗位入库 + 归一化基础）
-- 设计基调：
--   1) 原始层与画像层分开存。原始块是证据，画像可随 prompt 版本重抽，互不影响。
--   2) 每条硬约束都要留原文依据，否则「一票否决」无法给出可信解释。
--   3) 所有阶段留快照，多 agent 不可复现的问题靠重放而非重跑解决。

-- ---------------------------------------------------------------------------
-- 简历
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resume (
    id           BIGSERIAL PRIMARY KEY,
    file_hash    TEXT        NOT NULL UNIQUE,   -- SHA256。同一文件重复上传直接复用，避免重复抽取花钱
    file_path    TEXT        NOT NULL,          -- 本地绝对路径，原文不出本机
    file_name    TEXT        NOT NULL,
    page_count   INT,
    -- 决定走哪条解析分支：有文本层就读字，没有才走 OCR。这个判断必须先做，
    -- 否则会拿空文本层去喂 LLM 得到一堆幻觉。
    source_kind  TEXT        NOT NULL CHECK (source_kind IN ('text_layer', 'scanned', 'docx')),
    parse_status TEXT        NOT NULL DEFAULT 'pending'
                 CHECK (parse_status IN ('pending', 'parsing', 'done', 'partial', 'failed')),
    -- 失败时记录真实原因（"PDF 有密码保护"而不是"解析失败"），永久错误要能对用户说清楚
    parse_error  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 文本块：保留坐标。既是版面还原的输入，也是"为什么这么读"的证据
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resume_block (
    id          BIGSERIAL PRIMARY KEY,
    resume_id   BIGINT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    page_no     INT    NOT NULL,
    block_index INT    NOT NULL,           -- 该页内 PDF 原生顺序
    x0          REAL, y0 REAL, x1 REAL, y1 REAL,
    text        TEXT   NOT NULL,
    block_type  TEXT,                      -- text | image
    read_order  INT,                       -- 聚类还原后的阅读顺序，NULL 表示尚未排序
    UNIQUE (resume_id, page_no, block_index)
);
CREATE INDEX IF NOT EXISTS idx_resume_block_order ON resume_block (resume_id, read_order);

-- ---------------------------------------------------------------------------
-- 简历画像（归一化后的结构化结果）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resume_profile (
    id              BIGSERIAL PRIMARY KEY,
    resume_id       BIGINT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    extract_version TEXT   NOT NULL,       -- prompt/模型版本组合。换版本可并存多份并对比
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,
    -- basic 只放匹配需要的字段。姓名/手机/邮箱/住址在送 LLM 前已剔除，故此处不会出现
    basic           JSONB  NOT NULL DEFAULT '{}'::jsonb,
    skills          JSONB  NOT NULL DEFAULT '[]'::jsonb,
    experiences     JSONB  NOT NULL DEFAULT '[]'::jsonb,
    projects        JSONB  NOT NULL DEFAULT '[]'::jsonb,
    raw_extract     JSONB,                 -- LLM 原始输出，抽取出错时用于回溯是哪一步歪的
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 同一份简历只允许一份 current 画像，避免下游读到两条而不知用哪个
CREATE UNIQUE INDEX IF NOT EXISTS uq_resume_profile_current
    ON resume_profile (resume_id) WHERE is_current;

-- ---------------------------------------------------------------------------
-- 实体别名表（归一化 L1）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity_alias (
    id          BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('company', 'school', 'skill', 'city')),
    canonical   TEXT NOT NULL,             -- 归一后的标准名
    alias       TEXT NOT NULL,             -- 该标准名的一种写法
    -- 检索键：去掉空格、统一全半角与大小写、剔除"有限公司/集团/股份"等通用后缀。
    -- 用检索键而非 alias 原文做匹配，才能让"北京字节跳动科技有限公司"与"字节跳动"在 L1 就撞上
    norm_key    TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('manual', 'auto', 'user_confirmed')),
    hit_count   INT  NOT NULL DEFAULT 0,   -- 命中次数，用于日后按热度优先加载
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_type, alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_lookup ON entity_alias (entity_type, norm_key);

-- ---------------------------------------------------------------------------
-- 待人工确认队列（归一化 L3 入口）
-- 低置信度不猜，抬给用户。用户确认结果回写 entity_alias(source='user_confirmed')，
-- 这就是"用户每确认一次系统就更准"的闭环落点
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity_confirm_queue (
    id          BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    raw_text    TEXT NOT NULL,
    norm_key    TEXT NOT NULL,
    candidates  JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{"canonical": "...", "score": 0.82}]
    context     TEXT,                                  -- 出现位置，帮助人判断
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed', 'rejected', 'ignored')),
    resolved_to TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_confirm_pending ON entity_confirm_queue (status, created_at);

-- ---------------------------------------------------------------------------
-- 岗位（来源：插件搬运原文 / 手动粘贴）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job (
    id            BIGSERIAL PRIMARY KEY,
    -- 三个平台 + 手动粘贴。之前漏了 zhaopin，导致智联岗位入库时约束报错，
    -- 而扩展把错误吞掉后仍显示"已入库"——用户看到的来源就变成了错的
    source        TEXT NOT NULL CHECK (source IN ('boss', 'liepin', 'zhaopin', 'manual')),
    source_job_id TEXT,
    url           TEXT,
    -- 插件只搬原文，抽取在后端做。这样平台改 DOM 时爆的是"能不能找到容器"，不是字段全错
    raw_html      TEXT,
    raw_text      TEXT,
    fetched_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 同源同 ID 去重。source_job_id 可空（手动粘贴），用部分索引避免空值互相冲突
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_source_id
    ON job (source, source_job_id) WHERE source_job_id IS NOT NULL;

-- 数据完整度：决定这份岗位画像的匹配结果有多可信。
--   full   = 从详情页抓取，含完整 JD 与技能要求
--   digest = 从列表页卡片抓取，只有标题/薪资/城市等摘要，没有技能要求
-- 没有这个标记，用户无法区分"技能全不符"和"根本没抓到技能要求"，
-- 后者会让匹配看起来宽松，但屏幕上显示的却是正常结果。
ALTER TABLE job
    ADD COLUMN IF NOT EXISTS detail_level TEXT NOT NULL DEFAULT 'unknown'
    CHECK (detail_level IN ('full', 'digest', 'unknown'));

-- ---------------------------------------------------------------------------
-- 岗位画像
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_profile (
    id                     BIGSERIAL PRIMARY KEY,
    job_id                 BIGINT NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    extract_version        TEXT   NOT NULL,
    is_current             BOOLEAN NOT NULL DEFAULT TRUE,
    title                  TEXT,
    company                TEXT,
    city                   TEXT,
    salary_raw             TEXT,
    job_nature             TEXT,           -- 全职 | 实习 | 外包 | 兼职
    edu_level              TEXT,           -- 大专 | 本科 | 硕士 | 博士 | 不限
    -- 是否要求统招。三态：TRUE=JD 明写要统招，FALSE=明写不限，NULL=没提（按不限制处理）。
    -- 必须是三态而非布尔，否则"没提"会被误当成"不要求"或"要求"，两种都是错的
    edu_fulltime_required  BOOLEAN,
    exp_min                INT,
    exp_max                INT,
    skills                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    description            TEXT,
    -- 每条硬约束的原文依据。否决解释必须能引用到这里，不允许事后让模型编理由
    hard_constraint_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_extract            JSONB,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_profile_current
    ON job_profile (job_id) WHERE is_current;

-- 技能要求必须区分 must / nice：前者一票否决，后者只做累加。
-- 混在一个数组里，匹配层只能二选一 —— 全部当否决会过度淘汰（缺一个 Kafka 就出局），
-- 全部当加分则「精通 Go」和「了解 Docker」权重相同，排序失真。
ALTER TABLE job_profile
    ADD COLUMN IF NOT EXISTS must_have_skills JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE job_profile
    ADD COLUMN IF NOT EXISTS nice_to_have_skills JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ---------------------------------------------------------------------------
-- 阶段快照：多 agent 不可复现问题的缓解手段
-- 每个 agent 的输入输出都落库，异常时从此步重放，而不是整条链路重跑
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stage_snapshot (
    id           BIGSERIAL PRIMARY KEY,
    trace_id     UUID   NOT NULL,          -- 一次完整处理的链路 ID
    subject_type TEXT   NOT NULL,          -- resume | job
    subject_id   BIGINT,
    stage        TEXT   NOT NULL,          -- parse | normalize | extract | match | rewrite | factcheck
    agent        TEXT,                     -- 哪个 agent 产生的
    attempt      INT    NOT NULL DEFAULT 1,
    input_hash   TEXT,                     -- 相同输入可直接复用输出，省一次调用
    output_json  JSONB,
    model        TEXT,
    tokens_in    INT,
    tokens_out   INT,
    latency_ms   INT,
    status       TEXT   NOT NULL CHECK (status IN ('ok', 'failed', 'degraded')),
    error_kind   TEXT   CHECK (error_kind IN ('transient', 'permanent', 'bug', NULL)),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_snapshot_trace ON stage_snapshot (trace_id, stage, attempt);
CREATE INDEX IF NOT EXISTS idx_snapshot_input ON stage_snapshot (stage, input_hash);

-- ---------------------------------------------------------------------------
-- 改写建议（M3）
-- 每条建议都要留下事实核查的结论，供日后审计：用户被建议改成什么样、
-- 当时核查认为是否越界。没有这张表，改写环节是黑箱。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rewrite_suggestion (
    id            BIGSERIAL PRIMARY KEY,
    resume_id     BIGINT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    -- 定向改写才有 job_id；普适体检为 NULL。岗位删除时建议保留但解除关联
    job_id        BIGINT REFERENCES job(id) ON DELETE SET NULL,
    trace_id      UUID,
    category      TEXT NOT NULL
                  CHECK (category IN ('resume_defect', 'jd_gap', 'nice_gap')),
    severity      TEXT NOT NULL CHECK (severity IN ('high', 'medium', 'low')),
    anchor        TEXT,                -- 指向简历的哪一段（公司 / 字段）
    problem       TEXT NOT NULL,
    direction     TEXT,
    example       TEXT,                -- 改写示例，必须通过事实核查才有意义
    jd_requirement TEXT,               -- 对应的 JD 原文要求
    -- passed  = 核查通过
    -- flagged = 末轮仍未通过，保留但明确标注（不隐藏）
    -- pending = 尚未核查
    fact_status   TEXT NOT NULL DEFAULT 'pending'
                  CHECK (fact_status IN ('passed', 'flagged', 'pending')),
    fact_issue    TEXT,                -- 未通过的原因
    attempts      INT NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_suggestion_resume ON rewrite_suggestion (resume_id, job_id, created_at DESC);

-- 会话（ChatAgent 的持久化状态）
CREATE TABLE IF NOT EXISTS conversation (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT,                        -- 首条用户消息截断而来，便于回看列表
    slots       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 结构化槽位：意图/城市/简历/偏好
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversation_message (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    role            TEXT   NOT NULL,         -- user / assistant
    content         TEXT   NOT NULL,
    meta            JSONB,                   -- 工具结果摘要：命中的岗位 id、分数等
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conv_msg ON conversation_message (conversation_id, id);

-- ---------------------------------------------------------------------------
-- 约束修补（对已有库生效）
-- CREATE TABLE IF NOT EXISTS 不会修改已经存在的表的约束，
-- 所以新增取值必须在这里单独 ALTER 一次，否则老库跑新代码会一直被拒绝
-- ---------------------------------------------------------------------------
ALTER TABLE job DROP CONSTRAINT IF EXISTS job_source_check;
ALTER TABLE job ADD CONSTRAINT job_source_check
    CHECK (source IN ('boss', 'liepin', 'zhaopin', 'manual'));

-- ---------------------------------------------------------------------------
-- 向量缓存
-- 语义层原来只在进程内 dict 缓存，重启服务后第一次匹配要把所有技能文本重新
-- 编码一遍（bge 模型 CPU 推理，几百条就是几秒）。落到库里就能跨重启复用。
-- 存 BYTEA 而不是 pgvector 的 vector 类型：这里只做「按文本哈希精确取」，
-- 不做近邻检索，512 维 float32 直接是 2KB，转成文本形式反而要 8KB 且更慢。
-- key 里带模型标识，换模型后旧向量自然取不到，不会被误用。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embedding_cache (
    key        TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    dim        INT  NOT NULL,
    vector     BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 匹配结果缓存
-- 一次全库匹配要跑语义对齐（可能带一次模型调用）+ 逐岗位规则打分，
-- 用户切个筛选条件、来回翻页就重算一遍，既慢又费钱。
-- 缓存键 = 简历 + 筛选条件 + 岗位库版本；岗位库有新增/重抽、简历重解析都会换版本号，
-- 旧结果自然取不到，不会拿过期结论骗人。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS match_run (
    id         BIGSERIAL PRIMARY KEY,
    resume_id  BIGINT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    cond_hash  TEXT NOT NULL,
    job_version TEXT NOT NULL,
    payload    JSONB NOT NULL,
    elapsed_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_match_run_lookup
    ON match_run (resume_id, cond_hash, job_version, created_at DESC);

-- ---------------------------------------------------------------------------
-- 投递跟踪（工作台一期）
-- 一个岗位一条 application（UNIQUE job_id），状态由用户手动推进，
-- 每次推进往 application_event 记一条，形成时间线。
-- 不做自动推断：从聊天记录里猜「HR 约了面试」误判的代价是错过跟进，
-- 用户手动点一下的成本远低于补救一次误判。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS application (
    id         BIGSERIAL PRIMARY KEY,
    job_id     BIGINT NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'candidate'
               CHECK (status IN ('candidate', 'applied')),
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id)
);
CREATE INDEX IF NOT EXISTS idx_application_status ON application (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS application_event (
    id             BIGSERIAL PRIMARY KEY,
    application_id BIGINT NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    status         TEXT NOT NULL,
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_application_event ON application_event (application_id, created_at);

-- 主简历：匹配/建议/定制/HR 回复的默认简历。
-- 之前各功能「取最新一份」是错的：用户库里同时有示例简历和真实简历时，
-- 取到示例会让回复里冒出他本人没有的经历（实测踩过）。
-- 唯一索引保证同一时刻最多一份默认简历。
ALTER TABLE resume ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_resume_one_default ON resume (is_default) WHERE is_default;

-- ---------------------------------------------------------------------------
-- HR 聊天记录（工作台二期）
-- 扩展在聊天页每次打开都全量同步最近消息，靠唯一索引去重，
-- 同一条消息重复同步不会翻倍。
-- 去重键用 (平台, 岗位标题, 角色, 内容) 而不是 URL：
-- c.liepin.com/?time=xxx 每次进入时间戳都变，URL 去重必然失效。
-- 局限：不同 HR 在同一岗位发同样的话只存一条，可接受（主要价值是
-- 「工作台能看跟这家聊到哪了」，不是完整存档）。
-- job_id 靠岗位标题精确匹配库里的岗位，匹配不上就存 NULL——宁可不关联也不猜。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_log (
    id        BIGSERIAL PRIMARY KEY,
    platform  TEXT NOT NULL,
    page_url  TEXT,
    job_title TEXT,
    job_id    BIGINT REFERENCES job(id) ON DELETE SET NULL,
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_log_dedupe
    ON chat_log (platform, coalesce(job_title, ''), role, content);
CREATE INDEX IF NOT EXISTS idx_chat_log_job ON chat_log (job_id, id);

-- ---------------------------------------------------------------------------
-- LLM 调用打点
-- 记忆架构的所有优化（append-only / 前缀稳定 / 缓存复用）都建立在
-- "缓存真的能命中"这个前提上。没有实测数据，一切设计都是推测。
-- usage_raw 原样存整段 usage：不同服务商的缓存字段名不统一
-- （DeepSeek 是 prompt_cache_hit_tokens），原样留底才能事后对账。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_call (
    id            BIGSERIAL PRIMARY KEY,
    what          TEXT NOT NULL,
    model         TEXT NOT NULL,
    session_id    TEXT,
    tokens_in     INT NOT NULL DEFAULT 0,
    tokens_cached INT NOT NULL DEFAULT 0,
    tokens_out    INT NOT NULL DEFAULT 0,
    elapsed_ms    INT,
    cost_cny      NUMERIC(10, 6),
    usage_raw     JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_call_time ON llm_call (created_at DESC);

-- ---------------------------------------------------------------------------
-- 用户画像（记忆架构 L1）
-- 特点：结构化槽位、可覆盖、带证据链、唯一权威。
-- 为什么要 history：用户"先只找佛山，一周后说广州也可以"是真实高频场景，
-- 只有当前值的话，既解释不清"为什么系统认为他接受广州"，也无法回滚误判。
-- modality 记的是表述强度（exclusive/additive/tentative），
-- 它比模型自评的 confidence 可靠，写入裁决靠它。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_profile (
    key             TEXT PRIMARY KEY,
    value           JSONB NOT NULL,
    evidence        TEXT,
    modality        TEXT,
    confidence      REAL,
    conversation_id BIGINT REFERENCES conversation(id) ON DELETE SET NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_profile_history (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT NOT NULL,
    old_value  JSONB,
    new_value  JSONB NOT NULL,
    evidence   TEXT,
    modality   TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_profile_history_key ON user_profile_history (key, changed_at DESC);

-- ---------------------------------------------------------------------------
-- 语义判定缓存
-- 实测结论：bge 余弦的**绝对值没有区分度**（正例 0.504 vs 负例 0.513，分布重叠），
-- 所以不能用阈值直接判定，只能继续交给模型。既然判定交给模型，那就把结果存下来：
-- 同一份简历的同一句证据 × 同一个岗位要求，判定结果与岗位无关、只与这两段文本有关。
-- 第二次匹配同一批岗位时几乎全部命中，一次模型调用都不用花。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skill_judge_cache (
    id         BIGSERIAL PRIMARY KEY,
    cache_key  TEXT NOT NULL UNIQUE,
    skill      TEXT NOT NULL,
    evidence   TEXT NOT NULL,
    equivalent BOOLEAN NOT NULL,
    reason     TEXT,
    model      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 情节记忆（L2）
-- 一次会话聊完，压成一条"主题 + 摘要 + 关键事实"，带向量。
-- 下次开新会话时按「向量相似 + 时间衰减 + 命中频次」召回 top-k，
-- 拼进固定前缀。这样"昨天聊过的结论"不用重讲，也避免了每轮重装配破坏缓存。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT REFERENCES conversation(id) ON DELETE SET NULL,
    job_id          BIGINT REFERENCES job(id) ON DELETE SET NULL,
    topic           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    facts           JSONB,
    embedding       BYTEA,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_hit_at     TIMESTAMPTZ,
    hit_count       INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episode_time ON episode (created_at DESC);

-- ---------------------------------------------------------------------------
-- 保留清洗前的 JD 原文
-- 之前只存了清洗后的文本（raw_text 名字叫 raw，实际是 cleaned），后果是：
-- ① 清洗规则删了什么，事后无从查证
-- ② 规则改坏之后没有原料做回归 —— 想补一份真实样本时发现库里全是加工过的
-- ③ 抽取结果出错时无法判断是"原文就这样"还是"清洗删掉了"
-- 已有数据补不回来（当时没存），从这次起新入库的都会留底。
-- ---------------------------------------------------------------------------
ALTER TABLE job ADD COLUMN IF NOT EXISTS raw_text_original TEXT;

-- ---------------------------------------------------------------------------
-- 投递状态从六态收敛到两态
-- 六态（候选 / 已投 / 沟通中 / 面试 / Offer / 不合适）实测一次都没被手动点过：
-- 招聘平台自带投递记录且是自动的，手动维护状态是纯负担。
-- 现在系统只需要知道"投了没"，"有没有回应"由聊天记录带出来，不靠人填。
-- 这段迁移必须幂等：init_schema 每次启动都跑，只在还留着旧约束时才动手。
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'application'::regclass
           AND conname = 'application_status_check'
           AND pg_get_constraintdef(oid) LIKE '%talking%'
    ) THEN
        -- 进展中的状态一律归为已投；「不合适」归为未投，但把来历写进 note，
        -- 免得日后看到一条"未投"记录却想不起当初为什么
        UPDATE application SET status = 'applied'
         WHERE status IN ('talking', 'interview', 'offer');
        UPDATE application
           SET status = 'candidate',
               note = coalesce(nullif(note, '') || ' | ', '') || '（历史状态：不合适）'
         WHERE status = 'rejected';
        ALTER TABLE application DROP CONSTRAINT application_status_check;
        ALTER TABLE application
            ADD CONSTRAINT application_status_check CHECK (status IN ('candidate', 'applied'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 公司画像（岗位雷达的基石）
--
-- 为什么必须有这一层
--   「美云智数适不适合我」这个判断，四个依据全是公司级的：在佛山、不是互联网
--   大厂、不卡学历、是美的子公司。而 job_profile 只有岗位级字段，一个都没有。
--   缺了公司层，系统只能对每个岗位单独猜，而这些事实对同一家公司是共享的。
--
--   公司级判断还有一个好处：新岗位进来时不需要任何额外信息就能被评价。
--   一个从没见过的岗位，只要公司认得出来，就知道该不该推给用户。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,                     -- 展示名，保留原文写法
    -- 归一键：去括号内容、去"有限公司/集团/股份/科技"等通用后缀、统一大小写与全半角。
    -- 用归一键而不是 name 做唯一约束，才能让"美的集团股份有限公司"与"美的集团"
    -- 落到同一条记录上，而不是各建一条、各自只有一半的岗位
    norm_key    TEXT NOT NULL UNIQUE,
    parent      TEXT,                              -- 母公司/所属集团
    hq_city     TEXT,
    industry    TEXT,                              -- 家电 / 制造 / 工业软件 / 互联网 / 金融 …
    nature      TEXT,                              -- 民企 / 国企 / 外企 / 合资 / 上市 / 事业单位
    scale       TEXT,                              -- 规模区间原文
    -- 是否互联网大厂（字节/腾讯/阿里/美团…）。
    -- 单独成列而不是塞进 tags：它同时影响两件事 —— 竞争烈度与学历卡控，
    -- 而且是排序里唯一一个"减分"的硬信号，必须能单独取用
    is_big_tech BOOLEAN NOT NULL DEFAULT FALSE,
    -- 是否外包/人力供应商。这类公司给简历带来的增值很低，
    -- 且常以"驻场"形式派到甲方，岗位标题看着像正经岗，实际签约主体不是甲方
    is_outsourcing BOOLEAN NOT NULL DEFAULT FALSE,
    -- 有明确校招/实习通道（三态：NULL = 不知道，不当成"没有"）
    campus_channel BOOLEAN,
    edu_note    TEXT,                              -- 学历门槛的人工判断依据
    note        TEXT,                              -- 一句话点评，给用户看的
    tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- 来源分级：seed = 公开信息预置；inferred = 从 JD 学习；user = 用户改过（优先）
    source      TEXT NOT NULL DEFAULT 'inferred'
                CHECK (source IN ('seed', 'inferred', 'user')),
    -- user 来源的字段不允许被自动学习覆盖，这一列记的是"用户改过哪些字段"
    locked_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_company_norm ON company (norm_key);

-- 岗位 → 公司。存归一键而不是外键：
-- ① 公司画像后建，老岗位不需要回填也能先挂上
-- ② 公司改名/合并时只要改归一键的映射，不用改一排外键
ALTER TABLE job_profile ADD COLUMN IF NOT EXISTS company_key TEXT;
CREATE INDEX IF NOT EXISTS idx_job_profile_company ON job_profile (company_key);

-- 届别：JD 里的「2028届」「27届」。
-- 这是校招场景下用户最先看的一个信号，而原来的抽取完全没碰它 ——
-- 结果就是"27届管培生"和"2028届校招"在列表里长得一模一样
ALTER TABLE job_profile ADD COLUMN IF NOT EXISTS graduate_years JSONB NOT NULL DEFAULT '[]'::jsonb;
-- 校招批次：校招 / 实习 / 社招 / 不限 / NULL（没提）
ALTER TABLE job_profile ADD COLUMN IF NOT EXISTS campus_type TEXT;

-- ---------------------------------------------------------------------------
-- 雷达偏好：用户"想去哪、能不能进"的显式设定
--
-- 不复用 user_profile 的原因：那张表是对话中被动学习来的，会被后续对话改动，
-- 而雷达的判定标准必须是稳定的 —— 否则用户随口一句"深圳也行"就会让筛选条件漂移，
-- 下次打开发现推荐全变了，却不知道为什么。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS radar_pref (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    cities       JSONB NOT NULL DEFAULT '["佛山","广州"]'::jsonb,
    -- 通勤分档：同城 = 1.0，邻市（广佛） = 0.6，其余 = 0。用分档而不是公里数，
    -- 因为不引入地图 API 就拿不到坐标，而"广佛同城"是这个用户的主要场景
    nearby_cities JSONB NOT NULL DEFAULT '["广州"]'::jsonb,
    graduation_year INT,
    edu_level    TEXT,
    -- 现阶段的求职档位：internship = 实习优先 / campus = 校招优先 / both = 都要
    stage        TEXT NOT NULL DEFAULT 'internship'
                 CHECK (stage IN ('internship', 'campus', 'both')),
    accept_outsourcing BOOLEAN NOT NULL DEFAULT FALSE,
    -- 公司质量的权重。用户要的是"优质岗位"，所以默认高于匹配分
    weight_company REAL NOT NULL DEFAULT 0.45,
    weight_match   REAL NOT NULL DEFAULT 0.35,
    weight_distance REAL NOT NULL DEFAULT 0.20,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO radar_pref (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- 公司法人的其他写法。同一家公司常有多个抬头（美的集团 / 美的 / Midea），
-- 抽取出来的 company 字段写法不受控，靠别名才能把岗位落到同一条公司记录上
ALTER TABLE company ADD COLUMN IF NOT EXISTS aliases JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ---------------------------------------------------------------------------
-- JD 完整性结论
--
-- JD 是第三方文本，可能同时写了两套内容：给人看的正文，和给模型看的
-- 不可见字符 / 指令行。清洗掉的字符必须留一份记录 —— 删除是静默的，
-- 如果连"删了什么"都不存，用户会以为这份 JD 原本就长这样，
-- 而排查"为什么抽取结果不对劲"时也无从下手。
-- ---------------------------------------------------------------------------
ALTER TABLE job ADD COLUMN IF NOT EXISTS integrity JSONB NOT NULL DEFAULT '{}'::jsonb;
