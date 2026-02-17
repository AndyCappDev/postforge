# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript array functions
"""

from ..core import error as ps_error
from ..core import types as ps


def aload(ctxt, ostack):
    """
          array **aload** any(0) ... any(n-1) array
    **packedarray** **aload** any(0) ... any(n-1) **packedarray**

    successively pushes all n elements of array or **packedarray** on the operand stack
    (where n is the length of the operand), and then pushes the operand itself.

    **Example**
        [23 (ab) –6] **aload** -> 23 (ab) –6 [23 (ab) –6]

    **Errors**:     **invalidaccess**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **astore**, **get**, **getinterval**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, aload.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, aload.__name__)
    
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, aload.__name__)
    
    # 5. STACKOVERFLOW - Check result stack space
    if ctxt.MaxOpStack and len(ostack) + ostack[-1].length > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, aload.__name__)

    arr = ostack.pop()
    for i in range(arr.start, arr.start + arr.length, 1):
        elem = arr.val[i]
        # Defensive check: if element is a raw Python type, wrap it
        # This can happen if raw values were stored directly in an array
        if not isinstance(elem, ps.PSObject):
            if isinstance(elem, bool):
                elem = ps.Bool(elem)
            elif isinstance(elem, int):
                elem = ps.Int(elem)
            elif isinstance(elem, float):
                elem = ps.Real(elem)
            elif elem is None:
                elem = ps.Null()
            elif isinstance(elem, bytes):
                # Treat bytes as a Name
                elem = ps.Name(elem, is_global=ctxt.vm_alloc_mode)
        ostack.append(elem)
    ostack.append(arr)

    return None


def array(ctxt, ostack):
    """
    int **array** **array**

    creates an **array** of length int, each of whose elements is initialized with a null object,
    and pushes this **array** on the operand stack. The int operand must be a nonnegative
    integer not greater than the maximum allowable **array** length (see
    Appendix B). The **array** is allocated in local or global VM according to the current
    VM allocation mode (see Section 3.7.2, “Local and Global VM”).

    **Example**
        3 **array** -> [null null null]

    **Errors**:     **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**, **VMerror**
    **See Also**:   [, ], **aload**, **astore**, **packedarray**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, array.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, array.__name__)
    
    # 4. RANGECHECK - Check value range
    if ostack[-1].val < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, array.__name__)
    
    # 5. STACKOVERFLOW - Check result stack space
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, array.__name__)

    length = ostack[-1].val
    ostack[-1] = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
    ostack[-1].val = [ps.Null() for _ in range(length)]
    ostack[-1].length = length
    
    # Update local_refs to track the correct val after reassignment
    if not ostack[-1].is_global and ostack[-1].ctxt_id is not None:
        ps.contexts[ostack[-1].ctxt_id].local_refs[ostack[-1].created] = ostack[-1].val

    return None


def array_from_mark(ctxt, ostack, mark=b"["):
    """
    mark obj0 ... objn-1 **]** array

    Creates a new array of n elements, where n is the number of elements
    above the topmost mark on the operand stack. Stores those elements
    into the array and returns it. The topmost object becomes element
    n-1 and the bottommost (immediately above the mark) becomes element 0.
    Both the array elements and the mark are removed from the stack.

    The array is allocated in local or global VM according to the current
    VM allocation mode. An **invalidaccess** error occurs if the array is
    in global VM and any of the objects are in local VM.

    **Errors**: **invalidaccess**, **stackoverflow**, **unmatchedmark**
    **See Also**: **[**, **mark**, **array**, **astore**
    """

    if mark == b"{" and ctxt.packing:
        arr = ps.PackedArray(ctxt.id, is_global=ctxt.vm_alloc_mode)
    else:
        arr = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
    matched = False
    # bmark = bytes(mark, "ascii")
    while ostack:
        if ostack[-1].TYPE == ps.T_MARK and ostack[-1].val[0] == mark[0]:
            matched = True
            break
        else:
            # if this is a global composite object referenced from a local array
            # then add it to the global_refs dict
            if ostack[-1].is_global and ostack[-1].is_composite and not arr.is_global:
                ctxt.global_refs[ostack[-1].created] = ostack[-1].val

            arr.val.insert(0, ostack.pop())

    if not matched:
        return ps_error.e(ctxt, ps_error.UNMATCHEDMARK, mark.decode())

    arr.start = 0
    arr.length = len(arr.val)
    # replace the mark object now at the top of the operand
    # stack with our new array object
    ostack[-1] = arr

    return None


def astore(ctxt, ostack):
    """
    any(0) ... any(n-1) array **astore** array

    stores the objects any0 to anyn-1 from the operand stack into array, where n is the
    length of array. The **astore** operator first removes the array operand from the stack
    and determines its length. It then removes that number of objects from the stack,
    storing the topmost one into element n - 1 of array and the bottommost one into
    element 0. Finally, it pushes array back on the stack. Note that an **astore** operation
    cannot be performed on packed arrays.

    If the value of array is in global VM and any of the objects any(0) through any(n-1) are
    composite objects whose values are in local VM, an **invalidaccess** error occurs (see
    Section 3.7.2, "Local and Global VM").

    **Example**
        (a) (bcd) (ef) 3 array **astore** -> [(a) (bcd) (ef)]

    This example creates a three-element array, stores the strings a, bcd, and ef into it
    as elements 0, 1, and 2, and leaves the array object on the operand stack.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **aload**, **put**, **putinterval**
    """

    # 1. STACKUNDERFLOW - Check stack depth for array
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, astore.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_ARRAY:
        return ps_error.e(ctxt, ps_error.TYPECHECK, astore.__name__)
    
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, astore.__name__)
    
    # 1. STACKUNDERFLOW - Check sufficient elements for array length
    if len(ostack) - 1 < ostack[-1].length:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, astore.__name__)

    # 3. INVALIDACCESS - Check for invalid VM access
    if ostack[-1].is_global:
        for i in range(-2, -2 - ostack[-1].length, -1):
            if ostack[i].is_composite and not ostack[i].is_global:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, astore.__name__)

    arr = ostack.pop()

    for i in range(arr.length - 1, -1, -1):
        if not ostack:
            break
        item = ostack.pop()

        # if this is a global composite object referenced from a local array
        # then add it to the global_refs dict
        if item.is_global and item.is_composite and not arr.is_global:
            ctxt.global_refs[item.created] = item.val

        arr.put(ps.Int(i), item)

    ostack.append(arr)

    return None


def procedure_from_mark(ctxt, ostack):
    """
    mark obj0 ... objn-1 **}** proc

    Creates an executable array (procedure) from the objects between
    the matching **{** mark and this **}** operator. If packing mode is
    enabled, a packed array is created instead. The resulting procedure
    is given executable attribute so it can be invoked later.

    **Errors**: **unmatchedmark**
    **See Also**: **{**, **packedarray**
    """

    # create the array
    array_from_mark(ctxt, ostack, b"{")
    # mark it as executable
    ostack[-1].attrib = ps.ATTRIB_EXEC
