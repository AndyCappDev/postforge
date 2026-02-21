# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import re
import time

from . import dict as ps_dict
from ..core import error as ps_error
from ..core import types as ps
from . import control as ps_control


def _bind(ctxt, arr):
    """recursive **bind**"""
    for i, obj in enumerate(arr.val):
        if obj.TYPE in ps.ARRAY_TYPES and obj.attrib == ps.ATTRIB_EXEC and obj.access > ps.ACCESS_READ_ONLY:
            _bind(ctxt, obj)
            # make the nested procedure readonly
            if obj.access > ps.ACCESS_READ_ONLY:
                obj.access = ps.ACCESS_READ_ONLY
        elif obj.TYPE == ps.T_NAME and obj.attrib == ps.ATTRIB_EXEC:
            # look it up
            op = ps_dict.lookup(ctxt, obj)
            if op and op.TYPE == ps.T_OPERATOR:
                arr.put(ps.Int(i), op)
    arr.is_bound = True


def bind(ctxt, ostack):
    """
    proc **bind** proc


    replaces executable operator names in proc by their values. For each element of
    proc that is an executable name, **bind** looks up the name in the context of the current
    dictionary stack as if by the load operator. If the name is found and its value
    is an operator object, **bind** replaces the name with the operator in proc. If the
    name is not found or its value is not an operator, **bind** does not make a change.

    For each procedure object contained within proc, **bind** applies itself recursively to
    that procedure, makes the procedure read-only, and stores it back into proc. **bind**
    applies to both arrays and packed arrays, but it treats their access attributes differently.
    It will ignore a read-only array; that is, it will neither **bind** elements of the
    array nor examine nested procedures. On the other hand, **bind** will operate on a
    packed array (which always has read-only or even more restricted access), disregarding
    its access attribute. No error occurs in either case.

    The effect of **bind** is that all operator names in proc and in procedures nested
    within proc to any depth become tightly bound to the operators themselves. During
    subsequent execution of proc, the interpreter encounters the operators themselves
    rather than their names. See Section 3.12, "Early Name Binding."

    In LanguageLevel 3, if the user parameter **IdiomRecognition** is true, then after replacing
    executable names with operators, **bind** compares proc with every template
    procedure defined in instances of the **IdiomSet** resource category. If it finds a
    match, it returns the associated substitute procedure. See Section 3.12.1, "**bind**
    Operator."

    **Errors**:     **typecheck**
    **See Also**:   **load**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, bind.__name__)

    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, bind.__name__)

    _bind(ctxt, ostack[-1])


_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')


def _print_help(op):
    doc = op.__doc__
    if doc:
        doc = _BOLD_RE.sub(r'\033[1m\1\033[0m', doc)
    print(doc)


def help(ctxt, ostack):
    """
    name **help** -


    Prints **help** for the builtin PostScript command specified by name.

    name must be a literal **nametype** object and must describe the name of a builtin
    operator. If name's value is not of **operatortype**, a typecheck error occurs.

    **Errors**:     **typecheck**, **undefined**
    """

    if len(ostack) < 1:
        _print_help(help)
        return

    if ostack[-1].TYPE != ps.T_NAME:
        return ps_error.e(ctxt, ps_error.TYPECHECK, help.__name__)

    obj = ps_dict.lookup(ctxt, ostack[-1])
    if obj is None:
        return ps_error.e(ctxt, ps_error.UNDEFINED, ostack[-1].val.decode("ascii"))
    if obj.TYPE != ps.T_OPERATOR:
        return ps_error.e(ctxt, ps_error.TYPECHECK, help.__name__)

    ostack.pop()
    _print_help(obj.val)


def echo(ctxt, ostack):
    """
    bool **echo** –

    Specifies whether the special files %lineedit and %statementedit are to
    copy characters from the standard input file to the standard output file.
    This affects only the behavior of executive; it does not apply to normal
    communication with the PostScript interpreter. **echo** is not defined in
    products that do not support executive.

    Stack: bool **echo** –
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, echo.__name__)

    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, echo.__name__)

    ctxt.echo = ostack.pop().val


def _setinteractivepaint(ctxt, ostack):
    """Internal operator: enable/disable live paint callback for executive mode."""
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ".setinteractivepaint")
    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".setinteractivepaint")
    ctxt._interactive_painting = ostack.pop().val
    # When enabling, trigger an initial refresh to show the Qt window
    if ctxt._interactive_painting and ctxt.on_paint_callback:
        ctxt.on_paint_callback(ctxt, None)


def usertime(ctxt, ostack):
    """
    - **usertime** int


    returns the value of a clock that is incremented by 1 for every millisecond of execution
    by the PostScript interpreter. The value has no defined meaning in terms
    of calendar time or time of day; its only use is interval timing. The accuracy and
    stability of the clock depends on the environment in which the PostScript interpreter
    is running. As the time value becomes greater than the largest integer allowed
    in the implementation, it wraps to the smallest (most negative) integer.

    In an interpreter that supports multiple execution contexts, the value returned by
    **usertime** reports execution time on behalf of the current context only.

    **Errors**:     **stackoverflow**
    **See Also**:   **realtime**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, usertime.__name__)

    # return usertime in milliseconds
    ostack.append(ps.Int((time.perf_counter_ns() - ctxt.start_time) // 1000000))


def realtime(ctxt, ostack):
    """
    - **realtime** int

    returns the value of a clock that counts in real time, independently of the
    execution of the PostScript interpreter. The clock's starting value is arbitrary;
    it has no defined meaning in terms of calendar time. The unit of time represented
    by the **realtime** value is one millisecond. However, the rate at which it changes is
    implementation-dependent. As the time value becomes greater than the largest
    integer allowed in a particular implementation, it "wraps" to the smallest
    (most negative) integer.

    **Errors**:     **stackoverflow**
    **See Also**:   **usertime**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, realtime.__name__)

    # return realtime in milliseconds (wall-clock time since arbitrary start)
    ostack.append(ps.Int(time.time_ns() // 1000000))


def loopname(ctxt, ostack):
    """
    **loop** <**loopname**> name
    """

    # loop loopdesc name
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, loopname.__name__)

    if ostack[-1].TYPE != ps.T_LOOP:
        return ps_error.e(ctxt, ps_error.TYPECHECK, loopname.__name__)

    ostack[-1] = ps.Name(bytes(ostack[-1].__str__(), "ascii"))


def eexec(ctxt, ostack):
    """
    file **eexec** - | string **eexec** -
    
    PLRM Section 8.2: Creates a new file object that serves as a decryption **filter** on the 
    specified file or string. It pushes the new file object on the execution stack, making 
    it the current file for the PostScript interpreter. Subsequently, each time the 
    interpreter reads a character from this file, or a program reads explicitly from 
    **currentfile**, the decryption **filter** reads one character from the original file or 
    string and decrypts it.
    
    Before beginning execution, **eexec** pushes **systemdict** on the dictionary stack. This 
    ensures that the operators executed by the encrypted program have their standard 
    meanings. When the decryption **filter** file is closed either explicitly or implicitly, 
    the dictionary stack is popped.
    
    Adobe Type 1 Font Format **eexec** encryption:
    - Initial key R = 55665
    - Constants: c1 = 52845, c2 = 22719  
    - Random bytes to skip n = 4
    - Supports both binary and ASCII hexadecimal input
    
    **Errors**: **invalidaccess**, **ioerror**, **stackunderflow**, **typecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, eexec.__name__)
    # 2. TYPECHECK - Check operand type (file or string)
    if ostack[-1].TYPE not in {ps.T_FILE, ps.T_STRING} and not isinstance(ostack[-1], ps.StandardFileProxy):
        return ps_error.e(ctxt, ps_error.TYPECHECK, eexec.__name__)
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, eexec.__name__)
    
    source = ostack[-1]  # Peek at operand for validation before popping
    
    # Now pop the operand after all validation passes
    source = ostack.pop()
    
    # Handle StandardFileProxy (redirect to actual file)
    if type(source) is ps.StandardFileProxy:
        source = source.get_standard_file()
    
    # Convert string to file-like object if needed
    if source.TYPE == ps.T_STRING:
        # For strings, the content is the encrypted data to decrypt
        # Strings are always treated as binary encrypted data
        string_file = ps.File(ctxt.id, "eexec-string", "r", is_global=source.is_global)
        string_file.val = source  # Use the string as the file content
        string_file.is_real_file = False
        string_file._is_string_source = True  # Mark as string source
        source = string_file
    
    # Create the eexec decryption filter
    try:
        eexec_filter = ps.EexecDecryptionFilter(
            ctxt.id, 
            source, 
            ctxt,
            attrib=ps.ATTRIB_EXEC,
            is_global=source.is_global
        )
        
        # Push systemdict on the dictionary stack
        # the eexec filter pops it back off in its close() method
        gvm = ps.global_resources.get_gvm()
        if gvm and b"systemdict" in gvm.val:
            systemdict = gvm.val[b"systemdict"]
            ctxt.d_stack.append(systemdict)
            eexec_filter.systemdict_pushed = True
        
        # Push the decryption filter on execution stack to make it the current file
        # The PostScript interpreter will now read from this filter for subsequent operations
        ctxt.e_stack.append(eexec_filter)
        
    except Exception:
        return ps_error.e(ctxt, ps_error.IOERROR, eexec.__name__)


def exechistorystack(ctxt, ostack):
    """
    array **exechistorystack** subarray
    
    PLRM Section 8.2: Execution history stack operator (PostForge extension)
    Stack: array → subarray
    
    Fills the array operand with formatted strings representing the execution 
    history (most recent first) and returns a subarray containing the actual
    entries used. Similar to **dictstack** and **execstack** operators.
    
    **Errors**: **stackunderflow**, **typecheck**, **invalidaccess**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, exechistorystack.__name__)
    
    # 2. TYPECHECK - Check operand type (array)
    if ostack[-1].TYPE != ps.T_ARRAY:
        return ps_error.e(ctxt, ps_error.TYPECHECK, exechistorystack.__name__)
        
    # 3. INVALIDACCESS - Check access permission (array must be writable)  
    if ostack[-1].access < ps.ACCESS_UNLIMITED:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, exechistorystack.__name__)
    
    result_array = ostack[-1]  # Peek at array before popping
    
    # Pop the array operand after validation passes
    result_array = ostack.pop()
    
    # Convert execution history to formatted strings
    history_entries = list(ctxt.execution_history)  # Most recent first due to deque ordering
    
    # Fill array with formatted strings
    actual_count = 0
    for i, (input_obj, resolved_obj) in enumerate(history_entries):
        if i >= result_array.length:
            break
        # Format execution entry using debug format: {type} - {object}
        type_name = type(input_obj).__name__
        object_str = str(input_obj)
        formatted_str = f"{type_name} - {object_str}"
        formatted_bytes = formatted_str.encode()
        
        # Get the appropriate string buffer (global or local)
        strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings
        
        # Store string data in buffer and create String object
        offset = len(strings)
        strings.extend(formatted_bytes)
        length = len(formatted_bytes)
        
        result_array.val[i] = ps.String(ctxt.id, offset, length, is_global=ctxt.vm_alloc_mode)
        actual_count += 1
    
    # Create and return subarray of actual entries
    subarray = ps.Array(ctxt.id, is_global=result_array.is_global)
    if actual_count > 0:
        subarray.val = result_array.val[:actual_count]
        subarray.length = actual_count
    else:
        subarray.val = []  # Empty array if no history
        subarray.length = 0
    
    subarray.attrib = result_array.attrib
    subarray.access = result_array.access
    
    # Update local_refs to track the array val after assignment
    if not subarray.is_global and subarray.ctxt_id is not None:
        ps.contexts[subarray.ctxt_id].local_refs[subarray.created] = subarray.val
    
    ostack.append(subarray)


def _obj_to_string_detailed(obj):
    """Convert PostScript object to detailed string representation showing contents."""
    if obj is None:
        return "<null>"
    
    if obj.TYPE == ps.T_NAME:
        prefix = "/" if obj.attrib == ps.ATTRIB_LIT else ""
        try:
            name_str = obj.val.decode('ascii', errors='replace') if obj.val else "<unnamed>"
            return f"{prefix}{name_str}"
        except (AttributeError, UnicodeDecodeError):
            return f"{prefix}<name:{type(obj.val)}>"
    elif obj.TYPE == ps.T_OPERATOR:
        op_name = obj.val.__name__.replace('ps_', '') if hasattr(obj.val, '__name__') else "operator"
        return f"--{op_name}--"
    elif obj.TYPE == ps.T_INT:
        return str(obj.val)
    elif obj.TYPE == ps.T_REAL:
        return str(obj.val)
    elif obj.TYPE == ps.T_BOOL:
        return "true" if obj.val else "false"
    elif obj.TYPE == ps.T_STRING:
        if obj.attrib == ps.ATTRIB_EXEC:
            return f"--string--"
        else:
            if obj.val is None:
                return "()"
            try:
                content = obj.val.decode('ascii', errors='replace')[:50]  # Show more content
                return f"({content})" + ("..." if len(obj.val) > 50 else "")
            except (AttributeError, UnicodeDecodeError):
                return f"(string:{type(obj.val)})"
    elif obj.TYPE in ps.ARRAY_TYPES:
        if obj.attrib == ps.ATTRIB_EXEC:
            # Show procedure contents
            if hasattr(obj, 'val') and obj.val:
                try:
                    contents = []
                    for item in obj.val[obj.start:obj.start + min(obj.length, 5)]:  # Show first 5 items
                        contents.append(_obj_to_string_simple(item))
                    content_str = " ".join(contents)
                    if obj.length > 5:
                        content_str += " ..."
                    return f"{{{content_str}}}"
                except:
                    return "{proc}"
            return "{}"
        else:
            # Show literal array contents
            if hasattr(obj, 'val') and obj.val:
                try:
                    contents = []
                    for item in obj.val[obj.start:obj.start + min(obj.length, 5)]:
                        contents.append(_obj_to_string_simple(item))
                    content_str = " ".join(contents)
                    if obj.length > 5:
                        content_str += " ..."
                    return f"[{content_str}]"
                except:
                    return f"[array({obj.length})]"
            return "[]"
    elif obj.TYPE == ps.T_DICT:
        return f"<<dict({len(obj.val) if obj.val else 0})>>"
    elif obj.TYPE == ps.T_FILE:
        filename = getattr(obj, 'filename', '<unknown>')
        return f"--file--({filename})"
    elif obj.TYPE == ps.T_MARK:
        return "-mark-"
    elif obj.TYPE == ps.T_NULL:
        return "-null-"
    else:
        return f"<{obj.TYPE}>"


def _obj_to_string_simple(obj):
    """Simple object-to-string for use inside arrays/procedures - no recursion."""
    if obj is None:
        return "<null>"
    
    if obj.TYPE == ps.T_NAME:
        prefix = "/" if obj.attrib == ps.ATTRIB_LIT else ""
        try:
            name_str = obj.val.decode('ascii', errors='replace') if obj.val else "<unnamed>"
            return f"{prefix}{name_str}"
        except:
            return f"{prefix}<name>"
    elif obj.TYPE == ps.T_OPERATOR:
        op_name = obj.val.__name__.replace('ps_', '') if hasattr(obj.val, '__name__') else "op"
        return f"--{op_name}--"
    elif obj.TYPE == ps.T_INT:
        return str(obj.val)
    elif obj.TYPE == ps.T_REAL:
        return str(obj.val)
    elif obj.TYPE == ps.T_BOOL:
        return "true" if obj.val else "false"
    elif obj.TYPE == ps.T_STRING:
        if obj.attrib == ps.ATTRIB_EXEC:
            return "--string--"
        else:
            try:
                if obj.val:
                    content = obj.val.decode('ascii', errors='replace')[:10]
                    return f"({content}{'...' if len(obj.val) > 10 else ''})"
                else:
                    return "()"
            except:
                return "(string)"
    elif obj.TYPE in ps.ARRAY_TYPES:
        return "{...}" if obj.attrib == ps.ATTRIB_EXEC else "[...]"
    elif obj.TYPE == ps.T_DICT:
        return "<<...>>"
    else:
        return f"<{obj.TYPE}>"


def _obj_to_string(obj):
    """Convert PostScript object to string representation for execution history."""
    if obj is None:
        return "<null>"
    
    if obj.TYPE == ps.T_NAME:
        prefix = "/" if obj.attrib == ps.ATTRIB_LIT else ""
        try:
            name_str = obj.val.decode('ascii', errors='replace') if obj.val else "<unnamed>"
            return f"{prefix}{name_str}"
        except (AttributeError, UnicodeDecodeError):
            return f"{prefix}<name:{type(obj.val)}>"
    elif obj.TYPE == ps.T_OPERATOR:
        op_name = obj.val.__name__.replace('ps_', '') if hasattr(obj.val, '__name__') else "operator"
        return f"--{op_name}--"
    elif obj.TYPE == ps.T_INT:
        return str(obj.val)
    elif obj.TYPE == ps.T_REAL:
        return str(obj.val)
    elif obj.TYPE == ps.T_BOOL:
        return "true" if obj.val else "false"
    elif obj.TYPE == ps.T_STRING:
        if obj.attrib == ps.ATTRIB_EXEC:
            return f"--string--"
        else:
            if obj.val is None:
                return "()"  # Empty string
            try:
                content = obj.val.decode('ascii', errors='replace')[:20]  # Truncate long strings
                return f"({content})" + ("..." if len(obj.val) > 20 else "")
            except (AttributeError, UnicodeDecodeError):
                return f"(string:{type(obj.val)})"  # Fallback for unusual string types
    elif obj.TYPE in ps.ARRAY_TYPES:
        if obj.attrib == ps.ATTRIB_EXEC:
            return "{proc}"
        else:
            return f"[array({obj.length})]"
    elif obj.TYPE == ps.T_DICT:
        return f"<<dict({len(obj.val)}>>)"
    elif obj.TYPE == ps.T_FILE:
        filename = getattr(obj, 'filename', '<unknown>')
        return f"--file--({filename})"
    elif obj.TYPE == ps.T_MARK:
        return "-mark-"
    elif obj.TYPE == ps.T_NULL:
        return "-null-"
    else:
        return f"<{obj.TYPE}>"


def pauseexechistory(ctxt, ostack):
    """
    - **pauseexechistory** -
    
    PostForge extension: Temporarily pause execution history recording.
    Used internally during error handling to avoid polluting the history
    with error handling operations.
    
    Stack: - → -
    """
    ctxt.execution_history_paused = True


def resumeexechistory(ctxt, ostack):
    """
    - **resumeexechistory** -

    PostForge extension: Resume execution history recording after being paused.

    Stack: - → -
    """
    ctxt.execution_history_paused = False


# Magic number for internaldict access (per PLRM and Adobe Type 1 Font Format)
INTERNALDICT_PASSWORD = 1183615869


def internaldict(ctxt, ostack):
    """
    int **internaldict** dict

    Pushes the internal dictionary object on the operand stack. The int operand
    must be the integer 1183615869. The internal dictionary is in local VM and
    is writeable. It contains operators and other information whose purpose is
    internal to the PostScript interpreter.

    This is used primarily during construction of Type 1 font programs for
    Flex and hint replacement procedures.

    PLRM Section 8.2, Page 614 (Third Edition)
    Stack: int → dict
    **Errors**: **invalidaccess**, **stackunderflow**, **undefined**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, internaldict.__name__)

    # 2. TYPECHECK - Check operand type (must be integer)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, internaldict.__name__)

    password = ostack[-1].val

    # 3. INVALIDACCESS - Validate password
    if password != INTERNALDICT_PASSWORD:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, internaldict.__name__)

    # Get or create the internal dictionary
    # Store it in local VM as specified by PLRM
    if not hasattr(ctxt, '_internaldict') or ctxt._internaldict is None:
        # Create the internal dictionary with common entries used by Type 1 fonts
        ctxt._internaldict = ps.Dict(ctxt.id, None, name=b"internaldict", is_global=False)
        # Initialize with common entries that Type 1 fonts expect
        # These can be populated as needed by font programs

    ostack[-1] = ctxt._internaldict
