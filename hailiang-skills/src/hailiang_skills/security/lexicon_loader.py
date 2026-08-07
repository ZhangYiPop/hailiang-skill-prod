from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    term: str
    source_file: str


@dataclass(frozen=True, slots=True)
class Lexicon:
    entries: tuple[LexiconEntry, ...]
    version: str
    file_count: int


def load_lexicon(directory: str | Path) -> Lexicon:
    root = Path(directory).expanduser().resolve()
    entries: dict[str, set[str]] = {}
    file_hash = hashlib.sha256()
    files = sorted(root.glob("*.txt"))
    for path in files:
        data = path.read_bytes()
        file_hash.update(path.name.encode("utf-8"))
        file_hash.update(b"\0")
        file_hash.update(data)
        source = path.name
        for raw in data.decode("utf-8", errors="replace").splitlines():
            term = raw.strip()
            if term:
                entries.setdefault(term, set()).add(source)
    flattened = tuple(
        LexiconEntry(term=term, source_file=source)
        for term in sorted(entries)
        for source in sorted(entries[term])
    )
    return Lexicon(entries=flattened, version=f"sha256:{file_hash.hexdigest()}", file_count=len(files))

