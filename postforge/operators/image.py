# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

# PostScript Image Processing Operators Implementation
#
# Implements PostScript Language Reference Manual Section 4.10 image processing
# operators: image, imagemask, colorimage following PLRM specifications

from ..core import types as ps
from ..core import color_space
from ..core import error as ps_error
from .image_data import ImageDataProcessor
from .image_type3 import _image_type3_dict_form


def _compose_matrices_for_device_space(image_matrix, ctm, scale_x, scale_y):
    """Compose matrices in correct PostScript order: DPI_scale . CTM . Image_matrix"""

    # Extract matrix components
    # Image matrix: [a b c d tx ty]
    a1, b1, c1, d1, tx1, ty1 = image_matrix

    # CTM: [a b c d tx ty] - extract values from PostScript Array
    a2, b2, c2, d2, tx2, ty2 = [elem.val if hasattr(elem, 'val') else elem for elem in ctm.val]

    # DPI scale matrix: [scale_x 0 0 scale_y 0 0]
    a3, b3, c3, d3, tx3, ty3 = scale_x, 0, 0, scale_y, 0, 0

    # Compose matrices: M = M3 . M2 . M1 (DPI_scale . CTM . Image_matrix)
    # First: M2 . M1 (CTM . Image_matrix)
    temp_a = a2 * a1 + b2 * c1
    temp_b = a2 * b1 + b2 * d1
    temp_c = c2 * a1 + d2 * c1
    temp_d = c2 * b1 + d2 * d1
    temp_tx = a2 * tx1 + b2 * ty1 + tx2
    temp_ty = c2 * tx1 + d2 * ty1 + ty2

    # Then: M3 . (M2 . M1) (DPI_scale . (CTM . Image_matrix))
    final_a = a3 * temp_a + b3 * temp_c
    final_b = a3 * temp_b + b3 * temp_d
    final_c = c3 * temp_a + d3 * temp_c
    final_d = c3 * temp_b + d3 * temp_d
    final_tx = a3 * temp_tx + b3 * temp_ty + tx3
    final_ty = c3 * temp_tx + d3 * temp_ty + ty3

    return [final_a, final_b, final_c, final_d, final_tx, final_ty]


def ps_image(ctxt, ostack):
    """
    paints a sampled **image** onto the current page

    PLRM Section 8.2, Page 608
    Stack: width height bps matrix datasrc **image** -> -
           dict **image** -> - (LanguageLevel 2)
    **Errors**: **invalidaccess**, **ioerror**, **limitcheck**, **rangecheck**,
                          stackunderflow, typecheck, undefined, undefinedresult
    """
    # 0. Type 3 font cache mode restriction - UNDEFINED if called after setcachedevice
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, "image")

    # STEP 1: Determine form (5-operand vs 1-operand dictionary)
    if len(ostack) >= 1 and ostack[-1].TYPE == ps.T_DICT:
        return _image_dict_form(ctxt, ostack)
    else:
        return _image_five_operand_form(ctxt, ostack)


def _image_five_operand_form(ctxt, ostack):
    """Handle 5-operand form: width height bps matrix datasrc image"""

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 5:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "image")

    # STEP 2: Peek at operands to validate without modifying stack
    # Stack layout: [..., width, height, bits_per_sample, matrix, data_source]
    data_source = ostack[-1]    # Top of stack
    matrix = ostack[-2]         # Second from top
    bits_per_sample = ostack[-3] # Third from top
    height = ostack[-4]         # Fourth from top
    width = ostack[-5]          # Fifth from top

    # STEP 3: Validate operand types and access
    if width.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if height.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if bits_per_sample.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if matrix.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    # STEP 4: Validate parameter ranges - PLRM Section 4.10.2
    if width.val <= 0 or height.val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")

    if bits_per_sample.val not in [1, 2, 4, 8, 12]:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")

    # STEP 5: Validate image matrix
    matrix_result = ImageDataProcessor.validate_image_matrix(matrix, ctxt, "image")
    if matrix_result is not True:
        return matrix_result

    # STEP 6: Validate and classify data source
    data_source_type = ImageDataProcessor.validate_data_source(data_source, ctxt, "image")
    if isinstance(data_source_type, int):  # Error code returned
        return data_source_type

    # STEP 7: ALL VALIDATION PASSED - Now pop operands in reverse order (PostScript stack is LIFO)
    data_source = ostack.pop()
    matrix = ostack.pop()
    bits_per_sample = ostack.pop()
    height = ostack.pop()
    width = ostack.pop()

    # STEP 8: Create image element with device-converted color
    device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
    image_element = ps.ImageElement(device_color, ctxt.gstate, 'image')
    image_element.width = width.val
    image_element.height = height.val
    image_element.bits_per_component = bits_per_sample.val
    image_element.components = 1  # Single component for basic image
    image_element.decode_array = [0.0, 1.0]  # Default decode for single component
    image_element.interpolate = False  # Default interpolation setting

    # Capture current color space from graphics state for device rendering
    image_element.color_space = ctxt.gstate.color_space.copy() if hasattr(ctxt.gstate, 'color_space') else None

    user_matrix = [elem.val for elem in matrix.val]
    the_ctm = [elem.val for elem in ctxt.gstate.CTM.val]
    the_ictm = [elem.val for elem in ctxt.gstate.iCTM.val]

    image_element.image_matrix = user_matrix
    image_element.ctm = the_ctm
    image_element.ictm = the_ictm

    # STEP 9: Read ALL image data immediately - no VM references in display list
    if not ImageDataProcessor.read_all_image_data(data_source, image_element, ctxt):
        return ps_error.e(ctxt, ps_error.IOERROR, "image")

    # STEP 10: Add to display list for device rendering (VM-independent)
    ctxt.display_list.append(image_element)


def _image_dict_form(ctxt, ostack):
    """Handle LanguageLevel 2 dictionary form of image operator"""

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "image")
    # 2. TYPECHECK - Check dictionary type
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, "image")

    # Peek at image dictionary without popping
    image_dict = ostack[-1]

    # Check ImageType first to dispatch to appropriate handler
    if b'ImageType' not in image_dict.val:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if image_dict.val[b'ImageType'].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    image_type = image_dict.val[b'ImageType'].val

    # Dispatch ImageType 3 (masked images - LanguageLevel 3) to separate handler
    if image_type == 3:
        return _image_type3_dict_form(ctxt, ostack)

    if image_type not in [1, 4]:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")

    # Extract required dictionary entries per PLRM Table 4.20
    required_keys = [b'ImageType', b'Width', b'Height', b'ImageMatrix',
                    b'DataSource', b'BitsPerComponent', b'Decode']

    # Validate dictionary structure
    dict_entries = {}
    for key in required_keys:
        if key not in image_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
        dict_entries[key] = image_dict.val[key]

    # For ImageType 4, MaskColor is required
    mask_color = None
    if image_type == 4:
        if b'MaskColor' not in image_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
        mask_color_obj = image_dict.val[b'MaskColor']
        if mask_color_obj.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
        # MaskColor must contain integers
        for elem in mask_color_obj.val:
            if elem.TYPE != ps.T_INT:
                return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
        mask_color = [elem.val for elem in mask_color_obj.val]

    # Extract and validate parameters
    width = dict_entries[b'Width']
    height = dict_entries[b'Height']
    bps = dict_entries[b'BitsPerComponent']
    matrix = dict_entries[b'ImageMatrix']
    data_source = dict_entries[b'DataSource']
    decode = dict_entries[b'Decode']

    # Type validation
    if width.TYPE != ps.T_INT or height.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if bps.TYPE != ps.T_INT or matrix.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if decode.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    # Range validation
    if width.val <= 0 or height.val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")
    if bps.val not in [1, 2, 4, 8, 12]:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")

    # Matrix validation
    matrix_result = ImageDataProcessor.validate_image_matrix(matrix, ctxt, "image")
    if matrix_result is not True:
        return matrix_result

    # Check for optional MultipleDataSources parameter BEFORE validating DataSource
    multi_data_sources = False
    if b'MultipleDataSources' in image_dict.val:
        multi_obj = image_dict.val[b'MultipleDataSources']
        if multi_obj.TYPE == ps.T_BOOL:
            multi_data_sources = multi_obj.val

    # Data source validation - depends on MultipleDataSources
    if multi_data_sources:
        # Multiple data sources - DataSource must be a literal array of sources
        if data_source.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
        # Validate each individual data source
        for ds in data_source.val:
            ds_type = ImageDataProcessor.validate_data_source(ds, ctxt, "image")
            if isinstance(ds_type, int):  # Error code returned
                return ds_type
    else:
        # Single data source
        data_source_type = ImageDataProcessor.validate_data_source(data_source, ctxt, "image")
        if isinstance(data_source_type, int):  # Error code returned
            return data_source_type

    # Decode array validation - length must be 2 x color components (PLRM Section 4.10.4)
    try:
        expected_components = color_space.ColorSpaceEngine.get_component_count(ctxt.gstate.color_space)
        expected_decode_length = 2 * expected_components
        if len(decode.val) != expected_decode_length:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "image")
    except (ValueError, AttributeError):
        # Fallback: assume DeviceGray if color space determination fails
        if len(decode.val) != 2:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "image")
    for elem in decode.val:
        if elem.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    # ALL VALIDATION PASSED - Now pop the dictionary
    image_dict = ostack.pop()

    # Create image element with device-converted color
    device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
    image_element = ps.ImageElement(device_color, ctxt.gstate, 'image')
    image_element.width = width.val
    image_element.height = height.val
    image_element.bits_per_component = bps.val
    # Set components based on current color space (PLRM Section 4.10)
    try:
        component_count = color_space.ColorSpaceEngine.get_component_count(ctxt.gstate.color_space)
        image_element.components = component_count
    except (ValueError, AttributeError):
        # Fallback: assume DeviceGray if color space determination fails
        image_element.components = 1
    image_element.image_matrix = [elem.val for elem in matrix.val]
    image_element.ctm = [elem.val for elem in ctxt.gstate.CTM.val]
    image_element.decode_array = [elem.val for elem in decode.val]
    image_element.interpolate = False  # Default for LanguageLevel 2

    # Capture current color space from graphics state for device rendering
    image_element.color_space = ctxt.gstate.color_space.copy() if hasattr(ctxt.gstate, 'color_space') else None

    # Store mask color for ImageType 4 (color key masking)
    image_element.mask_color = mask_color  # None for Type 1, list of ints for Type 4

    # Check for optional Interpolate parameter
    if b'Interpolate' in image_dict.val:
        interp = image_dict.val[b'Interpolate']
        if interp.TYPE == ps.T_BOOL:
            image_element.interpolate = interp.val

    # Read ALL image data immediately - no VM references in display list
    # multi_data_sources was already set during validation phase
    if multi_data_sources:
        # Multiple data sources - DataSource is an array of sources (already validated)
        data_sources = list(data_source.val)
        if not ImageDataProcessor.read_all_colorimage_data(data_sources, image_element, ctxt, True):
            return ps_error.e(ctxt, ps_error.IOERROR, "image")
    else:
        # Single data source
        if not ImageDataProcessor.read_all_image_data(data_source, image_element, ctxt):
            return ps_error.e(ctxt, ps_error.IOERROR, "image")

    # Add to display list (VM-independent)
    ctxt.display_list.append(image_element)


def ps_imagemask(ctxt, ostack):
    """
    bool width height polarity matrix datasrc **imagemask** –
    dict **imagemask** –

    uses a monochrome sampled image as a stencil mask of 1-bit samples to control
    where to apply paint to the current page in the current color

    PLRM Section 8.2, Page 609
           dict **imagemask** -> - (LanguageLevel 2)
    **Errors**: **invalidaccess**, **ioerror**, **limitcheck**, **rangecheck**,
                          stackunderflow, typecheck, undefined, undefinedresult
    """

    # STEP 1: Determine form (5-operand vs 1-operand dictionary)
    if len(ostack) >= 1 and ostack[-1].TYPE == ps.T_DICT:
        return _imagemask_dict_form(ctxt, ostack)
    else:
        return _imagemask_five_operand_form(ctxt, ostack)


def _imagemask_five_operand_form(ctxt, ostack):
    """Handle 5-operand form: width height polarity matrix datasrc **imagemask**"""

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 5:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "imagemask")

    # STEP 2: Peek at operands to validate without modifying stack
    # Stack layout: [..., width, height, polarity, matrix, data_source]
    data_source = ostack[-1]    # Top of stack
    matrix = ostack[-2]         # Second from top
    polarity = ostack[-3]       # Third from top
    height = ostack[-4]         # Fourth from top
    width = ostack[-5]          # Fifth from top

    # STEP 3: Validate operand types
    if width.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")
    if height.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")
    if polarity.TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")
    if matrix.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")

    # STEP 4: Validate parameter ranges
    if width.val <= 0 or height.val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "imagemask")

    # STEP 5: Validate image matrix
    matrix_result = ImageDataProcessor.validate_image_matrix(matrix, ctxt, "imagemask")
    if matrix_result is not True:
        return matrix_result

    # STEP 6: Validate data source
    data_source_type = ImageDataProcessor.validate_data_source(data_source, ctxt, "imagemask")
    if isinstance(data_source_type, int):  # Error code returned
        return data_source_type

    # STEP 7: ALL VALIDATION PASSED - Now pop operands in reverse order
    data_source = ostack.pop()
    matrix = ostack.pop()
    polarity = ostack.pop()
    height = ostack.pop()
    width = ostack.pop()

    # STEP 8: Create imagemask element with device-converted color
    device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
    mask_element = ps.ImageMaskElement(device_color, ctxt.gstate)
    mask_element.width = width.val
    mask_element.height = height.val
    mask_element.polarity = polarity.val
    # Per PLRM: polarity=true corresponds to Decode [1 0], polarity=false to [0 1]
    mask_element.decode_array = [1, 0] if mask_element.polarity else [0, 1]
    mask_element.interpolate = False  # Default interpolation
    # Imagemask is always 1-bit, 1 component
    mask_element.bits_per_component = 1
    mask_element.components = 1

    # Use same approach as image operator - store raw matrices
    user_matrix = [elem.val for elem in matrix.val]
    the_ctm = [elem.val for elem in ctxt.gstate.CTM.val]
    the_ictm = [elem.val for elem in ctxt.gstate.iCTM.val]

    mask_element.image_matrix = user_matrix
    mask_element.ctm = the_ctm
    mask_element.ictm = the_ictm

    # STEP 8: Read ALL mask data immediately - no VM references in display list
    if not ImageDataProcessor.read_all_image_data(data_source, mask_element, ctxt):
        ps_error.e(ctxt, ps_error.IOERROR, "imagemask")
        return

    # Add to display list (VM-independent)
    ctxt.display_list.append(mask_element)


def _imagemask_dict_form(ctxt, ostack):
    """Handle LanguageLevel 2 dictionary form of **imagemask** operator"""

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "imagemask")
    # 2. TYPECHECK - Check dictionary type
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, "imagemask")

    # Peek at imagemask dictionary without popping
    image_dict = ostack[-1]

    # Extract required dictionary entries for imagemask
    required_keys = [b'ImageType', b'Width', b'Height', b'ImageMatrix',
                    b'DataSource', b'Decode']

    # Validate dictionary structure
    dict_entries = {}
    for key in required_keys:
        if key not in image_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")
        dict_entries[key] = image_dict.val[key]

    # Validate ImageType = 1
    if dict_entries[b'ImageType'].TYPE != ps.T_INT or dict_entries[b'ImageType'].val != 1:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "imagemask")

    # Extract parameters
    width = dict_entries[b'Width']
    height = dict_entries[b'Height']
    matrix = dict_entries[b'ImageMatrix']
    data_source = dict_entries[b'DataSource']
    decode = dict_entries[b'Decode']

    # Validate types and ranges
    if width.TYPE != ps.T_INT or height.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")
    if matrix.TYPE not in ps.ARRAY_TYPES or decode.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")

    if width.val <= 0 or height.val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "imagemask")

    # Matrix validation
    matrix_result = ImageDataProcessor.validate_image_matrix(matrix, ctxt, "imagemask")
    if matrix_result is not True:
        return matrix_result

    # Data source validation
    data_source_type = ImageDataProcessor.validate_data_source(data_source, ctxt, "imagemask")
    if isinstance(data_source_type, int):
        return data_source_type

    # ALL VALIDATION PASSED - Now pop the dictionary
    image_dict = ostack.pop()

    # Decode validation (must be 2 elements for imagemask)
    if len(decode.val) != 2:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "imagemask")
    for elem in decode.val:
        if elem.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "imagemask")

    # Determine polarity from decode array (per PLRM)
    # Decode [1 0] = polarity=true (1=paint), [0 1] = polarity=false (0=paint)
    decode_vals = [elem.val for elem in decode.val]
    polarity = decode_vals == [1, 0]  # True if [1 0], False if [0 1]

    # Create imagemask element with device-converted color
    device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
    mask_element = ps.ImageMaskElement(device_color, ctxt.gstate)
    mask_element.width = width.val
    mask_element.height = height.val
    mask_element.polarity = polarity
    mask_element.image_matrix = [elem.val for elem in matrix.val]
    mask_element.ctm = [elem.val for elem in ctxt.gstate.CTM.val]
    mask_element.decode_array = decode_vals
    mask_element.interpolate = False
    # Imagemask is always 1-bit, 1 component
    mask_element.bits_per_component = 1
    mask_element.components = 1

    # Check for optional Interpolate parameter
    if b'Interpolate' in image_dict.val:
        interp = image_dict.val[b'Interpolate']
        if interp.TYPE == ps.T_BOOL:
            mask_element.interpolate = interp.val

    # Read ALL mask data immediately - no VM references in display list
    if not ImageDataProcessor.read_all_image_data(data_source, mask_element, ctxt):
        return ps_error.e(ctxt, ps_error.IOERROR, "imagemask")

    # Add to display list (VM-independent)
    ctxt.display_list.append(mask_element)


def ps_colorimage(ctxt, ostack):
    """
    width height bits/comp matrix datasrc0 … datasrcncomp−1 multi ncomp **colorimage** –

    paints a sampled color image onto the current page

    PLRM Section 8.2, Page 544
    **Errors**: **invalidaccess**, **ioerror**, **limitcheck**, **rangecheck**,
                          stackunderflow, typecheck, undefined, undefinedresult
    """
    # 0. Type 3 font cache mode restriction - UNDEFINED if called after setcachedevice
    if getattr(ctxt, '_font_cache_mode', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, "colorimage")

    # 1. STACKUNDERFLOW - Check minimum stack depth for ncomp and multi
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "colorimage")

    # STEP 2: Peek and validate ncomp parameter (top of stack)
    ncomp_obj = ostack[-1]  # Top of stack
    if ncomp_obj.TYPE != ps.T_INT or ncomp_obj.val not in [1, 3, 4]:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "colorimage")

    ncomp = ncomp_obj.val

    # STEP 3: Peek and validate multi parameter (second from top)
    multi_obj = ostack[-2]  # Second from top
    if multi_obj.TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "colorimage")

    multi = multi_obj.val

    # STEP 4: Calculate required stack depth and validate
    required_depth = 6  # width, height, bits/comp, matrix, multi, ncomp
    if multi:
        required_depth += ncomp  # Multiple data sources
    else:
        required_depth += 1      # Single data source

    # Additional STACKUNDERFLOW - Check full required depth
    if len(ostack) < required_depth:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "colorimage")

    # STEP 5: Validate all remaining parameters before popping any
    # Calculate negative indices (stack positions from top)
    # Stack layout: [..., width, height, bits/comp, matrix, datasrc(s), multi, ncomp]
    data_source_start = -3  # Start after multi (-2) and ncomp (-1)
    matrix_index = data_source_start - (ncomp if multi else 1)
    bits_per_comp_index = matrix_index - 1
    height_index = bits_per_comp_index - 1
    width_index = height_index - 1

    # Peek and validate data sources
    data_sources = []
    if multi:
        # Multiple separate data sources (one per component)
        for i in range(ncomp):
            data_source = ostack[data_source_start - i]
            data_source_type = ImageDataProcessor.validate_data_source(data_source, ctxt, "colorimage")
            if isinstance(data_source_type, int):  # Error returned
                return data_source_type
            data_sources.append(data_source)
    else:
        # Single interleaved data source
        data_source = ostack[data_source_start]
        data_source_type = ImageDataProcessor.validate_data_source(data_source, ctxt, "colorimage")
        if isinstance(data_source_type, int):  # Error returned
            return data_source_type
        data_sources = [data_source]

    # Peek and validate remaining parameters
    matrix = ostack[matrix_index]
    bits_per_comp = ostack[bits_per_comp_index]
    height = ostack[height_index]
    width = ostack[width_index]

    # STEP 6: Validate remaining parameter types
    if width.TYPE != ps.T_INT or height.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "colorimage")
    if bits_per_comp.TYPE != ps.T_INT or matrix.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "colorimage")

    # STEP 7: Validate parameter ranges
    if width.val <= 0 or height.val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "colorimage")
    if bits_per_comp.val not in [1, 2, 4, 8, 12]:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "colorimage")

    # STEP 8: Validate image matrix
    matrix_result = ImageDataProcessor.validate_image_matrix(matrix, ctxt, "colorimage")
    if matrix_result is not True:
        return matrix_result

    # STEP 9: ALL VALIDATION PASSED - Now pop operands in correct order
    ncomp_obj = ostack.pop()  # ncomp (top)
    multi_obj = ostack.pop()  # multi (second)

    # Extract values from popped objects
    ncomp = ncomp_obj.val
    multi = multi_obj.val

    # Pop data sources
    if multi:
        # Multiple separate data sources (reverse order due to stack)
        temp_sources = []
        for i in range(ncomp):
            temp_sources.append(ostack.pop())
        data_sources = list(reversed(temp_sources))  # Restore correct order
    else:
        # Single interleaved data source
        data_sources = [ostack.pop()]

    # Pop remaining parameters (matrix, bits/comp, height, width)
    matrix = ostack.pop()
    bits_per_comp = ostack.pop()
    height = ostack.pop()
    width = ostack.pop()

    # STEP 10: Create color image element with device-converted color
    device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
    color_element = ps.ColorImageElement(device_color, ctxt.gstate if ctxt.gstate else None, ncomp)
    color_element.width = width.val
    color_element.height = height.val
    color_element.bits_per_component = bits_per_comp.val
    color_element.multi_data_sources = multi
    color_element.data_source = data_sources
    color_element.interpolate = False  # Default interpolation

    # Set decode array based on component count
    color_element.decode_array = [0.0, 1.0] * ncomp  # Default linear decode

    # Use same approach as image operator - store raw matrices
    user_matrix = [elem.val for elem in matrix.val]
    the_ctm = [elem.val for elem in ctxt.gstate.CTM.val]
    the_ictm = [elem.val for elem in ctxt.gstate.iCTM.val]

    color_element.image_matrix = user_matrix
    color_element.ctm = the_ctm
    color_element.ictm = the_ictm

    # STEP 11: Read ALL color image data immediately - no VM references in display list
    if not ImageDataProcessor.read_all_colorimage_data(data_sources, color_element, ctxt, multi):
        return ps_error.e(ctxt, ps_error.IOERROR, "colorimage")

    # STEP 12: Add to display list (VM-independent)
    ctxt.display_list.append(color_element)
