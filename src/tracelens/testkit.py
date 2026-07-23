"""合成 Trace 生成器：供属性测试、快照测试与性能基准共用（方案 §5.1）。

只依赖标准库——它是运行时包的一部分（`tracelens demo` 与基准脚本都会用），
因此不能引入 hypothesis 之类的开发依赖。hypothesis 策略定义在 ``tests/`` 下，
以本模块为底座。

生成的数据刻意贴近真实 Trace 的体积分布：少数 LLM span 携带巨大的 prompt，
多数 TOOL span 体积很小——「信息密度最高的部分体积最小」是整个方案成立的前提
（方案 §一），合成数据必须复现这个性质，否则基准与自验数据都不可信。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

__all__ = ["SynthConfig", "SynthSpan", "build_spans", "build_mlflow_trace", "build_otlp_trace"]

_KINDS = ("AGENT", "LLM", "TOOL", "CHAIN", "RETRIEVER")

_LOREM = (
    "根据检索结果，2025 年 Q3 的营收情况如下所述。The quick brown fox jumps over the lazy dog. "
)


@dataclass(slots=True)
class SynthConfig:
    """合成参数。"""

    span_count: int = 40
    """总 span 数（含根节点）。"""

    max_depth: int = 4
    max_children: int = 4

    small_payload_chars: int = 64
    """普通 span 的 payload 字符数。"""

    large_payload_chars: int = 4096
    """MODEL span 的 payload 字符数——刻意做大，复现真实 Trace 的体积分布。"""

    large_ratio: float = 0.2
    """携带大 payload 的 span 占比。"""

    error_ratio: float = 0.05
    """注入 ERROR 状态的 span 占比。"""

    unicode_payload: bool = True
    """payload 中混入中文与 emoji，用于 UTF-8 截断用例。"""

    pretty: bool = False
    """输出是否带缩进（紧凑/pretty 两种排版都要能被扫描器正确处理）。"""

    seed: int = 0


@dataclass(slots=True)
class SynthSpan:
    """合成出的单个 span 的中立描述，再由各格式的 builder 落成具体 JSON。"""

    span_id: str
    parent_id: str | None
    name: str
    span_type: str
    status: str
    start_ns: int
    end_ns: int
    inputs: str
    outputs: str
    children: list[str] = field(default_factory=list)


def _payload(rng: random.Random, chars: int, unicode_mix: bool) -> str:
    """造一段指定长度的 payload 文本。"""
    base = _LOREM if unicode_mix else "lorem ipsum dolor sit amet. "
    reps = max(1, chars // len(base) + 1)
    text = (base * reps)[:chars]
    if unicode_mix and chars > 16:
        # 混入 ZWJ emoji 与组合字符，专门用于截断边界用例
        text = text[:-8] + "👨‍👩‍👧é中"
    return text + rng.choice(("", " ", "。"))


def build_spans(cfg: SynthConfig) -> list[SynthSpan]:
    """按配置合成一棵 span 树（确定性：同 seed 同输出）。"""
    rng = random.Random(cfg.seed)
    spans: list[SynthSpan] = []
    # 树的构造：维护一个「可作为父节点」的候选列表，按深度限制取用
    depths: dict[str, int] = {}
    t = 1_700_000_000_000_000_000

    def new_id(i: int) -> str:
        return f"{i + 1:016x}"

    root = SynthSpan(
        span_id=new_id(0),
        parent_id=None,
        name="root_agent",
        span_type="AGENT",
        status="OK",
        start_ns=t,
        end_ns=t + 48_200_000_000,
        inputs=_payload(rng, cfg.small_payload_chars, cfg.unicode_payload),
        outputs=_payload(rng, cfg.small_payload_chars, cfg.unicode_payload),
    )
    spans.append(root)
    depths[root.span_id] = 0

    for i in range(1, cfg.span_count):
        candidates = [s for s in spans if depths[s.span_id] < cfg.max_depth - 1]
        parent = rng.choice(candidates) if candidates else root
        sid = new_id(i)
        span_type = rng.choice(_KINDS)
        is_large = span_type == "LLM" or rng.random() < cfg.large_ratio
        chars = cfg.large_payload_chars if is_large else cfg.small_payload_chars
        status = "ERROR" if rng.random() < cfg.error_ratio else "OK"
        start = t + i * 1_000_000
        span = SynthSpan(
            span_id=sid,
            parent_id=parent.span_id,
            name=f"{span_type.lower()}_{i}",
            span_type=span_type,
            status=status,
            start_ns=start,
            end_ns=start + rng.randint(1, 5_000) * 1_000_000,
            inputs=_payload(rng, chars, cfg.unicode_payload),
            outputs=_payload(rng, chars, cfg.unicode_payload),
        )
        spans.append(span)
        depths[sid] = depths[parent.span_id] + 1
        parent.children.append(sid)

    return spans


def build_mlflow_trace(cfg: SynthConfig | None = None) -> bytes:
    """合成一份 MLflow 形态的 trace JSON。"""
    cfg = cfg or SynthConfig()
    spans = build_spans(cfg)
    trace_id = "0" * 31 + "1"
    doc = {
        "info": {
            "trace_id": trace_id,
            "request_id": "tr-synthetic",
            "execution_time_ms": 48200,
        },
        "data": {
            "spans": [
                {
                    "name": s.name,
                    "context": {"trace_id": trace_id, "span_id": s.span_id},
                    "parent_id": s.parent_id,
                    "start_time": s.start_ns,
                    "end_time": s.end_ns,
                    "status_code": s.status,
                    "status_message": (
                        'relation "revenue_q3" does not exist' if s.status == "ERROR" else ""
                    ),
                    "attributes": {
                        "mlflow.spanType": json.dumps(s.span_type),
                        "mlflow.spanInputs": json.dumps({"query": s.inputs}),
                        "mlflow.spanOutputs": json.dumps({"content": s.outputs}),
                    },
                }
                for s in spans
            ]
        },
    }
    return _dump(doc, cfg.pretty)


def build_otlp_trace(cfg: SynthConfig | None = None) -> bytes:
    """合成一份 OTLP JSON 形态的 trace。"""
    cfg = cfg or SynthConfig()
    spans = build_spans(cfg)
    trace_id = "0" * 31 + "1"
    doc = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "synthetic-agent"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "tracelens.testkit"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": s.span_id,
                                "parentSpanId": s.parent_id or "",
                                "name": s.name,
                                "startTimeUnixNano": str(s.start_ns),
                                "endTimeUnixNano": str(s.end_ns),
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {
                                            "stringValue": (
                                                "chat" if s.span_type == "LLM" else "execute_tool"
                                            )
                                        },
                                    },
                                    {
                                        "key": "gen_ai.prompt",
                                        "value": {"stringValue": s.inputs},
                                    },
                                    {
                                        "key": "gen_ai.completion",
                                        "value": {"stringValue": s.outputs},
                                    },
                                ],
                                "status": {
                                    "code": 2 if s.status == "ERROR" else 1,
                                    "message": (
                                        'relation "revenue_q3" does not exist'
                                        if s.status == "ERROR"
                                        else ""
                                    ),
                                },
                            }
                            for s in spans
                        ],
                    }
                ],
            }
        ]
    }
    return _dump(doc, cfg.pretty)


def _dump(doc: object, pretty: bool) -> bytes:
    """序列化。``ensure_ascii=False`` 让中文以原样字节落盘，更贴近真实导出文件。"""
    if pretty:
        return json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
