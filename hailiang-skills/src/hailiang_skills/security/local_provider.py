from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from hailiang_skills.security.lexicon_loader import Lexicon
from hailiang_skills.security.models import ModerationResult


_ZERO_WIDTH = re.compile("[\\u200b-\\u200f\\u202a-\\u202e\\ufeff]")


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = _ZERO_WIDTH.sub("", normalized)
    normalized = normalized.replace("\\r\\n", "\\n").replace("\\r", "\\n")
    return normalized.casefold()


class LocalLexiconProvider:
    def __init__(self, lexicon: Lexicon) -> None:
        self.lexicon = lexicon
        self._trie: dict = {}
        high_risk_sources = {"政治类型.txt", "反动词库.txt", "暴恐词库.txt", "涉枪涉爆.txt", "色情词库.txt", "非法网址.txt"}
        for entry in lexicon.entries:
            term = normalize_text(entry.term)
            if not term:
                continue
            if len(term) == 1 and entry.source_file not in high_risk_sources:
                continue
            node = self._trie
            for char in term:
                node = node.setdefault(char, {})
            node.setdefault("\0", []).append((entry.term, entry.source_file))

    def check(self, content: str) -> ModerationResult:
        normalized = normalize_text(content)
        hits: list[tuple[int, int, str, str]] = []
        for start in range(len(normalized)):
            node = self._trie
            for index in range(start, len(normalized)):
                node = node.get(normalized[index])
                if node is None:
                    break
                for original, source in node.get("\0", []):
                    hits.append((start, index + 1, original, source))
        hits.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        return ModerationResult(
            matched=bool(hits),
            risk_level="high" if hits else "none",
            labels=sorted({Path(source).stem for _, _, _, source in hits}),
            provider="local",
            mode="local_fallback",
            lexicon_version=self.lexicon.version,
            source_files=sorted({source for _, _, _, source in hits}),
            matched_text_hashes=sorted({f"sha256:{hashlib.sha256(term.encode('utf-8')).hexdigest()}" for _, _, term, _ in hits}),
            match_positions=[{"start": start, "end": end} for start, end, _, _ in hits[:50]],
        )
