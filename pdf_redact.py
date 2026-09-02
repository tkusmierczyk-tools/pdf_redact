#!/usr/bin/env python3
"""
redact.py — remove literal terms / regex matches from a PDF and stamp
a replacement string in their place.

Requires: pymupdf >= 1.24   (pip install pymupdf)
Note: PyMuPDF is AGPL-3.0 unless you hold a commercial licence.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pymupdf as fitz  # `import fitz` also works on older versions


# ---------------------------------------------------------------- terms file

def load_terms(path: Path) -> tuple[list[str], list[re.Pattern]]:
    """
    One entry per line. Blank lines and lines starting with '#' are ignored.
    A line prefixed with 're:' is compiled as a regular expression;
    everything else is treated as a literal string.
    """
    literals: list[str] = []
    patterns: list[re.Pattern] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            patterns.append(re.compile(line[3:], re.IGNORECASE))
        else:
            literals.append(line)

    return literals, patterns


# ---------------------------------------------------------------- matching

def find_literal(page, term: str) -> list[fitz.Rect]:
    """
    MuPDF's search is case-insensitive and tolerant of ligatures and
    whitespace runs. It will NOT match text broken across a line wrap.
    """
    return page.search_for(term)


def find_regex(page, pattern: re.Pattern) -> list[fitz.Rect]:
    """
    Word-level regex matching. page.get_text("words") returns tuples of
    (x0, y0, x1, y1, word, block_no, line_no, word_no).

    This matches within single whitespace-delimited tokens only — fine for
    emails, IBANs, national ID numbers; not for multi-word phrases.
    """
    hits = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        if pattern.search(word):
            hits.append(fitz.Rect(x0, y0, x1, y1))
    return hits


# ---------------------------------------------------------------- redaction

def fit_fontsize(rect: fitz.Rect, text: str, cap: float = 11.0) -> float:
    """
    If the replacement doesn't fit the box, PyMuPDF silently draws nothing.
    Estimate a size that fits: Helvetica averages ~0.5 em per character.
    """
    if not text:
        return cap
    by_width = rect.width / (0.5 * len(text))
    by_height = rect.height * 0.8
    return max(4.0, min(cap, by_width, by_height))


def redact_page(page, literals, patterns, symbol: str, match_length: bool) -> int:
    rects: list[fitz.Rect] = []
    labels: list[str] = []

    for term in literals:
        for r in find_literal(page, term):
            rects.append(r)
            labels.append(symbol * len(term) if match_length else symbol)

    for pat in patterns:
        for r in find_regex(page, pat):
            rects.append(r)
            labels.append(symbol * 5 if match_length else symbol)

    for rect, label in zip(rects, labels):
        page.add_redact_annot(
            rect,
            text=label,
            fontname="helv",
            fontsize=fit_fontsize(rect, label),
            align=fitz.TEXT_ALIGN_LEFT,
            fill=(1, 1, 1),        # background painted over the removed area
            text_color=(0, 0, 0),
            cross_out=False,       # default True draws diagonal strike lines
        )

    if rects:
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_PIXELS,   # blank pixels under a hit
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        except (AttributeError, TypeError):
            page.apply_redactions()   # older PyMuPDF: fewer keyword options

    return len(rects)


def redact_widgets(page, literals, patterns, symbol: str) -> int:
    """
    Form-field values live outside the page content stream and survive
    apply_redactions(). Handle them separately.
    """
    n = 0
    for w in page.widgets() or []:
        val = w.field_value
        if not isinstance(val, str) or not val:
            continue
        hit = any(t.lower() in val.lower() for t in literals) or \
              any(p.search(val) for p in patterns)
        if hit:
            w.field_value = symbol * 5
            w.update()
            n += 1
    return n


# ---------------------------------------------------------------- verify

def verify(path: Path, literals, patterns) -> list[str]:
    """Re-open the written file and confirm nothing survived."""
    leftovers = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text()
            low = text.lower()
            for t in literals:
                if t.lower() in low:
                    leftovers.append(f"page {i}: literal {t!r}")
            for p in patterns:
                if p.search(text):
                    leftovers.append(f"page {i}: pattern {p.pattern!r}")
    return leftovers


# ---------------------------------------------------------------- driver

def main() -> int:
    ap = argparse.ArgumentParser(description="Redact terms from a PDF.")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("terms", type=Path, help="file of terms, one per line")
    ap.add_argument("-o", "--out", type=Path, help="default: <pdf>.redacted.pdf")
    ap.add_argument("-s", "--symbol", default="x")
    ap.add_argument("--fixed", action="store_true",
                    help="stamp the symbol verbatim instead of repeating it "
                         "to the length of the match")
    ap.add_argument("--keep-metadata", action="store_true")
    args = ap.parse_args()

    out = args.out or args.pdf.with_suffix(".redacted.pdf")
    literals, patterns = load_terms(args.terms)
    if not literals and not patterns:
        print("No terms loaded.", file=sys.stderr)
        return 2

    doc = fitz.open(args.pdf)

    if doc.is_encrypted and not doc.authenticate(""):
        print("PDF is password-protected.", file=sys.stderr)
        return 2

    total = 0
    for i, page in enumerate(doc, start=1):
        n = redact_page(page, literals, patterns, args.symbol, not args.fixed)
        n += redact_widgets(page, literals, patterns, args.symbol)
        total += n
        if n:
            print(f"page {i}: {n} redaction(s)")

    if not args.keep_metadata:
        doc.set_metadata({})       # /Info dictionary
        doc.del_xml_metadata()     # XMP packet
        doc.set_toc([])            # clear all bookmarks

    # Non-incremental save with full garbage collection. This is the step that
    # actually drops the superseded objects; an incremental save would leave
    # the original text recoverable in an earlier revision of the file.
    doc.save(out, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()

    print(f"\n{total} redaction(s) -> {out}")

    leftovers = verify(out, literals, patterns)
    if leftovers:
        print("\nVERIFICATION FAILED — text survived:", file=sys.stderr)
        for x in leftovers:
            print("  " + x, file=sys.stderr)
        return 1

    print("Verification passed: no target text extractable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
