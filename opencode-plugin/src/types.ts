export type SpanStatusCode = "OK" | "ERROR" | "UNSET";

/** 单个 span 的中立描述，最终序列化为 tracelens 可解析的标准 MLflow 形态。 */
export interface MlflowSpan {
  name: string;
  context: { trace_id: string; span_id: string };
  parent_id: string | null;
  /** 纳秒 */
  start_time: number;
  /** 纳秒 */
  end_time: number;
  status_code: SpanStatusCode;
  status_message: string;
  attributes: Record<string, string>;
}

export interface MlflowTrace {
  info: { request_id: string; trace_id: string; execution_time_ms: number };
  data: { spans: MlflowSpan[] };
}
