# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy

from ..core import error as ps_error
from ..core import types as ps
from ..core.display_list_builder import DisplayListBuilder
from .matrix import _transform_point


def clip(ctxt, ostack):
    """
    – **clip** –

    intersects the area inside the current clipping path with the area inside the current
    path to produce a new, smaller clipping path. The nonzero winding number rule
    (see "Nonzero Winding Number Rule" on page 195) is used to determine what
    points lie inside the current path, while the inside of the current clipping path is
    determined by whatever rule was used at the time the path was created.

    PLRM Section 8.2, Page 412
    **Errors**: **limitcheck**
    """
    # Get current path (already in device coordinates per PLRM)
    current_path = ctxt.gstate.path

    # Simple approach: set clipping path to current path
    # PostScript spec doesn't define how intersection is constructed
    if current_path:
        # Copy current path to clipping path
        ctxt.gstate.update_clipping_path(copy.deepcopy(current_path), ps.WINDING_NON_ZERO)

        # Add ClipElement immediately for clip
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_NON_ZERO)
        ctxt.display_list.append(clip_element)
        ctxt.display_list_builder.current_clip_version = ctxt.gstate.clip_path_version

    # Note: Unlike fill and stroke, clip does not implicitly perform newpath
    # The current path remains unchanged


def eoclip(ctxt, ostack):
    """
    – **eoclip** –

    intersects the inside of the current clipping path with the inside of the
    current path to produce a new, smaller current clipping path. The inside of the
    current path is determined by the even-odd rule, while the inside of the current
    clipping path is determined by whatever rule was used at the time that path was
    created.

    PLRM Section 8.2, Page 443
    **Errors**: **limitcheck**
    """
    # Get current path (already in device coordinates per PLRM)
    current_path = ctxt.gstate.path

    # Simple approach: set clipping path to current path with even-odd rule
    # PostScript spec doesn't define how intersection is constructed
    if current_path:
        # Copy current path to clipping path
        ctxt.gstate.update_clipping_path(copy.deepcopy(current_path), ps.WINDING_EVEN_ODD)

        # Add ClipElement immediately for eoclip
        if not hasattr(ctxt, 'display_list_builder'):
            ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

        clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_EVEN_ODD)
        ctxt.display_list.append(clip_element)
        ctxt.display_list_builder.current_clip_version = ctxt.gstate.clip_path_version

    # Note: Unlike fill and stroke, eoclip does not implicitly perform newpath
    # The current path remains unchanged


def clippath(ctxt, ostack):
    """
    – **clippath** –

    sets the current path to the current clipping path. This operator is useful for determining
    the exact extent of the imaging area on the current output device.

    If the current clipping path was set with **clip** or **eoclip**, the path set by **clippath** is
    generally suitable only for filling or clipping. It is not suitable for stroking, because
    it may contain interior segments or disconnected subpaths produced by the
    clipping process.

    PLRM Section 8.2, Page 418
    **Errors**: none
    """

    # Set current path to copy of clipping path
    ctxt.gstate.path = copy.deepcopy(ctxt.gstate.clip_path)

    # Update current point if path is not empty
    if ctxt.gstate.path and len(ctxt.gstate.path) > 0:
        # Find the last point in the path
        last_subpath = ctxt.gstate.path[-1]
        if last_subpath and len(last_subpath) > 0:
            # set the current point
            i = -1

            while not isinstance(last_subpath[i], ps.MoveTo):
                i -= 1
            ctxt.gstate.currentpoint = copy.copy(last_subpath[i].p)
    else:
        ctxt.gstate.currentpoint = None


def clipsave(ctxt, ostack):
    """
    – **clipsave** –

    saves the current clipping path on the stack.

    PLRM Section 8.2, Page 417
    **Errors**: **limitcheck**
    """
    # Check for stack overflow
    if len(ctxt.gstate.clip_path_stack) >= ps.CP_STACK_MAX:
        return ps_error.e(ctxt, ps_error.LIMITCHECK, clipsave.__name__)

    # Save current clipping path on stack
    ctxt.gstate.clip_path_stack.append(copy.deepcopy(ctxt.gstate.clip_path))


def cliprestore(ctxt, ostack):
    """
    – **cliprestore** –

    restores the clipping path from the stack.

    PLRM Section 8.2, Page 417
    **Errors**: **limitcheck**
    """
    # Check if stack is empty
    if len(ctxt.gstate.clip_path_stack) == 0:
        return ps_error.e(ctxt, ps_error.LIMITCHECK, cliprestore.__name__)

    # Store current clipping path version before restore
    old_clip_version = ctxt.gstate.clip_path_version

    # Restore clipping path from stack
    restored_clip_path = ctxt.gstate.clip_path_stack.pop()
    ctxt.gstate.update_clipping_path(restored_clip_path, ps.WINDING_NON_ZERO)  # Default rule

    # Check if clipping path changed after restore
    if ctxt.gstate.clip_path_version != old_clip_version:
        # Clipping path changed - we need to reset Cairo clipping first, then apply new path
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


def initclip(ctxt, ostack):
    """
    - **initclip** -


    sets the current clipping path in the graphics state to the default clipping path for
    the current output device. This path usually corresponds to the boundary of the
    maximum imageable area on the current device. For a page device, its dimensions
    are those established by the **setpagedevice** operator. For a display device, the clipping
    region established by **initclip** is not well defined.

    There are few situations in which a PostScript program should invoke **initclip** explicitly.
    A page description that invokes **initclip** usually produces incorrect results
    if it is embedded within another, composite page.

    **Errors**:     none
    **See Also**:   **clip**, **eoclip**, **rectclip**, **clippath**, **initgraphics**
    """

    pdm = ctxt.gstate.page_device

    # Null device: clipping path is degenerate (single point at origin, PLRM p.459)
    if b".NullDevice" in pdm:
        clip_path = ps.Path()
        subpath = ps.SubPath()
        subpath.append(ps.MoveTo(ps.Point(0.0, 0.0)))
        clip_path.append(subpath)
        ctxt.gstate.update_clipping_path(clip_path, ps.WINDING_EVEN_ODD)
        ctxt.gstate.clip_currentpoint = ps.Point(0, 0)
        return

    width = int(pdm[b"PageSize"].get(ps.Int(0))[1].val)
    height = int(pdm[b"PageSize"].get(ps.Int(1))[1].val)

    # Create page boundary path
    page_clip_path = ps.Path()
    subpath = ps.SubPath()
    x, y = _transform_point(ctxt.gstate.CTM, 0, 0)
    subpath.append(ps.MoveTo(ps.Point(x, y)))
    x, y = _transform_point(ctxt.gstate.CTM, 0, height)
    subpath.append(ps.LineTo(ps.Point(x, y)))
    x, y = _transform_point(ctxt.gstate.CTM, width, height)
    subpath.append(ps.LineTo(ps.Point(x, y)))
    x, y = _transform_point(ctxt.gstate.CTM, width, 0)
    subpath.append(ps.LineTo(ps.Point(x, y)))
    subpath.append(ps.ClosePath())
    page_clip_path.append(subpath)

    # Update clipping path properly to trigger version change
    ctxt.gstate.update_clipping_path(page_clip_path, ps.WINDING_EVEN_ODD)
    ctxt.gstate.clip_currentpoint = ps.Point(0, 0)

    # Add ClipElement immediately for initclip
    if not hasattr(ctxt, 'display_list_builder'):
        ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

    clip_element = ps.ClipElement(ctxt.gstate, ps.WINDING_EVEN_ODD, is_initclip=True)
    ctxt.display_list.append(clip_element)
    ctxt.display_list_builder.current_clip_version = ctxt.gstate.clip_path_version


def rectclip(ctxt, ostack):
    """
    x y width height **rectclip** –
           numarray **rectclip** –
          numstring **rectclip** –

    Intersects the area inside the current clipping path with a rectangular path
    defined by the operands to produce a new, smaller clipping path. After
    computing the new clipping path, **rectclip** clears the current path with an
    implicit **newpath** operation.

    PLRM Section 8.2, Page 641-642
    Stack: x y width height → – OR numarray → – OR numstring → –
    **Errors**: **limitcheck**, **stackunderflow**, **typecheck**
    **See Also**: **clip**, **eoclip**, **clippath**, **initclip**, **rectfill**, **rectstroke**
    """
    # 1. STACKUNDERFLOW
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rectclip.__name__)

    if ostack[-1].TYPE not in {ps.T_INT, ps.T_REAL, ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, rectclip.__name__)

    # Collect list of (x, y, w, h) tuples
    rects = []

    if ostack[-1].TYPE in ps.NUMERIC_TYPES:
        # First form: x y width height rectclip
        if len(ostack) < 4:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rectclip.__name__)
        for i in range(-4, 0):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, rectclip.__name__)

        rects.append((ostack[-4].val, ostack[-3].val,
                       ostack[-2].val, ostack[-1].val))
        ostack.pop()
        ostack.pop()
        ostack.pop()
        ostack.pop()

    elif ostack[-1].TYPE in ps.ARRAY_TYPES:
        # Second form: numarray rectclip
        arr = ostack[-1]
        if arr.length % 4 != 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, rectclip.__name__)
        for i in range(arr.start, arr.start + arr.length):
            if arr.val[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, rectclip.__name__)
        for i in range(arr.start, arr.start + arr.length, 4):
            rects.append((arr.val[i].val, arr.val[i + 1].val,
                           arr.val[i + 2].val, arr.val[i + 3].val))
        ostack.pop()

    else:
        # Third form: numstring rectclip (encoded number string)
        # TODO: decode encoded number string
        ostack.pop()
        ctxt.gstate.path = ps.Path()
        ctxt.gstate.currentpoint = None
        return

    # Build rectangular path in device coordinates
    clip_path = ps.Path()
    ctm = ctxt.gstate.CTM
    for x, y, w, h in rects:
        sub = ps.SubPath()
        sub.append(ps.MoveTo(ps.Point(*_transform_point(ctm, x, y))))
        sub.append(ps.LineTo(ps.Point(*_transform_point(ctm, x + w, y))))
        sub.append(ps.LineTo(ps.Point(*_transform_point(ctm, x + w, y + h))))
        sub.append(ps.LineTo(ps.Point(*_transform_point(ctm, x, y + h))))
        sub.append(ps.ClosePath())
        clip_path.append(sub)

    # Set as current path, apply clip, then newpath
    ctxt.gstate.path = clip_path
    clip(ctxt, ostack)
    ctxt.gstate.path = ps.Path()
    ctxt.gstate.currentpoint = None
