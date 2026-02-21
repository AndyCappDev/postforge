# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from copy import copy

from ..core import color_space
from ..core import error as ps_error
from ..core import icc_default
from ..core import icc_profile
from ..core import mesh_shading
from ..core import ps_function
from ..core import types as ps
from .matrix import _transform_delta, _transform_point, itransform
from .path import newpath
from .strokepath import strokepath
from ..core.display_list_builder import DisplayListBuilder



def erasepage(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **erasepage** -


    erases the current page by painting it with gray level 1.0 (which is ordinarily white,
    but may be some other color if an atypical transfer function has been defined).
    The entire page is erased, without reference to the clipping path currently in force.
    **erasepage** affects only the contents of raster memory; it does not modify the
    graphics state, nor does it cause a page to be transmitted to the output device.

    The **showpage** operator automatically invokes **erasepage** after imaging a page.
    There are few situations in which a PostScript page description should invoke
    **erasepage** explicitly, since it affects portions of the page outside the current clipping
    path. It is usually more appropriate to erase just the area inside the current
    clipping path (see **clippath**). This allows the page description to be embedded
    within another, composite page without undesirable effects.

    **Errors**:     **none**
    **See Also**:   **showpage**, **fill**, **clippath**
    """

    ctxt.display_list = ps.DisplayList()

    # Notify interactive display to refresh (show blank page)
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            # Don't let callback errors break PostScript execution
            pass


def fill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **fill** -


    paints the area inside the current path with the current color. The nonzero winding
    number rule is used to determine what points lie inside the path
    (see "Nonzero Winding Number Rule" on page 195).

    **fill** implicitly closes any open subpaths of the current path before painting. Any
    previous contents of the filled area are obscured, so an area can be erased by filling
    it with the current color set to white.

    After filling the current path, **fill** clears it with an implicit **newpath** operation. To
    preserve the current path across a **fill** operation, use the sequence

        **gsave**
            **fill**
        **grestore**

    **Errors**:     **limitcheck**
    **See Also**:   **stroke**, **eofill**, **ufill**, **shfill**
    """

    if ctxt.gstate.path:
        # Create DisplayListBuilder if it doesn't exist
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        # Add path element
        ctxt.display_list_builder.add_graphics_operation(ctxt, ctxt.gstate.path)

        # Check if current color space is Pattern
        current_space = ctxt.gstate.color_space[0] if isinstance(ctxt.gstate.color_space, list) else ctxt.gstate.color_space
        if current_space == "Pattern" and getattr(ctxt.gstate, '_current_pattern', None):
            # Pattern fill - create PatternFill element
            pattern_dict = ctxt.gstate._current_pattern
            # For uncolored patterns, convert underlying color to device space
            underlying_color = None
            if ctxt.gstate.color:  # Has underlying color components
                underlying_space = ctxt.gstate.color_space[1] if len(ctxt.gstate.color_space) > 1 else "DeviceGray"
                underlying_color = color_space.convert_to_device_color(
                    ctxt, ctxt.gstate.color, [underlying_space])
            ctxt.display_list_builder.add_graphics_operation(
                ctxt, ps.PatternFill(pattern_dict, ps.WINDING_NON_ZERO, ctxt.gstate, underlying_color))
        else:
            # Convert color to device space and add fill element
            # Handle Pattern color space without pattern set - use fallback color
            if current_space == "Pattern":
                # Pattern color space but no pattern - use black as fallback
                device_color = [0.0, 0.0, 0.0]
            else:
                device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
                # Ensure we have a valid color
                if not device_color:
                    device_color = [0.0, 0.0, 0.0]
            ctxt.display_list_builder.add_graphics_operation(ctxt, ps.Fill(device_color, ps.WINDING_NON_ZERO))

        # Clear current path
        ctxt.gstate.path = ps.Path()
        ctxt.gstate.currentpoint = None


def eofill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **eofill** -


    paints the area inside the current path with the current color. The even-odd rule is
    used to determine what points lie inside the path (see "Even-Odd Rule" on
    page 196). In all other respects, the behavior of **eofill** is identical to that of **fill**.

    **Errors**:     **limitcheck**
    **See Also**:   **fill**, **ineofill**, **ueofill**
    """

    if ctxt.gstate.path:
        # Create DisplayListBuilder if it doesn't exist
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        # Add path element
        ctxt.display_list_builder.add_graphics_operation(ctxt, ctxt.gstate.path)

        # Check if current color space is Pattern
        current_space = ctxt.gstate.color_space[0] if isinstance(ctxt.gstate.color_space, list) else ctxt.gstate.color_space
        if current_space == "Pattern" and getattr(ctxt.gstate, '_current_pattern', None):
            # Pattern fill - create PatternFill element
            pattern_dict = ctxt.gstate._current_pattern
            # For uncolored patterns, convert underlying color to device space
            underlying_color = None
            if ctxt.gstate.color:  # Has underlying color components
                underlying_space = ctxt.gstate.color_space[1] if len(ctxt.gstate.color_space) > 1 else "DeviceGray"
                underlying_color = color_space.convert_to_device_color(
                    ctxt, ctxt.gstate.color, [underlying_space])
            ctxt.display_list_builder.add_graphics_operation(
                ctxt, ps.PatternFill(pattern_dict, ps.WINDING_EVEN_ODD, ctxt.gstate, underlying_color))
        else:
            # Convert color to device space and add fill element
            # Handle Pattern color space without pattern set - use fallback color
            if current_space == "Pattern":
                # Pattern color space but no pattern - use black as fallback
                device_color = [0.0, 0.0, 0.0]
            else:
                device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
                # Ensure we have a valid color
                if not device_color:
                    device_color = [0.0, 0.0, 0.0]
            ctxt.display_list_builder.add_graphics_operation(ctxt, ps.Fill(device_color, ps.WINDING_EVEN_ODD))

        # Clear current path
        ctxt.gstate.path = ps.Path()
        ctxt.gstate.currentpoint = None


def rectfill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
      x y width height **rectfill** -
              numarray **rectfill** -
             numstring **rectfill** - (not supported yet)


    paints the area inside a path consisting of one or more rectangles defined by the
    operands, using the current color. In the first form, the operands are four numbers
    that define a single rectangle. In the other two forms, the operand is an array
    or an encoded number string that defines an arbitrary number of rectangles (see
    Sections 3.14.5, "Encoded Number Strings," and 4.6.5, "Rectangles"). **rectfill**
    neither reads nor alters the current path in the graphics state.

    Assuming width and height are positive, the first form of the operator is
    equivalent to the following code:

        **gsave**
            **newpath**
            x y **moveto**
            width 0 **rlineto**
            0 height **rlineto**
            width neg 0 **rlineto**
            **closepath**
            **fill**
        **grestore**

    **Errors**:     **limitcheck**, **stackunderflow**, **typecheck**
    **See Also**:   **fill**, **rectstroke**, **rectclip**
    """

    # 1. STACKUNDERFLOW - Check minimum stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rectfill.__name__)

    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_STRING}:
        # first form: x y width height rectfill -
        # Need 4 operands for this form
        if len(ostack) < 4:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rectfill.__name__)
        # 2. TYPECHECK - Check numeric operand types (x y width height)
        for i in range(-4, 0):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, rectfill.__name__)

        x = ostack[-4].val
        y = ostack[-3].val
        w = ostack[-2].val
        h = ostack[-1].val

        # start a new Path
        path = ps.Path()
        sub_path = ps.SubPath()
        sub_path.append(ps.MoveTo(ps.Point(*_transform_point(ctxt.gstate.CTM, x, y))))
        sub_path.append(
            ps.LineTo(ps.Point(*_transform_point(ctxt.gstate.CTM, x + w, y)))
        )
        sub_path.append(
            ps.LineTo(ps.Point(*_transform_point(ctxt.gstate.CTM, x + w, y + h)))
        )
        sub_path.append(
            ps.LineTo(ps.Point(*_transform_point(ctxt.gstate.CTM, x, y + h)))
        )
        sub_path.append(ps.ClosePath())
        path.append(sub_path)

        # Create DisplayListBuilder if it doesn't exist
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        ctxt.display_list_builder.add_graphics_operation(ctxt, path)

        # Check if current color space is Pattern
        current_space = ctxt.gstate.color_space[0] if isinstance(ctxt.gstate.color_space, list) else ctxt.gstate.color_space
        if current_space == "Pattern" and getattr(ctxt.gstate, '_current_pattern', None):
            # Pattern fill - create PatternFill element
            pattern_dict = ctxt.gstate._current_pattern
            underlying_color = None
            if ctxt.gstate.color:
                underlying_space = ctxt.gstate.color_space[1] if len(ctxt.gstate.color_space) > 1 else "DeviceGray"
                underlying_color = color_space.convert_to_device_color(
                    ctxt, ctxt.gstate.color, [underlying_space])
            ctxt.display_list_builder.add_graphics_operation(
                ctxt, ps.PatternFill(pattern_dict, ps.WINDING_NON_ZERO, ctxt.gstate, underlying_color))
        else:
            if current_space == "Pattern":
                device_color = [0.0, 0.0, 0.0]
            else:
                device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
                if not device_color:
                    device_color = [0.0, 0.0, 0.0]
            ctxt.display_list_builder.add_graphics_operation(ctxt, ps.Fill(device_color, ps.WINDING_NON_ZERO))

        ostack.pop()
        ostack.pop()
        ostack.pop()
        ostack.pop()
        return

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        # second form:       numarray rectfill -
        if ostack[-1].length % 4:
            return ps_error.e(ctxt, ps_error.RANGECHECK, rectfill.__name__)

        arr = ostack[-1]
        ctm = ctxt.gstate.CTM

        if not all(
            ostack[-1].val[i].TYPE in ps.NUMERIC_TYPES
            for i in range(arr.start, arr.start + arr.length)
        ):
            return ps_error.e(ctxt, ps_error.TYPECHECK, rectfill.__name__)

        # start a new Path
        path = ps.Path()

        for i in range(arr.start, arr.start + arr.length, 4):
            sub_path = ps.SubPath()
            sub_path.append(
                ps.MoveTo(
                    ps.Point(*_transform_point(ctm, arr.val[i].val, arr.val[i + 1].val))
                )
            )
            sub_path.append(
                ps.LineTo(
                    ps.Point(
                        *_transform_point(
                            ctm, arr.val[i].val + arr.val[i + 2].val, arr.val[i + 1].val
                        )
                    )
                )
            )
            sub_path.append(
                ps.LineTo(
                    ps.Point(
                        *_transform_point(
                            ctm,
                            arr.val[i].val + arr.val[i + 2].val,
                            arr.val[i + 1].val + arr.val[i + 3].val,
                        )
                    )
                )
            )
            sub_path.append(
                ps.LineTo(
                    ps.Point(
                        *_transform_point(
                            ctm, arr.val[i].val, arr.val[i + 1].val + arr.val[i + 3].val
                        )
                    )
                )
            )
            sub_path.append(ps.ClosePath())
            path.append(sub_path)

        # Create DisplayListBuilder if it doesn't exist
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        ctxt.display_list_builder.add_graphics_operation(ctxt, path)

        # Check if current color space is Pattern
        current_space = ctxt.gstate.color_space[0] if isinstance(ctxt.gstate.color_space, list) else ctxt.gstate.color_space
        if current_space == "Pattern" and getattr(ctxt.gstate, '_current_pattern', None):
            # Pattern fill - create PatternFill element
            pattern_dict = ctxt.gstate._current_pattern
            underlying_color = None
            if ctxt.gstate.color:
                underlying_space = ctxt.gstate.color_space[1] if len(ctxt.gstate.color_space) > 1 else "DeviceGray"
                underlying_color = color_space.convert_to_device_color(
                    ctxt, ctxt.gstate.color, [underlying_space])
            ctxt.display_list_builder.add_graphics_operation(
                ctxt, ps.PatternFill(pattern_dict, ps.WINDING_NON_ZERO, ctxt.gstate, underlying_color))
        else:
            device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
            if device_color is None or len(device_color) == 0:
                device_color = [0.0, 0.0, 0.0]
            ctxt.display_list_builder.add_graphics_operation(ctxt, ps.Fill(device_color, ps.WINDING_NON_ZERO))

        ostack.pop()
        return

    # TODO - encoded number strings are not supported yet
    return ps_error.e(ctxt, ps_error.UNSUPPORTED, rectfill.__name__)


def _build_rect_path(ctm: ps.Array, x: float, y: float, w: float, h: float) -> ps.SubPath:
    """Build a single rectangle subpath transformed through CTM."""
    sub_path = ps.SubPath()
    sub_path.append(ps.MoveTo(ps.Point(*_transform_point(ctm, x, y))))
    sub_path.append(ps.LineTo(ps.Point(*_transform_point(ctm, x + w, y))))
    sub_path.append(ps.LineTo(ps.Point(*_transform_point(ctm, x + w, y + h))))
    sub_path.append(ps.LineTo(ps.Point(*_transform_point(ctm, x, y + h))))
    sub_path.append(ps.ClosePath())
    return sub_path


def rectstroke(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    x y width height **rectstroke** –
    x y width height matrix **rectstroke** –
    numarray **rectstroke** –
    numarray matrix **rectstroke** –

    PLRM Section 8.2: Strokes a path consisting of one or more rectangles
    defined by the operands. **rectstroke** neither reads nor alters the current
    path in the graphics state. Forms that include a matrix operand
    concatenate it to the CTM before stroking (affects line width and dash
    pattern, not the path geometry).

    **Errors**: **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rectstroke.__name__)

    # Detect which form we have
    # Check if top operand could be a matrix (6-element array)
    has_matrix = False
    matrix_operand = None
    if ostack[-1].TYPE in ps.ARRAY_TYPES and ostack[-1].length == 6:
        # Could be a matrix. Check if it's the matrix form by looking deeper.
        # If there are 4 numbers below it, or an array below it, it's a matrix.
        if len(ostack) >= 2:
            below = ostack[-2]
            if below.TYPE in ps.NUMERIC_TYPES or below.TYPE in ps.ARRAY_TYPES:
                has_matrix = True
                matrix_operand = ostack[-1]

    if has_matrix:
        # Pop the matrix, then process the remaining operands
        if not has_matrix:
            pass
        top_below = ostack[-2] if len(ostack) >= 2 else None
    else:
        top_below = ostack[-1]

    # Determine if 4-number form or array form
    if has_matrix:
        check_idx = -2
    else:
        check_idx = -1

    if ostack[check_idx].TYPE in ps.NUMERIC_TYPES:
        # 4-number form: x y width height [matrix] rectstroke
        needed = 4 + (1 if has_matrix else 0)
        if len(ostack) < needed:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rectstroke.__name__)
        base = check_idx - 3
        for i in range(base, check_idx + 1):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, rectstroke.__name__)

        x = ostack[base].val
        y = ostack[base + 1].val
        w = ostack[base + 2].val
        h = ostack[base + 3].val

        path = ps.Path()
        path.append(_build_rect_path(ctxt.gstate.CTM, x, y, w, h))

        _stroke_rect_path(ctxt, path, matrix_operand)

        for _ in range(needed):
            ostack.pop()
        return

    elif ostack[check_idx].TYPE in ps.ARRAY_TYPES:
        # Array form: numarray [matrix] rectstroke
        arr = ostack[check_idx]
        if arr.length % 4:
            return ps_error.e(ctxt, ps_error.RANGECHECK, rectstroke.__name__)

        if not all(
            arr.val[i].TYPE in ps.NUMERIC_TYPES
            for i in range(arr.start, arr.start + arr.length)
        ):
            return ps_error.e(ctxt, ps_error.TYPECHECK, rectstroke.__name__)

        ctm = ctxt.gstate.CTM
        path = ps.Path()
        for i in range(arr.start, arr.start + arr.length, 4):
            x = arr.val[i].val
            y = arr.val[i + 1].val
            w = arr.val[i + 2].val
            h = arr.val[i + 3].val
            path.append(_build_rect_path(ctm, x, y, w, h))

        _stroke_rect_path(ctxt, path, matrix_operand)

        needed = 1 + (1 if has_matrix else 0)
        for _ in range(needed):
            ostack.pop()
        return

    return ps_error.e(ctxt, ps_error.TYPECHECK, rectstroke.__name__)


def _stroke_rect_path(ctxt: ps.Context, path: ps.Path, matrix_operand: ps.Array | None) -> None:
    """Stroke a rectangle path, optionally concatenating a matrix for **stroke** params."""
    # Read StrokeMethod from page device (default: StrokePathFill for bitmap safety)
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val
    stroke_method = page_device.get(b'StrokeMethod', ps.Name(b'StrokePathFill'))
    use_native = stroke_method.val == b'NativeStroke'

    if use_native:
        # Native stroke — emit Stroke display list element (used by PDF device)
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        ctxt.display_list_builder.add_graphics_operation(ctxt, path)
        device_color = color_space.convert_to_device_color(
            ctxt, ctxt.gstate.color, ctxt.gstate.color_space)

        if matrix_operand is not None:
            temp_gstate = copy(ctxt.gstate)
            old_ctm_vals = [m.val for m in ctxt.gstate.CTM.val]
            m = [v.val for v in matrix_operand.val[:6]]
            o = old_ctm_vals
            new_ctm_vals = [
                m[0]*o[0] + m[1]*o[2],       m[0]*o[1] + m[1]*o[3],
                m[2]*o[0] + m[3]*o[2],       m[2]*o[1] + m[3]*o[3],
                m[4]*o[0] + m[5]*o[2] + o[4], m[4]*o[1] + m[5]*o[3] + o[5]
            ]
            new_ctm = ps.Array(ctxt.id)
            new_ctm.val = [ps.Real(v) for v in new_ctm_vals]
            new_ctm.length = 6
            temp_gstate.CTM = new_ctm
            ctxt.display_list_builder.add_graphics_operation(
                ctxt, ps.Stroke(device_color, temp_gstate))
        else:
            ctxt.display_list_builder.add_graphics_operation(
                ctxt, ps.Stroke(device_color, ctxt.gstate))
    else:
        # StrokePathFill — convert stroke to filled outline (bitmap devices)
        saved_path = ctxt.gstate.path
        saved_cp = ctxt.gstate.currentpoint

        if matrix_operand is not None:
            # Temporarily concatenate matrix to CTM for stroke params
            old_ctm = ctxt.gstate.CTM
            old_ctm_vals = [m.val for m in old_ctm.val]
            m = [v.val for v in matrix_operand.val[:6]]
            o = old_ctm_vals
            new_ctm_vals = [
                m[0]*o[0] + m[1]*o[2],       m[0]*o[1] + m[1]*o[3],
                m[2]*o[0] + m[3]*o[2],       m[2]*o[1] + m[3]*o[3],
                m[4]*o[0] + m[5]*o[2] + o[4], m[4]*o[1] + m[5]*o[3] + o[5]
            ]
            new_ctm = ps.Array(ctxt.id)
            new_ctm.val = [ps.Real(v) for v in new_ctm_vals]
            new_ctm.length = 6
            ctxt.gstate.CTM = new_ctm

        ctxt.gstate.path = path
        ctxt.gstate.currentpoint = None
        strokepath(ctxt, None)
        fill(ctxt, None)

        if matrix_operand is not None:
            ctxt.gstate.CTM = old_ctm

        # Restore original path (rectstroke must not alter current path)
        ctxt.gstate.path = saved_path
        ctxt.gstate.currentpoint = saved_cp


def stroke(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **stroke** -

    paints a line centered on the current path, with sides parallel to the path segments.
    The line’s graphical properties are defined by various parameters of the graphics
    state. Its thickness is determined by the current line width parameter (see
    **setlinewidth**) and its color by the current color (see **setcolor**). The joints between
    connected path segments and the ends of open subpaths are painted with the current
    line join (see **setlinejoin**) and the current line cap (see **setlinecap**), respectively.
    The line is either solid or broken according to the dash pattern established by
    **setdash**. Uniform **stroke** width can be ensured by enabling automatic **stroke** adjustment
    (see **setstrokeadjust**). All of these graphics state parameters are consulted
    at the time **stroke** is executed; their values during the time the path is being
    constructed are irrelevant.

    If a subpath is degenerate (consists of a single-point closed path or of two or more
    points at the same coordinates), **stroke** paints it only if round line caps have been
    specified, producing a filled circle centered at the single point. If butt or projecting
    square line caps have been specified, **stroke** produces no output, because the
    orientation of the caps would be indeterminate. A subpath consisting of a singlepoint
    open path produces no output.

    After painting the current path, **stroke** clears it with an implicit **newpath** operation.
    To preserve the current path across a **stroke** operation, use the sequence

        **gsave**
            **fill**
        **grestore**

    **Errors**:     **limitcheck**
    **See Also**:   **setlinewidth**, **setlinejoin**, **setlinecap**, **setmiterlimit**, **setdash**,
                **setstrokeadjust**, **ustroke**, **fill**
    """

    if ctxt.gstate.path:
        # Read StrokeMethod from page device (default: StrokePathFill for bitmap safety)
        page_device = getattr(ctxt.gstate, 'page_device', {})
        if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
            page_device = page_device.val
        stroke_method = page_device.get(b'StrokeMethod', ps.Name(b'StrokePathFill'))
        use_native = stroke_method.val == b'NativeStroke'

        if use_native:
            # Native stroke — emit Stroke display list element (used by PDF device)
            if not hasattr(ctxt, 'display_list_builder'):
                ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)
            ctxt.display_list_builder.add_graphics_operation(ctxt, ctxt.gstate.path)
            device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
            ctxt.display_list_builder.add_graphics_operation(ctxt, ps.Stroke(device_color, ctxt.gstate))
            ctxt.gstate.path = ps.Path()
            ctxt.gstate.currentpoint = None
        else:
            # StrokePathFill — convert stroke to filled outline (bitmap devices)
            strokepath(ctxt, ostack)
            fill(ctxt, ostack)


def shfill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    dict **shfill** -

    Fills the area defined by a shading dictionary with a gradient **fill**.
    This is a LanguageLevel 3 operator.

    The shading dictionary specifies:
    - ShadingType: Type of shading (1-7)
    - ColorSpace: Color space for the gradient
    - Background: Optional background color
    - BBox: Optional bounding box
    - AntiAlias: Optional anti-aliasing hint

    Shading types:
    1 = Function-based shading
    2 = Axial shading (linear gradient)
    3 = Radial shading (circular gradient)
    4 = Free-form Gouraud-shaded triangle mesh
    5 = Lattice-form Gouraud-shaded triangle mesh
    6 = Coons patch mesh
    7 = Tensor-product patch mesh

    PLRM Section 4.9.3, Page 689
    **Errors**: **rangecheck**, **typecheck**, **undefinedresult**
    """
    # 1. Stack validation
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, shfill.__name__)

    # 2. Type validation - must be dictionary
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, shfill.__name__)

    shading_dict = ostack[-1]
    d = shading_dict.val

    # 3. Validate required ShadingType entry
    if b"ShadingType" not in d:
        return ps_error.e(ctxt, ps_error.RANGECHECK, shfill.__name__)

    shading_type_obj = d[b"ShadingType"]
    if shading_type_obj.TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, shfill.__name__)

    shading_type = int(shading_type_obj.val)
    if shading_type < 1 or shading_type > 7:
        return ps_error.e(ctxt, ps_error.RANGECHECK, shfill.__name__)

    # Unsupported shading types - silently ignore
    if shading_type not in (1, 2, 3, 4, 5, 6, 7):
        ostack.pop()
        return

    # 4. Get ColorSpace (required)
    if b"ColorSpace" not in d:
        return ps_error.e(ctxt, ps_error.RANGECHECK, shfill.__name__)

    # 5. Parse common entries
    cs_obj = d[b"ColorSpace"]
    shading_cs, cie_dict, cs_array = _resolve_color_space(cs_obj)

    # Domain defaults to [0, 1]
    domain = _get_shading_float_array(d, b"Domain", [0.0, 1.0])

    # Extend defaults to [false, false]
    extend_start = False
    extend_end = False
    if b"Extend" in d:
        ext_arr = d[b"Extend"]
        if hasattr(ext_arr, 'TYPE') and ext_arr.TYPE in ps.ARRAY_TYPES:
            extend_start = bool(ext_arr.val[ext_arr.start].val)
            extend_end = bool(ext_arr.val[ext_arr.start + 1].val)

    # BBox (optional, in user space)
    bbox = _get_shading_float_array(d, b"BBox", None)

    # Function (optional for some types, required for types 2 and 3)
    func_obj = d.get(b"Function")

    # 6. Extract CTM
    ctm_vals = ctxt.gstate.CTM.val
    ctm = (ctm_vals[0].val, ctm_vals[1].val, ctm_vals[2].val,
           ctm_vals[3].val, ctm_vals[4].val, ctm_vals[5].val)

    # 7. Build display list element
    if not hasattr(ctxt, 'display_list_builder'):
        ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

    if shading_type == 1:
        # Type 1: Function-based shading (2 inputs → n outputs)
        if func_obj is None:
            ostack.pop()
            return
        element = _build_function_shading(d, func_obj, shading_cs, ctm, bbox, ctxt, cie_dict, cs_array)
        if element is not None:
            ctxt.display_list_builder.add_graphics_operation(ctxt, element)
        ostack.pop()
        return

    if shading_type in (4, 5, 6, 7):
        element = _build_mesh_shading(d, shading_type, shading_cs, ctm, bbox, ctxt, func_obj, cie_dict, cs_array)
        if element is not None:
            ctxt.display_list_builder.add_graphics_operation(ctxt, element)
        ostack.pop()
        return

    # Types 2 and 3 require Function and Coords
    if func_obj is None:
        ostack.pop()
        return

    # Sample function to build color stops
    num_samples = 64
    color_stops = _sample_shading_function(func_obj, domain, num_samples, shading_cs, ctxt, cie_dict, cs_array)

    coords = _get_shading_float_array(d, b"Coords", None)
    if coords is None:
        ostack.pop()
        return

    if shading_type == 2:
        # Axial: Coords = [x0 y0 x1 y1]
        if len(coords) < 4:
            ostack.pop()
            return
        element = ps.AxialShadingFill(
            coords[0], coords[1], coords[2], coords[3],
            color_stops, extend_start, extend_end, ctm, bbox
        )
    else:
        # Radial: Coords = [x0 y0 r0 x1 y1 r1]
        if len(coords) < 6:
            ostack.pop()
            return
        element = ps.RadialShadingFill(
            coords[0], coords[1], coords[2],
            coords[3], coords[4], coords[5],
            color_stops, extend_start, extend_end, ctm, bbox
        )

    ctxt.display_list_builder.add_graphics_operation(ctxt, element)
    ostack.pop()


def _resolve_color_space(cs_obj: ps.PSObject) -> tuple[str, dict | None, ps.PSObject]:
    """Extract color space name string and optional CIE dict from a PostScript color space object.

    Returns:
        (space_name, cie_dict, cs_obj) tuple. cie_dict is None for device spaces.
        cs_obj is the original PS color space object (needed for DeviceN/Separation tint transforms).
    """
    if hasattr(cs_obj, 'TYPE'):
        if cs_obj.TYPE == ps.T_NAME:
            val = cs_obj.val
            name = val.decode('ascii') if isinstance(val, bytes) else val
            return name, None, cs_obj
        if cs_obj.TYPE in ps.ARRAY_TYPES:
            first = cs_obj.val[cs_obj.start]
            val = first.val
            name = val.decode('ascii') if isinstance(val, bytes) else val
            # For CIE-based spaces, the second element is the CIE dictionary
            cie_dict = None
            if name in ("CIEBasedABC", "CIEBasedA", "CIEBasedDEF", "CIEBasedDEFG") and cs_obj.length >= 2:
                dict_obj = cs_obj.val[cs_obj.start + 1]
                if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict):
                    cie_dict = dict_obj.val
            # ICCBased: check for Tier 2 ICC profile, else resolve to device space
            if name == "ICCBased":
                cs_list = [name]
                for i in range(cs_obj.start + 1, cs_obj.start + cs_obj.length):
                    cs_list.append(cs_obj.val[i])
                stream_obj = cs_list[1] if len(cs_list) >= 2 else None
                profile_hash = icc_profile.get_profile_hash(stream_obj) if stream_obj else None
                if profile_hash is not None:
                    return "ICCBased", None, cs_obj  # Keep ICCBased for Tier 2
                device_name = color_space.ColorSpaceEngine.resolve_iccbased_space(cs_list)
                return device_name, None, cs_obj
            return name, cie_dict, cs_obj
    return "DeviceRGB", None, cs_obj


def _get_shading_float_array(d: dict, key: bytes, default: list[float] | None) -> list[float] | None:
    """Get a list of floats from a PostScript array entry in a dict."""
    if key not in d:
        return default
    obj = d[key]
    if hasattr(obj, 'TYPE') and obj.TYPE in ps.ARRAY_TYPES:
        return [float(obj.val[i].val) for i in range(obj.start, obj.start + obj.length)]
    return default


def _sample_shading_function(func_obj: ps.PSObject, domain: list[float], num_samples: int, shading_cs: str, ctxt: ps.Context, cie_dict: dict | None = None, cs_array: ps.PSObject | None = None) -> list[tuple[float, tuple[float, float, float]]]:
    """
    Sample a shading Function across domain to produce device RGB color stops.

    Returns list of (t_normalized, (r, g, b)) where t_normalized is in [0, 1].
    """
    d_min, d_max = domain[0], domain[1]
    color_stops = []

    for i in range(num_samples + 1):
        t_norm = i / num_samples
        t = d_min + t_norm * (d_max - d_min)

        # Evaluate function
        try:
            result = ps_function.evaluate_function(func_obj, [t])
        except Exception:
            result = [0.0, 0.0, 0.0]

        # Convert from shading color space to device RGB
        rgb = _color_to_rgb(result, shading_cs, cie_dict, cs_array, ctxt)
        color_stops.append((t_norm, rgb))

    return color_stops


def _build_function_shading(d: dict, func_obj: ps.PSObject, shading_cs: str, ctm: tuple[float, ...], bbox: list[float] | None, ctxt: ps.Context, cie_dict: dict | None = None, cs_array: ps.PSObject | None = None) -> ps.FunctionShadingFill | None:
    """
    Build a FunctionShadingFill display list element for Type 1 shading.

    Rasterizes the 2-input function over its Domain onto a pixel grid,
    converts each output to RGB, and packs into an ARGB32 buffer.
    """
    # Domain defaults to [0 1 0 1] per PLRM
    domain = _get_shading_float_array(d, b"Domain", [0.0, 1.0, 0.0, 1.0])
    if len(domain) < 4:
        domain = [0.0, 1.0, 0.0, 1.0]

    x_min, x_max, y_min, y_max = domain[0], domain[1], domain[2], domain[3]

    # Matrix (optional) maps from domain to user space; default identity
    matrix = _get_shading_float_array(d, b"Matrix", [1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
    if len(matrix) < 6:
        matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]

    # Raster resolution — 256×256 gives good quality without being huge
    raster_w = 256
    raster_h = 256

    dx = (x_max - x_min) / raster_w if raster_w > 0 else 0
    dy = (y_max - y_min) / raster_h if raster_h > 0 else 0

    # Pre-allocate ARGB32 buffer (4 bytes per pixel: B, G, R, A on little-endian)
    pixel_data = bytearray(raster_w * raster_h * 4)

    for row in range(raster_h):
        y_val = y_min + (row + 0.5) * dy
        for col in range(raster_w):
            x_val = x_min + (col + 0.5) * dx

            try:
                result = ps_function.evaluate_function(func_obj, [x_val, y_val])
            except Exception:
                result = [0.0, 0.0, 0.0]

            r, g, b = _color_to_rgb(result, shading_cs, cie_dict, cs_array, ctxt)
            offset = (row * raster_w + col) * 4
            pixel_data[offset] = max(0, min(255, int(b * 255 + 0.5)))      # B
            pixel_data[offset + 1] = max(0, min(255, int(g * 255 + 0.5)))  # G
            pixel_data[offset + 2] = max(0, min(255, int(r * 255 + 0.5)))  # R
            pixel_data[offset + 3] = 255                                    # A

    # Build the mapping matrix: raster pixel coords → domain coords → user space
    # First: pixel → domain: x = x_min + px * dx, y = y_min + py * dy
    # As a matrix: [dx 0 0 dy x_min y_min]
    # Then compose with the shading Matrix to get to user space.
    # The combined matrix maps pixel coords to user space.
    p2d = [dx, 0.0, 0.0, dy, x_min, y_min]
    # Compose: p2d × matrix (pixel→domain→user)
    a1, b1, c1, d1, tx1, ty1 = p2d
    a2, b2, c2, d2, tx2, ty2 = matrix
    combined = (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        tx1 * a2 + ty1 * c2 + tx2,
        tx1 * b2 + ty1 * d2 + ty2,
    )

    return ps.FunctionShadingFill(pixel_data, raster_w, raster_h, combined, ctm, bbox)


def _build_mesh_shading(d: dict, shading_type: int, shading_cs: str, ctm: tuple[float, ...], bbox: list[float] | None, ctxt: ps.Context, func_obj: ps.PSObject | None = None, cie_dict: dict | None = None, cs_array: ps.PSObject | None = None) -> ps.MeshShadingFill | ps.PatchShadingFill | None:
    """Build a MeshShadingFill or PatchShadingFill for Types 4-7."""
    # Extract common parameters
    bpc = _get_shading_int(d, b"BitsPerCoordinate", 8)
    bpco = _get_shading_int(d, b"BitsPerComponent", 8)
    bpfl = _get_shading_int(d, b"BitsPerFlag", 8)
    decode = _get_shading_float_array(d, b"Decode", [0, 1, 0, 1, 0, 1, 0, 1])

    # Determine number of color components from color space
    n_comps = _get_color_space_ncomps(shading_cs, cs_array)

    # Get data source — may be binary bytes or a PS array of numbers
    ds_raw = d.get(b"DataSource")
    if ds_raw is None:
        return None

    # Check if DataSource is an array (list of numeric values)
    array_data = None
    if hasattr(ds_raw, 'val') and isinstance(ds_raw.val, list):
        array_data = [float(item.val) if hasattr(item, 'val') else float(item) for item in ds_raw.val]
    elif isinstance(ds_raw, list):
        array_data = [float(item.val) if hasattr(item, 'val') else float(item) for item in ds_raw]

    if array_data is not None:
        return _build_mesh_from_array(array_data, shading_type, d, shading_cs, ctm, bbox, cie_dict, n_comps, cs_array, ctxt)

    data = _get_data_source(d)
    if data is None:
        return None

    func = func_obj  # optional parametric function

    if shading_type == 4:
        triangles = mesh_shading.parse_type4_mesh(data, bpc, bpco, bpfl, decode, n_comps, func)
        return _triangles_to_element(triangles, shading_cs, ctm, bbox, cie_dict, cs_array, ctxt)
    elif shading_type == 5:
        vpr = _get_shading_int(d, b"VerticesPerRow", 2)
        triangles = mesh_shading.parse_type5_mesh(data, bpc, bpco, decode, n_comps, vpr, func)
        return _triangles_to_element(triangles, shading_cs, ctm, bbox, cie_dict, cs_array, ctxt)
    elif shading_type == 6:
        patches = mesh_shading.parse_type6_patches(data, bpc, bpco, bpfl, decode, n_comps, func)
        return _patches_to_element(patches, shading_cs, ctm, bbox, cie_dict, cs_array, ctxt)
    elif shading_type == 7:
        patches = mesh_shading.parse_type7_patches(data, bpc, bpco, bpfl, decode, n_comps, func)
        return _patches_to_element(patches, shading_cs, ctm, bbox, cie_dict, cs_array, ctxt)

    return None


def _build_mesh_from_array(values: list[float], shading_type: int, d: dict, shading_cs: str, ctm: tuple[float, ...], bbox: list[float] | None, cie_dict: dict | None, n_comps: int, cs_array: ps.PSObject | None = None, ctxt: ps.Context | None = None) -> ps.MeshShadingFill | ps.PatchShadingFill | None:
    """Build mesh shading from an array-based DataSource (flat list of numbers).

    Array format varies by type:
      Type 4: [flag x y c0 c1 ... flag x y c0 c1 ...]
      Type 5: [x y c0 c1 ... x y c0 c1 ...] (no flags)
      Type 6: [flag p0x p0y p1x p1y ... (12 points) c0_0 c0_1 ... (4 colors)]
      Type 7: [flag p0x p0y ... (16 points) c0_0 ... (4 colors)]

    Optimized with color caching to avoid redundant conversions.
    """
    idx = 0

    # Cache color conversions by color tuple (important for DeviceN/Separation)
    color_rgb_cache = {}

    def get_rgb(color):
        color_key = tuple(color)
        if color_key in color_rgb_cache:
            return color_rgb_cache[color_key]
        rgb = _color_to_rgb(color, shading_cs, cie_dict, cs_array, ctxt)
        color_rgb_cache[color_key] = rgb
        return rgb

    if shading_type == 4:
        # Type 4: free-form Gouraud triangles with edge flags
        stride = 1 + 2 + n_comps  # flag + x,y + color components
        vertices = []
        triangles_out = []
        while idx + stride <= len(values):
            flag = int(values[idx])
            x, y = values[idx + 1], values[idx + 2]
            color = values[idx + 3:idx + 3 + n_comps]
            rgb = get_rgb(color)
            idx += stride

            if flag == 0:
                vertices = [((x, y), rgb)]
                for _ in range(2):
                    if idx + stride > len(values):
                        break
                    f2 = int(values[idx])
                    x2, y2 = values[idx + 1], values[idx + 2]
                    c2 = values[idx + 3:idx + 3 + n_comps]
                    rgb2 = get_rgb(c2)
                    idx += stride
                    vertices.append(((x2, y2), rgb2))
                if len(vertices) >= 3:
                    triangles_out.append(tuple(vertices[-3:]))
            elif flag == 1 and len(vertices) >= 2:
                vertices.append(((x, y), rgb))
                triangles_out.append(tuple(vertices[-3:]))
            elif flag == 2 and len(vertices) >= 3:
                prev_a = vertices[-3]
                prev_c = vertices[-1]
                vertices.append(((x, y), rgb))
                triangles_out.append((prev_a, prev_c, vertices[-1]))

        if not triangles_out:
            return None
        return ps.MeshShadingFill(triangles_out, ctm, bbox)

    elif shading_type == 5:
        # Type 5: lattice Gouraud (no flags)
        vpr = _get_shading_int(d, b"VerticesPerRow", 2)
        stride = 2 + n_comps  # x,y + color
        all_verts = []
        while idx + stride <= len(values):
            x, y = values[idx], values[idx + 1]
            color = values[idx + 2:idx + 2 + n_comps]
            rgb = get_rgb(color)
            all_verts.append(((x, y), rgb))
            idx += stride

        num_rows = len(all_verts) // vpr if vpr > 0 else 0
        triangles_out = []
        for row in range(num_rows - 1):
            for col in range(vpr - 1):
                i = row * vpr + col
                v00, v10, v01, v11 = all_verts[i], all_verts[i + 1], all_verts[i + vpr], all_verts[i + vpr + 1]
                triangles_out.append((v00, v10, v01))
                triangles_out.append((v10, v11, v01))

        if not triangles_out:
            return None
        return ps.MeshShadingFill(triangles_out, ctm, bbox)

    elif shading_type == 6:
        # Type 6: Coons patches — flag + 12 points + 4 colors
        n_pts = 12
        patch_stride = 1 + n_pts * 2 + 4 * n_comps
        patches_out = []
        while idx + patch_stride <= len(values):
            idx += 1  # skip flag
            points = []
            for _ in range(n_pts):
                points.append((values[idx], values[idx + 1]))
                idx += 2
            colors = []
            for _ in range(4):
                c = values[idx:idx + n_comps]
                colors.append(get_rgb(c))
                idx += n_comps
            patches_out.append((points, colors))

        if not patches_out:
            return None
        return ps.PatchShadingFill(patches_out, ctm, bbox)

    elif shading_type == 7:
        # Type 7: tensor-product patches — flag + 16 points + 4 colors
        n_pts = 16
        patch_stride = 1 + n_pts * 2 + 4 * n_comps
        patches_out = []
        while idx + patch_stride <= len(values):
            idx += 1  # skip flag
            points = []
            for _ in range(n_pts):
                points.append((values[idx], values[idx + 1]))
                idx += 2
            colors = []
            for _ in range(4):
                c = values[idx:idx + n_comps]
                colors.append(get_rgb(c))
                idx += n_comps
            patches_out.append((points, colors))

        if not patches_out:
            return None
        return ps.PatchShadingFill(patches_out, ctm, bbox)

    return None


def _triangles_to_element(triangles: list, shading_cs: str, ctm: tuple[float, ...], bbox: list[float] | None, cie_dict: dict | None, cs_array: ps.PSObject | None = None, ctxt: ps.Context | None = None) -> ps.MeshShadingFill | None:
    """Convert parsed mesh triangles to a MeshShadingFill display list element.

    Optimized with color caching to avoid redundant conversions (especially important
    for DeviceN/Separation which execute PostScript tint transforms per conversion).
    """
    if not triangles:
        return None

    # Cache color conversions by vertex identity (shared vertices in mesh)
    # and by color tuple (same color values across different vertices)
    vertex_rgb_cache = {}  # id(vertex) -> rgb
    color_rgb_cache = {}   # tuple(color) -> rgb

    def get_rgb(v):
        # First check vertex identity cache (handles shared MeshVertex objects)
        vid = id(v)
        if vid in vertex_rgb_cache:
            return vertex_rgb_cache[vid]

        # Then check color value cache (handles duplicate color values)
        color_key = tuple(v.color)
        if color_key in color_rgb_cache:
            rgb = color_rgb_cache[color_key]
        else:
            rgb = _color_to_rgb(v.color, shading_cs, cie_dict, cs_array, ctxt)
            color_rgb_cache[color_key] = rgb

        vertex_rgb_cache[vid] = rgb
        return rgb

    converted = []
    for tri in triangles:
        verts = []
        for v in (tri.v0, tri.v1, tri.v2):
            rgb = get_rgb(v)
            verts.append(((v.x, v.y), rgb))
        converted.append(tuple(verts))
    return ps.MeshShadingFill(converted, ctm, bbox)


def _patches_to_element(patches: list, shading_cs: str, ctm: tuple[float, ...], bbox: list[float] | None, cie_dict: dict | None, cs_array: ps.PSObject | None = None, ctxt: ps.Context | None = None) -> ps.PatchShadingFill | None:
    """Convert parsed Coons/tensor patches to a PatchShadingFill display list element.

    Optimized with color caching to avoid redundant conversions.
    """
    if not patches:
        return None

    # Cache color conversions by color tuple
    color_rgb_cache = {}

    def get_rgb(color):
        color_key = tuple(color)
        if color_key in color_rgb_cache:
            return color_rgb_cache[color_key]
        rgb = _color_to_rgb(color, shading_cs, cie_dict, cs_array, ctxt)
        color_rgb_cache[color_key] = rgb
        return rgb

    converted = []
    for patch in patches:
        colors = [get_rgb(c) for c in patch.colors]
        converted.append((patch.points, colors))
    return ps.PatchShadingFill(converted, ctm, bbox)


def _get_shading_int(d: dict, key: bytes, default: int) -> int:
    """Get an integer value from a shading dictionary."""
    if key not in d:
        return default
    obj = d[key]
    if hasattr(obj, 'val'):
        return int(obj.val)
    return int(obj) if obj is not None else default


def _get_color_space_ncomps(space_name: str, cs_array: ps.PSObject | None = None) -> int:
    """Get the number of color components for a named color space."""
    counts = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4,
              "CIEBasedABC": 3, "CIEBasedA": 1, "CIEBasedDEF": 3,
              "CIEBasedDEFG": 4, "Separation": 1}
    if space_name in counts:
        return counts[space_name]
    if space_name == "ICCBased" and cs_array is not None:
        cs_list = _iccbased_cs_list(cs_array)
        try:
            return color_space.ColorSpaceEngine.get_component_count(cs_list)
        except ValueError:
            return 3
    if space_name == "DeviceN" and cs_array is not None:
        # DeviceN: n_comps = number of colorant names (element 1 of CS array)
        if hasattr(cs_array, 'TYPE') and cs_array.TYPE in ps.ARRAY_TYPES:
            names_obj = cs_array.val[cs_array.start + 1]
            if hasattr(names_obj, 'length'):
                return names_obj.length
    return 3


def _get_data_source(d: dict) -> bytes | None:
    """Extract binary data from a shading dictionary's DataSource entry."""
    if b"DataSource" not in d:
        return None
    ds = d[b"DataSource"]
    if hasattr(ds, 'val'):
        if isinstance(ds.val, (bytes, bytearray)):
            return ds.val
        if isinstance(ds.val, str):
            return ds.val.encode('latin-1')
    if isinstance(ds, (bytes, bytearray)):
        return ds
    return None


def _tint_transform_to_rgb(components: list[float], cs_array: ps.PSObject, ctxt: ps.Context) -> tuple[float, float, float]:
    """Execute a DeviceN/Separation tint **transform** and convert result to RGB.

    Args:
        components: list of float tint values
        cs_array: PS color space array [/DeviceN names altSpace tintTransform]
                  or [/Separation name altSpace tintTransform]
        ctxt: PS context for executing the tint **transform** procedure
    """
    from . import control as ps_control

    # Extract alternative space and tint transform from CS array
    alt_space_obj = cs_array.val[cs_array.start + 2]
    tint_transform = cs_array.val[cs_array.start + 3]

    # Determine alternative space name
    if hasattr(alt_space_obj, 'TYPE') and alt_space_obj.TYPE == ps.T_NAME:
        alt_name = alt_space_obj.val.decode('ascii') if isinstance(alt_space_obj.val, bytes) else alt_space_obj.val
    else:
        alt_name = "DeviceRGB"

    alt_ncomps = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}.get(alt_name, 3)

    # Push tint values and execute tint transform
    for tint in components:
        ctxt.o_stack.append(ps.Real(max(0.0, min(1.0, float(tint)))))

    ctxt.e_stack.append(ps.HardReturn())
    ctxt.e_stack.append(copy(tint_transform))
    ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

    # Pop results from operand stack
    result = []
    for _ in range(alt_ncomps):
        if ctxt.o_stack:
            val = ctxt.o_stack.pop()
            if hasattr(val, 'val'):
                result.insert(0, max(0.0, min(1.0, float(val.val))))
            else:
                result.insert(0, 0.0)
        else:
            result.insert(0, 0.0)

    # Convert alternative space color to RGB
    return _color_to_rgb(result, alt_name)


def _iccbased_cs_list(cs_array: ps.PSObject | list) -> list:
    """Build a Python list from a PS ICCBased color space array object."""
    if hasattr(cs_array, 'TYPE') and cs_array.TYPE in ps.ARRAY_TYPES:
        return ["ICCBased"] + [cs_array.val[i] for i in range(cs_array.start + 1, cs_array.start + cs_array.length)]
    if isinstance(cs_array, list):
        return cs_array
    return ["ICCBased"]


def _color_to_rgb(components: list[float], space_name: str, cie_dict: dict | None = None, cs_array: ps.PSObject | None = None, ctxt: ps.Context | None = None) -> tuple[float, float, float]:
    """Convert color components from a named color space to (r, g, b) tuple.

    Args:
        components: list of float color component values
        space_name: color space name string (e.g. "**DeviceRGB**", "CIEBasedABC")
        cie_dict: optional CIE dictionary (Python dict) for CIE-based spaces
        cs_array: optional PS color space array object (for DeviceN/Separation tint transforms)
        ctxt: optional PS context (needed to execute tint transforms)
    """
    engine = color_space.ColorSpaceEngine

    # Handle DeviceN/Separation by executing tint transform first
    if space_name in ("DeviceN", "Separation") and cs_array is not None and ctxt is not None:
        return _tint_transform_to_rgb(components, cs_array, ctxt)

    if space_name == "DeviceRGB":
        r = max(0.0, min(1.0, components[0] if len(components) > 0 else 0.0))
        g = max(0.0, min(1.0, components[1] if len(components) > 1 else 0.0))
        b = max(0.0, min(1.0, components[2] if len(components) > 2 else 0.0))
        return (r, g, b)
    elif space_name == "DeviceGray":
        gray = max(0.0, min(1.0, components[0] if components else 0.0))
        return (gray, gray, gray)
    elif space_name == "DeviceCMYK":
        c = components[0] if len(components) > 0 else 0.0
        m = components[1] if len(components) > 1 else 0.0
        y = components[2] if len(components) > 2 else 0.0
        k = components[3] if len(components) > 3 else 0.0
        icc_rgb = icc_default.convert_cmyk_color(c, m, y, k)
        if icc_rgb is not None:
            return icc_rgb
        return engine.cmyk_to_rgb(c, m, y, k)
    elif space_name in ("CIEBasedABC", "CIEBasedDEF"):
        d = cie_dict if cie_dict else {}
        return engine.cie_abc_to_rgb(components, d)
    elif space_name == "CIEBasedA":
        d = cie_dict if cie_dict else {}
        val = components[0] if components else 0.0
        return engine.cie_a_to_rgb(val, d)
    elif space_name == "CIEBasedDEFG":
        d = cie_dict if cie_dict else {}
        return engine.cie_defg_to_rgb(components, d)
    elif space_name == "ICCBased":
        # Try ICC Tier 2
        if cs_array is not None:
            cs_list = _iccbased_cs_list(cs_array)
            stream_obj = cs_list[1] if len(cs_list) >= 2 else None
            profile_hash = icc_profile.get_profile_hash(stream_obj) if stream_obj else None
            n = len(components)
            if profile_hash is not None:
                rgb = icc_profile.icc_convert_color(profile_hash, n, components)
                if rgb is not None:
                    return rgb
            # Tier 1 fallback
            device_space = engine.resolve_iccbased_space(cs_list)
        else:
            n = len(components)
            device_space = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(n, "DeviceRGB")
        return _color_to_rgb(components, device_space)
    else:
        # Fallback: treat as RGB or return gray
        if len(components) >= 3:
            return (max(0.0, min(1.0, components[0])),
                    max(0.0, min(1.0, components[1])),
                    max(0.0, min(1.0, components[2])))
        elif len(components) >= 1:
            g = max(0.0, min(1.0, components[0]))
            return (g, g, g)
        return (0.0, 0.0, 0.0)
