import path from "node:path";

export interface TraceLensConfig {
  enabled: boolean;
  /** trace.json 输出目录 */
  outDir: string;
}

export const DEFAULT_SUBDIR = ".tracelens/traces";

function envTruthy(name: string): boolean {
  const v = process.env[name];
  return v === "1" || v === "true" || v === "yes" || v === "on";
}

/**
 * 配置来源（从高到低）：
 *   TRACELENS_PLUGIN_DISABLED=1  关闭插件
 *   TRACELENS_TRACE_DIR=<dir>     覆盖输出目录
 *   默认 <项目目录>/.tracelens/traces
 */
export function resolveConfig(directory: string): TraceLensConfig {
  const enabled = !envTruthy("TRACELENS_PLUGIN_DISABLED");
  const outDir = process.env.TRACELENS_TRACE_DIR || path.join(directory, DEFAULT_SUBDIR);
  return { enabled, outDir };
}
