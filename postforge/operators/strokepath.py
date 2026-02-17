# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript strokepath operator.

Replaces the current path with an outline path that would result from
stroking it with the current graphics state parameters.
"""

import math

from ..core import error as ps_error
from ..core import types as ps
from . import strokepath_algorithm as algo


def _ps_path_to_algo(ps_path):
    """Convert ps.Path → algorithm format (list of list of algo path elements)."""
    result = []
    for subpath in ps_path:
        sp = []
        for elem in subpath:
            if isinstance(elem, ps.MoveTo):
                sp.append(algo.MoveTo(elem.p.x, elem.p.y))
            elif isinstance(elem, ps.LineTo):
                sp.append(algo.LineTo(elem.p.x, elem.p.y))
            elif isinstance(elem, ps.CurveTo):
                sp.append(algo.CurveTo(elem.p1.x, elem.p1.y,
                                        elem.p2.x, elem.p2.y,
                                        elem.p3.x, elem.p3.y))
            elif isinstance(elem, ps.ClosePath):
                sp.append(algo.ClosePath())
        if sp:
            result.append(sp)
    return result


def _algo_path_to_ps(algo_groups):
    """Convert algorithm output (list of groups of subpaths) back to ps.Path."""
    _Point = ps.Point
    _MoveTo = ps.MoveTo
    _LineTo = ps.LineTo
    _CurveTo = ps.CurveTo
    _ClosePath = ps.ClosePath
    _SubPath = ps.SubPath
    _aMoveTo = algo.MoveTo
    _aLineTo = algo.LineTo
    _aCurveTo = algo.CurveTo
    _isinstance = isinstance
    path = ps.Path()
    for group in algo_groups:
        for sp in group:
            subpath = _SubPath()
            _append = subpath.append
            for elem in sp:
                if _isinstance(elem, _aMoveTo):
                    _append(_MoveTo(_Point(elem.x, elem.y)))
                elif _isinstance(elem, _aLineTo):
                    _append(_LineTo(_Point(elem.x, elem.y)))
                elif _isinstance(elem, _aCurveTo):
                    _append(_CurveTo(
                        _Point(elem.x1, elem.y1),
                        _Point(elem.x2, elem.y2),
                        _Point(elem.x3, elem.y3)))
                else:
                    _append(_ClosePath())
            if subpath:
                path.append(subpath)
    return path


def _snap_path_to_pixels(algo_path, half_width):
    """Snap axis-aligned line segments to device pixel grid for crisp outlines.

    Only snaps coordinates for horizontal and vertical line segments.
    Diagonal segments and curves are left untouched to avoid distortion.

    For a horizontal segment, the y-coordinates are snapped so that the
    outline edges at ±half_width land on integer pixel boundaries.
    For a vertical segment, the x-coordinates are snapped similarly.
    """
    for sp in algo_path:
        # Build list of (start_point, element) pairs to detect axis alignment
        i = 0
        while i < len(sp):
            elem = sp[i]
            if isinstance(elem, algo.LineTo) and i > 0:
                # Find the preceding point
                prev = sp[i - 1]
                if isinstance(prev, algo.MoveTo):
                    px, py = prev.x, prev.y
                elif isinstance(prev, algo.LineTo):
                    px, py = prev.x, prev.y
                else:
                    i += 1
                    continue

                dx = abs(elem.x - px)
                dy = abs(elem.y - py)

                if dy < 0.01 and dx > 0.01:
                    # Horizontal line — snap y on both endpoints
                    snapped_y = _snap_coord(py, half_width)
                    _update_point_y(sp, i - 1, snapped_y)
                    sp[i] = algo.LineTo(elem.x, snapped_y)
                elif dx < 0.01 and dy > 0.01:
                    # Vertical line — snap x on both endpoints
                    snapped_x = _snap_coord(px, half_width)
                    _update_point_x(sp, i - 1, snapped_x)
                    sp[i] = algo.LineTo(snapped_x, elem.y)
            i += 1


def _update_point_x(sp, idx, x):
    """Update the x coordinate of element at idx."""
    elem = sp[idx]
    if isinstance(elem, algo.MoveTo):
        sp[idx] = algo.MoveTo(x, elem.y)
    elif isinstance(elem, algo.LineTo):
        sp[idx] = algo.LineTo(x, elem.y)


def _update_point_y(sp, idx, y):
    """Update the y coordinate of element at idx."""
    elem = sp[idx]
    if isinstance(elem, algo.MoveTo):
        sp[idx] = algo.MoveTo(elem.x, y)
    elif isinstance(elem, algo.LineTo):
        sp[idx] = algo.LineTo(elem.x, y)


def _snap_coord(val, half_width):
    """Snap a single coordinate so that val ± half_width lands on integers."""
    return round(val - half_width) + half_width


def _transform_algo_path(algo_path, m_a, m_b, m_c, m_d, tx, ty):
    """Transform algo path points using a 2x2 matrix with translation offset.

    For device→user: pass inverse CTM components; tx/ty are the CTM translation
    used to offset device coords before applying the inverse matrix.
    """
    result = []
    for sp in algo_path:
        new_sp = []
        for elem in sp:
            if isinstance(elem, algo.MoveTo):
                dx, dy = elem.x - tx, elem.y - ty
                new_sp.append(algo.MoveTo(m_a * dx + m_c * dy,
                                          m_b * dx + m_d * dy))
            elif isinstance(elem, algo.LineTo):
                dx, dy = elem.x - tx, elem.y - ty
                new_sp.append(algo.LineTo(m_a * dx + m_c * dy,
                                          m_b * dx + m_d * dy))
            elif isinstance(elem, algo.CurveTo):
                d1x, d1y = elem.x1 - tx, elem.y1 - ty
                d2x, d2y = elem.x2 - tx, elem.y2 - ty
                d3x, d3y = elem.x3 - tx, elem.y3 - ty
                new_sp.append(algo.CurveTo(
                    m_a * d1x + m_c * d1y, m_b * d1x + m_d * d1y,
                    m_a * d2x + m_c * d2y, m_b * d2x + m_d * d2y,
                    m_a * d3x + m_c * d3y, m_b * d3x + m_d * d3y))
            elif isinstance(elem, algo.ClosePath):
                new_sp.append(algo.ClosePath())
        if new_sp:
            result.append(new_sp)
    return result


def _transform_stroke_groups(groups, a, b, c, d, tx, ty):
    """Transform **stroke** outline groups from user space back to device space."""
    result = []
    for group in groups:
        new_group = []
        for sp in group:
            new_sp = []
            for elem in sp:
                if isinstance(elem, algo.MoveTo):
                    new_sp.append(algo.MoveTo(a * elem.x + c * elem.y + tx,
                                              b * elem.x + d * elem.y + ty))
                elif isinstance(elem, algo.LineTo):
                    new_sp.append(algo.LineTo(a * elem.x + c * elem.y + tx,
                                              b * elem.x + d * elem.y + ty))
                elif isinstance(elem, algo.CurveTo):
                    new_sp.append(algo.CurveTo(
                        a * elem.x1 + c * elem.y1 + tx,
                        b * elem.x1 + d * elem.y1 + ty,
                        a * elem.x2 + c * elem.y2 + tx,
                        b * elem.x2 + d * elem.y2 + ty,
                        a * elem.x3 + c * elem.y3 + tx,
                        b * elem.x3 + d * elem.y3 + ty))
                elif isinstance(elem, algo.ClosePath):
                    new_sp.append(algo.ClosePath())
            if new_sp:
                new_group.append(new_sp)
        result.append(new_group)
    return result


def strokepath(ctxt, ostack):
    """
    – **strokepath** –
    Replaces the current path with a path that outlines the area that would
    be painted by **stroke** using the current graphics state parameters.
    **Errors**: **limitcheck**
    """
    gstate = ctxt.gstate

    # Empty path is a no-op
    if not gstate.path:
        return

    # Extract stroke parameters
    line_width = gstate.line_width
    line_cap = gstate.line_cap
    line_join = gstate.line_join
    miter_limit = gstate.miter_limit
    dash_array_ps, dash_offset_ps = gstate.dash_pattern

    # Convert dash array values (they may be PS objects)
    dash_array = [d.val if hasattr(d, 'val') else float(d) for d in dash_array_ps]
    dash_offset = dash_offset_ps.val if hasattr(dash_offset_ps, 'val') else float(dash_offset_ps)

    # Get CTM components
    ctm = gstate.CTM.val
    a, b, c, d = ctm[0].val, ctm[1].val, ctm[2].val, ctm[3].val
    tx, ty = ctm[4].val, ctm[5].val
    det = a * d - b * c

    # Compute singular values to detect anisotropy
    sum_sq = a * a + b * b + c * c + d * d
    diff_term = math.sqrt((a * a + b * b - c * c - d * d) ** 2
                          + 4 * (a * c + b * d) ** 2)
    s_max = math.sqrt(max(0, 0.5 * (sum_sq + diff_term)))
    s_min = math.sqrt(max(0, 0.5 * (sum_sq - diff_term)))

    is_anisotropic = (s_min > 1e-10 and s_max / s_min > 1.01
                      and abs(det) > 1e-10)

    # Convert PS path to algorithm format
    algo_path = _ps_path_to_algo(gstate.path)

    if is_anisotropic:
        # Anisotropic path: transform to user space, stroke there,
        # transform back to device space.
        inv_a = d / det
        inv_b = -b / det
        inv_c = -c / det
        inv_d = a / det

        # Transform device-space path to user space
        user_path = _transform_algo_path(algo_path, inv_a, inv_b, inv_c, inv_d, tx, ty)

        # Use user-space line_width with minimum of 1 device pixel
        min_user_lw = 1.0 / s_max if s_max > 0 else 1.0
        user_lw = max(line_width, min_user_lw)
        if user_lw < 1e-12:
            user_lw = min_user_lw

        # Stroke in user space
        groups = algo.strokepath_grouped(
            user_path,
            user_lw,
            line_cap,
            line_join,
            miter_limit,
            dash_array if dash_array else None,
            dash_offset,
            tolerance=min(user_lw * 0.05, 0.1))

        # Transform result back to device space
        groups = _transform_stroke_groups(groups, a, b, c, d, tx, ty)
    else:
        # Isotropic path: stroke in device space with pixel snapping
        scale = math.sqrt(abs(det))
        if scale < 1e-12:
            scale = 1.0

        # Scale stroke parameters to device space
        # Per PLRM: line width of 0 means "thinnest line the device can render"
        # (1 device pixel).
        device_line_width = line_width * scale
        if device_line_width < 1e-12:
            device_line_width = 1.0

        # Ensure minimum 1 device pixel width (stroke adjust behaviour)
        if device_line_width < 1.0:
            device_line_width = 1.0

        device_dash_array = [v * scale for v in dash_array]
        device_dash_offset = dash_offset * scale

        # Pixel-snap path coordinates so outline edges land on integer boundaries.
        half_width = device_line_width / 2.0
        _snap_path_to_pixels(algo_path, half_width)

        # Compute stroke outline
        groups = algo.strokepath_grouped(
            algo_path,
            device_line_width,
            line_cap,
            line_join,
            miter_limit,
            device_dash_array if device_dash_array else None,
            device_dash_offset,
            tolerance=min(device_line_width * 0.05, 0.1))

    # Convert result back to PS path
    new_path = _algo_path_to_ps(groups)

    # Replace current path
    gstate.path = new_path

    # Update currentpoint to last point of new path
    if new_path:
        last_subpath = new_path[-1]
        for elem in reversed(last_subpath):
            if isinstance(elem, ps.MoveTo):
                gstate.currentpoint = elem.p
                break
            elif isinstance(elem, ps.LineTo):
                gstate.currentpoint = elem.p
                break
            elif isinstance(elem, ps.CurveTo):
                gstate.currentpoint = elem.p3
                break
    else:
        gstate.currentpoint = None
