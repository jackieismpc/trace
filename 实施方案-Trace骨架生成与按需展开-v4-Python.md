# Trace 骨架生成与按需展开组件 — 实施方案

- 提交仓库：https://atomgit.com/openeuler/agentic-engineering-team
- 计划完成：2026-08-15（Issue 期望时间为 09-30，我按提前完成安排）
- 许可证：Mulan PSL v2
- 技术栈：Python ≥ 3.11（写定）
- 版本：v4（2026-07-23 更新：技术栈由 Rust 调整为 Python；新增「业界参考」一节；时间计划扩写为分阶段实现步骤；性能指标按语言重新校定）

---

## 一、对任务的理解

这个任务表面是「裁剪 Trace + 按 span_id 展开」，实质要解决的是上下文预算的分配问题。

一次复杂 Agent 执行产生的 Trace，体积几乎全部由少数几类字段贡献：LLM span 的完整 prompt（每轮都带全量历史，随轮次平方级膨胀）、模型的 reasoning 内容、工具返回的检索结果或网页正文。这三类加起来通常占 90% 以上。而排查问题真正需要的东西——span 的父子关系、节点类型、成功还是失败、耗时——占比不到 1%。

也就是说，信息密度最高的部分体积最小。这是整个任务能成立的前提。所以骨架生成的本质不是压缩，而是把「高密度低体积」和「低密度高体积」两部分做物理分离。

再看排查者（不管是人还是 Agent）的实际行为：先看全局拓扑找异常点，再只对那一两个节点索取细节。全程需要完整 Payload 的节点通常不超过 5%。这就是一个典型的索引与内容分离 + 惰性加载结构，和 Git 的 `.idx` + packfile、LSM 的稀疏索引 + block fetch 是同一个东西。我打算直接照着这个结构做，下面的设计基本都是从这条类比推出来的。

从产出标准里我反推出四条隐含约束，它们才是设计的真正驱动力：

**「拓扑完整」不等于「一个节点都不能删」。** 可以删，但不能出现孤儿节点。删掉中间节点 B 时，B 的子节点必须重挂到最近的存活祖先上，并留下「此处折叠了几层」的痕迹。否则骨架会让人误以为调用链是扁平的——一个会撒谎的骨架比没有骨架更糟。

**「还原的 Payload 与原始数据完全一致」应该理解成字节级一致。** JSON 反序列化再序列化会丢空白、丢 Unicode 转义形式、丢数字的书写形态（`2.50` 变成 `2.5`）、静默吞掉重复 key。任何「解析成对象 → 存起来 → 展开时再序列化」的做法都过不了这一关。这条直接决定了实现路线，见第五节。

**配置要动态生效，意味着规则引擎必须是数据驱动的**，不能把规则硬编码进代码分支，规则的匹配语义、优先级、冲突处理本身也得是可测的。

**骨架的最终读者是 LLM，不是人。** 所以序列化形式要按 token 效率优化而不是可读性优化。同样的信息，缩进树形文本比 JSON 省 40%~60% 的 token（引号、花括号、重复的 key 名全是纯开销）。这点 Issue 没提，但它是「不撑爆上下文」这个目标的直接组成部分。

范围上，我只做「已落盘的 Trace 文件 → 骨架 → 按需展开」这条离线流水线。埋点采集交给 MLflow/OTel SDK，可视化 UI、在线流式处理、存储后端都不在范围内。定位是一个无状态的命令行数据加工工具。

---

## 二、数据源选型：MLflow 还是 OpenTelemetry

先说一个容易被忽略的事实：这两者不是竞争关系，是包含关系。MLflow Tracing 是构建在 OTel SDK 之上的，它的 Span 就是 OTel Span，GenAI 特有的那些语义是以 OTel attribute 的形式承载的——span 类型放在 `mlflow.spanType`，输入输出放在 `mlflow.spanInputs` / `mlflow.spanOutputs`，拓扑直接用 OTel 原生的 `trace_id` / `span_id` / `parent_span_id`。（具体 key 名我会在第一周拿真实样本对着实际 MLflow 版本核一遍，这块上游迭代比较快。）

这个事实决定了选型结论：**以 OTel 语义模型作为内部的规范化 IR，MLflow 作为一等 ingest 适配器，两边都支持。**

依据有三条。

正确性上，既然 MLflow Span 本来就是 OTel Span，那 MLflow 数据可以无损映射到 OTel 模型；反过来则不成立，一个任意的 OTel Trace 填不满 MLflow 的语义槽位。OTel 是两者的上界，选它做 IR 不丢信息。

长期演进上，openEuler 作为基础软件社区，组件应该对齐开放标准而不是绑定单一上游项目。以 OTel 为 IR，将来接 Langfuse、Phoenix、Jaeger 任意 OTLP 兼容源都只需新增一个 Reader，剪枝和索引逻辑零改动。

但短期可用性上有个问题：任务要求的「节点类型 Model/Tool」，在纯 OTel 下并不可靠——OTel 的 GenAI 语义约定（`gen_ai.*`）目前仍是 Experimental 状态，各框架落地不一致。而 MLflow 的 `mlflow.spanType` 是现成且明确的。所以 IR 里的 `SpanKind` 用分级推断：优先读 `mlflow.spanType`，其次 OTel GenAI 约定，再次 OpenInference 之类的第三方约定，最后才回退到 span name 的正则启发式，实在推不出来就如实标 Unknown。

这里有个我认为必须坚持的原则：**推断结果要携带来源等级**（`Explicit / Convention / Heuristic / Unknown`），骨架里对启发式推断出的类型打标记。因为下游是 LLM，如果把猜测伪装成事实，Agent 会基于错误的类型判断走错排查方向，而且错得毫无征兆。不知道就说不知道，这比猜一个好看的答案有价值。

---

## 三、技术栈：Python（写定）

**技术栈调整为 Python。** 理由有三条。

第一，仓库与生态一致性。agentic-engineering-team 是 Python 生态的仓库，MLflow、OTel SDK、LangGraph 全是 Python 原生，评审者和后续维护者也是 Python 背景——工具与被观测对象同栈，贡献门槛最低，这在社区项目里是压倒性的考量。

第二，工期效率。四周窗口内，Python 直接砍掉编译、交叉编译、静态链接、CI 工具链搭建这一整层成本；fixture 采集脚本（LangGraph demo agent）本来就要用 Python 写，现在工具和脚本共用一套环境。

第三，集成路径更顺。MCP 有官方 Python SDK，后续把工具暴露成 Agent 原生工具只是加一个模块的事；Python 侧使用者 `pip install` 即用，不存在跨语言绑定问题。

**关键澄清：换语言不换架构。** 骨架 + 字节偏移索引这套设计的「构造性正确」与语言无关——expand 依旧是对原文件做字节区间切片，不经过任何解析与再序列化。改变的只有一件事：**怎么拿到每个 span 的字节偏移**。Rust 版靠 `RawValue` 的零拷贝借用 + 指针算术；Python 的 `json.loads` 不暴露字节位置，所以原方案里作为兜底的「自写轻量扫描器」在这里升级为主路线：一个只识别 span 边界、不解析值的字节级状态机（约 150 行，设计见 5.2）。

同时诚实列出 Python 要付的三笔代价和对应处理：

一是字节一致必须绕开 `json.loads` 往返。Python 的 `json.dumps` 默认 `ensure_ascii=True` 会把中文转成 `\uXXXX`，浮点书写形态和键间空白会丢，重复 key 被静默吞掉——所以数据通路上**原始字节从不经过 loads→dumps 往返**：偏移由扫描器给出，展开就是 `mm[off:off+len]` 切片；`loads` 只用于「解析单个 span 提取元数据、用完即弃」这一条只读路径。

二是性能上界低于 Rust。缓解手段是让热路径全部落在 C 实现上：`mmap` 只读映射避免整文件读入；扫描器不逐字节循环，字符串内部用 `bytes.find(b'"')` 快进、结构判定用 `re.finditer` 批量定位（两者都是 C 速度，Python 层只处理稀疏的结构事件）；堆内存与最大单个 span 成正比而不是与文件成正比。性能验收指标按语言重新校定（见六），并在自验报告里如实标注。

三是部署不再是单文件静态二进制。openEuler 自带 python3，所以基本盘是 `pipx install` / `uv tool install` 一步可用；对确需单文件分发的场景，用 `shiv` 打一个 zipapp 兜底。这确实不如 Rust 的静态二进制干净，作为已知取舍写进第九节。

**依赖清单**（运行时压到最少，每一项都要能单独说清理由）：

标准库承担绝大部分：`json`、`mmap`、`argparse`（CLI）、`hashlib`（blake2b 摘要）、`fnmatch`（span name 通配）、`tomllib`（配置解析，3.11+）、`struct`（索引编解码）、`bisect`（索引二分）、`dataclasses`、`logging`、`unicodedata`。

第三方运行时依赖只有一个：`pydantic` v2——规则与配置的 schema 校验（「配置非法 → 退出码 4」的实现基础），MIT 许可证与 Mulan PSL v2 兼容，且本身就是 agentic 生态的事实标准。

可选 extras：`[tokens]` = tiktoken（精确 token 计数）；`[speed]` = orjson（解析加速备用）；`[grapheme]` = regex（`\X` 匹配 grapheme cluster）。开发依赖：pytest、hypothesis、syrupy、pytest-benchmark、pytest-cov、ruff、mypy。

Python 版本对齐 3.11+（`tomllib` 进标准库的版本），第一周核对 openEuler 发行版自带版本，若需向下兼容则退回 `tomli`。

代码约束在独立顶层目录 `tools/tracelens/`，标准 `pyproject.toml`（PEP 621）+ `src/` 布局，`console_scripts` 暴露 `tracelens` 命令。

---

## 四、业界参考：Claude Code / OpenCode / OpenHands 的上下文裁剪

这三个是当前上下文管理做得最工程化的 Agent 系统，它们处理的是同一个物理约束（上下文有限、工作量无限），值得先看清它们怎么做、哪些可以直接借鉴、哪里是本方案该占的空位。

**Claude Code。** 核心是 auto-compact：接近窗口上限时，把旧对话历史总结成结构化摘要并替换原文，有效窗口会预留摘要的输出空间；`/compact` 支持手动触发并附带「要保留什么」的指令。在此之外还有一组配套机制：microcompact（不调用 LLM 的内联清理，优先清掉旧的工具输出）、压缩豁免的持久记忆（CLAUDE.md 里的项目规则不随对话被压掉）、以及用子代理（subagent）把大体积文件读取隔离在独立上下文里、只把摘要和少量元数据带回主上下文。

**OpenCode。** 最有借鉴价值的是它把**存储历史与模型上下文分离**：完整会话历史持久化保存，模型每次只拿到一个整理过的切片，压缩与剪除都不删除历史。触发上，每次模型调用前用约 4 字符/token 粗估请求体积，估算超过「上限 − max(输出预算, 缓冲)」即压缩；压缩产物是结构化摘要（Goal / Constraints / Progress / Key Decisions / Next Steps / Critical Context 模板）加上最近内容的尾部。工具输出走单独的剪除通道：保留调用记录，把不再需要的大输出替换成占位符。

**OpenHands。** 架构上是事件存储（event store）而非消息数组，上下文裁剪抽象成可插拔的 condenser：默认的 LLMSummarizingCondenser 在历史超过阈值时，保留最近消息原样、保留最早的 keep_first 条事件（系统提示与初始任务），把中段旧内容替换成 LLM 生成的摘要；另有只留近期事件等更激进的策略。官方评测显示与不压缩基线相比任务成功率基本持平、时延与成本显著下降。

从中提炼四条共性模式，以及各自在本方案里的落点：

**其一，全量存档 + 精简视图。** 三家都不真删数据：完整历史在存储层，模型看到的是视图。本方案同构：原始 trace 文件是存档，骨架是视图，偏移索引是两者之间的桥。

**其二，关键内容保全 + 旧噪声降级。** keep_first 保初始任务、近期消息保原样、CLAUDE.md 压缩豁免——「有些东西永远不能被裁掉」是共识。落到本方案：默认规则集内置「根节点与 ERROR 节点到根的整条路径永不剪除」，这条硬保护有了业界佐证。

**其三，占位符化的输出剪除。** OpenCode 把大工具输出换成占位符、保留调用记录——这几乎就是本方案 `__truncated__` 标记的同构物。差别在可逆性：它们的占位符只是「此处删过东西」的痕迹，本方案的占位符携带 `expand_hint`，是一条可执行的精确取回路径。

**其四，预算感知触发。** 用粗粒度 token 估算对着阈值做决策（4 字符/token 就够用），是三家共同的工程判断。直接借鉴为一个新特性：`skeleton --max-tokens N`——给定预算，渲染前估算骨架体积，超预算则按既定次序逐级增压（先收紧截断阈值 → 再折叠同类兄弟 → 再压缩展示深度），直到达标或触及保底集（根 + ERROR 路径），估算方法同样用 chars/ratio 先行、`[tokens]` extra 精确化。

最后是一条**本质差异**，也是本方案价值主张的业界坐标：compaction / condenser 全部是**有损、不可逆、由 LLM 生成**的——摘要本身可能出错，也可能恰好把根因摘掉，且事后无法回头；它们服务的场景是「正在进行的会话」，丢一些细节换继续工作是划算的。而事后排查恰恰相反：证据不能有损。本方案的骨架 + 偏移索引是**确定性、无损、可精确回源、零 LLM 参与**的——裁掉的每一个字节都能凭 span_id 原样取回。两类技术是互补关系，本工具补的是「事后取证」这一侧的空位；自验报告会把这个定性对比写进局限性与定位说明。

参考资料：
- Claude Code 上下文窗口与压缩：https://code.claude.com/docs/en/context-window
- Claude 平台侧 compaction API：https://platform.claude.com/docs/en/build-with-claude/compaction
- OpenCode compaction 文档：https://v2.opencode.ai/docs/compaction/
- OpenCode 上下文管理源码解读：https://deepwiki.com/sst/opencode/2.4-context-management-and-compaction
- OpenHands Context Condenser：https://docs.openhands.dev/sdk/guides/context-condenser
- OpenHands condensation 博客：https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents

---

## 五、开发思路

### 5.1 数据流与模块划分

```
raw trace.json (mmap 只读，全程按 bytes 记账)
      │
      ├─► ingest.sniff    读头部若干 KB，判定 MLflow / OTLP，定位 spans 数组位置
      ├─► ingest.scanner  字节级扫描（find + re.finditer，C 速度）
      │        输出 Iterator[(span_start, span_end)]  ← 每个 span 对象的字节区间
      ├─► ingest.adapters 逐 span json.loads(mm[s:e]) → 抽元数据 → SpanMeta（用后即弃）
      │
      ├─► prune  (规则匹配 → keep / drop / collapse / truncate，拓扑重建)
      │
      ├─► render ──► skeleton.txt / .json / .md   (~KB，给 Agent 读)
      └─► index  ──► trace.idx  (struct 编码，span_id → offset,len)
                          │
                          └─► expand: mm[off:off+len] ──► 原样字节，逐字节一致
```

包结构（`src/tracelens/`）：`model.py`（dataclass 定义的 IR，零内部依赖）；`ingest/`（`scanner.py`、`sniff.py`、`mlflow.py`、`otlp.py`）；`prune/`（`rules.py`、`engine.py`、`topology.py`、`truncate.py`、`paths.py`）；`index/`（`format.py`、`reader.py`）；`render/`（`tree.py`、`jsonout.py`、`md.py`）；`config.py`；`cli.py`；`testkit.py`（合成 Trace 生成器，供 hypothesis 与基准用）。依赖纪律与 Rust 版的 workspace 完全一致：所有模块只单向依赖 `model`，彼此不依赖，`cli` 负责组装——将来 `mcp_server.py` 与 `cli.py` 平行放置，核心零改动。

### 5.2 索引与扫描器：整个方案的核心

三条候选路线的取舍不变：解析后另存过不了字节一致，另存原文空间翻倍，**只存偏移索引、expand 回原文件切片**是唯一让一致性「构造上成立」的做法——expand 执行的就是 `mm[off:off+len]`，中间没有解析与再序列化环节，想违反一致性都没有代码路径。`--detach` 仍作为文件会被轮转场景下的备选模式。

Python 下的新问题是偏移从哪来。`json.loads` 不暴露字节位置，且 **str 的索引是码点不是字节**（`len("中")==1` 但占 3 字节），所以整条通路的记账单位强制为 bytes，绝不混用 str 偏移。方案是一个约 150 行的字节级扫描器：

工作方式分两种模式交替。**字符串外**：用 `re.finditer(rb'["\{\}\[\]]', mm)` 拿到稀疏的结构字符事件流，维护 `depth` 与当前容器类型，在目标 spans 数组层级上，对每个元素对象记录 `{` 的进入位置与配对 `}` 的退出位置，即得 `(span_start, span_end)`。**字符串内**：不逐事件处理（payload 里未转义的花括号会造成海量无意义事件），改用 `mm.find(b'"', pos)` 直接快进到下一个引号，命中后向前回看连续反斜杠的奇偶判断是否转义——`find` 是 C 实现，字符串越大跳得越快，这保证了大 payload 场景的吞吐。两个模式都不解析任何值。

正确性验证不依赖信任：hypothesis 生成随机 JSON 文档（含转义引号、嵌套容器、Unicode、`\uXXXX`、紧凑与 pretty 两种排版），断言「扫描器给出的每个区间，`json.loads(mm[s:e])` 成功且与全量解析得到的对应元素相等」。这是 Python 版技术不确定性最高的一点（对应 Rust 版的 RawValue spike），放在第一阶段最先做，同时用微基准验证吞吐达标。

索引文件格式沿用：`struct` 小端编码，magic `b"TLNS"` + version(`H`) + `blake2b-256(原文件)`(32 字节) + entry_count(`I`) + 按 span_id 排序的定长 entry 数组（span_id 原始 8 字节 + offset `Q` + len `Q` + flags `H`）。读侧整表载入（KB 级）后 `bisect` 二分，支持短前缀匹配（同 Git short hash）。存原文件摘要是为了在文件被改动或轮转后让 expand 明确报错（退出码 3），而不是静默返回一段错位的垃圾数据——宁可失败也不能给 Agent 喂错数据。

### 5.3 剪枝与拓扑保持

算法与不变量完全沿用（语言无关）。先对每个 span 按规则求出动作（Keep / Drop / CollapseSubtree / Truncate）；对每个 Keep 节点把到根的祖先链强制 Keep（除非显式 collapse_subtree）；对 Drop 节点把子节点重挂到最近存活祖先并在边上记 `elided_depth`；最后把兄弟中同类连续被 Drop 的节点合并成占位节点，形如 `{ elided: 17, kind: "Tool", all_ok: true }`。

五条不变量用 hypothesis 钉死：输出是合法森林（无环、单亲、根可达）；原树中的祖先关系在存活节点间单调保持；存活节点的相对深度顺序不变；无孤儿；`elided_depth` 之和加存活节点数等于原节点数。其中「祖先关系单调保持」是核心——它保证 Agent 在骨架上读到的因果关系在真实执行中一定成立。默认规则集内置硬保护：根节点与 ERROR 节点到根的整条路径永不剪除（第四节业界共识的落地）。

### 5.4 截断

三种策略：Head（默认，prompt 的系统指令在开头）、Tail（错误栈的根因在结尾）、HeadTail（工具输出，兼顾格式和结论）。截断只发生在骨架视图里，**原文件从不被修改**。

实现上：对目标字段的值先 `decode("utf-8")` 到 str 再按字符数截断——在 str 层操作天然避开「把多字节字符劈成两半」的问题（这是 Python 相对 Rust 按字节切片的一个便利面）；grapheme cluster 仍是已知边界，emoji ZWJ 序列按字符截断可能破坏语义，默认按字符，`--strict-grapheme` 时引入 `regex` 的 `\X` 按 grapheme 截。中文 + emoji + 组合字符各写专门用例。

截断标记自描述且可寻址，结构不变：

```json
{
  "content": "根据检索结果，2025年Q3的营收为……",
  "__truncated__": {
    "span_id": "a3f2c1d4e5b60789",
    "field": "$.outputs.content",
    "original_chars": 48213,
    "kept_chars": 200,
    "strategy": "head_tail",
    "digest": "blake2b:1f4a...",
    "expand_hint": "tracelens expand --span-id a3f2c1 --field '$.outputs.content'"
  }
}
```

`expand_hint` 把「怎么取回」的知识直接写进给 LLM 的数据里，Agent 不需要额外的 system prompt 教它怎么展开。摘要算法从 blake3 换成标准库 `hashlib.blake2b`（digest_size=32）：零依赖、比 sha256 快，完整性校验强度足够。

Token 计数默认用 `chars / chars_per_token`（可配，英文默认 4.0，中文约 1.5，与 OpenCode 的 4 字符/token 估算是同一工程判断），零依赖且确定性；装了 `[tokens]` extra 时用 tiktoken 精确算。骨架末尾的预算统计显式标注估算方法。

### 5.5 骨架渲染

同一份骨架三种形式。默认 `tree`，给 LLM 看，token 效率最高：

```
trace 7f3a2b1c  status=ERROR  spans=412→38  dur=48.2s
└─ a3f2c1 AGENT  research_agent            OK    48.2s
   ├─ b4e1d2 MODEL  gpt-4o                 OK     2.1s  in=8.2K out=412
   ├─ c5f2e3 TOOL   web_search             OK     3.4s  in=64   out=182K ✂
   │  └─ ⋯ elided 17 similar TOOL spans (all OK)
   ├─ d6a3f4 TOOL   sql_query           ERROR     0.2s  in=210  out=1.1K ✂
   │     └─ error: relation "revenue_q3" does not exist
   └─ e7b4a5 MODEL  gpt-4o                 OK     4.8s  in=194K out=88   ⚠ ctx-heavy
```

`json` 给程序消费（稳定 schema、可 diff），`md` 给人看、可直接贴进 PR。`✂` 标截断、`⚠` 标启发式判定的可疑点、`⋯` 标折叠——每个符号占 1 token 但信息量很大。

新增 `--max-tokens N`（第四节的直接借鉴）：渲染前估算骨架 token 体积，超预算则按既定次序逐级增压——先收紧截断阈值，再折叠同类兄弟，再压缩展示深度——直到达标或触及保底集（根 + ERROR 路径不可再剪）。这让「骨架一定装得进指定预算」成为可承诺的性质，Agent harness 可以放心把它接进固定大小的上下文槽位。

### 5.6 CLI 与配置

```bash
# 生成骨架 + 索引
tracelens skeleton --input trace.json --config rules.toml \
    --format tree --max-tokens 4000 --out skeleton.txt --emit-index trace.idx

# 按需展开（span_id 支持短前缀；--field 定位到字段级；--raw 输出原始字节）
tracelens expand --input trace.json --index trace.idx \
    --span-id a3f2c1 [--field '$.outputs.content'] [--raw]

# 辅助：格式嗅探与 Trace 统计
tracelens inspect --input trace.json
```

字段级展开的一个设计澄清：`--field` 不是「解析整个 span 再把字段值重新序列化」——那会破坏字节承诺。实现是**展开时对该 span 的切片做一次带路径追踪的二次扫描**，定位目标字段值自身的字节区间，返回的仍是原文件字节。单个 span 的二次扫描是毫秒级开销，换来字段级同样成立的字节一致性。

配置合并在 `config.py` 手写四层叠加（内置默认 < TOML 文件 < 环境变量 `TRACELENS_*` < CLI 参数），最终交给 pydantic 模型校验——临时调试用 CLI 覆盖一切，CI 用环境变量，团队约定沉淀在配置文件。Issue 要求的「配置动态调整」由此天然满足：规则是数据不是代码，改配置重跑即可。

退出码语义写死并纳入测试：0 成功；1 输入不存在或解析失败；2 `span_id` 未命中；3 索引与原文件不匹配（摘要校验失败）；4 配置非法（pydantic 校验错误的出口）。明确的退出码是给上层脚本和 Agent harness 用的——它们必须能程序化地区分「没找到」和「数据坏了」。

---

## 六、测试方案

按 Issue 的三类覆盖要求（骨架生成 / 按需展开 / 配置动态调整）分层落实，全部跑在 pytest 之上。

单元测试盯住最危险的点：扫描器的边界行为——转义引号（`\"` 与 `\\"` 的奇偶）、字符串内的花括号、嵌套容器、`\uXXXX`、紧凑与 pretty 排版、空数组与单元素数组。属性测试用 hypothesis 承担两件大事：其一，扫描器交叉验证——随机 JSON 上「每个区间 `json.loads` 成功且与全量解析一致」；其二，五条拓扑不变量与**字节级往返**——对 testkit 合成的任意 Trace，`expand(span_id)` 输出与原文件对应切片逐字节相等（hypothesis 失败时自动 shrink 到最小反例并存入回归库）。快照测试用 syrupy 覆盖 tree / json / md 三种渲染，防止输出格式静默漂移。配置测试对应 Issue 第三条：同一 Trace 在不同规则集下的骨架必须体现规则差异，冲突按声明的优先级处理，非法配置以退出码 4 明确失败。

性能基准用 pytest-benchmark + testkit 合成大文件，**指标按 Python 重新校定并作为验收线**：扫描吞吐 ≥ 200 MB/s；1 GB Trace 全流程（扫描 + 元数据 + 剪枝 + 渲染 + 建索引）≤ 120 秒；堆内存峰值 ≤ max(256 MB, 2 × 最大单 span 解析产物)——堆占用与最大单 span 成正比而非与文件成正比，这是 mmap + 逐 span 即弃路线要验证的核心性质。（Rust 版写过 RSS < 50 MB，那个指标对 Python 不现实，如实改，自验报告同步注明。）UTF-8 用例组：中文、emoji、组合字符的截断不得产生非法输出。

---

## 七、自验报告计划

报告围绕三块实测数据组织。

**数据量与窗口占用。** demo 用的 Trace 来自一个故意植入 bug 的 LangGraph Agent（bug：SQL 表名写错，错误被工具层吞掉后当作正常字符串进入后续 prompt）。以目前 fixture 的量级估算：412 个 span 剪到 38 个存活节点，骨架体积约为原文件的 0.2%。不过结论我不打算用「压缩了 99.8%」来表述，那是压缩算法的语言。真正的价值命题是：**128K 窗口下，原始 Trace 只能塞进 4%，而且 Agent 无法选择塞哪 4%；用了这个工具之后，Agent 拿到 100% 的拓扑加上自己选定的关键细节，只占 5% 的窗口。**

**成功定位问题的案例。** 完整复现上面那个场景：Agent 读骨架一眼看到唯一的 ERROR 节点 `d6a3f4 TOOL sql_query`，且父节点状态是 OK，判断出错误被吞掉了；发起 expand 拿到完整 Payload 看到 `relation "revenue_q3" does not exist`；再 expand 下游 MODEL span 确认该错误字符串被当作数据进了 prompt；给出根因。

**对照组。** 不用工具，直接把 Trace 前 128K token 喂给同一个模型，展示它为什么定位失败（前 128K 全是第一轮的 prompt，根本走不到 `sql_query`）。没有对照组就说不清是工具起了作用还是模型本身能猜出来。

报告的定位说明里会附上与 compaction / condenser 类方案的定性对比（第四节的差异分析）：它们有损且服务于进行中的会话，本工具无损且服务于事后取证，互补而非竞争。局限性说明单独成节，见第九节。

---

## 八、时间计划与分阶段实现步骤

总窗口保持 7/18–8/15、每周约 40 小时不变。语言切换发生在 7/23，此前完成的 fixture 采集与 MLflow 字段核对成果直接沿用；第 1 阶段按剩余时间（7/23–7/25）重排为 Python 地基，后三周日期不动。节奏仍是按周提交增量 PR，每周同步一次进度（带 demo 或测试报告），前三个 PR 各自独立可评审。

### M1　地基（7/23–7/25）——目标：解析层可信，最大不确定性出清

**1.1 项目骨架。** 建 `tools/tracelens/`：`pyproject.toml`（PEP 621 元数据、`console_scripts` 注册 `tracelens`、extras 定义 `[tokens]/[speed]/[grapheme]`）、`src/` 布局、ruff + mypy + pytest 配置、CI 骨架（openEuler 容器镜像 × Python 3.11/3.12 矩阵，先只跑 lint + 单测）。
验收：干净环境里 `pipx install .`（或 `uv tool install .`）后 `tracelens --help` 正常输出。

**1.2 扫描器 spike → 定稿（`ingest/scanner.py`）。** 这是 Python 版风险最高的一步，最先做。实现 5.2 的双模式状态机，对外 API 定为 `iter_object_ranges(buf, array_start) -> Iterator[tuple[int, int]]`；同时写一个 20 行的微基准脚本，用 testkit 生成 500 MB 合成 Trace 实测吞吐。
验收：吞吐 ≥ 200 MB/s；边界用例（转义引号、字符串内花括号、`\uXXXX`、紧凑/pretty）全过。若吞吐不达标，当场启动兜底评估（见本节末风险）。

**1.3 IR 定义（`model.py`）。** `SpanMeta`（span_id、parent_id、name、kind、kind_source、status、start/end、in/out 字节数、raw_range）、`SpanKind`、`KindSource`、`Status` 等 dataclass（`slots=True`），零内部依赖。
验收：mypy 严格模式通过。

**1.4 格式嗅探（`ingest/sniff.py`）。** 读文件头部若干 KB，按顶层结构特征判定 MLflow / OTLP / 未知，并返回 spans 数组的定位信息（MLflow 与 OTLP 的嵌套路径不同，嗅探结果直接喂给扫描器的 `array_start`）。
验收：两类 fixture 与三个坏输入（空文件、非 JSON、无 spans）都给出正确判定或明确报错。

**1.5 双适配器（`ingest/mlflow.py` / `ingest/otlp.py`）。** 逐 span `json.loads(mm[s:e])` 抽元数据后即弃；字段抽取走 `FIELD_MAP` 配置表而非硬编码（R2 的缓解落点）；`SpanKind` 分级推断 + `KindSource` 标记在这里实现。本步同时用真实样本最终锁定 MLflow 的 attribute key。
验收：两种格式的 fixture 都解析出正确的 span 森林与字节区间，`pytest tests/test_ingest.py` 全绿。

**1.6 fixture 补齐。** 沿用已采集样本，补一份 OTLP file exporter 导出样本；带 bug 的 LangGraph Agent 脚本入库 `fixtures/`。

**M1 交付：PR#1**（scanner + sniff + model + 双适配器 + fixtures + CI 骨架），附微基准数据。

### M2　剪枝引擎（7/26–8/2）——目标：五条不变量全绿

**2.1 规则模型（`prune/rules.py`）。** pydantic 定义 `Rule{match{name_glob, kind, status, min_bytes, depth…}, action, params, priority}` 与 `RuleSet`；`tomllib` 加载 + 校验失败 → 退出码 4 的错误类型。
**2.2 求解器（`prune/engine.py`）。** 规则按 priority 排序、首匹配语义、冲突处理；内置默认规则集，硬保护（根 + ERROR 路径永不剪）写死在引擎而不是默认规则里——用户配置无法关掉它。
**2.3 拓扑重建（`prune/topology.py`）。** 四步算法照 5.3 实现：force-keep 祖先链 → 重挂 + `elided_depth` → 兄弟同类合并占位。
**2.4 截断（`prune/truncate.py` + `prune/paths.py`）。** 三策略、`__truncated__` 标记（blake2b 摘要 + expand_hint）；`paths.py` 实现 `$.a.b[0].c` 点路径子集的定位器（截断与 `--field` 共用）。
**2.5 测试基建（`testkit.py`）。** hypothesis 策略：随机 span 森林生成器（可控深度/宽度/payload 大小/错误注入）× 随机规则集生成器；五条不变量测试落地；UTF-8/emoji/组合字符截断用例。
验收：`pytest -k "invariant or truncate"` 多轮（不同 hypothesis 种子）全绿。

**M2 交付：PR#2**（prune 全模块 + testkit + 不变量测试报告）。

### M3　索引、展开、渲染、CLI（8/3–8/9）——目标：全链路可用，字节承诺闭环

**3.1 索引编解码（`index/format.py`）。** struct 布局照 5.2；写入前按 span_id 排序；魔数与版本校验。
**3.2 读路径（`index/reader.py`）。** 整表载入 + bisect 二分 + 短前缀解析（歧义前缀报多候选）；blake2b 全文件校验，失配 → 退出码 3。
**3.3 expand。** 整 span：`mm[off:off+len]` 原样输出；`--field`：对该 span 切片做带路径追踪的二次扫描，返回字段值的原始字节（见 5.6 的设计澄清）；`--detach` 模式的物化与读取。
**3.4 渲染（`render/`）。** tree / json / md 三形态；`--max-tokens` 预算收紧循环（估算 → 增压 → 复检，保底集不可再剪）。
**3.5 CLI 与配置（`cli.py` / `config.py`）。** argparse 三个子命令、四层配置叠加 + pydantic 校验、退出码全表落地。
**3.6 端到端验证。** hypothesis 字节级往返（任意合成 Trace：`expand(id) == 原切片`）；1 GB 基准跑六节的三条指标；`json.loads` 对 expand 输出做合法性交叉验证。
验收：往返全绿、基准达标、`skeleton | expand | inspect` 三命令全链路手工走查通过。

**M3 交付：PR#3**（index + render + cli + 基准报告），此时工具完整可用。

### M4　自验与收尾（8/10–8/15）

**4.1** demo 脚本：LangGraph Agent 读骨架 → 定位可疑 span → 发 expand → 给根因的完整闭环，记录每步的数据量。**4.2** 对照组脚本与结果留档。**4.3** 自验报告成文：三块实测数据填入 + 与 compaction 类方案的定性对比 + 局限性。**4.4** README、架构文档、配置样例（含一份注释完整的 `rules.toml`）。**4.5** CI 完整化：openEuler x86_64 / aarch64 容器矩阵（Python 无需交叉编译，矩阵只为验证两种架构的运行时行为一致）、覆盖率 ≥ 85%。**4.6** 打包：`uv build` 出 wheel；`shiv` 出单文件 zipapp 作为分发兜底。

**M4 交付：最终 PR + 自验报告。**

**如果提前完成**，按优先级做：`mcp_server.py`（官方 MCP Python SDK，暴露 `get_skeleton` / `expand_span` 两个 tool——Python 下这一步比原计划更近，是这个工具最自然的形态）；`tracelens diff traceA traceB`（「上次好的这次坏了」是最常见的排查入口）；分级骨架（`--detail low|high`，同一索引出多档视图）。

**主要风险**（按语言切换后重估）。R1'：扫描器吞吐不达标，概率低到中——1.2 的 spike 前置暴露；兜底路径按序为：`[speed]` extra 用 orjson 承担元数据解析、扫描器仅做边界；仍不够则接受「一次性 `json.loads` 全文件拿结构 + 扫描器只补偏移」的双通道（吞吐换实现简单，内存指标相应放宽并如实标注）。R2：MLflow attribute key 与预期不符，概率中——已在 1.5 用真实样本锁定，`FIELD_MAP` 配置表让修正只动一行配置。R3：依赖审查阻力，概率低——运行时第三方仅 pydantic（MIT，与 Mulan PSL v2 兼容），extras 全部可选。

---

## 九、局限性与改进方向

当前范围内的已知局限，写清楚比藏着好。

一是 expand 依赖原始文件在场且未被修改；会被轮转的场景要在生成骨架时用 `--detach` 提前物化被剪内容，代价是空间接近翻倍。二是性能上界低于同架构的 Rust 实现；缓解后的指标见第六节，若未来出现「单机批量处理数百 GB Trace」这类需求，热点扫描器可单点替换为 Rust 扩展（maturin 构建，接口不变，其余 Python 代码零改动）——分层设计为这条路留了口。三是 token 计数默认是估算值，只有 `[tokens]` extra 才精确，骨架里标注估算方法。四是 `SpanKind` 启发式推断可能出错，兜底是 `KindSource` 可信度标记而不是追求推断零错误。五是 grapheme cluster 默认不处理，`--strict-grapheme` 才引入 `regex`。六是定位为单机离线工具，不做流式摄入、在线服务和多 Trace 聚合分析。

改进方向与第八节的提前完成清单一致：MCP Server、`tracelens diff`、分级骨架；更远期的可能性是把扫描器下沉为 Rust 扩展以覆盖极端规模场景。

---

# 附录 A　关键设计决策与知识点问答

> 本附录把方案中的设计决策和涉及的概念整理成问答形式，每条按「结论 → 机制 → 例子」组织。一方面备评审时的追问，一方面作为自查清单。

## A. 设计决策类

**A1．为什么用字节偏移索引，而不是生成骨架时另存一份完整数据？**

结论：三条候选路线里，只有偏移索引让「字节级一致」成为构造上成立的性质，且空间开销不到 1%。
机制：候选一「解析成对象另存、展开时再序列化」，往返会丢空白、丢 Unicode 转义形式、丢数字书写形态（`2.50` → `2.5`）、静默吞掉重复 key，字节一致这关过不去。候选二「另存每个 span 的原文切片」能过关，但空间接近翻倍。候选三只存 `span_id → (offset, len)`，expand 就是 `mm[off:off+len]` 的字节切片，中间不存在解析与再序列化环节——想违反一致性都没有代码路径可走，测试只是加固，不是正确性的来源。
例子：Git 的 `.idx` 文件存 object id → packfile 内偏移，读对象时按偏移 seek 过去取；LSM-Tree 的稀疏索引 + block fetch 同理。本方案就是把 `trace.json` 当 packfile、`trace.idx` 当 `.idx`。

**A2．原始 Trace 文件被移动、轮转或修改了，expand 会怎样？**

结论：宁可明确失败，绝不静默返回错位数据；可预见的轮转场景用 `--detach` 提前物化。
机制：索引头部存 `blake2b(原文件)` 摘要，expand 前重新校验，不匹配立即以退出码 3 报错。下游读者是 Agent——喂给它一段「看起来是合法 JSON、实际是错位切片」的数据，它会一本正经地推理出错误结论，而且毫无征兆，这比直接报错危险得多。
例子：日志被 logrotate 轮转后所有 offset 失效，摘要校验直接拦下并给出可读错误；若预知文件会被轮转，生成骨架时加 `--detach`，把每个被剪 span 的原文单独存一份副本，用空间换可用性——类似 Git 把松散对象 repack 固化。

**A3．为什么以 OTel 做内部 IR，而不是直接用 MLflow 的格式？**

结论：两者是包含关系不是竞争关系；OTel 是语义上界，选它做 IR 不丢信息。
机制：MLflow Tracing 构建在 OTel SDK 之上，其 Span 就是 OTel Span，GenAI 语义以 attribute 承载（`mlflow.spanType` 等），拓扑用原生 `trace_id` / `span_id` / `parent_span_id`。因此 MLflow → OTel 可无损映射，反向不成立。以 OTel 为 IR，将来接任何 OTLP 兼容源（Langfuse、Phoenix、Jaeger）只需新增一个 Reader，剪枝、索引、渲染零改动。
例子：类比编译器架构——LLVM IR 是各语言前端的公共表示，优化器只面对 IR 而不关心源语言；这里 OTel IR 扮演同样角色，MLflow 和 OTLP JSON 是两个前端。

**A4．SpanKind 为什么要分级推断，还要携带 KindSource？**

结论：类型信息的来源可靠性参差不齐，必须把「有多确定」和「是什么」一起交付给下游。
机制：推断优先级为 `mlflow.spanType`（显式声明）> OTel GenAI 语义约定（`gen_ai.*`，仍处 Experimental）> OpenInference 等第三方约定 > span name 正则启发式 > Unknown。每个结果附带 `Explicit / Convention / Heuristic / Unknown` 来源等级，骨架对启发式结果打 `⚠` 标记。下游是 LLM，把猜测伪装成事实，Agent 会沿着错误的类型判断走错整条排查路线，且错得无征兆。
例子：一个叫 `search_tool` 的 span 被正则猜成 TOOL——多半是对的，但如果它实际是个子 Agent，读者会用「工具出错」的思路排查「模型出错」的问题。标了 Heuristic，Agent 至少知道这条推理链的地基是软的，必要时先 expand 验证类型。

**A5．剪枝为什么不能「过滤掉不匹配的、输出剩下的」？五条不变量各防什么？**

结论：朴素过滤会产生孤儿节点，让骨架谎报调用结构；五条不变量就是「骨架可信」的形式化定义。
机制：算法分四步——先按规则给每个 span 求动作（Keep / Drop / CollapseSubtree / Truncate）；再把每个 Keep 节点到根的祖先链强制 Keep；然后把 Drop 节点的子节点重挂到最近存活祖先并在边上记 `elided_depth`；最后把兄弟中同类连续被 Drop 的节点合并成占位节点。五条不变量：① 输出是合法森林（无环、单亲、根可达），防结构性损坏；② 祖先关系在存活节点间单调保持——核心，保证骨架上读到的因果关系在真实执行中必然成立；③ 存活节点相对深度顺序不变，防层级错乱；④ 无孤儿节点；⑤ `elided_depth` 之和加存活节点数等于原节点数——守恒律，防节点静默蒸发。
例子：调用链 A → B → C，规则删 B。朴素做法输出互不相连的 A 和 C，读者会以为 C 是顶层调用；正确做法把 C 挂到 A 下并在边上标 `elided_depth=1`，「A 间接导致 C」这条因果仍然成立。

**A6．为什么默认输出树形文本而不是 JSON？**

结论：骨架的读者是 LLM，序列化按 token 效率优化；树形文本比 JSON 省 40%~60% 的 token。
机制：JSON 的引号、花括号、每个对象里重复出现的 key 名，对 LLM 都是纯开销；缩进树用相对位置表达父子关系，用 `✂`（截断）`⚠`（可疑）`⋯`（折叠）这类单 token 符号承载元信息。`json` 格式保留给程序消费（schema 稳定、可 diff），`md` 给人看、可直接贴 PR。
例子：同样 38 个存活节点的骨架，JSON 形态可能 8K token，tree 形态 3K 出头——省下来的全是 Agent 的推理预算。

**A7．截断为什么分 Head / Tail / HeadTail？截断标记为什么要自描述？**

结论：不同字段的关键信息分布位置不同，策略要跟着信息分布走；自描述标记让 Agent 不需要外部教学就会取回全文。
机制：prompt 的系统指令在开头（Head 保头部），错误栈的根因在结尾（Tail 保尾部），工具输出常常开头是格式说明、结尾是结论（HeadTail 两头都保）。截断标记里带 `span_id`、点路径、原始/保留长度、策略名、blake2b 摘要，以及一条可直接执行的 `expand_hint` 命令。
例子：Agent 读到 `"expand_hint": "tracelens expand --span-id a3f2c1 --field '$.outputs.content'"`，照抄执行即可——这是 progressive disclosure（渐进式披露）的直接落地。OpenCode 对旧工具输出也做占位符化剪除，但它的占位符只是删除痕迹；本方案的占位符是一条可逆的取回路径，这是两者的关键差别。

**A8．怎么证明是工具起了作用，而不是模型本来就能定位问题？**

结论：必须设对照组，否则「成功定位」这个结论没有因果效力。
机制：实验组让 Agent 读骨架 → 定位可疑节点 → expand 取证 → 给出根因；对照组把原始 Trace 的前 128K token 直接喂给同一个模型做同一任务。两组之间唯一变量是「是否使用工具」，模型、任务、Trace 全部固定，结论才能归因到工具。
例子：植入的 bug 是 SQL 表名错误（`relation "revenue_q3" does not exist`）被工具层吞掉后混入后续 prompt。对照组的失败模式可以预判：前 128K 几乎全被第一轮的巨型 prompt 占满，`sql_query` span 根本进不了窗口——这个失败本身反向演示了任务动机。

**A9．为什么换成 Python 之后，核心架构可以不动？**

结论：「骨架 + 偏移索引 + 回源切片」的正确性建立在文件字节层，与实现语言无关；语言切换只影响「偏移怎么拿到」这一个环节。
机制：构造性正确的论证（A1）只依赖一件事：expand 是对原文件的字节区间拷贝。这个操作任何语言都能做。Rust 版用 `RawValue` 零拷贝借用 + 指针算术拿偏移；Python 的 `json.loads` 不暴露位置，于是 Rust 方案里的兜底路线——不解析值、只识别边界的字节级扫描器——升级为主路线。拓扑算法、不变量、索引格式、CLI 语义、测试性质全部原样平移。
例子：同一份 `trace.idx` 理论上可以被 Rust 版和 Python 版互读（格式是语言中立的 struct 布局）——这正是「架构与语言解耦」的直接证据。未来若需要极致吞吐，也只需把扫描器单点换成 Rust 扩展，其余不动。

**A10．这个工具和 Claude Code / OpenCode 的 compaction 有什么本质区别？**

结论：compaction / condenser 是有损、不可逆、由 LLM 生成的，服务于「进行中的会话」；本工具是确定性、无损、可精确回源、零 LLM 参与的，服务于「事后取证」。两者互补，不竞争。
机制：Claude Code 的 auto-compact 与 OpenHands 的 LLMSummarizingCondenser 都是让 LLM 把旧历史写成摘要替换原文——摘要本身可能出错，也可能恰好把根因摘掉，且替换后无法回头（OpenCode 虽保留完整历史存档，但模型上下文里的旧内容同样回不去了）。对进行中的会话这是划算的：丢些细节换继续工作。事后排查的约束正好相反：证据必须无损。本方案裁掉的每一个字节都能凭 `span_id` 原样取回，且骨架生成是纯确定性程序，不引入第二个会犯错的模型。
例子：设想根因藏在第 3 轮某个工具输出的最后一行。compaction 有一定概率在摘要时把它压缩成「工具返回了数据」——之后无论怎么问都找不回来；本方案的骨架至少保留该 span 的存在、状态与截断标记，一条 expand 就能拿回原文最后一行。共性借鉴与差异的完整分析见正文第四节。

## B. Python 与底层机制类

**B11．为什么 `json.loads` → `json.dumps` 往返不能保证字节一致？**

结论：JSON 解析是有损投影——文本形态里的多种信息在对象层不存在，往返必然重写。
机制：具体到 Python：`dumps` 默认 `ensure_ascii=True`，中文会被重写成 `\uXXXX`（反向也一样：原文里的 `\u4e2d` 会被 loads 解成字符、dumps 后书写形态改变）；键间与元素间的空白全部丢失；数字书写形态不保（`2.50` → `2.5`，`1e2` → `100.0`）；重复 key 被静默保留最后一个；NaN/Infinity 等非标准值处理因参数而异。这些每一条都足以让「与原始数据完全一致」失败。
例子：`json.dumps(json.loads('{"a": 2.50, "b": "中"}'))` 得到 `'{"a": 2.5, "b": "\\u4e2d"}'`——两处都变了。这就是数据通路上禁止 loads→dumps 往返、expand 必须走字节切片的直接原因。

**B12．字节级扫描器怎么设计？为什么它在 Python 里也够快？**

结论：双模式状态机——字符串外看结构字符、字符串内用 `find` 快进；Python 层只处理稀疏事件，重活全在 C 实现里。
机制：字符串外用 `re.finditer(rb'["\{\}\[\]]', mm)` 批量拿结构字符位置，维护深度与容器类型，在 spans 数组层级记录每个元素对象的进入/退出位置。进入字符串后切换模式：`mm.find(b'"', pos)` 直接跳到下一个引号（C 速度，跳过 payload 里未转义的花括号，避免海量无意义事件），命中后向前回看连续反斜杠的个数——奇数个说明这个引号被转义、仍在字符串里，偶数个才是字符串结束。全程不解析任何值。
例子：一个 100 MB 的 prompt 字符串，逐字节循环的纯 Python 状态机要按秒计；`find` 模式下它是一次（或转义引号数量次）C 级内存搜索，毫秒级掠过。这就是「热路径落在 C 上」的含义——Python 只当调度员，不当搬运工。

**B13．Python 的 mmap 怎么用？str 与 bytes 的偏移陷阱是什么？**

结论：标准库 `mmap` 提供与 Rust `memmap2` 等价的只读映射；而 Python 特有的坑是 str 索引按码点计数，与字节偏移不可混用。
机制：`mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)` 建立映射，对象支持切片与 buffer 协议，`re` 和 `find` 可直接在其上工作，物理内存随访问按页加载。偏移陷阱：`len("中") == 1` 但 UTF-8 占 3 字节——如果任何环节用 `str.find` 的结果当字节偏移写进索引，expand 会切出错位垃圾。因此项目纪律是：偏移记账一律 bytes，str 只出现在「单字段截断」这种局部只读操作里，产物不回写偏移。
例子：`"中文abc".find("a") == 2`（码点），但它在 UTF-8 字节流里的偏移是 6。测试里专门有一组「多字节字符前置」的用例防这类回归。风险面同 Rust 版：映射期间文件被外部 truncate 会触发总线错误，靠摘要校验 + 只读策略 + 明确报错兜住。

**B14．为什么用 `hashlib.blake2b` 而不是 blake3 或 SHA-256？**

结论：标准库里最合适的选择——比 SHA-256 快、零第三方依赖，完整性校验强度绰绰有余。
机制：BLAKE2b 是 RFC 7693 标准化的现代哈希，软件实现速度显著快于无硬件加速的 SHA-256；Python 的 `hashlib` 原生内置，`digest_size=32` 得到 256 位摘要。blake3 更快，但在 Python 里要引入带原生扩展的第三方包——为一个非瓶颈环节增加一个二进制依赖，不符合依赖最小化原则。
例子：本工具里摘要出现在两处——索引头的全文件校验、截断标记里的字段摘要。前者每次 expand 跑一遍，1 GB 文件 blake2b 亚秒级完成，不构成体验瓶颈；若未来实测成为瓶颈，`[speed]` extra 里再考虑 blake3 不迟。

**B15．hypothesis 是什么？shrinking 怎么工作？**

结论：Python 的属性测试框架（对应 Rust 的 proptest）——验证「对生成器覆盖的所有输入，性质恒成立」，失败时自动把输入缩到最小复现。
机制：用 `@given` + 策略（strategy）描述输入空间（例如「任意合法 span 森林 × 任意规则集」，用 `st.recursive`/`st.builds` 组合），框架随机生成大量输入检查性质（五条不变量、字节级往返、扫描器交叉验证）；一旦失败，hypothesis 沿策略结构反向简化输入给出最小反例，并把反例写入本地数据库，之后每次测试优先重放，防止回归。
例子：手写用例很难想到「兄弟节点中第 1、3、5 个被 collapse、中间夹一个 ERROR」这种组合；hypothesis 会撞上它，并把一棵 400 节点的失败树 shrink 成 5 个节点的最小树——一眼看出是占位合并逻辑错误地跨过了 ERROR 节点。

**B16．pydantic 在这里承担什么？为什么配置校验值得引入唯一的第三方运行时依赖？**

结论：把「配置非法 → 退出码 4」从口号变成机制——规则 schema 的声明、校验、错误定位全由它承担。
机制：规则集是嵌套结构（匹配条件 × 动作 × 参数 × 优先级），手写校验既啰嗦又容易漏；pydantic v2 用类型注解声明 schema，加载时自动校验并给出「哪个字段、哪行、什么原因」的结构化错误，cli 层捕获后统一映射退出码 4。它还顺带提供默认值填充与类型强转，四层配置叠加的结果直接灌进模型收口。
例子：用户在 `rules.toml` 里把 `action = "colapse"` 拼错——pydantic 报「不在枚举 {keep, drop, collapse, truncate} 中」并指出具体位置，工具以 4 退出；没有 schema 校验的话，这条规则会被静默忽略，用户以为剪枝生效了，骨架却悄悄变样——配置类工具最阴险的故障模式。

**B17．没有静态二进制了，部署故事怎么讲？**

结论：基本盘是「openEuler 自带 python3 + pipx/uv 一步安装」，单文件场景用 shiv zipapp 兜底；与 Rust 静态二进制的差距如实承认，但在目标环境里影响有限。
机制：`pipx install tracelens` / `uv tool install tracelens` 会创建隔离虚拟环境并把命令挂到 PATH，不污染系统 Python；`shiv` 把包与依赖打进一个自解压 zipapp，`scp` 过去 `./tracelens.pyz` 即跑，前提只有目标机存在 python3——openEuler 默认满足。真正做不到的是「目标机连 Python 都没有」的极端场景，这条写进第九节局限性。
例子：CI 里同时产出 wheel（正常分发）与 `.pyz`（应急分发）两个 artifact；自验报告的 demo 用 `.pyz` 形态演示「空环境拷贝即用」，把部署摩擦的实际水平展示出来而不是口头承诺。

**B18．UTF-8 截断在 Python 里怎么处理？grapheme cluster 又是什么？**

结论：先 decode 到 str、按字符数截断，天然不会劈开多字节字符；grapheme cluster 是用户感知的「一个字」，默认不处理、`--strict-grapheme` 才启用。
机制：Rust 版在字节层截断，必须用 `char_indices()` 找安全边界；Python 版对目标字段先 `decode("utf-8")`，str 切片以码点为单位，「劈开半个汉字」在 str 层不可能发生——这是 Python 在此处的便利面。但码点仍不等于用户感知字符：👨‍👩‍👧 这类 ZWJ（零宽连接符）序列由多个码点拼成一个 grapheme cluster，按码点截断可能把家庭切散；组合字符（`é` = `e` + U+0301）同理。默认按码点（覆盖绝大多数场景、零依赖），`--strict-grapheme` 引入 `regex` 包用 `\X` 按 grapheme 截。
例子：测试用例固定三组——纯中文、含 ZWJ emoji、含组合字符——分别验证默认与严格两种模式下输出合法且长度语义正确。这条权衡明确写进第九节局限性。

## C. Trace 与 Agent 领域类

**C19．Trace / Span 的基本模型是什么？**

结论：Trace 是一次端到端执行的完整记录，Span 是其中一个有起止时间的操作单元，父子指针把所有 Span 连成一棵调用树。
机制：每个 Span 携带 `trace_id`（属于哪次执行）、`span_id`（自身唯一标识）、`parent_span_id`（父操作，根 span 此字段为空）、起止时间戳、状态（OK/ERROR）以及一组 attributes 键值对（输入输出、模型名、token 用量都放这里）。同一 `trace_id` 下的所有 span 按 parent 指针重建，就还原出完整调用树。
例子：Agent 完成一次任务是一个 Trace；其中「调一次 gpt-4o」是一个 MODEL span，「执行一次 web_search」是它的兄弟 TOOL span，外层的 AGENT span 是它们共同的父节点。本工具的 expand 就是按 `span_id` 精确寻址其中一个节点的原始字节。

**C20．OTLP 是什么？OTel 的 GenAI 语义约定现状如何？**

结论：OTLP（OpenTelemetry Protocol）是 OTel 官方的遥测数据传输与编码协议；GenAI 语义约定（`gen_ai.*`）目前仍处 Experimental 阶段，不能单独依赖。
机制：OTLP 用 protobuf 定义 trace/metric/log 的数据结构，支持 gRPC 和 HTTP+JSON 两种承载方式，文件导出的 JSON 形态就是本工具 `OtlpJsonReader` 的输入。GenAI 语义约定规定了 LLM 调用应携带的标准 attribute（操作类型、模型名、token 用量等），但版本间字段仍在变动、各框架落地不一致——所以方案把它放在类型推断优先级的第二档，并打 `Convention` 来源标记。
例子：同一个 LLM 调用，MLflow 会写 `mlflow.spanType="LLM"`；规范的 OTel 埋点可能写 `gen_ai.operation.name="chat"`；某个老版本 SDK 可能什么都不写、只有一个叫 `llm_call` 的 span 名——三种情况分别落在推断的第一、二、四档，各有明确的来源等级。

**C21．为什么 Agent 调试特别需要这个工具？**

结论：Trace 体积与模型上下文窗口相差两个数量级，而暴力截断必然截掉关键证据——这是 context engineering（上下文工程）的一个具体子问题。
机制：多轮 Agent 的每轮 prompt 都携带全量历史，体积随轮次近似平方级增长，几百 MB 的 Trace 很常见，而窗口只有 128K~200K token；即便塞得下，lost in the middle 现象（长上下文中部信息利用率显著下降）也会让排查质量劣化。骨架 + 按需展开等价于给 Trace 做「索引 + 惰性加载」：窗口从「被动装前 N 个字节」变成「主动装 100% 拓扑 + 自选细节」。
例子：与业界的 context 管理手段同族——Claude Code / OpenHands 的 compaction 与 condenser 属于「历史摘要」一支，OpenCode 的存储/上下文分离属于「视图化」一支；本工具属于「外部记忆 + 按键检索」一支：低密度数据外置到文件，`span_id` 就是检索键。三支的完整对照见正文第四节。

**C22．progressive disclosure（渐进式披露）是什么？本工具哪里体现了它？**

结论：先给最小充分的摘要，细节按需拉取；骨架 / expand 的二元结构就是它的直接实现。
机制：一次性交付全量信息会淹没读者（无论人还是 LLM）的注意力预算；分层披露让读者先用极少信息完成「定位」，再只为定位到的目标支付「细节」的成本。两层之间必须有可执行的导航链接，否则读者知道有细节却不知道怎么拿。
例子：骨架是第一层（拓扑 + 状态 + 截断标记），`expand_hint` 是层间导航，expand 输出是第二层。类似 IDE 的代码折叠：默认收起函数体只看签名，点开才加载实现。Claude Code 用子代理隔离大体积读取、只回传摘要，是同一原则在会话侧的应用。

**C23．为什么说 MCP Server 是这个工具「最自然的形态」？**

结论：工具的目标用户是 Agent，MCP 让 Agent 用原生工具调用的方式消费它，比经由 shell 转一手更直接、更结构化；Python 下这条路尤其近。
机制：MCP（Model Context Protocol）是把工具和数据源接入 LLM 应用的开放协议，有官方 Python SDK。`mcp_server.py` 把 `get_skeleton` / `expand_span` 暴露成两个 tool，参数和返回都是结构化 schema——Agent 不用学 CLI 语法、不用解析 stdout 文本、错误也以结构化形式返回。
例子：CLI 形态下，Agent 要先生成一条正确的 shell 命令、再解析文本输出，两步都可能出错；MCP 形态下就是一次标准 tool 调用，和它调 web_search 没有任何区别。架构上只需在 `cli.py` 旁平行加一个模块，核心逻辑零改动——这正是 5.1 分层设计预留的回报。

## D. 工程实践类

**D24．为什么按周提交增量 PR，而不是最后一个大 PR？**

结论：小 PR 可评审、可回滚、暴露问题早；巨型 PR 对评审者约等于拒绝服务。
机制：四个里程碑四个 PR，前三个（ingest → prune → index/render/cli）各自独立可评审，每周附 demo 或测试报告同步进度。评审意见在 M2 期间到来，可以低成本修正方向；如果 8 月中旬一次性提交近万行，评审者只剩「全收」或「全拒」两个选项，返工成本最大化。
例子：PR#1 只含扫描器 + 双适配器 + fixtures + 微基准数据——即使后续方向有调整，这部分作为地基几乎不会白做；反过来它也最早验证了 Python 版风险最高的假设（扫描器吞吐）。

**D25．为什么刻意控制依赖数量？Mulan PSL v2 是什么？**

结论：每个依赖都是供应链审计、许可证合规和长期维护上的负债；Mulan PSL v2 是 openEuler 社区采用的宽松开源许可证。
机制：openEuler 社区对引入依赖有审查要求——许可证必须与 Mulan PSL v2（木兰宽松许可证第 2 版：中英双语、经 OSI 认证的 permissive 许可证，允许商用、修改、闭源再分发）兼容，依赖树规模直接影响构建与安全漏洞响应面。本方案运行时第三方只有 pydantic（MIT，兼容），其余全走标准库，重型能力（tiktoken、orjson、regex）一律做成可选 extras。
例子：反例是「为了显示进度条引入一个带 20 个传递依赖的 TUI 框架」——这类就该砍掉或手写替代；正例是 blake3 → blake2b 的替换：牺牲一点非瓶颈环节的速度，换掉一个原生扩展依赖。

**D26．syrupy 和 pytest-benchmark 分别测什么？**

结论：syrupy 管「输出长什么样」，pytest-benchmark 管「跑得多快」，两者防的都是静默劣化。
机制：syrupy 是 pytest 的快照插件——首次运行把渲染输出（tree/json/md）存成快照文件，之后每次运行做全文对比，出现差异需要人工 `--snapshot-update` 确认，适合「正确性 ≈ 格式稳定」的场景。pytest-benchmark 做统计学基准：多轮采样、报告分布，配合保存的历史结果对比性能回归。
例子：某次重构让 tree 输出每行多了一列空格——功能测试全绿，快照立刻标红；某次改动让扫描器慢了 3 倍——单测毫无感知，基准对比报告回归。两类问题都不该等到评审或线上才被发现。

**D27．索引文件为什么要有 magic + version？**

结论：自识别与向前兼容，是二进制格式设计的基本礼貌。
机制：magic（文件头固定字节 `b"TLNS"`）让工具在拿到错误文件时立即失败，而不是把垃圾数据解析成错误的 offset；version 字段让格式未来可演进——老工具遇到新版本明确报「不支持」，新工具可以兼容读旧格式。entry 按 `span_id` 排序则支撑 `bisect` 二分与短前缀匹配。
例子：用户把 `skeleton.txt` 误传给 `--index` 参数——magic 校验第一步就拦截并给出明确报错；这比读出一堆错位 offset、直到 expand 阶段才莫名报「摘要不匹配」友好得多。Git packfile（`PACK` + 版本号）、ELF（`\x7fELF`）都是同一惯例。

---

# 附录 B　术语表

> 按领域分组，深入解释见括号内交叉引用的正文章节或附录 A 条目。

## 可观测性与 Trace

- **Trace**：一次端到端执行（如 Agent 完成一个任务）的全过程记录，由一组 Span 构成。
- **Span**：Trace 中一个有起止时间的操作单元（一次模型调用、一次工具执行），携带状态与属性。（C19）
- **span_id / trace_id / parent_span_id**：Span 的唯一标识 / 所属 Trace 的标识 / 父 Span 的标识；三者足以重建整棵调用树。
- **Payload**：Span 上挂载的大体积内容，如完整 prompt、模型输出、工具返回——本工具剪枝与展开的主要对象。
- **attribute**：Span 上的键值对元数据，GenAI 语义（模型名、token 数、span 类型）都以此承载。
- **OpenTelemetry（OTel）**：CNCF 的可观测性开放标准，定义 trace/metric/log 的统一数据模型与 SDK。本方案的内部 IR 基于其语义模型。（§二）
- **OTLP**：OpenTelemetry Protocol，OTel 的标准传输与编码协议。（C20）
- **语义约定（semantic conventions）**：OTel 对 attribute 命名的标准化规定；GenAI 部分（`gen_ai.*`）截至目前仍为 Experimental。（C20）
- **MLflow Tracing**：MLflow 的 LLM/Agent 追踪功能，构建在 OTel SDK 之上，`mlflow.spanType` 等专有 attribute 提供了明确的节点类型。（§二）
- **OpenInference**：第三方 GenAI 追踪语义约定，在类型推断中位列第三档。
- **IR（中间表示）**：介于输入格式与处理逻辑之间的规范化内部数据结构；本方案以 OTel 语义模型为 IR。（A3）
- **SpanKind / KindSource**：节点类型（AGENT/MODEL/TOOL 等）/ 该类型判断的来源可信度等级（Explicit/Convention/Heuristic/Unknown）。（A4）

## 核心机制与数据结构

- **字节偏移索引**：`span_id → (offset, len)` 的映射表，expand 按其做字节切片，是「构造上正确」的关键。（A1、§5.2）
- **字节级扫描器**：只识别 span 边界、不解析值的双模式状态机；Python 版拿偏移的主路线。（B12、§5.2）
- **零拷贝（zero-copy）**：处理数据时不复制字节；本方案中体现为 mmap 切片直接输出、扫描器不构建对象。
- **mmap**：把文件映射进虚拟地址空间的机制，Python 标准库同名模块提供；按需缺页加载，内存占用与文件大小解耦。（B13）
- **buffer 协议 / memoryview**：Python 中零拷贝共享二进制数据的机制 / 其视图对象；`re` 与 `find` 可直接作用于 mmap。
- **缺页中断（page fault）／页缓存（page cache）**：访问未加载页时触发的内核加载机制 / 内核中缓存文件页的内存区域。
- **RSS / 堆内存**：进程常驻物理内存 / 其中由解释器对象占用的部分；本方案的内存指标以「堆峰值 ∝ 最大单 span」为核心性质。（§六）
- **packfile / .idx**：Git 的对象打包文件与其偏移索引；本方案结构与之同构。（A1）
- **稀疏索引（LSM）**：LSM-Tree 中只索引块起点、读取时定位块再扫描的结构；与「索引 + 回源取数」同族。
- **`--detach`**：备选模式，生成骨架时把被剪内容单独物化，应对原文件轮转，空间换可用性。（A2）
- **blake2b**：RFC 7693 标准化的现代哈希，Python `hashlib` 内置；此处用于文件完整性校验与截断摘要。（B14）
- **magic / version（文件头）**：二进制格式的自识别字节与版本号。（D27）
- **struct / bisect**：标准库的二进制编解码模块 / 有序序列二分查找模块；索引的写与读分别依赖两者。
- **幂等 / 确定性输出**：同一输入与配置产出逐字节相同的结果；可 diff、可缓存、可快照测试的前提。

## 剪枝与文本处理

- **剪枝（prune）**：按规则对 span 施加 Keep / Drop / CollapseSubtree / Truncate 动作并重建拓扑。（§5.3）
- **孤儿节点**：父节点被删后悬空、无法表达位置的节点；五条不变量明确禁止其出现。（A5）
- **elided_depth / 占位节点**：记录「此边折叠了几层」的计数 / 合并多个同类被删兄弟的汇总节点。
- **不变量（invariant）**：变换前后必须恒成立的性质；本方案用五条不变量形式化定义「骨架可信」。（A5）
- **Head / Tail / HeadTail**：三种截断策略，分别保留开头 / 结尾 / 两端。（A7）
- **expand_hint**：截断标记里内嵌的可执行取回命令。（A7）
- **UTF-8 / 码点 / grapheme cluster**：变长字符编码 / Unicode 标量值（Python str 的计数单位）/ 用户感知的单个字符（可能由多个码点组成，如 ZWJ emoji）。（B18）
- **点路径（`$.a.b[0].c`）**：字段级寻址语法的自实现子集，截断定位与 `--field` 共用。（§5.6）
- **token / tiktoken / chars_per_token**：LLM 的窗口计量单位 / 精确分词器（`[tokens]` extra）/ 默认估算系数。（§5.4）
- **`--max-tokens` 预算收紧**：给定 token 预算，按次序逐级增压剪枝直到骨架达标的机制；借鉴自业界的预算感知触发。（§5.5、§四）

## Python 工具链

- **pyproject.toml（PEP 621）/ console_scripts**：Python 项目的标准元数据文件 / 把函数注册为命令行入口的机制。
- **pipx / uv**：在隔离环境中安装命令行工具的两种主流方式；部署基本盘。（B17）
- **shiv / zipapp**：把包与依赖打进单个可执行 `.pyz` 的工具 / Python 的自解压归档格式；单文件分发兜底。（B17）
- **wheel**：Python 的标准二进制分发格式，`uv build` 产出。
- **pydantic**：基于类型注解的数据校验库；本方案唯一的第三方运行时依赖，承担规则/配置 schema 校验。（B16）
- **tomllib / tomli**：3.11+ 标准库的 TOML 解析器 / 其向下兼容的第三方前身。
- **dataclasses（slots）**：标准库的数据类装饰器；`slots=True` 省内存并防误写属性。
- **fnmatch**：标准库的 shell 风格通配匹配，承担 span name 的 glob 规则。
- **argparse**：标准库 CLI 解析器，承担三个子命令。
- **hypothesis / 策略（strategy）/ shrinking**：属性测试框架 / 其输入空间描述 / 失败输入自动最小化。（B15）
- **syrupy**：pytest 快照测试插件。（D26）
- **pytest-benchmark**：pytest 基准测试插件。（D26）
- **ruff / mypy**：极快的 lint+格式化工具 / 静态类型检查器；CI 第一道关。
- **orjson**：Rust 实现的高速 JSON 库，`[speed]` extra 备用。
- **regex（`\X`）**：功能超集的第三方正则库 / 其 grapheme cluster 匹配语法，`[grapheme]` extra。（B18）

## Agent 与业界机制

- **上下文窗口（context window）**：模型单次可处理的最大 token 数；本工具的核心约束来源。
- **context rot**：随上下文变长、无关内容累积导致模型表现劣化的现象。（C21）
- **lost in the middle**：长上下文中部信息被模型利用率显著下降的实证现象。（C21）
- **context engineering**：面向多轮多工具 Agent 的系统级上下文管理学科；本工具属其「外部记忆 + 检索」子方向。（C21）
- **progressive disclosure**：先摘要后细节的分层信息披露模式。（C22）
- **compaction / auto-compact / `/compact`**：把旧对话历史总结成摘要替换原文的机制及其自动/手动触发形态（Claude Code、OpenCode）。（§四、A10）
- **microcompact**：不调用 LLM 的内联清理，优先清除旧工具输出（Claude Code）。（§四）
- **condenser / LLMSummarizingCondenser / keep_first**：OpenHands 的可插拔历史压缩抽象 / 其默认 LLM 摘要实现 / 「最早 N 条事件永不压缩」参数。（§四）
- **event store**：以追加事件流（而非消息数组）为存储核心的架构（OpenHands）。（§四）
- **存储历史与模型上下文分离**：全量历史持久化、模型只见整理后切片的架构（OpenCode）；本方案「原文件 + 骨架」与之同构。（§四）
- **pruning placeholder**：工具输出被剪除后留下的占位符（OpenCode）；本方案 `__truncated__` 的不可逆版本对应物。（A7）
- **CLAUDE.md**：Claude Code 的项目级持久记忆文件，内容不随对话压缩丢失。（§四）
- **subagent（子代理）**：在独立上下文中执行子任务、只回传摘要的隔离机制。（§四、C22）
- **harness**：包裹模型的执行控制层（工具执行、循环控制、上下文组装）；退出码与 MCP 设计都以 harness 为消费方。
- **MCP（Model Context Protocol）**：把工具与数据源接入 LLM 应用的开放协议，有官方 Python SDK。（C23）
- **Mulan PSL v2**：木兰宽松许可证第 2 版，openEuler 采用的中英双语 permissive 开源许可证。（D25）
- **CI / PR / 退出码 / 对照组**：持续集成 / 拉取请求 / 进程返回状态 / 实验设计中的基线比较组。（D24、§5.6、A8）
