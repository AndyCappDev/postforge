# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Path, fill, stroke, and color operators for PDF content streams.

Converts PostScript display list Path/Fill/Stroke elements to PDF
content stream operators, including isotropic and anisotropic stroke
handling, rectangle detection, and curve shorthand (v/y operators).
"""

import math

from ...core import types as ps
from ._common import _fmt, _cfmt, _GState


def _close_aniso_batch(lines: list[bytes], gs: _GState) -> None:
    """Close an open anisotropic stroke batch (q/cm block)."""
    if gs.aniso_ctm is not None:
        lines.append(b'Q')
        gs.aniso_ctm = None
        gs.invalidate()


def _emit_path(path: ps.Path, close_subpaths: bool = False) -> list[bytes]:
    """Convert a Path to PDF path operators.

    Returns list of operator lines (not yet joined -- they get prepended
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
        # axis-aligned edges -> emit as a single ``re`` operator.
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
            # (x,y)->(x+w,y)->(x+w,y+h)->(x,y+h)->close.
            # Start from (x0, y1) so the ``re`` traversal visits
            # the same vertices in the same rotational order as the
            # original vertical-first path, preserving winding direction.
            if (x0 == x1 and x2 == x3 and y1 == y2 and y0 == y3):
                w = x2 - x0
                h = y0 - y1
                ops.append(f'{_cfmt(x0)} {_cfmt(y1)} {_cfmt(w)} {_cfmt(h)} re'.encode())
                continue

        # General path -- emit element by element
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
                    # First control point = current point -> v operator
                    ops.append(
                        f'{_cfmt(x2)} {_cfmt(y2)} '
                        f'{_cfmt(x3)} {_cfmt(y3)} v'.encode())
                elif _cfmt(x2) == _cfmt(x3) and _cfmt(y2) == _cfmt(y3):
                    # Second control point = endpoint -> y operator
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


def _emit_path_offset(path: ps.Path, ox: float, oy: float) -> list[bytes]:
    """Convert a Path to PDF path operators with a position offset.

    Adds (ox, oy) to all coordinates -- used for replaying cached glyph paths
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
        # Degenerate CTM -- fall back to isotropic
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
