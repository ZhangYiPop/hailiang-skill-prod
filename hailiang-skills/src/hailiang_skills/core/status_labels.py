from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re


_HIDDEN_LABELS = {"生成回复", "正在生成回复"}
_LABELS = {
    "意图判断": "正在识别本轮需求",
    "推理规划": "正在制定规划思路",
    "加载资料": "正在整理相关资料",
    "正在组织回答": "正在组织回答",
    # This is an explicit pre-answer product stage, so keep the intentional
    # "正在" wording instead of applying the generic prefix cleanup below.
    "正在总结信息": "正在总结信息",
}


def normalize_status_label(value: object) -> str:
    """Return the concise wording used by the reasoning-progress UI."""
    label = str(value or "").strip()
    if not label:
        return ""
    if label in _HIDDEN_LABELS:
        return ""
    normalized = label.replace(" ", "")
    if normalized in _HIDDEN_LABELS:
        return ""
    if normalized in _LABELS:
        return _LABELS[normalized]
    if label.startswith("正在"):
        label = label[2:].strip()
    return label or "推进本轮规划"


def normalize_ms_agent_progress_label(value: object, *, fallback: str = "推进本轮规划") -> str:
    """Normalize an MS-Agent thinking step for the user-facing intent label."""
    label = normalize_status_label(value)
    if not label:
        label = normalize_status_label(fallback)
    if label.startswith("正在"):
        label = label[2:].strip()
    # The prefix is part of the product copy, so reserve ten characters for
    # the action and keep the complete label within twelve characters.
    return f"正在{label[:15]}"


def redact_skill_file_names(value: object, skill_root: str | Path | None) -> str:
    """Replace current Skill file names before exposing a runtime status label."""
    label = str(value or "").strip()
    if not label or skill_root is None:
        return label
    file_names = _skill_file_names(str(Path(skill_root).expanduser().resolve()))
    for file_name in file_names:
        label = re.sub(re.escape(file_name), "内部文件", label, flags=re.IGNORECASE)
    return label


@lru_cache(maxsize=64)
def _skill_file_names(skill_root: str) -> tuple[str, ...]:
    root = Path(skill_root)
    if not root.is_dir():
        return ()
    names: set[str] = set()
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.name:
            names.add(file_path.name)
        # Stems make references such as `path_catalog` safe as well, while
        # avoiding tiny tokens that would over-redact ordinary status text.
        if len(file_path.stem) >= 3:
            names.add(file_path.stem)
    return tuple(sorted(names, key=len, reverse=True))
