# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# ASCII Encoding/Decoding Filters
#
# Implements ASCIIHex and ASCII85 encode/decode filters per PLRM Section 3.13,
# plus NullEncode pass-through filter.

from typing import TYPE_CHECKING

from ..core import types as ps
from .filter import FilterBase

if TYPE_CHECKING:
    from .filter import DataSource


class ASCIIHexDecodeFilter(FilterBase):
    """ASCII Hexadecimal decode filter - converts hex digits to bytes"""

    def __init__(self, data_source: DataSource, params: dict | None = None) -> None:
        super().__init__(data_source, params)
        self.hex_buffer = bytearray()
        self.eod_reached = False

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Decode hex data to binary"""
        if self.eof_reached or self.eod_reached:
            return b''

        result = bytearray()
        target_bytes = max_bytes or 1024

        while len(result) < target_bytes and not self.eof_reached and not self.eod_reached:
            # Read more data from source
            source_data = self.data_source.read_data(ctxt, 2048)
            if not source_data:
                self.eof_reached = True
                break

            self.hex_buffer.extend(source_data)

            # Process hex data
            while len(self.hex_buffer) >= 2 and len(result) < target_bytes:
                # Skip whitespace
                while self.hex_buffer and self.hex_buffer[0] in b' \t\n\r\f':
                    self.hex_buffer.pop(0)

                if not self.hex_buffer:
                    break

                # Check for EOD marker '>' (only relevant for file/stream sources)
                if self.hex_buffer[0] == ord('>'):
                    self.eod_reached = True
                    # Push back bytes after '>' to the underlying source
                    if len(self.hex_buffer) > 1:
                        self.data_source.putback(bytes(self.hex_buffer[1:]))
                        self.hex_buffer = bytearray()
                    break

                # Get two hex digits
                if len(self.hex_buffer) >= 2:
                    hex1 = self.hex_buffer.pop(0)
                    hex2 = self.hex_buffer.pop(0)

                    try:
                        # Convert hex pair to byte
                        hex_str = bytes([hex1, hex2]).decode('ascii')
                        byte_val = int(hex_str, 16)
                        result.append(byte_val)
                    except (ValueError, UnicodeDecodeError):
                        # Skip invalid hex digits
                        continue
                elif len(self.hex_buffer) == 1:
                    # Handle final odd hex digit by padding with 0
                    hex1 = self.hex_buffer.pop(0)
                    try:
                        hex_str = bytes([hex1, ord('0')]).decode('ascii')
                        byte_val = int(hex_str, 16)
                        result.append(byte_val)
                        self.eod_reached = True
                    except (ValueError, UnicodeDecodeError):
                        pass
                    break

        return bytes(result)


class ASCIIHexEncodeFilter(FilterBase):
    """ASCII Hexadecimal encode filter - converts bytes to hex digits"""

    def write_data(self, ctxt: ps.Context, data: bytes) -> None:
        """Encode binary data to hex"""
        # Convert each byte to two hex digits
        hex_data = bytearray()
        for byte_val in data:
            hex_data.extend(f"{byte_val:02X}".encode('ascii'))

        # Write to underlying target
        if isinstance(self.data_source.source, ps.File):
            for byte_val in hex_data:
                self.data_source.source.write(ctxt, byte_val)

    def close(self, ctxt: ps.Context) -> None:
        """Write EOD marker and **close**"""
        if isinstance(self.data_source.source, ps.File):
            self.data_source.source.write(ctxt, ord('>'))
        super().close(ctxt)


class NullEncodeFilter(FilterBase):
    """Null encode filter - pass-through filter for testing"""

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Pass through data unchanged"""
        return self.data_source.read_data(ctxt, max_bytes)

    def write_data(self, ctxt: ps.Context, data: bytes) -> None:
        """Pass through data unchanged"""
        if isinstance(self.data_source.source, ps.File):
            for byte_val in data:
                self.data_source.source.write(ctxt, byte_val)


class ASCII85DecodeFilter(FilterBase):
    """ASCII85 decode filter - PLRM compliant base-85 ASCII to binary conversion"""

    def __init__(self, data_source: DataSource, params: dict | None = None) -> None:
        super().__init__(data_source, params)
        self.decode_buffer = bytearray()
        self.eod_reached = False  # End of ASCII85 data stream (not end of file)

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Decode ASCII85 data to binary - PLRM Section 3.13

        Uses a "read all and cache" strategy: on first call, reads and decodes
        the ENTIRE ASCII85 stream until ~> is found. This guarantees that the
        ~> end marker is always consumed, preventing issues when **filter** chains
        are discarded before consuming all data.
        """
        # On first call, decode the entire stream into cache
        if not hasattr(self, '_decoded_cache'):
            self._decoded_cache = self._decode_entire_stream(ctxt)
            self._cache_pos = 0

        # Return requested bytes from cache
        if max_bytes is None:
            max_bytes = 1024

        start = self._cache_pos
        end = min(start + max_bytes, len(self._decoded_cache))
        result = self._decoded_cache[start:end]
        self._cache_pos = end

        return result

    def _decode_entire_stream(self, ctxt: ps.Context) -> bytes:
        """Read and decode the entire ASCII85 stream until ~> is found.

        Reads source data in large chunks and iterates with an index to avoid
        per-byte pop(0) overhead.
        """
        result = bytearray()
        ascii85_chars = []

        # Collect all source bytes into a flat buffer first, then decode.
        # This avoids per-byte read_data calls and pop(0) shifts.
        buf = bytearray(self.decode_buffer)
        self.decode_buffer = bytearray()
        pos = 0

        while not self.eod_reached:
            # Refill buffer when exhausted
            if pos >= len(buf):
                source_data = self.data_source.read_data(ctxt, 4096)
                if not source_data:
                    self.eof_reached = True
                    break
                buf = bytearray(source_data)
                pos = 0

            char = buf[pos]

            # Skip whitespace
            if char in (32, 9, 10, 13):  # space, tab, LF, CR
                pos += 1
                continue

            # Check for EOD marker '~>'
            if char == 126:  # '~'
                # Need to check next char
                if pos + 1 >= len(buf):
                    # Need more data to see if '>' follows
                    source_data = self.data_source.read_data(ctxt, 4096)
                    if source_data:
                        buf = buf[pos:] + bytearray(source_data)
                        pos = 0
                        continue
                    else:
                        self.eof_reached = True
                        break

                if buf[pos + 1] == 62:  # '>'
                    pos += 2
                    self.eod_reached = True
                    break
                else:
                    # ~ not followed by >, skip the ~
                    pos += 1
                    continue

            # Handle special case 'z' (four zero bytes)
            if char == 122:  # 'z'
                if ascii85_chars:
                    if len(ascii85_chars) >= 2:
                        decoded_bytes = self._decode_ascii85_group(ascii85_chars)
                        result.extend(decoded_bytes)
                    ascii85_chars = []
                result.extend(b'\x00\x00\x00\x00')
                pos += 1
                continue

            # Check for valid ASCII85 character (!-u, 33-117)
            if 33 <= char <= 117:
                ascii85_chars.append(char - 33)  # Convert to 0-84
                pos += 1

                # Process complete 5-tuple group
                if len(ascii85_chars) == 5:
                    try:
                        decoded_bytes = self._decode_ascii85_group(ascii85_chars)
                        result.extend(decoded_bytes)
                    except (ValueError, OverflowError, IOError) as e:
                        chars_str = ''.join(chr(c + 33) for c in ascii85_chars)
                        raise IOError(f"Invalid ASCII85 5-tuple '{chars_str}': {e}")
                    ascii85_chars = []
            else:
                # Invalid character - ignore it (PLRM: ignore all other characters)
                pos += 1

        # Push any unconsumed bytes back to the data source
        # This is critical for inline images: bytes after ~> belong to the
        # PostScript stream and must be available for subsequent reads
        if pos < len(buf):
            leftover = buf[pos:]
            if self.eod_reached:
                # We found ~>, so push leftover bytes back to source
                # so they can be read by subsequent operations
                self.data_source.putback(bytes(leftover))
            else:
                # Still decoding (hit EOF without ~>), keep in local buffer
                self.decode_buffer = leftover

        # Handle any remaining partial group at end of data
        if ascii85_chars and len(ascii85_chars) >= 2:
            try:
                decoded_bytes = self._decode_ascii85_group(ascii85_chars)
                result.extend(decoded_bytes)
            except (ValueError, OverflowError) as e:
                raise IOError(f"Invalid ASCII85 5-tuple value: {e}")

        return bytes(result)

    def _decode_ascii85_group(self, values: list[int]) -> bytes:
        """Decode ASCII85 5-tuple to 4 bytes - PLRM algorithm"""
        group_len = len(values)

        # PLRM: Partial final group must have at least 2 characters
        if group_len == 1:
            raise ValueError(f"Invalid partial ASCII85 group: got {group_len} character(s), need at least 2")

        # Pad to 5 values if needed (pad with 84 = 'u' - '!')
        while len(values) < 5:
            values.append(84)

        # Convert base-85 to 32-bit integer: c1*85^4 + c2*85^3 + c3*85^2 + c4*85^1 + c5*85^0
        value = 0
        for digit in values:
            if digit < 0 or digit > 84:
                raise ValueError(f"Invalid ASCII85 digit value: {digit} (must be 0-84)")
            value = value * 85 + digit

        # PLRM: Check for impossible combinations (value > 2^32-1)
        if value > 0xFFFFFFFF:
            raise IOError(f"ASCII85 5-tuple value {value} exceeds 2^32-1 (impossible combination)")

        # Convert to 4 bytes (big-endian: most significant byte first)
        byte1 = (value >> 24) & 0xFF
        byte2 = (value >> 16) & 0xFF
        byte3 = (value >> 8) & 0xFF
        byte4 = value & 0xFF

        # Return appropriate number of bytes for partial groups
        if group_len == 2:
            return bytes([byte1])  # 2 chars → 1 byte
        elif group_len == 3:
            return bytes([byte1, byte2])  # 3 chars → 2 bytes
        elif group_len == 4:
            return bytes([byte1, byte2, byte3])  # 4 chars → 3 bytes
        else:
            return bytes([byte1, byte2, byte3, byte4])  # 5 chars → 4 bytes


class ASCII85EncodeFilter(FilterBase):
    """ASCII85 encode filter - PLRM compliant binary to base-85 ASCII conversion"""

    def __init__(self, data_source: DataSource, params: dict | None = None) -> None:
        super().__init__(data_source, params)
        self.encode_buffer = bytearray()
        self.column_count = 0  # Track output column for line breaks

    def write_data(self, ctxt: ps.Context, data: bytes) -> None:
        """Encode binary data to ASCII85 - PLRM Section 3.13"""
        self.encode_buffer.extend(data)

        # Process complete 4-byte groups
        while len(self.encode_buffer) >= 4:
            four_bytes = self.encode_buffer[:4]
            self.encode_buffer = self.encode_buffer[4:]

            # PLRM: Special case - four zero bytes become single 'z'
            if four_bytes == b'\x00\x00\x00\x00':
                self._write_char(ctxt, ord('z'))
            else:
                # Convert 4 bytes to 32-bit integer (big-endian)
                value = (four_bytes[0] << 24) | (four_bytes[1] << 16) | \
                       (four_bytes[2] << 8) | four_bytes[3]

                # Convert to base-85 (5 characters)
                chars = []
                for _ in range(5):
                    chars.append(value % 85)
                    value //= 85

                # Write characters (reverse order: most significant first)
                for char in reversed(chars):
                    self._write_char(ctxt, ord('!') + char)

    def _write_char(self, ctxt: ps.Context, char_code: int) -> None:
        """Write single character with line break management"""
        if isinstance(self.data_source.source, ps.File):
            self.data_source.source.write(ctxt, char_code)
            self.column_count += 1

            # PLRM: Insert newlines at least every 80 characters
            if self.column_count >= 80:
                self.data_source.source.write(ctxt, ord('\n'))
                self.column_count = 0

    def close(self, ctxt: ps.Context) -> None:
        """Encode remaining bytes and write EOD marker - PLRM algorithm"""
        # Handle remaining bytes (1-3 bytes)
        if self.encode_buffer:
            remaining = len(self.encode_buffer)

            # Pad to 4 bytes with zeros
            padded = self.encode_buffer + b'\x00' * (4 - remaining)

            # Convert to 32-bit integer (big-endian)
            value = (padded[0] << 24) | (padded[1] << 16) | \
                   (padded[2] << 8) | padded[3]

            # Convert to base-85
            chars = []
            for _ in range(5):
                chars.append(value % 85)
                value //= 85

            # PLRM: Output first (n+1) characters for n input bytes
            chars_needed = remaining + 1
            for i in range(chars_needed):
                char = chars[4-i]  # Reverse order
                self._write_char(ctxt, ord('!') + char)

        # Write EOD marker '~>'
        if isinstance(self.data_source.source, ps.File):
            self.data_source.source.write(ctxt, ord('~'))
            self.data_source.source.write(ctxt, ord('>'))

        super().close(ctxt)
