# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript Control Flow and Execution Engine

This module implements the core PostScript execution engine and control flow
operations. It handles the fundamental execution model of PostScript including
stack-based execution, object processing, and control operators.

Key Functions:
    - exec_exec: Main PostScript execution loop
    - execjob: Execute PostScript files
    - start: Initialize execution context
    - ps_break/ps_breaki: Debugging breakpoint operators

The execution model follows PostScript specification requirements with proper
handling of executable objects, stack management, and error conditions.
"""

import copy
import os
import re
import time
from typing import Any

from . import dict as ps_dict
from ..core import error as ps_error
from . import graphics_state as ps_gs
from . import matrix as ps_matrix
from ..core import tokenizer as ps_token
from ..core import types as ps
from . import vm as ps_vm

def ps_break(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    PostScript **break** operator for debugging.

    Placeholder implementation for the PostScript '**break**' debugging operator.
    In a full implementation, this would set a breakpoint in the execution
    flow for interactive debugging purposes.

    Args:
        ctxt: PostScript execution context
        ostack: Operand stack (unused in current implementation)
    """
    ctxt = ctxt
    ostack = ostack


def ps_breaki(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    PostScript **breaki** operator for debugging.

    Placeholder implementation for the PostScript '**breaki**' debugging operator.
    In a full implementation, this would set an interactive breakpoint that
    allows examination of the PostScript execution state.

    Args:
        ctxt: PostScript execution context
        ostack: Operand stack (unused in current implementation)
    """
    ctxt = ctxt
    ostack = ostack


def start(ctxt: "ps.Context") -> None:
    """
    Initialize PostScript execution context for job execution.

    Placeholder function for any startup initialization required before
    executing PostScript code. Currently performs no operations but
    provides a hook for future initialization requirements.

    Args:
        ctxt: PostScript execution context to initialize
    """
    pass


_BBOX_RE = re.compile(rb'%%BoundingBox:\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)')
_HIRES_BBOX_RE = re.compile(
    rb'%%HiResBoundingBox:\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)'
)


def _read_eps_bounding_box(filepath):
    """Read bounding box from an EPS file, handling DOS EPS binary headers.

    Prefers %%HiResBoundingBox (sub-point float precision) when available,
    falling back to %%BoundingBox. Returns values as floats in both cases.
    """
    try:
        with open(filepath, 'rb') as f:
            header = f.read(4)
            if header == b'\xc5\xd0\xd3\xc6':
                ps_offset = int.from_bytes(f.read(4), byteorder='little')
                f.seek(ps_offset)
            else:
                f.seek(0)
            # Scan first ~4KB for BoundingBox
            chunk = f.read(4096)
            m = _HIRES_BBOX_RE.search(chunk)
            if m:
                return tuple(float(v) for v in m.groups())
            m = _BBOX_RE.search(chunk)
            if m:
                return tuple(float(v) for v in m.groups())
    except (OSError, ValueError):
        pass
    return None


def execjob(ctxt: "ps.Context", filepath: str) -> None:
    """
    Execute a PostScript file as a complete encapsulated job.
    
    Implements PostScript job server encapsulation per PostScript Language
    Reference Manual Third Edition, Section 3.7.7. Each file is executed as an
    independent job with proper **save**/**restore** bracketing and stack management.
    
    Job Server Start Sequence (PLRM 3.7.7):
    1. Execute **save** (outermost **save** with both local + global VM snapshot)
    2. **clear** operand stack, **cleardictstack**
    3. **initgraphics** to reset graphics state to defaults
    4. **false setglobal** (local VM allocation mode)
    5. Execute {(filepath) run} **stopped**
    6. Handle any errors via existing error handling

    Job Server End Sequence (PLRM 3.7.7, in _cleanup_job):
    7. Clear stacks
    8. Finalize output devices (PostForge-specific)
    9. **restore** VM state (reverts both local+global VM, grestoreall)
    
    Args:
        ctxt: PostScript execution context with initialized stacks and dictionaries
        filepath: Path to the PostScript file to execute as an encapsulated job
        
    PostScript Compliance:
        - Job bracketed with **save**/**restore** for encapsulation
        - Uses **stopped** context for error handling (existing implementation)
        - VM state isolated between jobs
    """
    # Record job start time and reset user wait time
    job_start_time = time.perf_counter()
    ctxt.user_wait_time = 0.0  # Reset accumulated wait time for this job

    # PLRM 3.7.7 Job Start Sequence:
    # 1. save  2. clear  3. cleardictstack  4. initgraphics  5. false setglobal

    # Step 1: Execute save (outermost save for job encapsulation)
    # This save captures both local and global VM state
    ps_vm.save(ctxt, ctxt.o_stack)
    job_save = ctxt.o_stack.pop()  # Remove save object from operand stack

    # Track job start save level for startjob operator validation (PLRM Section 3.7.7)
    # Add initial job to the job save level stack
    job_save_copy = copy.copy(job_save)
    ctxt.job_save_level_stack.append(job_save_copy)

    # Steps 2-3: Clear stacks and reset dictionary stack
    ctxt.o_stack.clear()
    while len(ctxt.d_stack) > 3:
        ctxt.d_stack.pop()

    # Step 4: Initialize graphics state to defaults
    ps_gs.initgraphics(ctxt, ctxt.o_stack)

    # Set global VM allocation mode for the string to run the job
    ctxt.vm_alloc_mode = True  # Global VM for job string allocation

    # Use forward slashes — backslashes are PS escape characters
    # (e.g., \r in \resources becomes carriage return on Windows)
    filepath = filepath.replace("\\", "/")

    try:
        # Execute the file in stopped context
        # For EPS files, auto-fit to letter page and append showpage
        if filepath.lower().endswith('.eps'):
            bbox = _read_eps_bounding_box(filepath)
            if bbox:
                llx, lly, urx, ury = bbox
                eps_w = urx - llx
                eps_h = ury - lly
                if eps_w > 0 and eps_h > 0:
                    # Crop page to EPS content dimensions via setpagedevice,
                    # then translate origin so EPS lower-left maps to (0,0)
                    s_t = bytes(
                        "{<< /PageSize ["
                        f"{eps_w:.4f} {eps_h:.4f}"
                        "] >> setpagedevice "
                        "gsave "
                        f"{-llx:.4f} {-lly:.4f} translate "
                        f"({filepath}) run "
                        "grestore showpage} stopped", "ascii")
                else:
                    s_t = bytes("{(" + filepath + ") run showpage} stopped", "ascii")
            else:
                s_t = bytes("{(" + filepath + ") run showpage} stopped", "ascii")
        else:
            s_t = bytes("{(" + filepath + ") run} stopped", "ascii")
        strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings
        offset = len(strings)
        strings += s_t
        ctxt.e_stack.append(
            ps.String(
                ctxt.id,
                offset=offset,
                length=len(s_t),
                attrib=ps.ATTRIB_EXEC,
                is_global=ctxt.vm_alloc_mode,
            )
        )

        # Step 5: Set local VM allocation mode (PostScript default for jobs)
        ctxt.vm_alloc_mode = False

        exec_exec_with_keyboard_interrupt(ctxt, ctxt.o_stack, ctxt.e_stack)

        # Handle errors from file execution (existing implementation)
        strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings
        if not ctxt.o_stack:
            return
        failed = ctxt.o_stack.pop()
        if failed.val:
            error_dict = ps_dict.lookup(ctxt, ps.Name(b"$error"))
            if error_dict.val[b"newerror"].val:
                s_t = b"errordict /handleerror get exec"
                offset = len(strings)
                strings += s_t
                ctxt.e_stack.append(
                    ps.String(
                        ctxt.id,
                        offset=offset,
                        length=len(s_t),
                        attrib=ps.ATTRIB_EXEC,
                        is_global=ctxt.vm_alloc_mode,
                    )
                )
                exec_exec_with_keyboard_interrupt(ctxt, ctxt.o_stack, ctxt.e_stack)
                
    finally:
        # Steps 5-6: Job cleanup regardless of success or failure
        _cleanup_job(ctxt, job_save)
        
        # Record job end time and display timing (excluding user wait time)
        job_end_time = time.perf_counter()
        job_duration = job_end_time - job_start_time - ctxt.user_wait_time
        print(f"\nJob execution time: {job_duration:.3f} seconds")


def _cleanup_job(ctxt: "ps.Context", job_save: "ps.Save") -> None:
    """
    Perform PostScript job cleanup per job server specification.
    
    This function implements steps 5-6 of the PostScript job server sequence:
    5. Clear operand stack and reset dictionary stack to initial state
    6. Execute **restore** (revert VM to state saved at job start)
    
    Also handles automatic cleanup of any pending saves that weren't properly
    restored by the job (per PostScript specification).
    
    Args:
        ctxt: PostScript execution context
        job_save: Save object created at job start for restoration
    """
    # Step 5: Clear stacks and reset dictionary stack to initial state
    
    # Clear operand stack
    ctxt.o_stack.clear()
    
    # Clear execution stack (job execution is complete)
    ctxt.e_stack.clear()
    
    # Reset dictionary stack to initial state
    # Keep systemdict, globaldict (if exists), userdict
    # This preserves system dictionaries while clearing any job-specific dicts
    while len(ctxt.d_stack) > 3:  # Keep bottom 3: systemdict, globaldict, userdict
        ctxt.d_stack.pop()

    # Clean up ALL nested jobs created by startjob within this file
    # Per PLRM: All startjob-created jobs end when the containing file ends
    while len(ctxt.job_save_level_stack) > 1:
        nested_job = ctxt.job_save_level_stack.pop()
        # Nested encapsulated job cleanup - these end automatically with file completion
        # No explicit restore needed as VM state will be handled by main job restore
    
    # Pop the main file job (the execjob-created job)
    if ctxt.job_save_level_stack:
        ctxt.job_save_level_stack.pop()

    # Finalize any open output devices (e.g., multi-page PDF)
    # Must run BEFORE restore since restore reverts page_device to pre-save state
    _finalize_output_devices(ctxt)

    # Step 6: Execute restore to revert VM to job start state (PLRM 3.7.7)
    # The outermost save in a job-encapsulation context reverts both local
    # and global VM, performs implicit grestoreall, and cleans up all save
    # bookkeeping (active_saves, save_id, cow state, gstate_stack).
    if job_save.id in ctxt.active_saves:
        restore_save = copy.copy(job_save)
        ctxt.o_stack.append(restore_save)
        ps_vm.restore(ctxt, ctxt.o_stack)


def _finalize_output_devices(ctxt: "ps.Context") -> None:
    """
    Finalize any open output devices at job end.

    This is called during job cleanup to ensure multi-page output devices
    (like PDF) properly close their documents and inject any embedded fonts.

    Currently supports:
    - PDF: Closes the Cairo surface and injects embedded fonts

    Args:
        ctxt: PostScript execution context
    """
    pd = ctxt.gstate.page_device

    # Check if this is a PDF device with pending state
    # The PDF module stores its state under a special key
    try:
        from ..devices.pdf.pdf import PDF_STATE_KEY, finalize_document
        if PDF_STATE_KEY in pd:
            finalize_document(pd)
    except ImportError:
        # PDF module not available, nothing to finalize
        pass
    except Exception as e:
        # Log but don't fail job cleanup
        print(f"Warning: Error finalizing PDF output: {e}")


def ps_exec(ctxt, ostack):
    """
    any **exec** –

    Pushes the operand on the execution stack, causing it to be executed
    immediately. The effect of **exec** depends on the type of the operand:
    for arrays and packed arrays, it is pushed as a procedure body to be
    executed element by element; for files, it is pushed as a source of
    characters to be scanned and executed; for strings, it is pushed as
    a source to be scanned and executed like a file; for names and
    operators, it is executed directly. For all other types, **exec** has
    no effect since they are treated as literals.

    **Errors**: **invalidaccess**, **stackunderflow**
    **See Also**: **xcheck**, **cvx**, **run**
    """
    op = "exec"
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    # 2. INVALIDACCESS - Check access permission (any object type, read access required)
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)

    estack = ctxt.e_stack

    # always execute the '}' operator
    if ostack[-1].TYPE == ps.T_NAME and ostack[-1].val and ostack[-1].val[0] == ps_token.R_CRLY_BRACKET:
        if ostack[-1].attrib == ps.ATTRIB_EXEC:
            # push it on the execution stack
            estack.append(ostack[-1])
            # pop it off the operand stack
            ostack.pop()
            return None

    if ctxt.proc_count == 0:
        if ostack[-1].TYPE in ps.STREAM_TYPES:
            if ostack[-1].attrib == ps.ATTRIB_EXEC:
                # push it on the execution stack
                estack.append(ostack[-1])
                # pop it off the operand stack
                ostack.pop()

        elif ostack[-1].TYPE == ps.T_NAME:
            # lookup the name and push it onto the exeution stack
            # if it has the executable attribute
            if ostack[-1].attrib == ps.ATTRIB_EXEC:
                obj = ps_dict.lookup(ctxt, ostack[-1])
                if obj is None:
                    # name is undefined
                    return ps_error.e(ctxt, ps_error.UNDEFINED, ostack[-1].__str__())
                # push the looked up object onto the execution stack
                estack.append(obj)

                # pop it off the operand stack
                ostack.pop()

        elif ostack[-1].TYPE == ps.T_OPERATOR:
            # push it onto the execution stack - operators are immutable, no copy needed
            estack.append(ostack[-1])
            # pop it off the operand stack
            ostack.pop()

        elif ostack[-1].TYPE in ps.ARRAY_TYPES:
            if ostack[-1].attrib == ps.ATTRIB_EXEC:
                # push it onto the execution stack
                estack.append(copy.copy(ostack[-1]))
                # pop it off the operand stack
                ostack.pop()
    return None


def exec_exec_with_keyboard_interrupt(ctxt: "ps.Context", o_stack: "ps.Stack", e_stack: "ps.Stack") -> None:
    """
    Wrapper for exec_exec that handles KeyboardInterrupt with detailed execution context.
    
    This wrapper catches Ctrl+C during PostScript execution and displays comprehensive
    debugging information including the current operator, execution context, and stack states.
    
    Args:
        ctxt: PostScript execution context containing all interpreter state
        o_stack: Operand stack for data values and procedure arguments  
        e_stack: Execution stack containing objects to be executed
    """
    try:
        exec_exec(ctxt, o_stack, e_stack)
    except KeyboardInterrupt:
        print("\n" + "="*80)
        print("*** KEYBOARD INTERRUPT - EXECUTION ANALYSIS ***")
        print("="*80)
        
        _show_execution_context_on_interrupt(ctxt, o_stack, e_stack)
        
        # Try to call ppstack if available
        try:
            ppstack_name = ps.Name(b"ppstack", is_global=ctxt.vm_alloc_mode)
            ppstack_proc = ps_dict.lookup(ctxt, ppstack_name)
            
            if ppstack_proc is not None:
                print("\n" + "-"*40)
                print("PostScript ppstack output:")
                print("-"*40)
                # Execute ppstack by putting it on execution stack
                e_stack.append(ppstack_proc)
                # Continue execution to run ppstack (without keyboard interrupt handling)
                exec_exec(ctxt, o_stack, e_stack)
            else:
                print("\nNote: ppstack procedure not found in dictionary stack")
                
        except Exception as e:
            print(f"\nError calling ppstack: {e}")
        
        print("\n" + "="*80)
        print("*** EXECUTION INTERRUPTED BY USER ***")
        print("="*80)
        
        quit
        # # Re-raise the KeyboardInterrupt to allow proper program termination
        # raise


def _show_execution_context_on_interrupt(ctxt: "ps.Context", o_stack: "ps.Stack", e_stack: "ps.Stack") -> None:
    """
    Display detailed execution context when KeyboardInterrupt occurs.
    
    Shows the current Python operator, PostScript object being executed, 
    stack states, and execution history to help debug hanging programs.
    """
    import traceback
    import os
    
    print(f"\nEXECUTION CONTEXT AT INTERRUPT")
    print("-" * 50)
    
    # 1. Analyze Python call stack to find current operator
    current_operator = None
    operator_location = None
    stack_frames = traceback.extract_stack()
    
    for frame in reversed(stack_frames):
        if 'operators/' in frame.filename:
            function_name = frame.name
            if function_name.startswith('ps_'):
                current_operator = function_name[3:]  # Remove 'ps_' prefix
                operator_location = f"{os.path.basename(frame.filename)}:{frame.lineno}"
                print(f"Python Operator: {function_name} ({current_operator})")
                print(f"   Location: {operator_location}")
                print(f"   Code: {frame.line}")
                break
            elif function_name in ['add', 'sub', 'mul', 'div', 'mod', 'abs', 'neg', 'sqrt', 'sin', 'cos']:
                # Direct function names for math operators
                current_operator = function_name
                operator_location = f"{os.path.basename(frame.filename)}:{frame.lineno}"
                print(f"Python Operator: {function_name}")
                print(f"   Location: {operator_location}")
                break
    
    if not current_operator:
        print("Python Operator: Not in operator code (in control flow)")
    
    # 2. Analyze execution stack top
    print(f"\nEXECUTION STACK ANALYSIS")
    print(f"   Depth: {len(e_stack)} objects")
    
    if e_stack:
        current_obj = e_stack[-1] 
        print(f"   Top object: {type(current_obj).__name__}")
        
        if current_obj.TYPE == ps.T_NAME:
            name = current_obj.val.decode('ascii') if hasattr(current_obj.val, 'decode') else str(current_obj.val)
            print(f"   >>About to execute: '{name}'")
            
        elif current_obj.TYPE == ps.T_ARRAY:
            if current_obj.attrib == ps.ATTRIB_EXEC:
                print(f"   >>Executing procedure of length {current_obj.length}")
                print(f"      Current position: {current_obj.start}")
                if current_obj.length > 0 and current_obj.start < len(current_obj.val):
                    next_obj = current_obj.val[current_obj.start]
                    if next_obj.TYPE == ps.T_NAME:
                        next_name = next_obj.val.decode('ascii') if hasattr(next_obj.val, 'decode') else str(next_obj.val)
                        print(f"      Next to execute: '{next_name}'")
            else:
                print(f"   >>Literal array of length {current_obj.length}")
                
        elif current_obj.TYPE == ps.T_LOOP:
            loop_types = {
                ps.LT_FOR: "for", ps.LT_REPEAT: "repeat", 
                ps.LT_LOOP: "loop", ps.LT_FORALL: "forall",
                ps.LT_CSHOW: "cshow", ps.LT_KSHOW: "kshow",
                ps.LT_PATHFORALL: "pathforall", ps.LT_FILENAMEFORALL: "filenameforall",
                ps.LT_RESOURSEFORALL: "resourceforall"
            }
            loop_name = loop_types.get(current_obj.val, f"unknown_loop({current_obj.val})")
            print(f"   >>Executing '{loop_name}' loop")
            if hasattr(current_obj, 'control') and hasattr(current_obj, 'limit'):
                print(f"      Control value: {current_obj.control}")
                print(f"      Limit: {current_obj.limit}")
                
        elif current_obj.TYPE in ps.TOKENIZABLE_TYPES:
            print(f"   >>Tokenizing/reading from stream")
            if current_obj.TYPE == ps.T_STRING:
                print(f"      String length: {current_obj.length}")
                print(f"      Position: {current_obj.start}")
            elif current_obj.TYPE == ps.T_FILE:
                print(f"      File: {current_obj.filename()}")
                
        elif current_obj.TYPE == ps.T_STOPPED:
            print(f"   >>Stopped context (error handling)")
            
        # Show next few objects on execution stack
        if len(e_stack) > 1:
            print(f"   Stack preview (top {min(5, len(e_stack))} objects):")
            for i, obj in enumerate(reversed(e_stack[:5])):
                marker = "→" if i == 0 else " "
                obj_desc = _describe_postscript_object(obj)
                print(f"     {marker} [{len(e_stack)-1-i}]: {obj_desc}")
                
    else:
        print("   >>Execution stack is empty")
    
    # 3. Show operand stack summary
    print(f"\nOPERAND STACK")
    print(f"   Depth: {len(o_stack)} objects")
    if o_stack:
        print(f"   Top {min(3, len(o_stack))} objects:")
        for i, obj in enumerate(reversed(o_stack[:3])):
            obj_desc = _describe_postscript_object(obj)
            print(f"     [{len(o_stack)-1-i}]: {obj_desc}")
    
    # 4. Show dictionary stack summary
    print(f"\nDICTIONARY STACK") 
    print(f"   Depth: {len(ctxt.d_stack)} dictionaries")
    if ctxt.d_stack:
        for i, dict_obj in enumerate(reversed(ctxt.d_stack)):
            dict_name = "unknown"
            if hasattr(dict_obj, 'name') and dict_obj.name:
                dict_name = dict_obj.name
            print(f"     [{len(ctxt.d_stack)-1-i}]: {dict_name} ({len(dict_obj.val)} entries)")
    
    # 5. Show VM state
    print(f"\nVIRTUAL MEMORY")
    print(f"   Allocation mode: {'Global' if ctxt.vm_alloc_mode else 'Local'}")
    print(f"   Save level: {len(ctxt.g_stack)} (graphics state stack depth)")


def _describe_postscript_object(obj) -> str:
    """Return a brief description of a PostScript object for debugging."""
    if obj.TYPE == ps.T_NAME:
        name = obj.val.decode('ascii') if hasattr(obj.val, 'decode') else str(obj.val)
        return f"Name '{name}'"
    elif obj.TYPE == ps.T_ARRAY:
        attr = "exec" if obj.attrib == ps.ATTRIB_EXEC else "literal"
        return f"Array({attr}, len={obj.length})"
    elif obj.TYPE == ps.T_STRING:
        return f"String(len={obj.length})"
    elif obj.TYPE in ps.NUMERIC_TYPES:
        return f"{type(obj).__name__}({obj.val})"
    elif obj.TYPE == ps.T_BOOL:
        return f"Bool({obj.val})"
    elif obj.TYPE == ps.T_OPERATOR:
        return f"Operator"
    elif obj.TYPE == ps.T_DICT:
        return f"Dict({len(obj.val)} entries)"
    elif obj.TYPE == ps.T_LOOP:
        return f"Loop(type={obj.val})"
    elif obj.TYPE == ps.T_STOPPED:
        return f"Stopped"
    else:
        return f"{type(obj).__name__}"


def exec_exec(ctxt: "ps.Context", o_stack: "ps.Stack", e_stack: "ps.Stack") -> None:
    """
    Main PostScript execution engine - the heart of the interpreter.

    IMPORTANT: A Cython-compiled copy of this function exists in
    postforge/operators/_control_cy.pyx. If you modify this function, you MUST
    also update the Cython version and rebuild with ./build_cython.sh.
    The Cython version includes additional optimizations (inlined dict
    lookup in the NAME path) so the two are not structurally identical.

    This function implements the core PostScript execution model by processing
    objects from the execution stack in a continuous **loop**. It handles all types
    of PostScript objects according to their executability and access permissions.

    EXECUTION FLOW (for sequence diagrams):
        Main execution **loop** processes objects from execution stack:
        
        1. LITERAL OBJECTS (Int, Real, Bool, Null, Mark, or ATTRIB_LIT):
           → Push directly to operand stack
           
        2. OPERATOR OBJECTS (ps.Operator):
           → Execute operator function directly
           → Operator pops operands from o_stack, executes, pushes results
           
        3. NAME OBJECTS (ps.Name):
           → Dictionary lookup via ps_dict.lookup()
           → Replace name on e_stack with looked-up object
           → Continue execution with the replacement object
           
        4. EXECUTABLE ARRAYS (procedures) and STRINGS:
           → Execute contents by pushing elements to execution stack
           → Arrays: push next element, advance pointer
           → Strings: tokenize and push resulting objects
           
        5. TOKENIZABLE OBJECTS (Run, File, String):
           → Call tokenizer to extract next PostScript object
           → Push tokenized object for execution

    The function continues until the execution stack is empty or an error occurs.
    All PostScript language semantics are implemented through this execution **loop**.

    Args:
        ctxt: PostScript execution context containing all interpreter state
        o_stack: Operand stack for data values and procedure arguments
        e_stack: Execution stack containing objects to be executed

    PostScript Compliance:
        - Follows PostScript Language Reference Manual execution model
        - Proper handling of executable vs literal objects
        - Access control enforcement for security
        - Error propagation through PostScript error handling system
    """

    while e_stack:
        # Cache top-of-stack to avoid repeated list.__getitem__(-1) calls
        top = e_stack[-1]

        # Periodic event loop callback for GUI responsiveness
        if ctxt.event_loop_callback is not None:
            ctxt._event_loop_counter += 1
            if ctxt._event_loop_counter >= 10000:
                ctxt._event_loop_counter = 0
                ctxt.event_loop_callback()

        # Record every object being processed by the execution engine
        if ctxt.execution_history_enabled and not ctxt.execution_history_paused:
            if top.TYPE not in ps.TOKENIZABLE_TYPES:
                # Record this in execution history - use copy to preserve state
                ctxt.record_execution(copy.copy(top), None)

        # EXECUTION PATH 1: LITERAL OBJECTS
        # All literal objects should be pushed onto the operand stack
        if top.TYPE in ps.LITERAL_TYPES or top.attrib == ps.ATTRIB_LIT:
            o_stack.append(top)
            e_stack.pop()
            continue

        # EXECUTION PATH 2: OPERATOR OBJECTS
        # Execute operator function directly
        elif top.TYPE == ps.T_OPERATOR:
            e_stack.pop()
            top.val(ctxt, o_stack)
            continue

        # EXECUTION PATH 3: NAME OBJECTS
        # Dictionary lookup, replace name with looked-up object on e_stack
        elif top.TYPE == ps.T_NAME:
            obj = ps_dict.lookup(ctxt, top)
            if obj is None:
                ps_error.e(ctxt, ps_error.UNDEFINED, top.val.decode("ascii"))
                continue
            # Replace the name with the looked-up object for continued execution
            # Operators are immutable - skip copy
            if obj.TYPE == ps.T_OPERATOR:
                e_stack[-1] = obj
            else:
                e_stack[-1] = obj.__copy__()  # Direct call avoids copy module overhead
            continue

        # EXECUTION PATH 4: TOKENIZABLE OBJECTS (Run, File, String)
        # Call tokenizer to extract next PostScript object from stream
        elif top.TYPE in ps.TOKENIZABLE_TYPES:
            success, er_name, command, do_exec = ps_token.__token(ctxt, e_stack)
            if not success:
                # pop the false off the operand stack
                o_stack.pop()
                ps_error.e(ctxt, er_name, command)
                continue

            success = o_stack[-1].val

            # pop the true/false object off the operand stack
            o_stack.pop()

            # if return value is false - pop the stream object off the execution stack
            if not success:
                e_stack.pop()
            else:
                if o_stack[-1].TYPE == ps.T_NAME and o_stack[-1].val == b"breaki":
                    e_stack.append(o_stack.pop())
                    continue
                else:
                    if do_exec:
                        ps_exec(ctxt, o_stack)
            continue

        # EXECUTION PATH 5: EXECUTABLE ARRAYS (procedures)
        # Execute contents by pushing elements to execution stack
        elif top.TYPE in ps.ARRAY_TYPES:  # an executable array (procedure)

            if not top.length:
                # an empty executable array
                e_stack.pop()
                continue

            if top.length == 1:
                # Only one item left in this procedure
                # Replace the entire procedure with the last item of the procedure.
                # This is the same as popping the procedure itself,
                # and pushing the last item of the procedure on the stack.

                obj = top.val[top.start]

                if obj.TYPE in ps.ARRAY_TYPES and obj.attrib == ps.ATTRIB_EXEC:
                    # Copy executable arrays to prevent cvx/cvlit from corrupting
                    # the original procedure (same protection as literal objects).
                    o_stack.append(obj.__copy__())
                    top.length -= 1
                    top.start += 1
                elif obj.attrib == ps.ATTRIB_LIT:
                    # Copy literal objects to prevent cvx/cvlit from corrupting
                    # the original procedure. Skip copy for immutable value types
                    # (Int, Real, Bool, Null, Mark) which are never mutated.
                    if obj.TYPE in ps.LITERAL_TYPES:
                        o_stack.append(obj)
                    else:
                        o_stack.append(copy.copy(obj))
                    top.length -= 1
                    top.start += 1
                else:
                    e_stack[-1] = top.val[top.start]
            else:
                obj = top.val[top.start]

                if obj.TYPE in ps.ARRAY_TYPES and obj.attrib == ps.ATTRIB_EXEC:
                    # Copy executable arrays to prevent cvx/cvlit from corrupting
                    # the original procedure (same protection as literal objects).
                    o_stack.append(obj.__copy__())
                elif obj.attrib == ps.ATTRIB_LIT:
                    # Copy literal objects to prevent cvx/cvlit from corrupting
                    # the original procedure. Skip copy for immutable value types.
                    if obj.TYPE in ps.LITERAL_TYPES:
                        o_stack.append(obj)
                    else:
                        o_stack.append(copy.copy(obj))
                else:
                    e_stack.append(obj)

                top.length -= 1
                top.start += 1
            continue

        elif top.TYPE == ps.T_STOPPED:
            # this stopped context was not stopped
            # push false onto the operand stack
            ctxt.o_stack.append(ps.Bool(False))
            ctxt.e_stack.pop()
            continue

        elif top.TYPE == ps.T_LOOP:
            if top.val == ps.LT_LOOP:
                # push a copy of the procedure onto the execution stack
                e_stack.append(copy.copy(top.proc))
                continue

            elif top.val == ps.LT_REPEAT:
                proc = top.proc
                top.limit -= 1
                if not top.limit:
                    # the last one
                    e_stack[-1] = copy.copy(proc)
                else:
                    # push a copy of the procedure onto the execution stack
                    e_stack.append(copy.copy(proc))
                continue

            elif top.val == ps.LT_FOR:
                if top.increment >= 0:
                    if top.control <= top.limit:
                        if type(top.control) == int:
                            ctxt.o_stack.append(ps.Int(top.control))
                        else:
                            ctxt.o_stack.append(ps.Real(top.control))
                        top.control += top.increment
                        # push a copy of the procedure onto the execution stack
                        e_stack.append(copy.copy(top.proc))
                    else:
                        # pop the for loop
                        e_stack.pop()
                else:
                    if top.control >= top.limit:
                        if type(top.control) == int:
                            ctxt.o_stack.append(ps.Int(top.control))
                        else:
                            ctxt.o_stack.append(ps.Real(top.control))
                        top.control += top.increment
                        # push a copy of the procedure onto the execution stack
                        e_stack.append(copy.copy(top.proc))
                    else:
                        # pop the for loop
                        e_stack.pop()
                continue

            elif top.val == ps.LT_FORALL:
                # push the obj's next item onto the operand stack
                if top.obj.TYPE == ps.T_STRING:
                    if top.obj.length:
                        # push the next item onto the operand stack
                        strings = (
                            ps.global_resources.global_strings
                            if top.obj.is_global
                            else ps.contexts[top.obj.ctxt_id].local_strings
                        )
                        ctxt.o_stack.append(
                            ps.Int(
                                strings[top.obj.offset + top.obj.start]
                            )
                        )
                        top.obj.start += 1
                        top.obj.length -= 1

                        # now push a copy of the procedure onto the execution stack
                        e_stack.append(copy.copy(top.proc))
                    else:
                        # pop the forall loop off the execution stack
                        e_stack.pop()
                    continue

                elif top.obj.TYPE in ps.ARRAY_TYPES:
                    if top.obj.length:
                        # push the next item onto the operand stack
                        elem = top.obj.val[top.obj.start]
                        # Defensive check: wrap raw Python types if they sneak into arrays
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
                                elem = ps.Name(elem, is_global=ctxt.vm_alloc_mode)
                        ctxt.o_stack.append(elem)
                        top.obj.start += 1
                        top.obj.length -= 1

                        # now push a copy of the procedure onto the execution stack
                        e_stack.append(copy.copy(top.proc))
                    else:
                        # pop the forall loop off the execution stack
                        e_stack.pop()
                    continue

                elif top.obj.TYPE == ps.T_DICT:
                    try:
                        key, val = top.generator.__next__()
                    except StopIteration:
                        # all done - pop the forall loop off the execution stack
                        e_stack.pop()
                        continue

                    try:
                        # Wrap raw Python key types as PSObjects
                        if not isinstance(key, ps.PSObject):
                            if isinstance(key, bool):
                                key = ps.Bool(key)
                            elif isinstance(key, int):
                                key = ps.Int(key)
                            elif isinstance(key, float):
                                key = ps.Real(key)
                            elif key is None:
                                key = ps.Null()
                            elif isinstance(key, bytes):
                                key = ps.Name(key, is_global=ctxt.vm_alloc_mode)
                        # skip the __status__ key
                        try:
                            while key.val == b"__status__":
                                key, val = top.generator.__next__()
                        except StopIteration:
                            # all done - pop the forall loop off the execution stack
                            e_stack.pop()
                            continue
                        # Wrap raw Python val types as PSObjects (defensive)
                        if not isinstance(val, ps.PSObject):
                            if isinstance(val, bool):
                                val = ps.Bool(val)
                            elif isinstance(val, int):
                                val = ps.Int(val)
                            elif isinstance(val, float):
                                val = ps.Real(val)
                            elif val is None:
                                val = ps.Null()
                            elif isinstance(val, bytes):
                                val = ps.Name(val, is_global=ctxt.vm_alloc_mode)
                        # push the key and value onto the operand stack
                        ctxt.o_stack.append(key)
                        ctxt.o_stack.append(val)

                        # now push a copy of the procedure onto the execution stack
                        e_stack.append(copy.copy(top.proc))
                    except (IndexError, AttributeError, TypeError, ValueError):
                        # Some other error - this should not happen
                        # pop the forall loop off the execution stack
                        e_stack.pop()
                continue

            elif top.val == ps.LT_FILENAMEFORALL:
                try:
                    fname = top.generator.__next__()
                    if len(fname) > top.scratch.length:
                        ps_error.e(ctxt, ps_error.RANGECHECK, "filenameforall")
                        continue
                    substring = copy.copy(top.scratch)
                    dst = (
                        ps.global_resources.global_strings
                        if top.scratch.is_global
                        else ctxt.local_strings
                    )
                    dst[
                        substring.offset
                        + substring.start : substring.offset
                        + substring.start
                        + len(fname)
                    ] = fname
                    substring.length = len(fname)

                    # push the substring onto the operand stack
                    o_stack.append(substring)
                    # now push a copy of the procedure onto the execution stack
                    e_stack.append(copy.copy(top.proc))
                except (StopIteration, IndexError, AttributeError, TypeError, ValueError):
                    # pop the filenameforall loop off the execution stack
                    e_stack.pop()
                continue

            elif top.val == ps.LT_PATHFORALL:
                path = top.path
                path_index = top.path_index
                sub_path_index = top.sub_path_index
                moveto_proc = top.moveto_proc
                lineto_proc = top.lineto_proc
                curveto_proc = top.curveto_proc
                closepath_proc = top.closepath_proc
                pathforall_popped = False

                if (
                    sub_path_index == len(path[path_index]) - 1
                    and path_index == len(path) - 1
                ):
                    # this is the last path item - pop the execution stack
                    e_stack.pop()
                    pathforall_popped = True

                if (
                    path_index < len(path) + 1
                    and sub_path_index < len(path[path_index]) + 1
                ):
                    # push a copy of the appropriate procedure onto the execution stack
                    if isinstance(
                        path[path_index][sub_path_index], (ps.MoveTo, ps.LineTo)
                    ):
                        x, y = ps_matrix._transform_point(
                            ctxt.gstate.iCTM,
                            path[path_index][sub_path_index].p.x,
                            path[path_index][sub_path_index].p.y,
                        )
                        o_stack.append(ps.Real(x))
                        o_stack.append(ps.Real(y))
                        if isinstance(path[path_index][sub_path_index], ps.MoveTo):
                            e_stack.append(copy.copy(moveto_proc))
                        else:
                            e_stack.append(copy.copy(lineto_proc))
                    if isinstance(path[path_index][sub_path_index], ps.CurveTo):
                        x, y = ps_matrix._transform_point(
                            ctxt.gstate.iCTM,
                            path[path_index][sub_path_index].p1.x,
                            path[path_index][sub_path_index].p1.y,
                        )
                        o_stack.append(ps.Real(x))
                        o_stack.append(ps.Real(y))
                        x, y = ps_matrix._transform_point(
                            ctxt.gstate.iCTM,
                            path[path_index][sub_path_index].p2.x,
                            path[path_index][sub_path_index].p2.y,
                        )
                        o_stack.append(ps.Real(x))
                        o_stack.append(ps.Real(y))
                        x, y = ps_matrix._transform_point(
                            ctxt.gstate.iCTM,
                            path[path_index][sub_path_index].p3.x,
                            path[path_index][sub_path_index].p3.y,
                        )
                        o_stack.append(ps.Real(x))
                        o_stack.append(ps.Real(y))
                        e_stack.append(copy.copy(curveto_proc))
                    if isinstance(path[path_index][sub_path_index], ps.ClosePath):
                        e_stack.append(copy.copy(closepath_proc))

                    if not pathforall_popped:
                        e_stack[-2].sub_path_index += 1
                        if e_stack[-2].sub_path_index == len(path[path_index]):
                            e_stack[-2].sub_path_index = 0
                            e_stack[-2].path_index += 1
                else:
                    e_stack.pop()
                continue

            elif top.val == ps.LT_CSHOW:
                # cshow: for each character, push wx wy charcode and call proc
                if top.obj.length:
                    strings = (
                        ps.global_resources.global_strings
                        if top.obj.is_global
                        else ps.contexts[top.obj.ctxt_id].local_strings
                    )

                    # Check if current font is Type 0 (composite) for multi-byte decoding
                    current_font = ctxt.gstate.font
                    font_type = 1
                    if current_font is not None:
                        ft_obj = current_font.val.get(b'FontType')
                        if ft_obj and ft_obj.TYPE in ps.NUMERIC_TYPES:
                            font_type = ft_obj.val

                    if font_type == 0:
                        # Type 0: decode multi-byte character from CMap codespace
                        cmap_dict = current_font.val.get(b'CMap')
                        byte_width = 1
                        if cmap_dict and cmap_dict.TYPE == ps.T_DICT:
                            codespace = cmap_dict.val.get(b'CodeSpaceRange')
                            if codespace and codespace.TYPE in ps.ARRAY_TYPES and len(codespace.val) >= 2:
                                lo = codespace.val[0]
                                if lo.TYPE == ps.T_STRING:
                                    lo_bytes = lo.byte_string()
                                    if isinstance(lo_bytes, str):
                                        lo_bytes = lo_bytes.encode('latin-1')
                                    byte_width = len(lo_bytes)

                        if top.obj.length >= byte_width:
                            # Build full multi-byte character code for CID lookup
                            full_char_code = 0
                            for bw in range(byte_width):
                                full_char_code = (full_char_code << 8) | strings[top.obj.offset + top.obj.start]
                                top.obj.start += 1
                                top.obj.length -= 1
                            # Per PLRM: push last byte as char code for proc
                            char_code = full_char_code & 0xFF
                            # Store the full CID on context so show can use it
                            # (PLRM: "the glyph actually shown is the one identified
                            # by the originally selected CID")
                            ctxt._cshow_pending_cid = full_char_code
                        else:
                            # Not enough bytes left for a complete character
                            e_stack.pop()
                            continue
                    else:
                        # Single-byte font
                        char_code = strings[top.obj.offset + top.obj.start]
                        top.obj.start += 1
                        top.obj.length -= 1

                    # Get character width from current font
                    wx, wy = 0.0, 0.0
                    if current_font is not None and font_type != 0:
                        try:
                            encoding = current_font.val.get(b'Encoding')
                            if encoding and encoding.TYPE in ps.ARRAY_TYPES:
                                glyph_name_obj = encoding.val[encoding.start + char_code]
                                glyph_name = glyph_name_obj.val if hasattr(glyph_name_obj, 'val') else b'.notdef'
                            else:
                                glyph_name = b'.notdef'
                            metrics = current_font.val.get(b'Metrics')
                            char_strings = current_font.val.get(b'CharStrings')
                            # Try Metrics dict first (PLRM 5.9.2)
                            # Check int char code (DVIPS) then glyph name (PLRM standard)
                            w = None
                            if metrics and metrics.TYPE == ps.T_DICT:
                                w = metrics.val.get(char_code)
                                if w is None:
                                    w = metrics.val.get(glyph_name)
                            if w is not None and hasattr(w, 'TYPE'):
                                if w.TYPE in ps.NUMERIC_TYPES:
                                    mw = float(w.val)
                                elif w.TYPE in ps.ARRAY_TYPES and len(w.val) >= 2:
                                    mw = float(w.val[0].val) if w.val[0].TYPE in ps.NUMERIC_TYPES else None
                                else:
                                    mw = None
                                if mw is not None:
                                    # Convert character space to user space via FontMatrix[0]
                                    fm = current_font.val.get(b'FontMatrix')
                                    if fm and fm.TYPE in ps.ARRAY_TYPES and fm.val:
                                        wx = mw * float(fm.val[0].val)
                                    else:
                                        wx = mw * 0.001
                        except Exception:
                            pass

                    # PLRM: push charcode wx wy (charcode deepest, wy on top)
                    o_stack.append(ps.Int(char_code))
                    o_stack.append(ps.Real(wx))
                    o_stack.append(ps.Real(wy))

                    # push a copy of the procedure onto the execution stack
                    e_stack.append(copy.copy(top.proc))
                else:
                    # Clean up cshow pending CID when loop ends
                    if hasattr(ctxt, '_cshow_pending_cid'):
                        delattr(ctxt, '_cshow_pending_cid')
                    e_stack.pop()
                continue

            elif top.val == ps.LT_KSHOW:
                # kshow: render each glyph, call proc between adjacent chars
                # PLRM: "When proc completes execution, the value of
                # currentfont is restored."
                strings = (
                    ps.global_resources.global_strings
                    if top.obj.is_global
                    else ps.contexts[top.obj.ctxt_id].local_strings
                )

                if top.obj.length:
                    # Restore font saved before proc (PLRM requirement)
                    if hasattr(top, '_saved_font') and top._saved_font is not None:
                        ctxt.gstate.font = top._saved_font

                    char_code = strings[top.obj.offset + top.obj.start]
                    top.obj.start += 1
                    top.obj.length -= 1

                    # Render the current glyph
                    current_font = ctxt.gstate.font
                    font_type = current_font.val.get(b'FontType', ps.Int(1)).val if current_font else 1
                    from .show_variants import _render_and_advance_single_glyph
                    _render_and_advance_single_glyph(ctxt, current_font, char_code, font_type)

                    # If there's a next character, push both char codes and call proc
                    if top.obj.length:
                        next_char_code = strings[top.obj.offset + top.obj.start]
                        o_stack.append(ps.Int(char_code))
                        o_stack.append(ps.Int(next_char_code))
                        # Save font before proc (restored on next iteration)
                        top._saved_font = ctxt.gstate.font
                        e_stack.append(copy.copy(top.proc))
                    # else: was last glyph, loop will pop on next iteration
                    #       (top.obj.length is now 0)
                else:
                    # Restore font on final exit too
                    if hasattr(top, '_saved_font') and top._saved_font is not None:
                        ctxt.gstate.font = top._saved_font
                    e_stack.pop()
                continue

        elif top.TYPE == ps.T_HARD_RETURN:
            e_stack.pop()
            return


# Try to use Cython-compiled exec_exec if available
try:
    from ._control_cy import exec_exec as _cy_exec_exec
    exec_exec = _cy_exec_exec
except ImportError:
    pass


def countexecstack(ctxt, ostack):
    """
    - **countexecstack** int


    counts the number of objects on the execution stack and pushes this count
    on the operand stack.

    **Errors**:     **stackoverflow**
    **See Also**:   **execstack**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, countexecstack.__name__)

    ostack.append(ps.Int(len(ctxt.e_stack)))


def ps_exit(ctxt, ostack):
    """
    - **exit** -


    terminates execution of the innermost, dynamically enclosing instance of a looping
    context without regard to lexical relationship. A looping context is a procedure
    invoked repeatedly by one of the following control operators:

        **cshow**               **forall**      **pathforall**
        **filenameforall**      **kshow**       **repeat**
        **for**                 **loop**        **resourceforall**

    **exit** pops the execution stack down to the level of that operator. The interpreter
    then resumes execution at the next object in normal sequence after that operator.

    **exit** does not affect the operand stack or dictionary stack. Any objects pushed on
    these stacks during execution of the looping context remain after the context is
    exited.

    If **exit** would escape from the context of a **run** or **stopped** operator, an **invalidexit**
    error occurs (still in the context of the **run** or **stopped**). If there is no enclosing
    looping context, the interpreter prints an error message and executes the built-in
    operator **quit**. This never occurs during execution of ordinary user programs, because
    they are enclosed by a **stopped** context.

    **Errors**:     **invalidexit**
    **See Also**:   **stop**, **stopped**
    """
    op = "exit"

    while len(ctxt.e_stack):
        if ctxt.e_stack[-1].TYPE == ps.T_LOOP:
            # pop the loop item off the execution stack and return
            ctxt.e_stack.pop()
            return

        elif ctxt.e_stack[-1].TYPE == ps.T_FILE:
            # close the file
            ctxt.e_stack[-1].close()
            ctxt.e_stack.pop()

        elif ctxt.e_stack[-1].TYPE == ps.T_STOPPED:
            return ps_error.e(ctxt, ps_error.INVALIDEXIT, op)

        elif type(ctxt.e_stack[-1]) == ps.Run:
            ctxt.e_stack[-1].close()
            ctxt.e_stack.pop()
            return ps_error.e(ctxt, ps_error.INVALIDEXIT, op)

        else:
            ctxt.e_stack.pop()

    # No enclosing looping context found
    print("Unrecoverable Error - No enclosing looping context in exit")
    quit()


def execstack(ctxt, ostack):
    """
    array **execstack** subarray


    stores all elements of the execution stack into array and returns an object describing
    the initial n-element subarray of array, where n is the current depth of the execution
    stack. **execstack** copies the topmost object into element n - 1 of array and
    the bottommost one into element 0 of array. The execution stack itself is unchanged.
    If the length of array is less than the depth of the execution stack, a
    **rangecheck** error occurs.

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **countexecstack**, **exec**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, execstack.__name__)
    # 2. TYPECHECK - Check operand type (array)
    if ostack[-1].TYPE != ps.T_ARRAY:
        return ps_error.e(ctxt, ps_error.TYPECHECK, execstack.__name__)
    # 3. INVALIDACCESS - Check access permission (default ACCESS_READ_ONLY)
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, execstack.__name__)

    if len(ostack[-1].val) < len(ctxt.e_stack):
        return ps_error.e(ctxt, ps_error.RANGECHECK, execstack.__name__)

    # check for invalid access
    if ostack[-1].is_global:
        for i in range(-1, -(len(ctxt.e_stack) + 1), -1):
            if ctxt.e_stack[i].is_composite and not ctxt.e_stack[i].is_global:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, execstack.__name__)

    sub_array = copy.copy(ostack[-1])
    sub_array.length = len(ctxt.e_stack)
    index = len(ctxt.e_stack) - 1
    for i in range(-1, -len(ctxt.e_stack) - 1, -1):
        success, e = sub_array.put(ps.Int(index), ctxt.e_stack[i])
        if not success:
            return ps_error.e(ctxt, e, execstack.__name__)
        index -= 1

    ostack[-1] = sub_array


def stop(ctxt, ostack):
    """
    - **stop** -


    terminates execution of the innermost, dynamically enclosing instance of a
    **stopped** context, without regard to lexical relationship. A **stopped** context is a
    procedure or other executable object invoked by the **stopped** operator. **stop** pops
    the execution stack down to the level of the **stopped** operator. The interpreter
    then pushes the boolean value true on the operand stack and resumes execution at
    the next object in normal sequence after the **stopped** operator. It thus appears
    that **stopped** returned the value true, whereas it normally returns false.

    **stop** does not affect the operand stack or dictionary stack. Any objects pushed on
    these stacks during the execution of the **stopped** context remain after the context
    is terminated.

    If **stop** is executed when there is no enclosing **stopped** context, the interpreter
    prints an error message and executes the built-in operator **quit**. This never occurs
    during execution of ordinary user programs.

    **Errors**:     none
    **See Also**:   **stopped**, **exit**
    """

    while len(ctxt.e_stack):
        if ctxt.e_stack[-1].TYPE == ps.T_STOPPED:
            # pop the stopped item off the execution stack
            ctxt.e_stack.pop()
            # push true onto the operand stack
            ctxt.o_stack.append(ps.Bool(True))

            # Clean up any orphaned resource category implementation dictionaries
            # that were pushed to d_stack by resource operators (findresource,
            # defineresource, etc.) but not cleaned up due to the error unwinding
            # past their cleanup code. These dictionaries have the required category
            # implementation keys: DefineResource, FindResource, etc.
            _cleanup_orphaned_category_dicts(ctxt)
            return

        elif ctxt.e_stack[-1].TYPE == ps.T_FILE:  # This includes Run type since Run inherits from File
            # close the file
            ctxt.e_stack[-1].close()
            ctxt.e_stack.pop()

        else:
            ctxt.e_stack.pop()

    # No enclosing stopped context found

    print("Unrecoverable Error - stop")
    quit()


def _cleanup_orphaned_category_dicts(ctxt):
    """
    Clean up any orphaned resource category implementation dictionaries from d_stack.

    When resource operators (**findresource**, **defineresource**, etc.) push the category
    implementation dictionary onto d_stack and then an error occurs, the **stop** operator
    unwinds the e_stack past the cleanup code, leaving the impl dict on d_stack.

    This function identifies and removes such dictionaries by checking if the top
    of d_stack is a category implementation dictionary (has the required keys:
    DefineResource, FindResource, UndefineResource, ResourceStatus, ResourceForAll).

    We only clean up above the base dictionaries (**systemdict**, **globaldict**, **userdict**).
    """
    # Category implementation dictionaries have these required keys
    required_keys = {b"DefineResource", b"FindResource", b"UndefineResource",
                     b"ResourceStatus", b"ResourceForAll"}

    # Keep cleaning up while there are orphaned category dicts on top of d_stack
    # Stop when we reach the base dictionaries (systemdict, globaldict, userdict)
    while len(ctxt.d_stack) > 3:  # Keep the base 3 dictionaries
        top_dict = ctxt.d_stack[-1]

        # Check if this looks like a category implementation dictionary
        if top_dict.TYPE == ps.T_DICT:
            dict_keys = set(top_dict.val.keys())
            if required_keys.issubset(dict_keys):
                # This is a category implementation dictionary - remove it
                ctxt.d_stack.pop()
                continue

        # If top dict is not a category impl dict, stop cleaning
        break


def stopped(ctxt, ostack):
    """
    any **stopped** bool


    executes any, which is typically, but not necessarily, a procedure, executable file,
    or executable string object. If any runs to completion normally, **stopped** returns
    false on the operand stack. If any terminates prematurely as a result of executing
    **stop**, **stopped** returns true. Regardless of the outcome, the interpreter resumes execution
    at the next object in normal sequence after **stopped**.

    This mechanism provides an effective way for a PostScript program to “catch”
    errors or other premature terminations, retain control, and perhaps perform its
    own error recovery. See Section 3.11, "**Errors**."

    When an error occurs, the standard error handler sets **newerror** to true in the
    $**error** dictionary. When using **stopped** to catch and continue from an error
    (without invoking **handleerror**), it is prudent to explicitly reset **newerror** to false
    in $**error**; otherwise, any subsequent execution of **stop** may result in inadvertent
    reporting of the leftover error. Also, note that the standard error handler sets the
    VM allocation mode to local.

    **Example**
        { ... } **stopped**
            {handleerror}
        if

    If execution of the procedure { ... } causes an error, the default error reporting
    procedure is invoked (by **handleerror**). In any event, normal execution continues
    at the token following the **if** operator.

    **Errors**:     **stackunderflow**
    **See Also**:   **stop**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, stopped.__name__)

    # push a stopped object onto the execution stack
    ctxt.e_stack.append(ps.Stopped())

    # push the top of the operand stack onto the execution stack
    ctxt.e_stack.append(copy.copy(ctxt.o_stack[-1]))
    ostack.pop()


def ps_if(ctxt, ostack):
    """
    bool proc **if** -


    removes both operands from the stack, then executes proc if bool is true. The **if** operator
    pushes no results of its own on the operand stack, but proc may do so (see
    Section 3.5, "Execution").

    Example
        3 4 lt {(3 is less than 4)} if      -> (3 is less than 4)

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **ifelse**
    """
    op = "if"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand types (boolean procedure)
    if ostack[-2].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    if ctxt.o_stack[-2].val:
        ctxt.e_stack.append(copy.copy(ctxt.o_stack[-1]))

    ctxt.o_stack.pop()
    ctxt.o_stack.pop()


def ifelse(ctxt, ostack):
    """
    bool proc₁ proc₂ **ifelse** -


    removes all three operands from the stack, then executes proc₁ if bool is true or
    proc₂ if bool is false. The **ifelse** operator pushes no results of its own on the operand
    stack, but the procedure it executes may do so (see Section 3.5, "Execution").

    **Example**
        4 3 lt
            {(TruePart)}
            {(FalsePart)}
        **ifelse**              -> (FalsePart)      % Since 4 is not less than 3

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **if**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ifelse.__name__)
    # 2. TYPECHECK - Check operand types (bool proc1 proc2)
    if ostack[-3].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ifelse.__name__)
    if ostack[-2].TYPE not in ps.ARRAY_TYPES or ostack[-2].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ifelse.__name__)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ifelse.__name__)

    if ctxt.o_stack[-3].val:
        ctxt.e_stack.append(copy.copy(ctxt.o_stack[-2]))
    else:
        ctxt.e_stack.append(copy.copy(ctxt.o_stack[-1]))

    ctxt.o_stack.pop()
    ctxt.o_stack.pop()
    ctxt.o_stack.pop()


def ps_for(ctxt, ostack):
    """
    initial increment limit proc **for** -


    executes the procedure proc repeatedly, passing it a sequence of values from initial
    by steps of increment to limit. The **for** operator expects initial, increment, and limit to
    be numbers. It maintains a temporary internal variable, **known** as the control
    variable, which it first sets to initial. Then, before each repetition, it compares the
    control variable to the termination value limit. If limit has not been exceeded, **for**
    pushes the control variable on the operand stack, executes proc, and adds increment
    to the control variable.

    The termination condition depends on whether increment is positive or negative.
    If increment is positive, **for** terminates when the control variable becomes greater
    than limit. If increment is negative, **for** terminates when the control variable becomes
    less than limit. If initial meets the termination condition, **for** does not execute
    proc at all. If proc executes the exit operator, **for** terminates prematurely.

    Usually, proc will use the value on the operand stack **for** some purpose. However,
    if proc does not remove the value, it will remain there. Successive executions of
    proc will cause successive values of the control variable to accumulate on the
    operand stack.

    **Examples**
        0 1 1 4 {add} **for**       -> 10
        1 2 6 { } **for**           -> 1 3 5
        3 -.5 1 { } **for**         -> 3.0 2.5 2.0 1.5 1.0

    In the first example above, the value of the control variable is added to whatever is
    on the stack, so 1, 2, 3, and 4 are added in turn to a running sum whose initial value
    is 0. The second example has an empty procedure, so the successive values of
    the control variable are left on the stack. The last example counts backward from
    3 to 1 by halves, leaving the successive values on the stack.

    Beware of using real numbers instead of integers **for** any of the first three operands.
    Most real numbers are not represented exactly. This can cause an error to
    accumulate in the value of the control variable, with possibly surprising results. In
    particular, if the difference between initial and limit is a multiple of increment, as in
    the last example, the control variable may not achieve the limit value.

    **Errors**:     **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **repeat**, **loop**, **forall**, **exit**
    """
    op = "for"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 4:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand types (initial increment limit proc)
    for n in range(-4, -1):
        if ostack[n].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    # Only convert control variable to real if initial or
    # increment is real. A real limit alone does not force real control values.
    make_control_real = (ostack[-4].TYPE == ps.T_REAL or ostack[-3].TYPE == ps.T_REAL)

    for_loop = ps.Loop(ps.LT_FOR)
    for_loop.control = (
        float(ctxt.o_stack[-4].val) if make_control_real else ctxt.o_stack[-4].val
    )
    for_loop.increment = (
        float(ctxt.o_stack[-3].val) if make_control_real else ctxt.o_stack[-3].val
    )
    for_loop.limit = float(ctxt.o_stack[-2].val) if ostack[-2].TYPE == ps.T_REAL else ctxt.o_stack[-2].val
    for_loop.proc = ctxt.o_stack[-1]

    # push the for loop onto the execution stack
    ctxt.e_stack.append(for_loop)

    ctxt.o_stack.pop()
    ctxt.o_stack.pop()
    ctxt.o_stack.pop()
    ctxt.o_stack.pop()


def forall(ctxt, ostack):
    """
          array proc **forall** -
    **packedarray** proc **forall** -
           dict proc **forall** -
         string proc **forall** -


    enumerates the elements of the first operand, executing the procedure proc for
    each element. If the first operand is an array, packed array, or string object, **forall**
    pushes an element on the operand stack and executes proc for each element in the
    object, beginning with the element whose index is 0 and continuing sequentially.
    In the case of a string, the elements pushed on the operand stack are integers in
    the range 0 to 255, not 1-character strings.

    If the first operand is a dictionary, **forall** pushes a key and a value on the operand
    stack and executes proc for each key-value pair in the dictionary. The order in
    which **forall** enumerates the entries in the dictionary is arbitrary. New entries put
    in the dictionary during the execution of proc may or may not be included in the
    enumeration. Existing entries removed from the dictionary by proc will not be encountered
    later in the enumeration.

    If the first operand is empty (that is, has length 0), **forall** does not execute proc at
    all. If proc executes the exit operator, **forall** terminates prematurely.

    Although **forall** does not leave any results on the operand stack when it is finished,
    the execution of proc may leave arbitrary results there. If proc does not remove
    each enumerated element from the operand stack, the elements will accumulate
    there.

    **Examples**
        0 [13 29 3 -8 21] {add} **forall**          -> 58
        /d 2 dict def
        d /abc 123 put
        d /xyz (test) put
        d {} **forall**                             -> /xyz (test) /abc 123
                                            or  -> /abc 123 /xyz (test)

    **Errors**:     **invalidaccess**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **for**, **repeat**, **loop**, **exit**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, forall.__name__)
    # 2. TYPECHECK - Check procedure type
    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, forall.__name__)

    if ostack[-1].access < ps.ACCESS_READ_ONLY or ostack[-2].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, forall.__name__)

    if ctxt.o_stack[-2].TYPE in [ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_STRING]:
        if ctxt.o_stack[-2].length:
            forall_loop = ps.Loop(ps.LT_FORALL)
            forall_loop.proc = ctxt.o_stack[-1]
            forall_loop.obj = copy.copy(ctxt.o_stack[-2])

            # push the forall loop onto the execution stack
            ctxt.e_stack.append(forall_loop)

    elif ctxt.o_stack[-2].TYPE == ps.T_DICT:
        if len(ctxt.o_stack[-2].val):
            forall_loop = ps.Loop(ps.LT_FORALL)
            forall_loop.proc = ctxt.o_stack[-1]
            forall_loop.obj = ctxt.o_stack[-2]

            # create a generator for looping through the dictionary
            forall_loop.generator = (
                (
                    (
                        ps.Name(key, is_global=ctxt.vm_alloc_mode)
                        if isinstance(key, bytes)
                        else key
                    ),
                    val,
                )
                for key, val in forall_loop.obj.val.items()
            )

            # push the forall loop onto the execution stack
            ctxt.e_stack.append(forall_loop)

    else:
        return ps_error.e(ctxt, ps_error.TYPECHECK, forall.__name__)

    ctxt.o_stack.pop()
    ctxt.o_stack.pop()


def loop(ctxt, ostack):
    """
    proc **loop** -


    repeatedly executes proc until proc executes the exit operator, at which point
    interpretation resumes at the object next in sequence after the **loop** operator.
    Control also leaves proc if the **stop** operator is executed. If proc never executes exit
    or **stop**, an infinite **loop** results, which can be broken only via an external interrupt
    (see **interrupt**).

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **for**, **repeat**, **forall**, **exit**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, loop.__name__)
    # 2. TYPECHECK - Check procedure type
    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, loop.__name__)

    the_loop = ps.Loop(ps.LT_LOOP)
    the_loop.proc = ctxt.o_stack[-1]

    # push the loop onto the execution stack
    ctxt.e_stack.append(the_loop)

    ctxt.o_stack.pop()


def repeat(ctxt, ostack):
    """
    int proc **repeat** -


    executes the procedure proc int times, where int is a nonnegative integer. This operator
    removes both operands from the stack before executing proc for the first
    time. If proc executes the exit operator, **repeat** terminates prematurely. **repeat**
    leaves no results of its own on the stack, but proc may do so.

    **Examples**
        4 {(abc)} **repeat**                    -> (abc) (abc) (abc) (abc)
        1 2 3 4 3 {pop} **repeat**              -> 1    % Pops 3 values (down to the 1)
        4 {} **repeat**                         ->      % Does nothing four times
        mark 0 {(will not happen)} **repeat**   -> mark

    In the last example above, a 0 **repeat** count means that the procedure is not executed
    at all, thus the mark is still topmost on the stack.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **for**, **loop**, **forall**, **exit**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, repeat.__name__)
    # 2. TYPECHECK - Check operand types (int proc)
    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, repeat.__name__)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, repeat.__name__)

    if ostack[-2].val < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, repeat.__name__)

    if not ostack[-2].val:
        ostack.pop()
        ostack.pop()
        return

    the_repeat = ps.Loop(ps.LT_REPEAT)
    the_repeat.limit = ostack[-2].val
    the_repeat.proc = ostack[-1]

    # push the loop onto the execution stack
    ctxt.e_stack.append(the_repeat)

    ostack.pop()
    ostack.pop()
