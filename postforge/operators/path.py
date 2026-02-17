# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import math
from typing import Union

from ..core import error as ps_error
from ..core import types as ps
from .matrix import _transform_delta, _transform_point

def _setcurrentpoint(ctxt, x: Union[int, float], y: Union[int, float]):
    # the currentpoint is always cast to float
    if ctxt.gstate.currentpoint is not None:
        ctxt.gstate.currentpoint.x = float(x)
        ctxt.gstate.currentpoint.y = float(y)
    else:
        # there is no currentpoint, create one
        ctxt.gstate.currentpoint = ps.Point(float(x), float(y))


def _acuteArcToBezier(
    start: Union[int, float], size: Union[int, float]
):
    # Evaluate constants.
    alpha = size / 2.0

    cos_alpha = math.cos(alpha)
    sin_alpha = math.sin(alpha)

    cot_alpha = 1.0 / math.tan(alpha)
    phi = start + alpha  # This is how far the arc needs to be rotated.

    cos_phi = math.cos(phi)
    sin_phi = math.sin(phi)

    lmbda = (4.0 - cos_alpha) / 3.0
    mu = sin_alpha + (cos_alpha - lmbda) * cot_alpha

    # Return rotated waypoints.
    return (
        math.cos(start),  # p0.x
        math.sin(start),  # p0.y
        lmbda * cos_phi + mu * sin_phi,  # p1.x
        lmbda * sin_phi - mu * cos_phi,  # p1.y
        lmbda * cos_phi - mu * sin_phi,  # p2.x
        lmbda * sin_phi + mu * cos_phi,  # p2.y
        math.cos(start + size),  # p3.x
        math.sin(start + size),
    )  # p3.y


def arc(ctxt, ostack):
    """
    x y r angle₁ angle₂ **arc** -


    appends an **arc** of a circle to the current path, possibly preceded by a straight line
    segment. The **arc** is centered at coordinates (x, y) in user space, with radius r. The
    operands angle₁ and angle₂ define the endpoints of the **arc** by specifying the angles
    of the vectors joining them to the center of the **arc**. The angles are measured in degrees
    counterclockwise from the positive x axis of the current user coordinate system.

    The **arc** produced is circular in user space. If user space is scaled nonuniformly
    (that is, differently in the x and y dimensions), the resulting curve will be elliptical
    in device space.

    If there is a current point, a straight line segment from the current point to the
    first endpoint of the **arc** is added to the current path preceding the **arc** itself. If the
    current path is empty, this initial line segment is omitted. In either case, the second
    endpoint of the **arc** becomes the new current point.

    If angle₂ is less than angle₁, it is increased by multiples of 360 until it becomes
    greater than or equal to angle₁. No other adjustments are made to the two angles.
    In particular, the angle subtended by the **arc** is not reduced modulo 360; if the difference
    angle₂ - angle₁ exceeds 360, the resulting path will trace portions of the
    circle more than once.

    The **arc** is represented internally by one or more cubic Bézier curves (see **curveto**)
    approximating the required shape. This is done with sufficient accuracy to produce
    a faithful rendition of the required **arc**. However, a program that reads the
    constructed path using **pathforall** will encounter **curveto** segments where arcs
    were specified originally.

    **Example**
        **newpath**
            0 0 **moveto**
            0 0 100 0 45 **arc**
        **closepath**

    This example constructs a 45-degree "pie slice" with a 100-unit radius, centered at
    the coordinate origin.

    **Errors**:     **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **arcn**, **arct**, **arcto**, **curveto**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 5:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, arc.__name__)
    # 2. TYPECHECK - Check operand types (x y r angle1 angle2)
    for i in range(-5, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, arc.__name__)

    x = ostack[-5].val
    y = ostack[-4].val
    rx, ry = ostack[-3].val, ostack[-3].val
    start = ostack[-2].val
    stop = ostack[-1].val

    while stop < start:
        stop += 360.0
    start = math.radians(start)
    stop = math.radians(stop)

    HALF_PI = math.pi / 2

    # Create curves
    epsilon = 0.00001  # Smallest visible angle on displays up to 4K.
    first = True
    while stop - start > epsilon:
        arcToDraw = min(stop - start, HALF_PI)
        p0_x, p0_y, p1_x, p1_y, p2_x, p2_y, p3_x, p3_y = _acuteArcToBezier(
            start, arcToDraw
        )
        if first:
            if ctxt.gstate.currentpoint is None:
                ctxt.gstate.path.append(ps.SubPath())
                subpath = ctxt.gstate.path[-1]
                subpath.append(
                    ps.MoveTo(
                        ps.Point(
                            *_transform_point(
                                ctxt.gstate.CTM, x + rx * p0_x, y + ry * p0_y
                            )
                        )
                    )
                )
            else:
                subpath = ctxt.gstate.path[-1]
                subpath.append(
                    ps.LineTo(
                        ps.Point(
                            *_transform_point(
                                ctxt.gstate.CTM, x + rx * p0_x, y + ry * p0_y
                            )
                        )
                    )
                )
            first = False
        subpath.append(
            ps.CurveTo(
                ps.Point(
                    *_transform_point(ctxt.gstate.CTM, x + rx * p1_x, y + ry * p1_y)
                ),
                ps.Point(
                    *_transform_point(ctxt.gstate.CTM, x + rx * p2_x, y + ry * p2_y)
                ),
                ps.Point(
                    *_transform_point(ctxt.gstate.CTM, x + rx * p3_x, y + ry * p3_y)
                ),
            )
        )
        _setcurrentpoint(
            ctxt, *_transform_point(ctxt.gstate.CTM, x + rx * p3_x, y + ry * p3_y)
        )

        start += arcToDraw

    ostack.pop()
    ostack.pop()
    ostack.pop()
    ostack.pop()
    ostack.pop()


def arcn(ctxt, ostack):
    """
    x y r angle₁ angle₂ **arcn** -


    (**arc** negative) appends an **arc** of a circle to the current path, possibly preceded by
    a straight line segment. Its behavior is identical to that of **arc**, except that the
    angles defining the endpoints of the **arc** are measured clockwise from the positive
    x axis of the user coordinate system, rather than counterclockwise. If angle₂ is
    greater than angle₁, it is decreased by multiples of 360 until it becomes less than or
    equal to angle₁.

    This example constructs a 90-degree "windshield wiper swath" 100 units wide, with
    an outer radius of 200 units, centered at the coordinate origin.

    **Example**
        **newpath**
            0 0 200 0 90 **arc**
            0 0 100 90 0 **arcn**
        **closepath**

    **Errors**:     **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **arc**, **arct**, **arcto**, **curveto**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 5:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, arcn.__name__)
    # 2. TYPECHECK - Check operand types (x y r angle1 angle2)
    for i in range(-5, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, arcn.__name__)

    x = ostack[-5].val
    y = ostack[-4].val
    rx, ry = ostack[-3].val, ostack[-3].val
    start = ostack[-1].val
    stop = ostack[-2].val

    while stop < start:
        stop += 360.0
    start = math.radians(start)
    stop = math.radians(stop)

    HALF_PI = math.pi / 2

    # Create curves
    epsilon = 0.00001  # Smallest visible angle on displays up to 4K.
    first = True

    curves = []
    while stop - start > epsilon:
        arcToDraw = min(stop - start, HALF_PI)
        p0_x, p0_y, p1_x, p1_y, p2_x, p2_y, p3_x, p3_y = _acuteArcToBezier(
            start, arcToDraw
        )
        curves.insert(0, [p3_x, p3_y, p2_x, p2_y, p1_x, p1_y, p0_x, p0_y])
        start += arcToDraw

    for p0_x, p0_y, p1_x, p1_y, p2_x, p2_y, p3_x, p3_y in curves:
        if first:
            if ctxt.gstate.currentpoint is None:
                ctxt.gstate.path.append(ps.SubPath())
                subpath = ctxt.gstate.path[-1]
                subpath.append(
                    ps.MoveTo(
                        ps.Point(
                            *_transform_point(
                                ctxt.gstate.CTM, x + rx * p0_x, y + ry * p0_y
                            )
                        )
                    )
                )
            else:
                subpath = ctxt.gstate.path[-1]
                subpath.append(
                    ps.LineTo(
                        ps.Point(
                            *_transform_point(
                                ctxt.gstate.CTM, x + rx * p0_x, y + ry * p0_y
                            )
                        )
                    )
                )
            first = False
        subpath.append(
            ps.CurveTo(
                ps.Point(
                    *_transform_point(ctxt.gstate.CTM, x + rx * p1_x, y + ry * p1_y)
                ),
                ps.Point(
                    *_transform_point(ctxt.gstate.CTM, x + rx * p2_x, y + ry * p2_y)
                ),
                ps.Point(
                    *_transform_point(ctxt.gstate.CTM, x + rx * p3_x, y + ry * p3_y)
                ),
            )
        )
        _setcurrentpoint(
            ctxt, *_transform_point(ctxt.gstate.CTM, x + rx * p3_x, y + ry * p3_y)
        )

    ostack.pop()
    ostack.pop()
    ostack.pop()
    ostack.pop()
    ostack.pop()


def _compute_arc_from_tangents(x0, y0, x1, y1, x2, y2, r):
    """
    Compute **arc** center, tangent points, angles, and direction from tangent line geometry.
    For **arct**: x0,y0 -> x1,y1 -> x2,y2 with radius r

    Returns:
        (cx, cy, xt1, yt1, xt2, yt2, start_angle, end_angle, clockwise)
        - cx, cy: **arc** center
        - xt1, yt1: first tangent point (on line from x0,y0 to x1,y1)
        - xt2, yt2: second tangent point (on line from x1,y1 to x2,y2)
        - start_angle, end_angle: angles in degrees from center to tangent points
        - clockwise: True if **arc** should be drawn clockwise

        Or "collinear" if lines are collinear or degenerate (zero-length tangent).
    """

    # Vectors FROM vertex (x1,y1) TO the other points
    # This gives us the direction we're coming from and going to
    u1x, u1y = x0 - x1, y0 - y1  # toward start point
    u2x, u2y = x2 - x1, y2 - y1  # toward end point

    u1_len = math.sqrt(u1x*u1x + u1y*u1y)
    u2_len = math.sqrt(u2x*u2x + u2y*u2y)

    if u1_len < 1e-10 or u2_len < 1e-10:
        # Degenerate case: currentpoint coincides with vertex, or vertex
        # coincides with second point.  Treat as collinear (lineto to x1,y1).
        # This happens at 72 DPI where device↔user round-trips are exact,
        # e.g. when a previous arcto's tangent point lands exactly on the
        # next arcto's vertex.  GhostScript handles this gracefully.
        return "collinear"

    # Normalize vectors
    u1x /= u1_len
    u1y /= u1_len
    u2x /= u2_len
    u2y /= u2_len

    # Cross product determines turn direction
    cross = u1x * u2y - u1y * u2x

    # Check for collinear
    # Use 1e-8 threshold to handle floating-point errors from coordinate transformations
    # (arcto converts currentpoint device->user via iCTM, which can introduce small errors)
    if abs(cross) < 1e-8:
        return "collinear"

    # Dot product for angle between vectors
    dot = u1x * u2x + u1y * u2y
    dot = max(-1.0, min(1.0, dot))

    # Half angle between the two lines
    half_angle = math.acos(dot) / 2.0

    if abs(math.sin(half_angle)) < 1e-10:
        return "collinear"

    # Distance from vertex to center along the angle bisector
    dist_to_center = abs(r) / math.sin(half_angle)

    # Distance from vertex to tangent points along each line
    dist_to_tangent = abs(r) / math.tan(half_angle)

    # Angle bisector direction (u1 + u2, normalized)
    bx = u1x + u2x
    by = u1y + u2y
    blen = math.sqrt(bx*bx + by*by)

    if blen < 1e-10:
        return "collinear"

    bx /= blen
    by /= blen

    # Center of the arc (along the bisector from vertex)
    cx = x1 + bx * dist_to_center
    cy = y1 + by * dist_to_center

    # Tangent points (along each line from vertex)
    xt1 = x1 + u1x * dist_to_tangent
    yt1 = y1 + u1y * dist_to_tangent
    xt2 = x1 + u2x * dist_to_tangent
    yt2 = y1 + u2y * dist_to_tangent

    # Calculate arc angles from center to tangent points
    start_angle = math.degrees(math.atan2(yt1 - cy, xt1 - cx))
    end_angle = math.degrees(math.atan2(yt2 - cy, xt2 - cx))

    # Determine arc direction based on cross product
    # cross > 0 means counterclockwise path turn (left turn) → arc should go clockwise
    # cross < 0 means clockwise path turn (right turn) → arc should go counterclockwise
    # The arc goes OPPOSITE direction of the path turn to "cut the corner"
    clockwise = cross > 0

    return (cx, cy, xt1, yt1, xt2, yt2, start_angle, end_angle, clockwise)


def arct(ctxt, ostack):
    """
    x1 y1 x2 y2 r **arct** -
    
    appends an **arc** of a circle to the current path, possibly preceded by a straight line
    segment. The **arc** is defined by a radius r and two tangent lines, drawn from the
    current point ( x0, y0) to ( x1, y1) and from ( x1, y1) to ( x2, y2). The center of the **arc**
    is located within the inner angle formed by the two tangent lines, and is the only point 
    located at a perpendicular distance r from both lines. The **arc** begins at the tangent 
    point ( xt1, yt1) on the first tangent line, passes between the center and the point ( x1, y1), 
    and ends at the tangent point ( xt2, yt2) on the second tangent line. If the current point is 
    undefined, a nocurrentpoint error occurs. 
    
    The **arc** produced is circular in user space. If user space is scaled nonuniformly
    (that is, differently in the x and y dimensions), the resulting curve will be elliptical
    in device space.
    
    PLRM Section 8.2, Page 532-533 (Third Edition)
    Stack: x1 y1 x2 y2 r **arct** -
    **Errors**: **limitcheck**, **nocurrentpoint**, **rangecheck**, **stackunderflow**, **typecheck**, **undefinedresult**
    """
    
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 5:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, arct.__name__)
    # 2. TYPECHECK - Check operand types (x1 y1 x2 y2 r)
    for i in range(-5, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, arct.__name__)
    
    # Check for current point
    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, arct.__name__)
    
    # Extract operands (keeping on stack for error handling)
    x1 = ostack[-5].val
    y1 = ostack[-4].val  
    x2 = ostack[-3].val
    y2 = ostack[-2].val
    r = ostack[-1].val
    
    # Note: Both positive and negative radius values are valid
    # Negative radius creates arc on opposite side of angle
    
    # Get current point (in device space)
    x0_device = ctxt.gstate.currentpoint.x
    y0_device = ctxt.gstate.currentpoint.y
    
    # Transform user space operands to device space
    x1_device, y1_device = _transform_point(ctxt.gstate.CTM, x1, y1)
    x2_device, y2_device = _transform_point(ctxt.gstate.CTM, x2, y2)
    # Compute scale factor correctly for any CTM (including rotations)
    # Scale factor is the length of a transformed unit vector: sqrt(a² + b²)
    ctm = ctxt.gstate.CTM.val
    scale_factor = math.sqrt(ctm[0].val**2 + ctm[1].val**2)
    r_device = r * scale_factor
    
    # Compute arc geometry in device space
    result = _compute_arc_from_tangents(x0_device, y0_device, x1_device, y1_device, x2_device, y2_device, r_device)
    if result is None:
        return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, arct.__name__)
    elif result == "collinear":
        # Handle collinear case per PLRM: create straight line segment to (x1, y1)
        # Remove operands from stack first
        ostack.pop()  # r
        ostack.pop()  # y2
        ostack.pop()  # x2
        ostack.pop()  # y1
        ostack.pop()  # x1

        # Add straight line segment to (x1, y1) in device space
        subpath = ctxt.gstate.path[-1]  # Current subpath exists (we have current point)
        subpath.append(ps.LineTo(ps.Point(x1_device, y1_device)))
        _setcurrentpoint(ctxt, x1_device, y1_device)
        return
    
    cx, cy, xt1, yt1, xt2, yt2, start_angle, end_angle, clockwise = result

    # Remove operands from stack (all validation passed)
    ostack.pop()  # r
    ostack.pop()  # y2
    ostack.pop()  # x2
    ostack.pop()  # y1
    ostack.pop()  # x1

    # Create arct path in device space coordinates
    # All coordinates (cx, cy, xt1, yt1, xt2, yt2) are now in device space

    current_x = ctxt.gstate.currentpoint.x
    current_y = ctxt.gstate.currentpoint.y

    # Determine actual arc start point
    # If currentpoint is close to xt1, use currentpoint as-is to avoid discontinuity
    if abs(current_x - xt1) > 1e-10 or abs(current_y - yt1) > 1e-10:
        # Need to add lineto - arc starts from xt1
        subpath = ctxt.gstate.path[-1]  # Current subpath exists
        subpath.append(ps.LineTo(ps.Point(xt1, yt1)))
        arc_start_x, arc_start_y = xt1, yt1
    else:
        # No lineto needed - arc starts from actual currentpoint
        arc_start_x, arc_start_y = current_x, current_y

    # Set current point to arc start
    _setcurrentpoint(ctxt, arc_start_x, arc_start_y)

    # Recalculate start angle based on actual arc start point
    start_rad = math.atan2(arc_start_y - cy, arc_start_x - cx)
    end_rad = math.atan2(yt2 - cy, xt2 - cx)

    # Use clockwise flag to determine arc direction
    # clockwise=True means arc goes clockwise (negative angle direction)
    # clockwise=False means arc goes counterclockwise (positive angle direction)
    if clockwise:
        # Clockwise: ensure end_rad < start_rad
        while end_rad >= start_rad:
            end_rad -= 2 * math.pi
    else:
        # Counterclockwise: ensure end_rad > start_rad
        while end_rad <= start_rad:
            end_rad += 2 * math.pi

    HALF_PI = math.pi / 2
    epsilon = 0.00001
    subpath = ctxt.gstate.path[-1]  # Get current subpath
    current_angle = start_rad

    # Create arc segments using Bézier curves
    # Force the final segment's endpoint to exactly match the tangent point
    if clockwise:
        # Clockwise: step in negative direction
        while current_angle > end_rad + epsilon:
            arcToDraw = max(end_rad - current_angle, -HALF_PI)
            is_last_segment = (current_angle + arcToDraw) <= end_rad + epsilon
            p0_x, p0_y, p1_x, p1_y, p2_x, p2_y, p3_x, p3_y = _acuteArcToBezier(
                current_angle, arcToDraw
            )
            # Use exact tangent point for final segment to avoid numerical drift
            if is_last_segment:
                end_x, end_y = xt2, yt2
            else:
                end_x = cx + abs(r_device) * p3_x
                end_y = cy + abs(r_device) * p3_y
            subpath.append(
                ps.CurveTo(
                    ps.Point(cx + abs(r_device) * p1_x, cy + abs(r_device) * p1_y),
                    ps.Point(cx + abs(r_device) * p2_x, cy + abs(r_device) * p2_y),
                    ps.Point(end_x, end_y)
                )
            )
            current_angle += arcToDraw
    else:
        # Counterclockwise: step in positive direction
        while current_angle < end_rad - epsilon:
            arcToDraw = min(end_rad - current_angle, HALF_PI)
            is_last_segment = (current_angle + arcToDraw) >= end_rad - epsilon
            p0_x, p0_y, p1_x, p1_y, p2_x, p2_y, p3_x, p3_y = _acuteArcToBezier(
                current_angle, arcToDraw
            )
            # Use exact tangent point for final segment to avoid numerical drift
            if is_last_segment:
                end_x, end_y = xt2, yt2
            else:
                end_x = cx + abs(r_device) * p3_x
                end_y = cy + abs(r_device) * p3_y
            subpath.append(
                ps.CurveTo(
                    ps.Point(cx + abs(r_device) * p1_x, cy + abs(r_device) * p1_y),
                    ps.Point(cx + abs(r_device) * p2_x, cy + abs(r_device) * p2_y),
                    ps.Point(end_x, end_y)
                )
            )
            current_angle += arcToDraw

    # Set current point to second tangent point
    _setcurrentpoint(ctxt, xt2, yt2)


def arcto(ctxt, ostack):
    """
    x1 y1 x2 y2 r **arcto** xt1 yt1 xt2 yt2
    
    appends an **arc** of a circle to the current path, possibly preceded by a straight line
    segment. Its behavior is identical to that of **arct**, except that it also returns the user
    space coordinates of the two tangent points ( xt1, yt1) and ( xt2, yt2) on the operand
    stack. 
    
    **arcto** is not allowed as an element of a user path (see Section 4.6, "User Paths"),
    whereas **arct** is allowed. 
    
    PLRM Section 8.2, Page 534 (Third Edition)
    Stack: x1 y1 x2 y2 r **arcto** xt1 yt1 xt2 yt2
    **Errors**: **limitcheck**, **nocurrentpoint**, **stackunderflow**, **typecheck**, **undefinedresult**
    """
    
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 5:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, arcto.__name__)
    # 2. TYPECHECK - Check operand types (x1 y1 x2 y2 r)
    for i in range(-5, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, arcto.__name__)
    
    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, arcto.__name__)
        
    # Calculate tangent points before arct consumes the operands
    x1_user = ostack[-5].val
    y1_user = ostack[-4].val  
    x2_user = ostack[-3].val
    y2_user = ostack[-2].val
    r_user = ostack[-1].val
    
    # Note: Both positive and negative radius values are valid
    # Negative radius creates arc on opposite side of angle
    
    # Get current point in user space (convert from device space)
    x0_user, y0_user = _transform_point(ctxt.gstate.iCTM, ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)

    # Calculate tangent points directly in user space (avoid coordinate conversion issues)
    result = _compute_arc_from_tangents(x0_user, y0_user, x1_user, y1_user, x2_user, y2_user, r_user)

    if result is None:
        # Let arct handle the error
        arct(ctxt, ostack)
        return
    elif result == "collinear":
        # Let arct handle collinear case, then add tangent points
        arct(ctxt, ostack)
        # For collinear case, tangent points are just the corner point
        ostack.append(ps.Real(x1_user))  # xt1
        ostack.append(ps.Real(y1_user))  # yt1  
        ostack.append(ps.Real(x1_user))  # xt2
        ostack.append(ps.Real(y1_user))  # yt2
        return
    
    # Extract tangent points (already in user space)
    cx, cy, xt1_user, yt1_user, xt2_user, yt2_user, start_angle, end_angle, clockwise = result

    # Remove operands from stack (all validation passed)
    ostack.pop()  # r
    ostack.pop()  # y2
    ostack.pop()  # x2
    ostack.pop()  # y1
    ostack.pop()  # x1

    # Transform user space coordinates to device space for path construction
    xt1_device, yt1_device = _transform_point(ctxt.gstate.CTM, xt1_user, yt1_user)
    xt2_device, yt2_device = _transform_point(ctxt.gstate.CTM, xt2_user, yt2_user)
    cx_device, cy_device = _transform_point(ctxt.gstate.CTM, cx, cy)
    x0_device, y0_device = _transform_point(ctxt.gstate.CTM, x0_user, y0_user)
    x1_device, y1_device = _transform_point(ctxt.gstate.CTM, x1_user, y1_user)
    x2_device, y2_device = _transform_point(ctxt.gstate.CTM, x2_user, y2_user)
    # Compute r_device from actual geometry (distance from center to tangent point)
    # This correctly handles any CTM including rotations
    r_device = math.sqrt((xt1_device - cx_device)**2 + (yt1_device - cy_device)**2)

    # Recompute clockwise flag in device space (Y-flip inverts direction)
    # Use device space vectors from vertex to the other points
    u1x_dev = x0_device - x1_device
    u1y_dev = y0_device - y1_device
    u2x_dev = x2_device - x1_device
    u2y_dev = y2_device - y1_device
    cross_device = u1x_dev * u2y_dev - u1y_dev * u2x_dev
    clockwise = cross_device > 0

    # Determine actual arc start point
    # If currentpoint is close to xt1, use currentpoint as-is to avoid discontinuity
    # Otherwise, add a lineto to xt1
    current_x = ctxt.gstate.currentpoint.x
    current_y = ctxt.gstate.currentpoint.y

    if abs(current_x - xt1_device) > 1e-10 or abs(current_y - yt1_device) > 1e-10:
        # Need to add lineto - arc starts from xt1_device
        subpath = ctxt.gstate.path[-1]
        subpath.append(ps.LineTo(ps.Point(xt1_device, yt1_device)))
        arc_start_x, arc_start_y = xt1_device, yt1_device
    else:
        # No lineto needed - arc starts from actual currentpoint
        # Use currentpoint for angle calculation to ensure control points match
        arc_start_x, arc_start_y = current_x, current_y

    # Set current point to arc start
    _setcurrentpoint(ctxt, arc_start_x, arc_start_y)

    # Calculate angles in device space based on ACTUAL arc start point
    start_angle_device = math.atan2(arc_start_y - cy_device, arc_start_x - cx_device)
    end_angle_device = math.atan2(yt2_device - cy_device, xt2_device - cx_device)

    # Use device space angles (already in radians)
    start_rad = start_angle_device
    end_rad = end_angle_device

    # Use clockwise flag to determine arc direction
    if clockwise:
        while end_rad >= start_rad:
            end_rad -= 2 * math.pi
    else:
        while end_rad <= start_rad:
            end_rad += 2 * math.pi

    HALF_PI = math.pi / 2
    epsilon = 0.00001
    subpath = ctxt.gstate.path[-1]
    current_angle = start_rad

    # Create arc segments using Bézier curves
    # Force the final segment's endpoint to exactly match the tangent point
    if clockwise:
        while current_angle > end_rad + epsilon:
            arcToDraw = max(end_rad - current_angle, -HALF_PI)
            is_last_segment = (current_angle + arcToDraw) <= end_rad + epsilon
            p0_x, p0_y, p1_x, p1_y, p2_x, p2_y, p3_x, p3_y = _acuteArcToBezier(
                current_angle, arcToDraw
            )
            # Use exact tangent point for final segment to avoid numerical drift
            if is_last_segment:
                end_x, end_y = xt2_device, yt2_device
            else:
                end_x = cx_device + r_device * p3_x
                end_y = cy_device + r_device * p3_y
            subpath.append(
                ps.CurveTo(
                    ps.Point(cx_device + r_device * p1_x, cy_device + r_device * p1_y),
                    ps.Point(cx_device + r_device * p2_x, cy_device + r_device * p2_y),
                    ps.Point(end_x, end_y)
                )
            )
            current_angle += arcToDraw
    else:
        while current_angle < end_rad - epsilon:
            arcToDraw = min(end_rad - current_angle, HALF_PI)
            is_last_segment = (current_angle + arcToDraw) >= end_rad - epsilon
            p0_x, p0_y, p1_x, p1_y, p2_x, p2_y, p3_x, p3_y = _acuteArcToBezier(
                current_angle, arcToDraw
            )
            # Use exact tangent point for final segment to avoid numerical drift
            if is_last_segment:
                end_x, end_y = xt2_device, yt2_device
            else:
                end_x = cx_device + r_device * p3_x
                end_y = cy_device + r_device * p3_y
            subpath.append(
                ps.CurveTo(
                    ps.Point(cx_device + r_device * p1_x, cy_device + r_device * p1_y),
                    ps.Point(cx_device + r_device * p2_x, cy_device + r_device * p2_y),
                    ps.Point(end_x, end_y)
                )
            )
            current_angle += arcToDraw

    # Set current point to second tangent point
    _setcurrentpoint(ctxt, xt2_device, yt2_device)

    # Add the calculated tangent points to the stack (in user space per PLRM)
    ostack.append(ps.Real(xt1_user))
    ostack.append(ps.Real(yt1_user))
    ostack.append(ps.Real(xt2_user))
    ostack.append(ps.Real(yt2_user))


def closepath(ctxt, ostack):
    """
    - **closepath** -


    closes the current subpath by appending a straight line segment connecting the
    current point to the subpath’s starting point, which is generally the point most recently
    specified by **moveto** (see Section 4.4, "Path Construction").

    **closepath** terminates the current subpath; appending another segment to the current
    path will begin a new subpath, even if the new segment begins at the endpoint
    reached by the **closepath** operation. If the current subpath is already closed
    or the current path is empty, **closepath** does nothing.

    **Errors**:     **limitcheck**
    **See Also**:   **newpath**, **moveto**, **lineto**
    """

    if ctxt.gstate.path and not isinstance(ctxt.gstate.path[-1], ps.ClosePath):
        ctxt.gstate.path[-1].append(ps.ClosePath())

        # set the current point
        i = -2
        subpath = ctxt.gstate.path[-1]

        while not isinstance(subpath[i], ps.MoveTo):
            i -= 1
        if isinstance(subpath[i], ps.CurveTo):
            ctxt.gstate.currentpoint = copy.copy(subpath[i].p3)
        else:
            ctxt.gstate.currentpoint = copy.copy(subpath[i].p)


def curveto(ctxt, ostack):
    """
    x₁ y₁ x₂ y₂ x₃ y₃ **cureveto** -


    appends a section of a cubic Bézier curve to the current path between the current
    point (x₀, y₀) and the endpoint (x₃, y₃), using (x₁, y₁) and (x₂, y₂) as the Bézier control
    points. The endpoint (x₃, y₃) becomes the new current point. If the current
    point is undefined because the current path is empty, a **nocurrentpoint** error occurs.

    The four points (x₀, y₀), (x₁, y₁), (x₂, y₂), and (x₃, y₃) define the shape of the curve
    geometrically. The curve is always entirely enclosed by the convex quadrilateral defined
    by the four points. It starts at (x₀, y₀), is tangent to the line from (x₀, y₀) to (x1, y1)
    at that point, and leaves the starting point in that direction.

    It ends at (x₃, y₃), is tangent to the line from (x₂, y₂) to (x₃, y₃) at that point, and
    approaches the endpoint from that direction. The lengths of the lines from (x₀, y₀)
    to (x₁, y₁) and from (x₂, y₂) to (x₃, y₃) represent, in a sense, the "velocity" of the
    path at the endpoints.

    Mathematically, a cubic Bézier curve is derived from a pair of parametric cubic
    equations:

        x(t) = aᵪt³ + bᵪt² + cᵪt + x₀
        y(t) = aᵧt³ + bᵧt² + cᵧt + y₀

    The cubic section produced by **cureveto** is the path traced by x(t) and y(t) as t
    ranges from 0 to 1. The Bézier control points corresponding to this curve are:

        x₁ = x₀ + cᵪ / 3                y₁ = y₀ + cᵧ / 3
        x₂ = x₁ + (bᵪ + cᵪ) / 3         y₂ = y₁ + (bᵧ + cᵧ) / 3
        x₃ = x₀ + aᵪ + bᵪ + cᵪ          y₃ = y₀ + aᵧ + bᵧ + cᵧ

    **Errors**:     **limitcheck**, **nocurrentpoint**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **moveto**, **lineto**, **arcto**, **arc**, **arcn**, **arct**

    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 6:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, curveto.__name__)
    # 2. TYPECHECK - Check operand types (x1 y1 x2 y2 x3 y3)
    for i in range(-6, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, curveto.__name__)

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, curveto.__name__)

    x1, y1 = _transform_point(ctxt.gstate.CTM, ostack[-6].val, ostack[-5].val)
    x2, y2 = _transform_point(ctxt.gstate.CTM, ostack[-4].val, ostack[-3].val)
    x3, y3 = _transform_point(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

    ctxt.gstate.path[-1].append(
        ps.CurveTo(ps.Point(x1, y1), ps.Point(x2, y2), ps.Point(x3, y3))
    )

    # update the currentpoint
    _setcurrentpoint(ctxt, x3, y3)

    for _ in range(-6, 0, 1):
        ostack.pop()


def lineto(ctxt, ostack):
    """
    x y **lineto** -


    appends a straight line segment to the current path (see Section 4.4, "Path Construction"),
    starting from the current point and extending to the coordinates
    (x, y) in user space. The endpoint (x, y) becomes the new current point.

    If the current point is undefined because the current path is empty, a **nocurrentpoint**
    error occurs.

    **Errors**:     **limitcheck**, **nocurrentpoint**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **rlineto**, **moveto**, **curveto**, **arc**, **closepath**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, lineto.__name__)
    # 2. TYPECHECK - Check operand types (x y)
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, lineto.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, lineto.__name__)

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, lineto.__name__)

    x, y = _transform_point(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

    ctxt.gstate.path[-1].append(ps.LineTo(ps.Point(x, y)))

    # update the currentpoint
    _setcurrentpoint(ctxt, x, y)

    ostack.pop()
    ostack.pop()


def moveto(ctxt, ostack):
    """
    x y **moveto** -


    starts a new subpath of the current path (see Section 4.4, "Path Construction") by
    setting the current point in the graphics state to the coordinates (x, y) in user
    space. No new line segments are added to the current path.

    If the previous path operation in the current path was **moveto** or **rmoveto**, that
    point is deleted from the current path and the new **moveto** point replaces it.

    **Errors**:     **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **rmoveto**, **lineto**, **curveto**, **arc**, **closepath**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, moveto.__name__)
    # 2. TYPECHECK - Check operand types (x y)
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, moveto.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, moveto.__name__)

    x, y = _transform_point(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

    # Per PLRM: "If the previous path operation was also a moveto or rmoveto,
    # that point is deleted from the current path and the new moveto point
    # replaces it."
    last = ctxt.gstate.path[-1] if ctxt.gstate.path else None
    if (last is not None and len(last) == 1
            and isinstance(last[0], ps.MoveTo)):
        last[0] = ps.MoveTo(ps.Point(x, y))
    else:
        ctxt.gstate.path.append(ps.SubPath())
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(x, y)))

    # update the currentpoint
    _setcurrentpoint(ctxt, x, y)

    ostack.pop()
    ostack.pop()


def newpath(ctxt, ostack):
    """
    - **newpath** -


    initializes the current path in the graphics state to an empty path.
    The current point becomes undefined.

    **Errors**:     none
    **See Also**:   **closepath**, **stroke**, **fill**, **eofill**, **currentpoint**
    """
    ctxt.gstate.path.clear()
    ctxt.gstate.currentpoint = None
    ctxt.gstate.bbox = None


def rcurveto(ctxt, ostack):
    """
    dx₁ dy₁ dx₂ dy₂ dx₃ dy₃ **rcurveto** -


    (relative **curveto**) appends a section of a cubic Bézier curve to the current path in
    the same manner as **curveto**. However, the operands are interpreted as relative
    displacements from the current point rather than as absolute coordinates. That is,
    **rcurveto** constructs a curve between the current point (x₀, y₀) and the endpoint
    (x₀ + dx₃, y₀ + dy₃), using (x₀ + dx₁, y₀ + dy₁) and (x₀ + dx₂, y₀ + dy₂) as the Bézier
    control points. In all other respects, the behavior of **rcurveto** is identical to that of
    **curveto**.

    **Errors**:     **limitcheck**, **nocurrentpoint**, **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **curveto**, **rlineto**, **rmoveto**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 6:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rcurveto.__name__)
    # 2. TYPECHECK - Check operand types (dx1 dy1 dx2 dy2 dx3 dy3)
    for i in range(-6, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, rcurveto.__name__)

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, rcurveto.__name__)

    x1, y1 = _transform_delta(ctxt.gstate.CTM, ostack[-6].val, ostack[-5].val)
    x2, y2 = _transform_delta(ctxt.gstate.CTM, ostack[-4].val, ostack[-3].val)
    x3, y3 = _transform_delta(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

    ctxt.gstate.path[-1].append(
        ps.CurveTo(
            ps.Point(ctxt.gstate.currentpoint.x + x1, ctxt.gstate.currentpoint.y + y1),
            ps.Point(ctxt.gstate.currentpoint.x + x2, ctxt.gstate.currentpoint.y + y2),
            ps.Point(ctxt.gstate.currentpoint.x + x3, ctxt.gstate.currentpoint.y + y3),
        )
    )

    # update the currentpoint
    _setcurrentpoint(
        ctxt, ctxt.gstate.currentpoint.x + x3, ctxt.gstate.currentpoint.y + y3
    )

    for _ in range(-6, 0, 1):
        ostack.pop()


def rmoveto(ctxt, ostack):
    """
    dx dy **rmoveto** -


    (relative **moveto**) starts a new subpath of the current path (see Section 4.4, "Path
    Construction") by displacing the coordinates of the current point dx user space
    units horizontally and dy units vertically, without connecting it to the previous
    current point. That is, the operands dx and dy are interpreted as relative
    displacements from the current point rather than as absolute coordinates. In all
    other respects, the behavior of **rmoveto** is identical to that of **moveto**.

    If the current point is undefined because the current path is empty, a
    **nocurrentpoint** error occurs.

    **Errors**:     **limitcheck**, **nocurrentpoint**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **moveto**, **rlineto**, **rcurveto**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rmoveto.__name__)
    # 2. TYPECHECK - Check operand types (dx dy)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, rmoveto.__name__)
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, rmoveto.__name__)

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, rmoveto.__name__)

    x, y = _transform_delta(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

    new_x = ctxt.gstate.currentpoint.x + x
    new_y = ctxt.gstate.currentpoint.y + y

    # Per PLRM: "the behavior of rmoveto is identical to that of moveto" —
    # consecutive movetos replace the previous moveto point.
    if (ctxt.gstate.path and len(ctxt.gstate.path[-1]) == 1
            and isinstance(ctxt.gstate.path[-1][0], ps.MoveTo)):
        ctxt.gstate.path[-1][0] = ps.MoveTo(ps.Point(new_x, new_y))
    else:
        ctxt.gstate.path.append(ps.SubPath())
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(new_x, new_y)))

    # update the currentpoint
    _setcurrentpoint(ctxt, new_x, new_y)

    ostack.pop()
    ostack.pop()


def rlineto(ctxt, ostack):
    """
    dx dy **rlineto** -


    (relative **lineto**) appends a straight line segment to the current path (see
    Section 4.4, "Path Construction"), starting from the current point and extending
    dx user space units horizontally and dy units vertically. That is, the operands dx
    and dy are interpreted as relative displacements from the current point rather
    than as absolute coordinates. In all other respects, the behavior of **rlineto** is
    identical to that of **lineto**.

    If the current point is undefined because the current path is empty, a
    **nocurrentpoint** error occurs.

    **Errors**:     **limitcheck**, **nocurrentpoint**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **lineto**, **rmoveto**, **rcurveto**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rlineto.__name__)
    # 2. TYPECHECK - Check operand types (dx dy)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, rlineto.__name__)
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, rlineto.__name__)

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, rlineto.__name__)

    x, y = _transform_delta(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

    ctxt.gstate.path[-1].append(
        ps.LineTo(
            ps.Point(ctxt.gstate.currentpoint.x + x, ctxt.gstate.currentpoint.y + y)
        )
    )

    # update the currentpoint
    _setcurrentpoint(
        ctxt, ctxt.gstate.currentpoint.x + x, ctxt.gstate.currentpoint.y + y
    )

    ostack.pop()
    ostack.pop()
