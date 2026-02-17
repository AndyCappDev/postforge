# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import math

from ..core import error as ps_error
from ..core import types as ps
from .matrix import _transform_point, itransform
from .path import _setcurrentpoint


def currentpoint(ctxt, ostack):
    """
    - **currentpoint** x y

    returns the x and y coordinates, in the user coordinate system, of the current
    point in the graphics state (the trailing endpoint of the current path).

    **Errors**: **nocurrentpoint**, **stackoverflow**, **undefinedresult**
    **See Also**: **moveto**, **lineto**, **curveto**, **arc**
    """

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, currentpoint.__name__)

    if ctxt.MaxOpStack and len(ostack) + 2 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentpoint.__name__)

    ostack.append(ps.Real(ctxt.gstate.currentpoint.x))
    ostack.append(ps.Real(ctxt.gstate.currentpoint.y))
    itransform(ctxt, ostack)


def pathbbox(ctxt, ostack):
    """
    - **pathbbox** llx lly urx ury

    returns the bounding box of the current path, the smallest rectangle enclosing all
    elements of the path. The results are four real numbers describing a rectangle in
    user space, oriented with its sides parallel to the axes of the user coordinate
    system.

    **Errors**: **nocurrentpoint**, **stackoverflow**
    **See Also**: **setbbox**, **flattenpath**
    """

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, pathbbox.__name__)

    if ctxt.MaxOpStack and len(ostack) + 4 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, pathbbox.__name__)

    # PLRM: If setbbox has been called, return the stored bbox directly
    if ctxt.gstate.bbox is not None:
        llx, lly, urx, ury = ctxt.gstate.bbox
        ostack.append(ps.Real(float(llx)))
        ostack.append(ps.Real(float(lly)))
        ostack.append(ps.Real(float(urx)))
        ostack.append(ps.Real(float(ury)))
        return

    # PLRM Level 2/3: Subpaths consisting only of a moveto should be excluded
    # from bbox calculation, as they don't represent actual drawn content.
    # Only exception: if the entire path is just one moveto, include it.

    # Identify subpaths that should be excluded (moveto-only subpaths)
    # A subpath with only a MoveTo doesn't draw anything
    excluded_subpaths = set()
    drawable_subpath_count = 0

    for i, subpath in enumerate(ctxt.gstate.path):
        if len(subpath) == 1 and isinstance(subpath[0], ps.MoveTo):
            excluded_subpaths.add(i)
        else:
            drawable_subpath_count += 1

    # If ALL subpaths would be excluded, include the first one
    # (path consisting only of moveto operations)
    if drawable_subpath_count == 0 and ctxt.gstate.path:
        excluded_subpaths.discard(0)

    # PLRM Algorithm: First compute bounding box in DEVICE space
    # Find the first non-excluded point for initialization
    dev_min_x = dev_min_y = dev_max_x = dev_max_y = None

    for i, subpath in enumerate(ctxt.gstate.path):
        if i in excluded_subpaths:
            continue
        for pc_item in subpath:
            if isinstance(pc_item, (ps.MoveTo, ps.LineTo)):
                if dev_min_x is None:
                    dev_min_x = dev_max_x = pc_item.p.x
                    dev_min_y = dev_max_y = pc_item.p.y
                else:
                    dev_min_x = min(dev_min_x, pc_item.p.x)
                    dev_min_y = min(dev_min_y, pc_item.p.y)
                    dev_max_x = max(dev_max_x, pc_item.p.x)
                    dev_max_y = max(dev_max_y, pc_item.p.y)
            elif isinstance(pc_item, ps.CurveTo):
                # Include all control points for curves
                if dev_min_x is None:
                    dev_min_x = min(pc_item.p1.x, pc_item.p2.x, pc_item.p3.x)
                    dev_min_y = min(pc_item.p1.y, pc_item.p2.y, pc_item.p3.y)
                    dev_max_x = max(pc_item.p1.x, pc_item.p2.x, pc_item.p3.x)
                    dev_max_y = max(pc_item.p1.y, pc_item.p2.y, pc_item.p3.y)
                else:
                    dev_min_x = min(dev_min_x, pc_item.p1.x, pc_item.p2.x, pc_item.p3.x)
                    dev_min_y = min(dev_min_y, pc_item.p1.y, pc_item.p2.y, pc_item.p3.y)
                    dev_max_x = max(dev_max_x, pc_item.p1.x, pc_item.p2.x, pc_item.p3.x)
                    dev_max_y = max(dev_max_y, pc_item.p1.y, pc_item.p2.y, pc_item.p3.y)

    # Fallback if no drawable content found (shouldn't happen if currentpoint exists)
    if dev_min_x is None:
        first_point = ctxt.gstate.path[0][0]
        dev_min_x = dev_max_x = first_point.p.x
        dev_min_y = dev_max_y = first_point.p.y

    # PLRM: Transform the 4 corners of the device bbox to user space
    # and find the axis-aligned bbox that encloses all 4 corners
    corners = [
        (dev_min_x, dev_min_y),  # lower-left
        (dev_max_x, dev_min_y),  # lower-right
        (dev_max_x, dev_max_y),  # upper-right
        (dev_min_x, dev_max_y),  # upper-left
    ]

    # Transform first corner to initialize user-space bbox
    ux, uy = _transform_point(ctxt.gstate.iCTM, corners[0][0], corners[0][1])
    min_x, min_y, max_x, max_y = ux, uy, ux, uy

    # Transform remaining corners and update user-space bbox
    for dx, dy in corners[1:]:
        ux, uy = _transform_point(ctxt.gstate.iCTM, dx, dy)
        min_x = min(min_x, ux)
        min_y = min(min_y, uy)
        max_x = max(max_x, ux)
        max_y = max(max_y, uy)

    # Round to 6 decimal places to eliminate tiny CTM/iCTM round-trip
    # artifacts (e.g. 1e-10 instead of 0.0).  Six decimal places gives
    # sub-point precision (~0.014 nm) which is far below any physical
    # relevance while cleaning up float noise.
    ostack.append(ps.Real(round(float(min_x), 6)))
    ostack.append(ps.Real(round(float(min_y), 6)))

    ostack.append(ps.Real(round(float(max_x), 6)))
    ostack.append(ps.Real(round(float(max_y), 6)))


def pathforall(ctxt, ostack):
    """
    move line curve close **pathforall** -

    enumerates the elements of the current path in order, executing one of the four
    procedures move, line, curve, or close for each element, depending on its nature.

    **Errors**: **invalidaccess**, **rangecheck**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**: **moveto**, **lineto**, **curveto**, **closepath**, **charpath**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 4:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, pathforall.__name__)

    # 2. TYPECHECK - Check operand types (move line curve close procedures)
    for n in range(-4, 0):
        if ostack[n].TYPE not in ps.ARRAY_TYPES or ostack[n].attrib != ps.ATTRIB_EXEC:
            return ps_error.e(ctxt, ps_error.TYPECHECK, pathforall.__name__)

    if not ctxt.gstate.path:
        ctxt.o_stack.pop()
        ctxt.o_stack.pop()
        ctxt.o_stack.pop()
        ctxt.o_stack.pop()
        return

    pathforall_loop = ps.Loop(ps.LT_PATHFORALL)
    pathforall_loop.control = 0

    pathforall_loop.moveto_proc = ctxt.o_stack[-4]
    pathforall_loop.lineto_proc = ctxt.o_stack[-3]
    pathforall_loop.curveto_proc = ctxt.o_stack[-2]
    pathforall_loop.closepath_proc = ctxt.o_stack[-1]
    # save a copy of the current path
    pathforall_loop.path = copy.deepcopy(ctxt.gstate.path)

    # push the for loop onto the execution stack
    ctxt.e_stack.append(pathforall_loop)

    ctxt.o_stack.pop()
    ctxt.o_stack.pop()
    ctxt.o_stack.pop()
    ctxt.o_stack.pop()


def reversepath(ctxt, ostack):
    """
    - **reversepath** -

    replaces the current path with an equivalent path whose segments are
    in **reverse** order. If a subpath of the original path ends with a
    **closepath**, the corresponding reversed subpath will also end with a
    **closepath**, but it will be traversed in the opposite direction.

    **Errors**: **limitcheck**
    **See Also**: **pathforall**
    """
    old_path = ctxt.gstate.path
    new_path = ps.Path()

    for subpath in old_path:
        if not subpath:
            continue

        new_subpath = ps.SubPath()
        has_close = isinstance(subpath[-1], ps.ClosePath)

        # Collect segments (excluding MoveTo and ClosePath)
        segments = []
        for item in subpath:
            if not isinstance(item, (ps.MoveTo, ps.ClosePath)):
                segments.append(item)

        # Build list of points: points[0] = moveto, points[i+1] = endpoint of segment i
        moveto_pt = subpath[0].p
        points = [moveto_pt]
        for seg in segments:
            if isinstance(seg, ps.CurveTo):
                points.append(seg.p3)
            else:
                points.append(seg.p)

        # Start reversed subpath from last point
        new_subpath.append(ps.MoveTo(ps.Point(points[-1].x, points[-1].y)))

        # Reverse each segment
        for i in range(len(segments) - 1, -1, -1):
            seg = segments[i]
            target = points[i]
            if isinstance(seg, ps.CurveTo):
                # Swap control points: cp2 becomes cp1, cp1 becomes cp2
                new_subpath.append(ps.CurveTo(
                    ps.Point(seg.p2.x, seg.p2.y),
                    ps.Point(seg.p1.x, seg.p1.y),
                    ps.Point(target.x, target.y)
                ))
            else:
                new_subpath.append(ps.LineTo(ps.Point(target.x, target.y)))

        if has_close:
            new_subpath.append(ps.ClosePath())

        new_path.append(new_subpath)

    ctxt.gstate.path = new_path

    # Update currentpoint to last point of last reversed subpath
    if new_path:
        last_subpath = new_path[-1]
        has_close = isinstance(last_subpath[-1], ps.ClosePath)
        if has_close:
            # After closepath, currentpoint is the moveto point
            _setcurrentpoint(ctxt, last_subpath[0].p.x, last_subpath[0].p.y)
        else:
            # Find last point-bearing element
            for item in reversed(last_subpath):
                if isinstance(item, ps.CurveTo):
                    _setcurrentpoint(ctxt, item.p3.x, item.p3.y)
                    break
                elif isinstance(item, (ps.MoveTo, ps.LineTo)):
                    _setcurrentpoint(ctxt, item.p.x, item.p.y)
                    break
    elif ctxt.gstate.currentpoint is not None:
        ctxt.gstate.currentpoint = None


def flattenpath(ctxt, ostack):
    """
    - **flattenpath** -

    replaces all curve segments in the current path with sequences of straight
    line segments that approximate the curves with the accuracy given by the
    current flatness parameter.

    **Errors**: **nocurrentpoint**
    **See Also**: **setflat**, **currentflat**, **pathbbox**
    """

    old_path = ctxt.gstate.path
    new_path = ctxt.gstate.path = ps.Path()

    flatness = ctxt.gstate.flatness

    for subpath in old_path:
        new_subpath = ps.SubPath()
        # Not necessary but flake8 will complain if we do not initialize this
        current_pt = None
        for item in subpath:
            if isinstance(item, ps.CurveTo):
                segment = _flatten_cubic_bezier_curve(
                    current_pt, item.p1, item.p2, item.p3, flatness
                )
                segs = 0
                for point in segment:
                    new_subpath.append(ps.LineTo(point))
                    segs += 1
                current_pt = point
            else:
                if isinstance(item, (ps.MoveTo, ps.LineTo)):
                    current_pt = item.p
                new_subpath.append(item)
        new_path.append(new_subpath)


def _flatten_cubic_bezier_curve(p0, p1, p2, p3, flatness):
    """
    Flatten a cubic Bezier curve into line segments.

    Uses the correct flatness test: measures the perpendicular distance from
    control points (p1, p2) to the chord (line from p0 to p3). If the maximum
    deviation is less than flatness, the curve is approximated by a single line.
    Otherwise, the curve is subdivided using de Casteljau's algorithm.

    Args:
        p0, p1, p2, p3: Control points of the cubic Bezier curve
        flatness: Maximum allowed deviation from the true curve (in device units)

    Returns:
        List of points representing line segment endpoints (excluding p0)
    """
    segments = []
    stack = [(p0, p1, p2, p3)]

    while stack:
        p0, p1, p2, p3 = stack.pop()

        # Calculate chord vector (from p0 to p3)
        dx = p3.x - p0.x
        dy = p3.y - p0.y
        chord_len_sq = dx * dx + dy * dy

        if chord_len_sq < 1e-10:
            # Degenerate case: endpoints essentially coincide
            segments.append(p3)
            continue

        chord_len = math.sqrt(chord_len_sq)

        # Calculate perpendicular distance from p1 to chord using cross product:
        # distance = |cross(p1 - p0, chord)| / |chord|
        d1 = abs((p1.x - p0.x) * dy - (p1.y - p0.y) * dx) / chord_len

        # Calculate perpendicular distance from p2 to chord
        d2 = abs((p2.x - p0.x) * dy - (p2.y - p0.y) * dx) / chord_len

        # Check if curve is flat enough (max deviation < flatness)
        if max(d1, d2) <= flatness:
            segments.append(p3)
        else:
            # Subdivide curve at midpoint using de Casteljau's algorithm
            p01 = ps.Point((p0.x + p1.x) / 2, (p0.y + p1.y) / 2)
            p12 = ps.Point((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)
            p23 = ps.Point((p2.x + p3.x) / 2, (p2.y + p3.y) / 2)
            p012 = ps.Point((p01.x + p12.x) / 2, (p01.y + p12.y) / 2)
            p123 = ps.Point((p12.x + p23.x) / 2, (p12.y + p23.y) / 2)
            p0123 = ps.Point((p012.x + p123.x) / 2, (p012.y + p123.y) / 2)

            # Push second half first so first half is processed next
            stack.append((p0123, p123, p23, p3))
            stack.append((p0, p01, p012, p0123))

    return segments


def setbbox(ctxt, ostack):
    """
    llx lly urx ury **setbbox** -

    Sets a bounding box for the current path, establishing the bounding box
    that **pathbbox** will return. The operands are four numbers representing
    the lower-left (llx, lly) and upper-right (urx, ury) corners of a
    rectangle in user space.

    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**: **pathbbox**, **ucache**, **upath**
    """
    if len(ostack) < 4:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setbbox.__name__)

    for i in range(-4, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setbbox.__name__)

    llx = ostack[-4].val
    lly = ostack[-3].val
    urx = ostack[-2].val
    ury = ostack[-1].val

    if urx < llx or ury < lly:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setbbox.__name__)

    ostack.pop()
    ostack.pop()
    ostack.pop()
    ostack.pop()

    ctxt.gstate.bbox = (llx, lly, urx, ury)
