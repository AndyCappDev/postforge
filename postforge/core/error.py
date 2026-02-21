# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import copy

from . import types as ps

# error types
VMERROR = 0
DICTFULL = 1
DICTSTACKOVERFLOW = 2
DICTSTACKUNDERFLOW = 3
EXECSTACKOVERFLOW = 4
INVALIDACCESS = 5
INVALIDEXIT = 6
INVALIDFILEACCESS = 7
INVALIDFONT = 8
INVALIDRESTORE = 9
IOERROR = 10
LIMITCHECK = 11
NOCURRENTPOINT = 12
RANGECHECK = 13
STACKOVERFLOW = 14
STACKUNDERFLOW = 15
SYNTAXERROR = 16
TIMEOUT = 17
TYPECHECK = 18
UNDEFINED = 19
UNDEFINEDFILENAME = 20
UNDEFINEDRESOURCE = 21
UNDEFINEDRESULT = 22
UNMATCHEDMARK = 23
UNREGISTERED = 24
UNSUPPORTED = 25
CONFIGURATIONERROR = 26


def e(ctxt: ps.Context, error_code: int, func_name: str) -> None:
    # Immediately pause execution history to preserve the actual error context
    # This prevents error handling operations from overwriting the history we care about
    ctxt.execution_history_paused = True

    # Late import to break circular dependency:
    # - utils/error.py needs operators/dict.py for dictionary lookup
    # - operators/dict.py needs utils/error.py for error handling
    # - PostScript requires dynamic errordict lookup (users can replace entire errordict)
    # - Late import allows both modules to load, then import happens at runtime
    from ..operators import dict as ps_dict
    
    if func_name.startswith("ps_"):
        func_name = func_name[3:]

    # get the errordict dictionary (must be dynamic - users can replace errordict)
    error_dict = ps_dict.lookup(
        ctxt, ps.Name(b"errordict", is_global=ctxt.vm_alloc_mode)
    )

    if error_dict is None:
        # errordict not yet defined (early initialization failure)
        error_name = ctxt.error_names[error_code].decode() if error_code < len(ctxt.error_names) else f"error#{error_code}"
        raise RuntimeError(
            f"PostScript error /{error_name} in --{func_name}-- during initialization "
            f"(errordict not yet available)"
        )

    # lookup the error handler for the error from the errordict
    error_handler = copy.copy(error_dict.val[ctxt.error_names[error_code]])

    # push the error handler onto the execution stack
    ctxt.e_stack.append(error_handler)

    # push the offending command onto the operand stack
    ctxt.o_stack.append(
        ps.Name(bytes(func_name, "ascii"), is_global=ctxt.vm_alloc_mode)
    )
