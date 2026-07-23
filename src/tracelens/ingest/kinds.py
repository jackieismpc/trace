"""SpanKind 的分级推断（方案 §二、附录 A4）。

类型信息的来源可靠性参差不齐，必须把「有多确定」和「是什么」一起交付给下游。
优先级由高到低：

    EXPLICIT    mlflow.spanType —— 数据里显式声明，最可信
    CONVENTION  OTel GenAI 语义约定 gen_ai.*（仍属 Experimental）、
                OpenInference 的 openinference.span.kind
    HEURISTIC   span name 的正则启发式，骨架里打 ⚠ 标记
    UNKNOWN     推不出来，如实标注

之所以不追求「推断零错误」，是因为兜底手段是 `KindSource` 可信度标记：
一个叫 ``search_tool`` 的 span 被正则猜成 TOOL 多半是对的，但如果它实际是
个子 Agent，读者会用「工具出错」的思路去排查「模型出错」的问题。标了
Heuristic，Agent 至少知道这条推理链的地基是软的。
"""

from __future__ import annotations

import re

from ..model import KindSource, SpanKind

__all__ = ["infer_kind", "kind_from_mlflow_span_type", "MLFLOW_SPAN_TYPE_MAP"]

# MLflow 的 spanType 取值 → IR 的 SpanKind。
# 放成配置表而不是硬编码分支，上游改名时只需动这张表（方案 R2 的缓解落点）。
MLFLOW_SPAN_TYPE_MAP: dict[str, SpanKind] = {
    "LLM": SpanKind.MODEL,
    "CHAT_MODEL": SpanKind.MODEL,
    "AGENT": SpanKind.AGENT,
    "TOOL": SpanKind.TOOL,
    "CHAIN": SpanKind.CHAIN,
    "RETRIEVER": SpanKind.RETRIEVER,
    "PARSER": SpanKind.PARSER,
    "EMBEDDING": SpanKind.EMBEDDING,
    "RERANKER": SpanKind.RERANKER,
    "UNKNOWN": SpanKind.UNKNOWN,
}

# OpenInference 的 span kind 取值 → SpanKind
OPENINFERENCE_KIND_MAP: dict[str, SpanKind] = {
    "LLM": SpanKind.MODEL,
    "AGENT": SpanKind.AGENT,
    "TOOL": SpanKind.TOOL,
    "CHAIN": SpanKind.CHAIN,
    "RETRIEVER": SpanKind.RETRIEVER,
    "RERANKER": SpanKind.RERANKER,
    "EMBEDDING": SpanKind.EMBEDDING,
}

# 最后一档：span name 的正则启发式。按顺序首匹配。
_NAME_PATTERNS: tuple[tuple[re.Pattern[str], SpanKind], ...] = (
    (re.compile(r"(?i)(^|[_\-.])(agent|planner|executor)([_\-.]|$)"), SpanKind.AGENT),
    (re.compile(r"(?i)(llm|chat|completion|gpt|claude|qwen|deepseek|generate)"), SpanKind.MODEL),
    (re.compile(r"(?i)(tool|call_|invoke|search|query|fetch|exec)"), SpanKind.TOOL),
    (re.compile(r"(?i)(retriev|vector|index_search)"), SpanKind.RETRIEVER),
    (re.compile(r"(?i)(embed)"), SpanKind.EMBEDDING),
    (re.compile(r"(?i)(rerank)"), SpanKind.RERANKER),
    (re.compile(r"(?i)(chain|pipeline|graph|workflow)"), SpanKind.CHAIN),
    (re.compile(r"(?i)(pars|format|render)"), SpanKind.PARSER),
)


def kind_from_mlflow_span_type(value: str) -> SpanKind:
    """把 MLflow 的 spanType 字符串映射到 SpanKind；未知取值归 UNKNOWN。"""
    return MLFLOW_SPAN_TYPE_MAP.get(value.strip().strip('"').upper(), SpanKind.UNKNOWN)


def _from_conventions(attrs: dict[str, object]) -> SpanKind | None:
    """按标准/第三方语义约定推断。"""
    oi = attrs.get("openinference.span.kind")
    if isinstance(oi, str):
        hit = OPENINFERENCE_KIND_MAP.get(oi.strip().strip('"').upper())
        if hit is not None:
            return hit

    op = attrs.get("gen_ai.operation.name")
    if isinstance(op, str):
        low = op.strip().strip('"').lower()
        if low in ("chat", "text_completion", "generate_content"):
            return SpanKind.MODEL
        if low == "embeddings":
            return SpanKind.EMBEDDING
        if low in ("execute_tool", "tool"):
            return SpanKind.TOOL
        if low in ("invoke_agent", "create_agent"):
            return SpanKind.AGENT

    # 没有 operation.name 但带了模型请求属性的，仍可判为一次模型调用
    if any(k.startswith("gen_ai.request.") or k.startswith("gen_ai.usage.") for k in attrs):
        return SpanKind.MODEL
    if "tool.name" in attrs or "gen_ai.tool.name" in attrs:
        return SpanKind.TOOL
    return None


def _from_name(name: str) -> SpanKind | None:
    for pat, kind in _NAME_PATTERNS:
        if pat.search(name):
            return kind
    return None


def infer_kind(
    name: str,
    attrs: dict[str, object],
    explicit: str | None = None,
) -> tuple[SpanKind, KindSource]:
    """分级推断节点类型，返回 ``(类型, 来源等级)``。

    :param name: span 名称，仅用于最后一档启发式
    :param attrs: 已提取的 attribute 字典
    :param explicit: 显式声明的类型字符串（MLflow 的 ``mlflow.spanType``）
    """
    if explicit:
        kind = kind_from_mlflow_span_type(explicit)
        if kind is not SpanKind.UNKNOWN:
            return kind, KindSource.EXPLICIT

    conv = _from_conventions(attrs)
    if conv is not None:
        return conv, KindSource.CONVENTION

    heur = _from_name(name)
    if heur is not None:
        return heur, KindSource.HEURISTIC

    return SpanKind.UNKNOWN, KindSource.UNKNOWN
