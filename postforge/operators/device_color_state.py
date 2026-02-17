# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Device-Dependent Color State Operators

Implements Level 2 device-dependent graphics state operators for halftone
screens, transfer functions, and color rendering:
  currenttransfer, setcolorscreen, currentcolorscreen,
  setcolortransfer, currentcolortransfer,
  setcolorrendering, currentcolorrendering
"""

from ..core import error as ps_error
from ..core import types as ps


def currenttransfer(ctxt, ostack):
    """
    - **currenttransfer** proc

    Returns the current transfer function from the graphics state.
    If no transfer function has been set, returns an empty procedure {}.

    PLRM Section 8.2
    Stack: - -> proc
    **Errors**: **stackoverflow**
    **See Also**: **settransfer**, **setcolortransfer**, **currentcolortransfer**
    """
    if ctxt.gstate.transfer_function is not None:
        ostack.append(ctxt.gstate.transfer_function)
    else:
        empty_proc = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
        empty_proc.attrib = ps.ATTRIB_EXEC
        ostack.append(empty_proc)


def setcolorscreen(ctxt, ostack):
    """
    redfreq redang redproc greenfreq greenang greenproc bluefreq blueang blueproc grayfreq grayang grayproc **setcolorscreen** –

    redfreq redang redproc greenfreq greenang greenproc
    bluefreq blueang blueproc grayfreq grayang grayproc **setcolorscreen** -

    Sets the halftone screen parameters independently for each of the four
    primary color components (red, green, blue, gray).

    PLRM Section 8.2
    **Errors**: **stackunderflow**, **typecheck**
    **See Also**: **currentcolorscreen**, **setscreen**
    """
    if len(ostack) < 12:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolorscreen.__name__)

    # Validate all 4 triplets (gray=top, blue, green, red=bottom)
    for i in range(4):
        base = -(i * 3) - 1
        # proc/dict
        if ostack[base].TYPE not in ps.ARRAY_TYPES and ostack[base].TYPE != ps.T_DICT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorscreen.__name__)
        # angle
        if ostack[base - 1].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorscreen.__name__)
        # frequency
        if ostack[base - 2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorscreen.__name__)

    # Pop in reverse order: gray (top), blue, green, red (bottom)
    gray_proc = ostack.pop()
    gray_angle = ostack.pop().val
    gray_freq = ostack.pop().val
    blue_proc = ostack.pop()
    blue_angle = ostack.pop().val
    blue_freq = ostack.pop().val
    green_proc = ostack.pop()
    green_angle = ostack.pop().val
    green_freq = ostack.pop().val
    red_proc = ostack.pop()
    red_angle = ostack.pop().val
    red_freq = ostack.pop().val

    ctxt.gstate.color_screen_params = (
        (red_freq, red_angle, red_proc),
        (green_freq, green_angle, green_proc),
        (blue_freq, blue_angle, blue_proc),
        (gray_freq, gray_angle, gray_proc),
    )
    # Also update screen_params with gray component (PLRM interop)
    ctxt.gstate.screen_params = (gray_freq, gray_angle, gray_proc)
    # setcolorscreen supersedes sethalftone (PLRM Section 7.4)
    ctxt.gstate.halftone = None


def currentcolorscreen(ctxt, ostack):
    """
    - **currentcolorscreen** redfreq redang redproc greenfreq greenang greenproc
                         bluefreq blueang blueproc grayfreq grayang grayproc

    Returns the current halftone screen parameters for all four color
    components. If **setcolorscreen** has not been used, returns the values
    from **setscreen** (replicated x4) or halftone or defaults.

    PLRM Section 8.2
    **Errors**: **stackoverflow**
    **See Also**: **setcolorscreen**, **currentscreen**
    """
    if ctxt.MaxOpStack and len(ostack) + 12 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentcolorscreen.__name__)

    if ctxt.gstate.color_screen_params is not None:
        # Return stored 4-component screen params
        for triplet in ctxt.gstate.color_screen_params:
            freq, angle, proc = triplet
            ostack.append(ps.Real(float(freq)))
            ostack.append(ps.Real(float(angle)))
            ostack.append(proc)
    elif ctxt.gstate.screen_params is not None:
        # Replicate single screen across all 4 components
        freq, angle, proc = ctxt.gstate.screen_params
        for _ in range(4):
            ostack.append(ps.Real(float(freq)))
            ostack.append(ps.Real(float(angle)))
            ostack.append(proc)
    elif ctxt.gstate.halftone is not None:
        ht = ctxt.gstate.halftone
        ht_type = ht.val.get(b'HalftoneType')
        if ht_type and ht_type.val == 1:
            freq_obj = ht.val.get(b'Frequency', ps.Int(60))
            angle_obj = ht.val.get(b'Angle', ps.Int(0))
            for _ in range(4):
                ostack.append(ps.Real(float(freq_obj.val)))
                ostack.append(ps.Real(float(angle_obj.val)))
                ostack.append(ht)
        else:
            for _ in range(4):
                ostack.append(ps.Real(60.0))
                ostack.append(ps.Real(0.0))
                ostack.append(ht)
    else:
        # Defaults
        for _ in range(4):
            ostack.append(ps.Real(60.0))
            ostack.append(ps.Real(45.0))
            empty_proc = ps.Array(ctxt.id)
            empty_proc.length = 0
            empty_proc.attrib = ps.ATTRIB_EXEC
            ostack.append(empty_proc)


def setcolortransfer(ctxt, ostack):
    """
    redproc greenproc blueproc grayproc **setcolortransfer** -

    Sets the transfer functions for each of the four primary color
    components independently.

    PLRM Section 8.2
    Stack: redproc greenproc blueproc grayproc -> -
    **Errors**: **stackunderflow**, **typecheck**
    **See Also**: **currentcolortransfer**, **settransfer**
    """
    if len(ostack) < 4:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolortransfer.__name__)

    # Validate all 4 are executable arrays
    for i in range(-1, -5, -1):
        if ostack[i].TYPE not in ps.ARRAY_TYPES or ostack[i].attrib != ps.ATTRIB_EXEC:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolortransfer.__name__)

    gray_proc = ostack.pop()
    blue_proc = ostack.pop()
    green_proc = ostack.pop()
    red_proc = ostack.pop()

    ctxt.gstate.color_transfer = (red_proc, green_proc, blue_proc, gray_proc)
    # Also set transfer_function to gray proc (PLRM interop with settransfer)
    ctxt.gstate.transfer_function = gray_proc


def currentcolortransfer(ctxt, ostack):
    """
    - **currentcolortransfer** redproc greenproc blueproc grayproc

    Returns the current transfer functions for all four color components.
    If **setcolortransfer** has not been used, returns the **settransfer** value
    replicated x4, or empty procs as defaults.

    PLRM Section 8.2
    **Errors**: **stackoverflow**
    **See Also**: **setcolortransfer**, **currenttransfer**
    """
    if ctxt.MaxOpStack and len(ostack) + 4 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentcolortransfer.__name__)

    if ctxt.gstate.color_transfer is not None:
        for proc in ctxt.gstate.color_transfer:
            ostack.append(proc)
    elif ctxt.gstate.transfer_function is not None:
        for _ in range(4):
            ostack.append(ctxt.gstate.transfer_function)
    else:
        for _ in range(4):
            empty_proc = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
            empty_proc.attrib = ps.ATTRIB_EXEC
            ostack.append(empty_proc)


def setcolorrendering(ctxt, ostack):
    """
    dict **setcolorrendering** -

    Sets the color rendering dictionary in the graphics state.

    PLRM Section 8.2
    Stack: dict -> -
    **Errors**: **stackunderflow**, **typecheck**
    **See Also**: **currentcolorrendering**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolorrendering.__name__)

    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorrendering.__name__)

    ctxt.gstate.color_rendering = ostack.pop()


def currentcolorrendering(ctxt, ostack):
    """
    - **currentcolorrendering** dict

    Returns the current color rendering dictionary from the graphics state.
    If none has been set, returns an empty dictionary.

    PLRM Section 8.2
    **Errors**: **stackoverflow**
    **See Also**: **setcolorrendering**
    """
    if ctxt.gstate.color_rendering is not None:
        ostack.append(ctxt.gstate.color_rendering)
    else:
        ostack.append(ps.Dict(ctxt.id, is_global=ctxt.vm_alloc_mode))
