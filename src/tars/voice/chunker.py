"""Split streaming model text into speakable phrases.

The first phrase goes to the voice as soon as it is speakable (a sentence end,
or a clause break after about 8 words, or 12 words regardless), so speech can
start before the model has finished replying.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"[.!?…](?:[\"')\]]*)\s")
_CLAUSE_BREAK = re.compile(r"[,;:—–](?:[\"')\]]*)\s")


class PhraseChunker:
    def __init__(self, clause_words: int = 8, max_words: int = 12, min_sentence_words: int = 1):
        self.clause_words = clause_words
        self.max_words = max_words
        self.min_sentence_words = min_sentence_words
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out = []
        while (phrase := self._take()) is not None:
            out.append(phrase)
        return out

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        return rest

    def _take(self) -> str | None:
        buf = self._buf
        for match in _SENTENCE_END.finditer(buf):
            if len(buf[: match.end()].split()) >= self.min_sentence_words:
                return self._cut(match.end())
        for match in _CLAUSE_BREAK.finditer(buf):
            if len(buf[: match.end()].split()) >= self.clause_words:
                return self._cut(match.end())
        words = list(re.finditer(r"\S+\s", buf))
        if len(words) >= self.max_words:
            limit = words[self.max_words - 1].end()
            # Prefer the last clause break inside the limit, if it leaves a real phrase.
            breaks = [m.end() for m in _CLAUSE_BREAK.finditer(buf[:limit]) if len(buf[: m.end()].split()) >= 4]
            return self._cut(breaks[-1] if breaks else limit)
        return None

    def _cut(self, end: int) -> str:
        phrase, self._buf = self._buf[:end], self._buf[end:]
        return phrase.strip()
