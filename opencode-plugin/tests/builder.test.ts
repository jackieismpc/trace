import { describe, expect, it } from "vitest";
import { buildTrace } from "../src/builder.js";
import { sampleMessages, sampleSession, sequentialIdGen } from "./fixtures.js";

const idGen = sequentialIdGen();

describe("buildTrace", () => {
  const trace = buildTrace(sampleSession, sampleMessages, { idGen });
  const { spans } = trace.data;

  it("生成 1 根 + 3 轮 LLM + 2 工具 span", () => {
    expect(spans).toHaveLength(6);
  });

  it("根 span 是 AGENT，无父节点", () => {
    const root = spans[0];
    expect(root).toBeDefined();
    expect(root.parent_id).toBeNull();
    expect(JSON.parse(root.attributes["mlflow.spanType"])).toBe("AGENT");
    expect(root.name).toContain("opencode:");
  });

  it("每轮 assistant 消息生成一个 LLM turn span，父为根", () => {
    const turns = spans.filter((s) => JSON.parse(s.attributes["mlflow.spanType"]) === "LLM");
    expect(turns).toHaveLength(3);
    for (const t of turns) expect(t.parent_id).toBe(spans[0].context.span_id);
    expect(JSON.parse(turns[1].attributes["mlflow.spanInputs"]).messages[0].content).toBe(
      "2025 年 Q3 的营收是多少？按业务线拆分。",
    );
  });

  it("工具 span 挂在所属轮次下，错误工具为 ERROR 并带错误原文", () => {
    const tools = spans.filter((s) => JSON.parse(s.attributes["mlflow.spanType"]) === "TOOL");
    expect(tools).toHaveLength(2);

    const ok = tools.find((t) => t.status_code === "OK");
    const err = tools.find((t) => t.status_code === "ERROR");
    expect(ok).toBeDefined();
    expect(err).toBeDefined();

    expect(err.status_message).toBe("File not found: /data/revenue_q3.json");
    expect(JSON.parse(err.attributes["mlflow.spanInputs"]).filePath).toBe("/data/revenue_q3.json");
    expect(JSON.parse(err.attributes["mlflow.spanOutputs"])).toBe("File not found: /data/revenue_q3.json");

    const errTurn = spans.find((s) => s.context.span_id === err.parent_id);
    expect(errTurn).toBeDefined();
    expect(JSON.parse(errTurn.attributes["mlflow.spanType"])).toBe("LLM");
  });

  it("span_id 全部唯一", () => {
    const ids = spans.map((s) => s.context.span_id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("时间以纳秒为单位且单调", () => {
    for (const s of spans) {
      expect(s.start_time).toBeGreaterThan(0);
      expect(s.end_time).toBeGreaterThanOrEqual(s.start_time);
    }
  });
});
