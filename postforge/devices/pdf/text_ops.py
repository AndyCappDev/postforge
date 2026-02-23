# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Text batching and invisible text overlay for PDF content streams.

Handles BT/ET text blocks, TJ array construction with kern values,
and Courier-based invisible text overlays for searchable PDF output.
"""

import math
import unicodedata

from ...core import types as ps
from .font_tracker import FontTracker
from ._common import _fmt, _cfmt, _GState


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
                     gs: _GState,
                     font_widths_cache: dict[tuple, dict[int, int]] | None = None) -> None:
    """Emit a batch of same-font TextObjs as a single BT/ET block.

    All entries in the batch share the same font, so one Tf suffices.
    Each logical text line (run of same-baseline entries) gets its own Tm.
    Within a run, consecutive characters are merged into TJ arrays with
    kern values.  Color operators are emitted inside BT/ET (valid per PDF
    spec) and only when the color changes.

    Args:
        lines: Output line buffer.
        batch: List of same-font TextObj items.
        font_tracker: Font tracker for key lookups.
        embedded_fonts: Dict mapping font_key -> (resource_name, font_ref).
        gs: Graphics state tracker.
        font_widths_cache: Dict mapping font_key -> {char_code: width_in_1000ths}.
    """
    if not batch:
        return

    pdf_name = _resolve_text_font(batch[0], font_tracker, embedded_fonts)
    if pdf_name is None:
        return

    # Get glyph widths for TJ kern computation
    font_key = font_tracker.get_font_key_for_dict(batch[0].font_dict)
    glyph_widths: dict[int, int] | None = None
    if font_key and font_widths_cache is not None:
        font_info = embedded_fonts.get(font_key)
        if font_info:
            glyph_widths = font_widths_cache.get(font_key)

    # Group into runs of same baseline (same tm orientation, same
    # perpendicular position).  For horizontal text the baseline is
    # constant-y; for 90 deg rotated text the baseline is constant-x.
    # The perpendicular direction to the advance vector (tm_a, tm_b)
    # is (-tm_b, tm_a), so the perpendicular component of a position
    # difference (dx, dy) is (-tm_b*dx + tm_a*dy) / |advance|.

    # Single BT/Tf block for the entire batch -- Tf is the same for all
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

        # Advance vector length squared -- used for baseline checks
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
                # Also check along-direction gap -- if the next entry
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
                # Degenerate text matrix -- only group at same position
                break
            run.append((next_obj, next_obj.start_x, next_obj.start_y))
            j += 1

        # Emit Tm + text for this baseline group
        lines.append(
            f'{tm_key[0]} {tm_key[1]} {tm_key[2]} {tm_key[3]} '
            f'{_cfmt(x)} {_cfmt(y)} Tm'.encode())

        if len(run) == 1:
            # Single entry -- simple Tj
            text_hex = text_obj.text.hex().upper()
            lines.append(f'<{text_hex}> Tj'.encode())
        else:
            # Multiple entries -- build TJ array with kern values
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
                        # Can't compute kern -- emit position break
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

    # Text matrix components (device space -- cm handles PDF conversion)
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
