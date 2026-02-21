# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PostScript user path operators: ucache, uappend, upath, ufill, ueofill,
ustroke, ustrokepath, inufill, inueofill, inustroke.

User paths are self-contained path descriptions stored as arrays.  They
support two formats:

  Ordinary: { ucache llx lly urx ury setbbox ... moveto ... lineto ... }
  Encoded:  [ data_array opcode_string ]

Caching (ucache) is accepted but treated as a no-op.
"""

import copy

from ..core import error as ps_error
from ..core import types as ps
from ..core.types.constants import NUMERIC_TYPES, ARRAY_TYPES
from . import graphics_state as ps_gstate
from . import insideness as ps_insideness
from . import painting as ps_painting
from . import path as ps_path
from . import path_query as ps_path_query
from . import strokepath as ps_strokepath
from .matrix import _transform_point, _setCTM, concat as matrix_concat


# ── Opcode table for encoded user paths ──────────────────────────────
# Maps opcode byte → (operator_name_bytes, num_operands)
_ENCODED_OPS = {
    0:  (b"setbbox",   4),
    1:  (b"moveto",    2),
    2:  (b"rmoveto",   2),
    3:  (b"lineto",    2),
    4:  (b"rlineto",   2),
    5:  (b"curveto",   6),
    6:  (b"rcurveto",  6),
    7:  (b"arc",       5),
    8:  (b"arcn",      5),
    9:  (b"arct",      5),
    10: (b"closepath", 0),
    11: (b"ucache",    0),
}


# ── ucache ────────────────────────────────────────────────────────────

def ucache(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **ucache** -
    Declares that the enclosing user path should be cached.
    This is a no-op in PostForge (caching is deferred).
    **Errors**: none
    """
    pass


# ── Internal helpers ──────────────────────────────────────────────────

def _uappend_ordinary(ctxt: ps.Context, ostack: ps.Stack, userpath: ps.PSObject) -> ps.PSObject | None:
    """Interpret an ordinary (procedure-body) user path array."""
    sysdict = ctxt.d_stack[0]
    ctxt.d_stack.append(sysdict)
    try:
        for item in userpath.val:
            if item.TYPE in NUMERIC_TYPES:
                ostack.append(item)
            elif item.TYPE == ps.T_NAME:
                name = item.val
                if name in sysdict.val:
                    op = sysdict.val[name]
                    if op.TYPE == ps.T_OPERATOR:
                        err = op.val(ctxt, ostack)
                        if err is not None:
                            return err
                    else:
                        ostack.append(op)
                else:
                    return ps_error.e(ctxt, ps_error.UNDEFINED, "uappend")
            elif item.TYPE == ps.T_OPERATOR:
                err = item.val(ctxt, ostack)
                if err is not None:
                    return err
    finally:
        ctxt.d_stack.pop()
    return None


def _uappend_encoded(ctxt: ps.Context, ostack: ps.Stack, data_arr: ps.PSObject, opcode_str: ps.PSObject) -> ps.PSObject | None:
    """Interpret an encoded user path (data array + opcode string)."""
    sysdict = ctxt.d_stack[0]
    data = data_arr.val
    opcodes = opcode_str.byte_string()  # bytes
    data_idx = 0
    op_idx = 0

    while op_idx < len(opcodes):
        code = opcodes[op_idx]
        op_idx += 1

        # Repeat prefix: bytes 32-255 mean repeat next opcode (code - 32) times
        if code >= 32:
            repeat_count = code - 32
            if op_idx >= len(opcodes):
                return ps_error.e(ctxt, ps_error.RANGECHECK, "uappend")
            code = opcodes[op_idx]
            op_idx += 1
        else:
            repeat_count = 1

        if code not in _ENCODED_OPS:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "uappend")

        op_name, num_operands = _ENCODED_OPS[code]

        for _ in range(repeat_count):
            # Push operands from data array
            if data_idx + num_operands > len(data):
                return ps_error.e(ctxt, ps_error.RANGECHECK, "uappend")
            for j in range(num_operands):
                ostack.append(data[data_idx])
                data_idx += 1

            # Look up and call the operator
            op = sysdict.val[op_name]
            err = op.val(ctxt, ostack)
            if err is not None:
                return err

    return None


def _is_encoded_userpath(userpath: ps.PSObject) -> bool:
    """Check if userpath is in encoded format: [data_array opcode_string]."""
    if userpath.length != 2:
        return False
    elem0 = userpath.val[0]
    elem1 = userpath.val[1]
    return elem0.TYPE in ARRAY_TYPES and elem1.TYPE == ps.T_STRING


# ── uappend ───────────────────────────────────────────────────────────

def uappend(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    userpath **uappend** -
    Interprets the user path description and appends the resulting path
    elements to the current path in the graphics state.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, uappend.__name__)

    if ostack[-1].TYPE not in ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, uappend.__name__)

    userpath = ostack.pop()

    if _is_encoded_userpath(userpath):
        return _uappend_encoded(ctxt, ostack, userpath.val[0], userpath.val[1])
    else:
        return _uappend_ordinary(ctxt, ostack, userpath)


# ── upath ─────────────────────────────────────────────────────────────

def upath(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    bool **upath** userpath
    Creates a user path description from the current path. If bool is true,
    the user path begins with **ucache**. The result is an executable array
    containing **setbbox**, **moveto**, **lineto**, **curveto**, and **closepath** operators
    interleaved with their numeric operands.
    **Errors**: **stackunderflow**, **typecheck**, **nocurrentpoint**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, upath.__name__)

    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, upath.__name__)

    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, upath.__name__)

    include_ucache = ostack.pop().val

    sysdict = ctxt.d_stack[0]
    result = []

    # Optionally include ucache
    if include_ucache:
        name = ps.Name(b"ucache")
        name.attrib = ps.ATTRIB_EXEC
        result.append(name)

    # Compute pathbbox — use stored bbox if available, otherwise compute
    if ctxt.gstate.bbox is not None:
        llx, lly, urx, ury = ctxt.gstate.bbox
    else:
        # Compute bbox by temporarily calling pathbbox logic
        temp_stack = []
        ps_path_query.pathbbox(ctxt, temp_stack)
        if len(temp_stack) >= 4:
            llx = temp_stack[-4].val
            lly = temp_stack[-3].val
            urx = temp_stack[-2].val
            ury = temp_stack[-1].val
        else:
            # pathbbox returned an error — use degenerate bbox
            cp = ctxt.gstate.currentpoint
            llx = lly = urx = ury = 0.0

    # Add setbbox
    result.append(ps.Real(float(llx)))
    result.append(ps.Real(float(lly)))
    result.append(ps.Real(float(urx)))
    result.append(ps.Real(float(ury)))
    name = ps.Name(b"setbbox")
    name.attrib = ps.ATTRIB_EXEC
    result.append(name)

    # Enumerate path elements, transforming from device to user space via iCTM
    iCTM = ctxt.gstate.iCTM
    for subpath in ctxt.gstate.path:
        for elem in subpath:
            if isinstance(elem, ps.MoveTo):
                ux, uy = _transform_point(iCTM, elem.p.x, elem.p.y)
                result.append(ps.Real(float(ux)))
                result.append(ps.Real(float(uy)))
                name = ps.Name(b"moveto")
                name.attrib = ps.ATTRIB_EXEC
                result.append(name)
            elif isinstance(elem, ps.LineTo):
                ux, uy = _transform_point(iCTM, elem.p.x, elem.p.y)
                result.append(ps.Real(float(ux)))
                result.append(ps.Real(float(uy)))
                name = ps.Name(b"lineto")
                name.attrib = ps.ATTRIB_EXEC
                result.append(name)
            elif isinstance(elem, ps.CurveTo):
                ux1, uy1 = _transform_point(iCTM, elem.p1.x, elem.p1.y)
                ux2, uy2 = _transform_point(iCTM, elem.p2.x, elem.p2.y)
                ux3, uy3 = _transform_point(iCTM, elem.p3.x, elem.p3.y)
                result.append(ps.Real(float(ux1)))
                result.append(ps.Real(float(uy1)))
                result.append(ps.Real(float(ux2)))
                result.append(ps.Real(float(uy2)))
                result.append(ps.Real(float(ux3)))
                result.append(ps.Real(float(uy3)))
                name = ps.Name(b"curveto")
                name.attrib = ps.ATTRIB_EXEC
                result.append(name)
            elif isinstance(elem, ps.ClosePath):
                name = ps.Name(b"closepath")
                name.attrib = ps.ATTRIB_EXEC
                result.append(name)

    # Build executable array
    arr = ps.Array(ctxt.id)
    arr.setval(result)
    arr.length = len(result)
    arr.attrib = ps.ATTRIB_EXEC
    ostack.append(arr)


# ── ufill / ueofill ──────────────────────────────────────────────────

def ufill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    userpath **ufill** -
    Paints the area enclosed by the user path using the nonzero winding
    number rule. Equivalent to: **gsave** **newpath** **uappend** **fill** **grestore**
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ufill.__name__)
    if ostack[-1].TYPE not in ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ufill.__name__)

    userpath = ostack.pop()

    ps_gstate.gsave(ctxt, ostack)
    ps_path.newpath(ctxt, ostack)

    ostack.append(userpath)
    err = uappend(ctxt, ostack)
    if err is not None:
        ps_gstate.grestore(ctxt, ostack)
        return err

    ps_painting.fill(ctxt, ostack)
    ps_gstate.grestore(ctxt, ostack)


def ueofill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    userpath **ueofill** -
    Paints the area enclosed by the user path using the even-odd rule.
    Equivalent to: **gsave** **newpath** **uappend** **eofill** **grestore**
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ueofill.__name__)
    if ostack[-1].TYPE not in ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ueofill.__name__)

    userpath = ostack.pop()

    ps_gstate.gsave(ctxt, ostack)
    ps_path.newpath(ctxt, ostack)

    ostack.append(userpath)
    err = uappend(ctxt, ostack)
    if err is not None:
        ps_gstate.grestore(ctxt, ostack)
        return err

    ps_painting.eofill(ctxt, ostack)
    ps_gstate.grestore(ctxt, ostack)


# ── ustroke ───────────────────────────────────────────────────────────

def _is_matrix_array(obj: ps.PSObject) -> bool:
    """Check if obj is a 6-element numeric array (a matrix)."""
    if obj.TYPE not in ARRAY_TYPES:
        return False
    if obj.length != 6:
        return False
    return all(item.TYPE in NUMERIC_TYPES for item in obj.val)


def ustroke(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    userpath **ustroke** -
    userpath matrix **ustroke** -
    Paints the user path with a **stroke**. If a matrix is provided, the
    CTM is temporarily concatenated with that matrix before stroking.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ustroke.__name__)

    # Detect matrix form: top is matrix, below is userpath
    has_matrix = False
    if len(ostack) >= 2 and _is_matrix_array(ostack[-1]):
        if ostack[-2].TYPE in ARRAY_TYPES:
            has_matrix = True

    if has_matrix:
        matrix = ostack.pop()
        if ostack[-1].TYPE not in ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, ustroke.__name__)
        userpath = ostack.pop()

        ps_gstate.gsave(ctxt, ostack)
        ps_path.newpath(ctxt, ostack)

        ostack.append(userpath)
        err = uappend(ctxt, ostack)
        if err is not None:
            ps_gstate.grestore(ctxt, ostack)
            return err

        # Concat matrix with CTM
        ostack.append(matrix)
        matrix_concat(ctxt, ostack)

        ps_painting.stroke(ctxt, ostack)
        ps_gstate.grestore(ctxt, ostack)
    else:
        if ostack[-1].TYPE not in ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, ustroke.__name__)
        userpath = ostack.pop()

        ps_gstate.gsave(ctxt, ostack)
        ps_path.newpath(ctxt, ostack)

        ostack.append(userpath)
        err = uappend(ctxt, ostack)
        if err is not None:
            ps_gstate.grestore(ctxt, ostack)
            return err

        ps_painting.stroke(ctxt, ostack)
        ps_gstate.grestore(ctxt, ostack)


# ── ustrokepath ───────────────────────────────────────────────────────

def ustrokepath(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    userpath **ustrokepath** -
    userpath matrix **ustrokepath** -
    Replaces the current path with the **stroke** outline of the user path.
    If a matrix is provided, the **stroke** is computed under a temporarily
    modified CTM.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ustrokepath.__name__)

    # Detect matrix form
    has_matrix = False
    if len(ostack) >= 2 and _is_matrix_array(ostack[-1]):
        if ostack[-2].TYPE in ARRAY_TYPES:
            has_matrix = True

    if has_matrix:
        matrix = ostack.pop()
        if ostack[-1].TYPE not in ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, ustrokepath.__name__)
        userpath = ostack.pop()

        ps_path.newpath(ctxt, ostack)

        ostack.append(userpath)
        err = uappend(ctxt, ostack)
        if err is not None:
            return err

        # Save CTM, concat matrix, strokepath, restore CTM
        saved_ctm = copy.deepcopy(ctxt.gstate.CTM.val)
        ostack.append(matrix)
        matrix_concat(ctxt, ostack)

        ps_strokepath.strokepath(ctxt, ostack)

        # Restore CTM
        _setCTM(ctxt, saved_ctm)
    else:
        if ostack[-1].TYPE not in ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, ustrokepath.__name__)
        userpath = ostack.pop()

        ps_path.newpath(ctxt, ostack)

        ostack.append(userpath)
        err = uappend(ctxt, ostack)
        if err is not None:
            return err

        ps_strokepath.strokepath(ctxt, ostack)


# ── inufill / inueofill / inustroke ───────────────────────────────────

def inufill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    x y userpath **inufill** bool
    Tests whether the point (x, y) would lie inside the area painted by
    **ufill** applied to the user path.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, inufill.__name__)

    if ostack[-1].TYPE not in ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, inufill.__name__)
    if ostack[-2].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, inufill.__name__)
    if ostack[-3].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, inufill.__name__)

    userpath = ostack.pop()
    y = ostack.pop()
    x = ostack.pop()

    # gsave; newpath; uappend userpath; push x y; infill; grestore
    ps_gstate.gsave(ctxt, ostack)
    ps_path.newpath(ctxt, ostack)

    ostack.append(userpath)
    err = uappend(ctxt, ostack)
    if err is not None:
        ps_gstate.grestore(ctxt, ostack)
        return err

    ostack.append(x)
    ostack.append(y)
    ps_insideness.infill(ctxt, ostack)

    result = ostack.pop()
    ps_gstate.grestore(ctxt, ostack)
    ostack.append(result)


def inueofill(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    x y userpath **inueofill** bool
    Tests whether the point (x, y) would lie inside the area painted by
    **ueofill** applied to the user path.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, inueofill.__name__)

    if ostack[-1].TYPE not in ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, inueofill.__name__)
    if ostack[-2].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, inueofill.__name__)
    if ostack[-3].TYPE not in NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, inueofill.__name__)

    userpath = ostack.pop()
    y = ostack.pop()
    x = ostack.pop()

    ps_gstate.gsave(ctxt, ostack)
    ps_path.newpath(ctxt, ostack)

    ostack.append(userpath)
    err = uappend(ctxt, ostack)
    if err is not None:
        ps_gstate.grestore(ctxt, ostack)
        return err

    ostack.append(x)
    ostack.append(y)
    ps_insideness.ineofill(ctxt, ostack)

    result = ostack.pop()
    ps_gstate.grestore(ctxt, ostack)
    ostack.append(result)


def inustroke(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    x y userpath **inustroke** bool
    x y userpath matrix **inustroke** bool
    Tests whether the point (x, y) would lie inside the area painted by
    **ustroke** applied to the user path.  If a matrix is given, the **stroke**
    is computed under a temporarily modified CTM.
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, inustroke.__name__)

    # Detect matrix form: top is matrix, below is userpath, then y, x
    has_matrix = False
    if len(ostack) >= 4 and _is_matrix_array(ostack[-1]):
        if ostack[-2].TYPE in ARRAY_TYPES:
            has_matrix = True

    if has_matrix:
        if len(ostack) < 4:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, inustroke.__name__)
        if ostack[-3].TYPE not in NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, inustroke.__name__)
        if ostack[-4].TYPE not in NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, inustroke.__name__)

        matrix = ostack.pop()
        userpath = ostack.pop()
        y = ostack.pop()
        x = ostack.pop()

        ps_gstate.gsave(ctxt, ostack)
        ps_path.newpath(ctxt, ostack)

        ostack.append(userpath)
        err = uappend(ctxt, ostack)
        if err is not None:
            ps_gstate.grestore(ctxt, ostack)
            return err

        # Concat matrix
        ostack.append(matrix)
        matrix_concat(ctxt, ostack)

        ostack.append(x)
        ostack.append(y)
        ps_insideness.instroke(ctxt, ostack)

        result = ostack.pop()
        ps_gstate.grestore(ctxt, ostack)
        ostack.append(result)
    else:
        if ostack[-1].TYPE not in ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, inustroke.__name__)
        if ostack[-2].TYPE not in NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, inustroke.__name__)
        if ostack[-3].TYPE not in NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, inustroke.__name__)

        userpath = ostack.pop()
        y = ostack.pop()
        x = ostack.pop()

        ps_gstate.gsave(ctxt, ostack)
        ps_path.newpath(ctxt, ostack)

        ostack.append(userpath)
        err = uappend(ctxt, ostack)
        if err is not None:
            ps_gstate.grestore(ctxt, ostack)
            return err

        ostack.append(x)
        ostack.append(y)
        ps_insideness.instroke(ctxt, ostack)

        result = ostack.pop()
        ps_gstate.grestore(ctxt, ostack)
        ostack.append(result)
