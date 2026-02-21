# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import copy
import math

from ..core import error as ps_error
from ..core import types as ps
from .graphics_state import gsave, grestore
from .matrix import _setCTM


def makepattern(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    dict matrix **makepattern** pattern

    Instantiates a pattern defined by a pattern dictionary, producing an instance
    of the pattern locked to the current user space.

    This operator creates a copy of dict in local VM, adding an Implementation entry.
    It saves a copy of the current graphics state for later use when PaintProc is called,
    with the matrix operand concatenated with the saved copy of the CTM.

    PLRM Section 8.2, Page 525
    Stack: dict matrix → pattern
    **Errors**: **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**, **undefined**, **VMerror**
    **See Also**: **setpattern**, **setcolor**, **setcolorspace**
    """
    # 1. Stack validation
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, makepattern.__name__)

    # 2. Type validation - matrix must be array with 6 elements
    matrix_op = ostack[-1]
    dict_op = ostack[-2]

    if matrix_op.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)

    if matrix_op.length != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, makepattern.__name__)

    if dict_op.TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)

    # 3. Validate pattern dictionary entries
    pattern_dict = dict_op.val

    # PatternType is required for all patterns
    if b'PatternType' not in pattern_dict:
        return ps_error.e(ctxt, ps_error.UNDEFINED, makepattern.__name__)

    # Validate PatternType (must be 1 for tiling patterns, or 2 for shading patterns)
    pattern_type = pattern_dict[b'PatternType']
    if pattern_type.TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)

    pattern_type_val = int(pattern_type.val)
    if pattern_type_val not in (1, 2):
        return ps_error.e(ctxt, ps_error.RANGECHECK, makepattern.__name__)

    # Validate type-specific required entries
    if pattern_type_val == 1:
        # Type 1 (tiling) patterns require these entries
        required_keys = [b'PaintType', b'TilingType', b'BBox', b'XStep', b'YStep', b'PaintProc']
        for key in required_keys:
            if key not in pattern_dict:
                return ps_error.e(ctxt, ps_error.UNDEFINED, makepattern.__name__)
    else:
        # Type 2 (shading) patterns require Shading entry
        if b'Shading' not in pattern_dict:
            return ps_error.e(ctxt, ps_error.UNDEFINED, makepattern.__name__)

    # For Type 1 patterns, validate additional entries
    if pattern_type_val == 1:
        # Validate PaintType (1 = colored, 2 = uncolored)
        paint_type = pattern_dict[b'PaintType']
        if paint_type.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)
        paint_type_val = int(paint_type.val)
        if paint_type_val not in (1, 2):
            return ps_error.e(ctxt, ps_error.RANGECHECK, makepattern.__name__)

        # Validate TilingType (1 = constant spacing, 2 = no distortion, 3 = fast)
        tiling_type = pattern_dict[b'TilingType']
        if tiling_type.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)
        tiling_type_val = int(tiling_type.val)
        if tiling_type_val not in (1, 2, 3):
            return ps_error.e(ctxt, ps_error.RANGECHECK, makepattern.__name__)

        # Validate BBox (array of 4 numbers)
        bbox = pattern_dict[b'BBox']
        if bbox.TYPE not in ps.ARRAY_TYPES or bbox.length != 4:
            return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)

        # Validate XStep and YStep (non-zero numbers)
        xstep = pattern_dict[b'XStep']
        ystep = pattern_dict[b'YStep']
        if xstep.TYPE not in ps.NUMERIC_TYPES or ystep.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)
        if float(xstep.val) == 0 or float(ystep.val) == 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, makepattern.__name__)

        # Validate PaintProc (must be executable procedure)
        paint_proc = pattern_dict[b'PaintProc']
        if paint_proc.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)

    # 4. Extract matrix values
    matrix_vals = []
    for i in range(6):
        elem = matrix_op.val[matrix_op.start + i]
        if elem.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, makepattern.__name__)
        matrix_vals.append(float(elem.val))

    # 5. Create pattern instance dictionary (copy in local VM)
    try:
        pattern_instance = ps.Dict(ctxt.id, None, b"pattern", is_global=False)

        # Copy all entries from prototype dictionary
        for key, value in pattern_dict.items():
            # Shallow copy - subsidiary objects are shared (per PLRM)
            pattern_instance.val[key] = value

        # 6. Create Implementation entry
        # The Implementation contains the saved graphics state and pattern matrix info
        # Save a copy of the current graphics state for PaintProc execution
        saved_gstate = ctxt.gstate.copy()

        # Concatenate the provided matrix with the CTM
        # New CTM = matrix × CTM (matrix is pre-multiplied)
        ctm = ctxt.gstate.CTM.val
        a1, b1, c1, d1, tx1, ty1 = matrix_vals
        a2 = float(ctm[0].val)
        b2 = float(ctm[1].val)
        c2 = float(ctm[2].val)
        d2 = float(ctm[3].val)
        tx2 = float(ctm[4].val)
        ty2 = float(ctm[5].val)

        # Matrix multiplication: result = matrix × CTM
        new_a = a1 * a2 + b1 * c2
        new_b = a1 * b2 + b1 * d2
        new_c = c1 * a2 + d1 * c2
        new_d = c1 * b2 + d1 * d2
        new_tx = tx1 * a2 + ty1 * c2 + tx2
        new_ty = tx1 * b2 + ty1 * d2 + ty2

        # Store the concatenated matrix in saved state
        saved_gstate.CTM.val[0] = ps.Real(new_a)
        saved_gstate.CTM.val[1] = ps.Real(new_b)
        saved_gstate.CTM.val[2] = ps.Real(new_c)
        saved_gstate.CTM.val[3] = ps.Real(new_d)
        saved_gstate.CTM.val[4] = ps.Real(new_tx)
        saved_gstate.CTM.val[5] = ps.Real(new_ty)

        # Store implementation data as a Python dict (internal use only)
        impl_data = {
            'graphics_state': saved_gstate,
            'pattern_matrix': [new_a, new_b, new_c, new_d, new_tx, new_ty],
            'pattern_type': pattern_type_val,
        }

        if pattern_type_val == 1:
            # Type 1 (tiling) pattern - extract BBox, steps, and execute PaintProc
            bbox_arr = pattern_dict[b'BBox']
            bbox_vals = [float(bbox_arr.val[bbox_arr.start + i].val) for i in range(4)]
            impl_data['bbox'] = bbox_vals
            impl_data['xstep'] = float(pattern_dict[b'XStep'].val)
            impl_data['ystep'] = float(pattern_dict[b'YStep'].val)

            # Execute PaintProc NOW while dictionary stack is intact
            # This captures the pattern cell as a display list for later rendering
            paint_proc = pattern_dict[b'PaintProc']
            if paint_proc.TYPE in ps.ARRAY_TYPES:
                import copy as copy_module
                from . import control as ps_control

                # Save current context state
                saved_display_list = ctxt.display_list
                saved_current_gstate = ctxt.gstate

                # Create new display list for pattern cell rendering
                # Use a reasonable size - actual scaling happens at render time
                ctxt.display_list = ps.DisplayList(100, 100)

                # Use the saved graphics state for PaintProc
                ctxt.gstate = saved_gstate.copy()

                # Set up CTM for pattern space (identity for now - scaling at render time)
                ctxt.gstate.CTM.val[0] = ps.Real(1.0)
                ctxt.gstate.CTM.val[1] = ps.Real(0.0)
                ctxt.gstate.CTM.val[2] = ps.Real(0.0)
                ctxt.gstate.CTM.val[3] = ps.Real(1.0)
                ctxt.gstate.CTM.val[4] = ps.Real(0.0)
                ctxt.gstate.CTM.val[5] = ps.Real(0.0)

                # Clear path
                ctxt.gstate.path = ps.Path()
                ctxt.gstate.currentpoint = None

                # Push pattern dict onto operand stack (per PLRM)
                ostack.append(pattern_instance)

                # Execute PaintProc with Stopped context to catch errors
                ctxt.e_stack.append(ps.HardReturn())
                ctxt.e_stack.append(ps.Stopped())
                ctxt.e_stack.append(copy_module.copy(paint_proc))
                ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)

                # Check for error and clean up stack
                if ostack and hasattr(ostack[-1], 'TYPE') and ostack[-1].TYPE == ps.T_BOOL:
                    ostack.pop()  # Pop the stopped result (true=error, false=success)

                # Cache the display list
                impl_data['cached_display_list'] = list(ctxt.display_list)

                # Restore context
                ctxt.display_list = saved_display_list
                ctxt.gstate = saved_current_gstate

        else:
            # Type 2 (shading) pattern - store reference to Shading dictionary
            # The shading will be rendered at fill time using the existing shading infrastructure
            impl_data['shading'] = pattern_dict[b'Shading']

        # Create Implementation entry (stored as Name pointing to internal data)
        # We use a special marker that the renderer can detect
        pattern_instance.val[b'Implementation'] = ps.Name(b'_pattern_impl')
        pattern_instance._pattern_impl = impl_data

        # 7. Make the pattern dictionary read-only
        pattern_instance.access = ps.ACCESS_READ_ONLY

        # 8. Pop operands and push result
        ostack.pop()  # matrix
        ostack.pop()  # dict
        ostack.append(pattern_instance)

    except MemoryError:
        return ps_error.e(ctxt, ps_error.VMERROR, makepattern.__name__)


def setpattern(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
              pattern **setpattern** –
    comp1 ... compn pattern **setpattern** –

    A convenience operator that sets the current color space to Pattern and
    establishes a specified pattern as the current color in a single operation.

    For colored tiling patterns (PatternType 1, PaintType 1) or shading patterns
    (PatternType 2), use the first form with just the pattern dictionary.

    For uncolored tiling patterns (PatternType 1, PaintType 2), use the second
    form with color components in the underlying color space.

    PLRM Section 8.2, Page 581
    Stack: pattern → – OR comp1 ... compn pattern → –
    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**, **undefined**
    **See Also**: **makepattern**, **setcolor**, **setcolorspace**
    """
    # 0. Type 3 font cache mode restriction
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setpattern.__name__)

    # 1. Stack validation - need at least the pattern
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setpattern.__name__)

    # 2. Type validation - pattern must be a dictionary with Implementation
    pattern_op = ostack[-1]
    if pattern_op.TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setpattern.__name__)

    # Must be an instantiated pattern (has Implementation entry)
    if b'Implementation' not in pattern_op.val:
        return ps_error.e(ctxt, ps_error.UNDEFINED, setpattern.__name__)

    # Get pattern type and paint type
    if b'PatternType' not in pattern_op.val:
        return ps_error.e(ctxt, ps_error.UNDEFINED, setpattern.__name__)

    pattern_type_val = int(pattern_op.val[b'PatternType'].val)

    # For Type 1 patterns, check PaintType
    if pattern_type_val == 1:
        if b'PaintType' not in pattern_op.val:
            return ps_error.e(ctxt, ps_error.UNDEFINED, setpattern.__name__)
        paint_type_val = int(pattern_op.val[b'PaintType'].val)
    else:
        # Type 2 (shading) patterns are always colored
        paint_type_val = 1

    # 3. Handle uncolored patterns (PaintType 2) - need underlying color components
    if paint_type_val == 2:
        # Get current color space for underlying colors
        current_space = ctxt.gstate.color_space
        underlying_space = current_space[0] if isinstance(current_space, list) else current_space

        # Get component count for underlying space
        component_counts = {
            "DeviceGray": 1,
            "DeviceRGB": 3,
            "DeviceCMYK": 4,
        }
        required_components = component_counts.get(underlying_space, 0)

        # Validate we have enough operands
        if len(ostack) < required_components + 1:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setpattern.__name__)

        # Validate and collect color components
        underlying_color = []
        for i in range(required_components):
            idx = -(required_components + 1) + i
            if ostack[idx].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, setpattern.__name__)
            underlying_color.append(max(0.0, min(1.0, float(ostack[idx].val))))

        # Pop the color components and pattern
        for _ in range(required_components + 1):
            ostack.pop()

        # Set color space to [/Pattern underlying_space]
        ctxt.gstate.color_space = ["Pattern", underlying_space]

        # Set color: store pattern dictionary and underlying color
        # The pattern dict reference is stored, underlying colors are separate
        ctxt.gstate.color = underlying_color
        ctxt.gstate._current_pattern = pattern_op

    else:
        # Colored pattern or shading pattern - no underlying color needed
        # Pop the pattern
        ostack.pop()

        # If current color space is not already Pattern, set it to [/Pattern]
        current_space = ctxt.gstate.color_space
        space_name = current_space[0] if isinstance(current_space, list) else current_space
        if space_name != "Pattern":
            ctxt.gstate.color_space = ["Pattern"]

        # Set color: store pattern dictionary reference
        ctxt.gstate.color = []  # No underlying color components
        ctxt.gstate._current_pattern = pattern_op


# Module-level cache for form display lists, keyed by id(form_dict.val).
# Dict.__copy__ shares the same .val dict, so id(form_dict.val) is stable
# across copies of the same PostScript dictionary.
_form_cache = {}


def _compose_ctm_tuple(cached: tuple[float, ...], real: tuple[float, ...]) -> tuple[float, float, float, float, float, float]:
    """Compose two affine **transform** tuples: result = cached · real.

    Each tuple is (a, b, c, d, tx, ty) in PostScript matrix convention.
    The result transforms a point first by cached, then by real.
    """
    ca, cb, cc, cd, ctx, cty = cached
    ra, rb, rc, rd, rtx, rty = real
    return (ca * ra + cb * rc, ca * rb + cb * rd,
            cc * ra + cd * rc, cc * rb + cd * rd,
            ctx * ra + cty * rc + rtx, ctx * rb + cty * rd + rty)


def _ctm_scale(ctm_tuple: tuple[float, ...]) -> float:
    """Compute geometric mean **scale** factor from a CTM tuple."""
    a, b, c, d = ctm_tuple[0], ctm_tuple[1], ctm_tuple[2], ctm_tuple[3]
    sx = math.sqrt(a * a + b * b)
    sy = math.sqrt(c * c + d * d)
    return math.sqrt(sx * sy)


def _replay_form_elements(cached_elements: list, ctm: ps.Array, display_list: ps.DisplayList) -> None:
    """Transform cached form-space display list elements to device space and append.

    Cached elements were captured with an identity CTM, so their coordinates are
    in form space. Path points are transformed directly via the real CTM. Elements
    that store a CTM (Stroke, Image, TextObj, etc.) have their cached CTM composed
    with the real CTM so that form-internal transforms (**translate**, **scale**, etc.
    inside PaintProc) are preserved.

    Args:
        cached_elements: List of display list elements captured with identity CTM
        ctm: Real CTM as a ps.Array (6-element PostScript matrix)
        display_list: Target display list to append transformed elements to
    """
    # Extract real CTM components once
    ctm_tuple = (ctm.val[0].val, ctm.val[1].val, ctm.val[2].val,
                 ctm.val[3].val, ctm.val[4].val, ctm.val[5].val)
    a, b, c, d, tx, ty = ctm_tuple

    def xform(x, y):
        """Transform point from form space to device space."""
        return (a * x + c * y + tx, b * x + d * y + ty)

    def xform_path(path):
        """Deep-copy and **transform** a Path (list of SubPaths with Points)."""
        new_path = ps.Path()
        for subpath in path:
            new_sp = ps.SubPath()
            for elem in subpath:
                if isinstance(elem, ps.MoveTo):
                    nx, ny = xform(elem.p.x, elem.p.y)
                    new_sp.append(ps.MoveTo(ps.Point(nx, ny)))
                elif isinstance(elem, ps.LineTo):
                    nx, ny = xform(elem.p.x, elem.p.y)
                    new_sp.append(ps.LineTo(ps.Point(nx, ny)))
                elif isinstance(elem, ps.CurveTo):
                    x1, y1 = xform(elem.p1.x, elem.p1.y)
                    x2, y2 = xform(elem.p2.x, elem.p2.y)
                    x3, y3 = xform(elem.p3.x, elem.p3.y)
                    new_sp.append(ps.CurveTo(
                        ps.Point(x1, y1), ps.Point(x2, y2), ps.Point(x3, y3)))
                elif isinstance(elem, ps.ClosePath):
                    new_sp.append(ps.ClosePath())
            new_path.append(new_sp)
        return new_path

    for elem in cached_elements:
        if isinstance(elem, ps.Path):
            display_list.append(xform_path(elem))

        elif isinstance(elem, ps.Fill):
            display_list.append(elem)

        elif isinstance(elem, ps.Stroke):
            s = copy.copy(elem)
            s.ctm = _compose_ctm_tuple(elem.ctm, ctm_tuple)
            display_list.append(s)

        elif isinstance(elem, ps.ClipElement):
            ce = copy.copy(elem)
            ce.path = xform_path(elem.path)
            display_list.append(ce)

        elif isinstance(elem, ps.TextObj):
            t = copy.copy(elem)
            t.start_x, t.start_y = xform(elem.start_x, elem.start_y)
            # Compose cached CTM (form-internal transforms) with real CTM
            cached_elem_ctm = tuple(elem.ctm)
            composed = _compose_ctm_tuple(cached_elem_ctm, ctm_tuple)
            # Scale font_size by ratio of composed vs cached scale factors
            cached_scale = _ctm_scale(cached_elem_ctm)
            if cached_scale > 0:
                t.font_size = elem.font_size * _ctm_scale(composed) / cached_scale
            else:
                t.font_size = elem.font_size * _ctm_scale(composed)
            t.ctm = list(composed)
            display_list.append(t)

        elif isinstance(elem, ps.ActualTextStart):
            at = copy.copy(elem)
            at.start_x, at.start_y = xform(elem.start_x, elem.start_y)
            cached_elem_ctm = tuple(elem.ctm)
            composed = _compose_ctm_tuple(cached_elem_ctm, ctm_tuple)
            cached_scale = _ctm_scale(cached_elem_ctm)
            if cached_scale > 0:
                at.font_size = elem.font_size * _ctm_scale(composed) / cached_scale
            else:
                at.font_size = elem.font_size * _ctm_scale(composed)
            at.ctm = list(composed)
            display_list.append(at)

        elif isinstance(elem, ps.ActualTextEnd):
            display_list.append(elem)

        elif isinstance(elem, (ps.ImageElement, ps.ImageMaskElement, ps.ColorImageElement)):
            img = copy.copy(elem)
            composed = _compose_ctm_tuple(elem.ctm, ctm_tuple)
            img.CTM = list(composed)
            img.ctm = composed
            img.ictm = None  # Recomputed lazily if needed
            display_list.append(img)

        elif isinstance(elem, ps.GlyphRef):
            g = copy.copy(elem)
            g.position_x, g.position_y = xform(elem.position_x, elem.position_y)
            display_list.append(g)

        elif isinstance(elem, ps.GlyphStart):
            g = copy.copy(elem)
            g.position_x, g.position_y = xform(elem.position_x, elem.position_y)
            display_list.append(g)

        elif isinstance(elem, ps.GlyphEnd):
            display_list.append(elem)

        elif isinstance(elem, ps.PatternFill):
            pf = copy.copy(elem)
            pf.ctm = _compose_ctm_tuple(elem.ctm, ctm_tuple)
            display_list.append(pf)

        elif isinstance(elem, (ps.AxialShadingFill, ps.RadialShadingFill,
                               ps.MeshShadingFill, ps.PatchShadingFill,
                               ps.FunctionShadingFill)):
            sf = copy.copy(elem)
            sf.ctm = _compose_ctm_tuple(elem.ctm, ctm_tuple)
            display_list.append(sf)

        elif isinstance(elem, (ps.ErasePage, ps.ShowPage)):
            pass  # Skip - shouldn't appear in form PaintProc

        else:
            # Unknown element type - append as-is rather than silently dropping
            display_list.append(elem)


def execform(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    form **execform** –

    Paints a form defined by a form dictionary constructed as described in
    Section 4.7, "Forms." The graphical output produced by **execform** is defined
    by the form dictionary's PaintProc procedure.

    If this is the first invocation of **execform** for form, **execform** first verifies
    that the dictionary contains the required entries. Then it adds an entry to
    the dictionary with the key Implementation, whose value is private to the
    PostScript interpreter. Finally, it makes the dictionary read-only.

    Caching: On first invocation, PaintProc is executed with an identity CTM so
    that display list elements are captured in form coordinate space. On replay,
    elements are transformed to device space via the current CTM.

    PLRM Section 8.2, Page 582-583
    Stack: form → –
    **Errors**: **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**, **undefined**, **VMerror**
    **See Also**: **findresource**
    """
    from . import control as ps_control

    # 1. STACKUNDERFLOW
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, execform.__name__)

    # 2. TYPECHECK - must be a dictionary
    form_dict = ostack[-1]
    if form_dict.TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, execform.__name__)

    # 3. Check read access
    if form_dict.access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, execform.__name__)

    first_invocation = b'Implementation' not in form_dict.val

    if first_invocation:
        # Validate required keys

        # FormType - must be integer, must be 1
        if b'FormType' not in form_dict.val:
            return ps_error.e(ctxt, ps_error.UNDEFINED, execform.__name__)
        form_type = form_dict.val[b'FormType']
        if form_type.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, execform.__name__)
        if int(form_type.val) != 1:
            return ps_error.e(ctxt, ps_error.RANGECHECK, execform.__name__)

        # BBox - must be 4-element numeric array
        if b'BBox' not in form_dict.val:
            return ps_error.e(ctxt, ps_error.UNDEFINED, execform.__name__)
        bbox = form_dict.val[b'BBox']
        if bbox.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, execform.__name__)
        if bbox.length != 4:
            return ps_error.e(ctxt, ps_error.RANGECHECK, execform.__name__)
        for i in range(4):
            if bbox.val[bbox.start + i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, execform.__name__)

        # Matrix - must be 6-element numeric array
        if b'Matrix' not in form_dict.val:
            return ps_error.e(ctxt, ps_error.UNDEFINED, execform.__name__)
        matrix = form_dict.val[b'Matrix']
        if matrix.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, execform.__name__)
        if matrix.length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, execform.__name__)
        for i in range(6):
            if matrix.val[matrix.start + i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, execform.__name__)

        # PaintProc - must be present (executable procedure)
        if b'PaintProc' not in form_dict.val:
            return ps_error.e(ctxt, ps_error.UNDEFINED, execform.__name__)

    # Pop form dict from ostack (all validation passed)
    ostack.pop()

    # --- Execute the form ---
    # Per PLRM: gsave, concat Matrix, clip to BBox, newpath, push dict,
    # exec PaintProc, grestore

    # 1. gsave
    if len(ctxt.gstate_stack) >= ps.G_STACK_MAX:
        return ps_error.e(ctxt, ps_error.LIMITCHECK, execform.__name__)
    gsave(ctxt, ostack)

    # 2. Concat form's Matrix with CTM
    matrix = form_dict.val[b'Matrix']
    ostack.append(matrix)
    from .matrix import concat
    concat(ctxt, ostack)

    # 3. Clip to BBox [llx lly urx ury] using rectclip
    # Per PLRM pseudocode: convert urx/ury to width/height, then rectclip
    bbox = form_dict.val[b'BBox']
    llx = float(bbox.val[bbox.start + 0].val)
    lly = float(bbox.val[bbox.start + 1].val)
    urx = float(bbox.val[bbox.start + 2].val)
    ury = float(bbox.val[bbox.start + 3].val)
    ostack.append(ps.Real(llx))
    ostack.append(ps.Real(lly))
    ostack.append(ps.Real(urx - llx))
    ostack.append(ps.Real(ury - lly))
    from .clipping import rectclip
    rectclip(ctxt, ostack)  # also does newpath

    # Save the real CTM values (after Matrix concat + BBox clip) for replay.
    # Must copy the list since _setCTM replaces CTM.val in place.
    real_ctm_vals = list(ctxt.gstate.CTM.val)

    if first_invocation:
        # --- Cache PaintProc output in form coordinate space ---

        # Set CTM to identity so display list elements are in form space
        identity = [ps.Real(1.0), ps.Real(0.0), ps.Real(0.0),
                    ps.Real(1.0), ps.Real(0.0), ps.Real(0.0)]
        _setCTM(ctxt, identity)

        # Switch to NativeStroke so strokes produce Stroke elements (line_width
        # + CTM tuple) instead of going through strokepath, which bakes in
        # device-pixel line widths that are wrong for an identity CTM.
        page_device = ctxt.gstate.page_device
        if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
            page_device_dict = page_device.val
        else:
            page_device_dict = page_device
        saved_stroke_method = page_device_dict.get(b'StrokeMethod')
        page_device_dict[b'StrokeMethod'] = ps.Name(b'NativeStroke')

        # Disable glyph bitmap caching so glyphs render as paths in form
        # space. Cached bitmaps would be at identity-CTM scale (tiny) and
        # unrotated, making them useless for replay at different CTMs.
        saved_glyph_cache_disabled = ps.global_resources.glyph_cache_disabled
        ps.global_resources.glyph_cache_disabled = True

        # Save current display list and create temporary one for capture
        saved_display_list = ctxt.display_list
        ctxt.display_list = ps.DisplayList(
            saved_display_list.width, saved_display_list.height)

        # Push form dict onto ostack for PaintProc
        ostack.append(form_dict)

        # Execute PaintProc
        paint_proc = form_dict.val[b'PaintProc']
        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(ps.Stopped())
        ctxt.e_stack.append(copy.copy(paint_proc))
        ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)

        # Clean up stopped result
        if ostack and hasattr(ostack[-1], 'TYPE') and ostack[-1].TYPE == ps.T_BOOL:
            ostack.pop()

        # Cache the form-space display list elements keyed by dict identity
        _form_cache[id(form_dict.val)] = list(ctxt.display_list)

        # Restore real display list, stroke method, and glyph cache
        ctxt.display_list = saved_display_list
        if saved_stroke_method is not None:
            page_device_dict[b'StrokeMethod'] = saved_stroke_method
        elif b'StrokeMethod' in page_device_dict:
            del page_device_dict[b'StrokeMethod']
        ps.global_resources.glyph_cache_disabled = saved_glyph_cache_disabled

        # Restore real CTM for replay
        _setCTM(ctxt, real_ctm_vals)

        # Add Implementation key and make read-only
        form_dict.access = ps.ACCESS_UNLIMITED
        form_dict.val[b'Implementation'] = ps.Name(b'_form_impl')
        form_dict.access = ps.ACCESS_READ_ONLY

    # Replay cached elements transformed to device space.
    # In both paths, ctxt.gstate.CTM holds the real CTM at this point:
    # - first_invocation: restored via _setCTM(ctxt, real_ctm_vals)
    # - subsequent: never changed (concat + rectclip already set it)
    _replay_form_elements(_form_cache[id(form_dict.val)], ctxt.gstate.CTM, ctxt.display_list)

    # grestore
    grestore(ctxt, ostack)
