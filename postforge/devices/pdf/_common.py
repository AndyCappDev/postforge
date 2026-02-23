# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Shared utilities for the PDF content stream and builder modules.

Contains formatting helpers and state tracking used across all submodules.
This module breaks what would otherwise be circular imports between
content_stream.py and its submodules.
"""

from dataclasses import dataclass, field


@dataclass
class _Type3GlyphDef:
    """Single glyph definition for a PDF Type 3 font."""
    char_code: int           # 0-255
    glyph_name: str          # from PS Encoding, e.g. 'A'
    width_x: float           # device-space advance width (wx)
    bbox: tuple | None       # (llx, lly, urx, ury) in device space or None
    charproc_stream: bytes   # PDF operators for the CharProc content stream


@dataclass
class _Type3FontDef:
    """Accumulated Type 3 font definition for a PDF page."""
    font_id: object          # from cache_key (FontName or id)
    font_matrix: tuple       # from cache_key
    ctm_scale: tuple         # from cache_key
    resource_name: str       # e.g. '/T3F0'
    glyphs: dict[int, _Type3GlyphDef] = field(default_factory=dict)
    font_dict: object = None  # PS font dict reference (for Encoding access)
    unicode_map: dict[int, str] = field(default_factory=dict)  # char_code -> Unicode char


def _fmt(v: float) -> str:
    """Format a float for PDF output, removing trailing zeros."""
    s = f'{v:.6f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s


def _cfmt(v: float) -> str:
    """Format a coordinate for PDF path output, removing trailing zeros.

    Uses 2 decimal places -- sufficient for sub-point precision even at
    high device DPIs (at 1200 DPI with 0.06 scale, 0.01pt precision
    exceeds any printer resolution).
    """
    s = f'{v:.2f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s


class _GState:
    """Tracks emitted PDF graphics state to suppress redundant operators."""
    __slots__ = ('fill_color', 'stroke_color', 'line_width', 'line_cap',
                 'line_join', 'miter_limit', 'dash_pattern', 'text_color',
                 'aniso_ctm')

    def __init__(self) -> None:
        self.fill_color: bytes | None = None
        self.stroke_color: bytes | None = None
        self.line_width: str | None = None
        self.line_cap: int | None = None
        self.line_join: int | None = None
        self.miter_limit: str | None = None
        self.dash_pattern: bytes | None = None
        self.text_color: bytes | None = None
        # CTM of currently open anisotropic stroke batch (None = no open batch)
        self.aniso_ctm: tuple | None = None

    def invalidate(self) -> None:
        """Called after q/Q to force re-emission of state."""
        self.fill_color = None
        self.stroke_color = None
        self.line_width = None
        self.line_cap = None
        self.line_join = None
        self.miter_limit = None
        self.dash_pattern = None
        self.text_color = None
