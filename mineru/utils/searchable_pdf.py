# Copyright (c) Opendatalab. All rights reserved.
"""Export a MinerU recognition result as a "searchable PDF": each page of the
*original* PDF is rasterised into a background image, and an invisible text
layer (positioned from ``*_middle.json`` line/span bboxes) is placed on top so
users can select and copy the recognised text with a normal PDF viewer.

Rasterising every page also "repairs" PDFs whose embedded text layer is
corrupted (mojibake): the old text layer is discarded entirely and the page is
rebuilt from scratch as image + recognised text.

Coordinate system
------------------
``*_middle.json`` line/span ``bbox`` values are already expressed in the same
point-based coordinate space as the page's own ``page_size`` (top-left origin,
y growing downward) -- this was verified numerically against a real hybrid
backend result, where the page's bbox extents matched ``page_size`` almost
exactly, and ``page_size`` itself matched the source PDF page's ``page.rect``
(via PyMuPDF) to within rounding. This is the same coordinate space PyMuPDF
uses for ``Page.insert_text``/``Page.rect``, so bboxes can be used directly as
insertion coordinates without any rescaling -- *unlike* ``content_list.json``,
whose bboxes live in a different (larger) coordinate space for the hybrid
backend, see ``mineru.utils.docx_export._page_reference_box``.

Each ``middle.json`` block normally holds exactly one "line" per paragraph
(its bbox equal to the block bbox). Depending on the backend, the paragraph's
physical line breaks may be encoded as ``"\\n"`` inside the span's ``content``
string -- or (hybrid backend, verified numerically on real output) the whole
multi-line paragraph is stored as ONE long string with no ``"\\n"`` at all,
so the block bbox is the only geometry available. Block-level bboxes alone
cannot position individual words, therefore placement is *ink-guided*: the
page render (already produced for the background image) is binarised, the
block bbox is split into physical line bands via a horizontal ink projection,
the words are distributed across those bands, and inside each band a vertical
ink projection yields per-word runs that the words are snapped to (with an
individual horizontal stretch per word). When ``"\\n"`` markers ARE present
they take precedence over the ink heuristic for deciding which text belongs
to which line. When ink segmentation yields nothing usable (empty/noisy
block, numpy unavailable), placement falls back to the legacy behaviour:
split on ``"\\n"`` with the bbox height divided evenly, one morphed line per
sub-band.

Known limitations
------------------
- Table cell text has no per-cell bbox in ``middle.json`` (the whole
  ``table_body`` block shares one bbox for the entire grid); its rows are
  matched to ink line bands when the counts agree, otherwise they are laid
  out one row per even sub-band spanning the full table width, so per-word
  alignment inside table cells stays coarse.
- Word-to-ink-run matching is count-based: words are assigned to line bands
  in reading order using each band's actual detected ink word runs (see
  ``_ink_word_runs``), so a band gets as many words as it has runs, only
  falling back to the old proportional (by font metrics) distribution when
  the total run count across all bands diverges from the paragraph's word
  count by more than 10% (segmentation likely failed for that block, e.g.
  heavy noise or touching words). The whole visual line is still inserted as
  a single ``insert_text`` run per band, spanning from the start of the
  band's first ink run to the end of its last -- only *which* words are
  assigned to which band changed, not how a band's run is rendered.
- Two known residual gaps in the count-matching heuristic (see
  ``_bucket_words_by_ink_runs``), both rare relative to the dominant
  boundary-hyphenation case it does correct: (1) when a band's ink is
  over-segmented (e.g. a token with internal punctuation, such as
  "В.1.2-14-2009", split into two runs by the same gap heuristic that
  separates words) and this exactly offsets a *different* band's ink being
  under-segmented, the total run/word count matches by coincidence and no
  correction is attempted, so the affected word can still land one band
  off; (2) the correction that does fire only inspects each band's *last*
  run for an anomaly, so an internal over-split elsewhere in a band (not at
  its edge) is not specifically targeted -- widening the check to "narrowest
  run anywhere in the band" was tried and reverted, as it false-positives on
  legitimate short words/punctuation and regressed the common case.
- Rotated text blocks (``angle`` != 0) are inserted unrotated (best effort,
  logged) and skip ink-guided placement.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import fitz
from loguru import logger

try:  # numpy is a core MinerU dependency; guarded anyway so a broken install
    import numpy as np  # only degrades placement quality instead of crashing.
except ImportError:  # pragma: no cover - defensive
    np = None

MIDDLE_JSON_SUFFIX = "_middle.json"
ORIGIN_PDF_SUFFIX = "_origin.pdf"

# --- font sizing tuning ---------------------------------------------------
# Tuned against real ink-band heights (see module verification): 0.82 puts the
# median word_h/ink_band_h ratio around 0.95-1.05 (extracted glyph bbox close
# to the actual ink height) without pushing q90 |dy| baseline error past ~1pt.
_FONT_LINE_FRAC = 0.82
_FONT_MIN_PT, _FONT_MAX_PT = 3.0, 28.0
# Horizontal stretch/squeeze applied via a morph matrix so the inserted text
# roughly spans the original bbox width; clamped to avoid degenerate glyphs.
_SCALE_X_MIN, _SCALE_X_MAX = 0.35, 3.0

# --- ink-guided placement tuning --------------------------------------------
# The background render is binarised (gray < _INK_THRESHOLD == "ink") and the
# text layer is aligned to the actual ink: a horizontal projection inside each
# block bbox splits it into physical line bands, a vertical projection inside
# each band splits it into per-word ink runs. All *_PT values are in points.
_INK_THRESHOLD = 128
# A pixel row belongs to a line band when its ink-pixel count exceeds this
# fraction of the block width (filters speckle noise between lines).
_BAND_ROW_FRAC = 0.03
# Line bands separated by a gap smaller than this are merged (diacritics,
# broken glyph outlines split by binarisation).
_BAND_MERGE_GAP_PT = 1.5
# Line bands shorter than this are discarded (specks, horizontal rules).
_BAND_MIN_HEIGHT_PT = 2.5
# A pixel column belongs to a word run when its ink-pixel count exceeds this
# fraction of the band height.
_RUN_COL_FRAC = 0.05
# Columns gaps wider than this fraction of the band height split word runs
# (inter-letter gaps are narrower than inter-word spaces at any font size).
_WORD_GAP_FRAC = 0.33
# Horizontal search padding around the block bbox when scanning for ink runs
# (detected line bands may start slightly outside the reported block bbox).
_RUN_X_PAD_PT = 3.0
# Merged ink runs narrower than this (points) are noise/punctuation slivers
# rather than standalone words (a lone diacritic or period detached from its
# glyph by binarisation) and are absorbed into the nearest neighbouring run
# instead of being reported as their own word run. Kept well below the width
# of any real single-letter word (e.g. Ukrainian "і", "у", "в") at ordinary
# scan resolutions/font sizes.
_RUN_MIN_WIDTH_PT = 1.0
# Reference font size used only for *relative* word-width weighting when
# distributing words across line bands.
_DISTRIBUTE_REF_FONTSIZE = 10.0

# --- background image tuning -----------------------------------------------
_DEFAULT_DPI = 200
_JPEG_QUALITY = 80
# Sampling stride (in pixels) used to decide whether a rendered page is
# effectively grayscale (common for B/W scans, sometimes with a small colored
# stamp/logo) so it can be re-encoded in the DeviceGray colorspace instead of
# RGB, cutting the embedded image size roughly 3x without a visible quality
# loss on the (overwhelmingly gray) text/background. A page is treated as
# grayscale when fewer than _GRAYSCALE_MAX_COLOR_FRACTION of the sampled
# pixels have a max(R,G,B)-min(R,G,B) spread above _GRAYSCALE_CHANNEL_TOLERANCE.
_GRAYSCALE_SAMPLE_STEP = 23
_GRAYSCALE_CHANNEL_TOLERANCE = 6
_GRAYSCALE_MAX_COLOR_FRACTION = 0.02

# Candidate TrueType fonts with broad Unicode (incl. Cyrillic/Greek) coverage,
# checked in order; the first one found on disk is embedded as the *primary*
# text-layer font. Kept deliberately small (a few hundred KB) since it is
# embedded in every generated searchable PDF and covers the primary use case
# (Cyrillic/Latin scanned documents) without the multi-megabyte cost of a CJK
# font. Covers both the Linux container image (Noto, installed via
# fonts-noto-core in docker/*/Dockerfile) and local Windows/Mac dev setups.
_PRIMARY_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
]

# PyMuPDF built-in CJK font used as fallback for lines whose characters the
# primary font can't encode (see ``_font_for_text``). "china-s" is MuPDF's
# bundled Droid Sans Fallback: it covers CJK *and* Cyrillic/Latin, and MuPDF
# writes it as a NON-embedded CID font, so the fallback adds ~1 KB to the
# output instead of a multi-megabyte embedded CJK font (system CJK TTC fonts
# like Noto Sans CJK are CFF-based, which ``Document.subset_fonts`` cannot
# subset -- embedding them bloated the PDF by ~14 MB). Since the text layer is
# invisible (render_mode=3), the non-embedded font never affects rendering,
# only copy/search behaviour.
_FALLBACK_BUILTIN_FONT = "china-s"

_FONT_NAME_PRIMARY = "mineru-searchable"


def _resolve_font(candidates: list[str]) -> Path | None:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


class _FallbackFontState:
    """Lazily-loaded built-in CJK fallback font, resolved at most once per export."""

    def __init__(self) -> None:
        self.attempted = False
        self.font: "fitz.Font | None" = None

    def get(self) -> "fitz.Font | None":
        if not self.attempted:
            self.attempted = True
            try:
                self.font = fitz.Font(_FALLBACK_BUILTIN_FONT)
            except Exception as exc:
                logger.warning(
                    f"Failed to load built-in CJK fallback font {_FALLBACK_BUILTIN_FONT!r}: {exc}"
                )
        return self.font


def _line_covered_by_font(text: str, font: "fitz.Font") -> bool:
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            continue
        try:
            if not font.has_glyph(ord(ch)):
                return False
        except Exception:
            return False
    return True


def _font_for_text(
    text: str,
    primary_font: "fitz.Font | None",
    primary_fontfile: str | None,
    fallback_state: "_FallbackFontState",
) -> tuple["fitz.Font | None", str | None, str]:
    """Pick the primary (embedded) or built-in CJK-fallback font for one line
    of text, based on Unicode glyph coverage (``fitz.Font.has_glyph``). Falls
    back to the primary font (best effort) if neither covers the text.

    Returns ``(font, fontfile, fontname)`` -- ``fontfile`` is ``None`` for the
    built-in fallback so ``Page.insert_text`` uses MuPDF's bundled font."""
    if primary_font is not None and _line_covered_by_font(text, primary_font):
        return primary_font, primary_fontfile, _FONT_NAME_PRIMARY
    fallback_font = fallback_state.get()
    if fallback_font is not None and _line_covered_by_font(text, fallback_font):
        return fallback_font, None, _FALLBACK_BUILTIN_FONT
    return primary_font, primary_fontfile, _FONT_NAME_PRIMARY


class _TableTextParser(HTMLParser):
    """Extracts plain text rows from a MinerU ``table_body`` HTML string."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._cell_parts: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_parts = []
        elif tag == "br" and self._in_cell:
            self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._current_row.append("".join(self._cell_parts).strip())
            self._in_cell = False
        elif tag == "tr":
            self.rows.append(self._current_row)
            self._current_row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def _html_table_to_lines(table_html: str) -> list[str]:
    parser = _TableTextParser()
    try:
        parser.feed(table_html or "")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to parse table HTML for searchable-PDF text layer: {exc}")
        return []
    return [" | ".join(cell for cell in row if cell) for row in parser.rows if any(row)]


def _iter_line_texts(blocks: list[dict[str, Any]]):
    """Recursively walk a middle.json block subtree, yielding
    ``(bbox, text, angle)`` triples (bbox = (x0, y0, x1, y1), text may
    contain embedded ``\\n`` for multi-line paragraphs, angle is the
    containing block's rotation in degrees, 0 for the common case)."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        angle = block.get("angle") or 0
        for line in block.get("lines") or []:
            bbox = line.get("bbox")
            spans = line.get("spans") or []
            parts: list[str] = []
            for span in spans:
                if not isinstance(span, dict):
                    continue
                span_type = span.get("type")
                if span_type == "table":
                    parts.extend(_html_table_to_lines(span.get("html") or ""))
                elif span_type == "image":
                    continue
                else:
                    content = span.get("content")
                    if content:
                        parts.append(str(content))
            if bbox and parts:
                yield tuple(bbox), "\n".join(parts), angle
        sub_blocks = block.get("blocks")
        if isinstance(sub_blocks, list):
            yield from _iter_line_texts(sub_blocks)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _normalize_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def _pt_range_to_px(a0: float, a1: float, px_per_pt: float, limit: int) -> tuple[int, int]:
    """Convert a point-space [a0, a1) interval to a clipped pixel-index range."""
    i0 = max(0, int(a0 * px_per_pt))
    i1 = min(limit, int(a1 * px_per_pt) + 1)
    return i0, i1


def _ink_line_bands(
    ink: "np.ndarray",
    bbox: tuple[float, float, float, float],
    px_per_pt: float,
) -> list[tuple[float, float]]:
    """Horizontal ink projection inside ``bbox`` (points): returns the
    physical line bands as a list of ``(y0, y1)`` point-space intervals,
    ordered top to bottom. Rows whose ink-pixel count is below
    ``_BAND_ROW_FRAC`` of the bbox width are treated as inter-line gaps;
    gaps narrower than ``_BAND_MERGE_GAP_PT`` are merged into one band, and
    bands shorter than ``_BAND_MIN_HEIGHT_PT`` are discarded as noise."""
    height_px, width_px = ink.shape[0], ink.shape[1]
    x0, y0, x1, y1 = bbox
    c0, c1 = _pt_range_to_px(x0, x1, px_per_pt, width_px)
    r0, r1 = _pt_range_to_px(y0, y1, px_per_pt, height_px)
    if c1 <= c0 or r1 <= r0:
        return []

    region = ink[r0:r1, c0:c1]
    col_count = c1 - c0
    row_mask = region.sum(axis=1) > (_BAND_ROW_FRAC * col_count)

    raw_bands: list[list[int]] = []
    in_band = False
    for i, flag in enumerate(row_mask.tolist()):
        if flag:
            if in_band:
                raw_bands[-1][1] = i + 1
            else:
                raw_bands.append([i, i + 1])
                in_band = True
        else:
            in_band = False
    if not raw_bands:
        return []

    merge_gap_px = _BAND_MERGE_GAP_PT * px_per_pt
    merged: list[list[int]] = []
    for band in raw_bands:
        if merged and (band[0] - merged[-1][1]) < merge_gap_px:
            merged[-1][1] = band[1]
        else:
            merged.append(band)

    min_height_px = _BAND_MIN_HEIGHT_PT * px_per_pt
    return [
        ((r0 + b0) / px_per_pt, (r0 + b1) / px_per_pt)
        for b0, b1 in merged
        if (b1 - b0) >= min_height_px
    ]


def _ink_word_extent(
    ink: "np.ndarray",
    band: tuple[float, float],
    bbox: tuple[float, float, float, float],
    px_per_pt: float,
) -> tuple[float, float] | None:
    """Vertical ink projection inside one line ``band`` (points): returns the
    first/last ink column (point-space x-range) within the band, searched
    across the block's x-extent padded by ``_RUN_X_PAD_PT`` (detected bands
    may start slightly outside the reported block bbox). Returns ``None``
    when no column clears the ``_RUN_COL_FRAC`` noise threshold."""
    height_px, width_px = ink.shape[0], ink.shape[1]
    x0, _, x1, _ = bbox
    by0, by1 = band
    c0, c1 = _pt_range_to_px(x0 - _RUN_X_PAD_PT, x1 + _RUN_X_PAD_PT, px_per_pt, width_px)
    r0, r1 = _pt_range_to_px(by0, by1, px_per_pt, height_px)
    if c1 <= c0 or r1 <= r0:
        return None

    region = ink[r0:r1, c0:c1]
    row_count = r1 - r0
    col_mask = region.sum(axis=0) > (_RUN_COL_FRAC * row_count)
    idx = np.nonzero(col_mask)[0]
    if idx.size == 0:
        return None
    first, last = int(idx[0]), int(idx[-1]) + 1
    return (c0 + first) / px_per_pt, (c0 + last) / px_per_pt


def _ink_word_runs(
    ink: "np.ndarray",
    band: tuple[float, float],
    bbox: tuple[float, float, float, float],
    px_per_pt: float,
) -> list[tuple[float, float]]:
    """Vertical ink projection inside one line ``band`` (points): returns the
    per-word ink runs as a list of ``(x0, x1)`` point-space intervals,
    ordered left to right. A pixel column belongs to ink using the same
    ``_RUN_COL_FRAC`` threshold as :func:`_ink_word_extent`; the resulting
    raw ink segments (letter strokes, which may not touch under
    binarisation) are merged whenever the gap between them is narrower than
    ``_WORD_GAP_FRAC`` of the band height (an inter-letter gap), so what
    survives merging are the actual inter-word spaces. Runs still narrower
    than ``_RUN_MIN_WIDTH_PT`` after merging (isolated noise/punctuation) are
    folded into a neighbouring run rather than reported standalone."""
    height_px, width_px = ink.shape[0], ink.shape[1]
    x0, _, x1, _ = bbox
    by0, by1 = band
    c0, c1 = _pt_range_to_px(x0 - _RUN_X_PAD_PT, x1 + _RUN_X_PAD_PT, px_per_pt, width_px)
    r0, r1 = _pt_range_to_px(by0, by1, px_per_pt, height_px)
    if c1 <= c0 or r1 <= r0:
        return []

    region = ink[r0:r1, c0:c1]
    row_count = r1 - r0
    col_mask = region.sum(axis=0) > (_RUN_COL_FRAC * row_count)

    raw_runs: list[list[int]] = []
    in_run = False
    for i, flag in enumerate(col_mask.tolist()):
        if flag:
            if in_run:
                raw_runs[-1][1] = i + 1
            else:
                raw_runs.append([i, i + 1])
                in_run = True
        else:
            in_run = False
    if not raw_runs:
        return []

    band_height_pt = max(by1 - by0, 0.1)
    merge_gap_px = _WORD_GAP_FRAC * band_height_pt * px_per_pt

    merged: list[list[int]] = []
    for run in raw_runs:
        if merged and (run[0] - merged[-1][1]) < merge_gap_px:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    min_width_px = _RUN_MIN_WIDTH_PT * px_per_pt
    filtered: list[list[int]] = []
    for run in merged:
        if (run[1] - run[0]) < min_width_px and filtered:
            filtered[-1][1] = max(filtered[-1][1], run[1])
        else:
            filtered.append(list(run))
    if len(filtered) >= 2 and (filtered[0][1] - filtered[0][0]) < min_width_px:
        filtered[1][0] = filtered[0][0]
        del filtered[0]

    return [((c0 + a) / px_per_pt, (c0 + b) / px_per_pt) for a, b in filtered]


def _bucket_words_by_ink_runs(
    words: list[str],
    band_runs: list[list[tuple[float, float]]],
) -> list[list[str]] | None:
    """Assign ``words`` of a single ``\\n``-less paragraph to ``band_runs``
    (one detected ink-word-run list per physical line band, same order as
    the bands) by matching *counts* rather than proportional width: when the
    total number of ink runs across all bands is within 10% of the word
    count, each band receives exactly as many words, in reading order, as it
    has ink runs. Any small residual (a short word fused with trailing
    punctuation into one run, or a run split in two by binarisation noise)
    is patched onto the bands with the most runs so every word is still
    placed. Returns ``None`` -- signalling the caller to fall back to
    proportional distribution -- when no runs were found at all, or the
    total run/word counts diverge by more than the 10% tolerance (segmentation
    likely failed for this block)."""
    num_words = len(words)
    if num_words == 0:
        return None
    targets = [len(r) for r in band_runs]
    total_runs = sum(targets)
    if total_runs == 0:
        return None
    tolerance = max(1, round(0.1 * num_words))
    if abs(total_runs - num_words) > tolerance:
        return None

    delta = num_words - total_runs
    if delta != 0 and targets:
        if delta < 0:
            # More ink runs than words -- the dominant cause in practice is a
            # hyphenated word wrapped across two bands (e.g. "впли-" / "вів"
            # at a justified line's end), where each half is counted as its
            # own run at a band's edge. The tell-tale shape is a run
            # noticeably narrower than the other runs in *its own* band (a
            # word fragment, not a full word), so bands are preferred for
            # trimming in order of how anomalously narrow their *last* run
            # is, before falling back to the band with the most words.
            #
            # A rarer cause -- a single token with internal punctuation
            # (e.g. "В.1.2-14-2009") whose ink gets over-split *inside* a
            # band -- is NOT specifically targeted here: in justified text
            # the gap around such a token's punctuation can be
            # indistinguishable from a genuine inter-word gap, and widening
            # this check to "narrowest run anywhere in the band" was tried
            # and rejected -- it false-positives on legitimate short words/
            # punctuation (e.g. a lone "–"), which regressed the far more
            # common boundary-hyphenation case. See module docstring /
            # export report for this known residual limitation.
            def _narrowness(i: int) -> float:
                runs = band_runs[i]
                if len(runs) < 2:
                    return 1.0
                widths = sorted(b - a for a, b in runs)
                median_w = widths[len(widths) // 2]
                last_w = runs[-1][1] - runs[-1][0]
                return (last_w / median_w) if median_w > 0 else 1.0

            order = sorted(
                (i for i in range(len(targets)) if targets[i] > 0),
                key=lambda i: (_narrowness(i), -targets[i]),
            )
        else:
            # Fewer ink runs than words -- likely two touching words merged
            # into one run by binarisation. Prefer the band with the widest
            # single run (the probable merge site) to absorb the extra word.
            def _max_run_width(i: int) -> float:
                runs = band_runs[i]
                return max((b - a for a, b in runs), default=0.0)

            order = sorted(range(len(targets)), key=lambda i: _max_run_width(i), reverse=True)
        if not order:
            order = sorted(range(len(targets)), key=lambda i: targets[i], reverse=True)
        remaining = abs(delta)
        idx = 0
        safety = 0
        while remaining > 0 and safety < 10000 and order:
            i = order[idx % len(order)]
            if delta > 0:
                targets[i] += 1
                remaining -= 1
            elif targets[i] > 0:
                targets[i] -= 1
                remaining -= 1
            idx += 1
            safety += 1

    buckets: list[list[str]] = []
    cursor = 0
    for t in targets:
        t = max(0, t)
        buckets.append(words[cursor : cursor + t])
        cursor += t
    if cursor < num_words:
        leftover = words[cursor:]
        for i in range(len(buckets) - 1, -1, -1):
            if buckets[i]:
                buckets[i] = buckets[i] + leftover
                break
        else:
            buckets[-1] = buckets[-1] + leftover
    return buckets


def _distribute_words_to_bands(
    words: list[str],
    bands: list[tuple[float, float]],
    ink: "np.ndarray",
    bbox: tuple[float, float, float, float],
    px_per_pt: float,
    primary_font: "fitz.Font | None",
) -> list[tuple[float, float, str]]:
    """Greedily distribute ``words`` of a single ``\\n``-less paragraph across
    ``bands`` (physical line bands with no per-word geometry of their own),
    proportionally to each band's ink width, weighting words by their
    approximate rendered width at ``_DISTRIBUTE_REF_FONTSIZE``. Returns
    ``(by0, by1, line_text)`` triples, one per band that received words."""
    band_ink_widths: list[float] = []
    for band in bands:
        extent = _ink_word_extent(ink, band, bbox, px_per_pt)
        band_ink_widths.append((extent[1] - extent[0]) if extent else 0.0)
    total_ink_width = sum(band_ink_widths)
    if total_ink_width <= 0:
        band_ink_widths = [1.0] * len(bands)
        total_ink_width = float(len(bands))

    def word_width(word: str) -> float:
        if primary_font is not None:
            try:
                return max(primary_font.text_length(word, fontsize=_DISTRIBUTE_REF_FONTSIZE), 0.1)
            except Exception:
                pass
        return max(len(word) * _DISTRIBUTE_REF_FONTSIZE * 0.5, 0.1)

    weights = [word_width(w) for w in words]
    total_weight = sum(weights) or 1.0

    band_targets: list[float] = []
    running = 0.0
    for w in band_ink_widths:
        running += w
        band_targets.append(running / total_ink_width)

    buckets: list[list[str]] = [[] for _ in bands]
    running_weight = 0.0
    band_i = 0
    for word, weight in zip(words, weights):
        midpoint_frac = (running_weight + weight / 2) / total_weight
        while band_i < len(bands) - 1 and midpoint_frac > band_targets[band_i]:
            band_i += 1
        buckets[band_i].append(word)
        running_weight += weight

    return [
        (bands[i][0], bands[i][1], " ".join(bucket))
        for i, bucket in enumerate(buckets)
        if bucket
    ]


def _render_run(
    page: "fitz.Page",
    by0: float,
    by1: float,
    rx0: float,
    rx1: float,
    text: str,
    primary_font: "fitz.Font | None",
    primary_fontfile: str | None,
    fallback_state: "_FallbackFontState",
    use_ink_baseline: bool,
) -> None:
    """Insert one invisible run of text spanning the point-space rectangle
    ``(rx0, by0, rx1, by1)``. When ``use_ink_baseline`` is set, ``by1`` is
    taken to be the bottom of the run's actual ink (so the baseline is
    placed ``descent`` above it); otherwise the legacy ascent-from-top
    placement is used (no reliable ink bottom is available)."""
    line_text = text.strip()
    if not line_text:
        return
    height = by1 - by0
    if height <= 0:
        return
    fontsize = _clamp(height * _FONT_LINE_FRAC, _FONT_MIN_PT, _FONT_MAX_PT)

    font, fontfile, fontname = _font_for_text(line_text, primary_font, primary_fontfile, fallback_state)

    if font is not None:
        try:
            natural_width = font.text_length(line_text, fontsize=fontsize)
        except Exception:
            natural_width = len(line_text) * fontsize * 0.5
        ascent = font.ascender or 0.8
        descent = -(font.descender or -0.2)
    else:
        natural_width = len(line_text) * fontsize * 0.5
        ascent = 0.8
        descent = 0.2

    width = max(rx1 - rx0, 0.01)
    scale_x = 1.0
    if natural_width > 0.01:
        scale_x = _clamp(width / natural_width, _SCALE_X_MIN, _SCALE_X_MAX)

    if use_ink_baseline:
        baseline_y = by1 - descent * fontsize
    else:
        baseline_y = min(by0 + ascent * fontsize, by0 + height)

    point = fitz.Point(rx0, baseline_y)
    morph = (point, fitz.Matrix(scale_x, 1)) if abs(scale_x - 1.0) > 0.01 else None

    try:
        page.insert_text(
            point,
            line_text,
            fontname=fontname,
            fontfile=fontfile,
            fontsize=fontsize,
            render_mode=3,  # invisible
            morph=morph,
        )
    except Exception as exc:
        logger.warning(f"Failed to insert invisible text line, skipping: {exc!r} text={line_text!r}")


def _insert_rotated_best_effort(
    page: "fitz.Page",
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    text: str,
    primary_font: "fitz.Font | None",
    primary_fontfile: str | None,
    fallback_state: "_FallbackFontState",
    angle: float,
) -> None:
    """Best-effort placement for a rotated block (``angle`` != 0): inserted
    unrotated, and skipping ink-guided placement entirely (the background
    ink itself is rotated, so an axis-aligned projection would be
    meaningless). The font size still comes from a single line's height --
    estimated from the text's natural width at a reference font size when no
    ``\\n`` markers are available -- instead of the whole block height."""
    logger.warning(f"Rotated text block (angle={angle}) inserted unrotated, best-effort placement")
    width, height = x1 - x0, y1 - y0
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    if not parts:
        return

    if len(parts) == 1 and width > 0:
        raw = parts[0]
        try:
            natural_width = (
                primary_font.text_length(raw, fontsize=_DISTRIBUTE_REF_FONTSIZE) if primary_font else None
            )
        except Exception:
            natural_width = None
        if natural_width is None:
            natural_width = len(raw) * _DISTRIBUTE_REF_FONTSIZE * 0.5
        estimated_lines = max(1, round(natural_width / width))
        words = raw.split()
        if estimated_lines > 1 and words:
            chunk = max(1, -(-len(words) // estimated_lines))  # ceil division
            parts = [" ".join(words[i : i + chunk]) for i in range(0, len(words), chunk)]

    sub_h = height / len(parts)
    for i, ptext in enumerate(parts):
        sy0 = y0 + i * sub_h
        sy1 = sy0 + sub_h
        _render_run(page, sy0, sy1, x0, x1, ptext, primary_font, primary_fontfile, fallback_state, use_ink_baseline=False)


def _insert_invisible_line(
    page: "fitz.Page",
    bbox: tuple[float, float, float, float],
    text: str,
    primary_font: "fitz.Font | None",
    primary_fontfile: str | None,
    fallback_state: "_FallbackFontState",
    angle: float = 0,
    ink: "np.ndarray | None" = None,
    px_per_pt: float = 1.0,
) -> None:
    """Place ``text`` (one middle.json line -- possibly a whole multi-line
    paragraph) as invisible text inside ``bbox``. See module docstring for
    the ink-guided placement strategy."""
    x0, y0, x1, y1 = _normalize_bbox(bbox)
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return

    if angle:
        _insert_rotated_best_effort(
            page, x0, y0, x1, y1, text, primary_font, primary_fontfile, fallback_state, angle
        )
        return

    parts = [p.strip() for p in text.split("\n")]
    if not any(parts):
        return

    bands: list[tuple[float, float]] | None = None
    if ink is not None and np is not None:
        try:
            detected = _ink_line_bands(ink, (x0, y0, x1, y1), px_per_pt)
        except Exception as exc:
            logger.warning(f"Ink line-band detection failed, falling back to equal split: {exc}")
            detected = []
        if detected:
            bands = detected

    # (a) explicit "\n" markers whose count matches the detected ink bands
    # 1:1 -- covers both multi-line paragraphs with real line breaks and
    # table rows (one HTML row per band).
    if bands and len(parts) == len(bands):
        for (by0, by1), ptext in zip(bands, parts):
            if not ptext:
                continue
            try:
                extent = _ink_word_extent(ink, (by0, by1), (x0, y0, x1, y1), px_per_pt)
            except Exception as exc:
                logger.warning(f"Ink word-extent detection failed: {exc}")
                extent = None
            rx0, rx1 = extent if extent else (x0, x1)
            _render_run(page, by0, by1, rx0, rx1, ptext, primary_font, primary_fontfile, fallback_state, use_ink_baseline=True)
        return

    # (b) no "\n" at all -- the whole paragraph is one string (hybrid
    # backend): distribute its words across the detected bands. Preferred
    # strategy is word-count-exact: match each band's actual ink word runs
    # (see _ink_word_runs) so words land in the visual line they were
    # actually rendered on, instead of drifting onto a neighbouring band the
    # way proportional width-based distribution could. Falls back to the
    # legacy proportional distribution when run detection doesn't yield a
    # usable per-band word count.
    if bands and len(parts) == 1 and parts[0]:
        words = parts[0].split()
        lines: list[tuple[float, float, str]] = []
        if words:
            buckets = None
            try:
                band_runs = [_ink_word_runs(ink, band, (x0, y0, x1, y1), px_per_pt) for band in bands]
                buckets = _bucket_words_by_ink_runs(words, band_runs)
            except Exception as exc:
                logger.warning(f"Ink word-run segmentation failed, falling back to proportional distribution: {exc}")
                buckets = None
            if buckets is not None:
                lines = [
                    (bands[i][0], bands[i][1], " ".join(bucket))
                    for i, bucket in enumerate(buckets)
                    if bucket
                ]
            else:
                try:
                    lines = _distribute_words_to_bands(words, bands, ink, (x0, y0, x1, y1), px_per_pt, primary_font)
                except Exception as exc:
                    logger.warning(f"Word-to-band distribution failed, falling back to equal split: {exc}")
                    lines = []
        if lines:
            for by0, by1, ltext in lines:
                try:
                    extent = _ink_word_extent(ink, (by0, by1), (x0, y0, x1, y1), px_per_pt)
                except Exception as exc:
                    logger.warning(f"Ink word-extent detection failed: {exc}")
                    extent = None
                rx0, rx1 = extent if extent else (x0, x1)
                _render_run(page, by0, by1, rx0, rx1, ltext, primary_font, primary_fontfile, fallback_state, use_ink_baseline=True)
            return

    # (c) fallback: no numpy / no usable ink bands / band count doesn't line
    # up with "\n" count -- legacy equal-height split, one morphed run per
    # sub-band spanning the full bbox width (no worse than before).
    sub_h = height / len(parts)
    for i, ptext in enumerate(parts):
        if not ptext:
            continue
        sy0 = y0 + i * sub_h
        sy1 = sy0 + sub_h
        _render_run(page, sy0, sy1, x0, x1, ptext, primary_font, primary_fontfile, fallback_state, use_ink_baseline=False)


def _is_grayscale_pixmap(pix: "fitz.Pixmap") -> bool:
    """Cheap heuristic: sample a sparse grid of pixels and check whether R/G/B
    channels are all close to each other -- true for B/W or grayscale scans,
    which can then be re-encoded in DeviceGray for a much smaller JPEG."""
    if pix.n < 3 or pix.alpha:
        return False
    samples = pix.samples
    stride = pix.stride
    n = pix.n
    w, h = pix.width, pix.height
    if w == 0 or h == 0:
        return False
    checked = 0
    colored = 0
    for y in range(0, h, _GRAYSCALE_SAMPLE_STEP):
        row_off = y * stride
        for x in range(0, w, _GRAYSCALE_SAMPLE_STEP):
            off = row_off + x * n
            r, g, b = samples[off], samples[off + 1], samples[off + 2]
            if max(r, g, b) - min(r, g, b) > _GRAYSCALE_CHANNEL_TOLERANCE:
                colored += 1
            checked += 1
    if checked == 0:
        return False
    return (colored / checked) <= _GRAYSCALE_MAX_COLOR_FRACTION


def _page_to_pixmap(page: "fitz.Page", dpi: int) -> "fitz.Pixmap":
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    return page.get_pixmap(matrix=matrix, alpha=False)


def _pixmap_to_jpeg_bytes(pix: "fitz.Pixmap") -> bytes:
    out_pix = pix
    try:
        if _is_grayscale_pixmap(pix):
            out_pix = fitz.Pixmap(fitz.csGRAY, pix)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Grayscale detection failed, keeping RGB render: {exc}")
    return out_pix.tobytes("jpeg", jpg_quality=_JPEG_QUALITY)


def _pixmap_to_ink_array(pix: "fitz.Pixmap") -> "np.ndarray | None":
    """Binarise ``pix`` (gray < ``_INK_THRESHOLD`` counts as ink) for
    ink-guided text placement, reusing the exact pixmap already rendered for
    the background JPEG (no second page render). Returns ``None`` when numpy
    is unavailable or the conversion fails; callers degrade to the legacy
    equal-split placement in that case."""
    if np is None:
        return None
    try:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        gray = arr[:, :, :3].mean(axis=2) if pix.n >= 3 else arr[:, :, 0].astype(np.float64)
        return gray < _INK_THRESHOLD
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to build ink array for text placement: {exc}")
        return None


def _load_middle_json(middle_json: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(middle_json, dict):
        return middle_json
    with open(middle_json, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _middle_pages(middle: dict[str, Any]) -> list[dict[str, Any]]:
    pages = middle.get("pdf_info") or middle.get("page_info") or []
    return pages if isinstance(pages, list) else []


def export_searchable_pdf(
    original_pdf: str | Path,
    middle_json: dict[str, Any] | str | Path,
    output_path: str | Path,
    dpi: int = _DEFAULT_DPI,
) -> Path:
    """Render ``original_pdf`` with an invisible, recognised-text layer on top.

    Each page of ``original_pdf`` is rasterised (at ``dpi``) into a JPEG
    background image sized to the original page dimensions, and the
    recognised text from ``middle_json`` (a MinerU ``*_middle.json`` dict, or
    a path to one) is inserted on top as invisible (``render_mode=3``),
    positioned/scaled from each line's bbox. A single malformed line never
    aborts the export -- it is skipped with a ``logger.warning``.

    :param original_pdf: path to the original PDF (the recognised document).
    :param middle_json: parsed ``*_middle.json`` dict, or a path to it.
    :param output_path: destination path of the generated searchable PDF.
    :param dpi: raster resolution for the background page images.
    :return: the resolved output path.
    """
    original_pdf = Path(original_pdf)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    middle = _load_middle_json(middle_json)
    pages_info = _middle_pages(middle)

    font_path = _resolve_font(_PRIMARY_FONT_CANDIDATES)
    font_obj = None
    if font_path is not None:
        try:
            font_obj = fitz.Font(fontfile=str(font_path))
        except Exception as exc:
            logger.warning(f"Failed to load font {font_path} for text layer: {exc}")
            font_path = None
    if font_obj is None:
        logger.warning(
            "No Unicode-capable TTF font found on disk for the searchable-PDF "
            "text layer; falling back to PyMuPDF's built-in Helvetica, which "
            "does not cover Cyrillic/CJK -- non-Latin text may fail to embed "
            "or extract incorrectly."
        )
    fallback_state = _FallbackFontState()

    src_doc = fitz.open(str(original_pdf))
    out_doc = fitz.open()

    if len(pages_info) != src_doc.page_count:
        logger.warning(
            f"middle.json page count ({len(pages_info)}) does not match "
            f"original PDF page count ({src_doc.page_count}); text layer will "
            f"only be added where both are available."
        )

    try:
        for page_idx in range(src_doc.page_count):
            src_page = src_doc[page_idx]
            new_page = out_doc.new_page(width=src_page.rect.width, height=src_page.rect.height)

            ink = None
            px_per_pt = dpi / 72.0
            try:
                pix = _page_to_pixmap(src_page, dpi)
                ink = _pixmap_to_ink_array(pix)
                jpeg_bytes = _pixmap_to_jpeg_bytes(pix)
                new_page.insert_image(new_page.rect, stream=jpeg_bytes)
            except Exception as exc:
                logger.warning(f"Failed to render background image for page {page_idx}: {exc}")

            if page_idx >= len(pages_info):
                continue
            mid_page = pages_info[page_idx]
            if not isinstance(mid_page, dict):
                continue
            blocks = mid_page.get("para_blocks") or mid_page.get("preproc_blocks") or []
            if not isinstance(blocks, list):
                continue

            for bbox, text, angle in _iter_line_texts(blocks):
                _insert_invisible_line(
                    new_page,
                    bbox,
                    text,
                    font_obj,
                    str(font_path) if font_path else None,
                    fallback_state,
                    angle=angle,
                    ink=ink,
                    px_per_pt=px_per_pt,
                )

        # Transfer link annotations in a second pass, after every page has
        # been created: a link's target can point *forward* to a page that
        # doesn't exist in out_doc yet during the main per-page loop above
        # (PyMuPDF's insert_link validates the target page number against
        # the document's current page count, so inserting a forward link
        # mid-loop raises "bad page number(s)"). The new pages are created at
        # the exact same size as their src_page counterparts (see
        # out_doc.new_page(...) above), so link rectangles/target points
        # transfer 1:1 with no rescaling. One bad link must never abort the
        # export -- skip it with a warning.
        for page_idx in range(min(src_doc.page_count, out_doc.page_count)):
            src_page = src_doc[page_idx]
            new_page = out_doc[page_idx]
            for link in src_page.get_links():
                try:
                    new_page.insert_link(link)
                except Exception as exc:
                    logger.warning(f"Failed to transfer link on page {page_idx}: {exc!r} link={link!r}")

        try:
            # Subset the embedded text-layer fonts to just the glyphs actually
            # used -- critical when the CJK fallback kicks in (Noto Sans CJK is
            # ~19 MB unsubsetted, a subset is usually well under 1 MB).
            out_doc.subset_fonts()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Font subsetting failed, saving with full fonts: {exc}")
        out_doc.save(str(output_path), garbage=4, deflate=True)
    finally:
        out_doc.close()
        src_doc.close()

    return output_path


def find_middle_json(result_dir: str | Path) -> Path | None:
    """Find the ``*_middle.json`` file inside a MinerU parse result directory."""
    result_dir = Path(result_dir)
    if not result_dir.is_dir():
        return None
    matches = sorted(result_dir.glob(f"*{MIDDLE_JSON_SUFFIX}"))
    return matches[0] if matches else None


def find_original_pdf(result_dir: str | Path, stem: str | None = None) -> Path | None:
    """Locate the original PDF for a MinerU parse result directory.

    Looks for ``<stem>_origin.pdf`` first (the name MinerU writes when
    ``f_dump_orig_pdf`` is enabled -- for image/office inputs this is the
    already-converted PDF rendition, matching ``middle.json`` page-for-page).
    Falls back to the single other ``*.pdf`` file in the directory (excluding
    the ``_layout.pdf``/``_span.pdf`` debug visualizations MinerU may also
    produce there), if exactly one candidate exists.
    """
    result_dir = Path(result_dir)
    if not result_dir.is_dir():
        return None

    if stem:
        candidate = result_dir / f"{stem}{ORIGIN_PDF_SUFFIX}"
        if candidate.is_file():
            return candidate

    origin_matches = sorted(result_dir.glob(f"*{ORIGIN_PDF_SUFFIX}"))
    if origin_matches:
        return origin_matches[0]

    other_pdfs = [
        p
        for p in sorted(result_dir.glob("*.pdf"))
        if not (p.name.endswith("_layout.pdf") or p.name.endswith("_span.pdf"))
    ]
    if len(other_pdfs) == 1:
        return other_pdfs[0]
    return None


def export_searchable_pdf_from_result_dir(
    result_dir: str | Path,
    original_pdf: str | Path | None = None,
    output_path: str | Path | None = None,
    dpi: int = _DEFAULT_DPI,
) -> Path:
    """Convenience wrapper: locate ``*_middle.json`` (and, unless given, the
    original PDF) inside a MinerU parse result directory and export a
    searchable PDF.

    :param result_dir: directory containing ``*_middle.json`` (the per-document
        parse directory MinerU produces, as resolved by
        ``mineru.cli.output_paths.resolve_parse_dir``).
    :param original_pdf: path to the original PDF; auto-detected via
        :func:`find_original_pdf` when omitted.
    :param output_path: destination path; defaults to
        ``<result_dir>/<stem>_searchable.pdf``.
    :param dpi: raster resolution for the background page images.
    :return: the resolved output path.
    :raises FileNotFoundError: if no ``*_middle.json`` or no original PDF can
        be found.
    """
    result_dir = Path(result_dir)
    middle_json_path = find_middle_json(result_dir)
    if middle_json_path is None:
        raise FileNotFoundError(f"No *{MIDDLE_JSON_SUFFIX} file found under: {result_dir}")

    stem = middle_json_path.name[: -len(MIDDLE_JSON_SUFFIX)]

    if original_pdf is None:
        resolved_original = find_original_pdf(result_dir, stem)
        if resolved_original is None:
            raise FileNotFoundError(
                f"No original PDF found under {result_dir} (expected "
                f"'{stem}{ORIGIN_PDF_SUFFIX}' or a single other *.pdf file)"
            )
        original_pdf = resolved_original

    if output_path is None:
        output_path = result_dir / f"{stem}_searchable.pdf"

    return export_searchable_pdf(original_pdf, middle_json_path, output_path, dpi=dpi)
