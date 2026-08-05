"""闭环测试：opencode-plugin 产出的标准 trace 样本可被 tracelens 全流程处理。

样本 `opencode-plugin/tests/fixtures/opencode_sample.trace.json` 由插件的
builder + serializer 用确定性 id 生成并提交（见 opencode-plugin/README.md）。
这里验证：解析 → 状态/错误可见 → 骨架 → 索引 → 按 span_id 逐字节展开。
"""

from __future__ import annotations

from pathlib import Path

from tracelens.index.reader import TraceIndex, write_index
from tracelens.ingest.reader import TraceReader
from tracelens.model import Status
from tracelens.prune.engine import prune
from tracelens.prune.rules import DEFAULT_RULESET
from tracelens.render import render_tree

SAMPLE = (
    Path(__file__).resolve().parent.parent
    / "opencode-plugin"
    / "tests"
    / "fixtures"
    / "opencode_sample.trace.json"
)


def test_sample_parses_and_error_visible() -> None:
    with TraceReader(SAMPLE) as reader:
        doc = reader.read()
    assert doc.span_count >= 6
    errors = [s for s in doc.spans if s.status is Status.ERROR]
    assert len(errors) == 1
    assert errors[0].name == "read: /data/revenue_q3.json"
    assert "File not found" in (errors[0].status_message or "")


def test_skeleton_and_expand_roundtrip(tmp_path: Path) -> None:
    index_path = tmp_path / "sample.idx"

    with TraceReader(SAMPLE) as reader:
        doc = reader.read()
        skeleton = prune(
            doc,
            DEFAULT_RULESET,
            buf=reader.buf,
            payload_fields_fn=reader.payload_fields,
            source_file=str(SAMPLE),
        )
        write_index(index_path, doc.spans, reader.buf)
        skeleton_text = render_tree(skeleton)

    assert "ERROR" in skeleton_text

    error_span = next(s for s in doc.spans if s.status is Status.ERROR)
    index = TraceIndex.load(index_path)
    raw_bytes = SAMPLE.read_bytes()

    with TraceReader(SAMPLE) as reader:
        expanded = index.expand(reader.buf, error_span.span_id)

    # expand 取回的是原始字节切片，必须能在原文件里逐字节找到
    assert expanded in raw_bytes
    assert b"File not found" in expanded
