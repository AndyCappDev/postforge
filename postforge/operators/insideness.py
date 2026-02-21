# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PostScript insideness testing operators: infill, ineofill, instroke.

These operators test whether a given point lies inside the area that would
be painted by fill, eofill, or stroke, without actually painting anything.
Per PLRM, they ignore the clipping path and do not disturb the current path.
"""

import math

from ..core import error as ps_error
from ..core import types as ps
from ..core.types.constants import NUMERIC_TYPES
from . import insideness_algorithm as algo
from . import strokepath as sp
from . import strokepath_algorithm as sp_algo
from .matrix import _transform_point


def infill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    x y **infill** bool
    Tests whether the point (x, y) in user space would be inside the area
    painted by **fill** (nonzero winding number rule) applied to the current path.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, infill.__name__)

    if ostack[-1].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, infill.__name__)
    if ostack[-2].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, infill.__name__)

    y = ostack.pop().val
    x = ostack.pop().val

    path = ctxt.gstate.path
    if not path:
        ostack.append(ps.Bool(False))
        return

    # Transform user-space point to device space
    dx, dy = _transform_point(ctxt.gstate.CTM, x, y)

    result = algo.point_in_path(path, dx, dy, ctxt.gstate.flatness, True)
    ostack.append(ps.Bool(result))


def ineofill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    x y **ineofill** bool
    Tests whether the point (x, y) in user space would be inside the area
    painted by **eofill** (even-odd rule) applied to the current path.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ineofill.__name__)

    if ostack[-1].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ineofill.__name__)
    if ostack[-2].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ineofill.__name__)

    y = ostack.pop().val
    x = ostack.pop().val

    path = ctxt.gstate.path
    if not path:
        ostack.append(ps.Bool(False))
        return

    # Transform user-space point to device space
    dx, dy = _transform_point(ctxt.gstate.CTM, x, y)

    result = algo.point_in_path(path, dx, dy, ctxt.gstate.flatness, False)
    ostack.append(ps.Bool(result))


def instroke(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    x y **instroke** bool
    Tests whether the point (x, y) in user space would be inside the area
    painted by **stroke** applied to the current path with the current graphics
    state **stroke** parameters.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, instroke.__name__)

    if ostack[-1].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, instroke.__name__)
    if ostack[-2].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, instroke.__name__)

    y = ostack.pop().val
    x = ostack.pop().val

    gstate = ctxt.gstate
    path = gstate.path
    if not path:
        ostack.append(ps.Bool(False))
        return

    # Transform user-space point to device space
    dx, dy = _transform_point(gstate.CTM, x, y)

    # Build stroke outline using the same logic as strokepath operator,
    # but without modifying the current path in the graphics state.
    line_width = gstate.line_width
    line_cap = gstate.line_cap
    line_join = gstate.line_join
    miter_limit = gstate.miter_limit
    dash_array_ps, dash_offset_ps = gstate.dash_pattern

    dash_array = [d.val if hasattr(d, 'val') else float(d) for d in dash_array_ps]
    dash_offset = dash_offset_ps.val if hasattr(dash_offset_ps, 'val') else float(dash_offset_ps)

    # Get CTM components
    ctm = gstate.CTM.val
    a, b, c, d = ctm[0].val, ctm[1].val, ctm[2].val, ctm[3].val
    tx, ty = ctm[4].val, ctm[5].val
    det = a * d - b * c

    # Compute singular values for anisotropy detection
    sum_sq = a * a + b * b + c * c + d * d
    diff_term = math.sqrt((a * a + b * b - c * c - d * d) ** 2
                          + 4 * (a * c + b * d) ** 2)
    s_max = math.sqrt(max(0, 0.5 * (sum_sq + diff_term)))
    s_min = math.sqrt(max(0, 0.5 * (sum_sq - diff_term)))

    is_anisotropic = (s_min > 1e-10 and s_max / s_min > 1.01
                      and abs(det) > 1e-10)

    # Convert PS path to algorithm format (does NOT modify gstate.path)
    algo_path = sp._ps_path_to_algo(path)

    if is_anisotropic:
        inv_a = d / det
        inv_b = -b / det
        inv_c = -c / det
        inv_d = a / det

        user_path = sp._transform_algo_path(
            algo_path, inv_a, inv_b, inv_c, inv_d, tx, ty
        )

        min_user_lw = 1.0 / s_max if s_max > 0 else 1.0
        user_lw = max(line_width, min_user_lw)
        if user_lw < 1e-12:
            user_lw = min_user_lw

        groups = sp_algo.strokepath_grouped(
            user_path, user_lw, line_cap, line_join, miter_limit,
            dash_array if dash_array else None, dash_offset,
            tolerance=min(user_lw * 0.05, 0.1)
        )
        groups = sp._transform_stroke_groups(groups, a, b, c, d, tx, ty)
    else:
        scale = math.sqrt(abs(det))
        if scale < 1e-12:
            scale = 1.0

        device_line_width = line_width * scale
        if device_line_width < 1e-12:
            device_line_width = 1.0
        if device_line_width < 1.0:
            device_line_width = 1.0

        device_dash_array = [v * scale for v in dash_array]
        device_dash_offset = dash_offset * scale

        groups = sp_algo.strokepath_grouped(
            algo_path, device_line_width, line_cap, line_join, miter_limit,
            device_dash_array if device_dash_array else None,
            device_dash_offset,
            tolerance=min(device_line_width * 0.05, 0.1)
        )

    # Convert stroke outline to ps.Path and test point-in-fill (winding rule)
    outline = sp._algo_path_to_ps(groups)
    result = algo.point_in_path(outline, dx, dy, gstate.flatness, True)
    ostack.append(ps.Bool(result))
