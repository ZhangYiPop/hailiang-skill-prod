from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_embedding_text(text: str) -> str:
    return str(text or "").strip()


@dataclass(slots=True, frozen=True)
class EmbeddingCacheRecord:
    text_hash: str
    skill_id: str
    scene_name: str
    source: str
    text: str
    vector: tuple[float, ...]
    model: str
    base_url: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "text_hash": self.text_hash,
            "skill_id": self.skill_id,
            "scene_name": self.scene_name,
            "source": self.source,
            "text": self.text,
            "vector": list(self.vector),
            "model": self.model,
            "base_url": self.base_url,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True, frozen=True)
class EmbeddingCacheSnapshot:
    records: dict[str, EmbeddingCacheRecord]
    stale_removed_count: int = 0


def build_text_hash(*, skill_id: str, source: str, text: str, model: str, base_url: str) -> str:
    payload = "||".join(
        [
            str(skill_id or "").strip(),
            str(source or "").strip(),
            normalize_embedding_text(text),
            str(model or "").strip(),
            str(base_url or "").strip(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FileEmbeddingCache:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.examples_path = self.root_dir / "examples.json"
        self.manifest_path = self.root_dir / "manifest.json"

    def load(self) -> EmbeddingCacheSnapshot:
        if not self.examples_path.exists():
            return EmbeddingCacheSnapshot(records={})
        try:
            payload = json.loads(self.examples_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return EmbeddingCacheSnapshot(records={})
        items = payload.get("records", [])
        records: dict[str, EmbeddingCacheRecord] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                text_hash = str(item.get("text_hash") or "").strip()
                vector_raw = item.get("vector")
                if not text_hash or not isinstance(vector_raw, list):
                    continue
                try:
                    vector = tuple(float(value) for value in vector_raw)
                except (TypeError, ValueError):
                    continue
                records[text_hash] = EmbeddingCacheRecord(
                    text_hash=text_hash,
                    skill_id=str(item.get("skill_id") or ""),
                    scene_name=str(item.get("scene_name") or ""),
                    source=str(item.get("source") or ""),
                    text=str(item.get("text") or ""),
                    vector=vector,
                    model=str(item.get("model") or ""),
                    base_url=str(item.get("base_url") or ""),
                    updated_at=str(item.get("updated_at") or ""),
                )
        return EmbeddingCacheSnapshot(records=records)

    def write(
        self,
        *,
        records: dict[str, EmbeddingCacheRecord],
        model: str,
        base_url: str,
        example_count: int,
        stale_removed_count: int,
    ) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        serialized = [records[key].as_dict() for key in sorted(records)]
        self.examples_path.write_text(
            json.dumps({"records": serialized}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = {
            "version": 1,
            "model": model,
            "base_url": base_url,
            "example_count": int(example_count),
            "record_count": len(records),
            "stale_removed_count": int(stale_removed_count),
            "updated_at": _utc_now_iso(),
        }
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
