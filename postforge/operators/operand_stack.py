# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from copy import copy

from ..core import error as ps_error
from ..core import types as ps


def clear(ctxt, ostack):
    """
    |- any(1) ... any(n) **clear** |-


    pops all objects from the operand stack and discards them.

    **Errors**:     **none**
    **See Also**:   **count**, **cleartomark**, **pop**
    """

    while len(ostack):
        ostack.pop()


def cleartomark(ctxt, ostack):
    """
    mark obj(1) ... obj(n) **cleartomark** -


    pops entries from the operand stack repeatedly until it encounters a mark, which
    it also pops from the stack. obj(1) through obj(n) are any objects other than marks.

    **Errors**:     **unmatchedmark**
    **See Also**:   **clear**, **mark**, **counttomark**, **pop**
    """

    marked = False
    for count in range(1, len(ostack) + 1, 1):
        if ostack[-count].TYPE == ps.T_MARK:
            marked = True
            break
        else:
            count += 1
    if not marked:
        return ps_error.e(ctxt, ps_error.UNMATCHEDMARK, cleartomark.__name__)

    for _ in range(count):
        ostack.pop()


def count(ctxt, ostack):
    """
    |- any(1) ... any(n) **count** |- any(1) ... any(n) n


    counts the number of items on the operand stack and pushes this **count** on the
    operand stack.

    **Examples**
        **clear** **count**         -> 0
        **clear** 1 2 3 **count**   -> 1 2 3 3

    **Errors**:     **stackoverflow**
    **See Also**:   **counttomark**
    """

    if ctxt.MaxOpStack and len(ostack) + 1 >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, count.__name__)

    ostack.append(ps.Int(len(ostack)))


def counttomark(ctxt, ostack, internal: bool = False):
    """
    mark obj(1) ... obj(n) **counttomark** - mark obj(1) ... obj(n) n


    counts the number of objects on the operand stack, starting with the top element
    and continuing down to but not including the first mark encountered. obj(1)
    through obj(n) are any objects other than marks.

    **Examples**
        1 mark 2 3 **counttomark**  -> 1 mark 2 3 2
        1 mark **counttomark**      -> 1 mark 0

    **Errors**:     **stackoverflow**, **unmatchedmark**
    **See Also**:   **mark**, **count**
    """

    if not internal and ctxt.MaxOpStack and len(ostack) + 1 >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, counttomark.__name__)

    marked = False
    for count in range(1, len(ostack) + 1, 1):
        if ostack[-count].TYPE == ps.T_MARK:
            marked = True
            break
        else:
            count += 1
    if not marked:
        if internal:
            return ps_error.UNMATCHEDMARK
        return ps_error.e(ctxt, ps_error.UNMATCHEDMARK, counttomark.__name__)

    ostack.append(ps.Int(count - 1))


def dup(ctxt, ostack):
    """
    any **dup** any any


    duplicates the top element on the operand stack. **dup** copies only the object; the
    value of a composite object is not copied but is shared. See Section 3.3, "Data
    Types and Objects."

    **Errors**:     **stackoverflow**, **stackunderflow**
    **See Also**:   **copy**, **index**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, dup.__name__)
    # 2. STACKOVERFLOW - Check stack overflow (will push 1 item)
    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, dup.__name__)

    ostack.append(copy(ostack[-1]))


def exch(ctxt, ostack):
    """
    any₁ any₂ **exch** any₂ any₁


    exchanges the top two elements on the operand stack.

    **Examples**
        1 2 **exch** -> 2 1

    **Errors**:     **stackunderflow**
    **See Also**:   **dup**, **roll**, **index**, **pop**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, exch.__name__)

    ostack[-1], ostack[-2] = ostack[-2], ostack[-1]


def index(ctxt, ostack):
    """
    any(n) ... any(0) n **index** any(n) ... any(0) any(n)


    removes the nonnegative integer n from the operand stack, counts down
    to the nth element from the top of the stack, and pushes a copy of
    that element on the stack.

    **Examples**
        (a) (b) (c) (d) 0 **index** -> (a) (b) (c) (d) (d)
        (a) (b) (c) (d) 3 **index** -> (a) (b) (c) (d) (a)

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **copy**, **dup**, **roll**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, index.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, index.__name__)

    if ostack[-1].val < 0 or ostack[-1].val > len(ostack) - 2:
        return ps_error.e(ctxt, ps_error.RANGECHECK, index.__name__)

    ostack[-1] = copy(ostack[-2 - ostack[-1].val])


def ps_mark(ctxt, ostack):
    """
    - **mark** **mark**


    pushes a **mark** object on the operand stack. All marks are identical, and the operand
    stack may contain any number of them at once.

    The primary use of marks is to indicate the stack position of the beginning of an
    indefinitely long list of operands being passed to an operator or procedure. The ]
    operator (array construction) is the most common operator that works this way;
    it treats as operands all elements of the stack down to a **mark** that was pushed by
    the [ operator ([ is a synonym for **mark**). It is possible to define procedures that
    work similarly. Operators such as **counttomark** and **cleartomark** are useful within
    such procedures.

    **Errors**:     **stackoverflow**
    **See Also**:   **counttomark**, **cleartomark**, **pop**
    """
    op = "mark"

    if ctxt.MaxOpStack and len(ostack) + 1 >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, op)

    ostack.append(ps.Mark(b"["))


def pop(ctxt, ostack):
    """
    any **pop** -


    removes the top element from the operand stack and discards it.

    **Examples**
        1 2 3 **pop**       -> 1 2
        1 2 3 **pop** **pop**   -> 1

    **Errors**:     **stackunderflow**
    **See Also**:   **clear**, **dup**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, pop.__name__)

    ostack.pop()


def roll(ctxt, ostack):
    """
    any(n-1) ... any(0) n j **roll** any((j-1) mod(n)) ... any(0) any(n-1) ... any((j)mod(n))


    performs a circular shift of the objects any(n-1) through any(0) on the operand stack
    by the amount j. Positive j indicates upward motion on the stack, whereas negative
    j indicates downward motion.

    n must be a nonnegative integer and j must be an integer. **roll** first removes these
    operands from the stack; there must be at least n additional elements. It then performs
    a circular shift of these n elements by j positions.

    If j is positive, each shift consists of removing an element from the top of the stack
    and inserting it between element n - 1 and element n of the stack, moving all intervening
    elements one level higher on the stack. If j is negative, each shift consists
    of removing element n - 1 of the stack and pushing it on the top of the stack,
    moving all intervening elements one level lower on the stack.

    **Examples**
        (a) (b) (c) 3 -1 **roll**   -> (b) (c) (a)
        (a) (b) (c) 3 1 **roll**    -> (c) (a) (b)
        (a) (b) (c) 3 0 **roll**    -> (a) (b) (c)

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **exch**, **index**, **copy**, **pop**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, roll.__name__)
    # 2. TYPECHECK - Check operand types (n j)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, roll.__name__)
    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, roll.__name__)

    if ostack[-2].val < 0 or len(ostack) < ostack[-2].val + 2:
        return ps_error.e(ctxt, ps_error.RANGECHECK, roll.__name__)

    n = ostack[-2].val
    j = ostack[-1].val
    ostack.pop()
    ostack.pop()

    for _ in range(abs(j)):
        if j < 0:
            ostack.append(ostack.pop(-n))
        else:
            ostack.insert(-(n - 1), ostack.pop())


def printostck(ctxt, ostack):
    """
    – **printostck** –

    PostForge extension. Prints the entire operand stack contents to
    stdout for debugging purposes. The stack is not modified.
    """
    print(ostack)
