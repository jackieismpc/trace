import { describe, expect, it } from "vitest";
import { buildTrace } from "../src/builder.js";
import { serializeTrace } from "../src/serializer.js";
import { sampleMessages, sampleSession, sequentialIdGen } from "./fixtures.js";

describe("serializeTrace", () => {
  const text = serializeTrace(buildTrace(sampleSession, sampleMessages, { idGen: sequentialIdGen() }));
  const doc = JSON.parse(text);

  it("是可解析的标准 MLflow 形态 JSON", () => {
    expect(doc.info).toMatchObject({ request_id: expect.stringContaining("opencode:") });
    expect(Array.isArray(doc.data.spans)).toBe(true);
    expect(doc.data.spans.length).toBeGreaterThanOrEqual(3);
  });

  it("attribute 值是 JSON 文本字符串（MLflow 约定）", () => {
    const root = doc.data.spans[0];
    expect(JSON.parse(root.attributes["mlflow.spanType"])).toBe("AGENT");
    const inputs = JSON.parse(root.attributes["mlflow.spanInputs"]);
    expect(typeof inputs.task).toBe("string");
  });

  it("ERROR 工具的错误在 status_message 中可见", () => {
    const err = doc.data.spans.find((s: { status_code: string }) => s.status_code === "ERROR");
    expect(err).toBeDefined();
    expect(err.status_message).toContain("File not found");
  });

  it("与提交的固定样本逐字节一致（builder 变更会显式更新样本）", async () => {
    const { readFile } = await import("node:fs/promises");
    const sample = new URL("./fixtures/opencode_sample.trace.json", import.meta.url);
    const committed = await readFile(sample, "utf8");
    expect(text).toBe(committed);
  });
});
