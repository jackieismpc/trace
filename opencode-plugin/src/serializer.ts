import type { MlflowTrace } from "./types.js";

/**
 * 把 trace 序列化为标准 MLflow 形态的 JSON 文本：
 *
 *   {"info": {"request_id","trace_id","execution_time_ms"},
 *    "data": {"spans": [ {name, context:{trace_id,span_id}, parent_id,
 *                         start_time, end_time, status_code, status_message,
 *                         attributes:{mlflow.spanType, mlflow.spanInputs, mlflow.spanOutputs}} ]}}
 *
 * 该形态与 tracelens `ingest/mlflow.py` 完全兼容（运行时可被 inspect/skeleton/expand 处理）。
 */
export function serializeTrace(doc: MlflowTrace): string {
  return JSON.stringify(doc, null, 2) + "\n";
}

/**
 * 与 MLflow 约定一致：attribute 值以「JSON 文本字符串」存储。
 * 例如 spanType 存 `"AGENT"`（含引号），spanInputs 存 `{"a":1}` 的文本。
 */
export function encodeAttr(value: unknown): string {
  return JSON.stringify(value ?? null);
}
