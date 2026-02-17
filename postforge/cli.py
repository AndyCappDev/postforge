#!/usr/bin/env python3
# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge - PostScript Interpreter

This is the main entry point for PostForge, a PostScript interpreter implemented in Python.
PostForge provides both interactive and batch modes for executing PostScript files.

Architecture Overview:
    PostForge follows the PostScript language specification with a stack-based execution model:
    - Operand Stack: holds operands for operators
    - Execution Stack: holds objects to be executed
    - Dictionary Stack: provides variable scoping
    - Graphics State Stack: maintains graphics state for rendering

Key Components:
    - Context: Encapsulates the entire PostScript execution environment
    - System Parameters: Configuration paths and settings
    - Device Support: Pluggable output devices (PNG, PDF, SVG, etc.)
    - Error Handling: User-friendly error messages and recovery

Usage:
    Interactive Mode:
        postforge

    Batch Mode:
        postforge input.ps
        postforge -d png -o output.png input.ps

Author: Scott Bowman
License: AGPL-3.0-or-later
"""


import argparse
import os
import random
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, Optional, Tuple, Union

from .operators import dict as ps_dict
from .utils import memory as ps_memory
from .utils import profiler as ps_profiler
from .core import icc_default
from .core import types as ps
from .operators import vm as ps_vm
from .operators import control as ps_control
from .operators.control import exec_exec, execjob, start
from .operators.matrix import _matrix_inverse
from .operators.graphics_state import initgraphics


def _parse_page_ranges(spec: str) -> set:
    """Parse a page range specification into a set of page numbers.

    Supports single pages (``3``), ranges (``1-5``), and comma-separated
    combinations (``1-3,7,10-12``).  Page numbers are 1-based.

    Args:
        spec: Page range string, e.g. ``"1-5,8,10-12"``

    Returns:
        Set of integer page numbers.

    Raises:
        ValueError: If the specification is malformed.
    """
    pages: set = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            if len(bounds) != 2 or not bounds[0].strip() or not bounds[1].strip():
                raise ValueError(f"Invalid page range: '{part}'")
            try:
                start = int(bounds[0])
                end = int(bounds[1])
            except ValueError:
                raise ValueError(f"Invalid page range: '{part}'")
            if start < 1 or end < 1:
                raise ValueError(f"Page numbers must be positive: '{part}'")
            if start > end:
                raise ValueError(f"Invalid page range (start > end): '{part}'")
            pages.update(range(start, end + 1))
        else:
            try:
                num = int(part)
            except ValueError:
                raise ValueError(f"Invalid page number: '{part}'")
            if num < 1:
                raise ValueError(f"Page numbers must be positive: '{part}'")
            pages.add(num)
    if not pages:
        raise ValueError("Empty page range specification")
    return pages


def get_output_base_name(outputfile: str, inputfiles: list) -> str:
    """
    Derive output base name from command-line arguments.

    Args:
        outputfile: The -o argument value (or None)
        inputfiles: List of input files (or empty list)

    Returns:
        Base name for output files (without extension)
    """
    if outputfile:
        # Extract base name from -o argument (remove path and extension)
        base = os.path.basename(outputfile)
        return os.path.splitext(base)[0]
    elif inputfiles:
        # Derive from first input file
        first = inputfiles[0]
        if first == "-":
            return "stdin"
        base = os.path.basename(first)
        return os.path.splitext(base)[0]
    else:
        # Interactive mode - use default
        return "page"


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

    # Get the directory containing this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
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

    The context creation process follows PostScript specification requirements
    and includes proper error handling for initialization failures.

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


def _auto_set_qt_resolution(ctxt):
    """Auto-calculate HWResolution for the Qt device so the rendered image
    fits in approximately 85% of the screen without needing to be scaled up.

    Uses the PySide6 screen geometry to determine the target pixel dimensions,
    then derives the DPI from the current PageSize.
    """
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        screen = app.primaryScreen()
        available = screen.availableGeometry()
        screen_w = available.width()
        screen_h = available.height()
    except Exception:
        return  # Can't determine screen size, keep default

    max_w = int(screen_w * 0.60)
    max_h = int(screen_h * 0.85)

    pd = ctxt.gstate.page_device
    page_w = pd[b"PageSize"].get(ps.Int(0))[1].val  # points
    page_h = pd[b"PageSize"].get(ps.Int(1))[1].val

    if page_w <= 0 or page_h <= 0:
        return

    # DPI that would make the page fit exactly in the max window
    dpi_for_width = max_w * 72.0 / page_w
    dpi_for_height = max_h * 72.0 / page_h
    dpi = int(min(dpi_for_width, dpi_for_height))

    # Clamp to reasonable range
    dpi = max(36, min(dpi, 9600))

    hw_res = ps.Array(ctxt.id)
    hw_res.setval([ps.Int(dpi), ps.Int(dpi)])
    ctxt.gstate.page_device[b"HWResolution"] = hw_res
    initgraphics(ctxt, ctxt.o_stack)


def main() -> int:
    """
    Main entry point for PostForge PostScript interpreter.

    Returns:
        Exit code: 0 for success, 1 for error
    """

    # Remember user's working directory — file paths on the command line
    # and in interactive mode should resolve relative to where pf was invoked
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    user_cwd = os.getcwd()

    # Get available devices from OutputDevice directory
    device_dir = os.path.join(project_dir, "resources", "OutputDevice")

    available_devices = []
    if os.path.exists(device_dir):
        for f in os.listdir(device_dir):
            if f.endswith(".ps"):
                available_devices.append(f[:-3])  # Remove .ps extension

    # Create argument parser
    parser = argparse.ArgumentParser(
        prog="postforge",
        description="PostForge - PostScript Interpreter",
        epilog="If no input file is provided, PostForge will run in interactive mode.",
    )

    parser.add_argument("inputfiles", nargs="*", help="PostScript input files to process (each as separate job)")
    parser.add_argument(
        "-o", "--output", dest="outputfile", help="Specify output filename"
    )
    parser.add_argument(
        "-d",
        "--device",
        choices=available_devices,
        help=f'Specify output device ({", ".join(available_devices)})',
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "--memory-profile", action="store_true", 
        help="Enable memory profiling and generate detailed memory usage report"
    )
    parser.add_argument(
        "--gc-analysis", action="store_true",
        help="Enable garbage collection analysis (implies --memory-profile)"
    )

    parser.add_argument(
        "--leak-analysis", action="store_true",
        help="Enable detailed memory leak analysis (implies --memory-profile)"
    )
    
    # Performance profiling options
    parser.add_argument(
        "--profile", action="store_true",
        help="Enable performance profiling (default: cprofile)"
    )
    parser.add_argument(
        "--profile-type", 
        choices=['cprofile', 'none'],  # Future: 'line', 'memory', 'py-spy'
        default='cprofile',
        help="Specify profiling backend type (default: cprofile)"
    )
    parser.add_argument(
        "--profile-output",
        help="Specify output file for profiling results (default: auto-generated)"
    )
    parser.add_argument(
        "--no-glyph-cache", action="store_true",
        help="Disable glyph caching (useful for debugging font rendering)"
    )
    parser.add_argument(
        "--cache-stats", action="store_true",
        help="Print glyph cache statistics after job completion"
    )
    parser.add_argument(
        "--output-dir", dest="output_dir", default="pf_output",
        help="Specify output directory (default: pf_output)"
    )
    parser.add_argument(
        "-r", "--resolution", type=int,
        help="Set device resolution in DPI (overrides device default, e.g., 150, 300, 600)"
    )
    parser.add_argument(
        "--pages",
        help="Page range to output (e.g., 1-5, 3, 1-3,7,10-12)"
    )
    parser.add_argument(
        "--antialias",
        choices=["none", "fast", "good", "best", "gray", "subpixel"],
        help="Set anti-aliasing mode for Cairo rendering (default: gray)"
    )
    parser.add_argument(
        "--no-icc", action="store_true",
        help="Disable ICC color management (use PLRM formulas)"
    )
    parser.add_argument(
        "--cmyk-profile",
        help="Path to CMYK ICC profile for color management"
    )
    parser.add_argument(
        "--rebuild-font-cache", action="store_true",
        help="Force rebuild of system font cache and exit"
    )

    args = parser.parse_intermixed_args()

    # Validate resolution range
    if args.resolution is not None:
        if args.resolution < 36 or args.resolution > 9600:
            print("PostForge Error: Resolution must be between 36 and 9600 DPI.")
            return 1

    # Validate --pages format early
    page_filter = None
    if args.pages:
        try:
            page_filter = _parse_page_ranges(args.pages)
        except ValueError as e:
            print(f"PostForge Error: {e}")
            print("Expected format: 1-5, 3, 1-3,7,10-12")
            return 1

    # Handle --rebuild-font-cache before context creation
    if args.rebuild_font_cache:
        from .core.system_font_cache import SystemFontCache
        cache = SystemFontCache.get_instance()
        cache.rebuild()
        print(f"System font cache rebuilt: {cache.font_count()} fonts found")
        return 0

    # Resolve input file paths to absolute (relative to user's CWD) before
    # we chdir to the project root for PS resource file resolution.
    inputfiles = [f if f == "-" else (os.path.join(user_cwd, f) if not os.path.isabs(f) else f)
                  for f in args.inputfiles]
    device = args.device
    memory_profile = args.memory_profile or args.gc_analysis or args.leak_analysis
    gc_analysis = args.gc_analysis
    leak_analysis = args.leak_analysis
    
    # Performance profiling setup
    performance_profile = args.profile
    profile_type = args.profile_type if performance_profile else 'none'
    profile_output = args.profile_output

    # Generate default output path if profiling enabled but no output specified
    if performance_profile and not profile_output:
        profile_output = ps_profiler.generate_default_output_path(profile_type)

    # Glyph cache control (enabled by default, disable with --no-glyph-cache)
    if args.no_glyph_cache:
        ps.global_resources.glyph_cache_disabled = True

    # ICC color management control
    if args.no_icc:
        icc_default.disable()
    if args.cmyk_profile:
        icc_default.set_custom_profile(args.cmyk_profile)
    # Initialize ICC eagerly so the profile message prints at startup
    icc_default.initialize()

    # Handle stdin input ("-" as filename)
    stdin_temp = None
    if "-" in inputfiles:
        if sys.stdin.isatty():
            print("PostForge Error: '-' specified but no data piped to stdin.")
            return 1
        stdin_temp = tempfile.NamedTemporaryFile(
            suffix=".ps", prefix="postforge_stdin_", delete=False
        )
        stdin_temp.write(sys.stdin.buffer.read())
        stdin_temp.close()
        inputfiles = [stdin_temp.name if f == "-" else f for f in inputfiles]

    try:
        return _run_postforge(args, inputfiles, stdin_temp, user_cwd, project_dir,
                              available_devices, device, memory_profile,
                              gc_analysis, leak_analysis, performance_profile,
                              profile_type, profile_output, page_filter)
    finally:
        _cleanup_stdin_temp(stdin_temp)


def _run_postforge(args, inputfiles, stdin_temp, user_cwd, project_dir,
                   available_devices, device, memory_profile, gc_analysis,
                   leak_analysis, performance_profile, profile_type,
                   profile_output, page_filter):
    """Core PostForge execution logic, called from main() with cleanup wrapper."""
    # Enable memory profiling if requested
    memory_profiler = None
    if memory_profile:
        memory_profiler = ps_memory.enable_memory_profiling(enable_tracemalloc=True)
        print("Memory profiling enabled")
        if gc_analysis:
            print("Garbage collection analysis enabled")
    
    # Initialize performance profiler
    perf_profiler = ps_profiler.initialize_profiler(
        backend_type=profile_type,
        output_path=profile_output,
        enabled=performance_profile
    )
    
    if performance_profile:
        print(f"Performance profiling enabled (backend: {profile_type})")
        if profile_output:
            print(f"Results will be saved to: {profile_output}")

    # initialize the global system params
    system_params = init_system_params()

    # Set CWD to project root for the entire session. PS resource files
    # (fonts, devices, encodings) use relative paths like resources/Font/...
    # that must resolve from the project root. Input file paths and output
    # directories are made absolute (relative to user_cwd) before use.
    os.chdir(project_dir)
    ctxt, err_string = create_context(system_params)

    if err_string:
        print(err_string)
        quit()
    
    # Take memory snapshot after context initialization
    if memory_profile:
        ps_memory.take_memory_snapshot("context_initialized", ctxt)

    # Auto-select device based on output file and PySide6 availability
    if not args.outputfile and not device:
        # No output file specified, no explicit device - try Qt interactive
        if "qt" in available_devices:
            try:
                from PySide6.QtWidgets import QApplication
                device = "qt"
            except ImportError:
                pass
        if not device:
            # Fall back to first available file-output device
            for fallback in ("png", "pdf", "svg"):
                if fallback in available_devices:
                    device = fallback
                    break
            if not device and available_devices:
                device = available_devices[0]
            if device:
                print(f"Note: PySide6 not available, using {device.upper()} output to ./pf_output/")
                print("      Install PySide6 for interactive display: pip install PySide6")
    elif args.outputfile and not device:
        # Output file specified - infer device from extension
        ext = os.path.splitext(args.outputfile)[1].lower().lstrip(".")
        if ext in available_devices:
            device = ext
        else:
            # Fall back to first available file-output device
            for fallback in ("png", "pdf", "svg"):
                if fallback in available_devices:
                    device = fallback
                    break
            if not device and available_devices:
                device = available_devices[0]

    # validate and set output device if specified
    if device:
        device_file = os.path.join(
            system_params["OutputDeviceResourceDir"], f"{device}.ps"
        )

        # Check if device file exists
        if not os.path.exists(device_file):
            print(f"PostForge Error: Output device '{device}' not found.")
            print(f"Expected file: {device_file}")
            device_dir = system_params["OutputDeviceResourceDir"]
            if os.path.exists(device_dir):
                available = [
                    f[:-3] for f in os.listdir(device_dir) if f.endswith(".ps")
                ]
                if available:
                    print("Available output devices:")
                    for dev in sorted(available):
                        print(f"  {dev}")
                else:
                    print("No output devices found in OutputDevice directory.")
            else:
                print(f"OutputDevice directory not found: {device_dir}")
            return 1

        # Validate device file is readable
        try:
            with open(device_file, "r") as f:
                content = f.read()
                if not content.strip():
                    print(f"PostForge Error: Device file '{device}.ps' is empty.")
                    return 1
        except PermissionError:
            print(
                f"PostForge Error: Permission denied reading device file '{device}.ps'."
            )
            return 1
        except UnicodeDecodeError:
            print(
                f"PostForge Error: Device file '{device}.ps' contains invalid characters."
            )
            return 1
        except OSError as e:
            print(f"PostForge Error: Cannot read device file '{device}.ps': {e}")
            return 1

        # Set the device in PostScript by executing setpagedevice
        try:
            device_cmd = f"/{device} /OutputDevice findresource setpagedevice"
            s_t = bytes(device_cmd, "ascii")
            offset = len(ps.global_resources.global_strings)
            ps.global_resources.global_strings += s_t
            ctxt.e_stack.append(
                ps.String(
                    ctxt.id,
                    offset=offset,
                    length=len(s_t),
                    attrib=ps.ATTRIB_EXEC,
                    is_global=True,
                )
            )
            ps_control.exec_exec_with_keyboard_interrupt(ctxt, ctxt.o_stack, ctxt.e_stack)
        except Exception as e:
            print(
                f"PostForge Error: Failed to initialize output device '{device}': {e}"
            )
            return 1

    # Register live rendering callback for Qt device (only in interactive mode)
    if device == "qt" and not inputfiles:
        try:
            from .devices.qt import refresh_display, _process_qt_events, qt_module
            ctxt.on_paint_callback = lambda ctxt, elem: refresh_display(ctxt)
            # Set up context reference and event loop callback for quit signaling
            qt_module._ctxt = ctxt
            ctxt.event_loop_callback = _process_qt_events
        except ImportError:
            pass

    # Set output naming parameters in page device
    if ctxt.gstate.page_device:
        base_name = get_output_base_name(args.outputfile, inputfiles)
        # Make output dir absolute relative to user's original CWD,
        # since PS execution runs with CWD set to the project root.
        output_dir = args.output_dir
        if not os.path.isabs(output_dir):
            output_dir = os.path.join(user_cwd, output_dir)

        # Store base name in global strings and create String object
        base_name_bytes = bytes(base_name, "ascii")
        base_name_offset = len(ps.global_resources.global_strings)
        ps.global_resources.global_strings += base_name_bytes
        ctxt.gstate.page_device[b"OutputBaseName"] = ps.String(
            ctxt.id,
            offset=base_name_offset,
            length=len(base_name_bytes),
            is_global=True,
        )

        # Store output directory in global strings and create String object
        output_dir_bytes = bytes(output_dir, "ascii")
        output_dir_offset = len(ps.global_resources.global_strings)
        ps.global_resources.global_strings += output_dir_bytes
        ctxt.gstate.page_device[b"OutputDirectory"] = ps.String(
            ctxt.id,
            offset=output_dir_offset,
            length=len(output_dir_bytes),
            is_global=True,
        )

        # Override HWResolution if --resolution flag was provided
        if args.resolution:
            hw_res = ps.Array(ctxt.id)
            hw_res.setval([ps.Int(args.resolution), ps.Int(args.resolution)])
            ctxt.gstate.page_device[b"HWResolution"] = hw_res
            initgraphics(ctxt, ctxt.o_stack)
        elif device == "qt":
            # Auto-calculate DPI so the rendered image fits in ~85% of screen
            _auto_set_qt_resolution(ctxt)

        # Store anti-aliasing mode if --antialias flag was provided
        if args.antialias:
            aa_bytes = bytes(args.antialias, "ascii")
            aa_offset = len(ps.global_resources.global_strings)
            ps.global_resources.global_strings += aa_bytes
            ctxt.gstate.page_device[b"AntiAliasMode"] = ps.String(
                ctxt.id, offset=aa_offset, length=len(aa_bytes), is_global=True
            )

    # Set page filter if --pages was provided
    if page_filter is not None:
        ctxt.page_filter = page_filter

    # execute start for the current context
    start(ctxt)

    if inputfiles:
        # Process multiple files sequentially, each as a separate job
        for i, inputfile in enumerate(inputfiles):
            is_stdin = stdin_temp is not None and inputfile == stdin_temp.name
            display_name = "<stdin>" if is_stdin else inputfile
            print(f"\n{'='*60}")
            print(f"Processing Job {i+1}/{len(inputfiles)}: {display_name}")
            print(f"{'='*60}")
            
            # Convert Windows path separators to PostScript format
            inputfile = inputfile.replace("\\", "/")
            # Remove leading ./ if present
            if inputfile.startswith("./"):
                inputfile = inputfile[2:]

            # Validate input file exists and is readable
            if not os.path.exists(inputfile):
                print(f"PostForge Error: Input file '{inputfile}' not found.")
                continue  # Continue with next file instead of exiting

            if not os.path.isfile(inputfile):
                print(f"PostForge Error: '{inputfile}' is not a file.")
                continue

            try:
                with open(inputfile, "r") as f:
                    # Just test if we can read the file
                    pass
            except PermissionError:
                print(f"PostForge Error: Permission denied reading '{inputfile}'.")
                continue
            except UnicodeDecodeError:
                print(
                    f"PostForge Error: '{inputfile}' contains invalid characters or is not a text file."
                )
                continue
            except OSError as e:
                print(f"PostForge Error: Cannot read '{inputfile}': {e}")
                continue

            # Update OutputBaseName per job (unless user specified -o)
            if not args.outputfile and ctxt.gstate.page_device:
                job_base_name = "stdin" if is_stdin else os.path.splitext(os.path.basename(inputfile))[0]
                job_base_bytes = bytes(job_base_name, "ascii")
                job_base_offset = len(ps.global_resources.global_strings)
                ps.global_resources.global_strings += job_base_bytes
                ctxt.gstate.page_device[b"OutputBaseName"] = ps.String(
                    ctxt.id,
                    offset=job_base_offset,
                    length=len(job_base_bytes),
                    is_global=True,
                )

            # Reset PageCount — redundant safety net since restore in _cleanup_job
            # reverts page_device to pre-save state (which has PageCount=0),
            # but cheap to keep as defense-in-depth.
            if ctxt.gstate.page_device and b"PageCount" in ctxt.gstate.page_device:
                ctxt.gstate.page_device[b"PageCount"].val = 0

            # Execute this file as a separate job
            try:
                # Take memory snapshot before job execution
                if memory_profile:
                    ps_memory.take_memory_snapshot(f"before_job_{i+1}", ctxt)
                    if gc_analysis and i > 0:  # Force GC before jobs after the first
                        ps_memory.force_gc_and_measure(f"pre_job_{i+1}_gc", ctxt)
                
                # Profile the PostScript execution
                if performance_profile:
                    with perf_profiler.profile_context():
                        execjob(ctxt, inputfile)
                else:
                    execjob(ctxt, inputfile)
                
                # Take memory snapshot after job execution
                if memory_profile:
                    ps_memory.take_memory_snapshot(f"after_job_{i+1}", ctxt)
                
                print(f"Job {i+1} completed successfully: {display_name}")

                # Print glyph cache statistics if requested
                if args.cache_stats:
                    path_cache = ps.global_resources.get_glyph_cache()
                    bitmap_cache = ps.global_resources.get_glyph_bitmap_cache()
                    if path_cache:
                        stats = path_cache.stats()
                        print(f"   Glyph path cache: {stats['hits']} hits, {stats['misses']} misses, "
                              f"{stats['hit_rate']:.1%} hit rate, {stats['entries']} entries")
                    if bitmap_cache:
                        stats = bitmap_cache.stats()
                        print(f"   Glyph bitmap cache: {stats['hits']} hits, {stats['misses']} misses, "
                              f"{stats['hit_rate']:.1%} hit rate, {stats['entries']} entries, "
                              f"{stats['memory_bytes']/1024/1024:.1f}MB used")
            except ModuleNotFoundError as e:
                print(f"PostForge Error: Missing required Python module: {e}")
                print(
                    "Please install required dependencies with: pip install -r requirements.txt"
                )
                continue
            except ImportError as e:
                print(f"PostForge Error: Module import failed: {e}")
                continue
            except KeyError as e:
                print(f"Job {i+1} FAILED with KeyError: {display_name}: {e}")
                import traceback
                print("Full traceback:")
                traceback.print_exc()
                continue
            except Exception as e:
                print(f"Job {i+1} FAILED: {display_name}: {e}")
                import traceback
                print("Full traceback:")
                traceback.print_exc()
                continue

        # Final memory analysis after all jobs
        if memory_profile:
            if gc_analysis:
                print("\nPerforming final garbage collection analysis...")
                ps_memory.force_gc_and_measure("final_cleanup", ctxt)
            
            ps_memory.take_memory_snapshot("all_jobs_complete", ctxt)
            
            print("\n" + "=" * 60)
            print("MEMORY ANALYSIS REPORT")
            print("=" * 60)
            print(ps_memory.generate_memory_report())
            
            if leak_analysis:
                print("\n" + ps_memory.analyze_memory_leaks())
        
        print(f"\n{'='*60}")
        print(f"Processed {len(inputfiles)} jobs")
        print(f"{'='*60}")
        print("\nFinal operand stack:")
        print(ctxt.o_stack)

        print("\nexecution stack")
        print(ctxt.e_stack)
    else:
        # interactive mode
        s_t = bytes("{executive} stopped", "ascii")
        offset = len(ps.global_resources.global_strings)
        ps.global_resources.global_strings += s_t
        ctxt.e_stack.append(
            ps.String(
                ctxt.id,
                offset=offset,
                length=len(s_t),
                attrib=ps.ATTRIB_EXEC,
                is_global=True,
            )
        )
        if memory_profile:
            ps_memory.take_memory_snapshot("interactive_mode_start", ctxt)
        
        # Profile interactive mode execution
        if performance_profile:
            with perf_profiler.profile_context():
                ps_control.exec_exec_with_keyboard_interrupt(ctxt, ctxt.o_stack, ctxt.e_stack)
        else:
            ps_control.exec_exec_with_keyboard_interrupt(ctxt, ctxt.o_stack, ctxt.e_stack)
        
        if memory_profile:
            ps_memory.take_memory_snapshot("interactive_mode_end", ctxt)
            print("\n" + ps_memory.generate_memory_report())

    # Enter Qt event loop if using Qt device (keeps window open after file execution)
    # Skip for interactive mode - user explicitly quit, so exit completely
    if device == "qt" and inputfiles:
        try:
            from .devices.qt import enter_event_loop
            enter_event_loop()  # Blocks until window closed
        except ImportError:
            pass

    # Generate profiling report
    if performance_profile:
        print("\n" + "="*50)
        print("PERFORMANCE PROFILING REPORT")
        print("="*50)
        perf_profiler.save_results()
        perf_profiler.print_summary()
        print("\nTo analyze detailed results:")
        if profile_type == 'cprofile':
            print(f"   python -m pstats {profile_output}")
            print(f"   # Or use snakeviz: pip install snakeviz && snakeviz {profile_output}")

    # delete any leftover saved vm
    shutil.rmtree(ctxt.system_params["VMDir"], ignore_errors=True)

    return ctxt.exit_code


def _cleanup_stdin_temp(stdin_temp):
    """Remove the temporary file created for stdin input."""
    if stdin_temp is not None:
        try:
            os.unlink(stdin_temp.name)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
