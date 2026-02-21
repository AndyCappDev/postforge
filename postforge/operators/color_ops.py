# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import copy

from ..core import error as ps_error
from ..core import icc_profile
from ..core import types as ps
from ..core import color_space
from ..core.color_space import ColorSpaceEngine


def setgray(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num **setgray** -

    sets the current color space in the graphics state to **DeviceGray** and the current
    color to the gray level specified by num. The gray level must be a number in the
    range 0.0 to 1.0, with 0.0 denoting black and 1.0 denoting white. If num is outside
    this range, the nearest valid value is substituted without error indication.

    Execution of this operator is not permitted in certain circumstances; see
    Section 4.8.1, "Types of Color Space."

    **Errors**:     **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **currentgray**, **setcolorspace**, **setcolor**, **setrgbcolor**,
                **sethsbcolor**, **setcmykcolor**
    """
    # PLRM Section 8.2, Page 571 - exact PLRM implementation

    # 0. Type 3 font cache mode restriction - UNDEFINED if called after setcachedevice
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setgray.__name__)

    # 1. Stack validation - must be done BEFORE popping operands
    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setgray.__name__)

    # 2. Type validation - must be done BEFORE popping operands
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setgray.__name__)

    # 3. ONLY after all validation passes - get and clamp value
    gray = float(ostack[-1].val)

    # Clamp to valid range (0.0-1.0) as specified in PLRM
    gray = max(0.0, min(1.0, gray))

    # 4. CRITICAL: Set color space to DeviceGray and single component
    ctxt.gstate.color_space = ["DeviceGray"]  # Must be array per PLRM
    ctxt.gstate.color = [gray]                # Single component, not [gray,gray,gray]!

    # 5. Pop operand only after successful completion
    ostack.pop()


def setcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
            comp(1) ... comp(n) **setcolor** -
                        pattern **setcolor** -
    comp(1) ... comp(n) pattern **setcolor** -

    sets the current color in the graphics state.

    The appropriate form of the operator depends on the current color space. All
    color spaces except **Pattern** use the first form, in which the operands comp1
    through comp(n) specify the values of the color components describing the desired
    color. The number of components and the valid ranges of their values depend on
    the specific characteristics of the color space; see Section 4.8, "Color Spaces." (In
    the case of an **Indexed** color space, the single operand comp(1) is actually an index
    into the space's color table rather than a true color component.) If the wrong
    number of components is specified, an error will occur, such as **stackunderflow** or
    **typecheck**. If a component value is outside the valid range, the nearest valid value
    will be substituted without error indication.

    The second and third forms of **setcolor** are used when the current color space is a
    **Pattern** space. In both forms, the pattern operand is a pattern dictionary describing
    the pattern to be established as the current color. The values of the dictionary's
    **PatternType** and **PaintType** entries determine whether additional operands
    are needed:

        • Shading patterns (**PatternType** 2) or colored tiling patterns (**PatternType** 1,
          **PaintType** 1) use the second form of the operator, in which the pattern dictionary
          is the only operand.

        • Uncolored tiling patterns (**PatternType** 1, **PaintType** 2) use the third form, in
          which the dictionary is accompanied by one or more component values in the
          pattern's underlying color space, defining the color in which the pattern is to
          be painted.

    The **setcolorspace** operator initializes the current color to a value that depends on
    the specific color space selected.

    Execution of this operator is not permitted in certain circumstances; see
    Section 4.8.1, "Types of Color Space."

    **Errors**:     **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **currentcolor**, **setcolorspace**, **setgray**, **setrgbcolor**,
                **sethsbcolor**, **setcmykcolor**
    """
    # PLRM Section 8.2, Page 571 - exact PLRM implementation

    # 0. Type 3 font cache mode restriction - UNDEFINED if called after setcachedevice
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcolor.__name__)

    # 1. Determine current color space and required components
    if not ctxt.gstate.color_space:
        # Fallback for empty color space (shouldn't happen after fixes)
        current_space = "DeviceGray"
    else:
        current_space = ctxt.gstate.color_space[0]

    # Handle different color space types
    if current_space == "Separation":
        # Separation color space: single tint component
        required_components = 1

        # Stack validation
        if len(ostack) < required_components:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        # Type validation
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        # Get and clamp tint value
        tint = max(0.0, min(1.0, float(ostack[-1].val)))

        # Get tint transform and alternative space from color space array
        tint_transform = ctxt.gstate.color_space[3]
        alt_space_name = _get_color_space_name(ctxt.gstate.color_space[2])

        # Execute tint transform to get alternative space color
        ctxt.gstate.color = _execute_tint_transform(ctxt, [tint], tint_transform, alt_space_name)

        # Pop operand
        ostack.pop()
        return

    elif current_space == "DeviceN":
        # DeviceN color space: n tint components (one per colorant)
        names_array = ctxt.gstate.color_space[1]
        required_components = names_array.length

        # Stack validation
        if len(ostack) < required_components:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        # Type validation
        for i in range(-required_components, 0):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        # Get and clamp tint values
        tints = []
        for i in range(-required_components, 0):
            tint = max(0.0, min(1.0, float(ostack[i].val)))
            tints.append(tint)

        # Get tint transform and alternative space from color space array
        tint_transform = ctxt.gstate.color_space[3]
        alt_space_name = _get_color_space_name(ctxt.gstate.color_space[2])

        # Execute tint transform to get alternative space color
        ctxt.gstate.color = _execute_tint_transform(ctxt, tints, tint_transform, alt_space_name)

        # Pop operands
        for _ in range(required_components):
            ostack.pop()
        return

    elif current_space == "Indexed":
        # Indexed color space: single index operand
        required_components = 1

        # Stack validation
        if len(ostack) < required_components:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        # Type validation
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        # Get index value - round float to nearest int per PLRM
        index = round(float(ostack[-1].val))

        # Clamp to [0, hival]
        hival_obj = ctxt.gstate.color_space[2]
        hival_val = hival_obj.val if hasattr(hival_obj, 'val') else int(hival_obj)
        index = max(0, min(hival_val, index))

        # Look up palette color and store resolved base-space color
        ctxt.gstate.color = _lookup_palette_color(ctxt, index, ctxt.gstate.color_space)

        # Pop operand
        ostack.pop()
        return

    elif current_space == "CIEBasedABC":
        # CIEBasedABC color space: 3 components
        required_components = 3

        # Stack validation
        if len(ostack) < required_components:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        # Type validation
        for i in range(-required_components, 0):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        # Get component values
        components = [float(ostack[i].val) for i in range(-3, 0)]

        # Extract CIE dict and convert to RGB
        cie_dict = _extract_cie_dict(ctxt.gstate.color_space)
        r, g, b = ColorSpaceEngine.cie_abc_to_rgb(components, cie_dict)
        ctxt.gstate.color = [r, g, b]

        # Pop operands
        for _ in range(required_components):
            ostack.pop()
        return

    elif current_space == "CIEBasedA":
        # CIEBasedA color space: 1 component
        required_components = 1

        # Stack validation
        if len(ostack) < required_components:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        # Type validation
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        # Get component value
        value = float(ostack[-1].val)

        # Extract CIE dict and convert to RGB
        cie_dict = _extract_cie_dict(ctxt.gstate.color_space)
        r, g, b = ColorSpaceEngine.cie_a_to_rgb(value, cie_dict)
        ctxt.gstate.color = [r, g, b]

        # Pop operand
        ostack.pop()
        return

    elif current_space == "CIEBasedDEF":
        # CIEBasedDEF color space: 3 components (DEF→Table→ABC pipeline)
        required_components = 3

        if len(ostack) < required_components:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        for i in range(-required_components, 0):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        components = [float(ostack[i].val) for i in range(-3, 0)]

        cie_dict = _extract_cie_dict(ctxt.gstate.color_space)
        r, g, b = ColorSpaceEngine.cie_def_to_rgb(components, cie_dict)
        ctxt.gstate.color = [r, g, b]

        for _ in range(required_components):
            ostack.pop()
        return

    elif current_space == "CIEBasedDEFG":
        # CIEBasedDEFG color space: 4 components (DEFG→Table→ABC pipeline)
        required_components = 4

        if len(ostack) < required_components:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        for i in range(-required_components, 0):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        components = [float(ostack[i].val) for i in range(-4, 0)]

        cie_dict = _extract_cie_dict(ctxt.gstate.color_space)
        r, g, b = ColorSpaceEngine.cie_defg_to_rgb(components, cie_dict)
        ctxt.gstate.color = [r, g, b]

        for _ in range(required_components):
            ostack.pop()
        return

    elif current_space == "ICCBased":
        # ICCBased color space: N components (from stream /N)
        n = ColorSpaceEngine.get_component_count(ctxt.gstate.color_space)

        # Stack validation
        if len(ostack) < n:
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

        # Type validation
        for i in range(-n, 0):
            if ostack[i].TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

        # Get and clamp components
        components = [max(0.0, min(1.0, float(ostack[i].val))) for i in range(-n, 0)]
        ctxt.gstate.color = components

        for _ in range(n):
            ostack.pop()
        return

    # Standard device color spaces
    component_counts = {
        "DeviceGray": 1,
        "DeviceRGB": 3,
        "DeviceCMYK": 4,
    }

    if current_space not in component_counts:
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcolor.__name__)

    required_components = component_counts[current_space]

    # 2. Stack validation - must be done BEFORE popping operands
    if len(ostack) < required_components:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolor.__name__)

    # 3. Type validation - must be done BEFORE popping operands
    for i in range(-required_components, 0):
        if ostack[i].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolor.__name__)

    # 4. ONLY after all validation passes - get and clamp component values
    components = []
    for i in range(-required_components, 0):
        value = float(ostack[i].val)
        # Clamp to valid range (0.0-1.0) as specified in PLRM
        value = max(0.0, min(1.0, value))
        components.append(value)

    # 5. Set color components in graphics state
    ctxt.gstate.color = components

    # 6. Pop operands only after successful completion
    for _ in range(required_components):
        ostack.pop()


def setcolorspace(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    array **setcolorspace** -
     name **setcolorspace** -

    The first form sets the color space parameter in the graphics state to that described
    by the specified array. The array must be in the form [key param1 ... paramn]
    where key is a name that identifies the color space family and the parameters
    param1 ... paramn further describe the space as a whole.

    The second form specifies a color space by giving just its name. This is allowed only
    for those color spaces that require no parameters, namely **DeviceGray**, **DeviceRGB**,
    **DeviceCMYK**, and Pattern. Specifying a color space by name is equivalent to
    specifying it by an array containing just that name.

    The **setcolorspace** operator also sets the current color parameter in the graphics state
    to its initial value, which depends on the color space.

    PLRM Section 8.2, Page 578
    **Errors**: **stackunderflow**, **typecheck**, **rangecheck**, **undefined**
    """
    # 0. Type 3 font cache mode restriction - UNDEFINED if called after setcachedevice
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcolorspace.__name__)

    # 1. Stack validation - must be done BEFORE popping operands
    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcolorspace.__name__)

    # 2. Type validation and conversion to array format
    operand = ostack[-1]

    if operand.TYPE == ps.T_NAME:
        # Second form: name setcolorspace (convert to array format)
        name_str = operand.val.decode('ascii') if isinstance(operand.val, bytes) else operand.val

        # Only device spaces and Pattern can be specified by name alone
        if name_str not in ["DeviceGray", "DeviceRGB", "DeviceCMYK", "Pattern"]:
            return ps_error.e(ctxt, ps_error.UNDEFINED, setcolorspace.__name__)

        color_space_array = [name_str]

    elif operand.TYPE in ps.ARRAY_TYPES:
        # First form: array setcolorspace
        if operand.length == 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Convert PS array to Python list for validation
        color_space_array = []
        for i in range(operand.length):
            element = operand.val[i]
            if i == 0:  # First element must be name
                if element.TYPE == ps.T_NAME:
                    name_str = element.val.decode('ascii') if isinstance(element.val, bytes) else element.val
                    color_space_array.append(name_str)
                else:
                    return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)
            else:
                # Additional parameters - preserve PS objects for procedures
                color_space_array.append(element)
    else:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

    # 3. Validate color space based on type
    space_name = color_space_array[0]

    if space_name in color_space.ColorSpaceEngine.DEVICE_SPACES:
        # Device color spaces - simple validation
        if len(color_space_array) != 1:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)
        # Set color space and initial color
        ctxt.gstate.color_space = color_space_array
        ctxt.gstate.color = color_space.ColorSpaceEngine.get_default_color(color_space_array)

    elif space_name == "Separation":
        # Separation color space: [/Separation name alternativeSpace tintTransform]
        # PLRM Section 4.8.4 - Separation Color Spaces
        if len(color_space_array) != 4:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        colorant_name = color_space_array[1]
        alt_space = color_space_array[2]
        tint_transform = color_space_array[3]

        # Validate colorant name (must be name or string)
        if colorant_name.TYPE not in [ps.T_NAME, ps.T_STRING]:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        # Validate alternative space
        alt_space_name = _get_color_space_name(alt_space)
        if alt_space_name not in color_space.ColorSpaceEngine.DEVICE_SPACES:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Validate tint transform (must be executable procedure)
        if tint_transform.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        # Set color space (store full array with PS objects)
        ctxt.gstate.color_space = color_space_array

        # Set initial color: tint of 1.0 (maximum colorant), execute tint transform
        # to get alternative space values
        initial_tint = [1.0]
        ctxt.gstate.color = _execute_tint_transform(ctxt, initial_tint, tint_transform, alt_space_name)

    elif space_name == "DeviceN":
        # DeviceN color space: [/DeviceN names alternativeSpace tintTransform]
        # PLRM Section 4.8.4 - DeviceN Color Spaces (LanguageLevel 3)
        if len(color_space_array) != 4:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        names_array = color_space_array[1]
        alt_space = color_space_array[2]
        tint_transform = color_space_array[3]

        # Validate names array
        if names_array.TYPE not in ps.ARRAY_TYPES or names_array.length == 0:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        # Validate each colorant name
        for i in range(names_array.length):
            name_elem = names_array.val[i]
            if name_elem.TYPE not in [ps.T_NAME, ps.T_STRING]:
                return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)
            # Check for disallowed special names
            name_val = name_elem.val.decode('ascii') if isinstance(name_elem.val, bytes) else name_elem.val
            if name_val in ["All", "None"]:
                return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Validate alternative space
        alt_space_name = _get_color_space_name(alt_space)
        if alt_space_name not in color_space.ColorSpaceEngine.DEVICE_SPACES:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Validate tint transform (must be executable procedure)
        if tint_transform.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        # Set color space (store full array with PS objects)
        ctxt.gstate.color_space = color_space_array

        # Set initial color: all tints at 1.0 (maximum colorant), execute tint transform
        num_colorants = names_array.length
        initial_tints = [1.0] * num_colorants
        ctxt.gstate.color = _execute_tint_transform(ctxt, initial_tints, tint_transform, alt_space_name)

    elif space_name == "Indexed":
        # Indexed color space: [/Indexed base hival lookup]
        # PLRM Section 4.8.3 - Indexed Color Spaces
        if len(color_space_array) != 4:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        base_space = color_space_array[1]
        hival_obj = color_space_array[2]
        lookup = color_space_array[3]

        # Validate base space - must be a device space (Level 2 restriction)
        base_space_name = _get_color_space_name(base_space)
        if base_space_name not in color_space.ColorSpaceEngine.DEVICE_SPACES:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Validate hival - must be integer 0-4095
        if hival_obj.TYPE != ps.T_INT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)
        if hival_obj.val < 0 or hival_obj.val > 4095:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        hival = hival_obj.val
        base_component_count = color_space.ColorSpaceEngine.COMPONENT_COUNTS.get(base_space_name, 3)

        # Validate lookup - must be string of correct length or executable procedure
        if lookup.TYPE == ps.T_STRING:
            expected_length = (hival + 1) * base_component_count
            if lookup.length < expected_length:
                return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)
        elif lookup.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        # Set color space (store full array with PS objects)
        ctxt.gstate.color_space = color_space_array

        # Set initial color: index 0, resolved via palette lookup
        ctxt.gstate.color = _lookup_palette_color(ctxt, 0, color_space_array)

    elif space_name == "CIEBasedABC":
        # CIEBasedABC color space: [/CIEBasedABC dict]
        # PLRM Section 4.8.2 - CIE-Based Color Spaces
        if len(color_space_array) != 2:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        cie_dict_obj = color_space_array[1]
        if not hasattr(cie_dict_obj, 'TYPE') or cie_dict_obj.TYPE != ps.T_DICT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        # WhitePoint is required
        cie_dict = cie_dict_obj.val
        if b"WhitePoint" not in cie_dict:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Set color space (store full array with PS objects)
        ctxt.gstate.color_space = color_space_array

        # Set initial color: all zeros → resolve to RGB via CIE pipeline
        r, g, b = ColorSpaceEngine.cie_abc_to_rgb([0, 0, 0], cie_dict)
        ctxt.gstate.color = [r, g, b]

    elif space_name == "CIEBasedA":
        # CIEBasedA color space: [/CIEBasedA dict]
        # PLRM Section 4.8.2 - CIE-Based Color Spaces
        if len(color_space_array) != 2:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        cie_dict_obj = color_space_array[1]
        if not hasattr(cie_dict_obj, 'TYPE') or cie_dict_obj.TYPE != ps.T_DICT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        # WhitePoint is required
        cie_dict = cie_dict_obj.val
        if b"WhitePoint" not in cie_dict:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Set color space (store full array with PS objects)
        ctxt.gstate.color_space = color_space_array

        # Set initial color: zero → resolve to RGB via CIE pipeline
        r, g, b = ColorSpaceEngine.cie_a_to_rgb(0, cie_dict)
        ctxt.gstate.color = [r, g, b]

    elif space_name == "CIEBasedDEF":
        # CIEBasedDEF color space: [/CIEBasedDEF dict]
        # PLRM Section 4.8.2 - 3 components with DEF→Table→ABC pipeline
        if len(color_space_array) != 2:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        cie_dict_obj = color_space_array[1]
        if not hasattr(cie_dict_obj, 'TYPE') or cie_dict_obj.TYPE != ps.T_DICT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        cie_dict = cie_dict_obj.val
        if b"WhitePoint" not in cie_dict:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        ctxt.gstate.color_space = color_space_array

        # Initial color: all zeros → resolve via DEF Table → ABC → RGB pipeline
        r, g, b = ColorSpaceEngine.cie_def_to_rgb([0, 0, 0], cie_dict)
        ctxt.gstate.color = [r, g, b]

    elif space_name == "CIEBasedDEFG":
        # CIEBasedDEFG color space: [/CIEBasedDEFG dict]
        # PLRM Section 4.8.2 - 4 components with DEFG→Table→ABC pipeline
        if len(color_space_array) != 2:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        cie_dict_obj = color_space_array[1]
        if not hasattr(cie_dict_obj, 'TYPE') or cie_dict_obj.TYPE != ps.T_DICT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcolorspace.__name__)

        cie_dict = cie_dict_obj.val
        if b"WhitePoint" not in cie_dict:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        ctxt.gstate.color_space = color_space_array

        # Initial color: all zeros + full black → resolve via DEFG Table → ABC → RGB
        r, g, b = ColorSpaceEngine.cie_defg_to_rgb([0, 0, 0, 1], cie_dict)
        ctxt.gstate.color = [r, g, b]

    elif space_name == "ICCBased":
        # ICCBased color space: [/ICCBased stream]
        # PLRM Section 4.8.4 - ICCBased Color Spaces (LanguageLevel 3)
        # Tier 1: map to alternate/fallback device space; no ICC profile processing.
        if len(color_space_array) != 2:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Validate /N exists and is a supported value
        try:
            n = ColorSpaceEngine.get_component_count(color_space_array)
        except ValueError:
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)
        if n not in (1, 3, 4):
            return ps_error.e(ctxt, ps_error.RANGECHECK, setcolorspace.__name__)

        # Store ICCBased array as-is (preserves stream for Tier 2)
        ctxt.gstate.color_space = color_space_array

        # Tier 2: Extract ICC profile from stream and cache transform
        if icc_profile.is_available():
            stream_obj = color_space_array[1]
            profile_hash = icc_profile.register_stream(ctxt, stream_obj)
            if profile_hash is not None:
                icc_profile.get_transform(profile_hash, n)  # Pre-build

        # Initial color: black in equivalent device space
        ctxt.gstate.color = ColorSpaceEngine.get_default_color(color_space_array)

    else:
        # Unknown color space
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcolorspace.__name__)

    # 5. Pop operand only after successful completion
    ostack.pop()


def _get_color_space_name(space_obj: ps.PSObject) -> str | None:
    """
    Extract color space name from a space object (Name or Array).

    Args:
        space_obj: PostScript Name or Array representing a color space

    Returns:
        String name of the color space (e.g., "**DeviceRGB**")
    """
    if space_obj.TYPE == ps.T_NAME:
        return space_obj.val.decode('ascii') if isinstance(space_obj.val, bytes) else space_obj.val
    elif space_obj.TYPE in ps.ARRAY_TYPES and space_obj.length > 0:
        first_elem = space_obj.val[0]
        if first_elem.TYPE == ps.T_NAME:
            return first_elem.val.decode('ascii') if isinstance(first_elem.val, bytes) else first_elem.val
    return None


def _execute_tint_transform(ctxt: ps.Context, tint_values: list[float], tint_transform: ps.Array, alt_space_name: str) -> list[float]:
    """
    Execute a tint **transform** procedure to convert tint values to alternative color space.

    Args:
        ctxt: PostScript context
        tint_values: List of Python float tint values (0.0-1.0)
        tint_transform: PostScript procedure (Array) for tint transformation
        alt_space_name: Name of alternative color space (e.g., "**DeviceRGB**")

    Returns:
        List of Python float color values in the alternative color space
    """

    # Local import to avoid circular dependency
    from . import control as ps_control

    # Push tint values onto operand stack
    for tint in tint_values:
        ctxt.o_stack.append(ps.Real(tint))

    # Execute the tint transform procedure
    ctxt.e_stack.append(ps.HardReturn())
    ctxt.e_stack.append(copy.copy(tint_transform))
    ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

    # Get expected component count for alternative space
    alt_component_count = color_space.ColorSpaceEngine.COMPONENT_COUNTS.get(alt_space_name, 3)

    # Pop results from operand stack
    result = []
    for _ in range(alt_component_count):
        if ctxt.o_stack:
            val = ctxt.o_stack.pop()
            if val.TYPE in ps.NUMERIC_TYPES:
                # Clamp to valid range
                result.insert(0, max(0.0, min(1.0, float(val.val))))
            else:
                result.insert(0, 0.0)
        else:
            result.insert(0, 0.0)

    return result


def _lookup_palette_color(ctxt: ps.Context, index: int, color_space_array: list) -> list[float]:
    """
    Look up a color in an Indexed color space palette.

    Args:
        ctxt: PostScript context
        index: Integer index into the palette (0-hival)
        color_space_array: Full Indexed color space array ["Indexed", base, hival, lookup]

    Returns:
        list[float] - Color components in the base color space
    """
    base_space = color_space_array[1]
    lookup = color_space_array[3]

    # Get base space name and component count
    base_space_name = _get_color_space_name(base_space)
    base_component_count = color_space.ColorSpaceEngine.COMPONENT_COUNTS.get(base_space_name, 3)

    # Clamp index to valid range
    hival_obj = color_space_array[2]
    hival_val = hival_obj.val if hasattr(hival_obj, 'val') else int(hival_obj)
    index = max(0, min(hival_val, int(index)))

    if hasattr(lookup, 'TYPE') and lookup.TYPE == ps.T_STRING:
        # String lookup: extract bytes at index * base_component_count, scale 0-255 → 0.0-1.0
        lookup_bytes = lookup.byte_string()
        offset = index * base_component_count
        components = []
        for i in range(base_component_count):
            if offset + i < len(lookup_bytes):
                byte_val = lookup_bytes[offset + i]
                components.append((byte_val if isinstance(byte_val, int) else ord(byte_val)) / 255.0)
            else:
                components.append(0.0)
        return components

    elif hasattr(lookup, 'TYPE') and lookup.TYPE in ps.ARRAY_TYPES:
        # Procedure lookup: push index, execute procedure, pop base_component_count results
        from . import control as ps_control

        ctxt.o_stack.append(ps.Int(index))
        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(copy.copy(lookup))
        ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

        result = []
        for _ in range(base_component_count):
            if ctxt.o_stack:
                val = ctxt.o_stack.pop()
                if val.TYPE in ps.NUMERIC_TYPES:
                    result.insert(0, max(0.0, min(1.0, float(val.val))))
                else:
                    result.insert(0, 0.0)
            else:
                result.insert(0, 0.0)
        return result

    # Fallback
    return [0.0] * base_component_count


def _extract_cie_dict(color_space_array: list) -> dict:
    """Extract the CIE dictionary (Python dict) from a CIEBasedABC/A color space array."""
    if len(color_space_array) >= 2:
        dict_obj = color_space_array[1]
        if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict):
            return dict_obj.val
    return {}


def currentcolorspace(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentcolorspace** array

    returns an array containing the identifying key and parameters of the color space
    in the graphics state. **currentcolorspace** always returns an array, even if the color
    space has no parameters and was selected by presenting just a name to **setcolorspace**.

    PLRM Section 8.2, Page 476
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentcolorspace.__name__)

    # 2. Get current color space from graphics state
    current_space = ctxt.gstate.color_space

    # 3. Create PostScript array - following pattern from array.py
    ps_array = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
    ps_array.val = []

    # Convert Python strings to PostScript Name objects
    for element in current_space:
        if isinstance(element, str):
            # Create PostScript Name - following pattern from tokenizer.py
            ps_name = ps.Name(element.encode('ascii'), is_global=ctxt.vm_alloc_mode)
            ps_array.val.append(ps_name)
        else:
            # Already a PostScript object (for future complex color spaces)
            ps_array.val.append(element)

    ps_array.length = len(ps_array.val)

    # Update local_refs if needed (following array.py pattern)
    if not ps_array.is_global and ps_array.ctxt_id is not None:
        ps.contexts[ps_array.ctxt_id].local_refs[ps_array.created] = ps_array.val

    # 4. Push result array onto operand stack
    ostack.append(ps_array)


def currentcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentcolor** num₁ ... numₙ

    returns the current color, which is a sequence of n numbers specifying individual color component values in the current color space. The number of numbers is appropriate for the current color space: 1 for **DeviceGray**, 3 for **DeviceRGB**, and 4 for **DeviceCMYK**.

    PLRM Section 8.2, Page 530
    Stack: - → num₁ ... numₙ
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation - check if there's room for results
    current_color = ctxt.gstate.color
    if ctxt.MaxOpStack and len(ostack) + len(current_color) > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentcolor.__name__)

    # 2. Push current color components onto operand stack
    for component in current_color:
        if isinstance(component, (int, float)):
            # Convert Python numeric to PostScript numeric
            if isinstance(component, int):
                ostack.append(ps.Int(component))
            else:
                ostack.append(ps.Real(component))
        else:
            # Already a PostScript object
            ostack.append(component)


def _icc_to_rgb(components: list[float], color_space_array: list, color_engine: color_space.ColorSpaceEngine) -> list[float]:
    """Convert ICCBased color to RGB, trying Tier 2 ICC then Tier 1 fallback.

    Returns:
        list of 3 floats [r, g, b]
    """
    stream_obj = color_space_array[1] if len(color_space_array) > 1 else None
    profile_hash = icc_profile.get_profile_hash(stream_obj) if stream_obj else None
    n = len(components)
    if profile_hash is not None:
        rgb = icc_profile.icc_convert_color(profile_hash, n, components)
        if rgb is not None:
            return list(rgb)
    # Tier 1 fallback
    device_space = ColorSpaceEngine.resolve_iccbased_space(color_space_array)
    if device_space == "DeviceGray":
        return list(color_engine.gray_to_rgb(components[0]))
    elif device_space == "DeviceRGB":
        return list(components[:3])
    elif device_space == "DeviceCMYK":
        return list(color_engine.cmyk_to_rgb(*components[:4]))
    return [0.0, 0.0, 0.0]


def currentgray(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentgray** num

    returns the current color rendered according to the **DeviceGray** color space (see Section 6.2.1, "RGB and Gray Conversion").

    PLRM Section 8.2, Page 531
    Stack: - → num
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentgray.__name__)

    # 2. Get current color space and color
    current_space = ctxt.gstate.color_space[0]  # Color space name
    current_color = ctxt.gstate.color

    # 3. Convert to gray using ColorSpaceEngine
    color_engine = color_space.ColorSpaceEngine()

    if current_space == "DeviceGray":
        gray_value = current_color[0]  # Already gray
    elif current_space == "DeviceRGB" or current_space in ("CIEBasedABC", "CIEBasedA", "CIEBasedDEF", "CIEBasedDEFG"):
        # CIE spaces store resolved RGB in gstate.color
        gray_value = color_engine.rgb_to_gray(current_color[0], current_color[1], current_color[2])
    elif current_space == "DeviceCMYK":
        gray_value = color_engine.cmyk_to_gray(current_color[0], current_color[1], current_color[2], current_color[3])
    elif current_space == "ICCBased":
        stream_obj = ctxt.gstate.color_space[1] if len(ctxt.gstate.color_space) > 1 else None
        profile_hash = icc_profile.get_profile_hash(stream_obj) if stream_obj else None
        if profile_hash is not None:
            rgb_values = _icc_to_rgb(current_color, ctxt.gstate.color_space, color_engine)
            gray_value = color_engine.rgb_to_gray(*rgb_values[:3])
        else:
            # Tier 1: use native conversion based on resolved device space
            device_space = ColorSpaceEngine.resolve_iccbased_space(ctxt.gstate.color_space)
            if device_space == "DeviceGray":
                gray_value = current_color[0]
            elif device_space == "DeviceRGB":
                gray_value = color_engine.rgb_to_gray(current_color[0], current_color[1], current_color[2])
            elif device_space == "DeviceCMYK":
                gray_value = color_engine.cmyk_to_gray(current_color[0], current_color[1], current_color[2], current_color[3])
            else:
                gray_value = 0.0
    else:
        # Future color spaces - fallback to gray
        gray_value = 0.0

    # 4. Push result
    ostack.append(ps.Real(gray_value))


def currentrgbcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentrgbcolor** red green blue

    returns the current color rendered according to the **DeviceRGB** color space (see Section 6.2.3, "CMYK and RGB Conversion").

    PLRM Section 8.2, Page 531
    Stack: - → red green blue
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation - need room for 3 values
    if ctxt.MaxOpStack and len(ostack) + 3 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentrgbcolor.__name__)

    # 2. Get current color space and color
    current_space = ctxt.gstate.color_space[0]  # Color space name
    current_color = ctxt.gstate.color

    # 3. Convert to RGB using ColorSpaceEngine
    color_engine = color_space.ColorSpaceEngine()

    if current_space == "DeviceRGB" or current_space in ("CIEBasedABC", "CIEBasedA", "CIEBasedDEF", "CIEBasedDEFG"):
        # CIE spaces store resolved RGB in gstate.color
        rgb_values = current_color
    elif current_space == "DeviceGray":
        gray = current_color[0]
        rgb_values = color_engine.gray_to_rgb(gray)
    elif current_space == "DeviceCMYK":
        rgb_values = color_engine.cmyk_to_rgb(current_color[0], current_color[1], current_color[2], current_color[3])
    elif current_space == "ICCBased":
        rgb_values = _icc_to_rgb(current_color, ctxt.gstate.color_space, color_engine)
    else:
        # Future color spaces - fallback to black
        rgb_values = [0.0, 0.0, 0.0]

    # 4. Push RGB components
    ostack.append(ps.Real(rgb_values[0]))  # Red
    ostack.append(ps.Real(rgb_values[1]))  # Green
    ostack.append(ps.Real(rgb_values[2]))  # Blue


def currenthsbcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currenthsbcolor** hue saturation brightness

    returns the current color rendered according to the HSB (Hue-Saturation-Brightness) color model.

    PLRM Section 8.2, Page 531
    Stack: - → hue saturation brightness
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation - need room for 3 values
    if ctxt.MaxOpStack and len(ostack) + 3 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currenthsbcolor.__name__)

    # 2. Get current color space and color
    current_space = ctxt.gstate.color_space[0]  # Color space name
    current_color = ctxt.gstate.color

    # 3. Convert to RGB first, then RGB to HSB
    color_engine = color_space.ColorSpaceEngine()

    if current_space == "DeviceRGB" or current_space in ("CIEBasedABC", "CIEBasedA", "CIEBasedDEF", "CIEBasedDEFG"):
        # CIE spaces store resolved RGB in gstate.color
        rgb_values = current_color
    elif current_space == "DeviceGray":
        gray = current_color[0]
        rgb_values = color_engine.gray_to_rgb(gray)
    elif current_space == "DeviceCMYK":
        rgb_values = color_engine.cmyk_to_rgb(current_color[0], current_color[1], current_color[2], current_color[3])
    elif current_space == "ICCBased":
        rgb_values = _icc_to_rgb(current_color, ctxt.gstate.color_space, color_engine)
    else:
        # Future color spaces - fallback to black
        rgb_values = [0.0, 0.0, 0.0]

    # 4. Convert RGB to HSB
    hsb_values = color_engine.rgb_to_hsb(rgb_values[0], rgb_values[1], rgb_values[2])

    # 5. Push HSB components
    ostack.append(ps.Real(hsb_values[0]))  # Hue
    ostack.append(ps.Real(hsb_values[1]))  # Saturation
    ostack.append(ps.Real(hsb_values[2]))  # Brightness


def currentcmykcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentcmykcolor** cyan magenta yellow black

    returns the current color rendered according to the **DeviceCMYK** color space (see Section 6.2.4, "RGB and CMYK Conversion").

    PLRM Section 8.2, Page 530
    Stack: - → cyan magenta yellow black
    **Errors**: **stackoverflow**
    """
    # 1. Stack overflow validation - need room for 4 values
    if ctxt.MaxOpStack and len(ostack) + 4 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentcmykcolor.__name__)

    # 2. Get current color space and color
    current_space = ctxt.gstate.color_space[0]  # Color space name
    current_color = ctxt.gstate.color

    # 3. Convert to CMYK using ColorSpaceEngine
    color_engine = color_space.ColorSpaceEngine()

    if current_space == "DeviceCMYK":
        cmyk_values = current_color  # Already CMYK
    elif current_space == "DeviceRGB" or current_space in ("CIEBasedABC", "CIEBasedA", "CIEBasedDEF", "CIEBasedDEFG"):
        # CIE spaces store resolved RGB in gstate.color
        cmyk_values = color_engine.rgb_to_cmyk(current_color[0], current_color[1], current_color[2])
    elif current_space == "DeviceGray":
        gray = current_color[0]
        cmyk_values = color_engine.gray_to_cmyk(gray)
    elif current_space == "ICCBased":
        stream_obj = ctxt.gstate.color_space[1] if len(ctxt.gstate.color_space) > 1 else None
        profile_hash = icc_profile.get_profile_hash(stream_obj) if stream_obj else None
        if profile_hash is not None:
            rgb_values = _icc_to_rgb(current_color, ctxt.gstate.color_space, color_engine)
            cmyk_values = list(color_engine.rgb_to_cmyk(*rgb_values[:3]))
        else:
            # Tier 1: use native conversion based on resolved device space
            device_space = ColorSpaceEngine.resolve_iccbased_space(ctxt.gstate.color_space)
            if device_space == "DeviceCMYK":
                cmyk_values = current_color
            elif device_space == "DeviceRGB":
                cmyk_values = list(color_engine.rgb_to_cmyk(current_color[0], current_color[1], current_color[2]))
            elif device_space == "DeviceGray":
                cmyk_values = list(color_engine.gray_to_cmyk(current_color[0]))
            else:
                cmyk_values = [0.0, 0.0, 0.0, 1.0]
    else:
        # Future color spaces - fallback to black
        cmyk_values = [0.0, 0.0, 0.0, 1.0]

    # 4. Push CMYK components
    ostack.append(ps.Real(cmyk_values[0]))  # Cyan
    ostack.append(ps.Real(cmyk_values[1]))  # Magenta
    ostack.append(ps.Real(cmyk_values[2]))  # Yellow
    ostack.append(ps.Real(cmyk_values[3]))  # Black


def _hsb_to_rgb(h: float, s: float, b: float) -> tuple[float, float, float]:
    """
    Convert HSB to RGB using hexcone model as specified in PLRM.

    Args:
        h: Hue (0.0-1.0) - 0=red, 1/3=green, 2/3=blue, 1=red again
        s: Saturation (0.0-1.0) - 0=gray, 1=pure color
        b: Brightness (0.0-1.0) - 0=black, 1=maximum brightness

    Returns:
        Tuple of (red, green, blue) values in range 0.0-1.0

    Reference: PLRM Section 6.2.1 and hexcone color model
    """
    # Handle special cases
    if s == 0.0:
        # Achromatic (gray) - no hue
        return (b, b, b)

    if b == 0.0:
        # Always black regardless of hue/saturation
        return (0.0, 0.0, 0.0)

    # Normalize hue to [0, 6) range for hexcone sectors
    h = h * 6.0
    if h >= 6.0:
        h = 0.0  # Wrap around

    # Determine which sector of the hexcone we're in
    sector = int(h)
    fractional = h - sector

    # Calculate intermediate values
    p = b * (1.0 - s)              # Minimum component value
    q = b * (1.0 - s * fractional)  # Decreasing component
    t = b * (1.0 - s * (1.0 - fractional))  # Increasing component

    # Assign RGB based on hexcone sector
    if sector == 0:    # Red to Yellow
        return (b, t, p)
    elif sector == 1:  # Yellow to Green
        return (q, b, p)
    elif sector == 2:  # Green to Cyan
        return (p, b, t)
    elif sector == 3:  # Cyan to Blue
        return (p, q, b)
    elif sector == 4:  # Blue to Magenta
        return (t, p, b)
    else:              # Magenta to Red (sector == 5)
        return (b, p, q)


def sethsbcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    hue saturation brightness **sethsbcolor** -

    sets the current color space in the graphics state to **DeviceRGB** and the current
    color to the color described by the parameters hue, saturation, and brightness. Each
    parameter must be a number in the range 0.0 to 1.0. If any of the operands is outside
    this range, the nearest value is substituted without error indication.

    Note that the HSB parameter values supplied to **sethsbcolor** are immediately converted
    into RGB color components. HSB is not a color space in its own right, but
    merely an alternate way of specifying color values in the **DeviceRGB** color space.

    Execution of this operator is not permitted in certain circumstances; see
    Section 4.8.1, "Types of Color Space."

    **Errors**:     **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **currenthsbcolor**, **setcolorspace**, **setcolor**, **setgray**,
                **setrgbcolor**, **setcmykcolor**
    """
    # PLRM Section 8.2, Page 576 - exact PLRM implementation

    # 1. Stack validation - must be done BEFORE popping operands
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, sethsbcolor.__name__)

    # 2. Type validation - must be done BEFORE popping operands
    if not all(ostack[i].TYPE in ps.NUMERIC_TYPES for i in range(-3, 0)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, sethsbcolor.__name__)

    # 3. ONLY after all validation passes - get and clamp HSB values
    hue = float(ostack[-3].val)
    saturation = float(ostack[-2].val)
    brightness = float(ostack[-1].val)

    # Clamp to valid range (0.0-1.0) as specified in PLRM
    hue = max(0.0, min(1.0, hue))
    saturation = max(0.0, min(1.0, saturation))
    brightness = max(0.0, min(1.0, brightness))

    # 4. CRITICAL: Convert HSB to RGB using hexcone model
    red, green, blue = _hsb_to_rgb(hue, saturation, brightness)

    # 5. Set color space to DeviceRGB and RGB components
    ctxt.gstate.color_space = ["DeviceRGB"]  # Must be array per PLRM
    ctxt.gstate.color = [red, green, blue]   # RGB components, NOT HSB!

    # 6. Pop operands only after successful completion
    ostack.pop()
    ostack.pop()
    ostack.pop()


def setrgbcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    red green blue **setrgbcolor** -

    sets the current color space in the graphics state to **DeviceRGB** and the current color
    to the component values specified by red, green, and blue. Each component
    must be a number in the range 0.0 to 1.0. If any of the operands is outside this
    range, the nearest valid value is substituted without error indication.

    Execution of this operator is not permitted in certain circumstances; see
    Section 4.8.1, "Types of Color Space."

    **Errors**:     **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **currentrgbcolor**, **setcolorspace**, **setcolor**, **setgray**,
                **sethsbcolor**, **setcmykcolor**
    """
    # PLRM Section 8.2, Page 579 - exact PLRM implementation

    # 0. Type 3 font cache mode restriction - UNDEFINED if called after setcachedevice
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setrgbcolor.__name__)

    # 1. Stack validation - must be done BEFORE popping operands
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setrgbcolor.__name__)

    # 2. Type validation - must be done BEFORE popping operands
    if not all(ostack[i].TYPE in ps.NUMERIC_TYPES for i in range(-3, 0)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, setrgbcolor.__name__)

    # 3. ONLY after all validation passes - get and clamp RGB values
    red = float(ostack[-3].val)
    green = float(ostack[-2].val)
    blue = float(ostack[-1].val)

    # Clamp to valid range (0.0-1.0) as specified in PLRM
    red = max(0.0, min(1.0, red))
    green = max(0.0, min(1.0, green))
    blue = max(0.0, min(1.0, blue))

    # 4. CRITICAL: Set color space to DeviceRGB and RGB components
    ctxt.gstate.color_space = ["DeviceRGB"]  # Must be array per PLRM
    ctxt.gstate.color = [red, green, blue]   # Three RGB components

    # 5. Pop operands only after successful completion
    ostack.pop()
    ostack.pop()
    ostack.pop()


def setcmykcolor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    cyan magenta yellow black **setcmykcolor** –

    sets the color space to **DeviceCMYK**, then sets the current color parameter in the graphics
    state to a color described by the parameters cyan, magenta, yellow, and black, each of which
    must be a number in the range 0.0 to 1.0. This establishes the color subsequently used to
    paint shapes, such as lines, areas, and characters on the current page (see section 4.8.2,
    "Device Color Spaces"). Color values set by **setcmykcolor** are not affected by the black
    generation and undercolor removal operations.

    **setcmykcolor** does not give an error for a value outside the range 0 to 1. It substitutes
    the nearest legal value.

    Execution of this operator is not permitted in certain circumstances; see section 4.8,
    "Color Spaces."

    **Errors**:     **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **setcolorspace**, **setcolor**, **currentcmykcolor**
    """
    # PLRM Section 8.2, Page 576 - exact PLRM implementation

    # 0. Type 3 font cache mode restriction - UNDEFINED if called after setcachedevice
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcmykcolor.__name__)

    # 1. Stack validation - must be done BEFORE popping operands
    if len(ostack) < 4:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcmykcolor.__name__)

    # 2. Type validation - must be done BEFORE popping operands
    if not all(ostack[i].TYPE in ps.NUMERIC_TYPES for i in range(-4, 0)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, setcmykcolor.__name__)

    # 3. ONLY after all validation passes - get and clamp CMYK values
    cyan = float(ostack[-4].val)
    magenta = float(ostack[-3].val)
    yellow = float(ostack[-2].val)
    black = float(ostack[-1].val)

    # Clamp to valid range (0.0-1.0) as specified in PLRM - no error for out of range
    cyan = max(0.0, min(1.0, cyan))
    magenta = max(0.0, min(1.0, magenta))
    yellow = max(0.0, min(1.0, yellow))
    black = max(0.0, min(1.0, black))

    # 4. CRITICAL: Set color space to DeviceCMYK and CMYK components per PLRM
    ctxt.gstate.color_space = ["DeviceCMYK"]  # Must be array per PLRM
    ctxt.gstate.color = [cyan, magenta, yellow, black]   # Four CMYK components

    # 5. Pop operands only after successful completion
    ostack.pop()
    ostack.pop()
    ostack.pop()
    ostack.pop()
