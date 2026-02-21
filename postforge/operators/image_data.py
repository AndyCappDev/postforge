# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

# Image Data Processing - ImageDataProcessor class and raw byte reading
#
# Device-independent image data source processing for PostScript compliance.
# Handles reading image data from procedures, files, and strings, plus
# colorimage multi-source interleaving and transfer function application.

import copy

from ..core import types as ps
from ..core import error as ps_error
from . import control as ps_control


class ImageDataProcessor:
    """Device-independent image data source processor for PostScript compliance"""

    @staticmethod
    def validate_data_source(data_source, ctxt, operator_name):
        """Validate PostScript data source - PLRM Section 4.10.2"""
        if data_source.TYPE in ps.ARRAY_TYPES and data_source.attrib == ps.ATTRIB_EXEC:
            # Procedure data source - LanguageLevel 1+
            if data_source.access < ps.ACCESS_EXECUTE_ONLY:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, operator_name)
            return 'procedure'
        elif data_source.TYPE == ps.T_FILE:
            # File data source (includes FilterFile) - LanguageLevel 2+
            if data_source.access < ps.ACCESS_READ_ONLY:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, operator_name)
            return 'file'
        elif data_source.TYPE == ps.T_STRING:
            # String data source - LanguageLevel 2+
            if data_source.access < ps.ACCESS_READ_ONLY:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, operator_name)
            return 'string'
        else:
            return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name)

    @staticmethod
    def validate_image_matrix(matrix_obj, ctxt, operator_name):
        """Validate 6-element transformation matrix - PLRM Section 4.10.3"""
        if matrix_obj.TYPE not in ps.ARRAY_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name)

        if matrix_obj.access < ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, operator_name)

        if len(matrix_obj.val) != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, operator_name)

        # Validate all elements are numeric
        for element in matrix_obj.val:
            if element.TYPE not in ps.NUMERIC_TYPES:
                return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name)

        # Check for singular matrix (determinant = 0)
        try:
            a, b, c, d, e, f = [elem.val for elem in matrix_obj.val]
            determinant = a * d - b * c
            if abs(determinant) < 1e-10:  # Essentially zero
                return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, operator_name)
        except (AttributeError, TypeError):
            return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name)

        return True

    @staticmethod
    def read_all_image_data(data_source, image_element, ctxt):
        """Read ALL image data immediately - no VM references in display list"""

        # Calculate total bytes needed
        # PLRM: Each row of sample data is padded to a byte boundary
        bits_per_row = (image_element.width * image_element.components *
                       image_element.bits_per_component)
        bytes_per_row = (bits_per_row + 7) // 8  # Pad each row to byte boundary
        bytes_needed = bytes_per_row * image_element.height

        try:
            if data_source.TYPE == ps.T_STRING:
                # String data source - direct byte extraction
                sample_bytes = data_source.byte_string()
                if len(sample_bytes) >= bytes_needed:
                    # Make an explicit copy to ensure data is preserved
                    image_element.sample_data = bytes(sample_bytes[:bytes_needed])
                    return True
                else:
                    return False

            elif data_source.TYPE == ps.T_FILE:
                # File data source - read all bytes now (includes FilterFile)
                # Use bulk read when available (FilterFile and real File both support it)
                if hasattr(data_source, 'read_bulk'):
                    sample_bytes = bytearray()
                    remaining = bytes_needed
                    while remaining > 0:
                        chunk = data_source.read_bulk(ctxt, min(remaining, 65536))
                        if not chunk:
                            break
                        sample_bytes.extend(chunk)
                        remaining -= len(chunk)
                else:
                    # Fallback: byte-at-a-time read
                    sample_bytes = bytearray()
                    remaining = bytes_needed
                    while remaining > 0:
                        byte_val = data_source.read(ctxt)
                        if byte_val is None:
                            break
                        sample_bytes.append(byte_val)
                        remaining -= 1

                if len(sample_bytes) >= bytes_needed:
                    image_element.sample_data = bytes(sample_bytes[:bytes_needed])
                    return True
                else:
                    return False

            elif data_source.TYPE in ps.ARRAY_TYPES and data_source.attrib == ps.ATTRIB_EXEC:
                # Procedure data source - complex execution required
                result = ImageDataProcessor._read_from_procedure(data_source, image_element, ctxt, bytes_needed)
                return result

            else:
                return False

        except Exception:
            return False

    @staticmethod
    def _read_from_procedure(procedure, image_element, ctxt, bytes_needed):
        """Read from PostScript procedure data source - with proper error handling"""
        try:
            # Execute procedure multiple times to get strings until we have enough data
            sample_bytes = bytearray()
            max_iterations = 1000000  # Prevent infinite loops

            for i in range(max_iterations):
                if len(sample_bytes) >= bytes_needed:
                    break

                # Use Stopped context to catch any errors during procedure execution
                # This prevents "Unrecoverable Error - stop" when errors occur
                # HardReturn acts as a boundary so inner exec_exec doesn't consume outer context
                ctxt.e_stack.append(ps.HardReturn())
                ctxt.e_stack.append(ps.Stopped())
                # IMPORTANT: Must copy the procedure because exec_exec modifies array's
                # start and length fields during execution. Without copy, subsequent
                # iterations would use corrupted procedure state.
                ctxt.e_stack.append(copy.copy(procedure))

                # Execute the procedure
                ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

                # Check if stop was called (True on ostack) or completed normally (False on ostack)
                # The Stopped mechanism pushes a Bool indicating whether stop was called
                if len(ctxt.o_stack) > 0 and ctxt.o_stack[-1].TYPE == ps.T_BOOL:
                    stopped_result = ctxt.o_stack.pop()
                    if stopped_result.val:
                        # stop was called - error occurred during procedure execution
                        return False

                # Check for string result from the procedure
                if len(ctxt.o_stack) > 0 and ctxt.o_stack[-1].TYPE == ps.T_STRING:
                    result_string = ctxt.o_stack.pop()
                    chunk = result_string.byte_string()
                    sample_bytes.extend(chunk)
                else:
                    # No string returned - might be end of data or error
                    break
            if len(sample_bytes) >= bytes_needed:
                image_element.sample_data = bytes(sample_bytes[:bytes_needed])
                return True
            else:
                return False

        except Exception as e:
            return False

    @staticmethod
    def _read_one_scanline(data_source, bytes_needed, ctxt):
        """Read a single scanline from a procedure data source.

        Returns the scanline data bytes, or None on error.
        """
        # Check if it's a procedure (executable array)
        is_procedure = (data_source.TYPE in ps.ARRAY_TYPES and
                       data_source.attrib == ps.ATTRIB_EXEC)

        if not is_procedure:
            # For non-procedure sources (File, String), read directly
            if data_source.TYPE == ps.T_FILE:
                result = bytearray()
                for _ in range(bytes_needed):
                    byte_val = data_source.read(ctxt)
                    if byte_val is None:
                        break
                    result.append(byte_val)
                return bytes(result) if result else None
            elif data_source.TYPE == ps.T_STRING:
                return data_source.byte_string()[:bytes_needed]
            return None

        # Procedure data source - execute once to get one scanline
        procedure = data_source

        # Use Stopped context to catch errors
        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(ps.Stopped())
        ctxt.e_stack.append(copy.copy(procedure))

        # Execute the procedure
        ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

        # Check if stop was called
        if len(ctxt.o_stack) > 0 and ctxt.o_stack[-1].TYPE == ps.T_BOOL:
            stopped_result = ctxt.o_stack.pop()
            if stopped_result.val:
                # Error occurred during procedure execution
                return None

        # Check for string result
        if len(ctxt.o_stack) > 0 and ctxt.o_stack[-1].TYPE == ps.T_STRING:
            result_string = ctxt.o_stack.pop()
            return result_string.byte_string()

        return None

    @staticmethod
    def _execute_procedure_once(procedure, ctxt):
        """Execute a procedure once and return its string result, or None."""
        try:
            ctxt.e_stack.append(ps.HardReturn())
            ctxt.e_stack.append(ps.Stopped())
            ctxt.e_stack.append(copy.copy(procedure))
            ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

            # Check if stop was called
            if len(ctxt.o_stack) > 0 and ctxt.o_stack[-1].TYPE == ps.T_BOOL:
                stopped_result = ctxt.o_stack.pop()
                if stopped_result.val:
                    return None

            # Check for string result
            if len(ctxt.o_stack) > 0 and ctxt.o_stack[-1].TYPE == ps.T_STRING:
                result_string = ctxt.o_stack.pop()
                return result_string.byte_string()

            return None
        except Exception:
            return None

    @staticmethod
    def read_all_colorimage_data(data_sources, color_element, ctxt, multi):
        """Read ALL **colorimage** data immediately - handles multiple data sources

        For multi=true with procedures: Uses round-robin reading to support procedures
        that share **currentfile** as their data source. Each call to procedure R, then G,
        then B reads the next chunk from the interleaved file data.

        For multi=true with non-procedures: Reads sequentially from each component's
        data source, then interleaves.

        For multi=false: A single data source provides interleaved component data.
        """
        try:
            if multi:
                ncomp = len(data_sources)
                width = color_element.width
                height = color_element.height
                bits = color_element.bits_per_component

                # Check if all data sources are procedures
                all_procedures = all(
                    ds.TYPE in ps.ARRAY_TYPES and ds.attrib == ps.ATTRIB_EXEC
                    for ds in data_sources
                )

                if all_procedures:
                    # Round-robin reading for procedures
                    # This is necessary because procedures may share currentfile,
                    # where data is interleaved (R-line, G-line, B-line, ...)
                    bits_per_row = width * bits
                    bytes_per_row = (bits_per_row + 7) // 8
                    bytes_per_component = bytes_per_row * height
                    component_data = [bytearray() for _ in range(ncomp)]

                    max_iterations = bytes_per_component * ncomp * 10  # Safety limit
                    iteration = 0

                    while iteration < max_iterations:
                        iteration += 1

                        # Check if all components have enough data
                        if all(len(component_data[i]) >= bytes_per_component for i in range(ncomp)):
                            break

                        # Read from each component in round-robin fashion
                        any_progress = False
                        for comp_idx, procedure in enumerate(data_sources):
                            if len(component_data[comp_idx]) >= bytes_per_component:
                                continue  # Already have enough for this component

                            # Execute procedure once
                            result = ImageDataProcessor._execute_procedure_once(procedure, ctxt)
                            if result and len(result) > 0:
                                component_data[comp_idx].extend(result)
                                any_progress = True

                        if not any_progress:
                            # No procedure returned data - all sources exhausted
                            break

                    # Check if we got enough data
                    if not all(len(component_data[i]) >= bytes_per_component for i in range(ncomp)):
                        return False

                    # Trim to exact size needed
                    component_data_list = [bytes(cd[:bytes_per_component]) for cd in component_data]

                    interleaved_data = ImageDataProcessor._interleave_component_data(
                        component_data_list, ncomp, width, height, bits
                    )
                    color_element.sample_data = interleaved_data
                    return interleaved_data is not None

                else:
                    # Non-procedure sources - read sequentially (strings, files)
                    component_data_list = []

                    for comp_idx, data_source in enumerate(data_sources):
                        temp_element = ps.ImageElement([], ctxt.gstate, 'temp')
                        temp_element.width = width
                        temp_element.height = height
                        temp_element.components = 1
                        temp_element.bits_per_component = bits

                        if not ImageDataProcessor.read_all_image_data(data_source, temp_element, ctxt):
                            return False

                        component_data_list.append(temp_element.sample_data)

                    interleaved_data = ImageDataProcessor._interleave_component_data(
                        component_data_list, ncomp, width, height, bits
                    )
                    color_element.sample_data = interleaved_data
                    return interleaved_data is not None

            else:
                # Single interleaved data source
                if len(data_sources) != 1:
                    return False

                return ImageDataProcessor.read_all_image_data(data_sources[0], color_element, ctxt)

        except Exception:
            return False

    @staticmethod
    def _interleave_component_data(component_data_list, ncomp, width, height, bits_per_component):
        """Interleave separate component data into single array"""
        try:
            if bits_per_component == 8:
                # Simple 8-bit interleaving
                interleaved = bytearray()
                samples_per_component = width * height

                for i in range(samples_per_component):
                    for comp in range(ncomp):
                        if i < len(component_data_list[comp]):
                            interleaved.append(component_data_list[comp][i])
                        else:
                            interleaved.append(0)  # Pad with zeros if needed

                return bytes(interleaved)
            elif bits_per_component == 12:
                # 12-bit interleaving - each sample is 1.5 bytes
                # Input: separate component streams, each with row padding
                # Output: interleaved stream with all components for each pixel, with row padding
                interleaved = bytearray()

                # Each component's data has its own row padding
                # For single component: bits_per_row = width * 12, padded to byte
                comp_bits_per_row = width * 12
                comp_bytes_per_row = (comp_bits_per_row + 7) // 8

                # Output has ncomp samples per pixel per row
                out_bits_per_row = width * ncomp * 12
                out_bytes_per_row = (out_bits_per_row + 7) // 8

                def get_12bit_sample_from_row(row_data, sample_in_row):
                    """Extract 12-bit sample at given index within a row."""
                    bit_pos = sample_in_row * 12
                    byte_idx = bit_pos // 8
                    bit_offset = bit_pos % 8

                    if bit_offset == 0:
                        if byte_idx + 1 < len(row_data):
                            return ((row_data[byte_idx] << 4) |
                                    (row_data[byte_idx + 1] >> 4))
                        elif byte_idx < len(row_data):
                            return row_data[byte_idx] << 4
                        return 0
                    else:  # bit_offset == 4
                        if byte_idx + 1 < len(row_data):
                            return (((row_data[byte_idx] & 0x0F) << 8) |
                                    row_data[byte_idx + 1])
                        elif byte_idx < len(row_data):
                            return (row_data[byte_idx] & 0x0F) << 8
                        return 0

                # Process row by row
                for row in range(height):
                    # Extract each component's row data
                    comp_rows = []
                    for comp in range(ncomp):
                        row_start = row * comp_bytes_per_row
                        comp_rows.append(component_data_list[comp][row_start:row_start + comp_bytes_per_row])

                    # Pack interleaved samples for this row
                    row_output = bytearray()
                    output_bit_pos = 0
                    current_byte = 0

                    for col in range(width):
                        for comp in range(ncomp):
                            sample = get_12bit_sample_from_row(comp_rows[comp], col)
                            sample = sample & 0xFFF

                            if output_bit_pos == 0:
                                row_output.append((sample >> 4) & 0xFF)
                                current_byte = (sample & 0x0F) << 4
                                output_bit_pos = 4
                            else:  # output_bit_pos == 4
                                row_output.append(current_byte | ((sample >> 8) & 0x0F))
                                row_output.append(sample & 0xFF)
                                output_bit_pos = 0
                                current_byte = 0

                    # Flush any remaining partial byte for this row
                    if output_bit_pos != 0:
                        row_output.append(current_byte)

                    # Pad row to expected output bytes
                    while len(row_output) < out_bytes_per_row:
                        row_output.append(0)

                    interleaved.extend(row_output)

                return bytes(interleaved)
            elif bits_per_component in [1, 2, 4]:
                # Sub-byte sample interleaving with row padding
                interleaved = bytearray()
                samples_per_byte = 8 // bits_per_component
                mask = (1 << bits_per_component) - 1

                # Input: each component has its own row padding
                comp_bits_per_row = width * bits_per_component
                comp_bytes_per_row = (comp_bits_per_row + 7) // 8

                # Output: interleaved row has ncomp samples per pixel
                out_bits_per_row = width * ncomp * bits_per_component
                out_bytes_per_row = (out_bits_per_row + 7) // 8

                # Helper to extract a sample from row-padded component data
                def get_sample_rowpad(comp_data, row, col):
                    sample_in_row = col
                    bit_pos = sample_in_row * bits_per_component
                    byte_idx = row * comp_bytes_per_row + bit_pos // 8
                    bit_offset_in_byte = (8 - bits_per_component) - (bit_pos % 8)
                    if byte_idx < len(comp_data):
                        return (comp_data[byte_idx] >> bit_offset_in_byte) & mask
                    return 0

                for row in range(height):
                    output_bit_pos = 8
                    current_byte = 0

                    for col in range(width):
                        for comp in range(ncomp):
                            sample = get_sample_rowpad(component_data_list[comp], row, col)
                            output_bit_pos -= bits_per_component
                            current_byte |= (sample << output_bit_pos)
                            if output_bit_pos == 0:
                                interleaved.append(current_byte)
                                current_byte = 0
                                output_bit_pos = 8

                    # Pad output row to byte boundary
                    if output_bit_pos != 8:
                        interleaved.append(current_byte)
                        current_byte = 0
                        output_bit_pos = 8

                return bytes(interleaved)
            else:
                # Unsupported bit depth
                return None

        except Exception as e:
            return None

    @staticmethod
    def apply_transfer_function(sample_data, transfer_proc, ctxt):
        """Apply standard gamma 2.2 transfer function per PLRM Section 7.3"""
        try:
            processed_data = bytearray()

            for byte_val in sample_data:
                # Normalize to 0.0-1.0 range (PLRM requirement)
                input_val = byte_val / 255.0

                # Apply gamma 2.2 correction: input^(1/2.2) = input^0.4545
                output_val = input_val ** 0.4545

                # Clamp result to 0.0-1.0 range and convert back to 0-255 (PLRM requirement)
                output_val = max(0.0, min(1.0, output_val))
                processed_byte = int(output_val * 255)
                processed_data.append(processed_byte)

            return bytes(processed_data)

        except Exception as e:
            return sample_data  # Return original data on error


def _read_raw_bytes(data_source, count, ctxt):
    """Read exactly count bytes from a data source using a temporary ImageElement."""
    temp = ps.ImageElement([0], ctxt.gstate, 'image')
    temp.width = count
    temp.height = 1
    temp.bits_per_component = 8
    temp.components = 1
    if ImageDataProcessor.read_all_image_data(data_source, temp, ctxt):
        return temp.sample_data
    return temp.sample_data if temp.sample_data and len(temp.sample_data) > 0 else None
