"""截断测试：三种策略、UTF-8 边界、自描述标记。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelens.ingest.common import PayloadField
from tracelens.ingest.reader import TraceReader
from tracelens.model import (
    ByteRange,
    KindSource,
    SpanKind,
    SpanMeta,
    SpanNode,
    Status,
    TraceDoc,
)
from tracelens.prune.engine import prune
from tracelens.prune.rules import Action, Match, Rule, RuleSet, TruncateParams
from tracelens.prune.truncate import (
    ELLIPSIS,
    apply_truncation,
    digest_bytes,
    expand_hint,
    truncate_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---- 三种策略 -------------------------------------------------------------


def test_head_keeps_prefix() -> None:
    assert truncate_text("abcdefghij", 4, "head") == "abcd" + ELLIPSIS


def test_tail_keeps_suffix() -> None:
    """错误栈的根因在结尾，所以要有 tail 策略。"""
    assert truncate_text("abcdefghij", 4, "tail") == ELLIPSIS + "ghij"


def test_head_tail_keeps_both_ends() -> None:
    assert truncate_text("abcdefghij", 4, "head_tail") == "ab" + ELLIPSIS + "ij"


def test_short_text_is_untouched() -> None:
    assert truncate_text("abc", 10, "head") == "abc"
    assert truncate_text("abc", 3, "head") == "abc"


# ---- UTF-8 与 grapheme ----------------------------------------------------


def test_chinese_is_not_split_mid_character() -> None:
    """在 str 层截断，天然不会劈开多字节字符。"""
    text = "中文测试内容一二三四"
    out = truncate_text(text, 4, "head")
    assert out == "中文测试" + ELLIPSIS
    out.encode("utf-8").decode("utf-8")  # 必须是合法 UTF-8


def test_combining_character_default_is_codepoint() -> None:
    """`é` = `e` + U+0301：默认按码点截，可能把组合字符切开——这是已知取舍。"""
    text = "éxyz"
    assert truncate_text(text, 1, "head") == "e" + ELLIPSIS


def test_emoji_zwj_sequence_stays_valid_utf8() -> None:
    """ZWJ emoji 按码点截断可能把家庭切散，但输出必须仍是合法 UTF-8。"""
    text = "👨‍👩‍👧abcdefgh"
    out = truncate_text(text, 3, "head")
    assert out.encode("utf-8").decode("utf-8") == out
    assert out.endswith(ELLIPSIS)


def test_strict_grapheme_keeps_family_intact() -> None:
    """装了 `regex` 时，--strict-grapheme 按 grapheme cluster 截，家庭不会被切散。"""
    pytest.importorskip("regex", reason="grapheme 模式需要可选依赖 [grapheme]")
    text = "👨‍👩‍👧abcdefgh"
    out = truncate_text(text, 1, "head", strict_grapheme=True)
    assert out == "👨‍👩‍👧" + ELLIPSIS


# ---- 自描述标记 -----------------------------------------------------------


def _node() -> SpanNode:
    meta = SpanMeta(
        span_id="a3f2c1d4e5b60789",
        parent_id=None,
        trace_id="t",
        name="gpt-4o",
        kind=SpanKind.MODEL,
        kind_source=KindSource.EXPLICIT,
        status=Status.OK,
        raw_range=ByteRange(0, 0),
    )
    return SpanNode(meta=meta)


def test_truncation_mark_is_self_describing() -> None:
    """标记里必须带够信息，让 Agent 不靠外部教学就能取回全文。"""
    buf = b'"' + b"x" * 500 + b'"'
    node = _node()
    marks = apply_truncation(
        node=node,
        fields=[PayloadField(path="$.outputs.content", start=0, end=len(buf))],
        buf=buf,
        max_chars=200,
        strategy="head_tail",
    )
    assert len(marks) == 1
    mark = marks[0]
    assert mark.original_chars == 502
    assert mark.kept_chars == 200
    assert mark.strategy == "head_tail"
    assert mark.digest.startswith("blake2b:")
    assert mark.expand_hint == ("tracelens expand --span-id a3f2c1 --field '$.outputs.content'")
    # 序列化后就是骨架 JSON 里的 __truncated__ 结构
    payload = json.dumps(mark.to_dict(), ensure_ascii=False)
    assert "expand_hint" in payload


def test_small_fields_are_not_marked() -> None:
    node = _node()
    marks = apply_truncation(
        node=node,
        fields=[PayloadField(path="$.a", start=0, end=5)],
        buf=b'"abc"',
        max_chars=200,
    )
    assert marks == []
    assert node.truncated_fields == []


def test_field_globs_select_subset() -> None:
    node = _node()
    buf = b'"' + b"y" * 400 + b'"'
    fields = [
        PayloadField(path='$.attributes["mlflow.spanInputs"]', start=0, end=len(buf)),
        PayloadField(path='$.attributes["mlflow.spanOutputs"]', start=0, end=len(buf)),
    ]
    marks = apply_truncation(
        node=node,
        fields=fields,
        buf=buf,
        max_chars=50,
        field_globs=["*spanOutputs*"],
    )
    assert [m.field_path for m in marks] == ['$.attributes["mlflow.spanOutputs"]']


def test_digest_is_stable_and_distinct() -> None:
    assert digest_bytes(b"abc") == digest_bytes(b"abc")
    assert digest_bytes(b"abc") != digest_bytes(b"abd")


def test_expand_hint_uses_short_id() -> None:
    assert "--span-id a3f2c1 " in expand_hint("a3f2c1d4e5b60789", "$.x")


# ---- 端到端：真实 fixture 上的截断 ----------------------------------------


def test_truncation_on_real_fixture() -> None:
    with TraceReader(FIXTURES / "mlflow_simple.json") as reader:
        doc = reader.read()
        ruleset = RuleSet(
            rules=[
                Rule(
                    match=Match(kind=SpanKind.MODEL),
                    action=Action.TRUNCATE,
                    params=TruncateParams(strategy="head", max_chars=30),
                )
            ]
        )
        skeleton = prune(doc, ruleset, buf=reader.buf, payload_fields_fn=reader.payload_fields)

    llm = next(n for n in skeleton.all_nodes() if n.meta.name == "gpt-4o")
    assert llm.truncated_fields, "大 prompt 应该被截断"
    paths = {m.field_path for m in llm.truncated_fields}
    assert '$.attributes["mlflow.spanInputs"]' in paths
    # 原文件从不被修改
    assert (FIXTURES / "mlflow_simple.json").read_bytes().count(b"revenue_q3") >= 1


def test_truncation_skipped_without_buffer() -> None:
    """不给 buf 时只做拓扑剪枝，不读任何 payload。"""
    doc = TraceDoc(
        trace_id="t",
        spans=[
            SpanMeta(
                span_id="0001",
                parent_id=None,
                trace_id="t",
                name="x",
                kind=SpanKind.MODEL,
                kind_source=KindSource.EXPLICIT,
                status=Status.OK,
                raw_range=ByteRange(0, 9999),
            )
        ],
        source_format="mlflow",
    )
    skeleton = prune(doc, RuleSet(rules=[Rule(match=Match(), action=Action.TRUNCATE)]))
    assert skeleton.roots[0].truncated_fields == []
