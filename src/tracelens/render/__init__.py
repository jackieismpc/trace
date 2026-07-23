"""render 层：同一份骨架的三种形式。

tree.py     缩进树形文本，默认，给 LLM 看，token 效率最高
jsonout.py  稳定 schema，给程序消费、可 diff
md.py       给人看，可直接贴进 PR
budget.py   --max-tokens 的预算收紧循环
"""

from .budget import fit_to_budget
from .jsonout import render_json
from .md import render_md
from .tree import render_tree

__all__ = ["render_tree", "render_json", "render_md", "fit_to_budget", "render"]


def render(skeleton: object, fmt: str = "tree") -> str:
    """按格式名渲染骨架。"""
    from ..errors import InputError
    from ..model import Skeleton

    assert isinstance(skeleton, Skeleton)
    if fmt == "tree":
        return render_tree(skeleton)
    if fmt == "json":
        return render_json(skeleton)
    if fmt == "md":
        return render_md(skeleton)
    raise InputError(f"未知的输出格式：{fmt}")
