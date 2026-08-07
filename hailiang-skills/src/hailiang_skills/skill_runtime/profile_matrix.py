from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import re
from pathlib import Path

from hailiang_skills.skill_runtime.models import SkillBundle


@dataclass(slots=True, frozen=True)
class ProfileMatrixEntry:
    stage: str
    score_band: str
    talent_flag: str
    persona_summary: str
    strategy: str
    scenes: tuple[str, ...]
    source_row: int


@dataclass(slots=True, frozen=True)
class ProfileMatrixRecommendation:
    stage: str
    score_band: str = ""
    talent_flag: str = ""
    recommended_scenes: tuple[str, ...] = ()
    matched_entries: tuple[ProfileMatrixEntry, ...] = ()


def load_profile_matrix_entries(bundle: SkillBundle) -> tuple[ProfileMatrixEntry, ...]:
    config = bundle.runtime_metadata.planner.scene_selection
    reference_path = config.matrix_reference.strip()
    if not reference_path:
        return ()
    markdown_text = bundle.references.get(reference_path, "")
    if not markdown_text:
        return ()
    return _parse_profile_matrix(reference_path, markdown_text)


def recommend_scenes_from_profile_matrix(
    bundle: SkillBundle,
    *,
    stage: str,
    score_band: str = "",
    talent_flag: str = "",
) -> ProfileMatrixRecommendation | None:
    normalized_stage = stage.strip()
    if not normalized_stage:
        return None
    entries = load_profile_matrix_entries(bundle)
    if not entries:
        return None
    normalized_score_band = score_band.strip()
    normalized_talent_flag = talent_flag.strip()
    matched_entries = tuple(
        entry
        for entry in entries
        if entry.stage == normalized_stage
        and (not normalized_score_band or entry.score_band == normalized_score_band)
        and (not normalized_talent_flag or entry.talent_flag == normalized_talent_flag)
    )
    if not matched_entries:
        return None
    scene_counter: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for entry_index, entry in enumerate(matched_entries):
        for scene in entry.scenes:
            scene_counter[scene] += 1
            first_seen.setdefault(scene, entry_index)
    ranked_scenes = tuple(
        scene
        for scene, _count in sorted(
            scene_counter.items(),
            key=lambda item: (-item[1], first_seen.get(item[0], 0), item[0]),
        )
    )
    return ProfileMatrixRecommendation(
        stage=normalized_stage,
        score_band=normalized_score_band,
        talent_flag=normalized_talent_flag,
        recommended_scenes=ranked_scenes,
        matched_entries=matched_entries,
    )


@lru_cache(maxsize=32)
def _parse_profile_matrix(reference_path: str, markdown_text: str) -> tuple[ProfileMatrixEntry, ...]:
    del reference_path
    lines = [line.rstrip() for line in markdown_text.splitlines()]
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("|") and "可探索场景" in line),
        -1,
    )
    if header_index < 0 or header_index + 2 > len(lines):
        return ()
    entries: list[ProfileMatrixEntry] = []
    for row_index, line in enumerate(lines[header_index + 2 :], start=1):
        if not line.startswith("|"):
            continue
        columns = _split_markdown_row(line)
        if len(columns) < 8:
            continue
        stage, score_band, talent_flag, persona_summary, _questions, _prompt, strategy, scenes_text = columns[:8]
        entries.append(
            ProfileMatrixEntry(
                stage=_clean_cell_text(stage),
                score_band=_clean_cell_text(score_band),
                talent_flag=_clean_cell_text(talent_flag),
                persona_summary=_clean_cell_text(persona_summary),
                strategy=_clean_cell_text(strategy),
                scenes=_split_scene_cell(scenes_text),
                source_row=row_index,
            )
        )
    return tuple(entries)


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [part.strip() for part in stripped.split("|")]


def _split_scene_cell(cell_text: str) -> tuple[str, ...]:
    normalized = cell_text.replace("<br>", "\n")
    parts = [
        _clean_cell_text(part)
        for part in normalized.splitlines()
        if _clean_cell_text(part)
    ]
    return tuple(parts)


def _clean_cell_text(value: str) -> str:
    text = value.strip()
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"`+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
