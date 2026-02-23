# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PDF Content Stream Generator

Converts PostScript display list elements to PDF content stream operators.
Preserves original color space information (CMYK, Gray, RGB) instead of
converting everything to RGB.
"""

import copy
import math
import unicodedata
import zlib
from dataclasses import dataclass, field

from ...core import types as ps
from ...core import icc_profile
from ...core.color_space import (ColorSpaceEngine, _get_cie_float_array,
                                 is_identity_cie, preconvert_cie_def_table)
from ...core.types.context import global_resources
from ...core.unicode_mapping import glyph_name_to_unicode
from ..pdf.font_tracker import FontTracker


# Module-level ref to font widths, set during generate_content_stream
_active_font_widths: dict[tuple, dict[int, int]] = {}


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
    unicode_map: dict[int, str] = field(default_factory=dict)  # char_code → Unicode char


def _fmt(v: float) -> str:
    """Format a float for PDF output, removing trailing zeros."""
    s = f'{v:.6f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s


def _cfmt(v: float) -> str:
    """Format a coordinate for PDF path output, removing trailing zeros.

    Uses 2 decimal places — sufficient for sub-point precision even at
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


def generate_content_stream(display_list: ps.DisplayList,
                            height_device: float,
                            font_tracker: FontTracker,
                            embedded_fonts: dict,
                            font_widths_cache: dict,
                            device_scale: float = 1.0,
                            type3_fonts: dict[tuple, _Type3FontDef] | None = None,
                            type3_font_counter: int = 0,
                            ) -> tuple[bytes, list[tuple[str, dict]], list[tuple[str, dict]], dict[tuple, _Type3FontDef], int, set[tuple]]:
    """Generate PDF content stream from a display list.

    Args:
        display_list: PostScript display list with paths, fills, strokes, etc.
        height_device: Page height in device pixels (for Y-flip).
        font_tracker: FontTracker for looking up font keys.
        embedded_fonts: Dict mapping font_key -> (pdf_resource_name, font_ref).
        font_widths_cache: Dict mapping font_key -> {char_code: width_in_1000ths}.
        device_scale: Scale factor from device units to PDF points (72/dpi).
        type3_fonts: Shared Type 3 font definitions to accumulate into
            (pass None to create a fresh dict).
        type3_font_counter: Next Type 3 font counter value.

    Returns:
        Tuple of (content_stream_bytes, shading_defs, image_defs,
        type3_font_defs, type3_font_counter, type3_page_keys) where
        shading_defs is a list of (resource_name, shading_description_dict)
        pairs, image_defs is a list of (resource_name, image_description_dict)
        pairs for XObject images, type3_font_defs maps font_key tuples to
        _Type3FontDef objects, type3_font_counter is the updated counter, and
        type3_page_keys is the set of font keys used on this page.
    """
    global _active_font_widths
    _active_font_widths = font_widths_cache

    lines: list[bytes] = []
    shading_defs: list[tuple[str, dict]] = []
    shading_counter = 0
    image_defs: list[tuple[str, dict]] = []
    image_counter = 0
    gs = _GState()
    clip_depth = 0  # Number of nested q/W n groups for clipping
    current_path: ps.Path | None = None
    current_path_lines: list[bytes] = []
    # Text batch: consecutive same-font TextObjs merged into one BT/ET
    text_batch: list[ps.TextObj] = []
    text_batch_font: str | None = None  # PDF resource name of batched font
    # Invisible text batch: same-baseline ActualTextStart entries
    invis_batch: list[tuple] = []

    # Type 3 font collection state (shared across pages when provided)
    if type3_fonts is None:
        type3_fonts = {}
    type3_page_keys: set[tuple] = set()  # font keys used on this page
    collecting_type3 = False
    type3_glyph_elements: list = []
    type3_glyph_cache_key: object = None
    type3_glyph_pos: tuple[float, float] = (0.0, 0.0)
    # Type 3 text batching: consecutive same-font/color chars → single BT/ET
    type3_text_batch: list[tuple] = []  # (char_code, x, y, width_x)
    type3_batch_font_key: tuple | None = None
    type3_batch_color: tuple | None = None
    type3_suppress_invis = False  # suppress ActualTextStart for Type 3 text
    # Pending Type 3 char codes for ActualText → ToUnicode correlation
    type3_pending_codes: list[tuple] = []  # (char_code, font_key)

    def _flush_text_batch() -> None:
        nonlocal text_batch, text_batch_font
        if text_batch:
            _emit_text_batch(lines, text_batch, font_tracker,
                             embedded_fonts, gs)
            text_batch = []
            text_batch_font = None

    def _flush_invis_batch() -> None:
        nonlocal invis_batch
        if invis_batch:
            _flush_invisible_batch(lines, invis_batch)
            invis_batch = []

    def _flush_type3_text() -> None:
        nonlocal type3_text_batch, type3_batch_font_key, type3_batch_color
        nonlocal type3_pending_codes
        if type3_text_batch and type3_batch_font_key is not None:
            _emit_type3_text_run(lines, type3_text_batch,
                                 type3_fonts[type3_batch_font_key],
                                 type3_batch_color, gs)
            type3_text_batch = []
            type3_batch_font_key = None
            type3_batch_color = None
        type3_pending_codes = []

    # The display list is in device space (Y=0 at top, Y increases downward)
    # but PDF uses Y=0 at bottom, Y increases upward. Apply a combined
    # device-to-points scale + Y-flip transform at the start.
    sx = device_scale
    sy = device_scale
    lines.append(f'{_fmt(sx)} 0 0 {_fmt(-sy)} 0 {_fmt(height_device * sy)} cm'.encode())

    for item in display_list:
        if isinstance(item, ps.Path):
            if collecting_type3:
                type3_glyph_elements.append(item)
                continue
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            type3_suppress_invis = False
            current_path = item
            current_path_lines = _emit_path(item)

        elif isinstance(item, ps.Fill):
            if collecting_type3:
                type3_glyph_elements.append(item)
                continue
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            _emit_fill(lines, current_path_lines, item, gs)
            current_path = None
            current_path_lines = []

        elif isinstance(item, ps.Stroke):
            if collecting_type3:
                # Strokes in Type 3 glyphs are unusual; include as element
                type3_glyph_elements.append(item)
                continue
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            type3_suppress_invis = False
            _emit_stroke(lines, current_path_lines, current_path, item, gs)
            current_path = None
            current_path_lines = []

        elif isinstance(item, ps.ClipElement):
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False

            if item.is_initclip:
                # initclip resets to page bounds — pop ALL nested clip groups
                while clip_depth > 0:
                    lines.append(b'Q')
                    clip_depth -= 1
                gs.invalidate()
            elif item.path:
                # Nest within existing clip group(s) — PDF W operator
                # intersects with the current clipping path, so nesting
                # q/W n inside an outer q/W n produces the correct
                # intersection, matching PostScript's clip semantics.
                lines.append(b'q')
                clip_depth += 1
                path_lines = _emit_path(item.path, close_subpaths=True)
                lines.extend(path_lines)
                if item.winding_rule == ps.WINDING_EVEN_ODD:
                    lines.append(b'W* n')
                else:
                    lines.append(b'W n')
                gs.invalidate()

        elif isinstance(item, ps.TextObj):
            _flush_type3_text()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            # Batch consecutive same-font TextObjs into one BT/ET
            font_name = _resolve_text_font(item, font_tracker, embedded_fonts)
            if font_name is not None and font_name == text_batch_font:
                text_batch.append(item)
            else:
                _flush_text_batch()
                if font_name is not None:
                    text_batch = [item]
                    text_batch_font = font_name

        elif isinstance(item, ps.ImageElement):
            if collecting_type3:
                type3_glyph_elements.append(item)
                continue
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            img_name, image_counter = _emit_image_xobject(
                lines, item, image_defs, image_counter, gs)

        elif isinstance(item, (ps.AxialShadingFill, ps.RadialShadingFill)):
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            if isinstance(item, ps.AxialShadingFill):
                sh_desc = _build_axial_shading(item)
            else:
                sh_desc = _build_radial_shading(item)
            if sh_desc is not None:
                sh_name = f'/Sh{shading_counter}'
                shading_counter += 1
                shading_defs.append((sh_name, sh_desc))
                _emit_shading_ref(lines, sh_name, item.ctm)

        elif isinstance(item, ps.FunctionShadingFill):
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            _emit_function_shading(lines, item)

        elif isinstance(item, ps.MeshShadingFill):
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            sh_desc = _build_mesh_shading(item)
            if sh_desc is not None:
                sh_name = f'/Sh{shading_counter}'
                shading_counter += 1
                shading_defs.append((sh_name, sh_desc))
                _emit_shading_ref(lines, sh_name, item.ctm)

        elif isinstance(item, ps.PatchShadingFill):
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            sh_desc = _build_patch_shading(item)
            if sh_desc is not None:
                sh_name = f'/Sh{shading_counter}'
                shading_counter += 1
                shading_defs.append((sh_name, sh_desc))
                _emit_shading_ref(lines, sh_name, item.ctm)

        elif isinstance(item, ps.ErasePage):
            pass  # No-op in PDF

        elif isinstance(item, ps.ShowPage):
            pass  # Handled by caller

        elif isinstance(item, (ps.PatternFill,)):
            _flush_type3_text()
            _flush_text_batch()
            _flush_invis_batch()
            _close_aniso_batch(lines, gs)
            type3_suppress_invis = False
            # PatternFill is not in scope for native_pdf v1
            current_path = None
            current_path_lines = []

        elif isinstance(item, ps.GlyphRef):
            _flush_text_batch()
            _close_aniso_batch(lines, gs)

            # Try to emit as Type 3 font reference
            path_cache = global_resources.get_glyph_cache()
            cached = path_cache.get(item.cache_key)
            if cached is not None and cached.char_bbox is not None:
                # Cacheable glyph (d1) — emit as Type 3 font character
                font_key = _type3_font_key(item.cache_key)
                color = item.cache_key.color
                if font_key not in type3_fonts:
                    t3_name = f'/T3F{type3_font_counter}'
                    type3_font_counter += 1
                    type3_fonts[font_key] = _Type3FontDef(
                        font_id=item.cache_key.font_id,
                        font_matrix=item.cache_key.font_matrix,
                        ctm_scale=item.cache_key.ctm_scale,
                        resource_name=t3_name,
                        font_dict=cached.font_dict)
                t3_font = type3_fonts[font_key]
                type3_page_keys.add(font_key)

                char_code = item.cache_key.char_selector[0]
                if char_code not in t3_font.glyphs:
                    glyph_name = _get_glyph_name_for_code(
                        cached.font_dict, char_code)
                    # Transform metrics from character space to device space
                    dev_wx, dev_bbox = _charspace_to_device(
                        cached.char_width, cached.char_bbox,
                        item.cache_key.font_matrix,
                        item.cache_key.ctm_scale)
                    charproc = _build_charproc_stream(
                        cached.display_elements,
                        (dev_wx, 0.0), dev_bbox)
                    t3_font.glyphs[char_code] = _Type3GlyphDef(
                        char_code=char_code,
                        glyph_name=glyph_name,
                        width_x=dev_wx,
                        bbox=dev_bbox,
                        charproc_stream=charproc)

                # Batch for combined BT/ET emission
                if (font_key != type3_batch_font_key
                        or color != type3_batch_color):
                    _flush_type3_text()
                    type3_batch_font_key = font_key
                    type3_batch_color = color
                type3_text_batch.append(
                    (char_code, item.position_x, item.position_y,
                     t3_font.glyphs[char_code].width_x))
                type3_pending_codes.append((char_code, font_key))
                type3_suppress_invis = True
            else:
                # Non-cacheable or missing — fall back to inline paths
                _flush_type3_text()
                type3_suppress_invis = False
                image_counter = _emit_glyph_ref(
                    lines, item, gs, image_defs, image_counter)

        elif isinstance(item, ps.GlyphStart):
            # Check if this is a cacheable glyph (has char_bbox in cache)
            path_cache = global_resources.get_glyph_cache()
            cached = path_cache.get(item.cache_key)
            if cached is not None and cached.char_bbox is not None:
                # Cacheable (d1) — collect elements for CharProc
                collecting_type3 = True
                type3_glyph_elements = []
                type3_glyph_cache_key = item.cache_key
                type3_glyph_pos = (item.position_x, item.position_y)
            # else: non-cacheable — elements flow through normal dispatch

        elif isinstance(item, ps.GlyphEnd):
            if collecting_type3 and type3_glyph_cache_key is not None:
                # End of cache miss — build CharProc from cached elements
                path_cache = global_resources.get_glyph_cache()
                cached = path_cache.get(type3_glyph_cache_key)
                ck = type3_glyph_cache_key

                if cached is not None and cached.display_elements:
                    font_key = _type3_font_key(ck)
                    color = ck.color
                    if font_key not in type3_fonts:
                        t3_name = f'/T3F{type3_font_counter}'
                        type3_font_counter += 1
                        type3_fonts[font_key] = _Type3FontDef(
                            font_id=ck.font_id,
                            font_matrix=ck.font_matrix,
                            ctm_scale=ck.ctm_scale,
                            resource_name=t3_name,
                            font_dict=cached.font_dict)
                    t3_font = type3_fonts[font_key]
                    type3_page_keys.add(font_key)

                    char_code = ck.char_selector[0]
                    if char_code not in t3_font.glyphs:
                        glyph_name = _get_glyph_name_for_code(
                            t3_font.font_dict, char_code)
                        # Transform metrics from character space to device space
                        dev_wx, dev_bbox = _charspace_to_device(
                            cached.char_width, cached.char_bbox,
                            ck.font_matrix, ck.ctm_scale)
                        charproc = _build_charproc_stream(
                            cached.display_elements,
                            (dev_wx, 0.0), dev_bbox)
                        t3_font.glyphs[char_code] = _Type3GlyphDef(
                            char_code=char_code,
                            glyph_name=glyph_name,
                            width_x=dev_wx,
                            bbox=dev_bbox,
                            charproc_stream=charproc)

                    # Batch for combined BT/ET emission
                    _flush_text_batch()
                    _close_aniso_batch(lines, gs)
                    if (font_key != type3_batch_font_key
                            or color != type3_batch_color):
                        _flush_type3_text()
                        type3_batch_font_key = font_key
                        type3_batch_color = color
                    ox, oy = type3_glyph_pos
                    type3_text_batch.append(
                        (char_code, ox, oy,
                         t3_font.glyphs[char_code].width_x))
                    type3_pending_codes.append((char_code, font_key))
                    type3_suppress_invis = True

                collecting_type3 = False
                type3_glyph_elements = []
                type3_glyph_cache_key = None

        elif isinstance(item, ps.ActualTextStart):
            if type3_suppress_invis or type3_text_batch:
                # Skip invisible overlay, but extract Unicode mappings
                # from ActualText for the ToUnicode CMap
                unicode_text = item.unicode_text
                if unicode_text and type3_pending_codes:
                    if len(unicode_text) == len(type3_pending_codes):
                        for (cc, fk), uch in zip(
                                type3_pending_codes, unicode_text):
                            if fk in type3_fonts:
                                type3_fonts[fk].unicode_map[cc] = uch
                    type3_pending_codes = []
            else:
                _flush_text_batch()
                _close_aniso_batch(lines, gs)
                params = _compute_invisible_text_params(item)
                if params is not None:
                    if (invis_batch and
                            _same_invisible_baseline(invis_batch[-1], params)):
                        invis_batch.append(params)
                    else:
                        _flush_invis_batch()
                        invis_batch.append(params)

        elif isinstance(item, ps.ActualTextEnd):
            pass  # Handled implicitly by batching/flushing

    # Flush any pending Type 3 text batch
    _flush_type3_text()

    # Flush any pending text batch
    _flush_text_batch()

    # Flush any pending invisible text batch
    _flush_invis_batch()

    # Close any open anisotropic stroke batch
    _close_aniso_batch(lines, gs)

    # Close any remaining clip groups
    while clip_depth > 0:
        lines.append(b'Q')
        clip_depth -= 1

    return (b'\n'.join(lines), shading_defs, image_defs, type3_fonts,
            type3_font_counter, type3_page_keys)


def _close_aniso_batch(lines: list[bytes], gs: _GState) -> None:
    """Close an open anisotropic stroke batch (q/cm block)."""
    if gs.aniso_ctm is not None:
        lines.append(b'Q')
        gs.aniso_ctm = None
        gs.invalidate()


def _emit_path(path: ps.Path, close_subpaths: bool = False) -> list[bytes]:
    """Convert a Path to PDF path operators.

    Returns list of operator lines (not yet joined — they get prepended
    to fill/stroke operators).

    Args:
        path: PostScript Path to convert.
        close_subpaths: If True, ensure every subpath ends with ``h``
            (closepath).  Used for clip paths, where the PLRM requires
            implicit closing of all open subpaths.

    Optimizations:
    - Detects axis-aligned rectangles (moveto + 3 lineto + closepath) and
      emits the compact ``re`` operator instead of m/l/l/l/h.
    - Uses ``v`` (curveto where first control point = current point) and
      ``y`` (curveto where second control point = endpoint) shorthand
      when the relevant points coincide.
    """
    ops: list[bytes] = []
    for subpath in path:
        elems = subpath
        n = len(elems)

        # Rectangle detection: moveto + 3 lineto + closepath with
        # axis-aligned edges → emit as a single ``re`` operator.
        if (n == 5
                and isinstance(elems[0], ps.MoveTo)
                and isinstance(elems[1], ps.LineTo)
                and isinstance(elems[2], ps.LineTo)
                and isinstance(elems[3], ps.LineTo)
                and isinstance(elems[4], ps.ClosePath)):
            x0, y0 = elems[0].p.x, elems[0].p.y
            x1, y1 = elems[1].p.x, elems[1].p.y
            x2, y2 = elems[2].p.x, elems[2].p.y
            x3, y3 = elems[3].p.x, elems[3].p.y
            # Check axis-aligned: two edges horizontal, two vertical
            # Pattern 1: right-left-right-left (or up-down-up-down)
            if (x0 == x3 and x1 == x2 and y0 == y1 and y2 == y3):
                w = x1 - x0
                h = y2 - y1
                ops.append(f'{_cfmt(x0)} {_cfmt(y0)} {_cfmt(w)} {_cfmt(h)} re'.encode())
                continue
            # Pattern 2: up-right-down-left (or rotated variant)
            # PDF ``re x y w h`` always traverses horizontal-first:
            # (x,y)→(x+w,y)→(x+w,y+h)→(x,y+h)→close.
            # Start from (x0, y1) so the ``re`` traversal visits
            # the same vertices in the same rotational order as the
            # original vertical-first path, preserving winding direction.
            if (x0 == x1 and x2 == x3 and y1 == y2 and y0 == y3):
                w = x2 - x0
                h = y0 - y1
                ops.append(f'{_cfmt(x0)} {_cfmt(y1)} {_cfmt(w)} {_cfmt(h)} re'.encode())
                continue

        # General path — emit element by element
        cur_x, cur_y = 0.0, 0.0
        for elem in elems:
            if isinstance(elem, ps.MoveTo):
                cur_x, cur_y = elem.p.x, elem.p.y
                ops.append(f'{_cfmt(cur_x)} {_cfmt(cur_y)} m'.encode())
            elif isinstance(elem, ps.LineTo):
                cur_x, cur_y = elem.p.x, elem.p.y
                ops.append(f'{_cfmt(cur_x)} {_cfmt(cur_y)} l'.encode())
            elif isinstance(elem, ps.CurveTo):
                x1, y1 = elem.p1.x, elem.p1.y
                x2, y2 = elem.p2.x, elem.p2.y
                x3, y3 = elem.p3.x, elem.p3.y
                if _cfmt(x1) == _cfmt(cur_x) and _cfmt(y1) == _cfmt(cur_y):
                    # First control point = current point → v operator
                    ops.append(
                        f'{_cfmt(x2)} {_cfmt(y2)} '
                        f'{_cfmt(x3)} {_cfmt(y3)} v'.encode())
                elif _cfmt(x2) == _cfmt(x3) and _cfmt(y2) == _cfmt(y3):
                    # Second control point = endpoint → y operator
                    ops.append(
                        f'{_cfmt(x1)} {_cfmt(y1)} '
                        f'{_cfmt(x3)} {_cfmt(y3)} y'.encode())
                else:
                    ops.append(
                        f'{_cfmt(x1)} {_cfmt(y1)} '
                        f'{_cfmt(x2)} {_cfmt(y2)} '
                        f'{_cfmt(x3)} {_cfmt(y3)} c'.encode())
                cur_x, cur_y = x3, y3
            elif isinstance(elem, ps.ClosePath):
                ops.append(b'h')
        # Ensure subpath is closed when requested (clip paths per PLRM)
        if close_subpaths and elems and not isinstance(elems[-1], ps.ClosePath):
            ops.append(b'h')
    return ops


def _emit_path_transformed(path: ps.Path,
                           ia: float, ib: float, ic: float, id_: float,
                           itx: float, ity: float) -> list[bytes]:
    """Convert a Path to PDF path operators with an affine transform applied.

    Transforms each coordinate (x, y) through the matrix [ia, ib, ic, id_, itx, ity]:
        x' = ia*x + ic*y + itx
        y' = ib*x + id_*y + ity

    Used to map device-space path coordinates back to user space for
    anisotropic stroke rendering.
    """
    ops: list[bytes] = []
    for subpath in path:
        cur_x, cur_y = 0.0, 0.0
        for elem in subpath:
            if isinstance(elem, ps.MoveTo):
                x, y = elem.p.x, elem.p.y
                cur_x = ia*x + ic*y + itx
                cur_y = ib*x + id_*y + ity
                ops.append(f'{_cfmt(cur_x)} {_cfmt(cur_y)} m'.encode())
            elif isinstance(elem, ps.LineTo):
                x, y = elem.p.x, elem.p.y
                cur_x = ia*x + ic*y + itx
                cur_y = ib*x + id_*y + ity
                ops.append(f'{_cfmt(cur_x)} {_cfmt(cur_y)} l'.encode())
            elif isinstance(elem, ps.CurveTo):
                x1, y1 = elem.p1.x, elem.p1.y
                x2, y2 = elem.p2.x, elem.p2.y
                x3, y3 = elem.p3.x, elem.p3.y
                tx1 = ia*x1 + ic*y1 + itx
                ty1 = ib*x1 + id_*y1 + ity
                tx2 = ia*x2 + ic*y2 + itx
                ty2 = ib*x2 + id_*y2 + ity
                tx3 = ia*x3 + ic*y3 + itx
                ty3 = ib*x3 + id_*y3 + ity
                if (_cfmt(tx1) == _cfmt(cur_x)
                        and _cfmt(ty1) == _cfmt(cur_y)):
                    ops.append(
                        f'{_cfmt(tx2)} {_cfmt(ty2)} '
                        f'{_cfmt(tx3)} {_cfmt(ty3)} v'.encode())
                elif (_cfmt(tx2) == _cfmt(tx3)
                      and _cfmt(ty2) == _cfmt(ty3)):
                    ops.append(
                        f'{_cfmt(tx1)} {_cfmt(ty1)} '
                        f'{_cfmt(tx3)} {_cfmt(ty3)} y'.encode())
                else:
                    ops.append(
                        f'{_cfmt(tx1)} {_cfmt(ty1)} '
                        f'{_cfmt(tx2)} {_cfmt(ty2)} '
                        f'{_cfmt(tx3)} {_cfmt(ty3)} c'.encode())
                cur_x, cur_y = tx3, ty3
            elif isinstance(elem, ps.ClosePath):
                ops.append(b'h')
    return ops


def _build_color_op(color_space: str | None, source_color: list | None,
                    device_color: list, stroking: bool) -> bytes:
    """Build a PDF color-setting operator as bytes.

    Uses source color when available for CMYK/Gray preservation.
    Falls back to device color otherwise.
    """
    if color_space == "DeviceCMYK" and source_color and len(source_color) >= 4:
        c, m, y, k = source_color[0], source_color[1], source_color[2], source_color[3]
        op = 'K' if stroking else 'k'
        return f'{_fmt(c)} {_fmt(m)} {_fmt(y)} {_fmt(k)} {op}'.encode()
    elif color_space == "DeviceGray" and source_color and len(source_color) >= 1:
        g = source_color[0]
        op = 'G' if stroking else 'g'
        return f'{_fmt(g)} {op}'.encode()
    else:
        # Fallback: infer color space from component count
        if len(device_color) >= 4:
            c, m, y, k = device_color[0], device_color[1], device_color[2], device_color[3]
            op = 'K' if stroking else 'k'
            return f'{_fmt(c)} {_fmt(m)} {_fmt(y)} {_fmt(k)} {op}'.encode()
        elif len(device_color) == 3:
            r, g, b = device_color[0], device_color[1], device_color[2]
            op = 'RG' if stroking else 'rg'
            return f'{_fmt(r)} {_fmt(g)} {_fmt(b)} {op}'.encode()
        elif len(device_color) == 1:
            v = device_color[0]
            op = 'G' if stroking else 'g'
            return f'{_fmt(v)} {op}'.encode()
        else:
            return b'0 0 0 RG' if stroking else b'0 0 0 rg'


def _emit_color(lines: list[bytes], color_space: str | None,
                source_color: list | None, device_color: list,
                stroking: bool, gs: _GState | None = None) -> None:
    """Emit PDF color-setting operator, suppressing if unchanged."""
    op = _build_color_op(color_space, source_color, device_color, stroking)
    if gs is not None:
        if stroking:
            if gs.stroke_color == op:
                return
            gs.stroke_color = op
        else:
            if gs.fill_color == op:
                return
            gs.fill_color = op
    lines.append(op)


def _emit_fill(lines: list[bytes], path_lines: list[bytes],
               fill: ps.Fill, gs: _GState) -> None:
    """Emit fill operators: color + path + f/f*."""
    _emit_color(lines, fill.color_space, fill.source_color,
                fill.color, stroking=False, gs=gs)
    lines.extend(path_lines)
    if fill.winding_rule == ps.WINDING_EVEN_ODD:
        lines.append(b'f*')
    else:
        lines.append(b'f')


def _emit_glyph_ref(lines: list[bytes], glyph_ref: ps.GlyphRef,
                    gs: _GState, image_defs: list[tuple[str, dict]],
                    image_counter: int) -> int:
    """Replay cached glyph display elements as PDF path+fill operators.

    Looks up the PS-level glyph cache for normalized display elements and
    replays Path + Fill + ImageMask operations translated to the glyph position.

    Returns the updated image_counter.
    """
    path_cache = global_resources.get_glyph_cache()
    cached = path_cache.get(glyph_ref.cache_key)
    if cached is None or not cached.display_elements:
        return image_counter

    ox = glyph_ref.position_x
    oy = glyph_ref.position_y

    path_lines: list[bytes] = []
    for element in cached.display_elements:
        if isinstance(element, ps.Path):
            # Emit path with position offset applied to all coordinates
            path_lines = _emit_path_offset(element, ox, oy)
            # Path will be consumed by the next Fill/Stroke element

        elif isinstance(element, ps.Fill):
            _emit_color(lines, element.color_space, element.source_color,
                        element.color, stroking=False, gs=gs)
            lines.extend(path_lines)
            path_lines = []
            if element.winding_rule == ps.WINDING_EVEN_ODD:
                lines.append(b'f*')
            else:
                lines.append(b'f')

        elif isinstance(element, ps.ImageMaskElement):
            # Translate the cached imagemask CTM to the glyph position
            elem = copy.copy(element)
            if element.ctm is not None:
                elem.ctm = element.ctm.copy()
                elem.ctm[4] += ox
                elem.ctm[5] += oy
            _, image_counter = _emit_image_xobject(
                lines, elem, image_defs, image_counter, gs)

    return image_counter


def _emit_path_offset(path: ps.Path, ox: float, oy: float) -> list[bytes]:
    """Convert a Path to PDF path operators with a position offset.

    Adds (ox, oy) to all coordinates — used for replaying cached glyph paths
    at the target position.
    """
    ops: list[bytes] = []
    for subpath in path:
        cur_x, cur_y = 0.0, 0.0
        for elem in subpath:
            if isinstance(elem, ps.MoveTo):
                cur_x = elem.p.x + ox
                cur_y = elem.p.y + oy
                ops.append(f'{_cfmt(cur_x)} {_cfmt(cur_y)} m'.encode())
            elif isinstance(elem, ps.LineTo):
                cur_x = elem.p.x + ox
                cur_y = elem.p.y + oy
                ops.append(f'{_cfmt(cur_x)} {_cfmt(cur_y)} l'.encode())
            elif isinstance(elem, ps.CurveTo):
                x1 = elem.p1.x + ox
                y1 = elem.p1.y + oy
                x2 = elem.p2.x + ox
                y2 = elem.p2.y + oy
                x3 = elem.p3.x + ox
                y3 = elem.p3.y + oy
                if _cfmt(x1) == _cfmt(cur_x) and _cfmt(y1) == _cfmt(cur_y):
                    ops.append(
                        f'{_cfmt(x2)} {_cfmt(y2)} '
                        f'{_cfmt(x3)} {_cfmt(y3)} v'.encode())
                elif _cfmt(x2) == _cfmt(x3) and _cfmt(y2) == _cfmt(y3):
                    ops.append(
                        f'{_cfmt(x1)} {_cfmt(y1)} '
                        f'{_cfmt(x3)} {_cfmt(y3)} y'.encode())
                else:
                    ops.append(
                        f'{_cfmt(x1)} {_cfmt(y1)} '
                        f'{_cfmt(x2)} {_cfmt(y2)} '
                        f'{_cfmt(x3)} {_cfmt(y3)} c'.encode())
                cur_x, cur_y = x3, y3
            elif isinstance(elem, ps.ClosePath):
                ops.append(b'h')
    return ops


def _get_glyph_name_for_code(font_dict: object, char_code: int) -> str:
    """Get glyph name from font encoding for a character code.

    Falls back to a generated name if the encoding is missing or invalid.
    """
    if font_dict is not None and hasattr(font_dict, 'val'):
        encoding = font_dict.val.get(b'Encoding')
        if (encoding is not None and encoding.TYPE in ps.ARRAY_TYPES
                and char_code < len(encoding.val)):
            glyph_name_obj = encoding.val[char_code]
            if glyph_name_obj.TYPE == ps.T_NAME:
                name = glyph_name_obj.val
                if isinstance(name, bytes):
                    return name.decode('latin-1')
                return str(name)
    return f'c{char_code}'


def _charspace_to_device(char_width: tuple, char_bbox: tuple | None,
                         font_matrix: tuple,
                         ctm_scale: tuple) -> tuple[float, tuple | None]:
    """Transform character-space width and bbox to device space.

    Character space values (from setcachedevice) are transformed through
    FontMatrix × CTM_scale to match the device-space coordinates used by
    the normalized display elements in CharProc content streams.

    Args:
        char_width: (wx, wy) advance width in character space.
        char_bbox: (llx, lly, urx, ury) bounding box or None.
        font_matrix: Font's FontMatrix tuple (a, b, c, d, e, f).
        ctm_scale: CTM scale/rotation part (a, b, c, d).

    Returns:
        (device_width_x, device_bbox) where device_bbox may be None.
    """
    fm_a = font_matrix[0]
    fm_b = font_matrix[1]
    fm_c = font_matrix[2]
    fm_d = font_matrix[3]
    ca, cb, cc, cd = ctm_scale

    # Transform width vector (delta — no translation)
    wx = char_width[0]
    wy = char_width[1] if len(char_width) > 1 else 0.0
    wx_fm = fm_a * wx + fm_c * wy
    wy_fm = fm_b * wx + fm_d * wy
    wx_dev = ca * wx_fm + cc * wy_fm

    if char_bbox is None:
        return wx_dev, None

    # Transform all 4 bbox corners and take extremes
    llx, lly, urx, ury = char_bbox
    dev_xs: list[float] = []
    dev_ys: list[float] = []
    for cx, cy in ((llx, lly), (urx, lly), (urx, ury), (llx, ury)):
        fx = fm_a * cx + fm_c * cy
        fy = fm_b * cx + fm_d * cy
        dev_xs.append(ca * fx + cc * fy)
        dev_ys.append(cb * fx + cd * fy)

    return wx_dev, (min(dev_xs), min(dev_ys), max(dev_xs), max(dev_ys))


def _build_charproc_stream(display_elements: list, char_width: tuple,
                           char_bbox: tuple | None) -> bytes:
    """Convert cached glyph display elements to a PDF CharProc content stream.

    Produces a d1 CharProc (cacheable, shape-only, inherits color from text state).
    The stream starts with 'wx 0 llx lly urx ury d1' followed by path/fill ops.
    ImageMask elements are emitted as inline images (BI/ID/EI) directly in the
    stream, eliminating the need for external XObject references.

    All metrics (char_width, char_bbox) must be in device space (matching the
    display_elements coordinates), NOT character space.  Use _charspace_to_device()
    to transform setcachedevice values before calling this function.

    Args:
        display_elements: Path, Fill, ImageMaskElement elements from glyph cache
            (normalized to origin, in device space).
        char_width: (wx, wy) character advance in device space.
        char_bbox: (llx, lly, urx, ury) bounding box in device space, or None.

    Returns:
        CharProc content stream bytes.
    """
    lines: list[bytes] = []

    wx = char_width[0] if char_width else 0.0

    if char_bbox:
        llx, lly, urx, ury = char_bbox
        lines.append(
            f'{_cfmt(wx)} 0 {_cfmt(llx)} {_cfmt(lly)} '
            f'{_cfmt(urx)} {_cfmt(ury)} d1'.encode())
    else:
        lines.append(f'{_cfmt(wx)} 0 d0'.encode())

    for element in display_elements:
        if isinstance(element, ps.Path):
            path_lines = _emit_path(element)
            # Path will be consumed by the next Fill element
            last_path_lines = path_lines

        elif isinstance(element, ps.Fill):
            # For d1, don't emit color — glyph inherits text color
            if char_bbox:
                lines.extend(last_path_lines)
            else:
                # d0: emit color since glyph defines its own color
                _emit_color(lines, element.color_space, element.source_color,
                            element.color, stroking=False)
                lines.extend(last_path_lines)
            if element.winding_rule == ps.WINDING_EVEN_ODD:
                lines.append(b'f*')
            else:
                lines.append(b'f')
            last_path_lines = []

        elif isinstance(element, ps.ImageMaskElement):
            # Emit imagemask as inline image (BI/ID/EI) within the CharProc
            if element.sample_data is not None:
                cm_line = _compute_image_cm(
                    element.image_matrix, element.ctm,
                    element.width, element.height)
                if cm_line:
                    lines.append(b'q')
                    lines.append(cm_line)
                    # Build inline image
                    bi_parts = [b'BI']
                    bi_parts.append(f'/W {element.width}'.encode())
                    bi_parts.append(f'/H {element.height}'.encode())
                    bi_parts.append(b'/BPC 1')
                    bi_parts.append(b'/IM true')
                    # PostScript: polarity=true means paint where bit=1
                    # PDF: default Decode [0 1] paints where bit=0
                    # So polarity=true needs /D [1 0] to invert
                    if element.polarity:
                        bi_parts.append(b'/D [1 0]')
                    bi_parts.append(b'ID ')
                    header = b'\n'.join(bi_parts)
                    lines.append(header + element.sample_data + b'\nEI')
                    lines.append(b'Q')

    last_path_lines = []  # noqa: F841
    return b'\n'.join(lines)


def _type3_font_key(cache_key: object) -> tuple:
    """Extract the Type 3 font grouping key from a GlyphCacheKey.

    Groups glyphs by font identity, CTM scale, and font matrix —
    color and subpixel_y are excluded because d1 glyphs inherit
    color from the text state.
    """
    return (cache_key.font_id, cache_key.ctm_scale, cache_key.font_matrix)


def _emit_type3_text_run(lines: list[bytes],
                         batch: list[tuple],
                         t3_font: _Type3FontDef,
                         color: tuple,
                         gs: _GState) -> None:
    """Emit a batch of Type 3 characters as BT/ET blocks with TJ arrays.

    Groups consecutive same-baseline characters into TJ runs for proper
    text selection and extraction in PDF viewers.

    Args:
        lines: Output line buffer.
        batch: List of (char_code, x, y, width_x) tuples.
        t3_font: Type 3 font definition.
        color: Fill color tuple for text.
        gs: Graphics state tracker.
    """
    if not batch:
        return

    _emit_text_color(lines, color, gs)

    # Single BT/Tf block for the entire batch — all entries share the
    # same Type 3 font.  Each baseline group gets its own Tm.
    lines.append(b'BT')
    lines.append(f'{t3_font.resource_name} 1 Tf'.encode())

    i = 0
    while i < len(batch):
        char_code, x, y, width_x = batch[i]

        lines.append(
            f'1 0 0 1 {_cfmt(x)} {_cfmt(y)} Tm'.encode())

        # Collect same-baseline chars into a TJ run
        tj_parts: list[str] = [f'<{char_code:02X}>']
        prev_x = x
        prev_width = width_x
        j = i + 1
        while j < len(batch):
            next_code, next_x, next_y, next_width = batch[j]
            # Same baseline = same y within 0.5 device pixels
            if abs(next_y - y) > 0.5:
                break
            # Compute TJ kern: displacement from expected position
            expected_x = prev_x + prev_width
            gap = next_x - expected_x
            kern = round(-gap * 1000)
            if kern != 0:
                tj_parts.append(str(kern))
            tj_parts.append(f'<{next_code:02X}>')
            prev_x = next_x
            prev_width = next_width
            j += 1

        if len(tj_parts) == 1:
            lines.append(f'{tj_parts[0]} Tj'.encode())
        else:
            lines.append(f'[{" ".join(tj_parts)}] TJ'.encode())

        i = j

    lines.append(b'ET')


def _emit_stroke(lines: list[bytes], path_lines: list[bytes],
                 raw_path: ps.Path | None,
                 stroke: ps.Stroke, gs: _GState) -> None:
    """Emit stroke operators: color + state + path + S.

    For anisotropic strokes (different X/Y scale factors in the CTM),
    applies the full CTM via a cm operator and transforms path coordinates
    back to user space so the pen shape is correctly non-uniform.
    Consecutive anisotropic strokes with the same CTM are batched into
    a single q/cm/Q block.
    """
    ctm = stroke.ctm  # (a, b, c, d, tx, ty)
    a, b, c, d = ctm[0], ctm[1], ctm[2], ctm[3]

    # Compute X and Y scale factors from the CTM
    sx = math.sqrt(a * a + b * b)
    sy = math.sqrt(c * c + d * d)

    # Detect anisotropy: scale factors differ by more than 0.1%
    is_anisotropic = (min(sx, sy) > 1e-10 and
                      abs(sx - sy) > 0.001 * max(sx, sy))

    if is_anisotropic and raw_path is not None:
        _emit_stroke_anisotropic(lines, raw_path, stroke, gs)
    else:
        _close_aniso_batch(lines, gs)
        _emit_stroke_isotropic(lines, path_lines, stroke, sx, sy, gs)


def _emit_stroke_state(lines: list[bytes], stroke: ps.Stroke,
                       scale: float, gs: _GState) -> None:
    """Emit stroke state operators, suppressing unchanged values."""
    lw = _fmt(stroke.line_width * scale)
    if gs.line_width != lw:
        lines.append(f'{lw} w'.encode())
        gs.line_width = lw

    if gs.line_cap != stroke.line_cap:
        lines.append(f'{stroke.line_cap} J'.encode())
        gs.line_cap = stroke.line_cap

    if gs.line_join != stroke.line_join:
        lines.append(f'{stroke.line_join} j'.encode())
        gs.line_join = stroke.line_join

    ml = _fmt(stroke.miter_limit)
    if gs.miter_limit != ml:
        lines.append(f'{ml} M'.encode())
        gs.miter_limit = ml

    dashes, offset = stroke.dash_pattern
    if dashes:
        dash_device = [_fmt(d * scale) for d in dashes]
        offset_device = _fmt(offset * scale)
        dash_op = f'[{" ".join(dash_device)}] {offset_device} d'.encode()
    else:
        dash_op = b'[] 0 d'
    if gs.dash_pattern != dash_op:
        lines.append(dash_op)
        gs.dash_pattern = dash_op


def _emit_stroke_isotropic(lines: list[bytes], path_lines: list[bytes],
                           stroke: ps.Stroke,
                           sx: float, sy: float, gs: _GState) -> None:
    """Emit stroke with uniform scaling (isotropic CTM)."""
    _emit_color(lines, stroke.color_space, stroke.source_color,
                stroke.color, stroking=True, gs=gs)

    scale = math.sqrt(sx * sy) if sx > 0 and sy > 0 else 1.0
    _emit_stroke_state(lines, stroke, scale, gs)

    lines.extend(path_lines)
    lines.append(b'S')


def _emit_stroke_anisotropic(lines: list[bytes], raw_path: ps.Path,
                             stroke: ps.Stroke, gs: _GState) -> None:
    """Emit stroke preserving anisotropic pen shape via the full CTM.

    Applies the stroke's CTM as a cm operator, then emits path coordinates
    transformed back to user space through the inverse CTM. Consecutive
    strokes with the same CTM are batched into a single q/cm/Q block.
    """
    ctm = stroke.ctm
    a, b, c, d, tx, ty = ctm
    det = a * d - b * c
    if abs(det) < 1e-10:
        # Degenerate CTM — fall back to isotropic
        _close_aniso_batch(lines, gs)
        sx = math.sqrt(a * a + b * b)
        sy = math.sqrt(c * c + d * d)
        _emit_stroke_isotropic(lines, _emit_path(raw_path), stroke,
                               sx, sy, gs)
        return

    # Check if we can reuse the current anisotropic batch (same CTM)
    ctm_tuple = tuple(ctm)
    if gs.aniso_ctm != ctm_tuple:
        _close_aniso_batch(lines, gs)
        # Open new batch with this CTM
        lines.append(b'q')
        lines.append(
            f'{_fmt(a)} {_fmt(b)} {_fmt(c)} {_fmt(d)} '
            f'{_fmt(tx)} {_fmt(ty)} cm'.encode())
        gs.aniso_ctm = ctm_tuple
        gs.invalidate()

    # Inverse CTM for transforming device-space path coords back to user space
    inv_det = 1.0 / det
    inv_a = d * inv_det
    inv_b = -b * inv_det
    inv_c = -c * inv_det
    inv_d = a * inv_det
    inv_tx = (c * ty - d * tx) * inv_det
    inv_ty = (b * tx - a * ty) * inv_det

    _emit_color(lines, stroke.color_space, stroke.source_color,
                stroke.color, stroking=True, gs=gs)

    # Stroke parameters in user space (no manual scaling needed)
    _emit_stroke_state(lines, stroke, 1.0, gs)

    # Emit path with coordinates transformed from device space to user space
    lines.extend(_emit_path_transformed(raw_path, inv_a, inv_b, inv_c, inv_d,
                                        inv_tx, inv_ty))
    lines.append(b'S')


def _build_text_color_op(color: tuple) -> bytes:
    """Build fill color operator for text, inferring color space from component count."""
    if len(color) >= 4:
        return f'{_fmt(color[0])} {_fmt(color[1])} {_fmt(color[2])} {_fmt(color[3])} k'.encode()
    elif len(color) == 3:
        return f'{_fmt(color[0])} {_fmt(color[1])} {_fmt(color[2])} rg'.encode()
    elif len(color) == 1:
        return f'{_fmt(color[0])} g'.encode()
    else:
        return b'0 g'


def _emit_text_color(lines: list[bytes], color: tuple,
                     gs: _GState | None = None) -> None:
    """Emit fill color for text, suppressing if unchanged."""
    op = _build_text_color_op(color)
    if gs is not None:
        if gs.fill_color == op:
            return
        gs.fill_color = op
    lines.append(op)


def _compute_text_matrix(text_obj: ps.TextObj) -> tuple[float, float, float, float]:
    """Compute text matrix components from TextObj CTM and font matrix."""
    ctm = text_obj.ctm
    ca, cb, cc, cd = ctm[0], ctm[1], ctm[2], ctm[3]
    fm = text_obj.font_matrix

    if fm:
        return (fm[0] * ca + fm[1] * cc,
                fm[0] * cb + fm[1] * cd,
                fm[2] * ca + fm[3] * cc,
                fm[2] * cb + fm[3] * cd)
    else:
        sx = math.sqrt(ca * ca + cb * cb)
        sy = math.sqrt(cc * cc + cd * cd)
        ctm_scale = math.sqrt(sx * sy)
        pt = text_obj.font_size / ctm_scale if ctm_scale > 0 else text_obj.font_size
        return (pt * ca, pt * cb, pt * cc, pt * cd)


def _resolve_text_font(text_obj: ps.TextObj, font_tracker: FontTracker,
                       embedded_fonts: dict) -> str | None:
    """Get PDF resource name for a TextObj's font. Returns None for Standard 14."""
    font_key = font_tracker.get_font_key_for_dict(text_obj.font_dict)
    if font_key:
        font_info = embedded_fonts.get(font_key)
        if font_info:
            return font_info[0]  # resource_name
    font_name = text_obj.font_name
    if font_name in FontTracker.STANDARD_14:
        return '/' + font_name.decode('latin-1')
    return None


def _emit_text_batch(lines: list[bytes], batch: list[ps.TextObj],
                     font_tracker: FontTracker, embedded_fonts: dict,
                     gs: _GState) -> None:
    """Emit a batch of same-font TextObjs as a single BT/ET block.

    All entries in the batch share the same font, so one Tf suffices.
    Each logical text line (run of same-baseline entries) gets its own Tm.
    Within a run, consecutive characters are merged into TJ arrays with
    kern values.  Color operators are emitted inside BT/ET (valid per PDF
    spec) and only when the color changes.
    """
    if not batch:
        return

    pdf_name = _resolve_text_font(batch[0], font_tracker, embedded_fonts)
    if pdf_name is None:
        return

    # Get glyph widths for TJ kern computation
    font_key = font_tracker.get_font_key_for_dict(batch[0].font_dict)
    glyph_widths: dict[int, int] | None = None
    if font_key:
        font_info = embedded_fonts.get(font_key)
        if font_info:
            glyph_widths = _active_font_widths.get(font_key)

    # Group into runs of same baseline (same tm orientation, same
    # perpendicular position).  For horizontal text the baseline is
    # constant-y; for 90° rotated text the baseline is constant-x.
    # The perpendicular direction to the advance vector (tm_a, tm_b)
    # is (-tm_b, tm_a), so the perpendicular component of a position
    # difference (dx, dy) is (-tm_b*dx + tm_a*dy) / |advance|.

    # Single BT/Tf block for the entire batch — Tf is the same for all
    # entries since they share the same font.  Color operators (rg, g, k)
    # are valid inside BT/ET per PDF spec, emitted per baseline group.
    lines.append(b'BT')
    lines.append(f'{pdf_name} 1 Tf'.encode())

    i = 0
    while i < len(batch):
        text_obj = batch[i]

        # Set text color inside BT (valid per PDF spec)
        _emit_text_color(lines, text_obj.color, gs)

        tm_a, tm_b, tm_c, tm_d = _compute_text_matrix(text_obj)
        tm_key = (_fmt(tm_a), _fmt(tm_b), _fmt(tm_c), _fmt(tm_d))
        x = text_obj.start_x
        y = text_obj.start_y

        # Advance vector length squared — used for baseline checks
        adv_len_sq = tm_a * tm_a + tm_b * tm_b

        # Collect consecutive entries on same baseline for TJ
        run: list[tuple[ps.TextObj, float, float]] = [(text_obj, x, y)]
        j = i + 1
        while j < len(batch) and glyph_widths is not None:
            next_obj = batch[j]
            # Check same color
            next_color = _build_text_color_op(next_obj.color)
            if _build_text_color_op(text_obj.color) != next_color:
                break
            # Check same text matrix orientation
            ntm_a, ntm_b, ntm_c, ntm_d = _compute_text_matrix(next_obj)
            ntm_key = (_fmt(ntm_a), _fmt(ntm_b), _fmt(ntm_c), _fmt(ntm_d))
            if ntm_key != tm_key:
                break
            # Check perpendicular distance from baseline
            prev_tobj, prev_x, prev_y = run[-1]
            ndx = next_obj.start_x - prev_x
            ndy = next_obj.start_y - prev_y
            if adv_len_sq > 1e-10:
                adv_len = math.sqrt(adv_len_sq)
                perp_dist = abs(-tm_b * ndx + tm_a * ndy) / adv_len
                if perp_dist > 0.5:
                    break
                # Also check along-direction gap — if the next entry
                # is too far from the previous entry's expected end,
                # they're separate text labels, not consecutive chars.
                along_dist = (ndx * tm_a + ndy * tm_b) / adv_len_sq
                # Estimate previous entry's text width in text units
                prev_text_width = 0.0
                for byte_val in prev_tobj.text:
                    if glyph_widths and byte_val in glyph_widths:
                        prev_text_width += glyph_widths[byte_val]
                # Gap = distance along advance minus text width
                # Allow some tolerance (500 = 0.5 em for word spacing)
                gap = abs(along_dist * 1000.0) - prev_text_width
                if gap > 500:
                    break
            elif abs(ndx) > 0.5 or abs(ndy) > 0.5:
                # Degenerate text matrix — only group at same position
                break
            run.append((next_obj, next_obj.start_x, next_obj.start_y))
            j += 1

        # Emit Tm + text for this baseline group
        lines.append(
            f'{tm_key[0]} {tm_key[1]} {tm_key[2]} {tm_key[3]} '
            f'{_cfmt(x)} {_cfmt(y)} Tm'.encode())

        if len(run) == 1:
            # Single entry — simple Tj
            text_hex = text_obj.text.hex().upper()
            lines.append(f'<{text_hex}> Tj'.encode())
        else:
            # Multiple entries — build TJ array with kern values
            tj_parts: list[str] = []
            for k, (tobj, obj_x, obj_y) in enumerate(run):
                if k > 0:
                    prev_tobj, prev_x, prev_y = run[k - 1]
                    dx = obj_x - prev_x
                    dy = obj_y - prev_y

                    # Compute advance along text direction for kern
                    # TJ kern works along the (tm_a, tm_b) advance axis
                    can_kern = False
                    if adv_len_sq > 1e-10:
                        # Advance in text-space x units
                        text_advance = (dx * tm_a + dy * tm_b) / adv_len_sq
                        prev_char = prev_tobj.text
                        prev_code = prev_char[-1] if len(prev_char) == 1 else None
                        if (prev_code is not None and glyph_widths
                                and prev_code in glyph_widths):
                            prev_width = glyph_widths[prev_code]
                            kern = prev_width - text_advance * 1000.0
                            kern_rounded = round(kern)
                            if kern_rounded != 0:
                                tj_parts.append(str(kern_rounded))
                            can_kern = True

                    if not can_kern:
                        # Can't compute kern — emit position break
                        if tj_parts:
                            lines.append(f'[{" ".join(tj_parts)}] TJ'.encode())
                            tj_parts = []
                        lines.append(
                            f'{tm_key[0]} {tm_key[1]} {tm_key[2]} {tm_key[3]} '
                            f'{_cfmt(obj_x)} {_cfmt(obj_y)} Tm'.encode())

                text_hex = tobj.text.hex().upper()
                tj_parts.append(f'<{text_hex}>')
            if tj_parts:
                lines.append(f'[{" ".join(tj_parts)}] TJ'.encode())

        i = j

    lines.append(b'ET')


def _compute_invisible_text_params(
        actual_text: ps.ActualTextStart) -> tuple | None:
    """Compute positioning parameters for invisible text overlay.

    Returns (x, y, tm_a, tm_b, tm_c, tm_d, text_bytes, adv_x,
             advance_width) or None if text cannot be encoded.
    Coordinates are in device space (the cm transform handles conversion).
    """
    try:
        normalized = unicodedata.normalize('NFKD', actual_text.unicode_text)
        text_bytes = normalized.encode('cp1252', errors='replace')
    except Exception:
        return None

    if not text_bytes:
        return None

    x = actual_text.start_x
    y = actual_text.start_y
    adv_x = x
    advance_width = actual_text.advance_width

    ctm = actual_text.ctm
    ca, cb, cc, cd = ctm[0], ctm[1], ctm[2], ctm[3]

    # Use visual Y bounds to size and position the invisible overlay only
    # when the font bbox is unreliable.  Fonts whose bbox came from the
    # glyph cache (_font_max_bbox, populated by setcachedevice) have
    # accurate font-wide metrics; per-character visual bounds would give
    # inconsistent heights (e.g., lowercase shorter than uppercase).
    vis_min_y = actual_text.visual_min_y
    vis_max_y = actual_text.visual_max_y
    use_visual_y = (vis_min_y is not None and vis_max_y is not None
                    and abs(vis_max_y - vis_min_y) > 0.5 and abs(cd) > 1e-6
                    and not actual_text.bbox_from_cache)

    if use_visual_y:
        visual_height = abs(vis_max_y - vis_min_y)
        point_size = visual_height / abs(cd)
        tm_d = vis_min_y - vis_max_y  # negative when min_y < max_y (standard)
        # Position baseline so Courier ascent (0.8 em) covers the visual top
        # and descent (0.2 em) covers the visual bottom.
        y = 0.2 * vis_min_y + 0.8 * vis_max_y
    else:
        font_size = actual_text.font_size
        bbox = actual_text.font_bbox
        if bbox:
            bbox_height = abs(bbox[3] - bbox[1])
            if bbox_height > 0:
                font_size = font_size * bbox_height / 1000.0
            else:
                font_size = font_size / 1000.0
        else:
            font_size = font_size / 1000.0

        sx = math.sqrt(ca * ca + cb * cb)
        sy = math.sqrt(cc * cc + cd * cd)
        ctm_scale = math.sqrt(sx * sy)
        point_size = font_size / ctm_scale if ctm_scale > 0 else font_size
        tm_d = point_size * cd

    # Text matrix components (device space — cm handles PDF conversion)
    tm_b = point_size * cb
    tm_c = point_size * cc

    # tm_a computed from advance width to match visible text span
    # Courier: each character advance = 0.6 em
    num_chars = len(text_bytes)
    if num_chars > 0 and abs(advance_width) > 0.01:
        tm_a = advance_width / (num_chars * 0.6)
    else:
        tm_a = abs(tm_d) if abs(tm_d) > 0.01 else point_size * ca

    # Use visual_start_x only when the rendering extends to the LEFT of
    # the character origin (e.g., bitfont.ps where the flipped image matrix
    # places the bitmap before the origin).  For fonts with a normal left
    # side bearing (visual_start_x > start_x), keep the character origin.
    vsx = actual_text.visual_start_x
    if vsx is not None and vsx < x:
        x = vsx

    return (x, y, tm_a, tm_b, tm_c, tm_d, text_bytes, adv_x,
            advance_width)


def _flush_invisible_batch(lines: list[bytes],
                           invis_batch: list[tuple]) -> None:
    """Emit batched invisible text entries as a single BT/ET block.

    Concatenates same-baseline fragments with spaces inserted where
    the gap between advance endpoint and next start exceeds a threshold.
    """
    if not invis_batch:
        return

    # x=0, y=1, tm_a=2, tm_b=3, tm_c=4, tm_d=5, text_bytes=6,
    # adv_x=7, advance_width=8
    first = invis_batch[0]
    first_x = first[0]
    tm_b, tm_c, tm_d = first[3], first[4], first[5]
    y = first[1]

    # Space detection threshold: fraction of font height
    font_height = abs(tm_d) if abs(tm_d) > 0.01 else 10.0
    space_threshold = font_height * 0.1

    # Build concatenated text with space detection
    combined_text = bytearray()
    for i, entry in enumerate(invis_batch):
        if i > 0:
            prev = invis_batch[i - 1]
            prev_adv_end = prev[7] + prev[8]  # adv_x + advance_width
            gap = entry[7] - prev_adv_end
            if gap > space_threshold:
                combined_text.extend(b' ')
        combined_text.extend(entry[6])

    if not combined_text:
        return

    # Compute tm_a so Courier glyphs span the full visible width
    first_adv_x = invis_batch[0][7]
    last = invis_batch[-1]
    total_width = (last[7] + last[8]) - first_adv_x
    total_chars = len(combined_text)
    if total_chars > 0 and total_width > 0:
        combined_tm_a = total_width / (total_chars * 0.6)
    else:
        combined_tm_a = font_height

    lines.append(b'BT')
    lines.append(b'3 Tr')
    lines.append(b'/PFCour 1 Tf')
    lines.append(
        f'{_fmt(combined_tm_a)} {_fmt(tm_b)} {_fmt(tm_c)} {_fmt(tm_d)} '
        f'{_cfmt(first_x)} {_cfmt(y)} Tm'.encode())
    text_hex = bytes(combined_text).hex().upper()
    lines.append(f'<{text_hex}> Tj'.encode())
    lines.append(b'0 Tr')
    lines.append(b'ET')


def _same_invisible_baseline(a: tuple, b: tuple) -> bool:
    """Check if two invisible text entries share the same baseline."""
    # Compare tm_b, tm_c, tm_d and y position
    if (abs(a[3] - b[3]) > 0.01 or abs(a[4] - b[4]) > 0.01 or
            abs(a[5] - b[5]) > 0.01):
        return False
    # Y proximity check
    dy = abs(a[1] - b[1])
    perp_len = math.sqrt(a[4] ** 2 + a[5] ** 2)
    if perp_len > 0:
        cross_dist = abs(dy * a[5] / perp_len)
    else:
        cross_dist = dy
    return cross_dist < abs(a[5]) * 0.3 if abs(a[5]) > 0.01 else dy < 1.0


def _compute_image_cm(image_matrix: list | None, ctm: list | None,
                      width: int, height: int) -> bytes | None:
    """Compute the PDF cm operator that places an inline image correctly.

    Maps the PDF unit square (where image data fills top-to-bottom) to the
    correct position in device space, matching PostScript's image placement.

    Returns the cm operator as bytes, or None if matrices are missing/degenerate.
    """
    if not image_matrix or not ctm:
        return None

    im_a, im_b, im_c, im_d, im_tx, im_ty = image_matrix
    det_im = im_a * im_d - im_b * im_c
    if abs(det_im) < 1e-10:
        return None

    # Inverse of image_matrix (maps image pixel space → user space)
    inv = 1.0 / det_im
    ii_a = im_d * inv
    ii_b = -im_b * inv
    ii_c = -im_c * inv
    ii_d = im_a * inv
    ii_tx = (im_c * im_ty - im_d * im_tx) * inv
    ii_ty = (im_b * im_tx - im_a * im_ty) * inv

    # T = inv(image_matrix) × CTM (image pixel space → device space)
    ca, cb, cc, cd, ctx_, cty = ctm
    ta = ii_a * ca + ii_b * cc
    tb = ii_a * cb + ii_b * cd
    tc = ii_c * ca + ii_d * cc
    td = ii_c * cb + ii_d * cd
    ttx = ii_tx * ca + ii_ty * cc + ctx_
    tty = ii_tx * cb + ii_ty * cd + cty

    # PDF cm: maps unit square to device space
    # Unit (ux, uy) → image pixel (ux*w, h - uy*h) → device via T
    w, h = float(width), float(height)
    m_a = ta * w
    m_b = tb * w
    m_c = -tc * h
    m_d = -td * h
    m_tx = tc * h + ttx
    m_ty = td * h + tty

    return (f'{_fmt(m_a)} {_fmt(m_b)} {_fmt(m_c)} {_fmt(m_d)} '
            f'{_fmt(m_tx)} {_fmt(m_ty)} cm').encode()


def _get_indexed_info(color_space: list | str | None) -> tuple[str, bytes] | None:
    """Extract base color space and lookup table from an Indexed color space.

    Returns (base_cs_name, lookup_bytes) or None if not Indexed.
    """
    if not isinstance(color_space, list) or len(color_space) < 4:
        return None
    if color_space[0] != "Indexed":
        return None
    base = color_space[1]
    if isinstance(base, list):
        base = base[0]
    # Convert PS Name object to Python string
    if isinstance(base, ps.PSObject):
        base = base.val
        if isinstance(base, bytes):
            base = base.decode('latin-1')
    lookup = color_space[3]
    if isinstance(lookup, ps.PSObject):
        lookup = lookup.byte_string()
    if isinstance(lookup, str):
        lookup = lookup.encode('latin-1')
    return base, lookup


def _expand_indexed_image(image: ps.ImageElement, lookup: bytes,
                          base_ncomp: int) -> ps.ImageElement:
    """Expand indexed image data to base color space values.

    Replaces each index byte with the corresponding color from the lookup table.
    Returns a modified copy of the image with expanded sample_data.
    """
    data = image.sample_data
    bpc = image.bits_per_component
    w = image.width
    h = image.height

    # Unpack indices from packed bit data
    indices: list[int] = []
    if bpc == 8:
        indices = list(data)
    elif bpc == 4:
        for row in range(h):
            row_start = row * ((w * 4 + 7) // 8)
            col = 0
            byte_idx = row_start
            while col < w and byte_idx < len(data):
                b = data[byte_idx]
                indices.append((b >> 4) & 0x0F)
                col += 1
                if col < w:
                    indices.append(b & 0x0F)
                    col += 1
                byte_idx += 1
    elif bpc == 2:
        for row in range(h):
            row_start = row * ((w * 2 + 7) // 8)
            col = 0
            byte_idx = row_start
            while col < w and byte_idx < len(data):
                b = data[byte_idx]
                for shift in (6, 4, 2, 0):
                    if col < w:
                        indices.append((b >> shift) & 0x03)
                        col += 1
                byte_idx += 1
    elif bpc == 1:
        for row in range(h):
            row_start = row * ((w + 7) // 8)
            col = 0
            byte_idx = row_start
            while col < w and byte_idx < len(data):
                b = data[byte_idx]
                for bit in range(7, -1, -1):
                    if col < w:
                        indices.append((b >> bit) & 1)
                        col += 1
                byte_idx += 1
    else:
        indices = list(data)

    # Look up each index in the palette
    expanded = bytearray()
    for idx in indices:
        offset = idx * base_ncomp
        if offset + base_ncomp <= len(lookup):
            expanded.extend(lookup[offset:offset + base_ncomp])
        else:
            expanded.extend(b'\x00' * base_ncomp)

    # Create a shallow copy with expanded data
    result = copy.copy(image)
    result.sample_data = bytes(expanded)
    result.bits_per_component = 8
    result.components = base_ncomp
    result.decode_array = None  # Reset decode — data is now direct color values
    return result


def _try_color_convert(color_space: list | str | None, sample_data: bytes,
                        width: int, height: int, ncomp: int,
                        decode_array: list | None) -> bytes | None:
    """Try to convert non-device color space image data to RGB.

    Handles ICCBased and CIE-based (CIEBasedABC, CIEBasedA, CIEBasedDEF,
    CIEBasedDEFG) color spaces by converting through the appropriate pipeline
    to produce sRGB output. Returns None if the color space is already a
    device space or conversion fails.
    """
    if (not color_space or not isinstance(color_space, list)
            or len(color_space) < 2):
        return None
    cs_name = color_space[0]
    if isinstance(cs_name, ps.PSObject):
        cs_name = cs_name.val
        if isinstance(cs_name, bytes):
            cs_name = cs_name.decode('latin-1')

    if cs_name == 'ICCBased':
        stream_obj = color_space[1]
        profile_hash = icc_profile.get_profile_hash(stream_obj)
        if profile_hash is not None:
            bgrx = icc_profile.icc_convert_image(
                profile_hash, ncomp, sample_data, width, height, 8,
                decode_array)
            if bgrx is not None:
                # Convert BGRX (Cairo format) → RGB for PDF output
                n_pixels = width * height
                rgb = bytearray(n_pixels * 3)
                for i in range(n_pixels):
                    off = i * 4
                    rgb[i * 3] = bgrx[off + 2]      # R
                    rgb[i * 3 + 1] = bgrx[off + 1]  # G
                    rgb[i * 3 + 2] = bgrx[off]       # B
                return bytes(rgb)
        return None

    elif cs_name in ('CIEBasedABC', 'CIEBasedA'):
        return _convert_cie_to_rgb(sample_data, width, height,
                                   decode_array, color_space, ncomp)

    elif cs_name in ('CIEBasedDEF', 'CIEBasedDEFG'):
        return _convert_cie_def_to_rgb(sample_data, width, height,
                                       decode_array, color_space, ncomp)

    return None


def _convert_cie_to_rgb(sample_data: bytes | bytearray, width: int, height: int,
                        decode_array: list | None, color_space: list,
                        ncomp: int) -> bytes | None:
    """Convert CIEBasedABC or CIEBasedA 8-bit image samples to RGB bytes.

    For the common identity-CIE case (sRGB wrapped as CIEBasedABC), passes
    data through directly.  Otherwise converts each pixel through the full
    CIE pipeline.
    """
    try:
        space_name = color_space[0]
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})

        # Fast path: identity CIE — data is already RGB/gray
        if is_identity_cie(cie_dict, space_name):
            if space_name == "CIEBasedABC":
                return bytes(sample_data[:width * height * 3])
            # CIEBasedA identity: expand gray → RGB
            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)
            for i in range(n_pixels):
                if i < len(sample_data):
                    v = sample_data[i]
                else:
                    v = 0
                off = i * 3
                rgb[off] = v
                rgb[off + 1] = v
                rgb[off + 2] = v
            return bytes(rgb)

        if not decode_array:
            return None

        # Slow path: full CIE conversion per pixel
        if space_name == "CIEBasedABC":
            r_min, r_max = decode_array[0], decode_array[1]
            g_min, g_max = decode_array[2], decode_array[3]
            b_min, b_max = decode_array[4], decode_array[5]

            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)

            for i in range(0, n_pixels * 3, 3):
                if i + 2 >= len(sample_data):
                    break
                a = r_min + (sample_data[i] / 255.0) * (r_max - r_min)
                b = g_min + (sample_data[i + 1] / 255.0) * (g_max - g_min)
                c = b_min + (sample_data[i + 2] / 255.0) * (b_max - b_min)

                r, g, b_ = ColorSpaceEngine.cie_abc_to_rgb([a, b, c], cie_dict)
                px = i  # output index matches input since both are 3-comp
                rgb[px] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[px + 1] = max(0, min(255, int(g * 255 + 0.5)))
                rgb[px + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
            return bytes(rgb)

        elif space_name == "CIEBasedA":
            d_min, d_max = decode_array[0], decode_array[1]

            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)

            for i in range(n_pixels):
                if i >= len(sample_data):
                    break
                a = d_min + (sample_data[i] / 255.0) * (d_max - d_min)
                r, g, b_ = ColorSpaceEngine.cie_a_to_rgb(a, cie_dict)
                off = i * 3
                rgb[off] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[off + 1] = max(0, min(255, int(g * 255 + 0.5)))
                rgb[off + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
            return bytes(rgb)

        return None

    except (ValueError, TypeError, IndexError, KeyError, ZeroDivisionError):
        return None


def _convert_cie_def_to_rgb(sample_data: bytes | bytearray, width: int, height: int,
                             decode_array: list | None, color_space: list,
                             ncomp: int) -> bytes | None:
    """Convert CIEBasedDEF or CIEBasedDEFG 8-bit image samples to RGB bytes.

    For CIEBasedDEF: pre-converts the 3D lookup table through the full CIE
    pipeline once, then uses fast trilinear interpolation per pixel.
    For CIEBasedDEFG: per-pixel conversion through cie_defg_to_rgb.
    """
    try:
        space_name = color_space[0]
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})

        if not decode_array:
            return None

        if space_name == "CIEBasedDEF":
            preconv = preconvert_cie_def_table(cie_dict)
            if preconv is None:
                # No Table — pass through as RGB
                return bytes(sample_data[:width * height * 3])

            r_tab, g_tab, b_tab, m1, m2, m3 = preconv
            range_def = _get_cie_float_array(cie_dict, b"RangeDEF",
                                             [0, 1, 0, 1, 0, 1])

            d_min, d_max = decode_array[0], decode_array[1]
            e_min, e_max = decode_array[2], decode_array[3]
            f_min, f_max = decode_array[4], decode_array[5]

            d_range = range_def[1] - range_def[0] if range_def[1] != range_def[0] else 1.0
            e_range = range_def[3] - range_def[2] if range_def[3] != range_def[2] else 1.0
            f_range = range_def[5] - range_def[4] if range_def[5] != range_def[4] else 1.0
            d_scale = (d_max - d_min) / 255.0
            e_scale = (e_max - e_min) / 255.0
            f_scale = (f_max - f_min) / 255.0
            m1_f = float(m1 - 1)
            m2_f = float(m2 - 1)
            m3_f = float(m3 - 1)
            stride_e = m3
            stride_d = m2 * m3

            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)
            out_idx = 0

            for i in range(0, n_pixels * 3, 3):
                if i + 2 >= len(sample_data):
                    break

                d = d_min + sample_data[i] * d_scale
                e = e_min + sample_data[i + 1] * e_scale
                f = f_min + sample_data[i + 2] * f_scale

                di = max(0.0, min(m1_f, (d - range_def[0]) / d_range * m1_f))
                ei = max(0.0, min(m2_f, (e - range_def[2]) / e_range * m2_f))
                fi = max(0.0, min(m3_f, (f - range_def[4]) / f_range * m3_f))

                di0 = int(di); ei0 = int(ei); fi0 = int(fi)
                di1 = min(di0 + 1, m1 - 1)
                ei1 = min(ei0 + 1, m2 - 1)
                fi1 = min(fi0 + 1, m3 - 1)
                dd = di - di0; de = ei - ei0; df = fi - fi0
                dd1 = 1.0 - dd; de1 = 1.0 - de; df1 = 1.0 - df

                i000 = di0 * stride_d + ei0 * stride_e + fi0
                i001 = di0 * stride_d + ei0 * stride_e + fi1
                i010 = di0 * stride_d + ei1 * stride_e + fi0
                i011 = di0 * stride_d + ei1 * stride_e + fi1
                i100 = di1 * stride_d + ei0 * stride_e + fi0
                i101 = di1 * stride_d + ei0 * stride_e + fi1
                i110 = di1 * stride_d + ei1 * stride_e + fi0
                i111 = di1 * stride_d + ei1 * stride_e + fi1

                r = (((r_tab[i000] * df1 + r_tab[i001] * df) * de1 +
                      (r_tab[i010] * df1 + r_tab[i011] * df) * de) * dd1 +
                     ((r_tab[i100] * df1 + r_tab[i101] * df) * de1 +
                      (r_tab[i110] * df1 + r_tab[i111] * df) * de) * dd)
                g = (((g_tab[i000] * df1 + g_tab[i001] * df) * de1 +
                      (g_tab[i010] * df1 + g_tab[i011] * df) * de) * dd1 +
                     ((g_tab[i100] * df1 + g_tab[i101] * df) * de1 +
                      (g_tab[i110] * df1 + g_tab[i111] * df) * de) * dd)
                b_ = (((b_tab[i000] * df1 + b_tab[i001] * df) * de1 +
                       (b_tab[i010] * df1 + b_tab[i011] * df) * de) * dd1 +
                      ((b_tab[i100] * df1 + b_tab[i101] * df) * de1 +
                       (b_tab[i110] * df1 + b_tab[i111] * df) * de) * dd)

                rgb[out_idx] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[out_idx + 1] = max(0, min(255, int(g * 255 + 0.5)))
                rgb[out_idx + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
                out_idx += 3

            return bytes(rgb)

        elif space_name == "CIEBasedDEFG":
            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)
            out_idx = 0

            for i in range(0, n_pixels * 4, 4):
                if i + 3 >= len(sample_data):
                    break
                d = decode_array[0] + (sample_data[i] / 255.0) * (decode_array[1] - decode_array[0])
                e = decode_array[2] + (sample_data[i + 1] / 255.0) * (decode_array[3] - decode_array[2])
                f = decode_array[4] + (sample_data[i + 2] / 255.0) * (decode_array[5] - decode_array[4])
                g = decode_array[6] + (sample_data[i + 3] / 255.0) * (decode_array[7] - decode_array[6])

                r, gv, b_ = ColorSpaceEngine.cie_defg_to_rgb([d, e, f, g], cie_dict)
                rgb[out_idx] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[out_idx + 1] = max(0, min(255, int(gv * 255 + 0.5)))
                rgb[out_idx + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
                out_idx += 3

            return bytes(rgb)

        return None

    except (ValueError, TypeError, IndexError, KeyError, ZeroDivisionError):
        return None


def _convert_12bit_to_8bit(data: bytes, width: int, height: int,
                            ncomp: int) -> bytes:
    """Convert 12-bit-per-component image data to 8-bit.

    PDF only supports BPC 1, 2, 4, 8, 16 — PostScript also allows 12-bit.
    Extracts each 12-bit sample and scales from 0-4095 to 0-255.
    Input data is bit-packed with row padding to byte boundaries.
    """
    samples_per_row = width * ncomp
    bits_per_row = samples_per_row * 12
    bytes_per_row = (bits_per_row + 7) // 8

    out = bytearray(width * height * ncomp)
    out_idx = 0

    for row in range(height):
        row_start = row * bytes_per_row
        for s in range(samples_per_row):
            bit_pos = s * 12
            byte_idx = row_start + bit_pos // 8
            bit_offset = bit_pos % 8

            if bit_offset == 0:
                # Sample starts at byte boundary — top 8 bits of byte + top 4 of next
                if byte_idx + 1 < len(data):
                    val = ((data[byte_idx] << 4) |
                           (data[byte_idx + 1] >> 4))
                elif byte_idx < len(data):
                    val = data[byte_idx] << 4
                else:
                    val = 0
            else:
                # bit_offset == 4 — bottom 4 bits of byte + all 8 of next
                if byte_idx + 1 < len(data):
                    val = (((data[byte_idx] & 0x0F) << 8) |
                           data[byte_idx + 1])
                elif byte_idx < len(data):
                    val = (data[byte_idx] & 0x0F) << 8
                else:
                    val = 0

            # Scale 12-bit (0-4095) to 8-bit (0-255)
            out[out_idx] = (val * 255 + 2047) // 4095
            out_idx += 1

    return bytes(out)


def _classify_image_color_space(image: ps.ImageElement) -> tuple[str, dict]:
    """Determine how an image's color space should be embedded in PDF.

    Returns (strategy, params) where strategy is one of:
      'device'       — DeviceGray/RGB/CMYK, params={'cs': '/G'|'/RGB'|'/CMYK'}
      'icc'          — ICCBased with valid profile, params={'hash': bytes, 'n': int}
      'calrgb'       — CIEBasedABC mappable to CalRGB, params={WhitePoint, ...}
      'calgray'      — CIEBasedA mappable to CalGray, params={WhitePoint, ...}
      'convert_rgb'  — must convert to RGB first
    """
    if isinstance(image, ps.ImageMaskElement):
        return ('device', {'cs': None})

    if isinstance(image, ps.ColorImageElement):
        cs_map = {'DeviceGray': '/G', 'DeviceRGB': '/RGB', 'DeviceCMYK': '/CMYK'}
        return ('device', {'cs': cs_map.get(image.color_space_name, '/RGB')})

    # Regular image — examine color space
    color_space = image.color_space
    if not color_space or not isinstance(color_space, list) or len(color_space) < 2:
        # Simple device space — infer from component count
        cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
        return ('device', {'cs': cs_map.get(image.components, '/RGB')})

    cs_name = color_space[0]
    if isinstance(cs_name, ps.PSObject):
        cs_name = cs_name.val
        if isinstance(cs_name, bytes):
            cs_name = cs_name.decode('latin-1')

    if cs_name == 'ICCBased':
        stream_obj = color_space[1]
        profile_hash = icc_profile.get_profile_hash(stream_obj)
        if profile_hash is not None and icc_profile.get_icc_bytes(profile_hash) is not None:
            return ('icc', {'hash': profile_hash, 'n': image.components})
        # ICC profile not available — fall back to device space based on N
        cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
        return ('device', {'cs': cs_map.get(image.components, '/RGB')})

    if cs_name == 'CIEBasedABC':
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})
        if is_identity_cie(cie_dict, 'CIEBasedABC'):
            return ('device', {'cs': '/RGB'})
        cal_params = _try_extract_calrgb(cie_dict)
        if cal_params is not None:
            return ('calrgb', cal_params)
        return ('convert_rgb', {})

    if cs_name == 'CIEBasedA':
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})
        if is_identity_cie(cie_dict, 'CIEBasedA'):
            return ('device', {'cs': '/G'})
        cal_params = _try_extract_calgray(cie_dict)
        if cal_params is not None:
            return ('calgray', cal_params)
        return ('convert_rgb', {})

    if cs_name in ('CIEBasedDEF', 'CIEBasedDEFG'):
        return ('convert_rgb', {})

    # Unknown — infer device from components
    cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
    return ('device', {'cs': cs_map.get(image.components, '/RGB')})


def _try_extract_calrgb(cie_dict: dict) -> dict | None:
    """Try to extract CalRGB parameters from a CIEBasedABC dict.

    Returns dict with WhitePoint and optional BlackPoint, Gamma, Matrix
    if the CIE space can be represented as PDF CalRGB. Returns None if
    decode procedures or non-identity MatrixABC are present.
    """
    # Can't map arbitrary PS procedures to PDF
    if b'DecodeABC' in cie_dict or b'DecodeLMN' in cie_dict:
        return None

    # MatrixABC must be identity or absent
    mat_abc = _get_cie_float_array(cie_dict, b'MatrixABC', None)
    if mat_abc is not None and mat_abc != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
        return None

    white_point = _get_cie_float_array(cie_dict, b'WhitePoint', None)
    if white_point is None or len(white_point) < 3:
        return None

    params: dict = {'WhitePoint': white_point[:3]}

    black_point = _get_cie_float_array(cie_dict, b'BlackPoint', None)
    if black_point is not None and black_point != [0, 0, 0]:
        params['BlackPoint'] = black_point[:3]

    # RangeABC → CalRGB Gamma (PDF CalRGB has per-component gamma)
    range_abc = _get_cie_float_array(cie_dict, b'RangeABC', [0, 1, 0, 1, 0, 1])
    # CalRGB doesn't support arbitrary ranges — only standard [0,1]
    if range_abc != [0, 1, 0, 1, 0, 1]:
        return None

    # MatrixLMN → CalRGB Matrix (same column-major 9-element format)
    mat_lmn = _get_cie_float_array(cie_dict, b'MatrixLMN', None)
    if mat_lmn is not None and mat_lmn != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
        params['Matrix'] = mat_lmn[:9]

    return params


def _try_extract_calgray(cie_dict: dict) -> dict | None:
    """Try to extract CalGray parameters from a CIEBasedA dict.

    Returns dict with WhitePoint and optional BlackPoint, Gamma
    if the CIE space can be represented as PDF CalGray. Returns None if
    decode procedures are present.
    """
    if b'DecodeA' in cie_dict or b'DecodeLMN' in cie_dict:
        return None

    white_point = _get_cie_float_array(cie_dict, b'WhitePoint', None)
    if white_point is None or len(white_point) < 3:
        return None

    # MatrixA must be uniform [k k k] or absent
    mat_a = _get_cie_float_array(cie_dict, b'MatrixA', None)
    if mat_a is not None:
        if len(mat_a) < 3:
            return None
        if not (abs(mat_a[0] - mat_a[1]) < 1e-6 and
                abs(mat_a[1] - mat_a[2]) < 1e-6):
            return None

    params: dict = {'WhitePoint': white_point[:3]}

    black_point = _get_cie_float_array(cie_dict, b'BlackPoint', None)
    if black_point is not None and black_point != [0, 0, 0]:
        params['BlackPoint'] = black_point[:3]

    return params


def _emit_image_xobject(lines: list[bytes], image: ps.ImageElement,
                        image_defs: list[tuple[str, dict]],
                        image_counter: int,
                        gs: _GState) -> tuple[str | None, int]:
    """Build an image description dict and emit an XObject reference.

    All images (including device-color and imagemasks) become XObject
    references in the content stream. The actual XObject is built later
    by pdf_builder.

    Returns (image_resource_name, updated_image_counter).
    """
    if image.sample_data is None:
        return (None, image_counter)

    width = image.width
    height = image.height
    bpc = image.bits_per_component
    is_mask = isinstance(image, ps.ImageMaskElement)

    # Handle Indexed color spaces — expand before classification
    if not is_mask and not isinstance(image, ps.ColorImageElement):
        indexed_info = _get_indexed_info(image.color_space)
        if indexed_info is not None:
            base_cs, lookup = indexed_info
            base_ncomp_map = {'DeviceGray': 1, 'DeviceRGB': 3, 'DeviceCMYK': 4}
            base_ncomp = base_ncomp_map.get(base_cs, 3)
            image = _expand_indexed_image(image, lookup, base_ncomp)
            width = image.width
            height = image.height
            bpc = image.bits_per_component

    # Convert 12-bit to 8-bit (PDF doesn't support 12-bit BPC)
    sample_data = image.sample_data
    ncomp = image.components
    original_bpc = bpc
    if bpc == 12 and not is_mask:
        sample_data = _convert_12bit_to_8bit(sample_data, width, height, ncomp)
        bpc = 8

    # Classify the image color space
    strategy, params = _classify_image_color_space(image)

    # For convert_rgb: run ICC/CIE conversion, then treat as device RGB
    icc_converted = False
    if strategy == 'convert_rgb' and bpc == 8:
        rgb_data = _try_color_convert(image.color_space, sample_data,
                                       width, height, ncomp,
                                       image.decode_array)
        if rgb_data is not None:
            sample_data = rgb_data
            ncomp = 3
            strategy = 'device'
            params = {'cs': '/RGB'}
            icc_converted = True
        else:
            # Conversion failed — fall back to device space from components
            cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
            strategy = 'device'
            params = {'cs': cs_map.get(ncomp, '/RGB')}

    # Build image description dict
    desc: dict = {
        'width': width,
        'height': height,
        'bpc': bpc,
        'sample_data': sample_data,
        'color_space_type': strategy,
        'device_cs': params.get('cs'),
        'icc_hash': params.get('hash'),
        'icc_n': params.get('n'),
        'cal_params': params if strategy in ('calrgb', 'calgray') else None,
        'interpolate': image.interpolate,
        'is_mask': is_mask,
        'mask_polarity': getattr(image, 'polarity', None) if is_mask else None,
        'mask_color': tuple(image.color) if is_mask else None,
    }

    # Type 3 stencil mask data
    stencil_mask = getattr(image, 'stencil_mask', None)
    if stencil_mask is not None and not is_mask:
        desc['stencil_mask'] = stencil_mask
        desc['stencil_mask_width'] = getattr(
            image, 'stencil_mask_width', width)
        desc['stencil_mask_height'] = getattr(
            image, 'stencil_mask_height', height)
        desc['stencil_mask_polarity'] = getattr(
            image, 'stencil_mask_polarity', True)
    else:
        desc['stencil_mask'] = None

    # Type 4 color key mask (MaskColor) → PDF /Mask array
    raw_mask = getattr(image, 'mask_color', None)
    if raw_mask is not None and not is_mask:
        mask_vals = [int(v) for v in raw_mask]
        # Scale mask values if 12-bit was converted to 8-bit
        if original_bpc == 12 and bpc == 8:
            mask_vals = [(v * 255 + 2047) // 4095 for v in mask_vals]
        # PDF /Mask is always 2n values (ranges).  Convert exact match
        # (n values) to min=max range pairs.
        if len(mask_vals) == ncomp:
            pdf_mask = []
            for v in mask_vals:
                pdf_mask.extend([v, v])
            mask_vals = pdf_mask
        desc['color_key_mask'] = mask_vals
    else:
        desc['color_key_mask'] = None

    # Decode array — skip if ICC-converted (decode already applied)
    if is_mask:
        desc['decode_array'] = None  # Handled via mask_polarity
    elif icc_converted:
        desc['decode_array'] = None
    elif image.decode_array:
        desc['decode_array'] = list(image.decode_array)
    else:
        desc['decode_array'] = None

    # Assign resource name and register
    img_name = f'/Im{image_counter}'
    image_counter += 1
    image_defs.append((img_name, desc))

    # Emit content stream reference
    lines.append(b'q')

    # For imagemask, set fill color before the Do
    if is_mask:
        _emit_text_color(lines, image.color, gs)

    cm_line = _compute_image_cm(image.image_matrix, image.ctm, width, height)
    if cm_line:
        lines.append(cm_line)

    lines.append(f'{img_name} Do'.encode())
    lines.append(b'Q')

    return (img_name, image_counter)


def _emit_shading_ref(lines: list[bytes], sh_name: str,
                      ctm: tuple) -> None:
    """Emit a shading reference: q {CTM} cm /ShN sh Q.

    The CTM transforms user-space shading coordinates to device space.
    """
    a, b, c, d, tx, ty = ctm
    lines.append(b'q')
    lines.append(
        f'{_fmt(a)} {_fmt(b)} {_fmt(c)} {_fmt(d)} '
        f'{_fmt(tx)} {_fmt(ty)} cm'.encode())
    lines.append(f'{sh_name} sh'.encode())
    lines.append(b'Q')


def _encode_coord_16(value: float, vmin: float, vmax: float) -> bytes:
    """Map a float coordinate to a 16-bit unsigned integer using Decode range.

    Returns 2 bytes (big-endian).
    """
    span = vmax - vmin
    if span == 0:
        return b'\x00\x00'
    t = (value - vmin) / span
    t = max(0.0, min(1.0, t))
    ival = int(t * 65535.0 + 0.5)
    return ival.to_bytes(2, 'big')


def _build_axial_shading(shading: ps.AxialShadingFill) -> dict | None:
    """Build a Type 2 (Axial) PDF shading description dict.

    Creates a Type 0 sampled function from the color stops and returns
    a shading dict with coordinates in user space.
    """
    if not shading.color_stops:
        return None

    # Build Type 0 sampled function: 65 uniformly spaced RGB samples
    n_samples = 65
    stops = shading.color_stops
    stream_data = bytearray(n_samples * 3)
    for i in range(n_samples):
        t = i / (n_samples - 1)
        r, g, b = _interpolate_color_stops(stops, t)
        stream_data[i * 3] = _clamp_byte(r)
        stream_data[i * 3 + 1] = _clamp_byte(g)
        stream_data[i * 3 + 2] = _clamp_byte(b)

    func_desc = {
        'type': 0,
        'domain': [0.0, 1.0],
        'range': [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'size': [n_samples],
        'bps': 8,
        'order': 1,
        'stream_data': bytes(stream_data),
    }

    desc: dict = {
        'type': 2,
        'color_space': 'DeviceRGB',
        'coords': [shading.x0, shading.y0, shading.x1, shading.y1],
        'extend': [shading.extend_start, shading.extend_end],
        'function': func_desc,
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc


def _build_radial_shading(shading: ps.RadialShadingFill) -> dict | None:
    """Build a Type 3 (Radial) PDF shading description dict.

    Same as axial but with ShadingType 3 and 6-element Coords.
    """
    if not shading.color_stops:
        return None

    n_samples = 65
    stops = shading.color_stops
    stream_data = bytearray(n_samples * 3)
    for i in range(n_samples):
        t = i / (n_samples - 1)
        r, g, b = _interpolate_color_stops(stops, t)
        stream_data[i * 3] = _clamp_byte(r)
        stream_data[i * 3 + 1] = _clamp_byte(g)
        stream_data[i * 3 + 2] = _clamp_byte(b)

    func_desc = {
        'type': 0,
        'domain': [0.0, 1.0],
        'range': [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'size': [n_samples],
        'bps': 8,
        'order': 1,
        'stream_data': bytes(stream_data),
    }

    desc: dict = {
        'type': 3,
        'color_space': 'DeviceRGB',
        'coords': [shading.x0, shading.y0, shading.r0,
                    shading.x1, shading.y1, shading.r1],
        'extend': [shading.extend_start, shading.extend_end],
        'function': func_desc,
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc


def _interpolate_color_stops(stops: list[tuple[float, tuple[float, float, float]]],
                              t: float) -> tuple[float, float, float]:
    """Linearly interpolate color stops at parameter t in [0, 1]."""
    if not stops:
        return (0.0, 0.0, 0.0)
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t0 <= t <= t1:
            span = t1 - t0
            if span < 1e-10:
                return c0
            f = (t - t0) / span
            return (c0[0] + f * (c1[0] - c0[0]),
                    c0[1] + f * (c1[1] - c0[1]),
                    c0[2] + f * (c1[2] - c0[2]))
    return stops[-1][1]


def _clamp_byte(v: float) -> int:
    """Clamp a float [0, 1] to a byte [0, 255]."""
    return max(0, min(255, int(v * 255.0 + 0.5)))


def _emit_function_shading(lines: list[bytes],
                           shading: ps.FunctionShadingFill) -> None:
    """Emit function shading as an inline image."""
    if not shading.pixel_data:
        return

    # The pixel_data is ARGB32 (BGRA on little-endian). Convert to RGB.
    w, h = shading.width, shading.height
    rgb_data = bytearray(w * h * 3)
    for i in range(w * h):
        offset = i * 4
        rgb_data[i * 3] = shading.pixel_data[offset + 2]      # R
        rgb_data[i * 3 + 1] = shading.pixel_data[offset + 1]  # G
        rgb_data[i * 3 + 2] = shading.pixel_data[offset]      # B

    # Compute placement matrix from shading matrix × CTM
    sm = shading.matrix  # pixel → user
    ctm = shading.ctm    # user → device
    a1, b1, c1, d1, tx1, ty1 = sm
    a2, b2, c2, d2, tx2, ty2 = ctm
    cm_a = a1 * a2 + b1 * c2
    cm_b = a1 * b2 + b1 * d2
    cm_c = c1 * a2 + d1 * c2
    cm_d = c1 * b2 + d1 * d2
    cm_tx = tx1 * a2 + ty1 * c2 + tx2
    cm_ty = tx1 * b2 + ty1 * d2 + ty2

    # The rasterization stores domain y_min at pixel row 0 (image top).
    # PDF images render row 0 at the top of the unit square (y=1), so
    # unit (0,0) — image bottom — would land at the domain-bottom position,
    # placing domain-bottom at the visual top after the global Y-flip.
    # Fix by negating the Y basis and shifting the origin so the image
    # top (row 0, domain y_min) lands at the domain-bottom position.
    cm_c_h = cm_c * h
    cm_d_h = cm_d * h
    lines.append(b'q')
    lines.append(
        f'{_fmt(cm_a * w)} {_fmt(cm_b * w)} {_fmt(-cm_c_h)} {_fmt(-cm_d_h)} '
        f'{_fmt(cm_tx + cm_c_h)} {_fmt(cm_ty + cm_d_h)} cm'.encode())

    lines.append(b'BI')
    lines.append(f'/W {w}'.encode())
    lines.append(f'/H {h}'.encode())
    lines.append(b'/BPC 8')
    lines.append(b'/CS /RGB')
    lines.append(b'ID')
    lines.append(bytes(rgb_data))
    lines.append(b'EI')
    lines.append(b'Q')


def _build_mesh_shading(shading: ps.MeshShadingFill) -> dict | None:
    """Build a Type 4 (Free-form Gouraud) PDF shading description dict.

    Encodes triangles as binary stream with 16-bit coordinates and 8-bit
    color components. Coordinates are in user space (CTM applied via cm).
    """
    if not shading.triangles:
        return None

    # Compute coordinate bounds from all vertices (user space)
    all_x: list[float] = []
    all_y: list[float] = []
    for tri in shading.triangles:
        for (x, y), _ in tri:
            all_x.append(x)
            all_y.append(y)

    if not all_x:
        return None

    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)
    # Add small padding to avoid edge clipping
    pad = max(xmax - xmin, ymax - ymin) * 0.001
    if pad < 0.01:
        pad = 0.01
    xmin -= pad
    xmax += pad
    ymin -= pad
    ymax += pad

    # Encode triangles: each vertex = flag(1) + x(2) + y(2) + R(1) + G(1) + B(1)
    stream = bytearray()
    for tri in shading.triangles:
        for (x, y), (r, g, b) in tri:
            stream.append(0)  # flag = 0 (new triangle vertex)
            stream.extend(_encode_coord_16(x, xmin, xmax))
            stream.extend(_encode_coord_16(y, ymin, ymax))
            stream.append(_clamp_byte(r))
            stream.append(_clamp_byte(g))
            stream.append(_clamp_byte(b))

    desc: dict = {
        'type': 4,
        'color_space': 'DeviceRGB',
        'bits_per_coordinate': 16,
        'bits_per_component': 8,
        'bits_per_flag': 8,
        'decode': [xmin, xmax, ymin, ymax, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'stream_data': bytes(stream),
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc


def _build_patch_shading(shading: ps.PatchShadingFill) -> dict | None:
    """Build a Type 6 (Coons) or Type 7 (Tensor-product) PDF shading dict.

    Detects type from control point count (12 = Coons, 16 = Tensor).
    Encodes as binary stream with 16-bit coordinates and 8-bit colors.
    """
    if not shading.patches:
        return None

    # Determine shading type from first patch's point count
    first_points = shading.patches[0][0]
    if len(first_points) >= 16:
        shading_type = 7  # Tensor-product
        n_points = 16
    elif len(first_points) >= 12:
        shading_type = 6  # Coons
        n_points = 12
    else:
        return None

    # Compute coordinate bounds from all control points (user space)
    all_x: list[float] = []
    all_y: list[float] = []
    for points, _ in shading.patches:
        for x, y in points[:n_points]:
            all_x.append(x)
            all_y.append(y)

    if not all_x:
        return None

    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)
    pad = max(xmax - xmin, ymax - ymin) * 0.001
    if pad < 0.01:
        pad = 0.01
    xmin -= pad
    xmax += pad
    ymin -= pad
    ymax += pad

    # Encode patches: flag(1) + n_points × (x(2) + y(2)) + 4 × (R(1) + G(1) + B(1))
    stream = bytearray()
    for points, colors in shading.patches:
        stream.append(0)  # flag = 0 (new patch)
        for x, y in points[:n_points]:
            stream.extend(_encode_coord_16(x, xmin, xmax))
            stream.extend(_encode_coord_16(y, ymin, ymax))
        for r, g, b in colors[:4]:
            stream.append(_clamp_byte(r))
            stream.append(_clamp_byte(g))
            stream.append(_clamp_byte(b))

    desc: dict = {
        'type': shading_type,
        'color_space': 'DeviceRGB',
        'bits_per_coordinate': 16,
        'bits_per_component': 8,
        'bits_per_flag': 8,
        'decode': [xmin, xmax, ymin, ymax, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'stream_data': bytes(stream),
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc
