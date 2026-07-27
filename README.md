# tracelens —— Trace 骨架生成与按需展开

把一份动辄几百 MB 的 Agent Trace 拆成两部分：

- **骨架**：完整拓扑 + 节点类型 + 状态 + 耗时 + 截断标记，几 KB，直接塞进模型上下文；
- **字节偏移索引**：`span_id → (offset, len)`，需要细节时凭 `span_id` 从原文件**逐字节**取回。

这不是压缩。一次 Agent 执行产生的 Trace，体积几乎全部由少数几类字段贡献
（LLM 的全量 prompt、reasoning、工具返回的网页正文），而排查问题真正需要的东西
——父子关系、节点类型、成功还是失败、耗时——占比不到 1%。
**信息密度最高的部分体积最小**，所以该做的是把「高密度低体积」和「低密度高体积」
两部分物理分离，而不是有损压缩。

实测（[验证报告](docs/验证报告.md)）：一份 3.8 MB / 411 span 的 Trace，骨架
16 KB（原文件的 0.41%、128K 窗口的 2.9%），全过程定位根因消耗约 3,963 token；
同一份 Trace 直接截断喂给模型，窗口只装得下 13.9%，且唯一的错误证据落在窗口之外。

## 安装

```bash
uv sync --dev
```

或作为独立工具安装：

```bash
uv tool install .
```

## 三条命令

```bash
# 1) 看一眼这是什么
tracelens inspect --input trace.json

# 2) 生成骨架 + 索引
tracelens skeleton --input trace.json --config examples/rules.toml \
    --format tree --max-tokens 4000 --out skeleton.txt --emit-index trace.idx

# 3) 按需展开（span_id 支持短前缀；--field 定位到字段级；--raw 输出原始字节）
tracelens expand --input trace.json --index trace.idx --span-id a3f2c1
tracelens expand --input trace.json --index trace.idx --span-id a3f2c1 \
    --field '$.attributes["mlflow.spanOutputs"]'
```

骨架长这样：

```
trace 4bf92f35  status=ERROR  spans=411→29  dur=48.2s
└─ 8f21a4 AGENT     research_agent                    OK   48.2s  in=76     out=44
   ├─ 14f03d MODEL     gpt-4o                            OK    2.1s  in=370    out=165     ✂ ⋯1层
   │     ✂ $.attributes["mlflow.spanInputs"] 210→160 chars, head
   │       "{\"messages\": [{\"role\": \"user\", \"content\": \"[system] 你是一个数据分析助手…
   │       expand: tracelens expand --span-id 14f03d --field '$.attributes["mlflow.spanInputs"]'
   ├─ 7c02be CHAIN     turn_9                            OK    3.5s
   │  ├─ 4fceb8 TOOL      sql_query                      ERROR   200ms  in=71     out=82
   │  │  └─ error: relation "revenue_q3" does not exist
   │  └─ f82df1 TOOL      tool_executor                     OK   210ms
   ├─ ⋯ elided 23 similar CHAIN spans (all OK)
   └─ ⋯ elided 345 similar TOOL spans (all OK)

图例：✂ 字段被截断（可 expand 取回）  ⚠ 类型为启发式推断，不要当事实用  ⋯ 此处折叠了同类节点
```

三个符号各占 1 token 但信息量很大。注意 `expand:` 那一行——**取回的方法直接写在
数据里**，Agent 不需要额外的 system prompt 教它怎么展开。

## 四条设计承诺

**① 还原的 Payload 与原始数据逐字节一致，且这是构造性的。**
`expand` 执行的就是 `mm[off:off+len]`，中间没有解析与再序列化环节——想违反都没有
代码路径可走。任何「解析成对象 → 存起来 → 展开时再序列化」的做法都过不了这一关：
`json.dumps` 会把中文写成 `\uXXXX`，`2.50` 变成 `2.5`，空白丢失，重复 key 被静默
吞掉。

**② 骨架不会撒谎。** 删掉中间节点时，子节点重挂到最近的存活祖先并标记折叠层数；
被删节点合并成占位节点，节点数守恒。五条拓扑不变量由 hypothesis 在随机森林 ×
随机规则集上钉死，其中最核心的一条是：**骨架上读到的祖先关系，在真实执行中一定
成立**。

**③ 不确定的信息一定标注来源。** 节点类型带 `KindSource`（Explicit / Convention /
Heuristic / Unknown）。下游读者是 LLM，把猜测伪装成事实会让它沿着错误的类型判断
走错整条排查路线，且错得毫无征兆。不知道就说不知道。

**④ 宁可失败，也不喂错数据。** 索引头部存原文件的 blake2b 摘要，文件被改动或轮转
后 `expand` 立即以退出码 3 报错，而不是返回一段「看起来合法、实际错位」的切片。

## 配置

规则是**数据不是代码**：改配置重跑即可生效。四层叠加，后者覆盖前者：

```
内置默认  <  TOML 文件  <  环境变量 TRACELENS_*  <  CLI 参数
```

对应三种实际用法：团队约定沉淀在配置文件，CI 用环境变量，临时调试用 CLI 覆盖一切。

```toml
# 折叠成功的检索工具调用；它们会变成占位节点，不会凭空消失
[[rules]]
name = "折叠成功的检索工具调用"
action = "drop"          # keep / drop / collapse_subtree / truncate
priority = 60
match = { name_glob = "web_search", status = "OK" }

# 模型调用是体积主力，但拓扑必须留着——保留节点、只截断内容
[[rules]]
action = "truncate"
priority = 40
match = { kind = "MODEL" }
params = { strategy = "head", max_chars = 160 }   # head / tail / head_tail
```

完整的带注释样例见 [examples/rules.toml](examples/rules.toml)。

**无法关闭的硬保护**：无论规则怎么写，根节点、以及任一 ERROR 节点到根的整条路径
永远不会被剪掉。排查问题时最需要的就是这两样，交给配置去保证太脆弱了。

配置非法（比如把 `collapse_subtree` 拼错）会以退出码 4 明确失败，而不是让这条规则
被静默忽略——那是配置类工具最阴险的故障模式。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 输入不存在或解析失败 |
| 2 | `span_id` 未命中（或短前缀有歧义） |
| 3 | 索引与原文件不匹配（摘要校验失败） |
| 4 | 配置非法 |

上层脚本与 Agent harness 必须能程序化地区分「没找到」和「数据坏了」。

## 支持的输入格式

| 格式 | 说明 |
| --- | --- |
| MLflow Tracing | `{"info": …, "data": {"spans": […]}}`，类型来自 `mlflow.spanType`（Explicit） |
| OTLP JSON | `resourceSpans → scopeSpans → spans`，类型靠 `gen_ai.*` 约定或 span 名推断 |

内部 IR 用 OTel 语义模型：MLflow Tracing 构建在 OTel SDK 之上，MLflow → OTel 可
无损映射，反向不成立。将来接 Langfuse、Phoenix、Jaeger 等任意 OTLP 兼容源，
只需新增一个 Reader，剪枝、索引、渲染零改动。

## 开发

```bash
uv run pytest                                   # 190 项测试
uv run pytest --cov --cov-report=term-missing   # 覆盖率（当前 92%）
uv run ruff check . && uv run ruff format --check .
uv run mypy                                     # strict 模式
```

## 测试

三层测试，各自回答一个问题（详见[验证报告](docs/验证报告.md)）：

1. **单元 / 属性 / 快照**（`tests/`，190 项 / 92% 覆盖）——每个模块行为对不对。核心是
   `test_topology.py` 用 hypothesis 钉死五条拓扑不变量、`test_index.py` 闭环验证字节级一致、
   `test_cli.py` 锁定退出码语义。
2. **脚本 demo / 基准**（`scripts/`）——端到端可达性与性能。
   `demo_investigate.py` / `demo_control.py` 是不调 LLM 的实验/对照组；`bench_pipeline.py`
   跑全流程性能。
3. **真实 Agent 盲测**——把一个事先不知道 bug 的 LLM 放进回路，验证它只凭骨架 + `expand`
   能否查出根因（对照组：把原始 JSON 截断进 128K 窗口）。

```bash
# 信息可达性自验（不调 LLM）
uv run python scripts/make_demo_fixture.py out/demo_trace.json
uv run python scripts/demo_investigate.py   # 实验组：定位根因
uv run python scripts/demo_control.py       # 对照组：证据进不了窗口
uv run python scripts/bench_pipeline.py 200 # 全流程基准

# 真实 Agent 盲测（根因随机且密封，每次运行都不同）
uv run python scripts/blind_bug_fixture.py out/blind_trace.json out/blind_answer_key.SEALED.json
# 把生成的骨架交给一个不知情的 Agent，让它只用 expand 查根因，再开封 SEALED 对分
```

## 文档

- [使用指南](docs/使用指南.md)
- [架构说明](docs/架构.md)
- [验证报告](docs/验证报告.md)（信息可达性自验 + 真实 Agent 盲测 + 性能 + 质量）

## 许可证

[Mulan PSL v2](LICENSE)
