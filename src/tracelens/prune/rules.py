"""规则与规则集的 schema（方案 2.1、附录 B16）。

**规则是数据不是代码**——这是 Issue「配置动态调整」要求的实现基础：改配置
重跑即可生效，不需要改代码分支。规则的匹配语义、优先级、冲突处理本身也是可测的。

pydantic 在这里把「配置非法 → 退出码 4」从口号变成机制：拼错
``action = "colapse"`` 会得到「不在枚举 {keep, drop, collapse_subtree, truncate}
中」并指出具体位置，工具以 4 退出。没有 schema 校验的话，这条规则会被静默忽略，
用户以为剪枝生效了、骨架却悄悄变样——配置类工具最阴险的故障模式。
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import ConfigError
from ..model import KindSource, SpanKind, SpanMeta, Status

__all__ = [
    "Action",
    "Match",
    "TruncateParams",
    "Rule",
    "RuleSet",
    "DEFAULT_RULESET",
    "load_ruleset",
    "ruleset_from_dict",
]


class Action(StrEnum):
    """规则动作。"""

    KEEP = "keep"
    """保留节点（默认动作）。"""

    DROP = "drop"
    """删除节点自身；其子节点重挂到最近存活祖先。"""

    COLLAPSE_SUBTREE = "collapse_subtree"
    """保留节点，但把它的整棵子树折叠成一个占位节点。"""

    TRUNCATE = "truncate"
    """保留节点，并按 params 截断它的大体积字段。"""


class Match(BaseModel):
    """匹配条件。多个条件之间是**与**关系；全部为空表示匹配一切。"""

    model_config = ConfigDict(extra="forbid")

    name_glob: str | None = Field(default=None, description="按 span name 做 shell 通配匹配")
    kind: SpanKind | None = Field(default=None, description="节点类型")
    kind_source: KindSource | None = Field(default=None, description="类型判断的来源等级")
    status: Status | None = Field(default=None, description="执行状态")
    min_bytes: int | None = Field(default=None, ge=0, description="span 原文字节数下限")
    max_bytes: int | None = Field(default=None, ge=0, description="span 原文字节数上限")
    min_depth: int | None = Field(default=None, ge=0, description="树深度下限（根为 0）")
    max_depth: int | None = Field(default=None, ge=0, description="树深度上限")

    def matches(self, span: SpanMeta, depth: int) -> bool:
        """判断某个 span 在给定深度下是否命中本条件。"""
        if self.name_glob is not None and not fnmatchcase(span.name, self.name_glob):
            return False
        if self.kind is not None and span.kind is not self.kind:
            return False
        if self.kind_source is not None and span.kind_source is not self.kind_source:
            return False
        if self.status is not None and span.status is not self.status:
            return False
        if self.min_bytes is not None and span.payload_bytes < self.min_bytes:
            return False
        if self.max_bytes is not None and span.payload_bytes > self.max_bytes:
            return False
        if self.min_depth is not None and depth < self.min_depth:
            return False
        return not (self.max_depth is not None and depth > self.max_depth)


class TruncateParams(BaseModel):
    """截断参数（方案 §5.4、附录 A7）。

    三种策略跟着信息分布走：prompt 的系统指令在开头（head），错误栈的根因在
    结尾（tail），工具输出常常开头是格式说明、结尾是结论（head_tail）。
    """

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["head", "tail", "head_tail"] = "head"
    max_chars: int = Field(default=200, gt=0, description="保留的字符数")
    field_globs: list[str] = Field(
        default_factory=list,
        description="要截断的字段路径通配；为空表示该 span 的全部大体积字段",
    )


class Rule(BaseModel):
    """一条规则。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", description="规则名，仅用于报错与调试")
    match: Match = Field(default_factory=Match)
    action: Action = Action.KEEP
    params: TruncateParams | None = Field(default=None, description="仅 action=truncate 时有意义")
    priority: int = Field(default=0, description="数值越大越先匹配；同值按声明顺序")

    def effective_params(self) -> TruncateParams:
        return self.params or TruncateParams()


class RuleSet(BaseModel):
    """规则集。求解语义：按 priority 降序排序后**首匹配生效**。"""

    model_config = ConfigDict(extra="forbid")

    rules: list[Rule] = Field(default_factory=list)

    def ordered(self) -> list[Rule]:
        """按优先级降序排序；同优先级保持声明顺序（Python 排序是稳定的）。"""
        return sorted(self.rules, key=lambda r: -r.priority)


# 内置默认规则集：小体量 Trace 上几乎不剪，大 payload 一律截断。
# 注意「根节点与 ERROR 路径永不剪除」这条硬保护**不在这里**——它写死在引擎里，
# 用户配置无法关掉它（方案 2.2）。
DEFAULT_RULESET = RuleSet(
    rules=[
        Rule(
            name="错误节点保留全文",
            match=Match(status=Status.ERROR),
            action=Action.KEEP,
            priority=100,
        ),
        Rule(
            name="大体积模型调用截断输入输出",
            match=Match(kind=SpanKind.MODEL, min_bytes=2048),
            action=Action.TRUNCATE,
            params=TruncateParams(strategy="head", max_chars=200),
            priority=50,
        ),
        Rule(
            name="大体积工具输出两端保留",
            match=Match(kind=SpanKind.TOOL, min_bytes=2048),
            action=Action.TRUNCATE,
            params=TruncateParams(strategy="head_tail", max_chars=200),
            priority=50,
        ),
        Rule(
            name="其余大体积节点统一截断",
            match=Match(min_bytes=2048),
            action=Action.TRUNCATE,
            params=TruncateParams(strategy="head", max_chars=200),
            priority=10,
        ),
    ]
)


def ruleset_from_dict(data: dict[str, Any]) -> RuleSet:
    """从字典构造规则集，校验失败统一抛 `ConfigError`（退出码 4）。"""
    try:
        return RuleSet.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"规则集校验失败：\n{exc}") from exc


def load_ruleset(path: str | Path) -> RuleSet:
    """从 TOML 文件加载规则集。

    文件形如：

    .. code-block:: toml

        [[rules]]
        name = "丢弃小体积成功工具调用"
        action = "drop"
        priority = 20
        match = { kind = "TOOL", status = "OK", max_bytes = 512 }
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"规则文件不存在：{p}")
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"规则文件不是合法 TOML：{p}\n{exc}") from exc
    return ruleset_from_dict(data)
