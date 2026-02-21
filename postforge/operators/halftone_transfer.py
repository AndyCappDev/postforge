# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from ..core import error as ps_error
from ..core import types as ps


# device dependant
def setscreen(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
        frequency angle proc **setscreen** -
    frequency angle halftone **setscreen** -


    sets the halftone screen parameter in the graphics state (see Section 7.4, "Halftones")
    as specified by the operands. **setscreen** sets the screen identically for all
    four primary color components of the output device (red, green, blue, and gray);
    this distinguishes it from **setcolorscreen**, which sets the four screens independently.

    frequency is a number specifying the screen frequency, measured in halftone cells
    per inch in device space. angle specifies the number of degrees by which the cells
    are rotated counterclockwise with respect to the device coordinate system. (Note,
    however, that most output devices have left-handed device spaces; on such devices,
    a counterclockwise angle in device space will correspond to a clockwise
    angle in default user space and on the physical medium.)

    In the first form of the operator, the proc operand is a PostScript procedure defining
    the spot function, which determines the order in which pixels within a halftone
    cell are whitened to produce any desired shade of gray. In the second form,
    halftone is a halftone dictionary defining the desired screen; in this case, **setscreen**
    performs the equivalent of **sethalftone**, except that if the dictionary is of type 1,
    the values of the frequency and angle operands are copied into the dictionary's
    **Frequency** and **Angle** entries, overriding the original values of those entries. (If
    the dictionary is read-only, **setscreen** makes a copy of it before copying the
    values.) For halftone dictionaries of types other than 1, the frequency and angle
    operands are ignored.

    A **rangecheck** error occurs if proc returns a result outside the range -1.0 to 1.0. A
    **limitcheck** error occurs if the size of the screen cell exceeds implementation limits.

    In LanguageLevel 3, the behavior of **setscreen** can be altered by the user
    parameters **AccurateScreens** (see "Type 1 Halftone Dictionaries" on page 487),
    **HalftoneMode** ("Halftone Setting" on page 757), and **MaxSuperScreen**
    (Section 7.4.8, "Supercells").

    Because the effect of the halftone screen is device-dependent, **setscreen** should
    not be used in a page description that is intended to be device-independent. Execution
    of this operator is not permitted in certain circumstances; see
    Section 4.8.1, "Types of Color Space."

    **Errors**:     **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **currentscreen**, **setcolorscreen**, **sethalftone**
    """

    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setscreen.__name__)

    # Validate types: freq=numeric, angle=numeric, proc=exec array or dict
    if ostack[-1].TYPE not in ps.ARRAY_TYPES and ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setscreen.__name__)

    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setscreen.__name__)

    if ostack[-3].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setscreen.__name__)

    proc_or_dict = ostack.pop()
    angle_val = ostack.pop().val
    freq_val = ostack.pop().val

    ctxt.gstate.screen_params = (freq_val, angle_val, proc_or_dict)
    # setscreen supersedes sethalftone/setcolorscreen (PLRM Section 7.4)
    ctxt.gstate.halftone = None
    ctxt.gstate.color_screen_params = None


def currentscreen(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentscreen** frequency angle proc
    - **currentscreen** frequency angle halftone


    returns the frequency, angle, and spot function of the current halftone screen parameter
    in the graphics state (see Section 7.4, "Halftones"), assuming that the
    halftone was established via the **setscreen** operator. If **setcolorscreen** was used instead,
    the values returned describe the screen for the gray color component only.

    If the current halftone was defined via the **sethalftone** operator, **currentscreen** returns
    a halftone dictionary describing its properties in place of the spot function.
    For type 1 halftone dictionaries, the values returned for frequency and angle are
    taken from the dictionary's **Frequency** and **Angle** entries; for all other halftone
    types, **currentscreen** returns a frequency of 60 and an angle of 0.

    **Errors**:     **stackoverflow**
    **See Also**:   **setscreen**, **setcolorscreen**, **sethalftone**, **currentcolorscreen**,
                **currenthalftone**
    """

    if ctxt.MaxOpStack and len(ostack) + 3 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentscreen.__name__)

    # Priority: screen_params → gray component of color_screen_params → halftone → defaults
    if ctxt.gstate.screen_params is not None:
        freq, angle, proc = ctxt.gstate.screen_params
        ostack.append(ps.Real(float(freq)))
        ostack.append(ps.Real(float(angle)))
        ostack.append(proc)
    elif ctxt.gstate.color_screen_params is not None:
        # Gray is the 4th (last) component
        freq, angle, proc = ctxt.gstate.color_screen_params[3]
        ostack.append(ps.Real(float(freq)))
        ostack.append(ps.Real(float(angle)))
        ostack.append(proc)
    elif ctxt.gstate.halftone is not None:
        ht = ctxt.gstate.halftone
        ht_type = ht.val.get(b'HalftoneType')
        if ht_type and ht_type.val == 1:
            freq_obj = ht.val.get(b'Frequency', ps.Int(60))
            angle_obj = ht.val.get(b'Angle', ps.Int(0))
            ostack.append(ps.Real(float(freq_obj.val)))
            ostack.append(ps.Real(float(angle_obj.val)))
        else:
            ostack.append(ps.Real(60.0))
            ostack.append(ps.Real(0.0))
        ostack.append(ht)
    else:
        ostack.append(ps.Real(60.0))
        ostack.append(ps.Real(45.0))
        empty_proc = ps.Array(ctxt.id)
        empty_proc.length = 0
        empty_proc.attrib = ps.ATTRIB_EXEC
        ostack.append(empty_proc)


def currenthalftone(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currenthalftone** halftone

    Returns a halftone dictionary describing the current halftone in the
    graphics state. If the halftone was set by **setscreen**, returns a type 1
    halftone dictionary with default values.

    **Errors**: **stackoverflow**
    """
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currenthalftone.__name__)

    if ctxt.gstate.halftone is not None:
        ostack.append(ctxt.gstate.halftone)
    else:
        # Return default type 1 halftone dictionary
        ht = ps.Dict(ctxt.id, name=b"halftone", is_global=ctxt.vm_alloc_mode)
        ht.put(ps.Name(b"HalftoneType"), ps.Int(1))
        ht.put(ps.Name(b"Frequency"), ps.Int(60))
        ht.put(ps.Name(b"Angle"), ps.Int(45))
        spot = ps.Array(ctxt.id)
        spot.length = 0
        spot.attrib = ps.ATTRIB_EXEC
        ht.put(ps.Name(b"SpotFunction"), spot)
        ostack.append(ht)


def sethalftone(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    halftone **sethalftone** -

    Sets the halftone parameter in the graphics state to the specified
    halftone dictionary.

    Stack: halftone **sethalftone** -
    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, sethalftone.__name__)

    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, sethalftone.__name__)

    ctxt.gstate.halftone = ostack.pop()
    # sethalftone supersedes setscreen/setcolorscreen (PLRM Section 7.4)
    ctxt.gstate.screen_params = None
    ctxt.gstate.color_screen_params = None


def settransfer(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    proc **settransfer** -


    sets the transfer function parameter in the graphics state to proc (see Section 7.3,
    "Transfer Functions"). A transfer function is a procedure that adjusts the value of
    a color component to compensate for nonlinear response in an output device and
    in the human eye. The procedure is called with a number in the range 0.0 to 1.0
    on the operand stack and must return a number in the same range. **settransfer**
    sets the transfer function identically for all four primary color components of the
    output device (red, green, blue, and gray); this distinguishes it from **setcolortransfer**,
    which sets the four transfer functions independently.

    Because the effect of the transfer function is device-dependent **settransfer** should
    not be used in a page description that is intended to be device-independent. Execution
    of this operator is not permitted in certain circumstances; see
    Section 4.8.1, "Types of Color Space."

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **currenttransfer**, **setcolortransfer**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, settransfer.__name__)

    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, settransfer.__name__)

    # Store transfer function in graphics state
    transfer_proc = ostack.pop()
    ctxt.gstate.transfer_function = transfer_proc


def setblackgeneration(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    proc **setblackgeneration** -

    Sets the black-generation function parameter in the graphics state to proc.
    The black-generation function controls how much black ink is added during
    conversion from **DeviceRGB** or **DeviceCMYK** to device color. The procedure is
    called with a number in the range 0.0 to 1.0 (representing the amount of
    undercolor) and must return a number in the same range (the amount of black
    to generate).

    Because the effect is device-dependent, **setblackgeneration** should not be used
    in a page description that is intended to be device-independent.

    PLRM Section 8.2, Page 658 (Third Edition)
    Stack: proc → -
    **Errors**: **stackunderflow**, **typecheck**
    **See Also**: **currentblackgeneration**, **setundercolorremoval**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setblackgeneration.__name__)

    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setblackgeneration.__name__)

    # Store black generation function in graphics state
    ctxt.gstate.black_generation = ostack.pop()


def currentblackgeneration(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentblackgeneration** proc

    Returns the current black-generation function from the graphics state.
    If no black-generation function has been set, returns an empty procedure {}.

    PLRM Section 8.2, Page 552 (Third Edition)
    Stack: - → proc
    **Errors**: **stackoverflow**
    **See Also**: **setblackgeneration**, **setundercolorremoval**
    """
    # Return current black generation function, or empty procedure if not set
    if ctxt.gstate.black_generation is not None:
        ostack.append(ctxt.gstate.black_generation)
    else:
        # Return empty executable array (procedure) as default
        empty_proc = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
        empty_proc.attrib = ps.ATTRIB_EXEC
        ostack.append(empty_proc)


def setundercolorremoval(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    proc **setundercolorremoval** -

    Sets the undercolor-removal function parameter in the graphics state to proc.
    The undercolor-removal function controls how much cyan, magenta, and yellow
    to remove during conversion from **DeviceRGB** or **DeviceCMYK** to device color,
    to compensate for the black that was added by black generation. The procedure
    is called with a number in the range 0.0 to 1.0 (the amount of black generated)
    and must return a number in the same range (the amount of color to remove).

    Because the effect is device-dependent, **setundercolorremoval** should not be used
    in a page description that is intended to be device-independent.

    PLRM Section 8.2, Page 710 (Third Edition)
    Stack: proc → -
    **Errors**: **stackunderflow**, **typecheck**
    **See Also**: currentundercolorremoval, **setblackgeneration**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setundercolorremoval.__name__)

    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-1].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setundercolorremoval.__name__)

    # Store undercolor removal function in graphics state
    ctxt.gstate.undercolor_removal = ostack.pop()


def currentundercolorremoval(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentundercolorremoval** proc

    Returns the current undercolor-removal function from the graphics state.
    If no undercolor-removal function has been set, returns an empty procedure {}.

    PLRM Section 8.2, Page 557 (Third Edition)
    Stack: - → proc
    **Errors**: **stackoverflow**
    **See Also**: **setundercolorremoval**, **setblackgeneration**
    """
    # Return current undercolor removal function, or empty procedure if not set
    if ctxt.gstate.undercolor_removal is not None:
        ostack.append(ctxt.gstate.undercolor_removal)
    else:
        # Return empty executable array (procedure) as default
        empty_proc = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
        empty_proc.attrib = ps.ATTRIB_EXEC
        ostack.append(empty_proc)
