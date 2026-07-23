# 真实 Agent 盲测 —— 归档结果（2026-07-23）

本目录保存 [docs/真实Agent测试报告.md](../../docs/真实Agent测试报告.md) 所依据的那一次
**真实 LLM 盲测**的原始产物。测试把一个真实的 Claude Sonnet Agent 放进回路，验证它能否
只凭 16 KB 骨架 + 按需 `expand` 定位一个**连测试设计者事先都不知道**的根因。

## 一句话结果
- **实验组**（骨架 + expand）：精确命中根因，数据足迹 13,078 token（占 128K 窗口 10.2%、
  占全文 1.13%）。
- **对照组**（原始 JSON 截断进 128K 窗口）：无法定位，根因落在窗口外第 3,129,145 字节。

## 文件清单
只归档小而有意义的**结果**文件；原始 3.8 MB trace、512 KB 对照截断、索引等大体积生成物
**不入库**（它们属于 `out/` 的运行产物，随用随生成）。

| 文件 | 说明 |
| --- | --- |
| `skeleton.tree` | 实验组 Agent 实际读到的 16 KB 骨架（核心输入，本身就印着根因那一行） |
| `answer_key.json` | 本次随机抽中的根因标准答案（评分基准；生成时密封） |
| `experimental_diagnosis.md` | 实验组冷启动子 Agent 的完整诊断 + 审计轨迹 |
| `control_diagnosis.md` | 对照组冷启动子 Agent 的结论（诚实判定证据不足） |
| `results.json` | 机器可读的评分与指标汇总 |

> 本次 trace 由 [`scripts/blind_bug_fixture.py`](../../scripts/blind_bug_fixture.py) 用运行时
> 随机熵生成，**每次运行都不同**；因此这里归档的是「这一次」的评分结果与骨架，而不保存那份
> 一次性的大 trace。要跑一次**全新**的盲测（换一个你我都不知道的新 bug），直接重跑该脚本。

## 跑一次新的盲测
```bash
python scripts/blind_bug_fixture.py out/blind_trace.json out/blind_answer_key.SEALED.json
tracelens skeleton --input out/blind_trace.json --config examples/demo_rules.toml \
    --format tree --max-tokens 4000 --out out/blind_skeleton.tree --emit-index out/blind.idx
# 把 out/blind_skeleton.tree 交给一个不知情的 Agent，让它只用 expand 查根因，
# 再打开 SEALED 答案对分。各测试的作用见 docs/测试总览.md。
```

隐私说明：trace 内容全部为合成数据（虚构的营收问答、模板化网页正文、`gpt-4o` 等公开模型名），
不含任何真实个人信息；诊断记录中的本机绝对路径已改写为相对形式。
