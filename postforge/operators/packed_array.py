# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from ..core import error as ps_error
from ..core import types as ps


def currentpacking(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentpacking** bool


    returns the array packing mode currently in effect.

    **Errors**:     **stackoverflow**
    **See Also**:   **setpacking**, **packedarray**
    """

    if ctxt.MaxOpStack and len(ostack) + 1 >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentpacking.__name__)

    ostack.append(ps.Bool(ctxt.packing))


def packedarray(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    any(0) ... any(n-1) n **packedarray** **packedarray**


    creates a packed array object of length n containing the objects any(0) through
    any(n-1) as elements. **packedarray** first removes the nonnegative integer n from the
    operand stack. It then removes that number of objects from the operand stack,
    creates a packed array containing those objects as elements, and finally pushes the
    resulting packed array object on the operand stack.

    The resulting object has a type of **packedarraytype**, a literal attribute, and **readonly**
    access. In all other respects, its behavior is identical to that of an ordinary
    array object.

    The packed array is allocated in local or global VM according to the current VM
    allocation mode. An **invalidaccess** error occurs if the packed array is in global VM
    and any of the objects any(0) through any(n-1) are in local VM (see Section 3.7.2,
    "Local and Global VM").

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**, **VMerror**
    **See Also**:   **aload**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, packedarray.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, packedarray.__name__)
    # 3. RANGECHECK - Check minimum value
    if ostack[-1].val < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, packedarray.__name__)

    if len(ostack) - 1 < ostack[-1].val:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, packedarray.__name__)

    # check for invalid access
    if ctxt.vm_alloc_mode:
        for i in range(-2, -2 - ostack[-1].val, -1):
            if ostack[i].is_composite and not ostack[i].is_global:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, packedarray.__name__)

    length = ostack[-1].val
    ostack.pop()

    parr = ps.PackedArray(ctxt.id, is_global=ctxt.vm_alloc_mode)
    for _ in range(length):
        parr.val.append(ostack.pop())
    parr.reverse()
    parr.access = ps.ACCESS_READ_ONLY
    parr.length = length

    ostack.append(parr)


def setpacking(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    bool **setpacking** -


    sets the array packing mode to bool. This determines the type of executable arrays
    subsequently created by the PostScript language scanner. The value true selects
    packed arrays; false selects ordinary arrays.

    The packing mode affects only the creation of procedures by the scanner when it
    encounters program text bracketed by { and } during interpretation of an executable
    file or string object, or during execution of the token operator. It does not
    affect the creation of literal arrays by the [ and ] operators or by the array operator.

    Modifications to the array packing mode parameter are subject to **save** and **restore**.
    In an interpreter that supports multiple contexts, this parameter is maintained
    separately for each context.

    **Example**
        **systemdict** /**setpacking** **known**
            {  /savepacking **currentpacking** def
               true **setpacking**
            } if

        ... Arbitrary procedure definitions ...

        **systemdict** /**setpacking** **known**
            {savepacking **setpacking**} if

    This example illustrates how to use packed arrays in a way that is compatible with
    all LanguageLevels. If the packed array facility is available, the procedures represented
    by the arbitrary procedure definitions are defined as packed arrays; otherwise,
    they are defined as ordinary arrays. The example is careful to preserve the
    array packing mode in effect before its execution.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **currentpacking**, **packedarray**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setpacking.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setpacking.__name__)

    ctxt.packing = ostack[-1].val
    ostack.pop()
