"""内部规范化 IR（中间表示）定义。

选型结论（方案 §二、附录 A3）：以 OpenTelemetry 的语义模型作为 IR。
MLflow Tracing 构建在 OTel SDK 之上，其 Span 就是 OTel Span，
所以 MLflow → OTel 可无损映射，反向不成立；选 OTel 做 IR 不丢信息。

本模块是整个包的依赖底座：**零内部依赖**，其余所有模块单向依赖它，
彼此互不依赖，由 `cli` 负责组装（方案 §5.1 的依赖纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SpanKind(StrEnum):
    """节点类型。

    取值对齐 MLflow 的 spanType 与 OTel GenAI 语义约定的公共子集。
    推断不出来时如实标 ``UNKNOWN``——不知道就说不知道，
    比猜一个好看的答案有价值（方案 §二）。
    """

    AGENT = "AGENT"
    MODEL = "MODEL"
    TOOL = "TOOL"
    CHAIN = "CHAIN"
    RETRIEVER = "RETRIEVER"
    PARSER = "PARSER"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    UNKNOWN = "UNKNOWN"


class KindSource(StrEnum):
    """`SpanKind` 的判断来源可信度等级（方案附录 A4）。

    下游读者是 LLM。如果把猜测伪装成事实，Agent 会沿着错误的类型判断
    走错整条排查路线，而且错得毫无征兆。所以类型必须和「有多确定」一起交付。

    优先级由高到低：
        EXPLICIT   —— 数据里显式声明（如 ``mlflow.spanType``）
        CONVENTION —— 标准语义约定推出（如 ``gen_ai.*``，仍属 Experimental）
        HEURISTIC  —— span name 正则启发式猜的，骨架里打 ``⚠`` 标记
        UNKNOWN    —— 推不出来
    """

    EXPLICIT = "Explicit"
    CONVENTION = "Convention"
    HEURISTIC = "Heuristic"
    UNKNOWN = "Unknown"


class Status(StrEnum):
    """Span 执行状态。"""

    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


@dataclass(slots=True, frozen=True)
class ByteRange:
    """原始文件中的字节区间 ``[start, end)``。

    整条通路的记账单位强制为 **bytes**，绝不混用 str 偏移——
    ``len("中") == 1`` 但它在 UTF-8 里占 3 字节，混用会切出错位垃圾
    （方案附录 B13）。
    """

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(slots=True)
class SpanMeta:
    """单个 span 的元数据。

    这是「高密度低体积」的那一半：拓扑、类型、状态、耗时、体积。
    真正占体积的 Payload 不在这里，只以 `raw_range` 的形式留一个字节指针。
    """

    span_id: str
    """span 的十六进制标识（原始字符串形态，用于索引与展开寻址）。"""

    parent_id: str | None
    """父 span 标识；根节点为 None。"""

    trace_id: str
    name: str
    kind: SpanKind
    kind_source: KindSource
    status: Status

    start_ns: int = 0
    """起始时间戳（纳秒）。缺失时为 0。"""

    end_ns: int = 0

    input_bytes: int = 0
    """输入 Payload 的字节数（未截断前的原始体积）。"""

    output_bytes: int = 0
    status_message: str = ""
    """状态描述；ERROR 时通常是错误信息，骨架会展示其首行。"""

    raw_range: ByteRange = field(default_factory=lambda: ByteRange(0, 0))
    """本 span 对象在原始文件中的字节区间——expand 的全部依据。"""

    attributes: dict[str, object] = field(default_factory=dict)
    """提取出的少量关键 attribute（模型名、token 用量等），不含大 Payload。"""

    @property
    def duration_ns(self) -> int:
        """耗时（纳秒）。时间戳缺失或倒挂时返回 0。"""
        return max(0, self.end_ns - self.start_ns)

    @property
    def payload_bytes(self) -> int:
        """本 span 在原文件中占的总字节数。"""
        return self.raw_range.length


@dataclass(slots=True)
class TraceDoc:
    """一份 Trace 的解析结果：span 元数据列表 + 来源信息。"""

    trace_id: str
    spans: list[SpanMeta]
    source_format: str
    """``mlflow`` / ``otlp``；由 `ingest.sniff` 判定。"""

    file_size: int = 0

    @property
    def span_count(self) -> int:
        return len(self.spans)

    def status(self) -> Status:
        """整体状态：任一 span 为 ERROR 则整体 ERROR。"""
        if any(s.status is Status.ERROR for s in self.spans):
            return Status.ERROR
        return Status.OK


# eq=False：节点按**对象身份**比较，不按字段值。
# 拓扑阶段用 `node in siblings` / `siblings.remove(node)` 定位节点，靠的就是身份；
# 且树里可能出现成环的脏数据，逐字段的 `__eq__` 会在 children 上无限递归。
# 身份比较既是正确语义，也回避了这个隐患。
@dataclass(slots=True, eq=False)
class SpanNode:
    """剪枝阶段使用的树节点，包裹 `SpanMeta` 并携带剪枝产生的附加信息。"""

    meta: SpanMeta
    children: list[SpanNode] = field(default_factory=list)

    elided_depth: int = 0
    """本节点与其存活父节点之间被折叠掉的层数。

    删掉中间节点时，子节点重挂到最近的存活祖先，并在边上留下这个计数——
    否则骨架会让人误以为调用链是扁平的，而一个会撒谎的骨架比没有骨架更糟
    （方案 §一）。
    """

    truncated_fields: list[TruncationMark] = field(default_factory=list)
    """本节点上被截断的字段标记。"""

    collapsed: bool = False
    """本节点是否为「折叠若干同类兄弟」而生成的占位节点。"""

    collapsed_count: int = 0
    collapsed_kind: SpanKind = SpanKind.UNKNOWN
    collapsed_all_ok: bool = True

    def walk(self) -> list[SpanNode]:
        """先序遍历自身与全部后代。"""
        out = [self]
        for c in self.children:
            out.extend(c.walk())
        return out


@dataclass(slots=True)
class TruncationMark:
    """截断标记：自描述且可寻址（方案 §5.4、附录 A7）。

    ``expand_hint`` 把「怎么取回」的知识直接写进给 LLM 的数据里，
    Agent 不需要额外的 system prompt 教它怎么展开。
    """

    span_id: str
    field_path: str
    original_chars: int
    kept_chars: int
    strategy: str
    digest: str
    expand_hint: str
    preview: str = ""
    """截断后保留下来的内容，渲染骨架时展示。"""

    def to_dict(self) -> dict[str, object]:
        """序列化成骨架 JSON 里的 ``__truncated__`` 结构。"""
        return {
            "span_id": self.span_id,
            "field": self.field_path,
            "original_chars": self.original_chars,
            "kept_chars": self.kept_chars,
            "strategy": self.strategy,
            "digest": self.digest,
            "expand_hint": self.expand_hint,
        }


@dataclass(slots=True)
class Skeleton:
    """剪枝后的骨架：一片森林 + 统计信息。"""

    trace_id: str
    roots: list[SpanNode]
    original_span_count: int
    kept_span_count: int
    status: Status = Status.OK
    duration_ns: int = 0
    source_file: str = ""
    source_size: int = 0
    notes: list[str] = field(default_factory=list)
    """渲染时附在末尾的说明（如 token 估算方法、预算收紧过程）。"""

    detail: int = 2
    """展示详细度，由 ``--max-tokens`` 的预算收紧循环调节：

        2 完整——截断字段显示保留下来的预览与可执行的 expand 命令
        1 紧凑——只显示一行截断标记（字段路径 + 原始/保留长度）
        0 最简——只在节点行留一个 ``✂``，取回方式见骨架末尾的图例

    降低详细度只影响**展示**，不影响可取回性：``span_id`` 与字段路径始终在骨架里，
    ``tracelens expand --span-id <id> --field <path>`` 永远有效。
    """

    def all_nodes(self) -> list[SpanNode]:
        out: list[SpanNode] = []
        for r in self.roots:
            out.extend(r.walk())
        return out
