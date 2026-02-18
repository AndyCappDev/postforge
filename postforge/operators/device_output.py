# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript Device Output Management

This module implements PostScript page device operations and output device
management. It handles the interface between PostScript's device-independent
graphics model and actual output devices like PNG, PDF, SVG, etc.

Key Functions:
    - showpage: Render current page to output device
    - currentpagedevice: Get current page device dictionary
    - setpagedevice: Configure page device settings

The module follows PostScript's device architecture where graphics operations
are device-independent and actual rendering is handled by pluggable device
implementations in the devices/ directory.
"""

import copy
import importlib
import os, sys
from typing import Any

from . import control as ps_control
from ..core import error as ps_error
from ..core import types as ps
from .graphics_state import initgraphics
from .matrix import _setCTM
from .painting import erasepage
from .resource import findresource
from ..core.types.constants import (
    ENDPAGE_SHOWPAGE, ENDPAGE_COPYPAGE
)

def currentpagedevice(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    – **currentpagedevice** dict

    Returns a read-only dictionary describing the current page device
    parameters. The returned dictionary is a copy of the internal page
    device dictionary; modifications to it have no effect on the device
    (use **setpagedevice** to change parameters).

    **Errors**: **stackoverflow**
    **See Also**: **setpagedevice**
    """
    src = ctxt.gstate.page_device
    # create a new dictionary
    dst = ps.Dict(ctxt.id, max_length=len(src), is_global=ctxt.vm_alloc_mode)

    # now copy the items
    for src_key, src_val in src.items():
        dst.val[src_key] = copy.copy(src_val)

    # set the access to the new dictionary to READ_ONLY
    dst._access = ps.ACCESS_READ_ONLY
    ostack.append(dst)


def setpagedevice(ctxt, ostack) -> None:
    """
    dict **setpagedevice** -


    modifies the contents of the page device dictionary in the graphics state based on
    the contents of the dictionary operand. The operand is a request dictionary containing
    requested new values for one or more page device parameters. If valid for
    the current page device, these requested values are merged by **setpagedevice** into
    the current page device dictionary. The interpretation of these parameters is described
    in Section 6.2, "Page Device Parameters."

    The results of **setpagedevice** are cumulative. The request dictionary for any given
    invocation is not required to include any particular keys; parameter values established
    in previous invocations will persist unless explicitly overridden. This
    cumulative behavior applies not only to the top-level dictionary, but also recursively
    to the subdictionaries **InputAttributes**, **OutputAttributes**, and **Policies**, as
    well as to some types of details dictionaries.

    The result of executing **setpagedevice** is to instantiate a page device dictionary,
    perform the equivalent of **initgraphics** and **erasepage**, and install the new device
    dictionary as an implicit part of the graphics state. The effects of **setpagedevice**
    are subject to **save** and **restore**, **gsave** and **grestore**, and **setgstate**.

    **setpagedevice** can be used by system administrators to establish a default state for
    a device by invoking it as part of an unencapsulated job (see Section 3.7.7, "Job
    Execution Environment"). This default state persists until the next restart of the
    PostScript interpreter. Some PostScript implementations store some of the device
    parameters in persistent storage when **setpagedevice** is executed as part of an unencapsulated
    job, making those parameters persist through interpreter restart.

    **setpagedevice** reinitializes everything in the graphics state except the font parameter,
    including parameters not affected by **initgraphics**. Device-dependent rendering
    parameters, such as the halftone screen, transfer functions, flatness tolerance,
    and color rendering dictionary, are reset to built-in default values or to ones provided
    in the **Install** procedure of the page device dictionary.

    When the current device in the graphics state is not a page device—for example,
    after **nulldevice** has been invoked or when an interactive display device is active—
    **setpagedevice** creates a new device dictionary from scratch before merging in the
    parameters from dict. The contents of this dictionary are implementationdependent.

    If a device’s **BeginPage** or **EndPage** procedure invokes **setpagedevice**, an
    **undefined** error occurs.

    **Errors**:     **configurationerror**, **invalidaccess**, **limitcheck**, **rangecheck**,
                **stackunderflow**, **typecheck**, **undefined**, **VMerror**
    **See Also**:   **currentpagedevice**, **nulldevice**, **gsave**, **grestore**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setpagedevice.__name__)

    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setpagedevice.__name__)

    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, setpagedevice.__name__)

    # PLRM: when current device is not a page device (e.g. after nulldevice),
    # or when switching to a different output device, create a new device
    # dictionary from scratch before merging request params.
    request_dict = ostack[-1]
    need_full_reload = b".IsPageDevice" not in ctxt.gstate.page_device

    # Also reload when switching to a different output device
    if (not need_full_reload
            and b"OutputDevice" in request_dict.val
            and b"OutputDevice" in ctxt.gstate.page_device):
        new_dev = request_dict.val[b"OutputDevice"].val
        cur_dev = ctxt.gstate.page_device[b"OutputDevice"].val
        if new_dev != cur_dev:
            need_full_reload = True

    if need_full_reload:
        # Determine device: request dict > previous device > fallback
        if b"OutputDevice" in request_dict.val:
            dev = request_dict.val[b"OutputDevice"]
        elif b".PrevOutputDevice" in ctxt.gstate.page_device:
            dev = ctxt.gstate.page_device[b".PrevOutputDevice"]
        else:
            # Fallback: use first available output device
            fallback_name = "png"
            device_dir = ctxt.system_params.get("OutputDeviceResourceDir", "")
            if device_dir and os.path.isdir(device_dir):
                for f in sorted(os.listdir(device_dir)):
                    if f.endswith(".ps"):
                        fallback_name = f[:-3]
                        break
            dev = ps.Name(fallback_name.encode("ascii"))
        # Save previous device name for potential restore
        if b"OutputDevice" in ctxt.gstate.page_device:
            ctxt.gstate.page_device[b".PrevOutputDevice"] = \
                ctxt.gstate.page_device[b"OutputDevice"]
        # Load full device dict via findresource (runs the device .ps resource)
        ostack.append(dev)
        ostack.append(ps.Name(b"OutputDevice"))
        findresource(ctxt, ostack)
        # findresource replaces top 2 with the result dict
        device_dict = ostack.pop()
        # Install as page_device base; merge loop below overlays request on top
        ctxt.gstate.page_device = {}
        for key, val in device_dict.val.items():
            ctxt.gstate.page_device[key] = copy.deepcopy(val)

    # Copy-on-write: create a new dict so that any gsave'd reference to the
    # old page_device stays intact (page_device is shallow-copied by gsave,
    # so in-place modification would corrupt the saved graphics state).
    # The full-reload path above already creates a fresh dict, so this only
    # matters for the incremental-merge path.
    if not need_full_reload:
        ctxt.gstate.page_device = dict(ctxt.gstate.page_device)

    # add/set the pagedevice values
    # Skip HWResolution — the device resolution is controlled by the output
    # device default and the CLI --resolution flag, not by PS programs.
    for key, val in ostack[-1].val.items():
        if key == b"HWResolution":
            continue
        ctxt.gstate.page_device[key] = copy.deepcopy(val)

    is_page_device = ctxt.gstate.page_device[b".IsPageDevice"]
    if is_page_device.val:
        # do this ONLY if .IsPageDevice is true
        initgraphics(ctxt, ostack)
        erasepage(ctxt, ostack)

    # call the Install proc in pagedevice dictionary
    ctxt.e_stack.append(ps.HardReturn())
    install_proc = ctxt.gstate.page_device[b"Install"]
    ctxt.e_stack.append(copy.copy(install_proc))
    ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)

    # set PageCount to 0
    ctxt.gstate.page_device[b"PageCount"].val = 0
    # push PageCount onto the operand stack
    ostack[-1] = ps.Int(0)
    # call BeginPage from the pagedevice dictionary
    ctxt.e_stack.append(ps.HardReturn())
    begin_page_proc = ctxt.gstate.page_device[b"BeginPage"]
    ctxt.e_stack.append(copy.copy(begin_page_proc))
    ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)


def showpage(ctxt: "ps.Context", ostack: "ps.Stack", is_copy: bool=False) -> None:
    """
    – **showpage** –

    PostScript **showpage** operator - render current page to output device.

    Transmits the contents of the current page to the current output device, causing
    any marks painted on the page to be rendered on the output medium. The function
    then erases the current page and reinitializes the graphics state in preparation for
    the next page.

    This is the primary interface between PostScript's device-independent graphics
    model and the actual output devices (PNG, PDF, SVG, etc.). The function:

    1. Calls the EndPage procedure from page device
    2. Renders page contents through device-specific implementation
    3. Creates output directory if specified in page device
    4. Loads and executes device-specific renderer
    5. Erases current page display list (if not called from **copypage**)
    6. Reinitializes graphics state (if not called from **copypage**)
    7. Calls the BeginPage procedure from page device

    Args:
        ctxt: PostScript execution context with current page and graphics state
        ostack: Operand stack (not used by **showpage** directly)

    Device Integration:
        - Reads OutputDevice name from page device dictionary
        - Dynamically loads device module from devices/ directory
        - Calls device.**showpage**() with context and page device parameters
        - Supports configurable output directories via OutputDir parameter

    Error Handling:
        - Graceful degradation if device implementation missing
        - Clear error messages for missing dependencies (cairo, Pillow, etc.)
        - Continues execution even if rendering fails
    composing the next page. (The actions of **showpage** may be modified by the
    **EndPage** procedure, as discussed below.)

    If the current device is a page device that was installed by **setpagedevice**
    (LanguageLevel 2), the precise behavior of **showpage** is determined by the values
    of parameters in the page device dictionary (see Sections 6.1.1, “Page Device Dictionary,”
    and 6.2, “Page Device Parameters”). Parameters affecting the behavior
    of **showpage** include **NumCopies**, **Collate**, **Duplex**, and perhaps others as well.

    Whether or not the current device is a page device, the precise manner in which
    the current page is transmitted is device-dependent. For certain devices (such as
    displays), no action is required, because the current page is visible while it is being
    composed.

    The main actions of **showpage** are as follows:

    1. Executes the **EndPage** procedure in the page device dictionary, passing an integer
       page count on the operand stack along with a reason code indicating that
       the procedure was called from **showpage**; see Section 6.2.6, "Device Initialization
       and Page Setup," for more information.

    2. If the boolean result returned by the **EndPage** procedure is true, transmits the
       page’s contents to the current output device and performs the equivalent of an
       **erasepage** operation, clearing the contents of raster memory in preparation
       for the next page. If the **EndPage** procedure returns false, **showpage** skips this
       step.

    3. Performs the equivalent of an **initgraphics** operation, reinitializing the graphics
       state for the next page.

    4. Executes the **BeginPage** procedure in the page device dictionary, passing an
       integer page count on the operand stack.

    If the **BeginPage** or **EndPage** procedure invokes **showpage**, an undefined error
    occurs.

    For a device that produces output on a physical medium such as paper, **showpage**
    can optionally transmit multiple copies of the page in step 2 above. In Language-
    Level 2 or 3, the page device parameter NumCopies specifies the number of copies
    to be transmitted. In LanguageLevel 1 (or in higher LanguageLevels if **NumCopies**
    is null), the number of copies is given by the value associated with the name
    #**copies** in the naming environment defined by the current dictionary stack. (The
    default value of #**copies** is 1, defined in **userdict**.) For example, the code

        /#copies 5 def
        **showpage**

    prints five copies of the current page, then erases the current page and reinitializes
    the graphics state.

    **Errors**:     **limitcheck**, **undefined**
    **See Also**:   **copypage**, **erasepage**, **setpagedevice**
    """

    pd = ctxt.gstate.page_device

    # Null device: output operators do nothing (PLRM p.459)
    if b".NullDevice" in pd:
        return

    # push PageCount onto the operand stack
    ostack.append(ps.Int(pd[b"PageCount"].val))
    if is_copy:
        # push the reason code (1 - for called from copypage) onto the operand stack
        ostack.append(ps.Int(ENDPAGE_COPYPAGE))
    else:
        # push the reason code (0 - for called from showpage) onto the operand stack
        ostack.append(ps.Int(ENDPAGE_SHOWPAGE))
    # execute the EndPage procedure
    ctxt.e_stack.append(ps.HardReturn())
    end_page_proc = pd[b"EndPage"]
    ctxt.e_stack.append(copy.copy(end_page_proc))
    ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)

    if not ostack:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "EndPage")
    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "EndPage")

    # EndPage returns true or false on the operand stack
    # the bool value designates whether to actually output the page
    output_page = ostack.pop().val

    if output_page:
        # increment the PageCount
        pd[b"PageCount"].val += 1
        page_num = pd[b"PageCount"].val

        # Only render to device if page is in the selected range (or no filter)
        if ctxt.page_filter is None or page_num in ctxt.page_filter:
            if pd[b"OutputDevice"].TYPE == ps.T_STRING:
                device_name = pd[b"OutputDevice"].python_string()
            else:
                device_name = pd[b"OutputDevice"].val.decode("ascii")

            # Create output directory if it doesn't exist (configurable per device)
            if b"OutputDirectory" in pd:
                output_dir = pd[b"OutputDirectory"].python_string()
            else:
                output_dir = ps.OUTPUT_DIRECTORY
            os.makedirs(os.path.join(os.getcwd(), output_dir), exist_ok=True)

            try:
                device = importlib.import_module(f"postforge.devices.{device_name}")
            except ModuleNotFoundError:
                print(
                    f"PostForge Warning: Device implementation '{device_name}.py' not found in devices/ directory."
                )
                print("Device file exists but Python implementation is missing.")
                print("Continuing without device output...")
                return
            except ImportError as e:
                print(f"PostForge Warning: Failed to import device '{device_name}': {e}")
                print("Continuing without device output...")
                return

            try:
                device.showpage(ctxt, pd)
            except AttributeError as e:
                if "showpage" in str(e):
                    print(f"PostForge Error: Device '{device_name}' missing required showpage() function.")
                else:
                    print(f"PostForge Error: AttributeError in device '{device_name}' showpage: {e}")
                    import traceback
                    traceback.print_exc()
            except ModuleNotFoundError as e:
                print(
                    f"PostForge Error: Device '{device_name}' missing required dependency: {e}"
                )
                if "cairo" in str(e):
                    print("Install with: pip install pycairo")
                elif "PIL" in str(e) or "Pillow" in str(e):
                    print("Install with: pip install Pillow")
            except Exception as e:
                print(
                    f"PostForge Error: Device '{device_name}' failed during rendering: {e}"
                )
            finally:
                if "device" in locals():
                    del device

        if not is_copy:
            # erase the current page (clear the display list)
            erasepage(ctxt, ostack)

        # Early termination: if all selected pages have been rendered,
        # reinitialize graphics state (matching what showpage normally
        # does after each page) then stop the job cleanly.  stop()
        # unwinds the e_stack to the enclosing stopped context, closing
        # any open Run/File objects along the way, then execjob's
        # finally block runs _cleanup_job which finalizes output devices.
        if ctxt.page_filter is not None and page_num >= max(ctxt.page_filter):
            if not is_copy:
                initgraphics(ctxt, ostack)
            ps_control.stop(ctxt, ostack)
            return

    if not is_copy:
        initgraphics(ctxt, ostack)

    # push the PageCount onto the operand stack
    ostack.append(ps.Int(pd[b"PageCount"].val))
    # execute the BeginPage proc from the page device dictionary
    ctxt.e_stack.append(ps.HardReturn())
    begin_page_proc = pd[b"BeginPage"]
    ctxt.e_stack.append(copy.copy(begin_page_proc))
    ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)


def copypage(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    – **copypage** –

    Transmits a copy of the current page to the output device without
    erasing the page or resetting the graphics state. Unlike **showpage**,
    **copypage** does not perform an implicit **erasepage** or **initgraphics**,
    so the current page contents and graphics state remain intact for
    further drawing.

    **See Also**: **showpage**, **erasepage**
    """
    showpage(ctxt, ostack, is_copy=True)


def flushpage(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    **flushpage** -

    Forces immediate rendering of the current page contents to the output
    device without erasing the page, advancing the page count, or
    reinitializing the graphics state. Unlike **showpage**, **flushpage**
    does not call EndPage/BeginPage procedures.

    This is commonly used in interactive PostScript programs that redefine
    **showpage** as ``{flushpage}`` to get live display updates without
    page advancement (e.g. PSChess, interactive viewers).

    **Errors**: none
    **See Also**: **showpage**, **copypage**, **erasepage**
    """
    pd = ctxt.gstate.page_device

    # Null device: output operators do nothing
    if b".NullDevice" in pd:
        return

    if pd[b"OutputDevice"].TYPE == ps.T_STRING:
        device_name = pd[b"OutputDevice"].python_string()
    else:
        device_name = pd[b"OutputDevice"].val.decode("ascii")

    try:
        device = importlib.import_module(f"postforge.devices.{device_name}")
    except (ModuleNotFoundError, ImportError):
        return

    try:
        # Use device-specific flushpage if available (e.g. Qt renders
        # without waiting for keypress or stealing focus), otherwise
        # fall back to showpage for basic rendering.
        if hasattr(device, 'flushpage'):
            device.flushpage(ctxt, pd)
        else:
            device.showpage(ctxt, pd)
    except Exception:
        pass
    finally:
        if "device" in locals():
            del device


def nulldevice(ctxt: "ps.Context", ostack: "ps.Stack") -> None:
    """
    – **nulldevice** –

    Installs a "null" device in the graphics state. The null device has the
    following characteristics:

    - The default transformation matrix is the identity matrix.
    - The default clipping path is a degenerate path (a single point at the origin).
    - Marks placed on the current page by painting operators are discarded.
    - The **showpage** and **copypage** operators do nothing.
    - Graphics state operators (**gsave**, **grestore**, etc.) still work normally.

    **nulldevice** is useful for performing operations that query the graphics state
    (such as computing bounding boxes, string widths, or coordinate transforms)
    without producing any output.

    The null device is typically bracketed by **gsave**/**grestore** and can also be
    cancelled by calling **setpagedevice**.

    PLRM Section 8.2, Page 459 (Second Edition)
    Stack: – **nulldevice** –
    **Errors**: none
    """
    # Replace page_device with minimal null device dict.
    # page_device is shallow-copied by gsave (not in _DEEPCOPY_ATTRS),
    # so saved gstate keeps the old reference — grestore restores it.
    # Save current output device name so setpagedevice can recover it
    prev_device = ctxt.gstate.page_device.get(b"OutputDevice")
    ctxt.gstate.page_device = {b".NullDevice": ps.Bool(True)}
    if prev_device is not None:
        ctxt.gstate.page_device[b".PrevOutputDevice"] = prev_device

    # Set CTM to identity [1 0 0 1 0 0]
    identity = [ps.Real(1.0), ps.Real(0.0), ps.Real(0.0),
                ps.Real(1.0), ps.Real(0.0), ps.Real(0.0)]
    _setCTM(ctxt, identity)

    # Set clipping path to degenerate path (single point at origin)
    clip_path = ps.Path()
    subpath = ps.SubPath()
    subpath.append(ps.MoveTo(ps.Point(0.0, 0.0)))
    clip_path.append(subpath)
    ctxt.gstate.update_clipping_path(clip_path, ps.WINDING_EVEN_ODD)

    # Clear current path
    ctxt.gstate.path = ps.Path()
    ctxt.gstate.currentpoint = None

