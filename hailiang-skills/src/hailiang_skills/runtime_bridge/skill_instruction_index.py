"""Lightweight, business-agnostic index of evidence rules written in SKILL.md."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_REFERENCE = re.compile(
    r"(?:\[)?((?:references/)?[^\s\]）)】,，;；:：]+\.(?:md|txt|json|csv|ya?ml))(?:\])?",
    re.IGNORECASE,
)
_DIRECTIVE = re.compile(r"(?:\bMUST\b|必须|禁止|仅(?:在|允许)|只(?:在|能)|不得|严禁|强制)", re.IGNORECASE)
_SENSITIVE = re.compile(r"(?:结论|推荐|清单|详情|方案|结果|校准|分析|规则|报告|列表|优势|赛事|政策|条件)")


@dataclass(frozen=True, slots=True)
class ReferenceInstruction:
    path: str
    section: str
    directive: str


@dataclass(frozen=True, slots=True)
class SkillInstructionIndex:
    """Only indexes explicit author text; it never embeds business semantics."""

    references: tuple[ReferenceInstruction, ...]
    prohibitions: tuple[str, ...]

    @property
    def has_explicit_evidence_rules(self) -> bool:
        return bool(self.references or self.prohibitions)

    def is_sensitive_turn(self, *, draft: str, stage: str, next_action: str, user_message: str) -> bool:
        if not self.has_explicit_evidence_rules:
            return False
        material = "\n".join((draft, stage, next_action, user_message))
        # A long, structured candidate reply is also evidence-sensitive even
        # when its stage name is business-defined and contains no common term.
        return bool(_SENSITIVE.search(material)) or "|" in draft or len(draft.strip()) >= 240


def build_skill_instruction_index(markdown: str, *, available_reference_paths: set[str] | None = None) -> SkillInstructionIndex:
    """Extract direct local-reference obligations and prohibitions from Markdown.

    The parser intentionally preserves author wording. A model later decides
    whether an instruction applies to this turn; this index merely avoids a
    model call for Skills that do not express any evidence constraints.
    """
    available = {str(path).replace("\\", "/") for path in (available_reference_paths or set())}
    section = "SKILL.md"
    references: list[ReferenceInstruction] = []
    prohibitions: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        heading = _HEADING.match(line)
        if heading:
            section = heading.group(1)
            continue
        if not line:
            continue
        if _DIRECTIVE.search(line):
            prohibitions.append(f"[{section}] {line}"[:1200])
        for match in _REFERENCE.finditer(line):
            raw_path = match.group(1).replace("\\", "/")
            path = raw_path if raw_path.startswith("references/") else f"references/{PurePosixPath(raw_path).name}"
            if available and path not in available:
                # Keep authored paths only when they name a real, local
                # reference. This prevents prose examples from becoming a
                # fake loading obligation.
                continue
            directive = f"[{section}] {line}"[:1200]
            key = (path, section, directive)
            if key not in seen:
                seen.add(key)
                references.append(ReferenceInstruction(path, section, directive))
    return SkillInstructionIndex(tuple(references), tuple(dict.fromkeys(prohibitions)))
