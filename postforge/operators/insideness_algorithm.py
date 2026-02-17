# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Point-in-path insideness testing algorithm.

Implements ray-casting for point-in-fill tests (winding number and even-odd
rules).  Operates on device-space ps.Path objects, flattening curves to line
segments before testing.
"""

from ..core import types as ps
from .path_query import _flatten_cubic_bezier_curve


def _flatten_subpath(subpath, flatness):
    """Flatten a subpath's curves into (x0, y0, x1, y1) line segments.

    Returns (segments, moveto_x, moveto_y, last_x, last_y, has_close).
    """
    segments = []
    mx = my = 0.0  # moveto point
    cx = cy = 0.0  # current point

    has_close = False

    for elem in subpath:
        if isinstance(elem, ps.MoveTo):
            mx = cx = elem.p.x
            my = cy = elem.p.y
        elif isinstance(elem, ps.LineTo):
            segments.append((cx, cy, elem.p.x, elem.p.y))
            cx = elem.p.x
            cy = elem.p.y
        elif isinstance(elem, ps.CurveTo):
            pts = _flatten_cubic_bezier_curve(
                ps.Point(cx, cy), elem.p1, elem.p2, elem.p3, flatness
            )
            for pt in pts:
                segments.append((cx, cy, pt.x, pt.y))
                cx = pt.x
                cy = pt.y
        elif isinstance(elem, ps.ClosePath):
            has_close = True

    return segments, mx, my, cx, cy, has_close


def point_in_path(path, px, py, flatness, use_winding):
    """Ray-casting point-in-path test on a device-space ps.Path.

    Casts a horizontal ray from (px, py) towards +x and counts crossings
    with path segments.

    Args:
        path: ps.Path in device-space coordinates.
        px, py: Test point in device space.
        flatness: Curve flattening tolerance.
        use_winding: True for nonzero winding rule, False for even-odd.

    Returns:
        True if the point is inside the path.
    """
    winding = 0
    crossings = 0

    for subpath in path:
        segs, mx, my, last_x, last_y, has_close = _flatten_subpath(
            subpath, flatness
        )

        # Implicit close: add closing segment back to moveto
        if has_close and (last_x != mx or last_y != my):
            segs.append((last_x, last_y, mx, my))

        for x0, y0, x1, y1 in segs:
            # Standard ray-casting crossing test: count a crossing when
            # exactly one endpoint is strictly below py.  This avoids
            # double-counting shared vertices and skips horizontal segments.
            if (y0 < py) == (y1 < py):
                continue

            # Compute x-intercept of segment with horizontal line y = py
            t = (py - y0) / (y1 - y0)
            x_intercept = x0 + t * (x1 - x0)

            if x_intercept > px:
                crossings += 1
                if use_winding:
                    if y1 > y0:
                        winding += 1  # upward crossing
                    else:
                        winding -= 1  # downward crossing

    if use_winding:
        return winding != 0
    else:
        return (crossings % 2) == 1
