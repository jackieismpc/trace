"""渲染与预算收紧测试。

快照测试（syrupy）防的是**静默漂移**：某次重构让 tree 输出每行多了一列空格，
功能测试全绿，快照立刻标红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelens.errors import InputError
from tracelens.ingest.reader import TraceReader
from tracelens.model import (
    ByteRange,
    KindSource,
    Skeleton,
    SpanKind,
    SpanMeta,
    SpanNode,
    Status,
)
from tracelens.prune.engine import prune
from tracelens.prune.rules import DEFAULT_RULESET, Action, Match, Rule, RuleSet, TruncateParams
from tracelens.render import fit_to_budget, render, render_json, render_md, render_tree
from tracelens.render.jsonout import SCHEMA_VERSION, skeleton_to_dict
from tracelens.render.tree import MARK_ELIDED, MARK_HEURISTIC, MARK_TRUNCATED, format_bytes
from tracelens.testkit import SynthConfig, build_mlflow_trace
from tracelens.tokens import count_tokens, estimate_tokens

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_skeleton(ruleset: RuleSet | None = None) -> Skeleton:
    with TraceReader(FIXTURES / "mlflow_simple.json") as reader:
        doc = reader.read()
        return prune(
            doc,
            ruleset or DEFAULT_RULESET,
            buf=reader.buf,
            payload_fields_fn=reader.payload_fields,
            source_file="tests/fixtures/mlflow_simple.json",
        )


# ---- 快照：三种形态都不许静默漂移 -----------------------------------------


def test_tree_snapshot(snapshot: object) -> None:
    assert render_tree(_fixture_skeleton()) == snapshot


def test_json_snapshot(snapshot: object) -> None:
    assert render_json(_fixture_skeleton()) == snapshot


def test_md_snapshot(snapshot: object) -> None:
    assert render_md(_fixture_skeleton()) == snapshot


# ---- tree 形态 ------------------------------------------------------------


def test_tree_header_shows_counts() -> None:
    text = render_tree(_fixture_skeleton())
    assert "status=ERROR" in text
    assert "spans=4→4" in text


def test_tree_shows_error_message_inline() -> None:
    """ERROR 的首行信息直接摊在骨架上，省掉一次 expand 往返。"""
    assert 'error: relation "revenue_q3" does not exist' in render_tree(_fixture_skeleton())


def test_tree_marks_truncation_with_expand_hint() -> None:
    skeleton = _fixture_skeleton(
        RuleSet(
            rules=[
                Rule(
                    match=Match(kind=SpanKind.MODEL),
                    action=Action.TRUNCATE,
                    params=TruncateParams(max_chars=20),
                )
            ]
        )
    )
    text = render_tree(skeleton)
    assert MARK_TRUNCATED in text
    assert "tracelens expand --span-id" in text


def test_tree_marks_heuristic_kind() -> None:
    """启发式推断的类型必须带 ⚠——不能把猜测伪装成事实。"""
    meta = SpanMeta(
        span_id="aaaa",
        parent_id=None,
        trace_id="t",
        name="web_search",
        kind=SpanKind.TOOL,
        kind_source=KindSource.HEURISTIC,
        status=Status.OK,
        raw_range=ByteRange(0, 10),
    )
    skeleton = Skeleton(
        trace_id="t",
        roots=[SpanNode(meta=meta)],
        original_span_count=1,
        kept_span_count=1,
    )
    assert MARK_HEURISTIC in render_tree(skeleton)


def test_tree_shows_placeholder_and_elided_depth() -> None:
    doc_skeleton = _synthetic_skeleton()
    text = render_tree(doc_skeleton)
    assert MARK_ELIDED in text
    assert "elided" in text


def _synthetic_skeleton() -> Skeleton:
    def span(i: int, parent: int | None, kind: SpanKind) -> SpanMeta:
        return SpanMeta(
            span_id=f"{i:04x}",
            parent_id=None if parent is None else f"{parent:04x}",
            trace_id="t",
            name=f"n{i}",
            kind=kind,
            kind_source=KindSource.EXPLICIT,
            status=Status.OK,
            raw_range=ByteRange(0, 100),
        )

    from tracelens.model import TraceDoc

    spans = [span(0, None, SpanKind.AGENT)] + [span(i, 0, SpanKind.TOOL) for i in range(1, 6)]
    doc = TraceDoc(trace_id="t", spans=spans, source_format="mlflow", file_size=600)
    return prune(doc, RuleSet(rules=[Rule(match=Match(kind=SpanKind.TOOL), action=Action.DROP)]))


def test_format_bytes() -> None:
    assert format_bytes(512) == "512"
    assert format_bytes(2048) == "2.0K"
    assert format_bytes(5 * 1024 * 1024) == "5.0M"


# ---- json 形态 ------------------------------------------------------------


def test_json_schema_is_stable() -> None:
    data = json.loads(render_json(_fixture_skeleton()))
    assert data["schema_version"] == SCHEMA_VERSION
    assert set(data) >= {
        "trace_id",
        "status",
        "original_span_count",
        "kept_span_count",
        "roots",
    }


def test_json_truncation_block_matches_spec() -> None:
    """截断标记的字段名与方案 §5.4 的示例一致。"""
    skeleton = _fixture_skeleton(
        RuleSet(
            rules=[
                Rule(
                    match=Match(kind=SpanKind.MODEL),
                    action=Action.TRUNCATE,
                    params=TruncateParams(max_chars=20),
                )
            ]
        )
    )
    data = skeleton_to_dict(skeleton)
    node = data["roots"][0]["children"][0]
    mark = node["truncated_fields"][0]["__truncated__"]
    assert set(mark) == {
        "span_id",
        "field",
        "original_chars",
        "kept_chars",
        "strategy",
        "digest",
        "expand_hint",
    }


def test_json_keeps_chinese_readable() -> None:
    assert "\\u" not in render_json(_fixture_skeleton())


# ---- md 形态 --------------------------------------------------------------


def test_md_has_sections() -> None:
    text = render_md(_fixture_skeleton())
    assert text.startswith("# Trace 骨架")
    assert "## 调用树" in text
    assert "## 错误节点" in text


# ---- 分发入口 -------------------------------------------------------------


def test_render_dispatch() -> None:
    skeleton = _fixture_skeleton()
    assert render(skeleton, "tree") == render_tree(skeleton)
    assert render(skeleton, "json") == render_json(skeleton)
    assert render(skeleton, "md") == render_md(skeleton)


def test_render_unknown_format() -> None:
    with pytest.raises(InputError):
        render(_fixture_skeleton(), "yaml")


# ---- token 计数 -----------------------------------------------------------


def test_estimate_weights_cjk_higher() -> None:
    """同样字符数，中文的 token 数应显著高于英文。"""
    assert estimate_tokens("中" * 100) > estimate_tokens("a" * 100)


def test_count_tokens_reports_method() -> None:
    est = count_tokens("hello world")
    assert "estimate" in est.method
    assert int(est) == est.tokens


def test_exact_falls_back_honestly() -> None:
    """没装 [tokens] extra 时回退到估算，并在 method 里如实说明。"""
    est = count_tokens("hello", exact=True)
    assert "tiktoken" in est.method or "已回退" in est.method


def test_fit_to_budget_respects_chars_per_token() -> None:
    """chars_per_token 必须真正影响估算——否则它就是个静默失效的配置项。"""
    skeleton = _fixture_skeleton()
    _s1, text1 = fit_to_budget(_clone_for_budget(skeleton), render_tree, None, chars_per_token=4.0)
    _s2, text2 = fit_to_budget(_clone_for_budget(skeleton), render_tree, None, chars_per_token=1.0)
    # 系数越小，同样文本估出的 token 越多；两条 note 里的数字必须不同
    n1 = int(text1.split("token 估算：")[1].split("（")[0])
    n2 = int(text2.split("token 估算：")[1].split("（")[0])
    assert n2 > n1
    assert "chars/token=1.0" in text2


def _clone_for_budget(skeleton: Skeleton) -> Skeleton:
    """给预算测试一份干净的骨架副本（fit_to_budget 会往 notes 里追加）。"""
    return Skeleton(
        trace_id=skeleton.trace_id,
        roots=skeleton.roots,
        original_span_count=skeleton.original_span_count,
        kept_span_count=skeleton.kept_span_count,
        status=skeleton.status,
        duration_ns=skeleton.duration_ns,
        source_file=skeleton.source_file,
        source_size=skeleton.source_size,
    )


# ---- 预算收紧 -------------------------------------------------------------


def _big_skeleton() -> Skeleton:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        fh.write(build_mlflow_trace(SynthConfig(span_count=120, seed=5)))
        path = fh.name
    with TraceReader(path) as reader:
        doc = reader.read()
        return prune(
            doc,
            DEFAULT_RULESET,
            buf=reader.buf,
            payload_fields_fn=reader.payload_fields,
            source_file=path,
        )


def test_budget_none_just_annotates() -> None:
    skeleton, text = fit_to_budget(_fixture_skeleton(), render_tree, None)
    assert "token 估算" in text
    assert skeleton.notes


def test_budget_already_fits() -> None:
    _skeleton, text = fit_to_budget(_fixture_skeleton(), render_tree, 100_000)
    assert "预算 100000" in text


def test_budget_tightens_until_it_fits() -> None:
    """给一个明显偏紧的预算，骨架必须被压进去。"""
    skeleton = _big_skeleton()
    _fitted, text = fit_to_budget(skeleton, render_tree, 2000)
    assert count_tokens(text).tokens <= 2000
    assert "增压到第" in text


def test_budget_keeps_error_nodes_when_squeezed() -> None:
    """压到最紧也不能把 ERROR 节点压掉——保底集不可再剪。"""
    skeleton = _big_skeleton()
    error_ids = {
        n.meta.span_id
        for n in skeleton.all_nodes()
        if not n.collapsed and n.meta.status is Status.ERROR
    }
    fitted, _text = fit_to_budget(skeleton, render_tree, 60)
    kept = {n.meta.span_id for n in fitted.all_nodes() if not n.collapsed}
    assert error_ids <= kept


def test_budget_impossible_is_reported_honestly() -> None:
    """达不到预算时不假装成功，明说「已压到最紧仍超预算」。"""
    skeleton = _big_skeleton()
    _fitted, text = fit_to_budget(skeleton, render_tree, 5)
    assert "已压到最紧仍超预算" in text


def test_budget_preserves_expand_hints() -> None:
    """收紧预览不能破坏取回路径：expand_hint 与摘要必须原样保留。"""
    skeleton = _big_skeleton()
    before = {m.expand_hint for n in skeleton.all_nodes() for m in n.truncated_fields}
    fitted, _text = fit_to_budget(skeleton, render_json, 2500)
    after = {m.expand_hint for n in fitted.all_nodes() for m in n.truncated_fields}
    assert after <= before
    for node in fitted.all_nodes():
        for m in node.truncated_fields:
            assert m.digest.startswith("blake2b:")
