import path from "node:path";
import { promises as fs } from "node:fs";
import type { Message, Part, Session } from "@opencode-ai/sdk";
import { buildTrace } from "./builder.js";
import { serializeTrace } from "./serializer.js";

/** 本插件用到的 SDK 客户端的最小结构面（结构类型，运行时不导入 SDK）。 */
export interface SessionClient {
  session: {
    get(opts: { path: { id: string } }): Promise<unknown>;
    messages(opts: { path: { id: string } }): Promise<unknown>;
  };
  app: {
    log(opts: {
      body: { service: string; level: string; message: string; extra?: Record<string, unknown> };
    }): Promise<unknown>;
  };
}

/** 兼容 `data` / `fields` 两种返回风格。 */
async function unwrap<T>(result: unknown): Promise<T> {
  if (result && typeof result === "object" && "data" in (result as object)) {
    return (result as { data: T }).data;
  }
  return result as T;
}

/**
 * 拉取一个会话的全部消息/部件，组装成标准 trace 并写入 outDir。
 * @returns 写出的文件路径；无可用数据时返回 null。
 */
export async function exportSessionTrace(
  client: SessionClient,
  sessionID: string,
  outDir: string,
): Promise<string | null> {
  const session = await unwrap<Session>(await client.session.get({ path: { id: sessionID } }));
  if (!session) return null;

  const raw = await unwrap<{ info: Message; parts: Part[] }[]>(
    await client.session.messages({ path: { id: sessionID } }),
  );
  if (!raw || raw.length === 0) return null;

  const doc = buildTrace(session, raw);
  const text = serializeTrace(doc);
  const safeName = session.title.replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 80) || session.id;
  await fs.mkdir(outDir, { recursive: true });
  const file = path.join(outDir, `${safeName}.trace.json`);
  await fs.writeFile(file, text, "utf8");
  return file;
}
