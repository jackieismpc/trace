import type { Plugin } from "@opencode-ai/plugin";
import { resolveConfig } from "./config.js";
import { exportSessionTrace } from "./export.js";
import type { SessionClient } from "./export.js";

/**
 * tracelens 采集插件（方案 A）：
 *
 * 监听 `session.idle`（每轮对话完成后触发），通过 SDK 拉取该会话的全部消息/部件
 * （text / reasoning / tool / step-finish），组装成标准 MLflow trace 并写入
 * `TRACELENS_TRACE_DIR`（默认 `<项目>/.tracelens/traces/<会话标题>.trace.json`）。
 *
 * 产出文件可直接被 tracelens 处理：
 *     tracelens inspect --input .tracelens/traces/<会话>.trace.json
 *
 * 幂等：同一会话的 `time.updated` 未变化时不重复写出（多轮时覆盖为更完整版本）。
 * 任何导出异常都不会影响会话本身。
 */
export const TraceLens: Plugin = async (input) => {
  const client = input.client as unknown as SessionClient;
  const cfg = resolveConfig(input.directory);
  if (!cfg.enabled) {
    return {};
  }

  const lastExported = new Map<string, number>();

  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle") return;
      const { sessionID } = event.properties;

      try {
        const rawSession = await client.session.get({ path: { id: sessionID } });
        const session =
          rawSession && typeof rawSession === "object" && "data" in (rawSession as object)
            ? (rawSession as { data: { time?: { updated?: number } } }).data
            : (rawSession as { time?: { updated?: number } });
        if (!session || !session.time) return;

        const key = session.time.updated ?? Date.now();
        if (lastExported.get(sessionID) === key) return;

        const file = await exportSessionTrace(client, sessionID, cfg.outDir);
        if (file) {
          lastExported.set(sessionID, key);
          await client.app.log({
            body: { service: "tracelens", level: "info", message: `exported ${file}` },
          });
        }
      } catch (err) {
        console.error("[tracelens] export trace failed:", err);
      }
    },
  };
};
