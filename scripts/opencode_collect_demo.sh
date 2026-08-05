#!/usr/bin/env bash
# 真实采集演示（方案 A）：opencode 插件 → 标准 MLflow trace.json → tracelens 全流程
#
# 作用：验证「由 opencode 插件产出的 trace 直接进入 tracelens 处理」这条主链路的真实性。
# 注意：需要本机可用模型（默认取 opencode 全局配置，如 mydeepseek）与网络，属「真实采集」证据线，
# 不进 CI（CI 用 tests/fixtures/opencode_sample.trace.json 做离线闭环）。
#
# 用法：  uv run bash scripts/opencode_collect_demo.sh
# 输出：  out/opencode_demo.trace.json  + 打印 inspect/skeleton 结果与字节级校验结论

set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$PWD"
OUT=out/opencode_demo.trace.json
mkdir -p out

# 1) 构建插件单文件
echo "==> 构建 opencode-plugin"
( cd opencode-plugin && npm ci >/dev/null 2>&1 && npm run build >/dev/null )

# 2) 临时项目安装插件
TMP="$(mktemp -d)"
mkdir -p "$TMP/.opencode/plugins"
cp opencode-plugin/dist/plugin.js "$TMP/.opencode/plugins/tracelens.js"

# 3) 真实跑一个会话（触发一次工具调用，让 trace 里有 TOOL span）
echo "==> opencode run（触发 bash 工具调用）"
( cd "$TMP" && opencode run "运行 pwd 命令查看当前目录，然后简单回答确认" >/dev/null 2>&1 )
sleep 3

TRACE_FILE="$(ls -t "$TMP/.tracelens/traces/"*.trace.json 2>/dev/null | head -1)"
if [ -z "${TRACE_FILE:-}" ]; then
  echo "错误：未找到插件产出的 trace（检查 opencode 是否加载了插件）" >&2
  exit 1
fi
cp "$TRACE_FILE" "$OUT"
echo "==> 插件产出：${OUT}（$(wc -c < "${OUT}") 字节）"

# 4) tracelens 全流程
echo "==> inspect"
uv run tracelens inspect --input "$OUT"

echo "==> skeleton（--max-tokens 4000）"
uv run tracelens skeleton --input "$OUT" --format tree --max-tokens 4000 \
  --out out/opencode_demo.skeleton.txt --emit-index out/opencode_demo.idx

echo "==> expand 字节级校验（对每个 span_id 取回并断言原样存在于原文件）"
uv run python - <<'PY'
import json, pathlib
from tracelens.index.reader import TraceIndex, write_index
from tracelens.ingest.reader import TraceReader

src = pathlib.Path("out/opencode_demo.trace.json")
raw = src.read_bytes()
with TraceReader(src) as r:
    doc = r.read()
    write_index("out/opencode_demo.idx", doc.spans, r.buf)
idx = TraceIndex.load("out/opencode_demo.idx")
with TraceReader(src) as r:
    for s in doc.spans:
        blob = idx.expand(r.buf, s.span_id)
        assert blob in raw, f"span {s.span_id[:8]} 取回字节不在原文件内"
print(f"全部 {doc.span_count} 个 span 展开字节均与原文逐字节一致 ✓")
PY

echo "==> 完成：$OUT"
