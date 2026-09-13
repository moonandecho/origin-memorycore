# origin-memorycore

[English](README.md) | [简体中文](README.zh-CN.md)

**MemoryCore 是一个面向 LLM Agent 的记忆治理层 (memory governance layer)。**

Agent 积累记忆的速度很快——偏好、事实、决策——而不维护的记忆会悄悄退化:重复条目堆积、过时事实滞留、热层塞满后开始拒绝写入。MemoryCore 阻止这一切发生。

它采用双层记忆架构:

- **热层 (Hot tier)** —— 高频使用的行为知识(偏好、规则、纠正),存放在本地快速文件中,始终在上下文内。
- **冷层 (Cold tier)** —— 低频事实,自动迁移出去,存放在进程内 SQLite 引擎中(或你配置的远程记忆服务)。

两层之间,一个治理核心维持记忆健康:

- **写入时去重** —— 通过全角→半角归一化、空白折叠、标点后空格删除 (`normalize_for_compare`) 后再比较去重;写入仍保留原始内容。
- **容量控制** —— 软/硬阈值在热层写满之前触发溢流,让它永不拒绝写入。
- **冷层治理** —— 周期性去重/清理,让冷层在增长中保持可检索。
- **回收队列** —— 被删除的条目有 30 天宽限期;召回一条被回收的记忆即可复活它。

结果:热层保持在预算内,冷层保持可检索,无论 Agent 积累多少记忆,记忆始终可维护。

基于 [MCP](https://modelcontextprotocol.io)(Model Context Protocol)`streamable-http` / stdio 标准构建。适用于任何 MCP 客户端,已在 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 上测试。

## 特性

- **记忆治理(核心)** —— 冷层数据完整性的三层保护:
  - **冷层写入去重**:写入冷层前,语义召回 + LLM 判断检查重复,更新已有条目而非创建冗余。
  - **热层去重归一化**:`normalize_for_compare` 执行全角→半角转换、空白折叠、标点后空格删除——确保去重在 CJK 标点变体和输入噪声下依然有效。写入始终保留原始内容。
  - **容量硬闸**:冷层强制软上限(6000 条,触发一次治理)和硬上限(10000 条,强制治理循环)——防止无界增长。
  - **回收队列**(`trash_store.py`):被删除的冷层条目移入 `~/.memorycore/trash.json`,30 天过期。召回被回收的条目时,若带有新的语义证据则恢复("召回即复活")。
- **冷/热路由** —— 每次写入都被分类:高重要度或偏好类 → 热层(本地);低频事实 → 冷层(远程);过时状态记录 → 丢弃。
- **六步溢流** —— 容量基线 → 去重 → 过时过滤 → 合并 → 安全写入(先写冷层,再删本地)→ 验证。
- **冷层治理** —— 去重合并、过时清理、冲突消解、embedding 完整性检查。
- **热层缓存策略 V2(2026-09-13)** —— 热层是缓存而非排序:rule/state/stub
  统一进入同一候选池,预算 `RULE_BUDGET_CHARS=2000`;寿命由活性 + 预算决定。
  protected 只是 ×3 排序乘数(`WEIGHT_PROTECT_MULT=3.0`),不是豁免;新鲜窗口
  再乘 `GRACE_MULT=9.0`。换出两阶段:先确认冷层写入,再留 ≤40 字指针 stub,
  通过 `memorycore_recall(handle=...)` 缺页写回;显式 0 指针预算时走 cold-only
  删除。`CACHE_POLICY_V2=0` 回滚候选池语义;`RULE_MIN_RESIDENCY_DAYS<=0` 或
  `GRACE_MULT<=0` 只关闭新鲜乘数。
- **SAFE-JUDGE v3 判型(2026-09-13)** —— `core/judge.py` 三态判型
  rule/state/ambiguous;同步路径零 LLM。ambiguous 不再静默下沉,而是留热层
  进入复审期限,周治理可用一次显式 LLM 终审;终审为 rule 给 14 天审计宽限。
  回滚:`JUDGE_V3_ENABLED=0`(`MEMORYCORE_JUDGE_V3_ENABLED`)、
  `JUDGE_AMBIGUOUS_HOLD=0`(`MEMORYCORE_JUDGE_AMBIGUOUS_HOLD`)。
- **每周治理(内置)** —— `python -m memorycore.weekly_maintenance` 是标准周度自动化:六步溢流 → 智能整理 → 冷层治理 → 报告落盘 `logs/`。智能整理将过时历史(LLM 确认、先写冷层)下沉、将重叠行为准则合并(原文先归档冷层);受保护准则绝不删除。调度由部署侧负责(systemd timer / launchd / cron)。仅通知为可选:设置环境变量 `MEMORYCORE_NOTIFY_SCRIPT` 将报告传给自有脚本;代码不内置任何个人信息。
- **容量控制** —— 软阈值(写入前溢流一次)/ 硬阈值(强制溢流)/ 目标比例。默认:5000 字符限制的 60% / 80% / 40%。
- **优雅降级** —— 冷层不可达?写入大声失败(绝不静默丢弃),溢流保留本地条目,健康检查返回本地状态并标注 `cold.error`。
- **零核心修改** —— 设计为即插即用的伴侣组件;Agent 内置的记忆工具继续正常工作。

## 架构

```
┌─────────────────────────────── Mac / 本地 ──────────────────────────────┐
│  LLM Agent (如 Hermes)                                                 │
│    │  MCP client                                                       │
│    ▼                                                                   │
│  MemoryCore MCP server                                                 │
│    ├─ local_store.py        热层: MEMORY.md / USER.md (基于字符)        │
│    ├─ classifier.py         冷/热/过时 路由规则                         │
│    ├─ overflow.py           六步溢流                                   │
│    ├─ maintenance.py        冷层治理                                   │
│    └─ cold_store_client.py  →  LocalBackend (SQLite, 进程内)           │
│                               or RemoteBackend (MCP streamable-http)   │
└─────────────────────────────────────────────────────────────────────────┘
                     LocalBackend: mnemosyne-memory (进程内引擎)
                     RemoteBackend: 远程 MCP 记忆服务

可选 (仅 Hermes Agent): hermes-plugin/memorycore-prefetch
  ┌───────────────────────────────────────────────────────────────────────┐
  │ MemoryProvider 插件 (单模型 qwen3, 默认开启)                           │
  │   system_prompt_block → 静态索引 (常驻激活)                            │
  │   prefetch → ColdStoreClient.recall_results(top_k=20)                 │
  │            → dense 排序 → 会话 + 热层去重 → top-5 注入                │
  │   关闭: MEMORYCORE_PREFETCH_ENABLED=0                                 │
  └───────────────────────────────────────────────────────────────────────┘
```

## 快速开始

### 前置依赖

- **ollama** — embedding API (安装: https://ollama.com)
- **qwen3-embedding:0.6b** — 推荐 embedding 模型 (1024 维)

```bash
# 安装 ollama (macOS/Linux)
curl -fsSL https://ollama.com/install.sh | sh

# 拉取 embedding 模型
ollama pull qwen3-embedding:0.6b
```

### 安装与运行

```bash
# 推荐: 用独立 venv 安装 — 不要与其它工具 (如 Hermes) 共用环境,
# 共用会让 memorycore 的 mcp 版本被别人决定, 宿主升级会连带它启动失败
python3 -m venv .venv && source .venv/bin/activate
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# 依赖: mcp>=2,<3 (已知兼容 2.0.0 / 2.2.0)
# 就这样! MemoryCore 使用 ollama 提供 embedding:
#   - 热层:  MEMORY.md / USER.md (默认 ~/.hermes/memories)
#   - 冷层:  SQLite (通过 mnemosyne-memory, 默认 ~/.memorycore/data/)
#   - Embedding: qwen3-embedding:0.6b (通过 ollama, http://localhost:11434/v1)
python -m memorycore.server          # stdio 传输 (默认)
```

**依赖说明** —— Port R1 新增的 `memorycore/core/judge.py`、缓存策略 V2 与
FIX8 换挡全部只依赖 Python 标准库, 不新增运行时依赖。LLM key/文件来源默认
关闭 (`MEMCORE_LLM_FILE_SOURCES=0`); 回滚开关见下文。

**数据目录布局**(全部位于 `~/.memorycore/` 下):

```
~/.memorycore/
├── data/          # SQLite 数据库 (MNEMOSYNE_DATA_DIR)
└── ...
```

可用 `MNEMOSYNE_DATA_DIR` 覆盖。

### 模型切换

默认 embedding 模型为 `qwen3-embedding:0.6b`(1024 维)。可通过环境变量使用任意 ollama 模型:

```bash
export MEMORYCORE_EMBED_URL="http://localhost:11434/v1"
export MEMORYCORE_EMBED_MODEL="nomic-embed-text"   # 或你偏好的模型
```

也可指向任何 OpenAI 兼容的 embedding API:

```bash
export MEMORYCORE_EMBED_URL="https://api.openai.com/v1"
export MEMORYCORE_EMBED_MODEL="text-embedding-3-small"
```

在 MCP 客户端注册(以 Hermes Agent `config.yaml` 为例):

```yaml
mcp_servers:
  memorycore:
    command: python
    args: ["-m", "memorycore.server"]
```

### 可选 LLM 增强(默认关闭)

热层压缩 / 休眠判定 / 模糊组合并可使用可选 LLM。未配置 key 时 MemoryCore
退化为纯规则(这是默认行为, 且始终安全)。启用方式:

```bash
export LLM_API_KEY="sk-..."                       # 必填
export LLM_BASE_URL="https://api.deepseek.com"    # 可选, 缺省值如上
export LLM_MODEL="deepseek-v4-flash"              # 可选, 缺省值如上
```

自检入口:

```bash
python -m memorycore.llm_check         # 零网络配置自检
python -m memorycore.llm_check --live  # 通路验证: 先 GET /models (零 token 成本),
                                       # 失败回退 max_tokens=1 completion
                                       # (费用 ~1e-5 元级, 可忽略)
```

默认只读环境变量。读取 `~/.hermes/.env` / `~/.hermes/config.yaml`(白名单键:
`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `DEEPSEEK_API_KEY` /
`XIAOMI_API_KEY`)需显式 `MEMCORE_LLM_FILE_SOURCES=1` — 默认关闭, 保证本仓库
不会静默捡起属于其它工具(如 Hermes)的 key 开始计费外呼。安全阀:
`MEMCORE_LLM_ENABLED=0`(总开关)、`MEMCORE_LLM_MAX_CALLS`(每轮调用上限,
默认 8)、`MEMCORE_LLM_COLD_MAX_CALLS`(冷层治理独立上限)、失败退避(一轮内
任一调用失败后剩余候选全部跳过)。LLM 状态(未配置/已解析/已验证)始终可见于
统计、报告与日志 — 绝不静默。

### 远程模式(可选)

如果你希望使用共享的远程 Mnemosyne MCP 服务而非本地引擎,设置 `MEMORYCORE_COLD_BACKEND=remote`:

```bash
export MEMORYCORE_COLD_BACKEND=remote
export MNEMOSYNE_URL="http://your-memory-service:9000/mcp"
python -m memorycore.server
```

暴露的工具:

| 工具 | 用途 |
|---|---|
| `memorycore_store_entry(content, importance, scope, target, type_hint)`(MCP 工具仍保留项目前缀的旧名称,可用 `list_tools` 查看) | 统一写入入口:路由冷 / 热 / 过时;可选 `type_hint=state|rule` 人工判型 |
| `memorycore_recall(query, top_k, handle)` | 主动召回冷层记忆(只读,补充每轮 prefetch);`handle` 支持指针直查/缺页路径 |
| `memorycore_trigger_overflow(target)` | 执行六步溢流,目标 ≤40% |
| `memorycore_run_cold_storage_maintenance()` | 冷层治理流程 |
| `memorycore_get_memory_usage()` | 热层用量 + 冷层统计 + 阈值 |
| `memorycore_memory_audit(target)` | 热层体检:条目类型/年龄/keep/sink 判定/LRU 观测/sink 候选 |
| `memorycore_get_rule_weight(target)` | 规则权重分布(只读缓存监控):w_eff、统一 `priority`/`in_grace`、字符vs预算、与实际选择器一致的下一批退役候选 |
| `memorycore_set_entry_type(target, match_text, type_override, protect_override)` | 人工标注(只写 sidecar,不改 .md),下次溢流按标注接管 |

## Hermes 集成 —— 每轮主动召回 prefetch

MCP server 与客户端无关。对于 **Hermes Agent**,有一个可选伴侣插件提供双通道冷层访问:

### 双通道设计

- **静态索引通道(常驻,零开销)** —— 系统提示块列出可用主题(通过 `MEMORYCORE_INDEX_TOPICS` 配置,逗号分隔),并引导 Agent 使用 `memorycore_recall(query)` 按需召回。
- **每轮主动召回通道(默认开启)** —— 每轮对话自动召回冷层,按 dense 分数排序,注入 top-5 到上下文,让 Agent 开口前就"想起"相关内容。设置 `MEMORYCORE_PREFETCH_ENABLED=0` 可关闭,仅保留按需召回。

### Prefetch 管道

```
query → 预处理 → 冷层召回(20 候选)
  → dense 排序 (qwen3) → top-5
  → 会话去重 → 热层去重 → 注入上下文
```

MemoryCore 采用**单模型 qwen3 架构(无 reranker)**。qwen3 的 dense 分数用于批次内相对排序;没有绝对阈值——dense 分数最高的 5 条候选在去重后始终注入。

### 优雅降级

当 ollama 不可达(未安装、未运行或模型未拉取)时,prefetch 静默返回空字符串——对话继续,没有注入的记忆,用户不会看到任何错误。DEBUG 级别日志会记录探测失败。

### 部署方案(Hermes Agent)

```bash
# 1. 安装 origin-memorycore(提供冷层引擎 + ColdStoreClient)
#    (独立 venv 安装; 依赖 mcp>=2,<3, 已知兼容 2.0.0 / 2.2.0)
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# 2. 把插件放入 Hermes 用户插件目录
mkdir -p ~/.hermes/plugins
cp -r hermes-plugin/memorycore-prefetch ~/.hermes/plugins/

# 3. 激活(下一会话生效)
hermes config set memory.provider memorycore-prefetch
```

部署后三种形态:

| 形态 | 配置 | 行为 |
|---|---|---|
| 默认(推荐) | 无需额外配置 | 静态索引 + 每轮 prefetch,注入 top-5 |
| 仅按需召回 | `MEMORYCORE_PREFETCH_ENABLED=0` | 只启用静态索引,Agent 通过 `memorycore_recall` 按需查询 |
| 自定义 embedding | `MEMORYCORE_EMBED_URL` + `MEMORYCORE_EMBED_MODEL` | 指向不同 ollama 实例或 OpenAI 兼容 API |

### 插件配置

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `MEMORYCORE_PREFETCH_ENABLED` | *(未设置)* | 设为 `0` 关闭每轮主动召回 |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | Ollama 或 OpenAI 兼容 embedding API 基地址 |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | Embedding 模型名称(推荐 1024 维) |
| `MEMORYCORE_INDEX_TOPICS` | *(未设置)* | 系统提示索引块的主题列表(逗号分隔) |

要求与注意:

- **Hermes 专用**:插件导入 Hermes 运行时模块(`agent.memory_provider`),不能作为独立包运行——它是 MemoryCore 的 Hermes 集成侧。完整说明见 [hermes-plugin/memorycore-prefetch/README.md](hermes-plugin/memorycore-prefetch/README.md)。
- 每次召回保持 5s 超时;失败静默降级为空注入,绝不阻塞对话。

## 热层治理机制

热层(MEMORY.md / USER.md)每轮全量注入上下文,必须保持精简与时效。MemoryCore
在六步溢流之上叠加三层机制,让历史记录确定性退役,而不是无限堆积:

### 热层元数据老化

- sidecar 元数据:`MEMORY.meta.json` / `USER.meta.json` 与 .md 文件同目录,
  以条目内容的 SHA-256 为键;原子写 + 文件锁保证跨进程安全;§ 分隔的 .md
  格式零改动,宿主 memory 工具不受影响。
- 每条条目被判定为 `state`(历史决策/状态记录)或 `rule`(准则/偏好):
  - `state`:写入 7 天后退役至冷层(可配置 `STATE_TTL_DAYS`)
  - `rule`:永不因年龄退役;30 天未更新的长条目(>200 字)成为 LLM 压缩
    候选(可配置 `RULE_COMPRESS_DAYS`)。准则另有失效信号阶梯(见下文)
    提供可持续出口,不误伤活跃偏好。
- 条目内容变化 → 键变化 → 下次 reconcile 对内容重新判型并回收孤儿键。

### 双写入口治理

- `store_fact` 写入口:内容呈完成态(含日期 + 拍板/已配置等完成态词,且
  无行为指令词)→ 直接写冷层,污染不进热层。
- 插件 `on_memory_write` 直写通道:内置 memory 工具每次 add/replace 提交后
  立即判型;`state` 直写后台迁移冷层(查重 → 冷层写成功 → 删热层;冷层失败
  则保留热层并盖章 state 作为 7 天到期兜底),与占用水位无关。单工作线程
  消费有界队列(容量 128),队列满则跳过,由下次溢流 reconcile 兜底补盖。

### 元数据优先溢流

每次溢流先 reconcile 元数据(补盖无元数据的存量条目、回收孤儿键),再按
元数据退役;关键词表降级为无元数据条目的兜底。sidecar 故障自动降级到
关键词路径,不阻塞溢流。

### rule 失效信号(分层保护)

纯 `rule` 构成的热层按设计没有出口("偏好永不下沉"),因此永不编辑的短准则
会一直占位、最终塞满热层。MemoryCore 用**压力阶梯**补上出口:每轮溢流实测
占用(基线),压力越高开放的出口越深(变化)。五个可观测信号只决定"资格与
排序",压力决定"出不出手":

| 信号 | 观测内容 | 动作 |
|---|---|---|
| S1 写入闲置 | sidecar `updated_at` | 压缩(30 天)/ stub(45 天)资格闸门 |
| S2 完成态复核 | 内嵌日期 ≥60 天 + ≥2 个完成态词 + 零行为指令词 | 误戴 rule 章的历史记录重判为 `state` → 走 7 天 TTL 正常下沉 |
| S3 同主题聚簇 | 词法相似度(+可选嵌入通道) | 同主题条目合并为一条;合并后变长的条目后续自动获得压缩资格 |
| S4 主题活性 | 本地查询活动日志(prefetch/recall,滚动 45 天,可选)+ LLM 休眠判定 | 高压下的休眠 B 类准则:全文先写冷层确认,本地留 ≤40 字指针 |
| S5 跨层冗余 | 冷层召回匹配 | 冷层已有等价全文 → 删本地副本(信息零丢失) |

**分层保护**:A 类元准则(行为/交互/写作风格)、红线类与 importance ≥ 0.9
的条目永不参与 S2/S4/S5,只允许合并/压缩。stub 指针自带生命周期(高压下
最老优先回收,冷层零调用),指针不会二次塞满热层。所有出口遵循"先冷层
后本地":冷层确认成功才动本地,任一失败保留原样。信号缺失(无活动日志、
无 LLM key)时阶梯整体降级为原有行为,绝不猜测。

常量(`memorycore/core/config.py`):`RULE_RETYPE_DAYS=60`、
`RULE_STUB_IDLE_DAYS=45`、`ACTIVITY_WINDOW_DAYS=30`、`MAX_STUB_PER_RUN=3`、
`STUB_MAX_CHARS=40`、`IMPORTANCE_PROTECT=0.9`。

### 热层缓存策略 V2(LRU,2026-09-13)

- **统一候选池**:`rule`/`state`/指针 `stub` 不再分档,全部进入同一排序池:
  `w_eff × protected(×3.0) × kw_sink(×0.5) × 新鲜窗口(×9.0)`。
- **预算硬约束**:规则生态 `RULE_BUDGET_CHARS=2000`(与 40% 目标同源);
  换出一律先冷层写成功;单轮全文换出 ≤ `MAX_EVICT_PER_RUN=3`。
- **两阶段换出**:普通路径留 ≤40 字指针(`STUB_MAX_CHARS=40`,句柄
  `STUB_HANDLE_MAX_CHARS=20`)+ `cold_id`;`memorycore_recall(handle=...)`
  绕过阈值直查并标记 `page_fault`,驱动写回恢复;显式 0 指针预算时全文冷写
  成功后直接删本地(`--budget 0` / T3 cold-only)。
- **无永久驻留**:全 protected / 红线 / importance≥0.9 / `protect_override`
  在足够压力下仍可换出;固定压测:
  `tests/test_cache_policy_v2.py::test_no_permanent_residency_all_protected`。
- **新鲜窗口是排序乘数**:`RULE_MIN_RESIDENCY_DAYS=7`,`written_at` /
  `last_recall_hit_at` 经 `_ts_anchor` 统一入口;窗口内 ×`GRACE_MULT=9.0`
  (设计用 bundled R2 合成快照夹具标定,25 条;`tools/residency_dryrun.py`
  可复现新鲜窗口排序),压力足够仍可换出。
- **活性参数**:`WEIGHT_INIT=1.0`,半衰期 30 天,强命中 +1.0
  (`HIT_STRONG_COS=0.48`),弱命中 +0.3,封顶 `WEIGHT_MAX=5.0`,
  kw-sink 乘数 `WEIGHT_KWSINK_MULT=0.5`。

### SAFE-JUDGE v3 判型(2026-09-13)

`memorycore/core/judge.py` 用一次确定性三态判型替代旧的
`classify()` + `should_keep_local()` 双判:

- `state` → 冷迁移(冷写成功才删本地);
- `rule` → 留热层,写 `judge_v3` 审计字段;
- `ambiguous` → 强制留热层,写 `judge_review_at`(首审 +7 天,
  `JUDGE_AMBIGUOUS_LRU_DAYS=21` A1 指针兜底,最多 2 次复审)。同步判型零
  LLM,只有周治理可发起一次显式 LLM 终审;终审为 rule 给
  `JUDGE_RESOLVED_RULE_GRACE_DAYS=14` 审计宽限。
- strong rule 与 ambiguous 在写入口强制 hot;ambiguous 本地写失败直接报错,
  不允许静默冷迁兜底(`DESIGN-DEVIATIONS.md` §6.5)。

### 回滚开关

| 开关 | 默认 | 效果 |
|---|---|---|
| `MEMORYCORE_CACHE_POLICY_V2=0` | `1` | 回退旧资格候选池(protected 资格豁免/年龄门),冷写安全铁律不变;`PROTECT_SKIP_LRU=1` 仅告警(已废弃) |
| `RULE_MIN_RESIDENCY_DAYS <= 0` | `7` | 只关闭新鲜窗口乘数(等价旧纯 rank 排序) |
| `GRACE_MULT <= 0` | `9.0` | 同上,排序乘数入口关闭 |
| `MEMORYCORE_RULE_BUDGET_ENABLED=0` | `1` | 关闭规则预算换出层(仍受硬 5000 字兜底) |
| `MEMORYCORE_JUDGE_V3_ENABLED=0` | `1` | 回退词法 v2 判型(`CLASSIFIER_V2_ENABLED` 决定 v2/v1),attack 基线 20/29 |
| `MEMORYCORE_JUDGE_AMBIGUOUS_HOLD=0` | `1` | ambiguous 当 rule(二值行为,不写复审期限) |
| `MEMCORE_LLM_FILE_SOURCES=1` | `0` | 选择性开启白名单 `~/.hermes/.env` / `config.yaml` 文件来源,默认绝不开 |

常量(`memorycore/core/config.py`):`RULE_BUDGET_CHARS=2000`、
`INDEX_BUDGET_CHARS=800`、`RULE_MIN_RESIDENCY_DAYS=7`、`GRACE_MULT=9.0`、
`WEIGHT_INIT=1.0`、`WEIGHT_PROTECT_MULT=3.0`、`WEIGHT_KWSINK_MULT=0.5`、
`WEIGHT_HALF_LIFE_DAYS=30`、`HIT_STRONG_COS=0.48`、`MAX_EVICT_PER_RUN=3`、
`MAX_STUB_PER_RUN=3`、`STUB_MAX_CHARS=40`、
`JUDGE_AMBIGUOUS_REVIEW_DAYS=7`、`JUDGE_AMBIGUOUS_LRU_DAYS=21`、
`JUDGE_RESOLVED_RULE_GRACE_DAYS=14`。

### 体检工具: memorycore_memory_audit

只读工具,列出热层每条条目的类型、年龄、退役计划与 keep/sink 判定,并附带
Phase 4 LRU 观测(每条规则的 weight / 有效权重 / 最近活跃 / 驻留天数)与
规则字符-预算对比 —— 排查"溢流空转"(热层满了却无条目可沉)的观测锚点。

活性维度 sink 候选 (2026-08-28):对 rule 型条目,若 `weight < 1.5` 且
`last_active_at` 距今超过 30 天且非 protected,体检将该条目标记为
`sink_candidate: true`(reason 为 `low_weight+inactive`),并汇总至
`lru_sink_candidates` 计数器 —— 仅体检可见,不改变溢流执行逻辑。

## 规模化测试与优化结果

MemoryCore 在万条级冷层规模下做了完整压力测试与召回优化(隔离测试环境,生产数据零接触,结果可复现)。

**写入与容量**

| 指标 | 结果 |
|---|---|
| 写入吞吐 | 10k 条共 467s,≈21.4 条/s(瓶颈在 embedding) |
| 库文件体积 | 300MB / 10k 条 |
| 内存占用 | 进程 RSS 仅 +19MB,全程平稳无泄漏特征 |

**查询延迟** —— top_k=5 时中位 48ms;万条规模与百条规模持平,无延迟退化。

**召回质量** —— 三项测试:

1. **精确匹配(原文自召回)**:20/20 全部命中 top1 —— 精确匹配能力完整。
2. **噪声抑制(无关查询)**:top1 dense 分数均值 0.056,绝大多数返回 0.0 —— 无关内容几乎不会混入结果。
3. **短查询召回(修复前 → 修复后)** —— 关键优化成果:

| 阶段 | 短查询命中率 |
|---|---|
| 修复前 | 0/8 |
| 修复后 | 5/8 (62.5%) |

**优化内容**:高密度主题下,固定候选截断 `k=max(top_k, 20)` 会把详细记忆挤出候选池,导致短查询召回失败。修复将候选截断放大为 `k=max(top_k*4, 300)`,并在召回入口内部放大候选后再截断返回——所有召回通道(每轮 prefetch + 按需 recall)一处修复全部受益。修复只发生在召回阶段,排序逻辑未改动,行为可预期、可回退。

> 注:测试在 10k 条合成库上进行(80 条"黄金记忆"+ 9920 条日常口吻填充记忆,与生产同配置),生产数据零污染。

## 可复现合成夹具

发布树在 `tests/fixtures/synthetic/` 提供中性合成夹具（25 条 `notehub` 规则：
19 rule + 恰好 6 条完成态/历史 state；5 条 USER 示例；配套 sidecar 元数据；
409 条写实分布的带时间戳查询：短问句改写、低重叠语义问法、真正无关噪声、
会被 F1 闸门挡掉的低信息短句与动作型指令，且不把规则原文抄进查询）。
依赖夹具的验收路径不读取任何生产记忆。用 bundled
一次性生成器重建 silver 回放夹具：

```bash
.venv/bin/python tools/build_fault_replay_fixture.py \
  --activity tests/fixtures/synthetic/activity.jsonl \
  --rules    tests/fixtures/synthetic/MEMORY.md \
  --out      tests/fixtures/fault_replay_silver.json
```

合成语料上复测基线：

| 检查项 | 结果 |
|---|---|
| 缺页率回放（`replay_fault_rate.py`） | `hits=186/200 faults=14 fault_rate=7.0% baseline=41.5% relative_drop=83.1% pass=True`（R3 写实合成语料；夹具可复现，与生产语料数值不同） |
| 合成快照 retype 干跑 | `19 rule / 6 state`，state 集合恰为 6 条 bundled 目标 |
| 迁移后水位 | `2598 / 5000 chars（51%）` |
| 快照预算回放 | `--budget 2000` 与 `--budget 0` 均 EXIT=0 |
| 驻留干跑 | 25 条 / 3441 chars / need 1441，`need_satisfied=True`、`new_evictable_when_full=True` |

## sqlite-vec 用户注意事项

如果你为 Mnemosyne 冷层启用 sqlite-vec 向量索引,请注意 `beam.py` 的 `_wm_vec_search_sqlite` 使用原始相似度公式 `sim = 1 - distance / (2 * EMBEDDING_DIM)`,会把 float32 距离压缩到 ~1.0,使动态阈值实际失效(所有结果都通过)。

**补丁**:在 float32 分支中,将公式替换为 `sim = 1 - d² / 2` —— 这给出归一化向量的精确余弦相似度,恢复正确的阈值行为。

## 冷存储契约

任何暴露以下五个 MCP 工具的服务都可以作为冷层:

| 工具 | 语义 |
|---|---|
| `remember(content, importance, scope)` | 存储一条记忆,返回 `memory_id` |
| `recall(query, top_k)` | 语义召回 |
| `update(memory_id, content)` | 合并更新已有记忆 |
| `forget(memory_id)` | 删除一条记忆 |
| `stats()` | `total` + embedding 完整性 |

完整契约与参考客户端见 [examples/cold-store-contract.md](examples/cold-store-contract.md)。

## 配置

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `MEMORYCORE_COLD_BACKEND` | `local` | 冷层后端:`local`(进程内)或 `remote`(MCP) |
| `MNEMOSYNE_URL` | *(空)* | 冷层 MCP 端点(`remote` 模式必需) |
| `MNEMOSYNE_DATA_DIR` | `~/.memorycore/data` | 本地 SQLite 数据目录 |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | Ollama 或 OpenAI 兼容 embedding API 基地址 |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | Embedding 模型名称(1024 维) |
| `MEMORY_DIR` | `~/.hermes/memories` | 热层目录(`MEMORY.md` / `USER.md`) |
| `ACTIVITY_LOG_ENABLED` | `1` | 查询活动日志(主题活性信号采集);设为 `0` 关闭日志并整体禁用 S4 stub-sink |
| `MNEMOSYNE_TIMEOUT` | `10.0` | 冷层请求超时(远程模式,秒) |
| `MEMORYCORE_CACHE_POLICY_V2` | `1` | 统一缓存候选池;`0` 回退旧资格池 |
| `MEMORYCORE_RULE_BUDGET_ENABLED` | `1` | 规则预算换出层;`0` 关闭(仍受硬 5000 字兜底) |
| `MEMORYCORE_JUDGE_V3_ENABLED` | `1` | SAFE-JUDGE v3 三态判型;`0` 回退词法 v2 |
| `MEMORYCORE_JUDGE_AMBIGUOUS_HOLD` | `1` | ambiguous 复审期限内留热;`0` 当 rule 处理 |
| `MEMCORE_LLM_FILE_SOURCES` | `0` | 选择性开启白名单 `~/.hermes/.env` / `config.yaml`,默认关闭 |

容量常量位于 `memorycore/core/config.py`(`CHAR_LIMIT_*`、`SOFT_THRESHOLD`、`HARD_THRESHOLD`、`TARGET_RATIO`)。

## 工作原理

1. **写入** —— `store_fact` 分类内容:
   - importance ≥ 0.8 或命中热关键词(偏好 / 规则 / 纠正 / 红线)→ **热层**,留在本地
   - 过时标记(短条目,如 "已修复 / fixed")→ **丢弃**(不迁移)
   - 其他 → **冷层**,直接写入远程服务
2. **溢流** —— 热层用量超过软阈值时,溢流将低频条目迁移到冷层;达到硬阈值时强制溢流直到 ≤ 目标。顺序永远是*先写冷层,验证,再删本地* —— 冷层失败也不会丢任何东西。
3. **治理** —— 周期性对冷层执行:合并重复、移除过时、消解冲突、验证 embedding 完整性。

## 许可

[MIT](LICENSE) © 2026 moonandecho

### 第三方许可

- [mnemosyne-memory](https://github.com/mnemosyne-oss/mnemosyne) — MIT,by AxDSan。`LocalBackend` 使用的进程内记忆引擎。
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — MIT。
- [ollama](https://ollama.com) — MIT。本地 embedding API 服务。
- [qwen3-embedding](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) — Apache-2.0,by Alibaba Cloud。默认 embedding 模型(非内置,通过 ollama 拉取)。
