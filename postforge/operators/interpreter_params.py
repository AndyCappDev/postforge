# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import collections
from copy import copy

from ..core import error as ps_error
from ..core import types as ps


def setsystemparams(ctxt, ostack):
    """
    dict **setsystemparams** -
    
    attempts to set one or more system parameters whose keys and new values are
    contained in the dictionary dict. Permission to alter system parameters is 
    controlled by a password. The dictionary usually must contain an entry named 
    Password whose value is a string or integer equal to the SystemPassword.
    If the password is incorrect, an invalidaccess error occurs.
    
    PLRM Section 8.2
    Stack: dict → -
    **Errors**: **invalidaccess**, **limitcheck**, **stackunderflow**, **typecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setsystemparams.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setsystemparams.__name__)
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, setsystemparams.__name__)
    
    # PLRM: "The dictionary usually must contain an entry named Password"
    if b"Password" not in ostack[-1].val:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, setsystemparams.__name__)
    
    # Validate password matches SystemParamsPassword system parameter  
    password_value = ostack[-1].val[b"Password"]
    if password_value.TYPE != ps.T_STRING:
        ps_error.e(ctxt, ps_error.TYPECHECK, setsystemparams.__name__)
        return
    provided_password = password_value.python_string()
    
    # Check if password matches SystemPassword
    if provided_password != ctxt.system_params["SystemParamsPassword"]:
        ps_error.e(ctxt, ps_error.INVALIDACCESS, setsystemparams.__name__)
        return
    
    # check the types first (skip Password key)
    for key, value in ostack[-1].val.items():
        if key == b"Password":
            continue
        if value.TYPE not in {ps.T_INT, ps.T_STRING, ps.T_BOOL}:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setsystemparams.__name__)

    # Process system parameters
    for key, value in ostack[-1].val.items():
        if key == b"Password":
            continue
        str_key = key.decode('ascii') if isinstance(key, bytes) else key
        if value.TYPE == ps.T_STRING:
            ctxt.system_params[str_key] = value.python_string()
        elif value.TYPE == ps.T_BOOL:
            ctxt.system_params[str_key] = bool(value.val)
        else:
            ctxt.system_params[str_key] = value.val

    # Propagate MaxFontCache changes to the live bitmap cache if it exists
    if b"MaxFontCache" in ostack[-1].val:
        bitmap_cache = ps.global_resources._glyph_bitmap_cache
        if bitmap_cache is not None:
            bitmap_cache._max_bytes = ctxt.system_params["MaxFontCache"]

    ostack.pop()


def currentsystemparams(ctxt, ostack):
    """
    - **currentsystemparams** dict

    Returns a dictionary containing the keys and current values of all system
    parameters that are defined in the implementation. The returned dictionary
    is a read-only container for key-value pairs. Each execution of
    **currentsystemparams** allocates and returns a new dictionary.

    System parameters control interpreter-wide settings such as resource
    directories, passwords, and device configurations.

    PLRM Section 8.2, Page 556 (Third Edition)
    Stack: - → dict
    **Errors**: **stackoverflow**, **VMerror**
    **See Also**: **setsystemparams**, **currentuserparams**
    """
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentsystemparams.__name__)

    # Create a new dictionary with system parameters
    sys_params_dict = ps.Dict(ctxt.id, name=b"systemparams", access=ps.ACCESS_READ_ONLY, is_global=ctxt.vm_alloc_mode)

    # Copy system parameters to the dictionary
    for key, value in ctxt.system_params.items():
        key_bytes = key.encode('ascii') if isinstance(key, str) else key
        if isinstance(value, bool):
            sys_params_dict.val[key_bytes] = ps.Bool(value)
        elif isinstance(value, int):
            sys_params_dict.val[key_bytes] = ps.Int(value)
        elif isinstance(value, float):
            sys_params_dict.val[key_bytes] = ps.Real(value)
        elif isinstance(value, str):
            # Create a PostScript string for string values
            strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings
            offset = len(strings)
            value_bytes = value.encode('ascii', errors='replace')
            strings += bytearray(value_bytes)
            sys_params_dict.val[key_bytes] = ps.String(
                ctxt.id, offset, len(value_bytes), is_global=ctxt.vm_alloc_mode
            )
        # Skip complex types that can't be easily converted

    ostack.append(sys_params_dict)


def setuserparams(ctxt, ostack):
    """
    dict **setuserparams** -


    attempts to set one or more user parameters whose keys and new values are contained
    in the dictionary dict. The dictionary is merely a container for key-value
    pairs; **setuserparams** reads the information from the dictionary but does not retain
    the dictionary itself. User parameters whose keys are not mentioned in the
    dictionary are left unchanged.

    Each parameter is identified by a key, which is always a name object. If the named
    user parameter does not exist in the implementation, it is ignored. If a specified
    numeric value is not achievable by the implementation, the nearest achievable
    value is substituted without error indication.

    String values should consist of nonnull characters; if a null character is present, it
    will terminate the string. String-valued parameters may be subject not only to the
    general implementation limit on strings (noted in Appendix B) but also to
    implementation-dependent limits specific to certain parameters. If either limit is
    exceeded, a **limitcheck** error occurs.

    The names of user parameters and details of their semantics are given in
    Appendix C. Additional parameters are described in the PostScript Language Reference
    Supplement and in product-specific documentation. Some user parameters
    have default values that are system parameters with the same names. These defaults
    can be set by **setsystemparams**.

    User parameters, unlike system parameters, can be set without supplying a password.
    Alterations to user parameters are subject to **save** and **restore**. In an interpreter
    that supports multiple execution contexts, user parameters are maintained
    separately for each context.

    Example
        << /MaxFontItem 7500 >> **setuserparams**

    This example attempts to set the **MaxFontItem** user parameter to 7500.

    **Errors**:     **invalidaccess**, **limitcheck**, **stackunderflow**, **typecheck**
    **See Also**:   **currentuserparams**, **setsystemparams**, **setdevparams**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setuserparams.__name__)

    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setuserparams.__name__)

    new_params = ostack[-1].val

    # all new param's keys must be names, and values must be ps.Int, ps.Bool, ps.String or ps.Name
    for key, val in new_params.items():
        if key.TYPE != ps.T_NAME or val.TYPE not in {ps.T_INT, ps.T_BOOL, ps.T_STRING, ps.T_NAME}:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setuserparams.__name__)

    # first update the UserParams Dictionary
    user_params = ctxt.lvm[b"UserParams"].val
    for key, val in new_params.items():
        if key in user_params:  # skip any keys that are not already in UserParams
            user_params[key] = val

    # now add/set the user params in the context's attributes
    for key, val in user_params.items():
        # items are stored in the context as native python types
        if val.TYPE in {ps.T_STRING, ps.T_NAME}:
            # just use setattr(class, key, value) instead
            ctxt.__setattr__(key.val.decode(), val.python_string())
        elif val.TYPE in ps.ARRAY_TYPES:
            # the array itself is stored as a native python list...
            # but it's members are PSObjects
            ctxt.__setattr__(
                key.val.decode(), val.val[val.start : val.start + val.length]
            )
        else:
            # Special handling for ExecutionHistory parameter
            param_name = key.val.decode()
            if param_name == "ExecutionHistory":
                if val.val:  # Enable execution history
                    ctxt.enable_execution_history()
                else:  # Disable execution history
                    ctxt.disable_execution_history()
            elif param_name == "ExecutionHistorySize":
                # Resize execution history deque
                if hasattr(val, 'val') and isinstance(val.val, int) and val.val > 0:
                    # Create new deque with new size, preserving existing entries
                    old_entries = list(ctxt.execution_history) if hasattr(ctxt, 'execution_history') else []
                    ctxt.execution_history = collections.deque(old_entries, maxlen=val.val)
                    ctxt.__setattr__(param_name, val.val)
            else:
                ctxt.__setattr__(param_name, val.val)

    ostack.pop()


def currentuserparams(ctxt, ostack):
    """
    - **currentuserparams** dict


    returns a dictionary containing the keys and current values of all user parameters
    that are defined in the implementation. The returned dictionary is a container for
    key-value pairs. Each execution of **currentuserparams** allocates and returns a new
    dictionary. See Appendix C for information about specific user parameters.

    **Errors**:     **stackoverflow**, **VMerror**
    **See Also**:   **setuserparams**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentuserparams.__name__)

    user_params = copy(ctxt.lvm[b"UserParams"])
    user_params.val = copy(ctxt.lvm[b"UserParams"].val)
    
    # Update local_refs to track the correct val after reassignment
    if not user_params.is_global and user_params.ctxt_id is not None:
        ps.contexts[user_params.ctxt_id].local_refs[user_params.created] = user_params.val
    
    ostack.append(user_params)


def vmreclaim(ctxt, ostack):
    """
    int **vmreclaim** -

    Controls garbage collection behavior. In PostForge, this is a no-op since
    Python handles garbage collection automatically. Valid values are -2, -1, 0,
    1, and 2.

    PLRM Section 8.2
    Stack: int -> -
    **Errors**: **stackunderflow**, **typecheck**, **rangecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, vmreclaim.__name__)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, vmreclaim.__name__)
    val = ostack[-1].val
    if val not in (-2, -1, 0, 1, 2):
        return ps_error.e(ctxt, ps_error.RANGECHECK, vmreclaim.__name__)
    # Update VMReclaim user param
    user_params = ctxt.lvm[b"UserParams"].val
    user_params[b"VMReclaim"] = ostack[-1]
    ostack.pop()


def setvmthreshold(ctxt, ostack):
    """
    int **setvmthreshold** -

    Sets the VM allocation threshold that triggers garbage collection.
    In PostForge, this is effectively a no-op but validates and stores the value.
    -1 means use implementation default. Other negative values are rangecheck.

    PLRM Section 8.2
    Stack: int -> -
    **Errors**: **stackunderflow**, **typecheck**, **rangecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setvmthreshold.__name__)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setvmthreshold.__name__)
    val = ostack[-1].val
    if val < -1:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setvmthreshold.__name__)
    # Update VMThreshold user param
    user_params = ctxt.lvm[b"UserParams"].val
    user_params[b"VMThreshold"] = ostack[-1]
    ostack.pop()


def cachestatus(ctxt, ostack):
    """
    - **cachestatus** bsize bmax msize mmax csize cmax blimit

    Returns 7 integers describing font cache status:
    - bsize: current bytes used in font cache
    - bmax: maximum bytes for font cache (MaxFontCache system param)
    - msize: number of entries in path cache
    - mmax: maximum entries in path cache
    - csize: number of entries in bitmap cache
    - cmax: maximum entries in bitmap cache
    - blimit: maximum bytes per font item (MaxFontItem user param)

    PLRM Section 8.2
    Stack: - -> bsize bmax msize mmax csize cmax blimit
    **Errors**: **stackoverflow**
    """
    if ctxt.MaxOpStack and len(ostack) + 7 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, cachestatus.__name__)

    # Bitmap cache stats
    bitmap_cache = ps.global_resources._glyph_bitmap_cache
    if bitmap_cache is not None:
        bsize = bitmap_cache._current_bytes
        csize = len(bitmap_cache)
        cmax = bitmap_cache._max_entries
    else:
        bsize = 0
        csize = 0
        cmax = 0

    bmax = ctxt.system_params.get("MaxFontCache", 0)

    # Path cache stats
    path_cache = ps.global_resources._glyph_cache
    if path_cache is not None:
        msize = len(path_cache)
        mmax = path_cache._max_entries
    else:
        msize = 0
        mmax = 0

    # MaxFontItem from user params
    user_params = ctxt.lvm[b"UserParams"].val
    blimit_obj = user_params.get(b"MaxFontItem")
    blimit = blimit_obj.val if blimit_obj is not None else 0

    ostack.append(ps.Int(bsize))
    ostack.append(ps.Int(bmax))
    ostack.append(ps.Int(msize))
    ostack.append(ps.Int(mmax))
    ostack.append(ps.Int(csize))
    ostack.append(ps.Int(cmax))
    ostack.append(ps.Int(blimit))


def setcacheparams(ctxt, ostack):
    """
    mark ... size lower upper **setcacheparams** -

    Sets font cache parameters. Takes variable number of arguments above mark.
    Top of stack = upper (MaxFontItem), next = lower (MinFontCompress),
    next = size (MaxFontCache). Extra values are ignored.

    PLRM Section 8.2
    Stack: mark ... size lower upper -> -
    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**
    """
    # Find mark on stack
    mark_pos = None
    for i in range(len(ostack) - 1, -1, -1):
        if ostack[i].TYPE == ps.T_MARK:
            mark_pos = i
            break
    if mark_pos is None:
        return ps_error.e(ctxt, ps_error.UNMATCHEDMARK, setcacheparams.__name__)

    # Collect values above mark (TOS first)
    values = []
    for i in range(len(ostack) - 1, mark_pos, -1):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcacheparams.__name__)
        values.append(int(ostack[i].val))

    # Validate non-negative
    for v in values:
        if v < 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcacheparams.__name__)

    # Pop everything including mark
    del ostack[mark_pos:]

    user_params = ctxt.lvm[b"UserParams"].val

    # Assign: values[0]=upper(MaxFontItem), values[1]=lower(MinFontCompress),
    #         values[2]=size(MaxFontCache)
    if len(values) >= 1:
        user_params[b"MaxFontItem"] = ps.Int(values[0])
    if len(values) >= 2:
        user_params[b"MinFontCompress"] = ps.Int(values[1])
    if len(values) >= 3:
        ctxt.system_params["MaxFontCache"] = values[2]
        # Propagate to live bitmap cache
        bitmap_cache = ps.global_resources._glyph_bitmap_cache
        if bitmap_cache is not None:
            bitmap_cache._max_bytes = values[2]


def currentcacheparams(ctxt, ostack):
    """
    - **currentcacheparams** mark size lower upper

    Returns font cache parameters as mark followed by three integers.

    PLRM Section 8.2
    Stack: - -> mark size lower upper
    **Errors**: **stackoverflow**
    """
    if ctxt.MaxOpStack and len(ostack) + 4 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentcacheparams.__name__)

    size = ctxt.system_params.get("MaxFontCache", 0)

    user_params = ctxt.lvm[b"UserParams"].val
    lower_obj = user_params.get(b"MinFontCompress")
    lower = lower_obj.val if lower_obj is not None else 0
    upper_obj = user_params.get(b"MaxFontItem")
    upper = upper_obj.val if upper_obj is not None else 0

    ostack.append(ps.Mark(b"["))
    ostack.append(ps.Int(size))
    ostack.append(ps.Int(lower))
    ostack.append(ps.Int(upper))


def setucacheparams(ctxt, ostack):
    """
    mark ... blimit **setucacheparams** -

    Sets user path cache parameters. Takes variable args above mark.
    Top of stack = blimit (MaxUPathItem).

    PLRM Section 8.2
    Stack: mark ... blimit -> -
    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**
    """
    # Find mark on stack
    mark_pos = None
    for i in range(len(ostack) - 1, -1, -1):
        if ostack[i].TYPE == ps.T_MARK:
            mark_pos = i
            break
    if mark_pos is None:
        return ps_error.e(ctxt, ps_error.UNMATCHEDMARK, setucacheparams.__name__)

    # Collect values above mark
    values = []
    for i in range(len(ostack) - 1, mark_pos, -1):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setucacheparams.__name__)
        values.append(int(ostack[i].val))

    # Validate non-negative
    for v in values:
        if v < 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setucacheparams.__name__)

    # Pop everything including mark
    del ostack[mark_pos:]

    # Assign: values[0]=blimit(MaxUPathItem)
    if len(values) >= 1:
        user_params = ctxt.lvm[b"UserParams"].val
        user_params[b"MaxUPathItem"] = ps.Int(values[0])


def ucachestatus(ctxt, ostack):
    """
    - **ucachestatus** mark bsize bmax rsize rmax blimit

    Returns user path cache status as mark followed by five integers.

    PLRM Section 8.2
    Stack: - -> mark bsize bmax rsize rmax blimit
    **Errors**: **stackoverflow**
    """
    if ctxt.MaxOpStack and len(ostack) + 6 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, ucachestatus.__name__)

    bsize = ctxt.system_params.get("CurUPathCache", 0)
    bmax = ctxt.system_params.get("MaxUPathCache", 0)

    user_params = ctxt.lvm[b"UserParams"].val
    blimit_obj = user_params.get(b"MaxUPathItem")
    blimit = blimit_obj.val if blimit_obj is not None else 0

    ostack.append(ps.Mark(b"["))
    ostack.append(ps.Int(bsize))
    ostack.append(ps.Int(bmax))
    ostack.append(ps.Int(0))  # rsize - no upath cache exists
    ostack.append(ps.Int(0))  # rmax - no upath cache exists
    ostack.append(ps.Int(blimit))


def setdevparams(ctxt, ostack):
    """
    string dict **setdevparams** -

    Sets device parameters for the named device. In PostForge, this validates
    types and accepts gracefully (no real I/O devices to configure).

    PLRM Section 8.2
    Stack: string dict -> -
    **Errors**: **invalidaccess**, **limitcheck**, **stackunderflow**, **typecheck**
    """
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setdevparams.__name__)
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setdevparams.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setdevparams.__name__)
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, setdevparams.__name__)
    ostack.pop()
    ostack.pop()


def currentdevparams(ctxt, ostack):
    """
    string **currentdevparams** dict

    Returns a dictionary of device parameters for the named device.
    In PostForge, returns an empty dictionary for any device name.

    PLRM Section 8.2
    Stack: string -> dict
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, currentdevparams.__name__)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, currentdevparams.__name__)
    ostack[-1] = ps.Dict(ctxt.id, is_global=ctxt.vm_alloc_mode)


def setcachelimit(ctxt, ostack):
    """
    int **setcachelimit** -

    Sets the maximum number of bytes for a single cached font item.
    Equivalent to << /MaxFontItem int >> **setuserparams**.

    PLRM Section 8.2
    Stack: int -> -
    **Errors**: **stackunderflow**, **typecheck**, **rangecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcachelimit.__name__)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setcachelimit.__name__)
    val = ostack[-1].val
    if val < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setcachelimit.__name__)
    user_params = ctxt.lvm[b"UserParams"].val
    user_params[b"MaxFontItem"] = ostack[-1]
    ostack.pop()
