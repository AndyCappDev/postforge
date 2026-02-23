# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Type 3 font operations for PDF output.

Handles Type 3 glyph emission in content streams (CharProc building,
text runs, glyph reference fallback) and PDF Type 3 font object
construction.
"""

import copy
import zlib

from ...core import types as ps
from ...core.types.context import global_resources
from ...core.unicode_mapping import glyph_name_to_unicode
from .font_embedder import generate_tounicode_cmap
from ._common import _fmt, _cfmt, _Type3GlyphDef, _Type3FontDef, _GState
from .stroke_ops import _emit_path, _emit_path_offset, _emit_color
from .image_ops import _compute_image_cm, _emit_image_xobject
from .text_ops import _emit_text_color
from .pdf_objects import (
    PdfArray, PdfDict, PdfName, PdfNumber, PdfStream,
)


def _type3_font_key(cache_key: object) -> tuple:
    """Extract the Type 3 font grouping key from a GlyphCacheKey.

    Groups glyphs by font identity, CTM scale, and font matrix --
    color and subpixel_y are excluded because d1 glyphs inherit
    color from the text state.
    """
    return (cache_key.font_id, cache_key.ctm_scale, cache_key.font_matrix)


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
    FontMatrix x CTM_scale to match the device-space coordinates used by
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

    # Transform width vector (delta -- no translation)
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

    last_path_lines: list[bytes] = []
    for element in display_elements:
        if isinstance(element, ps.Path):
            path_lines = _emit_path(element)
            # Path will be consumed by the next Fill element
            last_path_lines = path_lines

        elif isinstance(element, ps.Fill):
            # For d1, don't emit color -- glyph inherits text color
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

    # Single BT/Tf block for the entire batch -- all entries share the
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


# ---------------------------------------------------------------------------
# PDF Type 3 font object construction (extracted from PDFBuilder)
# ---------------------------------------------------------------------------


def build_type3_font(writer: object, t3_def: _Type3FontDef) -> object | None:
    """Build a PDF Type 3 font object from collected glyph data.

    CharProc streams use inline images (BI/ID/EI), so no external
    XObject resources are needed.

    Args:
        writer: PdfWriter to add objects to.
        t3_def: _Type3FontDef with glyph definitions.

    Returns:
        Indirect reference to the Type 3 font object, or None.
    """
    if not t3_def.glyphs:
        return None

    # Determine char code range
    char_codes = sorted(t3_def.glyphs.keys())
    first_char = char_codes[0]
    last_char = char_codes[-1]

    # Compute union bounding box across all glyphs
    bbox_union = [0.0, 0.0, 1.0, 1.0]
    has_bbox = False
    for glyph_def in t3_def.glyphs.values():
        if glyph_def.bbox:
            llx, lly, urx, ury = glyph_def.bbox
            if not has_bbox:
                bbox_union = [llx, lly, urx, ury]
                has_bbox = True
            else:
                bbox_union[0] = min(bbox_union[0], llx)
                bbox_union[1] = min(bbox_union[1], lly)
                bbox_union[2] = max(bbox_union[2], urx)
                bbox_union[3] = max(bbox_union[3], ury)

    # Build CharProcs dictionary
    charprocs_dict = PdfDict()
    for glyph_def in t3_def.glyphs.values():
        compressed = zlib.compress(glyph_def.charproc_stream)
        cp_stream = PdfStream(compressed)
        cp_stream['/Filter'] = PdfName('/FlateDecode')
        cp_ref = writer.add_object(cp_stream)
        charprocs_dict['/' + glyph_def.glyph_name] = cp_ref

    # Build Encoding with Differences array
    differences = PdfArray()
    last_code = -2
    for cc in char_codes:
        glyph_def = t3_def.glyphs[cc]
        if cc != last_code + 1:
            differences.append(PdfNumber(cc))
        differences.append(PdfName('/' + glyph_def.glyph_name))
        last_code = cc

    encoding_dict = PdfDict()
    encoding_dict['/Type'] = PdfName('/Encoding')
    encoding_dict['/Differences'] = differences

    # Build Widths array
    widths_array = PdfArray()
    for cc in range(first_char, last_char + 1):
        glyph_def = t3_def.glyphs.get(cc)
        if glyph_def is not None:
            widths_array.append(PdfNumber(round(glyph_def.width_x, 2)))
        else:
            widths_array.append(PdfNumber(0))

    # Build font dictionary
    font_obj = PdfDict()
    font_obj['/Type'] = PdfName('/Font')
    font_obj['/Subtype'] = PdfName('/Type3')
    font_obj['/FontBBox'] = PdfArray([
        PdfNumber(round(bbox_union[0], 2)),
        PdfNumber(round(bbox_union[1], 2)),
        PdfNumber(round(bbox_union[2], 2)),
        PdfNumber(round(bbox_union[3], 2)),
    ])
    font_obj['/FontMatrix'] = PdfArray([
        PdfNumber(1), PdfNumber(0),
        PdfNumber(0), PdfNumber(1),
        PdfNumber(0), PdfNumber(0),
    ])
    font_obj['/CharProcs'] = writer.add_object(charprocs_dict)
    font_obj['/Encoding'] = encoding_dict
    font_obj['/FirstChar'] = PdfNumber(first_char)
    font_obj['/LastChar'] = PdfNumber(last_char)
    font_obj['/Widths'] = widths_array

    # Build ToUnicode CMap for text selection/searchability.
    # Prefer ActualText-derived unicode_map (accurate for re-encoded
    # fonts with non-standard glyph names), fall back to Adobe Glyph
    # List lookup from glyph names.
    tounicode_map: dict[int, str] = {}
    for cc, glyph_def in t3_def.glyphs.items():
        if cc in t3_def.unicode_map:
            tounicode_map[cc] = t3_def.unicode_map[cc]
        else:
            gname_bytes = glyph_def.glyph_name.encode('latin-1')
            unicode_char = glyph_name_to_unicode(gname_bytes)
            if unicode_char and unicode_char != '\ufffd':
                tounicode_map[cc] = unicode_char
    if tounicode_map:
        cmap_data = generate_tounicode_cmap(tounicode_map, 'Type3')
        cmap_compressed = zlib.compress(cmap_data)
        cmap_stream = PdfStream(cmap_compressed)
        cmap_stream['/Filter'] = PdfName('/FlateDecode')
        font_obj['/ToUnicode'] = writer.add_object(cmap_stream)

    return writer.add_object(font_obj)
