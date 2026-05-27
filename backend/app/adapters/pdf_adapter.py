"""
PDF adapter (PyMuPDF / fitz).

PDFs are the dangerous format. The classic mistake is drawing a black rectangle
over text — the visible page looks redacted but the text is still selectable and
extractable underneath. That is a data leak, not a redaction.

PyMuPDF does it correctly: add_redact_annot() marks a region, and
apply_redactions() actually REMOVES the underlying text/image content within it.
We rely on that for true removal.

Extraction strategy:
  - We extract text per page with positional info ("words") so we can locate
    each finding's bounding box(es) to redact.
  - The normalized flat text is page text joined with "\n", matching what
    detection sees.

Two policies behave differently in a PDF:
  - redact: mark the span's rect(s) and apply_redactions -> text is gone, black box.
  - fake:   we can't easily "type into" the original layout, so we redact the
            original span (removing it) and then insert the fake string as new
            text at the span's location. This keeps the document visually intact
            and the original PII genuinely removed.

NOTE: this works for text-based PDFs. Scanned/image PDFs need OCR first
(Tesseract), which is a separate, larger feature deliberately out of v1 scope.
A scanned PDF will yield little/no extractable text and the API flags that.
"""
from __future__ import annotations

import io
from typing import List, Tuple

import fitz  # PyMuPDF


class PdfState:
    def __init__(self, raw_bytes: bytes):
        self.doc = fitz.open(stream=raw_bytes, filetype="pdf")
        self.text, self.char_to_page = self._build_text_map()

    def _build_text_map(self):
        """Flat text across pages + a parallel array mapping each char index
        to its page number, so we can find which page a finding lives on."""
        parts = []
        char_to_page = []
        for pno, page in enumerate(self.doc):
            page_text = page.get_text("text")
            parts.append(page_text)
            char_to_page.extend([pno] * len(page_text))
            parts.append("\n")
            char_to_page.append(pno)
        return "".join(parts), char_to_page

    def is_probably_scanned(self) -> bool:
        return len(self.text.strip()) < 20 and self.doc.page_count > 0


def extract(raw_bytes: bytes) -> Tuple[str, "PdfState"]:
    state = PdfState(raw_bytes)
    return state.text, state


def reassemble(state: "PdfState", edits: List[Tuple[int, int, str]],
               policies: List[str]) -> bytes:
    """
    edits: (start, end, replacement) on the flat text.
    policies: parallel list giving 'redact' or 'fake' for each edit
              (so we know whether to reinsert a fake string after removal).
    """
    doc = state.doc

    # Group work per page. For each edit, locate the matched text on its page
    # via search, mark redaction, and (for fake) remember the text to reinsert.
    # We search by the original substring text because PyMuPDF gives us rects
    # for a string occurrence on a page.
    reinsert = {}  # pno -> list of (rect, fake_text, fontsize)

    for (start, end, repl), policy in zip(edits, policies):
        if start >= len(state.char_to_page):
            continue
        pno = state.char_to_page[start]
        page = doc[pno]
        original = state.text[start:end]
        needle = original.strip()
        if not needle:
            continue

        rects = page.search_for(needle, quads=False)
        if not rects:
            continue
        for rect in rects:
            page.add_redact_annot(rect, fill=(0, 0, 0) if policy == "redact" else (1, 1, 1))
            if policy == "fake":
                # estimate a font size from the rect height
                fontsize = max(6, min(12, rect.height * 0.8))
                reinsert.setdefault(pno, []).append((rect, repl, fontsize))

    # Apply removals page by page (this is the step that truly deletes text).
    for page in doc:
        page.apply_redactions()

    # Reinsert fake text where requested (after redactions, onto cleared areas).
    for pno, items in reinsert.items():
        page = doc[pno]
        for rect, fake_text, fontsize in items:
            page.insert_textbox(
                rect, fake_text,
                fontsize=fontsize, fontname="helv",
                color=(0, 0, 0), align=0,
            )

    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)  # garbage=4 scrubs removed objects
    return buf.getvalue()
