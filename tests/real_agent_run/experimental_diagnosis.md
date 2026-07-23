# 实验组诊断记录（冷启动 Claude Sonnet 子 Agent）

> 输入：仅 `skeleton.tree`（16 KB）+ `tracelens expand`。禁止直接读取原始 3.8 MB JSON。
> 子 Agent 事先不知道 bug。以下为其原样交付的诊断（已把本机绝对路径改为相对形式）。

## 1. 根因 span
- span_id：`f61c23699371208d`（骨架前缀 `f61c23`）
- 名称：`load_dataset`
- 类型：`TOOL`
- 状态：`ERROR`
- 父节点：`b0e11fe926dcac09`（CHAIN `turn_20`，第 20 轮）

## 2. 确切错误
```json
{
  "status_code": "ERROR",
  "status_message": "KeyError: config key 'FINANCE_PATH' is not set",
  "mlflow.spanInputs":  "{\"dataset\": \"q3_revenue\", \"config_key\": \"FINANCE_PATH\"}",
  "mlflow.spanOutputs": "{\"error\": \"KeyError: config key 'FINANCE_PATH' is not set\", \"code\": \"KeyError\"}"
}
```
错误码：`KeyError`。真实原因是财务数据源的路径配置项 `FINANCE_PATH` 未设置，导致数据
加载环节根本没有执行，营收数据从未被真正取到。

## 3. 掩盖 / 传播机制（关键发现）
1. `f61c23 load_dataset` 抛出 `KeyError`，status=ERROR（骨架里唯一的红色节点）。
2. 其兄弟节点 `6b7005 config_loader`（同属 `turn_20`）**status=OK**，但 `mlflow.spanOutputs`
   是 `{"result": "KeyError: config key 'FINANCE_PATH' is not set"}`——把上游异常文本原样
   塞进 `result` 字段并标记 OK，是一次「错误 → 成功结果」的语义转换，掩盖的核心机制。
3. 父节点 `b0e11f turn_20`（CHAIN）状态也是 OK，没有把子节点 ERROR 向上冒泡。
4. 被「漂白」的错误文本随后作为普通工具返回被拼进对话历史，喂给后续每一轮模型：
   `[tool:load_dataset] KeyError: config key 'FINANCE_PATH' is not set`。
5. 最终答案（`a727c0 final_answer`）：「2025 年 Q3 营收数据暂时无法从数据库获取，根据公开
   资料推算，企业级业务约占总营收的 45%。」——把噪声级别的检索碎片当数据编造出具体百分比，
   违反了系统提示词「不得把错误信息当作数据继续推理」的硬约束。

## 4. 修复建议
1. 部署/启动强制校验 `FINANCE_PATH` 等必需配置项，缺失即 fail-fast。
2. 包装层（`config_loader`）不得把子调用异常塞进 `result` 并标 OK，应透传 `status=ERROR`
   与结构化错误对象。
3. CHAIN 层（`turn_20`）检测到任一子 span ERROR 时应自身置 ERROR 或标 `has_error_child`。
4. 工具结果入历史前增加「错误检测」步骤，命中 `KeyError/Error/Exception` 走错误分支。
5. `final_answer` 生成前加闸门：存在未解决的数据源 TOOL ERROR 时，强制输出「数据不可用」。

## 5. 审计轨迹（全部 expand 命令与返回字节数）

| # | 命令 | 返回字节 |
|---|---|---|
| 1 | `expand --span-id f61c23`（完整 span） | 700 |
| 2 | `expand --span-id f61c23 --field '$.attributes["mlflow.spanOutputs"]'` | 90 |
| 3 | `expand --span-id f61c23 --field '$.attributes["mlflow.spanInputs"]'` | 66 |
| 4 | `expand --span-id b0e11f`（turn_20） | 527 |
| 5 | `expand --span-id 6b7005`（config_loader） | 632 |
| 6 | `expand --span-id 376f77 --field '$.attributes["mlflow.spanInputs"]'` | 9,715 |
| 7 | `expand --span-id 376f77 --field '$.attributes["mlflow.spanOutputs"]'` | 165 |
| 8 | `expand --span-id a727c0 --field '$.attributes["mlflow.spanOutputs"]'` | 146 |
| 9 | `expand --span-id a727c0 --field '$.attributes["mlflow.spanInputs"]'` | 11,726 |
| 10 | `expand --span-id a453e2`（root AGENT） | 606 |

取回内容总字节数：**24,373 字节**（骨架文件另计 16 KB）。

## 6. 合规确认
全程仅通过骨架文件 + `tracelens expand` 按 span_id/字段逐条取回，**未以任何方式直接读取
3.8 MB 的原始 trace 文件**，严格遵守限制。
