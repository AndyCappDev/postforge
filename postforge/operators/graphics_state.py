# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy

from ..core import error as ps_error
from ..core import types as ps
from .matrix import initmatrix
from .clipping import initclip


def currentflat(ctxt, ostack) -> None:
    """
    - **currentflat** num


    returns the current value of the flatness parameter in the graphics state.

    **Errors**:     **stackoverflow**
    **See Also**:   **setflat**, **flattenpath**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentflat.__name__)

    ostack.append(ps.Real(ctxt.gstate.flatness))


def currentlinecap(ctxt, ostack) -> None:
    """
    – **currentlinecap** int


    returns the current value of the line cap parameter in the graphics state.

    **Errors**:     **stackoverflow**
    **See Also**:   **setlinecap**, **stroke**, **currentlinejoin**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentlinecap.__name__)

    ostack.append(ps.Int(ctxt.gstate.line_cap))


def currentlinejoin(ctxt, ostack) -> None:
    """
    – **currentlinejoin** int


    returns the current value of the line join parameter in the graphics state.

    **Errors**:     **stackoverflow**
    **See Also**:   **setlinecap**, **stroke**, **currentlinewidth**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentlinejoin.__name__)

    ostack.append(ps.Int(ctxt.gstate.line_join))


def currentlinewidth(ctxt, ostack) -> None:
    """
    - **currentlinewidth** num


    returns the current value of the line width parameter in the graphics state.

    **Errors**:     **stackoverflow**
    **See Also**:   **setlinewidth**, **stroke**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentlinewidth.__name__)

    # Line width is stored in user space, return it directly
    ostack.append(ps.Real(ctxt.gstate.line_width))


def grestore(ctxt, ostack) -> None:
    """
    - **grestore** -


    resets the current graphics state from the one on the top of the graphics state stack
    and pops the graphics state stack, restoring the graphics state in effect at the time
    of the matching **gsave** operation. This operator provides a simple way to undo
    complicated transformations and other graphics state modifications without having
    to reestablish all graphics state parameters individually.

    If the topmost graphics state on the stack was saved with **save** rather than **gsave**
    (that is, if there has been no **gsave** operation since the most recent unmatched
    **save**), **grestore** restores that topmost graphics state without popping it from the
    stack. If there is no unmatched **save** (which can happen only during an unencapsulated
    job) and the graphics state stack is empty, **grestore** has no effect.

    **Errors**:     none
    **See Also**:   **gsave**, **grestoreall**, **save**, **setgstate**
    """


    if len(ctxt.gstate_stack):
        # Store current clipping path version before restore
        old_clip_version = ctxt.gstate.clip_path_version

        if ctxt.gstate_stack[-1].saved:
            # State was saved by 'save' - restore but don't pop
            ctxt.gstate = ctxt.gstate_stack[-1].copy()
        else:
            # State was saved by 'gsave' - restore and pop
            ctxt.gstate = ctxt.gstate_stack.pop()
        
        # Check if clipping path changed after restore
        if ctxt.gstate.clip_path_version != old_clip_version:
            # Clipping path changed - we need to reset Cairo clipping first, then apply new path
            from ..core.display_list_builder import DisplayListBuilder
            if not hasattr(ctxt, 'display_list_builder'):
                ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)
            
            # First: Add ClipElement with is_initclip=True to reset Cairo clipping
            reset_clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_NON_ZERO, is_initclip=True)
            ctxt.display_list.append(reset_clip_element)
            
            # Second: Check if restored path is full page by comparing each point
            pdm = ctxt.gstate.page_device
            is_full_page = False
            if b"MediaSize" not in pdm:
                # nulldevice or incomplete page device — skip full-page optimization
                pass
            elif (len(ctxt.gstate.clip_path) == 1 and len(ctxt.gstate.clip_path[0]) == 5):
                page_width = int(pdm[b"MediaSize"].get(ps.Int(0))[1].val)
                page_height = int(pdm[b"MediaSize"].get(ps.Int(1))[1].val)
                subpath = ctxt.gstate.clip_path[0]
                # Expected: MoveTo(0,0), LineTo(0,height), LineTo(width,height), LineTo(width,0), ClosePath
                if (isinstance(subpath[0], ps.MoveTo) and subpath[0].p.x == 0 and subpath[0].p.y == 0 and
                    isinstance(subpath[1], ps.LineTo) and subpath[1].p.x == 0 and subpath[1].p.y == page_height and
                    isinstance(subpath[2], ps.LineTo) and subpath[2].p.x == page_width and subpath[2].p.y == page_height and
                    isinstance(subpath[3], ps.LineTo) and subpath[3].p.x == page_width and subpath[3].p.y == 0 and
                    isinstance(subpath[4], ps.ClosePath)):
                    is_full_page = True
            
            if not is_full_page:
                # Add the actual clipping path
                clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_NON_ZERO)
                ctxt.display_list.append(clip_element)
            
            ctxt.display_list_builder.current_clip_version = ctxt.gstate.clip_path_version


def grestoreall(ctxt, ostack) -> None:
    """
    - **grestoreall** -


    repeatedly performs **grestore** operations until it encounters a graphics state that
    was saved by a **save** operation (as opposed to **gsave**), leaving that state on the top
    of the graphics state stack and resetting the current graphics state from it. If no
    such graphics state is encountered (which can happen only during an unencapsulated
    job), the current graphics state is reset to the bottommost state on the stack
    and the stack is cleared to empty. If the graphics state stack is empty, **grestoreall**
    has no effect.

    **Errors**:     none
    **See Also**:   **gsave**, **grestore**, **save**, **setgstate**
    """

    if not ctxt.gstate_stack:
        return  # No effect if stack is empty

    # Store current clipping path version before restore
    old_clip_version = ctxt.gstate.clip_path_version

    # Check if there are ANY save-created states on the stack
    has_save_state = any(state.saved for state in ctxt.gstate_stack)

    if not has_save_state:
        # Edge case: unencapsulated job - no save-created states
        # Restore from bottom-most state and clear stack
        if ctxt.gstate_stack:
            bottom_state = ctxt.gstate_stack[0].copy()
            ctxt.gstate_stack.clear()
            ctxt.gstate = bottom_state
    else:
        # Normal case: pop gsave-created states until we hit a save-created one
        while ctxt.gstate_stack and not ctxt.gstate_stack[-1].saved:
            ctxt.gstate_stack.pop()

        # Restore from save-created state (but leave it on stack)
        if ctxt.gstate_stack and ctxt.gstate_stack[-1].saved:
            ctxt.gstate = ctxt.gstate_stack[-1].copy()

    # Check if clipping path changed after restore (same logic as grestore)
    if ctxt.gstate.clip_path_version != old_clip_version:
        # Clipping path changed - we need to reset Cairo clipping first, then apply new path
        from ..core.display_list_builder import DisplayListBuilder
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        # First: Add ClipElement with is_initclip=True to reset Cairo clipping
        reset_clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_NON_ZERO, is_initclip=True)
        ctxt.display_list.append(reset_clip_element)

        # Second: Check if restored path is full page by comparing each point
        pdm = ctxt.gstate.page_device
        page_width = int(pdm[b"MediaSize"].get(ps.Int(0))[1].val)
        page_height = int(pdm[b"MediaSize"].get(ps.Int(1))[1].val)

        is_full_page = False
        if (len(ctxt.gstate.clip_path) == 1 and len(ctxt.gstate.clip_path[0]) == 5):
            subpath = ctxt.gstate.clip_path[0]
            # Expected: MoveTo(0,0), LineTo(0,height), LineTo(width,height), LineTo(width,0), ClosePath
            if (isinstance(subpath[0], ps.MoveTo) and subpath[0].p.x == 0 and subpath[0].p.y == 0 and
                isinstance(subpath[1], ps.LineTo) and subpath[1].p.x == 0 and subpath[1].p.y == page_height and
                isinstance(subpath[2], ps.LineTo) and subpath[2].p.x == page_width and subpath[2].p.y == page_height and
                isinstance(subpath[3], ps.LineTo) and subpath[3].p.x == page_width and subpath[3].p.y == 0 and
                isinstance(subpath[4], ps.ClosePath)):
                is_full_page = True

        if not is_full_page:
            # Add the actual clipping path
            clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_NON_ZERO)
            ctxt.display_list.append(clip_element)

        ctxt.display_list_builder.current_clip_version = ctxt.gstate.clip_path_version


def gsave(ctxt, ostack) -> None:
    """
    - **gsave** -


    pushes a copy of the current graphics state on the graphics state stack (see
    Section 4.2, "Graphics State"). All elements of the graphics state are saved, including
    the current transformation matrix, current path, clipping path, and identity of
    the raster output device, but not the contents of raster memory. The saved state
    can later be restored by a matching **grestore**. After saving the graphics state, **gsave**
    resets the clipping path stack in the current graphics state to empty.

    The **save** operator also implicitly performs a **gsave** operation, but restoring a
    graphics state saved by **save** is slightly different from restoring one saved by **gsave**;
    see the descriptions of **grestore** and **grestoreall**.

    Note that, unlike **save**, **gsave** does not return a **save** object on the operand stack to
    represent the saved state. **gsave** and **grestore** work strictly in a stacklike fashion,
    except for the wholesale restoration performed by **restore** and **grestoreall**.

    **Errors**:     **limitcheck**
    **See Also**:   **grestore**, **grestoreall**, **save**, **restore**, **gstate**, **currentgstate**,
                **clipsave**, **cliprestore**
    """

    # Check for graphics state stack overflow (max 10 levels per PostScript spec)
    if len(ctxt.gstate_stack) >= ps.G_STACK_MAX:
        return ps_error.e(ctxt, ps_error.LIMITCHECK, gsave.__name__)

    ctxt.gstate_stack.append(ctxt.gstate.copy())
    ctxt.gstate_stack[-1].saved = False
    # reset the clipping path stack in the current gstate to empty
    ctxt.gstate.clip_path_stack = ps.Stack(10)


def initgraphics(ctxt, ostack) -> None:
    """
    - **initgraphics** -


    resets the following parameters of the current graphics state to their default values,
    as follows:

        current transformation matrix (CTM)—default for device
        current position (current point)—undefined
        current path—empty
        current clipping path—default for device
        current color space—**DeviceGray**
        current color—black
        current line width—1 user space unit
        current line cap—butt end caps
        current line join—miter joins
        current miter limit—10
        current dash pattern—solid, unbroken lines

    All other graphics state parameters are left unchanged. These include the current
    output device, font parameter, **stroke** adjustment, clipping path stack, and all
    device-dependent parameters. **initgraphics** affects only the graphics state, not the
    contents of raster memory or the configuration of the current output device.

    **initgraphics** is equivalent to the following code:

        **initmatrix**
        **newpath**
        **initclip**
        0 **setgray**
        1 **setlinewidth**
        0 **setlinecap**
        0 **setlinejoin**
        10 **setmiterlimit**
        [] 0 **setdash**

    There are few situations in which a PostScript program should invoke
    **initgraphics** explicitly. A page description that invokes **initgraphics** usually produces
    incorrect results if it is embedded within another, composite page. A program
    requiring information about its initial graphics state should explicitly read
    and **save** that state at the beginning of the program rather than assume that the
    default state prevailed initially.

    **Errors**:     none
    **See Also**:   **grestoreall**
    """

    initmatrix(ctxt, ostack)
    initclip(ctxt, ostack)
    ctxt.gstate.currentpoint = None
    ctxt.gstate.path = ps.Path()
    # ctxt.gstate.clip_path = Path()
    
    # CRITICAL: Set default color space and color per PLRM
    ctxt.gstate.color_space = ["DeviceGray"]  # Must be DeviceGray per PLRM
    ctxt.gstate.color = [0.0]                 # Single gray component for black

    # Default line width is 1.0 user space units per PLRM
    # Line width is stored in user space, transformation happens at stroke time
    ctxt.gstate.line_width = 1.0

    ctxt.gstate.line_cap = 0
    ctxt.gstate.line_join = 0

    ctxt.gstate.miter_limit = 10.0

    ctxt.gstate.flatness = 1.0

    ctxt.gstate.dash_pattern = [[], 0]


def setdash(ctxt, ostack) -> None:
    """
    array offset **setdash** -


    sets the dash pattern parameter in the graphics state. This parameter controls the
    lines to be drawn by subsequent invocations of **stroke** and related operators, such
    as **rectstroke** and **ustroke**. An empty (zero-length) array operand denotes solid,
    unbroken lines. If array is not empty, its elements (which must be nonnegative
    numbers and not all zero) define the sequence of dashes and gaps constituting the
    dash pattern.

    The elements of array alternately specify the length of a dash and the length of a
    gap between dashes, expressed in units of the user coordinate system. The **stroke**
    operator uses these elements cyclically; when it reaches the end of the array, it
    starts again at the beginning.

    Dashed strokes wrap around curves and corners in the same way as solid strokes.
    The ends of each dash are treated with the current line cap, and corners within a
    dash are treated with the current line join. **stroke** takes no measures to coordinate
    the dash pattern with features of the path itself; it simply dispenses dashes along
    the path in the pattern defined by array.

    The offset operand can be thought of as the “phase” of the dash pattern relative to
    the start of the path. It is interpreted as a distance into the dash pattern (measured
    in user space units) at which to start the pattern. Before beginning to **stroke** a
    path, the **stroke** operator cycles through the elements of array, adding up distances
    and alternating dashes and gaps as usual, but without generating any output.
    When the accumulated distance reaches the value specified by offset, it begins
    stroking from the starting point of the path, using the dash pattern from the point
    that has been reached. Each subpath of a path is treated independently; the dash
    pattern is restarted and the offset reapplied at the beginning of each subpath.

    **Examples**
        [] 0 **setdash**        % Solid, unbroken lines
        [3] 0 **setdash**       % 3 units on, 3 units off, ...
        [2] 1 **setdash**       % 1 on, 2 off, 2 on, 2 off, ...
        [2 1] 0 **setdash**     % 2 on, 1 off, 2 on, 1 off, ...
        [3 5] 6 **setdash**     % 2 off, 3 on, 5 off, 3 on, 5 off, ...
        [2 3] 11 **setdash**    % 1 on, 3 off, 2 on, 3 off, 2 on, ...

    **Errors**:     **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **currentdash**, **stroke**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setdash.__name__)
    # 2. TYPECHECK - Check operand types (offset array)
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setdash.__name__)
    if ostack[-2].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setdash.__name__)

    # Check array elements are numeric - need to use list access since range() returns indices not objects
    for i in range(ostack[-2].length):
        if ostack[-2].val[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setdash.__name__)

    if ostack[-2].length and all(
        ostack[-2].val[i].val == 0 for i in range(ostack[-2].length)
    ):
        return ps_error.e(ctxt, ps_error.RANGECHECK, setdash.__name__)

    # Store dash pattern in user space - transformation to device space happens at stroke time
    # This ensures that if the CTM changes after setdash, the dash pattern will be
    # correct when stroke is called.
    dashes = [
        abs(ostack[-2].val[i].val)
        for i in range(ostack[-2].length)
    ]
    ctxt.gstate.dash_pattern = [
        dashes,
        abs(ostack[-1].val),
    ]

    ostack.pop()
    ostack.pop()


def currentdash(ctxt, ostack) -> None:
    """
    - **currentdash** array offset

    returns an array and offset defining the current value of the dash pattern
    parameter in the graphics state.

    **Errors**:     **stackoverflow**
    **See Also**:   **setdash**, **stroke**
    """
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack - 1:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentdash.__name__)

    # Dash pattern is stored in user space, return it directly
    dashes_user, offset_user = ctxt.gstate.dash_pattern

    # Create the array
    arr = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
    arr.setval([ps.Real(d) for d in dashes_user])

    ostack.append(arr)
    ostack.append(ps.Real(offset_user))


def setflat(ctxt, ostack) -> None:
    """
    num **setflat** -


    sets the flatness parameter in the graphics state to num, which must be a positive
    number. This parameter controls the precision with which curved path segments
    are rendered on the raster output device by operators such as **stroke**, **fill**, and **clip**.
    These operators render curves by approximating them with a series of straight
    line segments. Flatness is the error tolerance of this approximation; it is the maximum
    allowable distance of any point of the approximation from the corresponding
    point on the true curve, measured in output device pixels. The acceptable
    range of values is 0.2 to 100.0. If num is outside this range, the nearest valid value
    is substituted without error indication.

    The choice of a flatness value is a tradeoff between precision and execution efficiency.
    Very small values (less than 1 device pixel) produce very precise curves at
    high cost, because enormous numbers of tiny line segments must be generated.
    Larger values produce cruder approximations with substantially less computation.
    A default value of the flatness parameter is established by the device setup
    (**Install**) procedure for each raster output device. This value is based on the characteristics
    of the individual device and is suitable for most applications.

    **setflat** sets a graphics state parameter whose effect is device-dependent. It should
    not be used in a page description that is intended to be device-independent.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **currentflat**, **flattenpath**, **stroke**, **fill**, **clip**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setflat.__name__)

    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setflat.__name__)

    flatness = min(max(ostack[-1].val, 0.2), 100)

    ctxt.gstate.flatness = flatness

    ostack.pop()

def setlinecap(ctxt, ostack) -> None:
    """
    int **setlinecap** -


    sets the line cap parameter in the graphics state to int, which must be 0, 1, or 2.
    This parameter controls the shape to be painted at the ends of open subpaths (and
    dashes, if any) by subsequent invocations of **stroke** and related operators, such as
    **ustroke** (see Section 4.5.1, "Stroking"). Possible values are as follows.

    0   Butt cap. The **stroke** is squared off at the endpoint of the path. There is no
        projection beyond the end of the path.

    1   Round cap. A semicircular **arc** with a diameter equal to the line width is
        drawn around the endpoint and filled in.

    2   Projecting square cap. The **stroke** continues beyond the endpoint of the
        path for a distance equal to half the line width and is then squared off.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **currentlinecap**, **stroke**, **setlinejoin**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setlinecap.__name__)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setlinecap.__name__)

    if ostack[-1].val < ps.LINE_CAP_BUTT or ostack[-1].val > ps.LINE_CAP_SQUARE:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setlinecap.__name__)

    ctxt.gstate.line_cap = ostack[-1].val

    ostack.pop()


def setlinejoin(ctxt, ostack) -> None:
    """
    int **setlinejoin** -


    sets the line join parameter in the graphics state to int, which must be 0, 1, or 2.
    This parameter controls the shape to be painted at corners by subsequent invocations
    of **stroke** and related operators, such as **rectstroke** and **ustroke** (see
    Section 4.5.1, "Stroking"). Possible values are as follows:

    0   Miter join. The outer edges of the strokes for the two segments are
        extended until they meet at an angle, as in a picture frame. If the segments
        meet at too sharp an angle (as defined by the miter limit parameter—see
        **setmiterlimit**), a bevel join is used instead.

    1   Round join. A circular **arc** with a diameter equal to the line width is drawn
        around the point where the two segments meet and is filled in, producing
        a rounded corner. **stroke** draws a full circle at this point; if path segments
        shorter than half the line width meet at sharp angles, an unintended
        "wrong side" of this circle may appear.

    2   Bevel join. The two segments are finished with butt caps (see **setlinecap**),
        and the resulting notch beyond the ends of the segments is filled with a
        triangle.

    Join styles are significant only at points where consecutive segments of a path
    connect at an angle. Segments that meet or intersect fortuitously receive no special
    treatment. Curved segments are actually rendered as sequences of straight line
    segments, and the current line join is applied to the "corners" between these segments.
    However, for typical values of the flatness parameter (see **setflat**), the corners
    are so shallow that the difference between join styles is not visible.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **currentlinejoin**, **stroke**, **setlinecap**, **setmiterlimit**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setlinejoin.__name__)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setlinejoin.__name__)

    if ostack[-1].val < ps.LINE_JOIN_MITER or ostack[-1].val > ps.LINE_JOIN_BEVEL:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setlinejoin.__name__)

    ctxt.gstate.line_join = ostack[-1].val

    ostack.pop()


def setlinewidth(ctxt, ostack) -> None:
    """
    num **setlinewidth** -


    sets the line width parameter in the graphics state to num. This parameter controls
    the thickness of lines to be drawn by subsequent invocations of **stroke** and related
    operators, such as **rectstroke** and **ustroke**. When stroking a path, **stroke** paints all
    points whose perpendicular distance from the path in user space is less than or
    equal to half the absolute value of num. The effect produced in device space depends
    on the current transformation matrix (CTM) in effect at the time the path
    is stroked. If the CTM specifies scaling by different factors in the x and y
    dimensions, the thickness of stroked lines in device space will vary according
    to their orientation.

    A line width of 0 is acceptable, and is interpreted as the thinnest line that can be
    rendered at device resolution—1 device pixel wide. However, some devices cannot
    reproduce 1-pixel lines, and on high-resolution devices, they are nearly invisible.
    Since the results of rendering such "zero-width" lines are device-dependent,
    their use is not recommended.

    The actual line width achieved by **stroke** can differ from the requested width by as
    much as 2 device pixels, depending on the positions of lines with respect to the
    pixel grid. Automatic **stroke** adjustment (see **setstrokeadjust**) can be used to ensure
    uniform line width.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **currentlinewidth**, **stroke**, **setstrokeadjust**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setlinewidth.__name__)

    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setlinewidth.__name__)

    # Store line width in user space - transformation to device space happens at stroke time
    # This ensures that if the CTM changes after setlinewidth, the visual line width
    # will be correct when stroke is called.
    ctxt.gstate.line_width = float(abs(ostack[-1].val))

    ostack.pop()


def setmiterlimit(ctxt, ostack) -> None:
    """
    num **setmiterlimit** -


    sets the miter limit parameter in the graphics state to num, which must be a number
    greater than or equal to 1. This parameter controls the treatment of corners by
    **stroke** and related operators, such as **rectstroke** and **ustroke** (see Section 4.5.1,
    “Stroking”), when miter joins have been specified by **setlinejoin**. When path segments
    connect at a sharp angle, a miter join will result in a spike that extends well
    beyond the connection point. The purpose of the miter limit is to cut off such
    spikes when they become objectionably long.

    At any given corner, the miter length is the distance from the point at which the
    inner edges of the strokes intersect to the point at which their outer edges intersect
    This distance increases as the angle between the segments decreases. If the ratio of
    the miter length to the line width exceeds the specified miter limit, the **stroke**
    operator treats the corner with a bevel join instead of a miter join.

    Example miter limit values are:
        • 1.414 cuts off miters (converts them to bevels) at angles less than 90 degrees.
        • 2.0 cuts off miters at angles less than 60 degrees.
        • 10.0 cuts off miters at angles less than 11 degrees.
        • 1.0 cuts off miters at all angles, so that bevels are always produced even when
          miters are specified.

    The default value of the miter limit is 10.0

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **currentmiterlimit**, **stroke**, **setlinejoin**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setmiterlimit.__name__)

    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setmiterlimit.__name__)

    if ostack[-1].val < 1:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setmiterlimit.__name__)

    ctxt.gstate.miter_limit = ostack[-1].val

    ostack.pop()


def currentmiterlimit(ctxt, ostack) -> None:
    """
    - **currentmiterlimit** num

    returns the current value of the miter limit parameter in the graphics state.

    **Errors**:     **stackoverflow**
    **See Also**:   **setmiterlimit**, **stroke**
    """
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentmiterlimit.__name__)

    ostack.append(ps.Real(ctxt.gstate.miter_limit))


def setstrokeadjust(ctxt, ostack) -> None:
    """
    bool **setstrokeadjust** -

    sets the **stroke** adjust parameter in the graphics state to bool. If bool is true, 
    automatic **stroke** adjustment will be performed during subsequent execution of **stroke** 
    and related operators, including **strokepath** (see section 6.5, "Scan Conversion Details"). 
    If bool is false, **stroke** adjustment will not be performed.

    The initial value of the **stroke** adjustment parameter is device dependent; typically 
    it is true for displays and false for printers. It is set to false when a font's 
    BuildChar or BuildGlyph procedure is called, but the procedure can change it. 
    It is not altered by **initgraphics**.

    PLRM Section 8.2, Page 683
    Stack: bool → -
    **Errors**: **stackunderflow**, **typecheck**
    """
    # 1. Stack validation - must be done BEFORE popping operands
    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setstrokeadjust.__name__)

    # 2. Type validation - must be done BEFORE popping operands  
    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setstrokeadjust.__name__)

    # 3. ONLY after all validation passes - set stroke adjust parameter
    bool_value = ostack[-1].val
    ctxt.gstate.stroke_adjust = bool_value

    # 4. Pop operand only after successful completion
    ostack.pop()


def currentstrokeadjust(ctxt, ostack) -> None:
    """
    - **currentstrokeadjust** bool

    returns the current **stroke** adjust parameter in the graphics state.

    PLRM Section 8.2, Page 531
    Stack: - → bool
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentstrokeadjust.__name__)

    # 2. Push current stroke adjust value onto operand stack
    ostack.append(ps.Bool(ctxt.gstate.stroke_adjust))


def setoverprint(ctxt, ostack) -> None:
    """
    bool **setoverprint** –

    sets the overprint parameter in the graphics state to bool. On output devices 
    capable of producing separations or of generating composite output in multiple 
    colorants, this parameter controls whether painting in one separation or colorant 
    causes the corresponding areas of other separations or colorants to be erased 
    (false) or left unchanged (true); see Section 4.8.5, "Overprint Control." The 
    default value is false.

    PLRM Section 8.2, Page 677
    Stack: bool → –
    **Errors**: **stackunderflow**, **typecheck**
    """
    # 1. Stack underflow validation - requires 1 operand
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setoverprint.__name__)

    # 2. Type validation - must be done BEFORE popping operands  
    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setoverprint.__name__)

    # 3. ONLY after all validation passes - set overprint parameter
    bool_value = ostack[-1].val
    ctxt.gstate.overprint = bool_value

    # 4. Pop operand only after successful completion
    ostack.pop()


def currentoverprint(ctxt, ostack) -> None:
    """
    – **currentoverprint** bool

    returns the current value of the overprint parameter in the graphics state.

    PLRM Section 8.2, Page 560
    Stack: – → bool
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentoverprint.__name__)

    # 2. Push current overprint value onto operand stack
    ostack.append(ps.Bool(ctxt.gstate.overprint))


# =============================================================================
# GSTATE OPERATORS (PostScript Level 2)
# =============================================================================

def gstate(ctxt, ostack) -> None:
    """
    - **gstate** **gstate**

    creates a new **gstate** (graphics state) object and pushes it on the operand stack.
    Its initial value is a copy of the current graphics state.

    This operator consumes VM; it is the only graphics state operator that does.
    The **gstate** is allocated in either local or global VM according to the current VM
    allocation mode (see section 3.7, "Memory Management").

    If **gstate** is allocated in global VM, **gstate** will generate an invalidaccess error
    if any of the composite objects in the current graphics state are in local VM.
    Such objects might include the current font, screen function, halftone dictionary,
    transfer function, or dash pattern. In general, allocating **gstate** objects in
    global VM is risky and should be avoided.

    **Errors**:     **invalidaccess**, **stackoverflow**, **VMerror**
    **See Also**:   **currentgstate**, **setgstate**
    """

    # Check for operand stack overflow
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, gstate.__name__)

    # Create new GState object with copy of current graphics state
    try:
        # Deep copy current graphics state
        graphics_state_copy = ctxt.gstate.copy()

        # Create GState object in current VM allocation mode
        new_gstate = ps.GState(
            ctxt_id=ctxt.id,
            graphics_state=graphics_state_copy,
            is_global=ctxt.vm_alloc_mode
        )

        # Validate global VM constraints if allocating in global VM
        if ctxt.vm_alloc_mode and not new_gstate.validate_global_vm_constraints(ctxt):
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, gstate.__name__)

        # Push new gstate object onto operand stack
        ostack.append(new_gstate)

    except MemoryError:
        return ps_error.e(ctxt, ps_error.VMERROR, gstate.__name__)


def currentgstate(ctxt, ostack) -> None:
    """
    gstate **currentgstate** gstate

    replaces the value of the gstate object by a copy of the current graphics state
    and pushes gstate back on the operand stack. If gstate is in global VM (see
    section 3.7, "Memory Management"), **currentgstate** will generate an invalidaccess
    error if any of the composite objects in the current graphics state are in local VM.
    Such objects might include the current font, screen function, halftone dictionary,
    transfer function, or dash pattern. In general, allocating gstate objects in
    global VM is risky and should be avoided.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **gstate**, **setgstate**
    """

    # Check for operand stack underflow
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, currentgstate.__name__)

    # Check operand type
    if ostack[-1].TYPE != ps.T_GSTATE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, currentgstate.__name__)

    # Get the gstate object (don't pop yet)
    gstate_obj = ostack[-1]

    # Create copy of current graphics state
    try:
        graphics_state_copy = ctxt.gstate.copy()

        # If gstate object is in global VM, validate constraints
        if gstate_obj.is_global:
            # Create temporary GState to validate constraints
            temp_gstate = ps.GState(
                ctxt_id=ctxt.id,
                graphics_state=graphics_state_copy,
                is_global=True
            )
            if not temp_gstate.validate_global_vm_constraints(ctxt):
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, currentgstate.__name__)

        # Replace the gstate object's value with current graphics state copy
        gstate_obj.val = graphics_state_copy

        # gstate object is already on stack, so we're done

    except MemoryError:
        return ps_error.e(ctxt, ps_error.VMERROR, currentgstate.__name__)


def setgstate(ctxt, ostack) -> None:
    """
    gstate **setgstate** -

    replaces the current graphics state by the value of the gstate object. This is a
    copying operation, so subsequent modifications to the value of gstate will not
    affect the current graphics state or vice versa. Note that this is a wholesale
    replacement of all components of the graphics state; in particular, the current
    clipping path is replaced by the value in gstate, not intersected with it
    (see section 4.2, "Graphics State").

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **gstate**, **currentgstate**
    """

    # Check for operand stack underflow
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setgstate.__name__)

    # Check operand type
    if ostack[-1].TYPE != ps.T_GSTATE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setgstate.__name__)

    # Pop the gstate object
    gstate_obj = ostack.pop()

    # Store current clipping path version before replacement
    old_clip_version = ctxt.gstate.clip_path_version

    # Replace current graphics state with copy from gstate object
    try:
        # Deep copy graphics state from gstate object
        ctxt.gstate = gstate_obj.val.copy()

        # Check if clipping path changed after setgstate
        if ctxt.gstate.clip_path_version != old_clip_version:
            # Clipping path changed - update display list similar to grestore
            from ..core.display_list_builder import DisplayListBuilder
            if not hasattr(ctxt, 'display_list_builder'):
                ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

            # First: Add ClipElement with is_initclip=True to reset Cairo clipping
            reset_clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_NON_ZERO, is_initclip=True)
            ctxt.display_list.append(reset_clip_element)

            # Second: Check if restored path is full page
            pdm = ctxt.gstate.page_device
            page_width = int(pdm[b"MediaSize"].get(ps.Int(0))[1].val)
            page_height = int(pdm[b"MediaSize"].get(ps.Int(1))[1].val)

            is_full_page = False
            if (len(ctxt.gstate.clip_path) == 1 and len(ctxt.gstate.clip_path[0]) == 5):
                subpath = ctxt.gstate.clip_path[0]
                # Expected: MoveTo(0,0), LineTo(0,height), LineTo(width,height), LineTo(width,0), ClosePath
                if (isinstance(subpath[0], ps.MoveTo) and subpath[0].p.x == 0 and subpath[0].p.y == 0 and
                    isinstance(subpath[1], ps.LineTo) and subpath[1].p.x == 0 and subpath[1].p.y == page_height and
                    isinstance(subpath[2], ps.LineTo) and subpath[2].p.x == page_width and subpath[2].p.y == page_height and
                    isinstance(subpath[3], ps.LineTo) and subpath[3].p.x == page_width and subpath[3].p.y == 0 and
                    isinstance(subpath[4], ps.ClosePath)):
                    is_full_page = True

            if not is_full_page:
                # Add the actual clipping path
                clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_NON_ZERO)
                ctxt.display_list.append(clip_element)

            ctxt.display_list_builder.current_clip_version = ctxt.gstate.clip_path_version

    except Exception as e:
        return ps_error.e(ctxt, ps_error.VMERROR, setgstate.__name__)

