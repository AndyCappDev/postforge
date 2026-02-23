# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PDF Content Stream Generator

Converts PostScript display list elements to PDF content stream operators.
Preserves original color space information (CMYK, Gray, RGB) instead of
converting everything to RGB.

This module is the orchestrator: it dispatches display list elements to
focused submodules (stroke_ops, text_ops, type3_ops, image_ops, shading_ops).
"""

from ...core import types as ps
from ...core.types.context import global_resources
from .font_tracker import FontTracker
from ._common import _fmt, _GState, _Type3GlyphDef, _Type3FontDef
from .stroke_ops import (_emit_path, _emit_fill, _emit_stroke,
                         _close_aniso_batch, _emit_color)
from .text_ops import (_emit_text_batch, _flush_invisible_batch,
                       _same_invisible_baseline, _compute_invisible_text_params,
                       _resolve_text_font)
from .type3_ops import (_type3_font_key, _emit_type3_text_run,
                        _charspace_to_device, _build_charproc_stream,
                        _get_glyph_name_for_code, _emit_glyph_ref)
from .image_ops import _emit_image_xobject
from .shading_ops import (_emit_shading_ref, _build_axial_shading,
                          _build_radial_shading, _emit_function_shading,
                          _build_mesh_shading, _build_patch_shading)


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
    # Type 3 text batching: consecutive same-font/color chars -> single BT/ET
    type3_text_batch: list[tuple] = []  # (char_code, x, y, width_x)
    type3_batch_font_key: tuple | None = None
    type3_batch_color: tuple | None = None
    type3_suppress_invis = False  # suppress ActualTextStart for Type 3 text
    # Pending Type 3 char codes for ActualText -> ToUnicode correlation
    type3_pending_codes: list[tuple] = []  # (char_code, font_key)

    def _flush_text_batch() -> None:
        nonlocal text_batch, text_batch_font
        if text_batch:
            _emit_text_batch(lines, text_batch, font_tracker,
                             embedded_fonts, gs, font_widths_cache)
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
                # initclip resets to page bounds -- pop ALL nested clip groups
                while clip_depth > 0:
                    lines.append(b'Q')
                    clip_depth -= 1
                gs.invalidate()
            elif item.path:
                # Nest within existing clip group(s) -- PDF W operator
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
            # PatternFill is not yet supported
            current_path = None
            current_path_lines = []

        elif isinstance(item, ps.GlyphRef):
            _flush_text_batch()
            _close_aniso_batch(lines, gs)

            # Try to emit as Type 3 font reference
            path_cache = global_resources.get_glyph_cache()
            cached = path_cache.get(item.cache_key)
            if cached is not None and cached.char_bbox is not None:
                # Cacheable glyph (d1) -- emit as Type 3 font character
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
                # Non-cacheable or missing -- fall back to inline paths
                _flush_type3_text()
                type3_suppress_invis = False
                image_counter = _emit_glyph_ref(
                    lines, item, gs, image_defs, image_counter)

        elif isinstance(item, ps.GlyphStart):
            # Check if this is a cacheable glyph (has char_bbox in cache)
            path_cache = global_resources.get_glyph_cache()
            cached = path_cache.get(item.cache_key)
            if cached is not None and cached.char_bbox is not None:
                # Cacheable (d1) -- collect elements for CharProc
                collecting_type3 = True
                type3_glyph_elements = []
                type3_glyph_cache_key = item.cache_key
                type3_glyph_pos = (item.position_x, item.position_y)
            # else: non-cacheable -- elements flow through normal dispatch

        elif isinstance(item, ps.GlyphEnd):
            if collecting_type3 and type3_glyph_cache_key is not None:
                # End of cache miss -- build CharProc from cached elements
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
