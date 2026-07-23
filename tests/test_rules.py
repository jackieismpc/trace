"""规则与配置测试——对应 Issue 第三条覆盖要求「配置动态调整」。

要点有三：
* 同一 Trace 在不同规则集下的骨架必须**体现规则差异**；
* 冲突按声明的优先级处理；
* 非法配置以退出码 4 明确失败，而不是被静默忽略。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracelens.errors import ConfigError
from tracelens.model import ByteRange, KindSource, SpanKind, SpanMeta, Status, TraceDoc
from tracelens.prune.engine import prune, resolve_actions
from tracelens.prune.rules import (
    DEFAULT_RULESET,
    Action,
    Match,
    Rule,
    RuleSet,
    load_ruleset,
    ruleset_from_dict,
)
from tracelens.prune.topology import build_forest, compute_depths


def _span(i: int, parent: int | None, kind: SpanKind, status: Status, size: int) -> SpanMeta:
    return SpanMeta(
        span_id=f"{i:04x}",
        parent_id=None if parent is None else f"{parent:04x}",
        trace_id="t",
        name=f"{kind.value.lower()}_{i}",
        kind=kind,
        kind_source=KindSource.EXPLICIT,
        status=status,
        raw_range=ByteRange(0, size),
    )


def _doc() -> TraceDoc:
    spans = [_span(0, None, SpanKind.AGENT, Status.OK, 100)]
    spans += [_span(i, 0, SpanKind.TOOL, Status.OK, 100) for i in range(1, 5)]
    spans += [_span(i, 0, SpanKind.MODEL, Status.OK, 8000) for i in range(5, 8)]
    return TraceDoc(trace_id="t", spans=spans, source_format="mlflow", file_size=10_000)


# ---- 规则差异必须体现在骨架上 ---------------------------------------------


def test_different_rulesets_produce_different_skeletons() -> None:
    doc = _doc()
    keep_all = prune(doc, RuleSet(rules=[]))
    drop_tools = prune(
        doc, RuleSet(rules=[Rule(match=Match(kind=SpanKind.TOOL), action=Action.DROP)])
    )
    assert keep_all.kept_span_count == 8
    assert drop_tools.kept_span_count == 4
    # 被删的 4 个工具节点合并成一个占位节点，而不是凭空消失
    holders = [n for n in drop_tools.all_nodes() if n.collapsed]
    assert sum(h.collapsed_count for h in holders) == 4


def test_name_glob_matching() -> None:
    doc = _doc()
    ruleset = RuleSet(rules=[Rule(match=Match(name_glob="tool_*"), action=Action.DROP)])
    assert prune(doc, ruleset).kept_span_count == 4


def test_min_bytes_matching() -> None:
    doc = _doc()
    ruleset = RuleSet(rules=[Rule(match=Match(min_bytes=1000), action=Action.DROP)])
    # 三个 8000 字节的 MODEL 节点被删
    assert prune(doc, ruleset).kept_span_count == 5


def test_depth_matching() -> None:
    doc = _doc()
    ruleset = RuleSet(rules=[Rule(match=Match(min_depth=1), action=Action.DROP)])
    assert prune(doc, ruleset).kept_span_count == 1


# ---- 冲突与优先级 ---------------------------------------------------------


def test_priority_decides_conflicts() -> None:
    """两条规则都命中同一个 span 时，priority 大的先匹配、首匹配生效。"""
    doc = _doc()
    roots, _ = build_forest(doc.spans)
    depths = compute_depths(roots)

    high_keep = RuleSet(
        rules=[
            Rule(name="keep", match=Match(kind=SpanKind.TOOL), action=Action.KEEP, priority=10),
            Rule(name="drop", match=Match(kind=SpanKind.TOOL), action=Action.DROP, priority=1),
        ]
    )
    decisions = resolve_actions(doc.spans, depths, high_keep)
    assert decisions["0001"].action is Action.KEEP
    assert decisions["0001"].rule is not None
    assert decisions["0001"].rule.name == "keep"

    high_drop = RuleSet(
        rules=[
            Rule(name="keep", match=Match(kind=SpanKind.TOOL), action=Action.KEEP, priority=1),
            Rule(name="drop", match=Match(kind=SpanKind.TOOL), action=Action.DROP, priority=10),
        ]
    )
    assert resolve_actions(doc.spans, depths, high_drop)["0001"].action is Action.DROP


def test_same_priority_keeps_declaration_order() -> None:
    doc = _doc()
    roots, _ = build_forest(doc.spans)
    depths = compute_depths(roots)
    ruleset = RuleSet(
        rules=[
            Rule(name="first", match=Match(), action=Action.KEEP),
            Rule(name="second", match=Match(), action=Action.DROP),
        ]
    )
    decision = resolve_actions(doc.spans, depths, ruleset)["0001"]
    assert decision.rule is not None
    assert decision.rule.name == "first"


def test_unmatched_span_defaults_to_keep() -> None:
    doc = _doc()
    roots, _ = build_forest(doc.spans)
    depths = compute_depths(roots)
    decision = resolve_actions(doc.spans, depths, RuleSet(rules=[]))["0001"]
    assert decision.action is Action.KEEP
    assert decision.rule is None
    assert decision.explicit is False  # 隐式保留不强制保留祖先链


def test_hard_protection_beats_user_config() -> None:
    """用户写「全删」，根节点与 ERROR 路径仍然存活——硬保护关不掉。"""
    spans = [
        _span(0, None, SpanKind.AGENT, Status.OK, 100),
        _span(1, 0, SpanKind.CHAIN, Status.OK, 100),
        _span(2, 1, SpanKind.TOOL, Status.ERROR, 100),
    ]
    doc = TraceDoc(trace_id="t", spans=spans, source_format="mlflow", file_size=300)
    skeleton = prune(doc, RuleSet(rules=[Rule(match=Match(), action=Action.DROP)]))
    kept = {n.meta.span_id for n in skeleton.all_nodes() if not n.collapsed}
    assert kept == {"0000", "0001", "0002"}


# ---- schema 校验：非法配置必须明确失败 ------------------------------------


def test_unknown_action_is_rejected() -> None:
    """拼错 action 必须报错，而不是让这条规则被静默忽略。"""
    with pytest.raises(ConfigError) as exc:
        ruleset_from_dict({"rules": [{"action": "colapse"}]})
    assert exc.value.exit_code == 4
    assert "colapse" in str(exc.value)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ConfigError):
        ruleset_from_dict({"rules": [{"action": "keep", "matchh": {}}]})


def test_negative_max_chars_is_rejected() -> None:
    with pytest.raises(ConfigError):
        ruleset_from_dict({"rules": [{"action": "truncate", "params": {"max_chars": 0}}]})


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ConfigError):
        ruleset_from_dict({"rules": [{"action": "drop", "match": {"kind": "MODLE"}}]})


# ---- TOML 加载 ------------------------------------------------------------


def test_load_ruleset_from_toml(tmp_path: Path) -> None:
    p = tmp_path / "rules.toml"
    p.write_text(
        """
[[rules]]
name = "丢弃小体积成功工具调用"
action = "drop"
priority = 20
match = { kind = "TOOL", status = "OK", max_bytes = 512 }

[[rules]]
name = "大模型调用截断"
action = "truncate"
priority = 10
match = { kind = "MODEL" }
params = { strategy = "head_tail", max_chars = 120 }
""",
        encoding="utf-8",
    )
    ruleset = load_ruleset(p)
    assert len(ruleset.rules) == 2
    assert ruleset.ordered()[0].name == "丢弃小体积成功工具调用"
    assert ruleset.rules[1].effective_params().max_chars == 120


def test_load_missing_file_is_config_error() -> None:
    with pytest.raises(ConfigError) as exc:
        load_ruleset("/nonexistent/rules.toml")
    assert exc.value.exit_code == 4


def test_load_malformed_toml(tmp_path: Path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_ruleset(p)


def test_example_ruleset_is_valid() -> None:
    """仓库里给出的示例配置必须始终可加载——文档与代码不能脱节。"""
    example = Path(__file__).parents[1] / "examples" / "rules.toml"
    ruleset = load_ruleset(example)
    assert ruleset.rules


def test_default_ruleset_truncates_big_model_spans() -> None:
    doc = _doc()
    roots, _ = build_forest(doc.spans)
    depths = compute_depths(roots)
    decisions = resolve_actions(doc.spans, depths, DEFAULT_RULESET)
    assert decisions["0005"].action is Action.TRUNCATE  # 8000 字节的 MODEL
    assert decisions["0001"].action is Action.KEEP  # 100 字节的 TOOL
