"""四层配置叠加与校验（方案 §5.6）。

    内置默认 < TOML 文件 < 环境变量 TRACELENS_* < CLI 参数

这个次序对应三种实际用法：临时调试用 CLI 覆盖一切，CI 用环境变量，
团队约定沉淀在配置文件里。最终结果统一灌进 pydantic 模型收口，
校验失败 → `ConfigError` → 退出码 4。

Issue 要求的「配置动态调整」由此天然满足：规则是数据不是代码，改配置重跑即可。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError
from .prune.rules import DEFAULT_RULESET, RuleSet

__all__ = ["Config", "ENV_PREFIX", "load_config"]

ENV_PREFIX = "TRACELENS_"


class Config(BaseModel):
    """一次运行的完整配置。"""

    model_config = ConfigDict(extra="forbid")

    format: Literal["tree", "json", "md"] = "tree"
    max_tokens: int | None = Field(default=None, gt=0, description="骨架的 token 预算")
    strict_grapheme: bool = Field(
        default=False, description="按 grapheme cluster 截断（需 [grapheme] extra）"
    )
    exact_tokens: bool = Field(
        default=False, description="用 tiktoken 精确计数（需 [tokens] extra）"
    )
    chars_per_token: float = Field(default=4.0, gt=0, description="token 估算系数")
    rules: RuleSet = Field(default_factory=lambda: DEFAULT_RULESET)


def _from_env(env: dict[str, str]) -> dict[str, Any]:
    """从环境变量取配置项。只认识已声明的字段，未知的 TRACELENS_* 一律忽略。"""
    out: dict[str, Any] = {}
    bool_fields = {"strict_grapheme", "exact_tokens"}
    for name in ("format", "max_tokens", "strict_grapheme", "exact_tokens", "chars_per_token"):
        raw = env.get(ENV_PREFIX + name.upper())
        if raw is None:
            continue
        if name in bool_fields:
            out[name] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif name == "max_tokens":
            try:
                out[name] = int(raw)
            except ValueError as exc:
                raise ConfigError(f"环境变量 {ENV_PREFIX}MAX_TOKENS 不是整数：{raw}") from exc
        elif name == "chars_per_token":
            try:
                out[name] = float(raw)
            except ValueError as exc:
                raise ConfigError(f"环境变量 {ENV_PREFIX}CHARS_PER_TOKEN 不是数字：{raw}") from exc
        else:
            out[name] = raw
    return out


def _from_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在：{p}")
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 TOML：{p}\n{exc}") from exc

    out: dict[str, Any] = {}
    for key in ("format", "max_tokens", "strict_grapheme", "exact_tokens", "chars_per_token"):
        if key in data:
            out[key] = data[key]
    if "rules" in data:
        # 规则集与其余配置项同文件；schema 校验一并交给 Config 完成
        out["rules"] = {"rules": data["rules"]}
    unknown = set(data) - {
        "format",
        "max_tokens",
        "strict_grapheme",
        "exact_tokens",
        "chars_per_token",
        "rules",
    }
    if unknown:
        raise ConfigError(f"配置文件里有未知的顶层字段：{', '.join(sorted(unknown))}")
    return out


def load_config(
    config_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """按四层次序叠加并校验，产出最终配置。

    :param config_path: TOML 配置/规则文件；None 时看环境变量 ``TRACELENS_CONFIG``
    :param cli_overrides: CLI 显式给出的项（值为 None 的键会被忽略，
        因为「没写这个参数」和「把它设成空」是两回事）
    """
    env = dict(os.environ) if env is None else env
    layered: dict[str, Any] = {}

    path = config_path or env.get(ENV_PREFIX + "CONFIG")
    if path:
        layered.update(_from_file(path))

    layered.update(_from_env(env))

    for key, value in (cli_overrides or {}).items():
        if value is not None:
            layered[key] = value

    try:
        return Config.model_validate(layered)
    except ValidationError as exc:
        raise ConfigError(f"配置校验失败：\n{exc}") from exc
