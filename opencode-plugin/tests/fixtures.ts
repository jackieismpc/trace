import type { Message, Part, Session } from "@opencode-ai/sdk";
import type { MessageEntry } from "../src/builder.js";

export const sampleSession: Session = {
  id: "ses_sample001",
  projectID: "proj_demo",
  directory: "/tmp/demo",
  title: "营收查询 fixture 会话",
  version: "1.0",
  time: { created: 1_750_000_000_000, updated: 1_750_000_090_000 },
};

function base(id: string, messageID: string): Pick<Part, "id" | "sessionID" | "messageID"> {
  return { id, sessionID: sampleSession.id, messageID };
}

export const sampleMessages: MessageEntry[] = [
  {
    info: {
      id: "msg_1",
      sessionID: sampleSession.id,
      role: "user",
      time: { created: 1_750_000_000_000 },
      agent: "build",
      model: { providerID: "mydeepseek", modelID: "deepseek-v4-flash" },
    } satisfies Message,
    parts: [
      { ...base("p_1a", "msg_1"), type: "text", text: "2025 年 Q3 的营收是多少？按业务线拆分。" },
    ],
  },
  {
    info: {
      id: "msg_2",
      sessionID: sampleSession.id,
      role: "assistant",
      time: { created: 1_750_000_010_000, completed: 1_750_000_030_000 },
      parentID: "msg_1",
      modelID: "deepseek-v4-flash",
      providerID: "mydeepseek",
      mode: "build",
      path: { cwd: "/tmp/demo", root: "/tmp" },
      cost: 0.01,
      tokens: { input: 100, output: 50, reasoning: 20, cache: { read: 0, write: 0 } },
    } satisfies Message,
    parts: [
      {
        ...base("p_2a", "msg_2"),
        type: "reasoning",
        text: "我先尝试查询数仓，再按业务线拆分。",
        time: { start: 1_750_000_010_000, end: 1_750_000_011_000 },
      },
      {
        ...base("p_2b", "msg_2"),
        type: "tool",
        callID: "call_01",
        tool: "bash",
        state: {
          status: "completed",
          input: { command: "python3 -c 'print(\"ok\")'" },
          output: "ok\n",
          title: "bash",
          metadata: {},
          time: { start: 1_750_000_015_000, end: 1_750_000_020_000 },
        },
      },
      { ...base("p_2c", "msg_2"), type: "text", text: "我先查一下数仓数据。" },
    ],
  },
  {
    info: {
      id: "msg_3",
      sessionID: sampleSession.id,
      role: "assistant",
      time: { created: 1_750_000_040_000, completed: 1_750_000_060_000 },
      parentID: "msg_2",
      modelID: "deepseek-v4-flash",
      providerID: "mydeepseek",
      mode: "build",
      path: { cwd: "/tmp/demo", root: "/tmp" },
      cost: 0.02,
      tokens: { input: 200, output: 80, reasoning: 40, cache: { read: 0, write: 0 } },
    } satisfies Message,
    parts: [
      {
        ...base("p_3a", "msg_3"),
        type: "reasoning",
        text: "数仓返回错误，记录错误并改用公开资料检索。",
        time: { start: 1_750_000_040_000, end: 1_750_000_041_000 },
      },
      {
        ...base("p_3b", "msg_3"),
        type: "tool",
        callID: "call_02",
        tool: "read",
        state: {
          status: "error",
          input: { filePath: "/data/revenue_q3.json" },
          error: "File not found: /data/revenue_q3.json",
          metadata: {},
          time: { start: 1_750_000_045_000, end: 1_750_000_046_000 },
        },
      },
      {
        ...base("p_3c", "msg_3"),
        type: "step-finish",
        reason: "tool-calls",
        cost: 0.02,
        tokens: { input: 200, output: 80, reasoning: 40, cache: { read: 0, write: 0 } },
      },
      { ...base("p_3d", "msg_3"), type: "text", text: "数据库表不存在，改用公开资料估算。" },
    ],
  },
  {
    info: {
      id: "msg_4",
      sessionID: sampleSession.id,
      role: "assistant",
      time: { created: 1_750_000_080_000, completed: 1_750_000_090_000 },
      parentID: "msg_3",
      modelID: "deepseek-v4-flash",
      providerID: "mydeepseek",
      mode: "build",
      path: { cwd: "/tmp/demo", root: "/tmp" },
      cost: 0.01,
      tokens: { input: 300, output: 120, reasoning: 0, cache: { read: 0, write: 0 } },
    } satisfies Message,
    parts: [
      { ...base("p_4a", "msg_4"), type: "text", text: "最终：企业级约 45%，云服务约 30%。" },
    ],
  },
];

/** 确定性 id 生成器：保证产出样本可复现（用于提交固定样本与测试断言）。 */
export function sequentialIdGen(): () => string {
  let i = 0;
  return () => (++i).toString(16).padStart(16, "0");
}
