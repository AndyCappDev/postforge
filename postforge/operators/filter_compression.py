# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Lossless Compression Filters
#
# Implements RunLength, LZW, and Flate encode/decode filters per PLRM Section 3.13.

import zlib
from typing import TYPE_CHECKING

from ..core import types as ps
from .filter import FilterBase

if TYPE_CHECKING:
    from .filter import DataSource


class RunLengthDecodeFilter(FilterBase):
    """RunLengthDecode filter - PLRM compliant run-length decompression"""

    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        super().__init__(data_source, params)
        self.decode_state = 'length'  # 'length' or 'data'
        self.run_length = 0
        self.run_byte = 0
        self.remaining_copies = 0
        self.literal_remaining = 0

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Decode run-length data - PLRM Section 3.13"""
        if self.eof_reached:
            return b''

        result = bytearray()
        target_bytes = max_bytes or 1024
        while len(result) < target_bytes and not self.eof_reached:
            if self.decode_state == 'length':
                # Read run length byte
                length_data = self.data_source.read_data(ctxt, 1)
                if not length_data:
                    self.eof_reached = True
                    break

                length_byte = length_data[0]

                # PLRM: Length 128 indicates EOD
                if length_byte == 128:
                    self.eof_reached = True
                    break

                # PLRM: Length 0-127 = literal copy (length+1) bytes
                elif length_byte <= 127:
                    self.literal_remaining = length_byte + 1
                    self.decode_state = 'literal'

                # PLRM: Length 129-255 = replicate next byte (257-length) times
                else:  # length_byte >= 129
                    self.remaining_copies = 257 - length_byte
                    self.decode_state = 'replicate_read'

            elif self.decode_state == 'literal':
                # Copy literal bytes
                bytes_to_copy = min(self.literal_remaining, target_bytes - len(result))
                literal_data = self.data_source.read_data(ctxt, bytes_to_copy)
                if not literal_data:
                    self.eof_reached = True
                    break

                result.extend(literal_data)
                self.literal_remaining -= len(literal_data)

                if self.literal_remaining == 0:
                    self.decode_state = 'length'

            elif self.decode_state == 'replicate_read':
                # Read the byte to replicate
                byte_data = self.data_source.read_data(ctxt, 1)
                if not byte_data:
                    self.eof_reached = True
                    break

                self.run_byte = byte_data[0]
                self.decode_state = 'replicate_write'

            elif self.decode_state == 'replicate_write':
                # Write replicated bytes
                bytes_to_write = min(self.remaining_copies, target_bytes - len(result))
                result.extend([self.run_byte] * bytes_to_write)
                self.remaining_copies -= bytes_to_write

                if self.remaining_copies == 0:
                    self.decode_state = 'length'

        return bytes(result)


class RunLengthEncodeFilter(FilterBase):
    """RunLengthEncode filter - PLRM compliant run-length compression"""

    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        super().__init__(data_source, params)

        # Extract RecordSize parameter
        self.record_size = 0  # 0 = no record boundaries
        if params and isinstance(params, dict):
            if b'RecordSize' in params:
                recordsize_obj = params[b'RecordSize']
                if hasattr(recordsize_obj, 'val'):
                    self.record_size = recordsize_obj.val
                else:
                    self.record_size = int(recordsize_obj)

        self.input_buffer = bytearray()
        self.record_position = 0

    def write_data(self, ctxt: ps.Context, data: bytes) -> None:
        """Encode data using run-length compression - PLRM algorithm"""
        self.input_buffer.extend(data)

        # Process complete records or all data if no record boundaries
        while self._can_process_data():
            encoded_data = self._encode_next_segment(ctxt)
            if encoded_data:
                self._write_encoded_data(ctxt, encoded_data)

    def _can_process_data(self) -> bool:
        """Check if we have data ready to process"""
        if self.record_size == 0:
            return len(self.input_buffer) > 0
        else:
            # Process when we have a complete record or at end of data
            return (self.record_position + len(self.input_buffer) >= self.record_size)

    def _encode_next_segment(self, ctxt: ps.Context) -> bytes:
        """Encode the next segment of data"""
        if not self.input_buffer:
            return b''

        encoded = bytearray()
        i = 0

        while i < len(self.input_buffer):
            # Check for run (repeated bytes)
            run_length = 1
            current_byte = self.input_buffer[i]

            # Count consecutive identical bytes (max 128, respect record boundaries)
            max_run = min(128, len(self.input_buffer) - i)
            if self.record_size > 0:
                bytes_to_record_end = self.record_size - self.record_position
                max_run = min(max_run, bytes_to_record_end - (i % self.record_size))

            while (run_length < max_run and
                   i + run_length < len(self.input_buffer) and
                   self.input_buffer[i + run_length] == current_byte):
                run_length += 1

            if run_length >= 3:  # Use run-length encoding for 3+ identical bytes
                # PLRM: Length 129-255 = replicate (257-length) times
                length_byte = 257 - run_length
                encoded.extend([length_byte, current_byte])
                i += run_length
            else:
                # Collect literal bytes (non-repeating sequence)
                literal_start = i
                literal_count = 0

                # Collect up to 128 literal bytes, respect record boundaries
                max_literal = min(128, len(self.input_buffer) - i)
                if self.record_size > 0:
                    bytes_to_record_end = self.record_size - self.record_position
                    max_literal = min(max_literal, bytes_to_record_end - (i % self.record_size))

                while (literal_count < max_literal and
                       i + literal_count < len(self.input_buffer)):
                    # Check if we're starting a run of 3+ identical bytes
                    if (i + literal_count + 2 < len(self.input_buffer) and
                        self.input_buffer[i + literal_count] ==
                        self.input_buffer[i + literal_count + 1] ==
                        self.input_buffer[i + literal_count + 2]):
                        break
                    literal_count += 1

                if literal_count > 0:
                    # PLRM: Length 0-127 = copy (length+1) literal bytes
                    length_byte = literal_count - 1
                    encoded.append(length_byte)
                    encoded.extend(self.input_buffer[literal_start:literal_start + literal_count])
                    i += literal_count
                else:
                    i += 1  # Avoid infinite loop

            # Update record position
            if self.record_size > 0:
                self.record_position = (self.record_position + (i - literal_start)) % self.record_size

        # Remove processed data
        self.input_buffer = self.input_buffer[i:]
        return bytes(encoded)

    def _write_encoded_data(self, ctxt: ps.Context, encoded_data: bytes) -> None:
        """Write encoded data to target"""
        if isinstance(self.data_source.source, ps.File):
            for byte_val in encoded_data:
                self.data_source.source.write(ctxt, byte_val)

    def close(self, ctxt: ps.Context) -> None:
        """Encode remaining data and write EOD marker"""
        # Process any remaining data
        if self.input_buffer:
            encoded_data = self._encode_next_segment(ctxt)
            if encoded_data:
                self._write_encoded_data(ctxt, encoded_data)

        # PLRM: Write EOD marker (byte value 128)
        if isinstance(self.data_source.source, ps.File):
            self.data_source.source.write(ctxt, 128)

        super().close(ctxt)


class LZWDecodeFilter(FilterBase):
    """LZWDecode filter - PLRM compliant Lempel-Ziv-Welch decompression"""

    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        super().__init__(data_source, params)

        # Extract parameters from dictionary
        self.unit_length = 8  # Default: 8-bit units
        self.early_change = 1  # Default: increase code length one code early
        self.low_bit_first = False  # Default: high-order bit first

        if params and hasattr(params, 'val'):  # PostScript Dict object
            param_dict = params.val  # Get the Python dict from PostScript Dict
            if b'UnitLength' in param_dict:
                unit_obj = param_dict[b'UnitLength']
                self.unit_length = unit_obj.val if hasattr(unit_obj, 'val') else int(unit_obj)
            if b'EarlyChange' in param_dict:
                early_obj = param_dict[b'EarlyChange']
                self.early_change = early_obj.val if hasattr(early_obj, 'val') else int(early_obj)
            if b'LowBitFirst' in param_dict:
                low_obj = param_dict[b'LowBitFirst']
                self.low_bit_first = low_obj.val if hasattr(low_obj, 'val') else bool(low_obj)

        # LZW algorithm state
        self.clear_code = 2 ** self.unit_length      # 256 for unit_length=8
        self.eod_code = self.clear_code + 1          # 257 for unit_length=8
        self.reset_decoder()

        # Bit stream handling
        self.bit_buffer = 0
        self.bits_available = 0
        self.input_buffer = bytearray()

    def reset_decoder(self) -> None:
        """Reset LZW decoder state to initial conditions"""
        # Initialize string table with single-character entries
        self.string_table = {}
        for i in range(self.clear_code):
            self.string_table[i] = bytes([i])

        self.next_code = self.clear_code + 2  # First available code after clear/eod
        self.code_length = self.unit_length + 1  # Start with 9 bits for unit_length=8
        self.previous_code = None

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Decode LZW data to binary - PLRM Section 3.13"""
        if self.eof_reached:
            return b''

        result = bytearray()
        target_bytes = max_bytes or 1024

        while len(result) < target_bytes and not self.eod_reached:
            # Read more data if needed
            while len(self.input_buffer) < 4 and not self.data_source.at_eof():
                source_data = self.data_source.read_data(ctxt, 1024)
                if source_data:
                    self.input_buffer.extend(source_data)
                else:
                    break

            # Read next code from bit stream
            code = self.read_code()
            if code is None:
                self.eof_reached = True
                break

            # Process the code
            if code == self.clear_code:
                # Clear table and reset
                self.reset_decoder()
                continue
            elif code == self.eod_code:
                # End of data
                self.eof_reached = True
                break
            elif code in self.string_table:
                # Known code - output its string
                string = self.string_table[code]
                result.extend(string)

                # Add new table entry if we have a previous code
                if self.previous_code is not None and self.next_code < 4096:
                    prev_string = self.string_table[self.previous_code]
                    new_string = prev_string + bytes([string[0]])
                    self.string_table[self.next_code] = new_string
                    self.next_code += 1
                    self.update_code_length()

                self.previous_code = code
            else:
                # Unknown code - should be next available code
                if code == self.next_code and self.previous_code is not None:
                    # Special case: code refers to string we're about to create
                    prev_string = self.string_table[self.previous_code]
                    new_string = prev_string + bytes([prev_string[0]])
                    self.string_table[self.next_code] = new_string
                    result.extend(new_string)
                    self.next_code += 1
                    self.update_code_length()
                    self.previous_code = code
                else:
                    # Invalid code - raise ioerror as per PLRM
                    self.eod_reached = True
                    raise IOError("Invalid LZW code sequence")

        return bytes(result)

    def read_code(self) -> int | None:
        """Read next code from bit stream"""
        # Need enough bits for current code length
        while self.bits_available < self.code_length and self.input_buffer:
            byte = self.input_buffer.pop(0)
            if self.low_bit_first:
                # Pack low-order bit first
                self.bit_buffer |= (byte << self.bits_available)
            else:
                # Pack high-order bit first (default)
                self.bit_buffer = (self.bit_buffer << 8) | byte
            self.bits_available += 8

        if self.bits_available < self.code_length:
            return None  # Not enough data

        # Extract code
        if self.low_bit_first:
            code = self.bit_buffer & ((1 << self.code_length) - 1)
            self.bit_buffer >>= self.code_length
        else:
            # High-order bit first: extract from the top
            shift = self.bits_available - self.code_length
            code = self.bit_buffer >> shift
            self.bit_buffer &= (1 << shift) - 1

        self.bits_available -= self.code_length
        return code

    def update_code_length(self) -> None:
        """Update code length when string table grows"""
        # PLRM: Check if we need to increase code length
        if self.code_length < 12:
            # Calculate threshold for code length increase
            if self.early_change == 1:
                # Increase one code early
                threshold = (1 << self.code_length) - 1
            else:
                # Postpone as long as possible
                threshold = 1 << self.code_length

            if self.next_code >= threshold:
                self.code_length += 1


class LZWEncodeFilter(FilterBase):
    """LZWEncode filter - PLRM compliant Lempel-Ziv-Welch compression"""

    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        super().__init__(data_source, params)

        # Extract parameters - encoding only supports default values per PLRM
        self.unit_length = 8  # Fixed for encoding
        self.early_change = 1  # Default behavior
        self.low_bit_first = False  # Fixed for encoding

        # LZW algorithm state
        self.clear_code = 2 ** self.unit_length      # 256
        self.eod_code = self.clear_code + 1          # 257
        self.reset_encoder()

        # Bit stream handling
        self.bit_buffer = 0
        self.bits_used = 0
        self.output_buffer = bytearray()
        self.input_sequence = bytearray()

        self._pending_string = b''  # Carried across write_data calls

        # Write initial clear-table code
        self.write_code(self.clear_code)

    def reset_encoder(self) -> None:
        """Reset LZW encoder state to initial conditions"""
        # Initialize string table with single-character entries
        self.string_table = {}
        for i in range(self.clear_code):
            self.string_table[bytes([i])] = i

        self.next_code = self.clear_code + 2  # First available code after clear/eod
        self.code_length = self.unit_length + 1  # Start with 9 bits

    def write_data(self, ctxt: ps.Context, data: bytes) -> None:
        """Encode data using LZW compression"""
        self.input_sequence.extend(data)

        # Resume from pending string carried over from the previous call.
        # writestring sends one byte at a time through FilterFile.write(),
        # so _pending_string is essential for building multi-byte sequences.
        w = self._pending_string

        for byte in data:
            wc = w + bytes([byte])  # w + current character

            if wc in self.string_table:
                w = wc  # Extend current string
            else:
                # Output code for w
                if w:  # Don't output empty string
                    code = self.string_table[w]
                    self.write_code(code)

                # Add wc to string table if table not full
                if self.next_code < 4096:
                    self.string_table[wc] = self.next_code
                    self.next_code += 1
                    self.update_code_length()
                else:
                    # Table full - issue clear-table code and reset
                    self.write_code(self.clear_code)
                    self.reset_encoder()

                w = bytes([byte])  # Start new string

        # Keep w for next write_data call (don't output yet)
        self._pending_string = w

    def write_code(self, code: int) -> None:
        """Write code to bit stream"""
        # Pack code into bit buffer (high-order bit first)
        self.bit_buffer = (self.bit_buffer << self.code_length) | code
        self.bits_used += self.code_length

        # Output complete bytes
        while self.bits_used >= 8:
            byte = (self.bit_buffer >> (self.bits_used - 8)) & 0xFF
            self.output_buffer.append(byte)
            self.bits_used -= 8
            self.bit_buffer &= (1 << self.bits_used) - 1

        # Output buffer will be written in close() method when we have context

    def _write_output_buffer(self, ctxt: ps.Context) -> None:
        """Write output buffer to target"""
        if isinstance(self.data_source.source, ps.File):
            for byte_val in self.output_buffer:
                self.data_source.source.write(ctxt, byte_val)
            self.output_buffer.clear()

    def update_code_length(self) -> None:
        """Update code length when string table grows"""
        if self.code_length < 12:
            # Calculate threshold (early change by default)
            threshold = (1 << self.code_length) - 1
            if self.next_code >= threshold:
                self.code_length += 1

    def close(self, ctxt: ps.Context) -> None:
        """Finish encoding and write EOD marker"""
        # Output final pending string if any
        if hasattr(self, '_pending_string') and self._pending_string:
            if self._pending_string in self.string_table:
                code = self.string_table[self._pending_string]
                self.write_code(code)

        # Write EOD marker
        self.write_code(self.eod_code)

        # Flush remaining bits (pad with zeros)
        if self.bits_used > 0:
            # Pad to byte boundary
            self.bit_buffer <<= (8 - self.bits_used)
            self.output_buffer.append(self.bit_buffer & 0xFF)

        # Write any remaining output buffer
        self._write_output_buffer(ctxt)

        super().close(ctxt)


def _paeth_predictor(a: int, b: int, c: int) -> int:
    """PNG Paeth predictor function per PNG specification."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    else:
        return c


class FlateDecodeFilter(FilterBase):
    """FlateDecode filter - PLRM compliant zlib/deflate decompression (LanguageLevel 3)"""

    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        super().__init__(data_source, params)

        # Extract parameters from dictionary
        self.predictor = 1  # Default: no predictor
        self.columns = 1    # Default columns
        self.colors = 1     # Default colors
        self.bits_per_component = 8  # Default bits per component

        if params and hasattr(params, 'val'):  # PostScript Dict object
            param_dict = params.val
            if b'Predictor' in param_dict:
                pred_obj = param_dict[b'Predictor']
                self.predictor = pred_obj.val if hasattr(pred_obj, 'val') else int(pred_obj)
            if b'Columns' in param_dict:
                col_obj = param_dict[b'Columns']
                self.columns = col_obj.val if hasattr(col_obj, 'val') else int(col_obj)
            if b'Colors' in param_dict:
                colors_obj = param_dict[b'Colors']
                self.colors = colors_obj.val if hasattr(colors_obj, 'val') else int(colors_obj)
            if b'BitsPerComponent' in param_dict:
                bpc_obj = param_dict[b'BitsPerComponent']
                self.bits_per_component = bpc_obj.val if hasattr(bpc_obj, 'val') else int(bpc_obj)

        # Predictor state for row-based decoding
        if self.predictor > 1:
            self.bpp = max(1, (self.colors * self.bits_per_component + 7) // 8)
            self.row_width = (self.columns * self.colors * self.bits_per_component + 7) // 8
            if self.predictor >= 10:
                self._raw_row_len = 1 + self.row_width  # PNG: filter byte + data
            else:
                self._raw_row_len = self.row_width       # TIFF: no prefix
            self._predictor_buffer = bytearray()
            self._prev_row = bytearray(self.row_width)   # zeros initially (per PNG spec)

        # Initialize decompressor
        self.decompressor = zlib.decompressobj()
        self.input_buffer = bytearray()
        self.output_buffer = bytearray()

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Decompress zlib/deflate data - PLRM Section 3.13"""
        if self.eof_reached:
            return b''

        result = bytearray()
        target_bytes = max_bytes or 1024

        while len(result) < target_bytes and not self.eof_reached:
            # Try to serve from output buffer first
            if self.output_buffer:
                bytes_to_take = min(len(self.output_buffer), target_bytes - len(result))
                result.extend(self.output_buffer[:bytes_to_take])
                self.output_buffer = self.output_buffer[bytes_to_take:]
                continue

            # Read more compressed data from source
            source_data = self.data_source.read_data(ctxt, 2048)
            if not source_data:
                # Try to flush any remaining data from decompressor
                try:
                    remaining_data = self.decompressor.flush()
                    if remaining_data:
                        if self.predictor > 1:
                            self._predictor_buffer.extend(remaining_data)
                            self._process_predictor_rows()
                            # Flush any partial final row as-is
                            if self._predictor_buffer:
                                self.output_buffer.extend(self._predictor_buffer)
                                self._predictor_buffer = bytearray()
                        else:
                            self.output_buffer.extend(remaining_data)
                        continue
                    else:
                        # Flush any remaining predictor buffer
                        if self.predictor > 1 and self._predictor_buffer:
                            self.output_buffer.extend(self._predictor_buffer)
                            self._predictor_buffer = bytearray()
                            continue
                        self.eof_reached = True
                        break
                except zlib.error:
                    raise IOError("Flate decompression error")

            # Decompress the data
            try:
                decompressed = self.decompressor.decompress(source_data)
                if decompressed:
                    if self.predictor > 1:
                        self._predictor_buffer.extend(decompressed)
                        self._process_predictor_rows()
                    else:
                        self.output_buffer.extend(decompressed)
                elif self.decompressor.eof:
                    # Flush predictor buffer at EOF
                    if self.predictor > 1:
                        self._process_predictor_rows()
                        if self._predictor_buffer:
                            self.output_buffer.extend(self._predictor_buffer)
                            self._predictor_buffer = bytearray()
                    self.eof_reached = True
            except zlib.error:
                raise IOError("Flate decompression error")

        return bytes(result)

    def _process_predictor_rows(self) -> None:
        """Process complete rows from _predictor_buffer into output_buffer."""
        while len(self._predictor_buffer) >= self._raw_row_len:
            raw_row = self._predictor_buffer[:self._raw_row_len]
            self._predictor_buffer = self._predictor_buffer[self._raw_row_len:]

            if self.predictor >= 10:
                decoded = self._decode_png_row(raw_row)
            else:
                decoded = self._decode_tiff_row(raw_row)

            self.output_buffer.extend(decoded)
            self._prev_row = bytearray(decoded)

    def _decode_png_row(self, raw_row: bytearray | bytes) -> bytearray:
        """Decode a single PNG-predicted row. First byte is filter type (0-4)."""
        filter_type = raw_row[0]
        filtered = raw_row[1:]
        row = bytearray(self.row_width)
        bpp = self.bpp
        prev = self._prev_row

        if filter_type == 0:
            # None
            row[:] = filtered
        elif filter_type == 1:
            # Sub: Recon[i] = Filt[i] + Recon[i - bpp]
            for i in range(self.row_width):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (filtered[i] + left) & 0xFF
        elif filter_type == 2:
            # Up: Recon[i] = Filt[i] + Prior[i]
            for i in range(self.row_width):
                row[i] = (filtered[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            # Average: Recon[i] = Filt[i] + floor((Recon[i-bpp] + Prior[i]) / 2)
            for i in range(self.row_width):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (filtered[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            # Paeth: Recon[i] = Filt[i] + PaethPredictor(a, b, c)
            for i in range(self.row_width):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                row[i] = (filtered[i] + _paeth_predictor(a, b, c)) & 0xFF
        else:
            # Unknown filter type — pass through as-is
            row[:] = filtered

        return row

    def _decode_tiff_row(self, raw_row: bytearray | bytes) -> bytearray:
        """Decode a single TIFF Predictor 2 row (horizontal undifferencing)."""
        row = bytearray(raw_row)

        if self.bits_per_component == 8:
            colors = self.colors
            for i in range(colors, self.row_width):
                row[i] = (row[i] + row[i - colors]) & 0xFF
        elif self.bits_per_component == 16:
            # 16-bit components: 2 bytes per value, big-endian
            colors = self.colors
            byte_stride = colors * 2
            for i in range(byte_stride, self.row_width, 2):
                prev_val = (row[i - byte_stride] << 8) | row[i - byte_stride + 1]
                cur_val = (row[i] << 8) | row[i + 1]
                result_val = (cur_val + prev_val) & 0xFFFF
                row[i] = (result_val >> 8) & 0xFF
                row[i + 1] = result_val & 0xFF
        # For other bit depths, pass through as-is

        return row


class FlateEncodeFilter(FilterBase):
    """FlateEncode filter - PLRM compliant zlib/deflate compression (LanguageLevel 3)"""

    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        super().__init__(data_source, params)

        # Extract parameters from dictionary
        self.effort = -1    # Default: reasonable default
        self.predictor = 1  # Default: no predictor
        self.columns = 1    # Default columns
        self.colors = 1     # Default colors
        self.bits_per_component = 8  # Default bits per component

        if params and hasattr(params, 'val'):  # PostScript Dict object
            param_dict = params.val
            if b'Effort' in param_dict:
                effort_obj = param_dict[b'Effort']
                self.effort = effort_obj.val if hasattr(effort_obj, 'val') else int(effort_obj)
            if b'Predictor' in param_dict:
                pred_obj = param_dict[b'Predictor']
                self.predictor = pred_obj.val if hasattr(pred_obj, 'val') else int(pred_obj)
            if b'Columns' in param_dict:
                col_obj = param_dict[b'Columns']
                self.columns = col_obj.val if hasattr(col_obj, 'val') else int(col_obj)
            if b'Colors' in param_dict:
                colors_obj = param_dict[b'Colors']
                self.colors = colors_obj.val if hasattr(colors_obj, 'val') else int(colors_obj)
            if b'BitsPerComponent' in param_dict:
                bpc_obj = param_dict[b'BitsPerComponent']
                self.bits_per_component = bpc_obj.val if hasattr(bpc_obj, 'val') else int(bpc_obj)

        # Predictor state for row-based encoding
        if self.predictor > 1:
            self.bpp = max(1, (self.colors * self.bits_per_component + 7) // 8)
            self.row_width = (self.columns * self.colors * self.bits_per_component + 7) // 8
            self._encode_buffer = bytearray()
            self._prev_row = bytearray(self.row_width)  # zeros initially

        # Map effort to zlib compression level
        if self.effort == -1:
            compression_level = zlib.Z_DEFAULT_COMPRESSION
        else:
            compression_level = max(0, min(9, self.effort))

        # Initialize compressor
        self.compressor = zlib.compressobj(level=compression_level)
        self.input_buffer = bytearray()

    def write_data(self, ctxt: ps.Context, data: bytes) -> None:
        """Compress data using zlib/deflate - PLRM Section 3.13"""
        if self.predictor > 1:
            self._encode_buffer.extend(data)
            self._process_encode_rows(ctxt)
        else:
            # No predictor — compress directly
            try:
                compressed = self.compressor.compress(data)
                if compressed:
                    self._write_compressed_data(ctxt, compressed)
            except zlib.error:
                raise IOError("Flate compression error")

    def _process_encode_rows(self, ctxt: ps.Context) -> None:
        """Process complete rows from _encode_buffer, encode and compress."""
        while len(self._encode_buffer) >= self.row_width:
            row = self._encode_buffer[:self.row_width]
            self._encode_buffer = self._encode_buffer[self.row_width:]

            if self.predictor >= 10:
                encoded = self._encode_png_row(row)
            else:
                encoded = self._encode_tiff_row(row)

            self._prev_row = bytearray(row)

            try:
                compressed = self.compressor.compress(bytes(encoded))
                if compressed:
                    self._write_compressed_data(ctxt, compressed)
            except zlib.error:
                raise IOError("Flate compression error")

    def _encode_png_row(self, row: bytearray | bytes) -> bytearray:
        """Encode a row using PNG Sub filter (type 1)."""
        bpp = self.bpp
        encoded = bytearray(1 + self.row_width)
        encoded[0] = 1  # Sub filter type byte
        for i in range(self.row_width):
            left = row[i - bpp] if i >= bpp else 0
            encoded[1 + i] = (row[i] - left) & 0xFF
        return encoded

    def _encode_tiff_row(self, row: bytearray | bytes) -> bytearray:
        """Encode a row using TIFF Predictor 2 (horizontal differencing)."""
        encoded = bytearray(row)

        if self.bits_per_component == 8:
            colors = self.colors
            for i in range(self.row_width - 1, colors - 1, -1):
                encoded[i] = (encoded[i] - encoded[i - colors]) & 0xFF
        elif self.bits_per_component == 16:
            colors = self.colors
            byte_stride = colors * 2
            for i in range(self.row_width - 2, byte_stride - 2, -2):
                cur_val = (encoded[i] << 8) | encoded[i + 1]
                prev_val = (encoded[i - byte_stride] << 8) | encoded[i - byte_stride + 1]
                result_val = (cur_val - prev_val) & 0xFFFF
                encoded[i] = (result_val >> 8) & 0xFF
                encoded[i + 1] = result_val & 0xFF

        return encoded

    def _write_compressed_data(self, ctxt: ps.Context, compressed_data: bytes) -> None:
        """Write compressed data to target"""
        if isinstance(self.data_source.source, ps.File):
            for byte_val in compressed_data:
                self.data_source.source.write(ctxt, byte_val)

    def close(self, ctxt: ps.Context) -> None:
        """Flush compressor and write final data"""
        # Flush any remaining encode buffer (partial row)
        if self.predictor > 1 and self._encode_buffer:
            # Pad partial row to row_width with zeros
            partial = self._encode_buffer
            padded = partial + bytearray(self.row_width - len(partial))
            if self.predictor >= 10:
                encoded = self._encode_png_row(padded)
            else:
                encoded = self._encode_tiff_row(padded)
            self._encode_buffer = bytearray()
            try:
                compressed = self.compressor.compress(bytes(encoded))
                if compressed:
                    self._write_compressed_data(ctxt, compressed)
            except zlib.error:
                raise IOError("Flate compression error")

        try:
            # Flush any remaining compressed data
            final_data = self.compressor.flush()
            if final_data:
                self._write_compressed_data(ctxt, final_data)
        except zlib.error:
            raise IOError("Flate compression error")

        super().close(ctxt)
