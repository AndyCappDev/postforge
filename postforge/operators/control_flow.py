# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript compound operators
"""

from copy import copy

from . import dict as ps_dict
from ..core import error as ps_error
from ..core import types as ps


def ps_copy(ctxt, ostack):
    """
        any(0) ... any(n) n **copy** any₁ ... any(n) any(1) ... any(n)

          array₁ array₂ **copy** subarray₂
            dict₁ dict₂ **copy** dict₂
        string₁ string₂ **copy** substring₂
    packedarray₁ array₂ **copy** subarray₂
        gstate₁ gstate₂ **copy** gstage₂


    performs two entirely different functions, depending on the type of the topmost
    operand.

    In the first form, where the top element on the operand stack is a nonnegative integer
    n, **copy** pops n from the stack and duplicates the top n elements on the stack
    as shown above. This form of **copy** operates only on the objects themselves, not
    on the values of composite objects.

    **Examples**
        (a) (b) (c) 2 **copy**      -> (a) (b) (c) (b) (c)
        (a) (b) (c) 0 **copy**      -> (a) (b) (c)

    In the other forms, **copy** copies all the elements of the first composite object into
    the second. The composite object operands must be of the same type, except that
    a packed array can be copied into an array (and only into an array—**copy** cannot
    **copy** into packed arrays, because they are read-only). This form of **copy** copies the
    value of a composite object. This is quite different from dup and other operators
    that **copy** only the objects themselves (see Section 3.3.1, "Simple and Composite
    Objects"). However, **copy** performs only one level of copying. It does not apply
    recursively to elements that are themselves composite objects; instead, the values
    of those elements become shared.

    In the case of arrays or strings, the length of the second object must be at least as
    great as the first; **copy** returns the initial subarray or substring of the second operand
    into which the elements were copied. Any remaining elements of array₂ or
    string₂ are unaffected.

    In the case of dictionaries, LanguageLevel 1 requires that dict₂ have a length (as returned
    by the length operator) of 0 and a maximum capacity (as returned by the
    **maxlength** operator) at least as great as the length of dict₁. LanguageLevels 2 and 3
    do not impose this restriction, since dictionaries can expand when necessary.

    The literal/executable and access attributes of the result are normally the same as
    those of the second operand. However, in LanguageLevel 1 the access attribute of
    dict₂ is copied from that of dict₁.

    If the value of the destination object is in global VM and any of the elements copied
    from the source object are composite objects whose values are in local VM, an
    **invalidaccess** error occurs (see Section 3.7.2, "Local and Global VM").

    **Example**
        /a1 [1 2 3] def
        a1 dup length array **copy** -> [1 2 3]

    **Errors**:     **invalidaccess**, **rangecheck**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **dup**, **get**, **put**, **putinterval**
    """
    op = "copy"

    if len(ostack) < 1:
        ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
        return
    
    if ostack[-1].TYPE not in {ps.T_INT, ps.T_STRING, ps.T_ARRAY, ps.T_DICT, ps.T_GSTATE}:
        ps_error.e(ctxt, ps_error.TYPECHECK, op)
        return

    if ostack[-1].TYPE == ps.T_INT:
        # 1. STACKUNDERFLOW - Already checked above
        # 2. TYPECHECK - Already checked (INT case)
        
        # 4. RANGECHECK - Check value range
        if ostack[-1].val < 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, op)

        # 5. STACKOVERFLOW - Check result stack space
        if ostack[-1].val + 1 > len(ostack):
            ps_error.e(ctxt, ps_error.STACKOVERFLOW, op)
            return

        copies = ostack[-1].val
        ostack.pop()
        pad = 0
        for i in range(-copies, 0, 1):
            ostack.append(copy(ostack[i - pad]))
            pad += 1
        return None

    # 1. STACKUNDERFLOW - Check stack depth for non-integer copy
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    # 2. TYPECHECK - Packed arrays not supported for copy
    if ostack[-1].TYPE == ps.T_PACKED_ARRAY:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    
    # 3. INVALIDACCESS - Check destination access
    if ostack[-1].access < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)

    # 3. INVALIDACCESS - Check source access
    if ostack[-2].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)

    if ostack[-2].TYPE in {ps.T_DICT, ps.T_STRING, ps.T_GSTATE}:
        if not isinstance(ostack[-1], type(ostack[-2])):
            return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    elif ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    # Array
    if ostack[-1].TYPE == ps.T_ARRAY:
        if ostack[-2].TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, op)
        src = ostack[-2]
        dst = ostack[-1]

        if dst.length < src.length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, op)

        # first check for invalid access
        dst_i = dst.start
        for src_i in range(src.start, src.start + src.length, 1):
            if (
                dst.is_global
                and src.val[src_i].is_composite
                and not src.val[src_i].is_global
            ):
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)
            dst_i += 1

        # now copy the items
        if not dst.is_global and hasattr(dst, '_cow_check'):
            dst._cow_check()
        dst_i = dst.start
        for src_i in range(src.start, src.start + src.length, 1):
            dst.val[dst_i] = copy(src.val[src_i])
            dst_i += 1

        sub_arr = ps.Array(ctxt.id, is_global=dst.is_global)
        sub_arr.val = dst.val
        sub_arr.length = src.length
        
        # Update local_refs to track the correct val after reassignment
        if not sub_arr.is_global and sub_arr.ctxt_id is not None:
            ps.contexts[sub_arr.ctxt_id].local_refs[sub_arr.created] = sub_arr.val
        ostack.pop()
        ostack[-1] = sub_arr

    # String
    elif ostack[-1].TYPE == ps.T_STRING or ostack[-2].TYPE == ps.T_STRING:
        if ostack[-1].TYPE != ostack[-2].TYPE:
            return ps_error.e(ctxt, ps_error.TYPECHECK, op)

        src = ostack[-2]
        dst = ostack[-1]

        if dst.length < src.length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, op)
        dst_strings = (
            ps.global_resources.global_strings
            if dst.is_global
            else ps.contexts[dst.ctxt_id].local_strings
        )
        src_strings = (
            ps.global_resources.global_strings
            if src.is_global
            else ps.contexts[src.ctxt_id].local_strings
        )
        dst_strings[dst.offset + dst.start : dst.offset + dst.start + src.length] = (
            src_strings[src.offset + src.start : src.offset + src.start + src.length]
        )

        # s = ps.String()
        sub_str = ps.String(
            ctxt.id, dst.offset, src.length, start=dst.start, is_global=dst.is_global
        )
        ostack.pop()
        ostack[-1] = sub_str

    # Dictionary
    elif ostack[-1].TYPE == ps.T_DICT:
        src = ostack[-2]
        dst = ostack[-1]

        for src_key, src_val in src.val.items():
            if dst.is_global and src_val.is_composite and not src_val.is_global:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)

        # now copy the items
        if not dst.is_global and hasattr(dst, '_cow_check'):
            dst._cow_check()
        for src_key, src_val in src.val.items():
            dst.val[src_key] = copy(src_val)

        if len(dst.val) <= dst.max_length:
            dst.max_length += 10

        ostack[-1], ostack[-2] = ostack[-2], ostack[-1]
        ostack.pop()

    elif ostack[-1].TYPE == ps.T_GSTATE:
        # not implimented yet
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    return None


def get(ctxt, ostack):
    """
          array index **get** any
    **packedarray** index **get** any
             dict key **get** any
         string index **get** int


    returns a single element from the value of the first operand. If the first operand is
    an array, a packed array, or a string, **get** treats the second operand as an index and
    returns the element identified by the index, counting from 0. index must be in the
    range 0 to n - 1, where n is the length of the array, packed array, or string. If it is
    outside this range, a **rangecheck** error occurs.

    If the first operand is a dictionary, **get** looks up the second operand as a key in the
    dictionary and returns the associated value. If the key is not present in the dictionary,
    an **undefined** error occurs.

    **Examples**
        [31 41 59] 0 **get**                -> 31
        [0 (string1) [ ] {add 2 div}]           % A mixed-type array
            2 **get**                       -> [ ]  % An empty array

        /mykey (myvalue) def
        **currentdict** /mykey **get**          -> (myvalue)

        (abc) 1 **get**                     -> 98   % Character code for b
        (a) 0 **get**                       -> 97

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **put**, **getinterval**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, get.__name__)

    # 2. TYPECHECK - Check composite object type
    if ostack[-2].TYPE not in ps.COMPOSITE_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, get.__name__)

    if ostack[-2].TYPE == ps.T_DICT:
        obj = ps_dict.lookup(ctxt, ostack[-1], ostack[-2])
        # lookup returns a copy of the object, so we dont need to copy it here
        if not obj:
            return ps_error.e(ctxt, ps_error.UNDEFINED, str(ostack[-1].val))
    else:
        # ps.Array, ps.PackedArray, or ps.String
        # Index must be an integer (REAL indices raise typecheck per GhostScript behavior)
        if ostack[-1].TYPE != ps.T_INT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, get.__name__)
        index = ostack[-1]
        success, obj = ostack[-2].get(index)
        if not success:
            return ps_error.e(ctxt, obj, get.__name__)

    ostack.pop()
    ostack[-1] = obj
    return None


def getinterval(ctxt, ostack):
    """
          array index count **getinterval** subarray
    **packedarray** index count **getinterval** subarray
         string index count **getinterval** substring


    creates a new array, packed array, or string object whose value consists of some
    subsequence of the original array, packed array, or string. The subsequence consists
    of count elements starting at the specified index in the original object. The elements
    in the subsequence are shared between the original and new objects (see
    Section 3.3.1, "Simple and Composite Objects").

    The returned subarray or substring is an ordinary array, packed array, or string
    object whose length is count and whose elements are indexed starting at 0. The
    element at index 0 in the result is the same as the element at index in the original
    object.

    **getinterval** requires index to be a valid index in the original object and count to be
    a nonnegative integer such that index + count is not greater than the length of the
    original object.

    **Examples**
        [9 8 7 6 5] 1 3 **getinterval**     -> [8 7 6]
        (abcde) 1 3 **getinterval**         -> (bcd)
        (abcde) 0 0 **getinterval**         -> ()       % An empty string

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **get**, **putinterval**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, getinterval.__name__)
    
    # 2. TYPECHECK - Check operand types (index count composite)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, getinterval.__name__)
    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, getinterval.__name__)
    if ostack[-3].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, getinterval.__name__)

    success, obj = ostack[-3].getinterval(ostack[-2], ostack[-1])
    if not success:
        return ps_error.e(ctxt, obj, getinterval.__name__)

    ostack.pop()
    ostack.pop()
    ostack[-1] = obj
    return None


def length(ctxt, ostack):
    """
          array length int
    packedarray length int
           dict length int
         string length int
           name length int


    returns the number of elements in the value of its operand if the operand is an
    array, a packed array, or a string. If the operand is a dictionary, length returns the
    current number of entries it contains (as opposed to its maximum capacity, which
    is returned by **maxlength**). If the operand is a name object, the length returned is
    the number of characters in the text string that defines it.

    **Examples**
        [1 2 4] length                      -> 3
        [] length                           -> 0    % An array of zero length

        /ar 20 array def
        ar length                           -> 20

        /mydict 5 dict def
        mydict length                       -> 0

        mydict /firstkey (firstvalue) put
        mydict length                       -> 1

        (abc\\n) length                      -> 4    % Newline (\\n) is one character
        () length                           -> 0    % No characters between ( and )
        /foo length                         -> 3

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **maxlength**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, length.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_STRING, ps.T_DICT, ps.T_NAME}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, length.__name__)
    
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, length.__name__)

    ostack[-1] = ostack[-1].len()
    return None


def put(ctxt, ostack):
    """
     array index any **put** -
        dict key any **put** -
    string index int **put** -


    replaces a single element of the value of the first operand. If the first operand is an
    array or a string, **put** treats the second operand as an index and stores the third
    operand at the position identified by the index, counting from 0. index must be in
    the range 0 to n - 1, where n is the length of the array or string. If it is outside this
    range, a **rangecheck** error occurs.

    If the first operand is a dictionary, **put** uses the second operand as a key and the
    third operand as a value, and stores this key-value pair into dict. If key is already
    present as a key in dict, **put** simply replaces its value by any; otherwise, **put** creates
    a new entry for key and associates any with it. In LanguageLevel 1, if dict is already
    full, a **dictfull** error occurs.

    If the value of array or dict is in global VM and any is a composite object whose
    value is in local VM, an **invalidaccess** error occurs (see Section 3.7.2, "Local and
    Global VM").

    **Examples**
        /ar [5 17 3 8] def
        ar 2 (abcd) **put**
        ar                  -> [5 17 (abcd) 8]

        /d 5 dict def
        d /abc 123 **put**
        d { } **forall**        -> /abc 123

        /st (abc) def
        st 0 65 **put**                     % 65 is the ASCII code for the character A
        st                  -> (Abc)

    **Errors**:     **dictfull**, **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **get**, **putinterval**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, put.__name__)

    # 2. TYPECHECK - Check composite object type
    if ostack[-3].TYPE not in {ps.T_ARRAY, ps.T_DICT, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, put.__name__)

    # 2. TYPECHECK - Array/String index must be integer or real (real truncated to int)
    if ostack[-3].TYPE in {ps.T_ARRAY, ps.T_STRING}:
        if ostack[-2].TYPE == ps.T_INT:
            index = ostack[-2]
        elif ostack[-2].TYPE == ps.T_REAL:
            index = ps.Int(int(ostack[-2].val))
        else:
            return ps_error.e(ctxt, ps_error.TYPECHECK, put.__name__)
    else:
        # Dictionary - key can be various types
        if ostack[-2].TYPE not in {ps.T_STRING, ps.T_NAME, ps.T_INT, ps.T_REAL, ps.T_BOOL}:
            return ps_error.e(ctxt, ps_error.TYPECHECK, put.__name__)
        index = ostack[-2]

    # 3. INVALIDACCESS - Check VM access rules
    if ostack[-3].is_global and ostack[-1].is_composite and not ostack[-1].is_global:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, put.__name__)

    success, error_code = ostack[-3].put(index, ostack[-1])
    if not success:
        return ps_error.e(ctxt, error_code, put.__name__)

    # ostack[-3].put(ostack[-2], ostack[-1])

    # if this is a global composite object referenced from a local array
    # then add it to the global_refs dict
    if ostack[-1].is_global and ostack[-1].is_composite and not ostack[-3].is_global:
        ctxt.global_refs[ostack[-1].created] = ostack[-1].val

    ostack.pop()
    ostack.pop()
    ostack.pop()


def putinterval(ctxt, ostack):
    """
          array₁ index array₂ **putinterval** -
    array₁ index packedarray₂ **putinterval** -
        string₁ index string₂ **putinterval** -


    replaces a subsequence of the elements of the first operand by the entire contents
    of the third operand. The subsequence that is replaced begins at index in the first
    operand; its length is the same as the length of the third operand.

    The objects are copied from the third operand to the first, as if by a sequence of
    individual **get** and **put** operations. In the case of arrays, if the copied elements are
    themselves composite objects, the values of those objects are shared between
    array₂ and array₁ (see Section 3.3.1, "Simple and Composite Objects").

    **putinterval** requires index to be a valid index in array₁ or string₁ such that
    index plus the length of array₂ or string₂ is not greater than the length
    of array₁ or string₁.

    If the value of array₁ is in global VM and any of the elements copied from
    array₂ or packedarray₂ are composite objects whose values are in local VM,
    an **invalidaccess** error occurs (see Section 3.7.2, "Local and Global VM").

    **Examples**
        /ar [5 8 2 7 3] def
        ar 1 [(a) (b) (c)] **putinterval**
        ar                                  -> [5 (a) (b) (c) 3]

        /st (abc) def
        st 1 (de) **putinterval**
        st                                  -> (ade)

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **getinterval**, **put**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, putinterval.__name__)

    src = ostack[-1]
    index = ostack[-2]
    dst = ostack[-3]

    if src.TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, putinterval.__name__)

    if dst.TYPE not in {ps.T_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, putinterval.__name__)

    # 2. TYPECHECK - Index must be integer
    if index.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, putinterval.__name__)

    # 2. TYPECHECK - Array destination requires array source
    if dst.TYPE == ps.T_ARRAY:
        if src.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, putinterval.__name__)

    # 2. TYPECHECK - String destination requires string source
    if dst.TYPE == ps.T_STRING and src.TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, putinterval.__name__)

    success, error_code = dst.putinterval(src, index)
    if not success:
        return ps_error.e(ctxt, error_code, putinterval.__name__)

    ostack.pop()
    ostack.pop()
    ostack.pop()
    return None


def reverse(ctxt, ostack):
    """
    array|packedarray|string **reverse** array|packedarray|string

    PostForge extension. Reverses the elements of array, packedarray,
    or string in place and returns the modified object. The original
    object on the stack is replaced with the reversed version.

    **Errors**: **stackunderflow**, **typecheck**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, reverse.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, reverse.__name__)
    
    # 3. INVALIDACCESS - Default access level (no specific requirement)

    ostack[-1].reverse()
    return None
