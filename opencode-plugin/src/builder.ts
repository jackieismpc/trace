import { randomBytes } from "node:crypto";
import type { Message, Part, Session } from "@opencode-ai/sdk";
import { encodeAttr } from "./serializer.js";
import type { MlflowSpan, MlflowTrace, SpanStatusCode } from "./types.js";

const MS_TO_NS = 1_000_000;

export interface BuildOptions {
  /** 注入 id 生成器，测试用确定性序列保证样本可复现；缺省为随机。 */
  idGen?: () => string;
}

export interface MessageEntry {
  info: Message;
  parts: Part[];
}

export function defaultIdGen(): string {
  return randomBytes(8).toString("hex");
}

function textOf(parts: Part[]): string {
  return parts
    .filter((p): p is Extract<Part, { type: "text"; text: string }> => p.type === "text")
    .map((p) => p.text)
    .join("\n");
}

function reasoningOf(parts: Part[]): string {
  return parts
    .filter((p): p is Extract<Part, { type: "reasoning"; text: string }> => p.type === "reasoning")
    .map((p) => p.text)
    .join("\n");
}

function tokensOf(parts: Part[]): Record<string, unknown> | undefined {
  const sf = parts.find((p): p is Extract<Part, { type: "step-finish"; tokens: unknown }> => p.type === "step-finish");
  return sf ? (sf.tokens as Record<string, unknown>) : undefined;
}

function describeTool(tool: string, input: Record<string, unknown>): string {
  for (const key of ["command", "filePath", "path", "pattern", "url", "query"]) {
    const v = input[key];
    if (typeof v === "string" && v) {
      return `${tool}: ${v.slice(0, 80)}`;
    }
  }
  return tool;
}

/**
 * 把一个 opencode 会话（Session + 消息/部件序列）映射为标准 MLflow trace：
 *
 *   - 会话                 → 根 AGENT span
 *   - 每个 assistant 消息   → 一轮 LLM span（inputs=触发它的用户文本，outputs=助手文本+reasoning+tokens）
 *   - 该消息内的 tool part  → 其下的 TOOL span（status=ERROR 时带 status_message）
 *
 * 纯函数：不依赖任何 I/O，便于单测与确定性样本。
 */
export function buildTrace(
  session: Session,
  messages: MessageEntry[],
  options: BuildOptions = {},
): MlflowTrace {
  const idGen = options.idGen ?? defaultIdGen;
  const traceId = idGen();
  const rootId = idGen();
  const spans: MlflowSpan[] = [];

  const startNs = session.time.created * MS_TO_NS;
  const endNs = Math.max(session.time.updated, session.time.created) * MS_TO_NS;

  const firstUserText = (() => {
    const u = messages.find((m) => m.info.role === "user");
    return u ? textOf(u.parts) : "";
  })();

  const lastAssistantText = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m && m.info.role === "assistant") {
        const t = textOf(m.parts);
        if (t) return t;
      }
    }
    return "";
  })();

  spans.push({
    name: `opencode:${session.title || session.id}`,
    context: { trace_id: traceId, span_id: rootId },
    parent_id: null,
    start_time: startNs,
    end_time: endNs,
    status_code: "OK",
    status_message: "",
    attributes: {
      "mlflow.spanType": encodeAttr("AGENT"),
      "mlflow.spanInputs": encodeAttr({ task: firstUserText }),
      "mlflow.spanOutputs": encodeAttr({ answer: lastAssistantText }),
    },
  });

  let turnIndex = 0;
  let lastUserText = firstUserText;

  for (const entry of messages) {
    const { info, parts } = entry;
    if (info.role === "user") {
      lastUserText = textOf(parts);
      continue;
    }

    turnIndex += 1;
    const turnId = idGen();
    const assistantText = textOf(parts);
    const reasoning = reasoningOf(parts);
    const tokens = tokensOf(parts);
    const msgStart = info.time.created * MS_TO_NS;
    const msgEnd = (info.time.completed ?? info.time.created) * MS_TO_NS;

    const err = (info as { error?: unknown }).error;
    const statusCode: SpanStatusCode = err ? "ERROR" : "OK";
    const statusMessage =
      err && typeof err === "object" && "message" in err
        ? String((err as { message: unknown }).message)
        : "";

    const llmOutputs: Record<string, unknown> = { content: assistantText, reasoning };
    if (tokens) llmOutputs.tokens = tokens;

    spans.push({
      name: `turn_${turnIndex}`,
      context: { trace_id: traceId, span_id: turnId },
      parent_id: rootId,
      start_time: msgStart,
      end_time: msgEnd,
      status_code: statusCode,
      status_message: statusMessage,
      attributes: {
        "mlflow.spanType": encodeAttr("LLM"),
        "mlflow.spanInputs": encodeAttr({
          messages: [{ role: "user", content: lastUserText }],
          model: `${info.providerID}/${info.modelID}`,
        }),
        "mlflow.spanOutputs": encodeAttr(llmOutputs),
      },
    });

    for (const part of parts) {
      if (part.type !== "tool") continue;
      const state = part.state;
      if (state.status === "pending" || state.status === "running") continue;
      const isError = state.status === "error";
      const input: Record<string, unknown> = (state as { input?: Record<string, unknown> }).input ?? {};
      const output = isError ? state.error : (state as { output?: string }).output ?? "";
      const st = (state as { time?: { start?: number; end?: number } }).time;
      const tStart = st?.start ? st.start * MS_TO_NS : msgStart;
      const tEnd = st?.end ? st.end * MS_TO_NS : msgEnd;

      spans.push({
        name: describeTool(part.tool, input),
        context: { trace_id: traceId, span_id: idGen() },
        parent_id: turnId,
        start_time: tStart,
        end_time: tEnd,
        status_code: isError ? "ERROR" : "OK",
        status_message: isError ? state.error : "",
        attributes: {
          "mlflow.spanType": encodeAttr("TOOL"),
          "mlflow.spanInputs": encodeAttr(input),
          "mlflow.spanOutputs": encodeAttr(output),
        },
      });
    }
  }

  return {
    info: {
      request_id: `opencode:${session.id}`,
      trace_id: traceId,
      execution_time_ms: Math.max(0, session.time.updated - session.time.created),
    },
    data: { spans },
  };
}
