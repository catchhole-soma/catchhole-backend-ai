"""Token-sized exact original slices; offsets are Unicode code points, not bytes."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from hashlib import sha256
import re

import tiktoken

POLICY = "cl100k-800-100-boundary-v1"
TARGET_TOKENS = 800
OVERLAP_TOKENS = 100


@dataclass(frozen=True)
class RetrievalSlice:
    index: int
    start: int
    end: int
    text: str
    tokens: int
    content_hash: str


class RetrievalSplitter:
    policy = POLICY

    def __init__(self):
        self.encoding = tiktoken.get_encoding("cl100k_base")

    def count(self, text: str) -> int:
        return len(self.encoding.encode(text, disallowed_special=()))

    def split(self, text: str) -> list[RetrievalSlice]:
        if not text.strip():
            return []
        # Keep whitespace/CRLF/emoji verbatim. BPE token bytes may split Hangul, so
        # decode neither individual tokens nor token windows to create source text.
        boundaries = [
            m.end() for m in re.finditer(r"(?:\r\n|[\r\n])|[.!?。！？][\"\'”’)]*\s+", text)
        ]
        chunks, start = [], 0
        while start < len(text):
            # A token represents at most a few bytes in normal prose; bounded
            # character windows also avoid repeatedly tokenizing the whole novel.
            end = min(len(text), start + TARGET_TOKENS * 16)
            while self.count(text[start:end]) > TARGET_TOKENS:
                end = start + max(1, (end - start) * 3 // 4)
            if end == start:
                raise ValueError("Cannot split source")
            # Fill incrementally; re-encode the actual concatenated text because
            # token counts of separately encoded sentences are not additive.
            low, high = end, min(len(text), start + TARGET_TOKENS * 16)
            while low + 1 < high:
                mid = (low + high) // 2
                if self.count(text[start:mid]) <= TARGET_TOKENS:
                    low = mid
                else:
                    high = mid
            end = high if self.count(text[start:high]) <= TARGET_TOKENS else low
            if end < len(text):
                boundary_index = bisect_right(boundaries, end) - 1
                # Do not reduce an otherwise useful chunk to one tiny paragraph.
                if boundary_index >= 0 and boundaries[boundary_index] > start:
                    boundary = boundaries[boundary_index]
                    if self.count(text[start:boundary]) >= TARGET_TOKENS // 2:
                        end = boundary
            value = text[start:end]
            if value.strip():
                chunks.append(
                    RetrievalSlice(
                        len(chunks),
                        start,
                        end,
                        value,
                        self.count(value),
                        sha256(value.encode("utf-8")).hexdigest(),
                    )
                )
            else:
                # Discarded whitespace needs no overlap with the following chunk.
                start = end
                continue
            if end == len(text):
                break
            overlap = end
            while overlap > start + 1 and self.count(text[overlap - 1 : end]) <= OVERLAP_TOKENS:
                overlap -= 1
            boundary_index = bisect_left(boundaries, overlap)
            start = (
                boundaries[boundary_index]
                if boundary_index < len(boundaries) and boundaries[boundary_index] < end
                else overlap
            )
        return chunks
