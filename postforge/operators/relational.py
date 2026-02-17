# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from ..core import error as ps_error
from ..core import types as ps


def ps_and(ctxt, ostack):
    """
    bool₁ bool₂ **and** bool₃
      int₁ int₂ **and** int₃


    returns the logical conjunction of the operands if they are boolean. If the operands
    are integers, **and** returns the bitwise "**and**" of their binary representations.

    **Examples**
        true true **and**       -> true         % A complete truth table
        true false **and**      -> false
        false true **and**      -> false
        false false **and**     -> false

        99 1 **and**            -> 1
        52 7 **and**            -> 4

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **or**, **xor**, **not**, true, false
    """
    op = "and"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand types (bool₁|int₁ bool₂|int₂, must be same type)
    if ostack[-1].TYPE not in {ps.T_BOOL, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    if ostack[-2].TYPE not in {ps.T_BOOL, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    # Must be the same type
    if ostack[-1].TYPE != ostack[-2].TYPE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    if ostack[-2].TYPE == ps.T_BOOL:
        ostack[-2].val = ostack[-2].val and ostack[-1].val
    else:
        ostack[-2].val = ostack[-2].val & ostack[-1].val
    ostack.pop()


def bitshift(ctxt, ostack):
    """
    int₁ shift **bitshift** int₂


    shifts the binary representation of int1 left by shift bits and returns the
    result. Bits shifted out are lost; bits shifted in are 0. If shift is negative,
    a right shift by –shift bits is performed. This operation produces an arithmetically
    correct result only for positive values of int1. Both int₁ and shift must be integers.

    **Examples**
        7 3 **bitshift**        -> 56
        142 –3 **bitshift**     -> 17

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **and**, **or**, **xor**, **not**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, bitshift.__name__)
    # 2. TYPECHECK - Check operand types (int₁ shift)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, bitshift.__name__)
    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, bitshift.__name__)

    if ostack[-1].val >= 0:
        ostack[-2] = ps.Int(ostack[-2].val << ostack[-1].val)
    else:
        ostack[-2] = ps.Int(ostack[-2].val >> abs(ostack[-1].val))
    ostack.pop()


def eq(ctxt, ostack, op_name=None):
    """
    any₁ any₂ **eq** bool


    pops two objects from the operand stack and pushes true if they are equal, or false
    if not. The definition of equality depends on the types of the objects being compared.
    Simple objects are equal if their types and values are the same. Strings are
    equal if their lengths and individual elements are equal. Other composite objects
    (arrays and dictionaries) are equal only if they share the same value. Separate values
    are considered unequal, even if all the components of those values are the
    same.

    This operator performs some type conversions. Integers and real numbers can be
    compared freely: an integer and a real number representing the same mathematical
    value are considered equal by **eq**. Strings and names can likewise be compared
    freely: a name defined by some sequence of characters is equal to a string whose
    elements are the same sequence of characters.

    The literal/executable and access attributes of objects are not considered in
    comparisons between objects.

    **Examples**
        4.0 4 eq            -> true     % A real number and an integer may be equal
        (abc) (abc) eq      -> true     % Strings with equal elements are equal
        (abc) /abc eq       -> true     % A string and a name may be equal
        [1 2 3] dup eq      -> true     % An array is equal to itself
        [1 2 3] [1 2 3] eq  -> false    % Distinct array objects are not equal

    **Errors**:     **invalidaccess**, **stackunderflow**
    **See Also**:   **ne**, **le**, **lt**, **ge**, **gt**
    """

    if op_name is None:
        op_name = eq.__name__

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op_name)
    # 2. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, op_name)

    result = ostack[-2] == ostack[-1]
    # Ensure we return a PostScript Bool, not a Python bool
    b = result if isinstance(result, ps.Bool) else ps.Bool(result)

    ostack.pop()
    ostack[-1] = b


def ge(ctxt, ostack):
    """
          num₁ num₂ **ge** bool
    string₁ string₂ **ge** bool


    pops two objects from the operand stack and pushes true if the first operand is
    greater than or equal to the second, or false otherwise. If both operands are
    numbers, **ge** compares their mathematical values. If both operands are strings, **ge**
    compares them element by element, treating the elements as integers in the range
    0 to 255, to determine whether the first string is lexically greater than or equal
    to the second. If the operands are of other types or one is a string and the other
    is a number, a **typecheck** error occurs.

    **Examples**
        4.2 4 ge            -> true
        (abc) (d) ge        -> false
        (aba) (ab) ge       -> true
        (aba) (aba) ge      -> true

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **gt**, **eq**, **ne**, **le**, **lt**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ge.__name__)
    # 2. TYPECHECK - Check operand types (numeric or string)
    if ostack[-1].TYPE not in {ps.T_INT, ps.T_REAL, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ge.__name__)
    if ostack[-2].TYPE not in {ps.T_INT, ps.T_REAL, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ge.__name__)
    # For non-numeric types, both must be the same type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES and ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        if ostack[-1].TYPE != ostack[-2].TYPE:
            return ps_error.e(ctxt, ps_error.TYPECHECK, ge.__name__)
    # 3. INVALIDACCESS - Check string access permissions
    if ostack[-1].TYPE == ps.T_STRING:
        if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, ge.__name__)

    result = ostack[-2] >= ostack[-1]
    # Ensure we return a PostScript Bool, not a Python bool
    b = result if isinstance(result, ps.Bool) else ps.Bool(result)
    ostack.pop()
    ostack[-1] = b


def gt(ctxt, ostack):
    """
          num₁ num₂ **gt** bool
    string₁ string₂ **gt** bool


    pops two objects from the operand stack and pushes true if the first operand is
    greater than the second, or false otherwise. If both operands are numbers, **gt**
    compares their mathematical values. If both operands are strings, **gt** compares them
    element by element, treating the elements as integers in the range 0 to 255, to
    determine whether the first string is lexically greater than the second.
    If the operands are of other types or one is a string and the other is a number,
    a **typecheck** error occurs.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **ge**, **eq**, **ne**, **le**, **lt**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, gt.__name__)
    
    # 2. TYPECHECK - Check operand types (num1/string1 num2/string2)
    valid_types = {ps.T_INT, ps.T_REAL, ps.T_STRING}
    if ostack[-2].TYPE not in valid_types:
        return ps_error.e(ctxt, ps_error.TYPECHECK, gt.__name__)
    if ostack[-1].TYPE not in valid_types:
        return ps_error.e(ctxt, ps_error.TYPECHECK, gt.__name__)
    
    # Both non-numeric must be same type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES and ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        if ostack[-1].TYPE != ostack[-2].TYPE:
            return ps_error.e(ctxt, ps_error.TYPECHECK, gt.__name__)

    # 3. INVALIDACCESS - Check string access permissions
    if ostack[-1].TYPE == ps.T_STRING:
        if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, gt.__name__)

    result = ostack[-2] > ostack[-1]
    # Ensure we return a PostScript Bool, not a Python bool
    b = result if isinstance(result, ps.Bool) else ps.Bool(result)
    ostack.pop()
    ostack[-1] = b


def le(ctxt, ostack):
    """
          num₁ num₂ **le** bool
    string₁ string₂ **le** bool


    pops two objects from the operand stack and pushes true if the first operand is less
    than or equal to the second, or false otherwise. If both operands are numbers, **le**
    compares their mathematical values. If both operands are strings, **le** compares
    them element by element, treating the elements as integers in the range 0 to 255,
    to determine whether the first string is lexically less than or equal to the second. If
    the operands are of other types or one is a string and the other is a number, a
    **typecheck** error occurs.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **lt**, **eq**, **ne**, **ge**, **gt**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, le.__name__)
    
    # 2. TYPECHECK - Check operand types (num1/string1 num2/string2)
    valid_types = {ps.T_INT, ps.T_REAL, ps.T_STRING}
    if ostack[-2].TYPE not in valid_types:
        return ps_error.e(ctxt, ps_error.TYPECHECK, le.__name__)
    if ostack[-1].TYPE not in valid_types:
        return ps_error.e(ctxt, ps_error.TYPECHECK, le.__name__)
    
    # Both non-numeric must be same type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES and ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        if ostack[-1].TYPE != ostack[-2].TYPE:
            return ps_error.e(ctxt, ps_error.TYPECHECK, le.__name__)

    # 3. INVALIDACCESS - Check string access permissions
    if ostack[-1].TYPE == ps.T_STRING:
        if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, le.__name__)

    result = ostack[-2] <= ostack[-1]
    # Ensure we return a PostScript Bool, not a Python bool
    b = result if isinstance(result, ps.Bool) else ps.Bool(result)
    ostack.pop()
    ostack[-1] = b


def lt(ctxt, ostack):
    """
          num₁ num₂ **lt** bool
    string₁ string₂ **lt** bool


    pops two objects from the operand stack and pushes true if the first operand is less
    than the second, or false otherwise. If both operands are numbers, **lt** compares
    their mathematical values. If both operands are strings, **lt** compares them element
    by element, treating the elements as integers in the range 0 to 255, to determine
    whether the first string is lexically less than the second. If the operands are of
    other types or one is a string and the other is a number, a **typecheck** error occurs.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **le**, **eq**, **ne**, **ge**, **gt**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, lt.__name__)
    
    # 2. TYPECHECK - Check operand types (num1/string1 num2/string2)
    valid_types = {ps.T_INT, ps.T_REAL, ps.T_STRING}
    if ostack[-2].TYPE not in valid_types:
        return ps_error.e(ctxt, ps_error.TYPECHECK, lt.__name__)
    if ostack[-1].TYPE not in valid_types:
        return ps_error.e(ctxt, ps_error.TYPECHECK, lt.__name__)
    
    # Both non-numeric must be same type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES and ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        if ostack[-1].TYPE != ostack[-2].TYPE:
            return ps_error.e(ctxt, ps_error.TYPECHECK, lt.__name__)

    # 3. INVALIDACCESS - Check string access permissions
    if ostack[-1].TYPE == ps.T_STRING:
        if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, lt.__name__)

    result = ostack[-2] < ostack[-1]
    # Ensure we return a PostScript Bool, not a Python bool
    b = result if isinstance(result, ps.Bool) else ps.Bool(result)
    ostack.pop()
    ostack[-1] = b


def ne(ctxt, ostack):
    """
    any₁ any₂ **ne** bool


    pops two objects from the operand stack and pushes false if they are equal,
    or true if not. What it means for objects to be equal is presented in the
    description of the eq operator.

    **Errors**:     **invalidaccess**, **stackunderflow**
    **See Also**:   **eq**, **ge**, **gt**, **le**, **lt**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ne.__name__)

    if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, ne.__name__)

    if ostack[-1].TYPE == ps.T_DICT and ostack[-2].TYPE == ps.T_DICT:
        if ostack[-1].access() < ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, ne.__name__)

    eq(ctxt, ostack, op_name="ne")
    try:
        ostack[-1].val = not ostack[-1].val
    except AttributeError:
        pass


def ps_not(ctxt, ostack):
    """
    bool₁ **not** bool₂
     int₁ **not** int₂


    returns the logical negation of the operand if it is boolean. If the operand is an
    integer, **not** returns the bitwise complement (ones complement) of its binary
    representation.

    **Examples**
        true **not**        -> false    % A complete truth table
        false **not**       -> true
        52 **not**          -> -53

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **and**, **or**, **xor**, **if**
    """
    op = "not"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand type (bool1 or int1)
    if ostack[-1].TYPE not in {ps.T_BOOL, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    if ostack[-1].TYPE == ps.T_BOOL:
        ostack[-1].val = not ostack[-1].val
    else:
        ostack[-1].val = ~ostack[-1].val


def ps_or(ctxt, ostack):
    """
    bool₁ bool₂ **or** bool₃
      int₁ int₂ **or** int₃


    returns the logical disjunction of the operands if they are boolean. If the
    operands are integers, or returns the bitwise "inclusive or" of their binary
    representations.

    **Examples**
        true true or        -> true     % A complete truth table
        true false or       -> true
        false true or       -> true
        false false or      -> false

        17 5 or             -> 21

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **and**, **not**, **xor**
    """
    op = "or"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand types (bool1/int1 bool2/int2)
    if ostack[-2].TYPE not in {ps.T_BOOL, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    if ostack[-1].TYPE not in {ps.T_BOOL, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    # They must be the same type
    if ostack[-1].TYPE != ostack[-2].TYPE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    if ostack[-2].TYPE == ps.T_BOOL:
        ostack[-2].val = ostack[-2].val or ostack[-1].val
    else:
        ostack[-2].val = ostack[-2].val | ostack[-1].val
    ostack.pop()


def xor(ctxt, ostack):
    """
    bool₁ bool₂ **xor** bool₃
      int₁ int₂ **xor** int₃


    returns the logical "exclusive or" of the operands if they are boolean.
    If the operands are integers, **xor** returns the bitwise "exclusive or" of
    their binary representations.

    **Examples**
        true true **xor**       -> false    % A complete truth table
        true false **xor**      -> true
        false true **xor**      -> true
        false false **xor**     -> false

        7 3 **xor**             -> 4
        12 3 **xor**            -> 15

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **and**, **not**, **xor**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, xor.__name__)
    # 2. TYPECHECK - Check operand types (bool1/int1 bool2/int2)
    if ostack[-2].TYPE not in {ps.T_BOOL, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xor.__name__)
    if ostack[-1].TYPE not in {ps.T_BOOL, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xor.__name__)

    # They must be the same type
    if ostack[-1].TYPE != ostack[-2].TYPE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xor.__name__)

    if ostack[-2].TYPE == ps.T_BOOL:
        ostack[-2].val = ostack[-2].val != ostack[-1].val
    else:
        ostack[-2].val = ostack[-2].val ^ ostack[-1].val
    ostack.pop()
