# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import copy
import string

from ..core import error as ps_error
from ..core import types as ps


def cvi(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
       num **cvi** int
    string **cvi** int


    (convert to integer) takes an integer, real, or string object from the stack and
    produces an integer result. If the operand is an integer, **cvi** simply returns it. If the
    operand is a real number, it truncates any fractional part (that is, rounds it toward
    0) and converts it to an integer. If the operand is a string, **cvi** invokes the equivalent
    of the token operator to interpret the characters of the string as a number
    according to the PostScript syntax rules. If that number is a real number, **cvi** converts
    it to an integer. A **rangecheck** error occurs if a real number is too large to
    convert to an integer. (See the round, **truncate**, **floor**, and **ceiling** operators, which
    remove fractional parts without performing type conversion.)

    **Examples**
        (3.3E1) **cvi**     -> 33
        –47.8 **cvi**       -> –47
        520.9 **cvi**       -> 520

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **syntaxerror**, **typecheck**,
                **undefinedresult**
    **See Also**:   **cvr**, **ceiling**, **floor**, **round**, **truncate**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cvi.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in {ps.T_INT, ps.T_REAL, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cvi.__name__)
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, cvi.__name__)

    if ostack[-1].TYPE == ps.T_INT:
        return

    elif ostack[-1].TYPE == ps.T_REAL:
        ostack[-1] = ps.Int(int(ostack[-1].val))

    else:
        try:
            ostack[-1] = ps.Int(int(float(ostack[-1].byte_string())))
        except (ValueError, UnicodeDecodeError):
            return ps_error.e(ctxt, ps_error.SYNTAXERROR, cvi.__name__)


def cvlit(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    any **cvlit** any


    (convert to literal) makes the object on the top of the operand stack have
    the literal instead of the executable attribute.

    **Errors**:     **stackunderflow**
    **See Also**:   **cvx**, **xcheck**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cvlit.__name__)

    ostack[-1].attrib = ps.ATTRIB_LIT


def cvn(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    string **cvn** name


    (convert to name) converts the string operand to a name object that is lexically
    the same as the string. The name object is executable if the string was executable.

    **Examples**
        (abc) **cvn**       -> /abc
        (abc) **cvx** **cvn**   -> abc

    **Errors**:     **invalidaccess**, **limitcheck**, **stackunderflow**, **typecheck**
    **See Also**:   **cvs**, **type**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cvn.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cvn.__name__)
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, cvn.__name__)

    ostack[-1] = ps.Name(
        ostack[-1].byte_string(), attrib=ostack[-1].attrib, is_global=ctxt.vm_alloc_mode
    )


def cvr(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
       num **cvr** real
    string **cvr** real


    (convert to real) takes an integer, real, or string object and produces a real result.
    If the operand is an integer, **cvr** converts it to a real number. If the operand is a
    real number, **cvr** simply returns it. If the operand is a string, **cvr** invokes the equivalent
    of the token operator to interpret the characters of the string as a number
    according to the PostScript syntax rules. If that number is an integer, **cvr** converts
    it to a real number.

    **Errors**:     **invalidaccess**, **limitcheck**, **stackunderflow**, **syntaxerror**,
                **typecheck**, **undefinedresult**
    **See Also**:   **cvi**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cvr.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in {ps.T_INT, ps.T_REAL, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cvr.__name__)
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, cvr.__name__)

    if ostack[-1].TYPE == ps.T_REAL:
        return

    elif ostack[-1].TYPE == ps.T_INT:
        ostack[-1] = ps.Real(float(ostack[-1].val))

    else:
        try:
            ostack[-1] = ps.Real(float(ostack[-1].byte_string()))
        except (ValueError, UnicodeDecodeError):
            return ps_error.e(ctxt, ps_error.SYNTAXERROR, cvr.__name__)


def cvrs(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num radix string **cvrs** substring


    (convert with radix to string) produces a text representation of the number num
    in the specified radix, stores the text into string (overwriting some initial portion
    of its value), and returns a string object designating the substring actually used. If
    string is too small to hold the result of the conversion, a **rangecheck** error occurs.

    If radix is 10, **cvrs** produces the same result as the **cvs** operator when applied to
    either an integer or a real number. That is, it produces a signed integer or real
    token that conforms to the PostScript language syntax for that number.

    If radix is not 10, **cvrs** converts num to an integer, as if by the **cvi** operator. Then it
    treats the machine representation of that integer as an unsigned positive integer
    and converts it to text form according to the specific radix. The resulting text is
    not necessarily a valid number. However, if it is immediately preceded by the
    same radix and #, the combination is a valid PostScript token that represents the
    same number.

    **Examples**
        /temp 12 string def
        123 10 temp **cvrs**    -> (123)
        -123 10 temp **cvrs**   -> (-123)
        123.4 10 temp **cvrs**  -> (123.4)
        123 16 temp **cvrs**    -> (7B)
        -123 16 temp **cvrs**   -> (FFFFFF85)
        123.4 16 temp **cvrs**  -> (7B)

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **cvs**
    """

    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cvrs.__name__)

    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cvrs.__name__)

    if ostack[-2].TYPE != ps.T_INT or ostack[-3].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cvrs.__name__)
    
    if ostack[-1].access < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, cvrs.__name__)


    # only bases 2-36 allowed
    if ostack[-2].val < 2 or ostack[-2].val > 36:
        return ps_error.e(ctxt, ps_error.RANGECHECK, cvrs.__name__)

    base = ostack[-2].val
    num = ostack[-3].val

    bs = bytes(string.digits + string.ascii_uppercase, "ascii")

    if base == 10:
        val = bytes(str(num), "ascii")
    else:
        num = int(num)
        if num < 0:
            num = 4294967050 + abs(num)
        res = bytearray()
        while num:
            res.append(bs[num % base])
            num //= base
        val = res[::-1] or "0"

    if len(val) > ostack[-1].length:
        return ps_error.e(ctxt, ps_error.RANGECHECK, cvrs.__name__)

    st = copy.copy(ostack[-1])
    strings = ps.global_resources.global_strings if st.is_global else ctxt.local_strings
    strings[st.offset + st.start : st.offset + st.start + len(val)] = val
    st.length = len(val)

    ostack.pop()
    ostack.pop()
    ostack[-1] = st


def cvs(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    any string **cvs** substring


    (convert to string) produces a text representation of an arbitrary object any, stores
    the text into string (overwriting some initial portion of its value), and returns a
    string object designating the substring actually used. If string is too small to hold
    the result of the conversion, a **rangecheck** error occurs.

    If any is a number, **cvs** produces a string representation of that number. If any is a
    boolean value, **cvs** produces either the string true or the string false. If any is a
    string, **cvs** copies its contents into string. If any is a name or an operator, **cvs**
    produces the text representation of that name or the operator’s name. If any is any
    other type, **cvs** produces the text --nostringval--.

    If any is a real number, the precise format of the result string is implementation
    dependent and not under program control. For example, the value 0.001 might be
    represented as 0.001 or as 1.0E-3.

    **Examples**
        /str 20 string def
        123 456 add str **cvs**     -> (579)
        mark str **cvs**            -> (--nostringval--)

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **cvi**, **cvr**, **string**, **type**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cvs.__name__)

    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cvs.__name__)

    if ostack[-1].access < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, cvs.__name__)

    the_string = ostack[-1]
    obj = ostack[-2]
    sub_string = copy.copy(the_string)
    sub_string.is_substring = True
    dst_strings = (
        ps.global_resources.global_strings
        if the_string.is_global
        else ps.contexts[the_string.ctxt_id].local_strings
    )

    if obj.TYPE in {ps.T_INT, ps.T_REAL, ps.T_BOOL}:
        s = obj.__str__()
        # s = str(obj.val).lower()
        if len(s) > the_string.length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, cvs.__name__)
        index = the_string.offset + the_string.start
        dst_strings[index : index + len(s)] = bytes(s, "ascii")
        sub_string.length = len(s)

    elif obj.TYPE == ps.T_STRING:
        if obj.length > the_string.length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, cvs.__name__)
        index = the_string.offset + the_string.start
        src_index = obj.offset + obj.start
        src_strings = (
            ps.global_resources.global_strings
            if obj.is_global
            else ps.contexts[obj.ctxt_id].local_strings
        )
        dst_strings[index : index + obj.length] = src_strings[
            src_index : src_index + obj.length
        ]
        sub_string.length = obj.length

    elif obj.TYPE == ps.T_NAME:
        if len(obj.val) > the_string.length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, cvs.__name__)
        index = the_string.offset + the_string.start
        dst_strings[index : index + len(obj.val)] = obj.val
        sub_string.length = len(obj.val)

    elif obj.TYPE == ps.T_OPERATOR:
        if len(obj.val.__name__) > the_string.length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, cvs.__name__)
        index = the_string.offset + the_string.start
        the_name = (
            obj.val.__name__[3:]
            if obj.val.__name__.startswith("ps_")
            else obj.val.__name__
        )
        dst_strings[index : index + len(the_name)] = bytes(the_name, "ascii")
        sub_string.length = len(the_name)

    else:
        s = b"--nostringval--"
        if len(s) > the_string.length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, cvs.__name__)
        index = the_string.offset + the_string.start
        dst_strings[index : index + len(s)] = s
        sub_string.length = len(s)

    ostack.pop()
    ostack[-1] = sub_string


def cvx(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    any **cvx** any


    (convert to executable) makes the object on the top of the operand stack
    have the executable instead of the literal attribute.

    **Errors**:     **stackunderflow**
    **See Also**:   **cvlit**, **xcheck**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cvx.__name__)

    ostack[-1].attrib = ps.ATTRIB_EXEC


def executeonly(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
          array **executeonly** array
    **packedarray** **executeonly** **packedarray**
           file **executeonly** file
         string **executeonly** string


    reduces the access attribute of an array, packed array, file, or string object to
    execute-only (see Section 3.3.2, "Attributes of Objects"). Access can only be reduced
    by this operator, never increased. When an object is execute-only, its value
    cannot be read or modified explicitly by PostScript operators (an **invalidaccess**
    error will result), but it can still be executed by the PostScript interpreter,
    for example, by invoking it with the exec operator.

    **executeonly** affects the access attribute only of the object that it returns. If there
    are other composite objects that share the same value, their access attributes are
    unaffected.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **rcheck**, **wcheck**, **xcheck**, **readonly**, **noaccess**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, executeonly.__name__)

    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_FILE, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, executeonly.__name__)

    if ostack[-1].access < ps.ACCESS_EXECUTE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, executeonly.__name__)

    ostack[-1].access = ps.ACCESS_EXECUTE_ONLY
    pass


def noaccess(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
          array **noaccess** array
    **packedarray** **noaccess** **packedarray**
           dict **noaccess** dict
           file **noaccess** file
         string **noaccess** string


    reduces the access attribute of an array, packed array, dictionary, file, or string object
    to none (see Section 3.3.2, “Attributes of Objects”). The value of a no-access
    object cannot be executed or accessed directly by PostScript operators. No-access
    objects are of no use to PostScript programs, but serve certain internal purposes
    that are not documented in this book.

    For an array, packed array, file, or string object, **noaccess** affects the access attribute
    only of the object that it returns. If there are other objects that share the
    same value, their access attributes are unaffected. However, in the case of a dictionary,
    **noaccess** affects the value of the object, so all dictionary objects sharing
    the same dictionary are affected. Applying **noaccess** to a dictionary whose access
    is already read-only causes an **invalidaccess** error.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **rcheck**, **wcheck**, **xcheck**, **readonly**, **executeonly**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, noaccess.__name__)

    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_DICT, ps.T_FILE, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, noaccess.__name__)

    if ostack[-1].TYPE == ps.T_DICT:
        # Level 1 compatibility: Always allow noaccess on dictionaries
        # This maintains compatibility with old Type 1 fonts
        if 'BlueValues' in ostack[-1].val or 'FontBBox' in ostack[-1].val:
            # Level 1 compatibility: Always allow noaccess on dictionaries
            # This maintains compatibility with old Type 1 fonts
            ostack[-1].access = ps.ACCESS_READ_ONLY
        # elif ostack[-1].val[b"__access__"] == ps.Int(ps.ACCESS_READ_ONLY):
        elif ostack[-1].access == ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, noaccess.__name__)
        else:
            ostack[-1].access = ps.ACCESS_NONE
    else:
        # Create a copy for non-dictionary objects
        obj_copy = copy.copy(ostack[-1])
        obj_copy.access = ps.ACCESS_NONE
        ostack[-1] = obj_copy


def readonly(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
          array **readonly** array
    **packedarray** **readonly** **packedarray**
           dict **readonly** dict
           file **readonly** file
         string **readonly** string


    reduces the access attribute of an array, packed array, dictionary, file, or string object
    to read-only (see Section 3.3.2, "Attributes of Objects"). Access can only be
    reduced by this operator, never increased. When an object is read-only, its value
    cannot be modified by PostScript operators (an **invalidaccess** error will result),
    but it can still be read by operators or executed by the PostScript interpreter.

    For an array, packed array, file, or string object, **readonly** affects the access attribute
    only of the object that it returns. If there are other objects that share the
    same value, their access attributes are unaffected. However, in the case of a dictionary,
    **readonly** affects the value of the object, so all dictionary objects sharing
    the same dictionary are affected.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **executeonly**, **noaccess**, **rcheck**, **wcheck**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, readonly.__name__)

    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_DICT, ps.T_FILE, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, readonly.__name__)

    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, readonly.__name__)

    if ostack[-1].TYPE == ps.T_DICT:
        ostack[-1].access = ps.ACCESS_READ_ONLY
    else:
        ostack[-1].access = ps.ACCESS_READ_ONLY
        pass
        # # Create a copy for non-dictionary objects
        # obj_copy = copy.copy(ostack[-1])
        # obj_copy.access = ps.ACCESS_READ_ONLY
        # ostack[-1] = obj_copy


def rcheck(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
          array **rcheck** bool
    **packedarray** **rcheck** bool
           dict **rcheck** bool
           file **rcheck** bool
         string **rcheck** bool


    tests whether the operand’s access permits its value to be read explicitly by Post-
    Script operators. It returns true if the operand’s access is unlimited or read-only,
    or false otherwise.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **executeonly**, **noaccess**, **readonly**, **wcheck**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rcheck.__name__)

    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_DICT, ps.T_FILE, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, rcheck.__name__)

    ostack[-1] = ps.Bool(ostack[-1].access >= ps.ACCESS_READ_ONLY)


def ps_type(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    any **type** name


    returns a name object that identifies the **type** of the object any. The possible
    names that **type** can return are as follows:

        **arraytype**                       **nametype**
        **booleantype**                     **nulltype**
        **dicttype**                        **operatortype**
        **filetype**                        **packedarraytype** (LanguageLevel 2)
        **fonttype**                        **realtype**
        **gstatetype** (LanguageLevel 2)    **savetype**
        **integertype**                     **stringtype**
        **marktype**

    The name **fonttype** identifies an object of **type** fontID. It has nothing to do with a
    font dictionary, which is identified by **dicttype** the same as any other dictionary.

    The returned name has the executable attribute. This makes it convenient to perform
    **type**-dependent processing of an object simply by executing the name returned
    by **type** in the context of a dictionary that defines all the **type** names to
    have procedure values (this is how the == operator works).

    The set of types is subject to enlargement in future revisions of the language. A
    program that examines the types of arbitrary objects should be prepared to behave
    reasonably if **type** returns a name that is not in this list.

    **Errors**:     **stackunderflow**
    """
    op = "type"

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    ostack[-1] = ps.Name(
        ctxt.type_names[ostack[-1].TYPE],
        attrib=ps.ATTRIB_EXEC,
        is_global=ctxt.vm_alloc_mode,
    )


def wcheck(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
          array **wcheck** bool
    **packedarray** **wcheck** false
           dict **wcheck** bool
           file **wcheck** bool
         string **wcheck** bool


    tests whether the operand’s access permits its value to be written explicitly by
    PostScript operators. It returns true if the operand’s access is unlimited,
    or false otherwise.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **rcheck**, **readonly**, **executeonly**, **noaccess**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, wcheck.__name__)

    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_DICT, ps.T_FILE, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, wcheck.__name__)

    ostack[-1] = ps.Bool(ostack[-1].access == ps.ACCESS_UNLIMITED)


def xcheck(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    any **xcheck** bool


    tests whether the operand has the executable or the literal attribute, returning true
    if it is executable or false if it is literal. This has nothing to do with the object’s
    access attribute (for example, execute-only). See Section 3.3.2, "Attributes of Objects."

    **Errors**:     **stackunderflow**
    **See Also**:   **cvx**, **cvlit**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, xcheck.__name__)

    if ostack[-1].attrib == ps.ATTRIB_EXEC:
        ostack[-1] = ps.Bool(True)
    else:
        ostack[-1] = ps.Bool(False)
