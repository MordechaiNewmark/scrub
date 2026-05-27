"""Plain-text adapter. Extraction is identity; reassembly rebuilds the string
from the original text + a list of (start, end, replacement) edits."""
from __future__ import annotations

from typing import List, Tuple


def extract(raw_bytes: bytes) -> str:
    return raw_bytes.decode("utf-8", errors="replace")


def reassemble(text: str, edits: List[Tuple[int, int, str]]) -> bytes:
    """edits: list of (start, end, replacement) in the original text offsets."""
    edits = sorted(edits, key=lambda e: e[0])
    out = []
    cursor = 0
    for start, end, repl in edits:
        out.append(text[cursor:start])
        out.append(repl)
        cursor = end
    out.append(text[cursor:])
    return "".join(out).encode("utf-8")
