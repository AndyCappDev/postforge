# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Type 3 Masked Image Support
#
# Implements LanguageLevel 3 ImageType 3 (masked images) per PLRM Table 4.22.
# Handles InterleaveType 1 (sample-interleaved), InterleaveType 2 (row-interleaved),
# and InterleaveType 3 (separate data sources) masked image formats.

from ..core import types as ps
from ..core import color_space
from ..core import error as ps_error
from .image_data import ImageDataProcessor, _read_raw_bytes


def _image_type3_dict_form(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """Handle LanguageLevel 3 ImageType 3 (masked images) per PLRM Table 4.22

    The outer dictionary contains:
      ImageType=3, InterleaveType, DataDict, MaskDict
    DataDict is a Type 1 image dictionary; MaskDict is a Type 1 mask dictionary.
    """
    image_dict = ostack[-1]

    # Validate required keys for Type 3
    for key in [b'InterleaveType', b'DataDict', b'MaskDict']:
        if key not in image_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    interleave_type_obj = image_dict.val[b'InterleaveType']
    if interleave_type_obj.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    interleave_type = interleave_type_obj.val
    if interleave_type not in [1, 2, 3]:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")

    data_dict = image_dict.val[b'DataDict']
    mask_dict = image_dict.val[b'MaskDict']
    if data_dict.TYPE != ps.T_DICT or mask_dict.TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    # Extract image parameters from DataDict
    for key in [b'Width', b'Height', b'ImageMatrix', b'BitsPerComponent', b'Decode']:
        if key not in data_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    # DataSource location depends on InterleaveType
    if interleave_type in [1, 2]:
        # DataSource is in DataDict (contains interleaved mask+image data)
        if b'DataSource' not in data_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    else:  # InterleaveType 3
        # Both DataDict and MaskDict have their own DataSource
        if b'DataSource' not in data_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
        if b'DataSource' not in mask_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    # Extract mask parameters from MaskDict
    for key in [b'Width', b'Height', b'ImageMatrix', b'BitsPerComponent', b'Decode']:
        if key not in mask_dict.val:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    img_width = data_dict.val[b'Width']
    img_height = data_dict.val[b'Height']
    img_bps = data_dict.val[b'BitsPerComponent']
    img_matrix = data_dict.val[b'ImageMatrix']
    img_decode = data_dict.val[b'Decode']

    mask_width = mask_dict.val[b'Width']
    mask_height = mask_dict.val[b'Height']
    mask_bps = mask_dict.val[b'BitsPerComponent']
    mask_matrix = mask_dict.val[b'ImageMatrix']
    mask_decode = mask_dict.val[b'Decode']

    # Type validation
    if img_width.TYPE != ps.T_INT or img_height.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if img_bps.TYPE != ps.T_INT or img_matrix.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if img_decode.TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if mask_width.TYPE != ps.T_INT or mask_height.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")
    if mask_bps.TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

    # Range validation
    if img_width.val <= 0 or img_height.val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")
    if img_bps.val not in [1, 2, 4, 8, 12]:
        return ps_error.e(ctxt, ps_error.RANGECHECK, "image")

    # Matrix validation
    matrix_result = ImageDataProcessor.validate_image_matrix(img_matrix, ctxt, "image")
    if matrix_result is not True:
        return matrix_result

    # Determine component count from color space
    try:
        component_count = color_space.ColorSpaceEngine.get_component_count(ctxt.gstate.color_space)
    except (ValueError, AttributeError):
        component_count = 1

    # ALL VALIDATION PASSED - pop dictionary
    image_dict = ostack.pop()

    # Check for MultipleDataSources in DataDict
    multi_data_sources = False
    if b'MultipleDataSources' in data_dict.val:
        mds_obj = data_dict.val[b'MultipleDataSources']
        if mds_obj.TYPE == ps.T_BOOL:
            multi_data_sources = mds_obj.val

    # Create image element for the data image
    device_color = color_space.convert_to_device_color(ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
    image_element = ps.ImageElement(device_color, ctxt.gstate, 'image')
    image_element.width = img_width.val
    image_element.height = img_height.val
    image_element.bits_per_component = img_bps.val
    image_element.components = component_count
    image_element.image_matrix = [elem.val for elem in img_matrix.val]
    image_element.ctm = [elem.val for elem in ctxt.gstate.CTM.val]
    image_element.decode_array = [elem.val for elem in img_decode.val]
    image_element.color_space = ctxt.gstate.color_space.copy() if hasattr(ctxt.gstate, 'color_space') else None

    # Check for optional Interpolate parameter in DataDict
    image_element.interpolate = False
    if b'Interpolate' in data_dict.val:
        interp = data_dict.val[b'Interpolate']
        if interp.TYPE == ps.T_BOOL:
            image_element.interpolate = interp.val

    # Determine mask polarity from decode array
    # PLRM: Decoded value 0 = paint, 1 = mask out
    mask_decode_vals = [elem.val for elem in mask_decode.val]
    # polarity=True means 1-bit=paint (Decode [0 1]), False means 0-bit=paint (Decode [1 0])
    mask_polarity = (mask_decode_vals[0] == 0.0) if len(mask_decode_vals) >= 2 else True

    data_source = data_dict.val[b'DataSource']

    if interleave_type == 1:
        # Interleaved by sample: mask component precedes color components in same stream
        # Total components per pixel = 1 (mask) + color components
        total_components = 1 + component_count
        # Read all data as a single stream with extra mask component
        temp_element = ps.ImageElement(device_color, ctxt.gstate, 'image')
        temp_element.width = img_width.val
        temp_element.height = img_height.val
        temp_element.bits_per_component = img_bps.val
        temp_element.components = total_components
        if not ImageDataProcessor.read_all_image_data(data_source, temp_element, ctxt):
            return ps_error.e(ctxt, ps_error.IOERROR, "image")

        # Separate mask and image data
        _separate_interleave_type1(temp_element.sample_data, image_element,
                                   img_bps.val, img_width.val, img_height.val,
                                   component_count, mask_polarity)

    elif interleave_type == 2:
        # Interleaved by row: blocks of mask rows then image rows
        _read_interleave_type2(data_source, image_element, ctxt,
                               img_bps.val, img_width.val, img_height.val,
                               mask_width.val, mask_height.val,
                               component_count, mask_polarity)

    elif interleave_type == 3:
        # Separate data sources
        mask_data_source = mask_dict.val[b'DataSource']

        # Read image data
        if multi_data_sources:
            data_sources = list(data_source.val)
            if not ImageDataProcessor.read_all_colorimage_data(data_sources, image_element, ctxt, True):
                return ps_error.e(ctxt, ps_error.IOERROR, "image")
        else:
            if not ImageDataProcessor.read_all_image_data(data_source, image_element, ctxt):
                return ps_error.e(ctxt, ps_error.IOERROR, "image")

        # Read mask data separately
        mask_element = ps.ImageElement(device_color, ctxt.gstate, 'image')
        mask_element.width = mask_width.val
        mask_element.height = mask_height.val
        mask_element.bits_per_component = mask_bps.val
        mask_element.components = 1
        if not ImageDataProcessor.read_all_image_data(mask_data_source, mask_element, ctxt):
            return ps_error.e(ctxt, ps_error.IOERROR, "image")

        # Store mask data on image element for renderer
        image_element.stencil_mask = mask_element.sample_data
        image_element.stencil_mask_width = mask_width.val
        image_element.stencil_mask_height = mask_height.val
        image_element.stencil_mask_polarity = mask_polarity

    # Add to display list
    ctxt.display_list.append(image_element)


def _separate_interleave_type1(raw_data: bytes, image_element: ps.ImageElement, bps: int, width: int, height: int, ncomp: int, mask_polarity: bool) -> None:
    """Separate InterleaveType 1 data: mask sample precedes color samples per pixel.

    For BPS=8: straightforward byte separation.
    For sub-byte BPS: need bit-level separation with row padding.
    """
    total_comp = 1 + ncomp  # mask + color components
    bits_per_row = width * total_comp * bps
    bytes_per_row = (bits_per_row + 7) // 8

    # Output: image data and mask data, both with their own row padding
    img_bits_per_row = width * ncomp * bps
    img_bytes_per_row = (img_bits_per_row + 7) // 8
    mask_bits_per_row = width * bps  # mask uses same BPS as image for InterleaveType 1
    mask_bytes_per_row = (mask_bits_per_row + 7) // 8

    image_data = bytearray()
    mask_data = bytearray()

    if bps == 8:
        for row in range(height):
            row_start = row * bytes_per_row
            img_row = bytearray()
            mask_row = bytearray()
            for col in range(width):
                pixel_start = row_start + col * total_comp
                # First sample is mask
                mask_val = raw_data[pixel_start] if pixel_start < len(raw_data) else 0
                mask_row.append(mask_val)
                # Remaining samples are color
                for c in range(ncomp):
                    idx = pixel_start + 1 + c
                    img_row.append(raw_data[idx] if idx < len(raw_data) else 0)
            image_data.extend(img_row)
            mask_data.extend(mask_row)
    else:
        # Sub-byte or 12-bit: extract samples at bit level
        samples_per_byte = 8 // bps if bps <= 8 else 0

        def get_sample(data, row, sample_in_row):
            """Get a sample from row-padded data."""
            bit_pos = sample_in_row * bps
            byte_idx = row * bytes_per_row + bit_pos // 8
            bit_offset = bit_pos % 8
            mask = (1 << bps) - 1
            if bps <= 8:
                remaining = 8 - bit_offset - bps
                if byte_idx < len(data):
                    return (data[byte_idx] >> remaining) & mask
                return 0
            else:  # 12-bit
                if byte_idx + 1 < len(data):
                    if bit_offset == 0:
                        return ((data[byte_idx] << 4) | (data[byte_idx + 1] >> 4))
                    else:
                        return ((data[byte_idx] & 0x0F) << 8) | data[byte_idx + 1]
                return 0

        def pack_sample(output, bit_pos_in_row, sample, bps_val):
            """Pack a sample into the output at given bit position."""
            byte_idx = bit_pos_in_row // 8
            while byte_idx >= len(output):
                output.append(0)
            bit_offset = bit_pos_in_row % 8
            if bps_val <= 8:
                remaining = 8 - bit_offset - bps_val
                output[byte_idx] |= (sample << remaining)
            elif bps_val == 12:
                if bit_offset == 0:
                    output[byte_idx] = (sample >> 4) & 0xFF
                    while byte_idx + 1 >= len(output):
                        output.append(0)
                    output[byte_idx + 1] = (output[byte_idx + 1] & 0x0F) | ((sample & 0x0F) << 4)
                else:
                    output[byte_idx] = (output[byte_idx] & 0xF0) | ((sample >> 8) & 0x0F)
                    while byte_idx + 1 >= len(output):
                        output.append(0)
                    output[byte_idx + 1] = sample & 0xFF

        for row in range(height):
            img_row = bytearray()
            mask_row = bytearray()
            for col in range(width):
                sample_base = col * total_comp
                # Mask sample
                m_val = get_sample(raw_data, row, sample_base)
                pack_sample(mask_row, col * bps, m_val, bps)
                # Color samples
                for c in range(ncomp):
                    c_val = get_sample(raw_data, row, sample_base + 1 + c)
                    pack_sample(img_row, (col * ncomp + c) * bps, c_val, bps)
            # Pad rows to byte boundary
            while len(img_row) < img_bytes_per_row:
                img_row.append(0)
            while len(mask_row) < mask_bytes_per_row:
                mask_row.append(0)
            image_data.extend(img_row[:img_bytes_per_row])
            mask_data.extend(mask_row[:mask_bytes_per_row])

    image_element.sample_data = bytes(image_data)

    # Convert mask: for InterleaveType 1, mask BPS matches image BPS
    # All bits must be same value; treat any nonzero as "paint" (1)
    # Build a 1-bit mask from the BPS-wide mask samples
    final_mask = bytearray()
    one_bit_per_row = (width + 7) // 8
    if bps == 8:
        for row in range(height):
            row_byte = 0
            for col in range(width):
                idx = row * width + col
                val = mask_data[idx] if idx < len(mask_data) else 0
                # Nonzero = paint (mask bit = 1)
                if val != 0:
                    row_byte |= (1 << (7 - (col % 8)))
                if col % 8 == 7 or col == width - 1:
                    final_mask.append(row_byte)
                    row_byte = 0
    else:
        # For sub-byte BPS, extract each sample and convert to 1-bit
        smask = (1 << bps) - 1
        for row in range(height):
            row_byte = 0
            for col in range(width):
                bit_pos = col * bps
                byte_idx = row * mask_bytes_per_row + bit_pos // 8
                bit_off = bit_pos % 8
                if bps <= 8:
                    remaining = 8 - bit_off - bps
                    val = ((mask_data[byte_idx] >> remaining) & smask) if byte_idx < len(mask_data) else 0
                elif bps == 12:
                    if bit_off == 0:
                        if byte_idx + 1 < len(mask_data):
                            val = ((mask_data[byte_idx] << 4) | (mask_data[byte_idx + 1] >> 4))
                        elif byte_idx < len(mask_data):
                            val = mask_data[byte_idx] << 4
                        else:
                            val = 0
                    else:  # bit_off == 4
                        if byte_idx + 1 < len(mask_data):
                            val = ((mask_data[byte_idx] & 0x0F) << 8) | mask_data[byte_idx + 1]
                        elif byte_idx < len(mask_data):
                            val = (mask_data[byte_idx] & 0x0F) << 8
                        else:
                            val = 0
                else:
                    val = 0
                if val != 0:
                    row_byte |= (1 << (7 - (col % 8)))
                if col % 8 == 7 or col == width - 1:
                    final_mask.append(row_byte)
                    row_byte = 0

    image_element.stencil_mask = bytes(final_mask)
    image_element.stencil_mask_width = width
    image_element.stencil_mask_height = height
    image_element.stencil_mask_polarity = mask_polarity


def _read_interleave_type2(data_source: ps.PSObject, image_element: ps.ImageElement, ctxt: ps.Context, img_bps: int, img_width: int, img_height: int,
                           mask_width: int, mask_height: int, ncomp: int, mask_polarity: bool) -> None:
    """Read InterleaveType 2: rows of mask data followed by rows of image data in blocks.

    One height must be an integral multiple of the other.
    Within each block, all mask rows precede all image rows.
    Mask is always 1 bit per sample.
    """
    # Calculate row sizes
    mask_bits_per_row = mask_width * 1  # 1 bps for mask
    mask_bytes_per_row = (mask_bits_per_row + 7) // 8
    img_bits_per_row = img_width * ncomp * img_bps
    img_bytes_per_row = (img_bits_per_row + 7) // 8

    # Determine block structure
    if img_height >= mask_height:
        ratio = img_height // mask_height
        mask_rows_per_block = 1
        img_rows_per_block = ratio
        num_blocks = mask_height
    else:
        ratio = mask_height // img_height
        mask_rows_per_block = ratio
        img_rows_per_block = 1
        num_blocks = img_height

    block_bytes = mask_rows_per_block * mask_bytes_per_row + img_rows_per_block * img_bytes_per_row
    total_bytes = block_bytes * num_blocks

    # Read all interleaved data as a single stream
    raw_data = _read_raw_bytes(data_source, total_bytes, ctxt)
    if raw_data is None:
        return

    # Separate mask and image data
    mask_data = bytearray()
    img_data = bytearray()
    offset = 0
    for _ in range(num_blocks):
        for _ in range(mask_rows_per_block):
            mask_data.extend(raw_data[offset:offset + mask_bytes_per_row])
            offset += mask_bytes_per_row
        for _ in range(img_rows_per_block):
            img_data.extend(raw_data[offset:offset + img_bytes_per_row])
            offset += img_bytes_per_row

    image_element.sample_data = bytes(img_data)
    image_element.stencil_mask = bytes(mask_data)
    image_element.stencil_mask_width = mask_width
    image_element.stencil_mask_height = mask_height
    image_element.stencil_mask_polarity = mask_polarity
