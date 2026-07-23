"""四层配置叠加测试：内置默认 < TOML < 环境变量 < CLI。

这个次序对应三种实际用法：临时调试用 CLI 覆盖一切，CI 用环境变量，
团队约定沉淀在配置文件里。次序错了不会报错、只会悄悄用错配置，
所以每一层的覆盖关系都要单独测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracelens.config import ENV_PREFIX, Config, load_config
from tracelens.errors import ConfigError


def test_defaults() -> None:
    cfg = load_config(env={})
    assert cfg.format == "tree"
    assert cfg.max_tokens is None
    assert cfg.strict_grapheme is False
    assert cfg.rules.rules  # 内置默认规则集


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "conf.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_file_layer(tmp_path: Path) -> None:
    p = _write(tmp_path, 'format = "md"\nmax_tokens = 1000\n')
    cfg = load_config(config_path=p, env={})
    assert cfg.format == "md"
    assert cfg.max_tokens == 1000


def test_env_overrides_file(tmp_path: Path) -> None:
    p = _write(tmp_path, 'format = "md"\n')
    cfg = load_config(config_path=p, env={ENV_PREFIX + "FORMAT": "json"})
    assert cfg.format == "json"


def test_cli_overrides_env(tmp_path: Path) -> None:
    p = _write(tmp_path, 'format = "md"\n')
    cfg = load_config(
        config_path=p,
        cli_overrides={"format": "tree"},
        env={ENV_PREFIX + "FORMAT": "json"},
    )
    assert cfg.format == "tree"


def test_none_cli_values_do_not_override() -> None:
    """「没写这个参数」和「把它设成空」是两回事。"""
    cfg = load_config(cli_overrides={"format": None}, env={ENV_PREFIX + "FORMAT": "md"})
    assert cfg.format == "md"


def test_env_config_path(tmp_path: Path) -> None:
    p = _write(tmp_path, 'format = "json"\n')
    cfg = load_config(env={ENV_PREFIX + "CONFIG": str(p)})
    assert cfg.format == "json"


def test_env_bool_and_number() -> None:
    cfg = load_config(
        env={
            ENV_PREFIX + "STRICT_GRAPHEME": "true",
            ENV_PREFIX + "MAX_TOKENS": "4000",
            ENV_PREFIX + "CHARS_PER_TOKEN": "3.5",
        }
    )
    assert cfg.strict_grapheme is True
    assert cfg.max_tokens == 4000
    assert cfg.chars_per_token == 3.5


def test_unknown_env_vars_are_ignored() -> None:
    cfg = load_config(env={ENV_PREFIX + "NOT_A_REAL_OPTION": "x"})
    assert cfg.format == "tree"


def test_rules_come_from_same_file(tmp_path: Path) -> None:
    """规则集与其余配置项写在同一个文件里。"""
    p = _write(
        tmp_path,
        """
format = "json"

[[rules]]
action = "drop"
match = { kind = "TOOL" }
""",
    )
    cfg = load_config(config_path=p, env={})
    assert cfg.format == "json"
    assert len(cfg.rules.rules) == 1


# ---- 非法配置一律退出码 4 --------------------------------------------------


def test_missing_file_is_config_error() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(config_path="/nonexistent.toml", env={})
    assert exc.value.exit_code == 4


def test_bad_toml(tmp_path: Path) -> None:
    p = _write(tmp_path, "= = =")
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


def test_unknown_top_level_key(tmp_path: Path) -> None:
    p = _write(tmp_path, 'formatt = "tree"\n')
    with pytest.raises(ConfigError, match="未知的顶层字段"):
        load_config(config_path=p, env={})


def test_bad_format_value(tmp_path: Path) -> None:
    p = _write(tmp_path, 'format = "yaml"\n')
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


def test_bad_env_number() -> None:
    with pytest.raises(ConfigError):
        load_config(env={ENV_PREFIX + "MAX_TOKENS": "many"})
    with pytest.raises(ConfigError):
        load_config(env={ENV_PREFIX + "CHARS_PER_TOKEN": "x"})


def test_config_rejects_extra_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017  pydantic 的 ValidationError
        Config.model_validate({"nope": 1})
