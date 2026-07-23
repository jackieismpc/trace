# tracelens —— Trace 骨架生成与按需展开

把一份动辄几百 MB 的 Agent Trace，拆成两部分：

- **骨架**：完整拓扑 + 类型 + 状态 + 耗时 + 截断标记，几 KB，直接塞进模型上下文；
- **字节偏移索引**：`span_id → (offset, len)`，需要细节时凭 `span_id` 从原文件**逐字节**取回。

这不是压缩。一次 Agent 执行产生的 Trace，体积几乎全部由少数几类字段贡献
（LLM 的全量 prompt、reasoning、工具返回的网页正文），而排查问题真正需要的东西
——父子关系、节点类型、成功还是失败、耗时——占比不到 1%。
**信息密度最高的部分体积最小**，所以该做的是把「高密度低体积」和「低密度高体积」
两部分物理分离，而不是有损压缩。

## 当前进度

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M1 | 字节级扫描器、IR、格式嗅探、MLflow/OTLP 双适配器、`inspect` | ✅ 已完成 |
| M2 | 剪枝引擎（规则、拓扑重建、截断） | 进行中 |
| M3 | 索引、`expand`、三种渲染、完整 CLI | 待开始 |
| M4 | 自验报告、文档、打包 | 待开始 |

## 安装

```bash
uv sync --dev
```

## 使用

```bash
uv run tracelens inspect --input tests/fixtures/mlflow_simple.json
```

输出示例：

```
文件      : tests/fixtures/mlflow_simple.json
格式      : mlflow
trace_id  : 4bf92f3577b34da6a3ce929d0e0e4736
span 总数 : 4（根节点 1 个）
整体状态  : ERROR，ERROR 节点 1 个
类型分布  : AGENT=1, MODEL=1, TOOL=1, UNKNOWN=1
类型来源  : Explicit=3, Unknown=1
```

「类型来源」是刻意暴露的：类型是显式声明的（Explicit）、按标准约定推出的
（Convention），还是拿 span 名猜的（Heuristic），下游必须知道。
把猜测伪装成事实，Agent 会沿着错误的类型判断走错整条排查路线，且错得毫无征兆。

## 开发

```bash
uv run pytest                          # 全部测试
uv run ruff check . && uv run mypy     # lint 与严格类型检查
uv run python scripts/bench_scanner.py # 扫描器吞吐基准（验收线 200 MB/s）
```

## 文档

- [架构说明](docs/架构.md)
- [实施方案（v4）](实施方案-Trace骨架生成与按需展开-v4-Python.md)

## 许可证

[Mulan PSL v2](LICENSE)
