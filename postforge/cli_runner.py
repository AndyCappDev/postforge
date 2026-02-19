# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge execution logic.

Handles device setup, job execution (batch and interactive modes), and the
main run loop that ties context initialization to PostScript execution.
"""

import os
import shutil
import traceback

from .cli_args import get_output_base_name
from .core import types as ps
from .core.context_init import create_context, init_system_params
from .operators import control as ps_control
from .operators.control import execjob, start
from .operators.graphics_state import initgraphics
from .utils import memory as ps_memory
from .utils import profiler as ps_profiler


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


def _setup_device(ctxt, args, system_params, available_devices):
    """Select, validate and initialize the output device.

    Auto-selects a device if none was specified on the command line, validates
    the device file, and executes the PostScript ``setpagedevice`` command.

    Args:
        ctxt: PostScript execution context.
        args: Parsed CLI arguments.
        system_params: System parameters dictionary.
        available_devices: List of available device names.

    Returns:
        Tuple of (resolved_device_name, error_code_or_None).
        error_code is None on success, or an int exit code on failure.
    """
    device = args.device

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

    # Validate and set output device if specified
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
                avail = [
                    f[:-3] for f in os.listdir(device_dir) if f.endswith(".ps")
                ]
                if avail:
                    print("Available output devices:")
                    for dev in sorted(avail):
                        print(f"  {dev}")
                else:
                    print("No output devices found in OutputDevice directory.")
            else:
                print(f"OutputDevice directory not found: {device_dir}")
            return device, 1

        # Validate device file is readable
        try:
            with open(device_file, "r") as f:
                content = f.read()
                if not content.strip():
                    print(f"PostForge Error: Device file '{device}.ps' is empty.")
                    return device, 1
        except PermissionError:
            print(
                f"PostForge Error: Permission denied reading device file '{device}.ps'."
            )
            return device, 1
        except UnicodeDecodeError:
            print(
                f"PostForge Error: Device file '{device}.ps' contains invalid characters."
            )
            return device, 1
        except OSError as e:
            print(f"PostForge Error: Cannot read device file '{device}.ps': {e}")
            return device, 1

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
            return device, 1

    return device, None


def _configure_page_device(ctxt, args, inputfiles, user_cwd, device):
    """Configure page device parameters (output naming, resolution, antialias).

    Args:
        ctxt: PostScript execution context.
        args: Parsed CLI arguments.
        inputfiles: List of resolved input file paths.
        user_cwd: User's original working directory.
        device: Resolved device name (may be None).
    """
    # Register Qt callbacks for live rendering and event processing
    if device == "qt":
        try:
            from .devices.qt import refresh_display, _process_qt_events, qt_module
            qt_module._ctxt = ctxt
            ctxt.event_loop_callback = _process_qt_events
            if inputfiles:
                # Batch mode: only refresh on paint operations when executive
                # is active (the _interactive_painting flag is toggled by
                # the _setinteractivepaint operator called from executive)
                def _conditional_paint(ctxt, elem):
                    if getattr(ctxt, '_interactive_painting', False):
                        refresh_display(ctxt)
                ctxt.on_paint_callback = _conditional_paint
            else:
                # Interactive mode: always refresh on paint operations
                ctxt.on_paint_callback = lambda ctxt, elem: refresh_display(ctxt)
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

        # For EPS files, pre-set PageSize to BoundingBox dimensions so that
        # DPI auto-calculation (below) uses the actual content size, not the
        # default letter page.  execjob will also call setpagedevice with
        # the same dimensions, but the DPI must be right before that.
        if inputfiles and inputfiles[0].lower().endswith('.eps'):
            bbox = ps_control._read_eps_bounding_box(inputfiles[0])
            if bbox:
                llx, lly, urx, ury = bbox
                eps_w = urx - llx
                eps_h = ury - lly
                if eps_w > 0 and eps_h > 0:
                    page_size = ps.Array(ctxt.id)
                    page_size.setval([ps.Real(eps_w), ps.Real(eps_h)])
                    ctxt.gstate.page_device[b"PageSize"] = page_size

        # Override HWResolution if --resolution flag was provided
        if args.resolution:
            hw_res = ps.Array(ctxt.id)
            hw_res.setval([ps.Int(args.resolution), ps.Int(args.resolution)])
            ctxt.gstate.page_device[b"HWResolution"] = hw_res
            initgraphics(ctxt, ctxt.o_stack)
        elif device == "qt":
            # Auto-calculate DPI so the rendered image fits in ~85% of screen
            _auto_set_qt_resolution(ctxt)

        # Override TextRenderingMode if --text-as-paths flag was provided
        if args.text_as_paths:
            ctxt.gstate.page_device[b"TextRenderingMode"] = ps.Name(b"GlyphPaths")

        # Store anti-aliasing mode if --antialias flag was provided
        if args.antialias:
            aa_bytes = bytes(args.antialias, "ascii")
            aa_offset = len(ps.global_resources.global_strings)
            ps.global_resources.global_strings += aa_bytes
            ctxt.gstate.page_device[b"AntiAliasMode"] = ps.String(
                ctxt.id, offset=aa_offset, length=len(aa_bytes), is_global=True
            )


def _run_batch_jobs(ctxt, args, inputfiles, stdin_temp,
                    memory_profile, gc_analysis, performance_profile,
                    perf_profiler, leak_analysis):
    """Execute input files as batch jobs.

    Args:
        ctxt: PostScript execution context.
        args: Parsed CLI arguments.
        inputfiles: List of resolved input file paths.
        stdin_temp: Temporary file for stdin input (or None).
        memory_profile: Whether memory profiling is enabled.
        gc_analysis: Whether GC analysis is enabled.
        performance_profile: Whether performance profiling is enabled.
        perf_profiler: Performance profiler instance.
        leak_analysis: Whether leak analysis is enabled.
    """
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
            print("Full traceback:")
            traceback.print_exc()
            continue
        except Exception as e:
            print(f"Job {i+1} FAILED: {display_name}: {e}")
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


def _run_interactive(ctxt, memory_profile, performance_profile, perf_profiler):
    """Run the PostScript interactive interpreter (executive).

    Args:
        ctxt: PostScript execution context.
        memory_profile: Whether memory profiling is enabled.
        performance_profile: Whether performance profiling is enabled.
        perf_profiler: Performance profiler instance.
    """
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


def run(args, inputfiles, stdin_temp, user_cwd, project_dir,
        available_devices, device, memory_profile, gc_analysis,
        leak_analysis, performance_profile, profile_type,
        profile_output, page_filter):
    """Core PostForge execution logic.

    Initializes the PostScript context, sets up the output device, and runs
    either batch jobs or the interactive interpreter.

    Args:
        args: Parsed CLI arguments.
        inputfiles: List of resolved input file paths.
        stdin_temp: Temporary file for stdin input (or None).
        user_cwd: User's original working directory.
        project_dir: PostForge project root directory.
        available_devices: List of available device names.
        device: Explicitly requested device name (or None).
        memory_profile: Whether memory profiling is enabled.
        gc_analysis: Whether GC analysis is enabled.
        leak_analysis: Whether leak analysis is enabled.
        performance_profile: Whether performance profiling is enabled.
        profile_type: Profiling backend type string.
        profile_output: Path for profiling output file.
        page_filter: Set of page numbers to render (or None for all).

    Returns:
        Exit code (0 for success, non-zero for error).
    """
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

    # Initialize the global system params
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

    # Store user's original CWD so file operators (run, file, etc.) can
    # resolve relative paths against where the user invoked PostForge,
    # not the project root we chdir'd to above.
    ctxt.user_cwd = user_cwd

    # Take memory snapshot after context initialization
    if memory_profile:
        ps_memory.take_memory_snapshot("context_initialized", ctxt)

    # Setup output device (auto-select, validate, initialize)
    device, error_code = _setup_device(ctxt, args, system_params, available_devices)
    if error_code is not None:
        return error_code

    # Configure page device (output naming, resolution, antialias, Qt callbacks)
    _configure_page_device(ctxt, args, inputfiles, user_cwd, device)

    # Set page filter if --pages was provided
    if page_filter is not None:
        ctxt.page_filter = page_filter

    # Execute start for the current context
    start(ctxt)

    if inputfiles:
        _run_batch_jobs(ctxt, args, inputfiles, stdin_temp,
                        memory_profile, gc_analysis, performance_profile,
                        perf_profiler, leak_analysis)
    else:
        _run_interactive(ctxt, memory_profile, performance_profile, perf_profiler)

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

    # Delete any leftover saved vm
    shutil.rmtree(ctxt.system_params["VMDir"], ignore_errors=True)

    return ctxt.exit_code
