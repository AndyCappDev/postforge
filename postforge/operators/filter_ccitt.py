# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

# CCITTFax Decode Filter
#
# Implements CCITTFaxDecode per PLRM Section 3.13, Table 3.21.
# Uses Pillow's libtiff backend via TIFF wrapping to decode Group 3/4 fax data.

import io
import struct

from PIL import Image

from .filter import FilterBase


def _extract_param(params, key, default):
    """Extract a parameter value from a PS Dict or plain dict."""
    if params is None:
        return default
    # PS Dict object
    if hasattr(params, 'val'):
        param_dict = params.val
    elif isinstance(params, dict):
        param_dict = params
    else:
        return default
    if key not in param_dict:
        return default
    obj = param_dict[key]
    if hasattr(obj, 'val'):
        return obj.val
    return obj


class CCITTFaxDecodeFilter(FilterBase):
    """CCITTFaxDecode filter — PLRM Section 3.13, Table 3.21.

    Decodes CCITT Group 3 (1-D and 2-D) and Group 4 fax-encoded data.
    Implementation wraps encoded data in a minimal TIFF container and
    uses Pillow's libtiff backend for actual decoding.

    PLRM Parameters:
        K           int   0      <0=Group4, 0=Group3-1D, >0=Group3-2D
        Columns     int   1728   Image width in pixels
        Rows        int   0      Image height (0=unknown)
        EndOfBlock  bool  True   Expect EOFB/RTC termination
        EndOfLine   bool  False  EOL patterns prefix each line
        EncodedByteAlign bool False  Lines are byte-aligned
        BlackIs1    bool  False  Pixel polarity
        DamagedRowsBeforeError int 0  Error tolerance
    """

    def __init__(self, data_source, params=None):
        super().__init__(data_source, params)

        self.k = _extract_param(params, b'K', 0)
        self.columns = _extract_param(params, b'Columns', 1728)
        self.rows = _extract_param(params, b'Rows', 0)
        self.end_of_block = _extract_param(params, b'EndOfBlock', True)
        self.end_of_line = _extract_param(params, b'EndOfLine', False)
        self.encoded_byte_align = _extract_param(params, b'EncodedByteAlign', False)
        self.black_is_1 = _extract_param(params, b'BlackIs1', False)
        self.damaged_rows_before_error = _extract_param(params, b'DamagedRowsBeforeError', 0)

        self.output_buffer = bytearray()
        self._buf_pos = 0
        self._decoded = False

    def read_data(self, ctxt, max_bytes=None):
        """Decode CCITT fax data via TIFF wrapping — PLRM Section 3.13."""
        if self.eof_reached:
            return b''

        if not self._decoded:
            self._decode_all(ctxt)

        if self._buf_pos >= len(self.output_buffer):
            self.eof_reached = True
            return b''

        target = max_bytes or 1024
        chunk = bytes(self.output_buffer[self._buf_pos:self._buf_pos + target])
        self._buf_pos += len(chunk)

        if self._buf_pos >= len(self.output_buffer):
            self.eof_reached = True

        return chunk

    def _decode_all(self, ctxt):
        """Read all encoded data from upstream and decode via TIFF wrapping."""
        self._decoded = True

        # Collect all encoded data
        chunks = []
        while True:
            data = self.data_source.read_data(ctxt, 65536)
            if not data:
                break
            chunks.append(data)
        encoded = b''.join(chunks)

        if not encoded:
            self.eof_reached = True
            return

        decoded = self._decode_via_tiff(encoded)
        if decoded is not None:
            self.output_buffer = bytearray(decoded)
        else:
            self.eof_reached = True

    def _decode_via_tiff(self, encoded_data):
        """Wrap CCITT data in a minimal TIFF and decode with Pillow.

        Returns packed 1-bit data (8 pixels per byte, MSB first, rows padded
        to byte boundaries) matching PostScript image data conventions.
        """
        # Map K parameter to TIFF compression tag
        if self.k < 0:
            compression = 4   # Group 4 (T.6)
        elif self.k == 0:
            compression = 2   # CCITT Modified Huffman (Group 3 1-D, no EOL)
        else:
            compression = 3   # T.4 (Group 3, may be 2-D)

        columns = self.columns
        rows = self.rows if self.rows > 0 else self._estimate_rows(encoded_data)

        # Always use WhiteIsZero (standard CCITT convention) in TIFF.
        # We handle BlackIs1 polarity when packing the output bits.
        photometric = 0  # WhiteIsZero

        tiff_bytes = self._build_tiff(
            encoded_data, columns, rows, compression, photometric
        )

        try:
            img = Image.open(io.BytesIO(tiff_bytes))
            img.load()
            return self._pack_image_bits(img)
        except Exception:
            # Try with fallback row estimates if Rows was unknown
            if self.rows <= 0:
                for divisor in [2, 4, 8]:
                    fallback_rows = max(1, rows // divisor)
                    tiff_bytes = self._build_tiff(
                        encoded_data, columns, fallback_rows,
                        compression, photometric
                    )
                    try:
                        img = Image.open(io.BytesIO(tiff_bytes))
                        img.load()
                        return self._pack_image_bits(img)
                    except Exception:
                        continue
            return None

    def _pack_image_bits(self, img):
        """Convert Pillow image to packed 1-bit PostScript data.

        Pillow mode '1' tobytes() returns packed bits (8 pixels/byte, MSB first,
        rows padded to byte boundaries) with CCITT convention: 0=white, 1=black.

        PostScript BlackIs1 polarity:
          BlackIs1=true: 1=black, 0=white — matches Pillow/CCITT, use as-is
          BlackIs1=false (default): 0=black, 1=white — inverted from CCITT
        """
        if img.mode != '1':
            img = img.convert('1')

        packed = img.tobytes()

        if not self.black_is_1:
            # Invert all bits: CCITT 0=white→1, CCITT 1=black→0
            packed = bytes(b ^ 0xFF for b in packed)

        return packed

    def _estimate_rows(self, encoded_data):
        """Estimate image height when Rows=0.

        Group 4 typically compresses to ~1/10 of raw size for text.
        Use a generous estimate — libtiff will stop at actual EOB.
        """
        raw_bits = len(encoded_data) * 8
        # Rough estimate: assume ~5:1 compression ratio
        estimated_pixels = raw_bits * 5
        rows = max(1, estimated_pixels // max(1, self.columns))
        # Clamp to a reasonable range
        return min(rows, 100000)

    def _build_tiff(self, encoded_data, columns, rows, compression, photometric):
        """Build a minimal TIFF file wrapping the CCITT encoded data."""
        # IFD tags to include
        tags = []

        # Tag format: (tag_id, type, count, value)
        # Types: 3=SHORT(2 bytes), 4=LONG(4 bytes)
        tags.append((256, 3, 1, columns))           # ImageWidth
        tags.append((257, 3, 1, rows))              # ImageLength
        tags.append((258, 3, 1, 1))                 # BitsPerSample
        tags.append((259, 3, 1, compression))       # Compression
        tags.append((262, 3, 1, photometric))       # PhotometricInterpretation
        tags.append((266, 3, 1, 1))                 # FillOrder: MSB first
        tags.append((277, 3, 1, 1))                 # SamplesPerPixel

        # RowsPerStrip
        tags.append((278, 4, 1, rows))              # RowsPerStrip

        # T4Options / T6Options
        if compression == 3:
            # T4Options: bit 0 = 2D encoding if K > 0
            t4_options = 1 if self.k > 0 else 0
            tags.append((292, 4, 1, t4_options))    # T4Options
        elif compression == 4:
            tags.append((293, 4, 1, 0))             # T6Options

        # Calculate offsets
        # Header: 8 bytes
        # IFD: 2 (count) + num_tags*12 + 4 (next IFD pointer)
        num_tags = len(tags) + 2  # +2 for StripOffsets and StripByteCounts
        ifd_offset = 8
        ifd_size = 2 + num_tags * 12 + 4
        data_offset = ifd_offset + ifd_size

        # Insert StripOffsets and StripByteCounts into tag list
        tags.append((273, 4, 1, data_offset))       # StripOffsets
        tags.append((279, 4, 1, len(encoded_data))) # StripByteCounts

        # Sort tags by tag ID (TIFF requires sorted IFD entries)
        tags.sort(key=lambda t: t[0])

        # Build binary TIFF
        buf = io.BytesIO()

        # TIFF header: byte order (little-endian) + magic + IFD offset
        buf.write(b'II')
        buf.write(struct.pack('<H', 42))
        buf.write(struct.pack('<I', ifd_offset))

        # IFD entry count
        buf.write(struct.pack('<H', len(tags)))

        # IFD entries
        for tag_id, tag_type, count, value in tags:
            buf.write(struct.pack('<H', tag_id))
            buf.write(struct.pack('<H', tag_type))
            buf.write(struct.pack('<I', count))
            if tag_type == 3:  # SHORT — value in first 2 bytes, pad with 2 zeros
                buf.write(struct.pack('<H', value))
                buf.write(b'\x00\x00')
            else:  # LONG
                buf.write(struct.pack('<I', value))

        # Next IFD pointer (0 = no more IFDs)
        buf.write(struct.pack('<I', 0))

        # CCITT encoded data
        buf.write(encoded_data)

        return buf.getvalue()
