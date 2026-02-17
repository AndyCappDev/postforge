# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript Job Control Operators - startjob and exitserver

This module implements PostScript job control operators per PLRM Section 3.7.7
"Job Execution Environment". These operators allow PostScript programs to control
job encapsulation and make persistent changes to the interpreter state.

Key Functions:
    - ps_startjob: Core job control operator (Level 2/3)
    - ps_exitserver: Compatibility wrapper around startjob (Level 1)

PLRM Compliance:
    - Full PLRM Section 3.7.7 implementation
    - Three validation conditions for startjob
    - Proper job server sequence execution
    - Unencapsulated job support with persistent VM changes
"""

import sys
import copy
from typing import Union

from ..core import error as ps_error
from ..core import types as ps
from . import control as ps_control
from . import vm as ps_vm
from . import dict as ps_dict
from . import operand_stack as ps_operand_stack


def startjob(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    bool password **startjob** bool
    
    conditionally starts a new job whose execution may alter the initial VM for sub-
    sequent jobs. The bool operand specifies whether the new job's side effects are
    to be persistent. The semantics of job execution are described in section 3.7.7,
    "Job Execution Environment."
    
    PLRM Section 8.2 (both Second and Third Editions)
    Stack: bool password → bool
    **Errors**: **invalidaccess**, **stackunderflow**, **typecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, startjob.__name__)
    # 2. TYPECHECK - Check operand types (bool password)
    if ostack[-1].TYPE not in {ps.T_STRING, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, startjob.__name__)
    if ostack[-2].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, startjob.__name__)
    
    persistent_bool = ostack[-2]
    password = ostack[-1]
    
    # STEP 2: Evaluate the three PLRM validation conditions
    
    # Condition 1: The current execution context supports job encapsulation
    supports_encapsulation = ctxt.supports_job_encapsulation
    
    # Condition 2: The password is correct (matches StartJobPassword system parameter)
    # Get StartJobPassword from system parameters
    start_job_password = ctxt.system_params.get("StartJobPassword", 0)
    
    if password.TYPE == ps.T_STRING:
        password_value = password.python_string()
    else:  # ps.Int
        password_value = str(password.val)
        
    password_correct = (password_value == start_job_password)
    
    # Condition 3: Save level validation
    # PLRM 8.2: "The current level of save nesting is no deeper than it was at the
    # time the current job started."
    # PLRM 3.7.7: "startjob works only when the current save level is equal to the
    # level at which the current job started."
    save_level_valid = (ctxt.save_id == ctxt.current_job_start_save_level)
    
    # STEP 3: Execute startjob actions if all conditions are satisfied
    all_conditions_met = supports_encapsulation and password_correct and save_level_valid
    
    if all_conditions_met:
        # Pop operands before executing job server sequence
        ostack.pop()  # password
        bool_operand = ostack.pop()  # bool
        
        # Execute job server sequence per PLRM Section 3.7.7
        _execute_job_server_sequence(ctxt, bool_operand.val)
        
        # Push true result to indicate successful startjob
        ostack.append(ps.Bool(True))
    else:
        # startjob unsuccessful - pop operands and push false
        ostack.pop()  # password
        ostack.pop()  # bool
        ostack.append(ps.Bool(False))


def _execute_job_server_sequence(ctxt: "ps.Context", persistent: bool) -> None:
    """
    Execute the PostScript job server sequence per PLRM Section 8.2.

    When **startjob** succeeds, it performs the following actions:
    1. Ends the current job — resets the stacks and, if the current job
       was encapsulated, performs a **restore** operation (PLRM 3.7.7 steps 5, 6).
    2. Begins a new job. If persistent is True, the usual **save** at the
       beginning of the job is omitted (unencapsulated). If False,
       the usual **save** is performed (encapsulated).

    The caller pushes true on the operand stack after this returns.

    Args:
        ctxt: PostScript execution context
        persistent: True for unencapsulated job, False for encapsulated job
    """

    # === Step 1: End the current job (PLRM 3.7.7 steps 5 and 6) ===

    # Step 5: Clear the operand stack and reset the dictionary stack to its initial state
    ps_operand_stack.clear(ctxt, ctxt.o_stack)
    while len(ctxt.d_stack) > 3:  # Reset to [systemdict, globaldict, userdict]
        ctxt.d_stack.pop()

    # Step 6: If the current job was encapsulated, restore VM (both local and global)
    if ctxt.job_save_level_stack:
        current_job_save = ctxt.job_save_level_stack[-1]
        if current_job_save.id in ctxt.active_saves:
            # Current job was encapsulated (has a real save) — restore VM
            # Temporarily save and clear the execution stack to bypass the
            # invalidrestore check. The job server restore is an internal
            # operation, not user-level PostScript — per PLRM 3.7.7, startjob
            # "does not disturb the standard input and output files; the
            # interpreter resumes consuming the remainder of the same input file."
            saved_estack = list(ctxt.e_stack)
            ctxt.e_stack.clear()

            restore_save = copy.copy(current_job_save)
            ctxt.o_stack.append(restore_save)
            ps_vm.restore(ctxt, ctxt.o_stack)

            # Restore the execution stack so execution continues from the same file
            ctxt.e_stack.extend(saved_estack)
        ctxt.job_save_level_stack.pop()

    # === Step 2: Begin a new job ===

    if persistent:
        # Unencapsulated: skip save — VM changes will be persistent
        ctxt.vm_alloc_mode = False  # Local VM allocation mode

        # Track the new unencapsulated job (no real save, save_id stays at -1)
        current_save = ps.Save(ctxt.save_id)
        current_save_copy = copy.copy(current_save)
        ctxt.job_save_level_stack.append(current_save_copy)
    else:
        # Encapsulated: execute save to create job boundary
        ps_vm.save(ctxt, ctxt.o_stack)
        job_save = ctxt.o_stack.pop()

        ctxt.vm_alloc_mode = False  # Local VM allocation mode

        # Track the new encapsulated job
        job_save_copy = copy.copy(job_save)
        ctxt.job_save_level_stack.append(job_save_copy)


def ps_quitwithcode(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    int **.quitwithcode** -

    Sets the interpreter exit code to be returned to the shell when the
    interpreter terminates. Does not quit — call quit separately.

    Stack: int → -
    Errors: stackunderflow, typecheck
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ".quitwithcode")
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".quitwithcode")
    ctxt.exit_code = ostack.pop().val


def exitserver(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    password **exitserver** -
    
    This has the same effect as:
    true password **startjob** not
    {/**exitserver** **errordict** /invalidaccess get exec} if
    
    PLRM Section 8.2 (both Second and Third Editions)  
    Stack: password → - (or invalidaccess error)
    **Errors**: **invalidaccess**, **stackunderflow**, **typecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, exitserver.__name__)
    # 2. TYPECHECK - Check operand type (password)
    if ostack[-1].TYPE not in {ps.T_STRING, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, exitserver.__name__)
    
    if ostack[-1].TYPE == ps.T_INT:
        # convert to a string
        password = str(ostack[-1].val)
    else:
        password = ostack[-1].python_string()

    if password != ctxt.system_params.get("StartJobPassword", ""):
        ps_error.e(ctxt, ps_error.INVALIDACCESS, "setsystemparams")
        return
    
    # STEP 2: Implement exitserver as equivalent to startjob sequence
    # Push true and password for startjob call
    psswrd = ostack[-1]
    ostack.pop()
    ostack.append(ps.Bool(True))  # true (unencapsulated job)
    ostack.append(psswrd)
    # password is already on stack
    
    # Call startjob
    startjob(ctxt, ostack)
    
    # Check startjob result
    success = ostack.pop()  # Get startjob result
    
    if not success.val:
        # startjob failed - generate invalidaccess error per PLRM
        # (startjob already popped both operands and pushed false, which we popped above)
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, exitserver.__name__)
    
    # STEP 3: exitserver successful - now do post-success actions
    # Output the standard exitserver message to stdout
    # PLRM: "successful execution of exitserver sends the message 
    # %%[exitserver: permanent state may be changed]%% to the standard output file"
    try:
        stdout_file = ctxt.stdout_file
        if stdout_file and hasattr(stdout_file, 'write'):
            # Use the file's write method (works with both StandardFile and StandardFileProxy)
            stdout_file.write("%%[exitserver: permanent state may be changed]%%\n")
        else:
            # Fallback to sys.stdout
            sys.stdout.write("%%[exitserver: permanent state may be changed]%%\n")
            sys.stdout.flush()
    except Exception:
        # Continue even if message output fails
        pass
    
    # Clear the dictionary stack of serverdict (per canonical exitserver usage)
    # The PLRM notes that successful exitserver "removes serverdict from the dictionary stack"
    if len(ctxt.d_stack) > 3:  # Keep systemdict, globaldict, userdict
        # Look for serverdict and remove it
        for i in range(len(ctxt.d_stack) - 1, 2, -1):  # Search from top down, but keep bottom 3
            dict_obj = ctxt.d_stack[i]
            if (dict_obj.TYPE == ps.T_DICT and 
                hasattr(dict_obj, 'name') and 
                dict_obj.name == b"serverdict"):
                ctxt.d_stack.pop(i)
                break