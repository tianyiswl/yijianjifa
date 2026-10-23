from __future__ import annotations

_SENTENCE_ENDINGS = frozenset("。！？.!?")


def split_spoken_sentences(text: str) -> list[str]:
    """Split spoken copy only at sentence-ending punctuation.

    Commas and line wrapping/newlines remain inside a sentence. Trailing text
    without punctuation is returned as the final sentence.
    """
    sentences: list[str] = []
    buffer: list[str] = []
    for char in str(text or ""):
        buffer.append(char)
        if char in _SENTENCE_ENDINGS:
            value = "".join(buffer).strip()
            if value:
                sentences.append(value)
            buffer.clear()
    tail = "".join(buffer).strip()
    if tail:
        sentences.append(tail)
    return sentences


def split_subtitle_chunks(text: str, limit: int) -> list[str]:
    """Prefer punctuation boundaries, then balance long clauses within one line."""
    import math
    import re
    chunks = []
    for clause in re.findall(r'[^，,；;：:。！？!?\n]+[，,；;：:。！？!?\n]*|[，,；;：:。！？!?\n]+', text):
        count = max(1, math.ceil(len(clause) / limit))
        size, extra = divmod(len(clause), count)
        offset = 0
        for index in range(count):
            length = size + (index < extra)
            chunks.append(clause[offset:offset + length])
            offset += length
    return chunks
