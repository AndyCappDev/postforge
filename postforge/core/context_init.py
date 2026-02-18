# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript context initialization.

Creates and initializes the PostScript execution environment including stacks,
virtual memory, graphics state, system dictionaries, and standard file objects.
"""

import os
import random
import sys
import time
from typing import Any, Dict, Optional, Tuple

from . import types as ps
from ..operators import control as ps_control
from ..operators import dict as ps_dict
from ..operators.matrix import _matrix_inverse


def init_system_params() -> Dict[str, Any]:
    """
    Initialize system parameters for PostScript interpreter.

    Creates a dictionary containing all the essential system parameters including
    resource directories, VM settings, and device configurations. These parameters
    are used throughout the PostScript execution environment.

    The function automatically detects the project structure and sets up paths
    relative to the main project directory, ensuring proper resource loading
    regardless of how PostForge is executed.

    Returns:
        Dict[str, Any]: System parameters dictionary containing:
            - ByteOrder: Boolean indicating byte order (True for big-endian)
            - CurrDisplayList: Current display list index
            - FontResourceDir: Path to font resources
            - GenericResourceDir: Path to generic resources
            - OutputDeviceResourceDir: Path to output device definitions
            - VMDir: Path for virtual memory storage
            - PageCount: Initial page count
            - PrinterName: Default printer name
            - RealFormat: Real number format ('IEE')
            - Revision: PostScript language revision level
    """

    # Get the directory containing the postforge package
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Get the parent directory (main project directory)
    project_dir = os.path.dirname(script_dir)

    return {
        "ProjectDir": project_dir,
        "ByteOrder": True,
        "CurrDisplayList": 0,
        "FontResourceDir": os.path.join(project_dir, "resources", "Font"),
        "GenericResourceDir": os.path.join(project_dir, "generic_resources"),
        "OutputDeviceResourceDir": os.path.join(
            project_dir, "resources", "OutputDevice"
        ),
        "VMDir": os.path.join(project_dir, "vm"),
        "PageCount": 0,
        "PrinterName": "PostForge",
        "RealFormat": "IEE",
        "Revision": 1,
        "SystemParamsPassword": "0",  # Default SystemPassword
        "StartJobPassword": "0",  # Default password for startjob/exitserver operators (PLRM Section 3.7.7)
        # Cache size limits (PLRM Table C.2 - system parameters)
        "MaxFontCache": 67108864,
        "MaxFormCache": 131072,
        "MaxPatternCache": 131072,
        "MaxUPathCache": 131072,
        "MaxScreenStorage": 524288,
        "MaxDisplayList": 2097152,
        "MaxDisplayAndSourceList": 4194304,
        "MaxSourceList": 2097152,
        "MaxImageBuffer": 524288,
        "MaxOutlineCache": 65536,
        "MaxStoredScreenCache": 0,
        # Read-only current cache usage counters (PLRM Table C.2)
        "CurFontCache": 0,
        "CurFormCache": 0,
        "CurPatternCache": 0,
        "CurUPathCache": 0,
        "CurScreenStorage": 0,
        "CurSourceList": 0,
        "CurStoredScreenCache": 0,
        "CurOutlineCache": 0,
}


def create_context(
    system_params: Dict[str, Any],
) -> Tuple[Optional[ps.Context], Optional[str]]:
    """
    Create and initialize a complete PostScript execution context.

    This function sets up the entire PostScript runtime environment including:
    - All execution stacks (operand, execution, dictionary, graphics state)
    - Virtual memory management (local and global VM)
    - Resource dictionaries and system dictionaries
    - Graphics state initialization with identity transformation matrix
    - PostScript system initialization via sysdict.ps

    Args:
        system_params: Dictionary containing system parameters from init_system_params(),
                      including resource paths and configuration settings

    Returns:
        Tuple[Optional[ps.Context], Optional[str]]: Success/failure result
            - Success: (Context object, None) - Ready for PostScript execution
            - Failure: (None, error_description) - Initialization failed

    PostScript Stacks Created:
        - o_stack: Operand stack (500 element capacity)
        - e_stack: Execution stack (250 element capacity)
        - d_stack: Dictionary stack (250 element capacity)
        - g_stack: Graphics state stack (10 element capacity)

    VM Management:
        - lvm: Local virtual memory dictionary
        - gvm: Global virtual memory dictionary
        - Proper allocation mode handling for memory management
    """

    ctxt = ps.Context(system_params)

    ctxt.id = None
    # get an empty context slot
    for i in range(len(ps.contexts)):
        if ps.contexts[i] is None:
            ps.contexts[i] = ctxt
            ctxt.id = i
            break

    ctxt.o_stack = ps.Stack(ps.O_STACK_MAX)
    ctxt.e_stack = ps.Stack(ps.E_STACK_MAX)
    ctxt.d_stack = ps.Stack(ps.D_STACK_MAX)
    ctxt.g_stack = ps.Stack(ps.G_STACK_MAX)

    # the local vm dictionary
    ctxt.lvm = ps.Dict(ctxt.id, None, name="lvm", is_global=False)

    # Wire system params to GlobalResources so caches read limits from params
    ps.global_resources.set_system_params(system_params)

    # Initialize or get the global vm dictionary from GlobalResources
    ctxt.vm_alloc_mode = True
    if ps.global_resources.get_gvm() is None:
        # First context initializes the global VM
        gvm = ps.Dict(ctxt.id, None, name="gvm", is_global=True)
        ps.global_resources.set_gvm(gvm)
    ctxt.vm_alloc_mode = False

    # create the global resource dictionary (only if not already created)
    gvm = ps.global_resources.get_gvm()
    ctxt.vm_alloc_mode = True
    if b"resource" not in gvm.val:
        gvm.val[b"resource"] = ps.Dict(ctxt.id, None, name="resource", is_global=True)

        # define the Category resource dictionary in the resourse dictionary itself
        gvm.val[b"resource"].val[b"Category"] = ps.Dict(
            ctxt.id, name="Category", is_global=True
        )
    ctxt.vm_alloc_mode = False

    # add it to the global_ref dictionary
    ctxt.global_refs[gvm.val[b"resource"].val[b"Category"].created] = gvm.val[
        b"resource"
    ].val[b"Category"]

    # create the local resource dictionary
    ctxt.lvm.val[b"resource"] = ps.Dict(ctxt.id, None, name="resource", is_global=False)

    # initialize the UserParams Dictionary
    userparams = ps.Dict(ctxt.id, None, name=b"UserParams", is_global=False)
    userparams.put(ps.Name(b"MaxDictStack"), ps.Int(0))
    userparams.put(ps.Name(b"MaxExecStack"), ps.Int(0))
    userparams.put(ps.Name(b"MaxOpStack"), ps.Int(0))
    userparams.put(ps.Name(b"MaxFontItem"), ps.Int(0))
    userparams.put(ps.Name(b"MaxFormItem"), ps.Int(0))
    userparams.put(ps.Name(b"MaxPatternItem"), ps.Int(0))
    userparams.put(ps.Name(b"MaxUPathItem"), ps.Int(0))
    userparams.put(ps.Name(b"MaxScreenItem"), ps.Int(0))
    userparams.put(ps.Name(b"MaxSuperScreen"), ps.Int(0))
    userparams.put(ps.Name(b"MinFontCompress"), ps.Int(0))
    userparams.put(ps.Name(b"MaxLocalVM"), ps.Int(0))
    userparams.put(ps.Name(b"VMReclaim"), ps.Int(0))
    userparams.put(ps.Name(b"VMThreshold"), ps.Int(0))
    userparams.put(ps.Name(b"JobName"), ps.String(ctxt.id, 0, 0, is_global=False))
    userparams.put(ps.Name(b"ExecutionHistory"), ps.Bool(False))  # Disabled by default for performance
    userparams.put(ps.Name(b"ExecutionHistorySize"), ps.Int(20))  # Default history size
    ctxt.lvm.put(ps.Name(b"UserParams"), userparams)
    # set the User Params for easy access
    ctxt.MaxDictStack = 0
    ctxt.MaxExecStack = 0
    ctxt.MaxOpStack = 0
    ctxt.ExecutionHistory = False  # Initialize context attribute
    ctxt.ExecutionHistorySize = 20  # Default history size

    # Initialize PostScript random number generator with a random seed
    ctxt.random_seed = random.randrange(ps.MAX_POSTSCRIPT_INTEGER)
    random.seed(ctxt.random_seed)

    # ctxt.page_device = ps.Dict(-1, None, name="page_device", is_global=False)

    # create the inital graphics state
    ctxt.gstate = ps.GraphicsState(ctxt.id)

    # Initialize Current Transformation Matrix (CTM) to identity matrix
    # PostScript identity matrix: [1 0 0 1 0 0] (scale_x, skew_x, skew_y, scale_y, translate_x, translate_y)
    ctxt.gstate.CTM = ps.Array(ctxt.id)
    identity_matrix = [ps.Int(1), ps.Int(0), ps.Int(0), ps.Int(1), ps.Int(0), ps.Int(0)]
    ctxt.gstate.CTM.setval(identity_matrix)

    # Calculate inverse Current Transformation Matrix (iCTM) for coordinate transformations
    inverse_transform = _matrix_inverse(
        [
            [identity_matrix[0].val, identity_matrix[1].val, 0],
            [identity_matrix[2].val, identity_matrix[3].val, 0],
            [identity_matrix[4].val, identity_matrix[5].val, 1],
        ]
    )

    ctxt.gstate.iCTM = ps.Array(ctxt.id)
    ctxt.gstate.iCTM.setval(
        [
            ps.Real(inverse_transform[0][0]),
            ps.Real(inverse_transform[0][1]),
            ps.Real(inverse_transform[1][0]),
            ps.Real(inverse_transform[1][1]),
            ps.Real(inverse_transform[2][0]),
            ps.Real(inverse_transform[2][1]),
        ]
    )

    # create the display list
    ctxt.display_list = ps.DisplayList()

    # Initialize standard file objects
    # stdin and stdout are context-specific, stderr is shared
    # Create StandardFile objects and register them with the manager
    # Then use proxy objects to avoid direct references during serialization
    stdin_std_file = ps.StandardFile(
        ctxt_id=ctxt.id,
        name="%stdin",
        stream=sys.stdin,
        mode="r",
        is_global=True  # Standard files are global objects
    )
    stdout_std_file = ps.StandardFile(
        ctxt_id=ctxt.id,
        name="%stdout",
        stream=sys.stdout,
        mode="w",
        is_global=True  # Standard files are global objects
    )

    # Register with manager and create proxy objects
    file_manager = ps.StandardFileManager.get_instance()
    stdin_id = file_manager.register(stdin_std_file)
    stdout_id = file_manager.register(stdout_std_file)

    # Use proxy objects in context to avoid direct serialization
    ctxt.stdin_file = ps.StandardFileProxy(stdin_id, "%stdin", is_global=True)
    ctxt.stdout_file = ps.StandardFileProxy(stdout_id, "%stdout", is_global=True)
    # stderr is managed by GlobalResources and shared across contexts

    # record the start time
    ctxt.start_time = time.perf_counter_ns()

    # run the init script
    #
    # add the filename to ctxt.strings
    # Use absolute path so PostForge works from any working directory
    file_name = os.path.join(system_params["ProjectDir"], "resources", "Init", "sysdict.ps")
    # Use forward slashes — backslashes are PS escape characters
    # (\r in \resources becomes carriage return, corrupting the path)
    file_name = file_name.replace("\\", "/")
    s_t = bytes("{(" + file_name + ") run} stopped", "ascii")
    offset = len(ps.global_resources.global_strings)
    ps.global_resources.global_strings += s_t
    ctxt.vm_alloc_mode = True
    ctxt.e_stack.append(
        ps.String(
            ctxt.id,
            offset=offset,
            length=len(s_t),
            attrib=ps.ATTRIB_EXEC,
            is_global=True,
        )
    )
    ctxt.vm_alloc_mode = False

    ctxt.initializing = True
    ps_dict.init_dictionaries(ctxt, "systemdict")
    ps_control.exec_exec_with_keyboard_interrupt(ctxt, ctxt.o_stack, ctxt.e_stack)
    ctxt.initializing = False

    error_dict = ps_dict.lookup(ctxt, ps.Name(b"$error", is_global=ctxt.vm_alloc_mode))
    error_dict.val[b"initializing"] = ps.Bool(False)

    # exit if there was an error in the init script
    failed = ctxt.o_stack.pop()
    if failed.val:
        ps.contexts[i] = None
        return None, "ps init failed"

    # Note: Initial save moved to job-level encapsulation in execjob()
    # Context initialization now complete without save

    return ctxt, None
