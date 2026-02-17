# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

# JPEG/DCT Filters
#
# Implements DCTDecode and DCTEncode filters per PLRM Section 3.13.
# Requires optional jpeglib and numpy dependencies.

from ..core import types as ps
from ..core import error as ps_error
from .filter import FilterBase

# DCT filter dependencies (with error handling)
try:
    import jpeglib
    import numpy as np
    from ..core import dct_transforms
    from ..core import dct_params
    DCTColorTransform = dct_transforms.DCTColorTransform
    DCTParameterParser = dct_params.DCTParameterParser
    DCT_AVAILABLE = True
    DCT_IMPORT_ERROR = None
except ImportError as e:
    jpeglib = None
    np = None
    DCTColorTransform = None
    DCTParameterParser = None
    DCT_AVAILABLE = False
    DCT_IMPORT_ERROR = str(e)


class DCTDecodeFilter(FilterBase):
    """DCTDecode filter - JPEG baseline decoding per PLRM Section 3.13"""

    def __init__(self, data_source, params=None):
        super().__init__(data_source, params)

        # Check for DCT filter availability
        if not DCT_AVAILABLE:
            self.available = False
            return

        self.available = True

        # Store parameters for later validation during usage
        # DCTDecode usually requires no parameters
        self.dct_params = None

        # State for JPEG processing
        self.jpeg_data_buffer = bytearray()
        self.decoded_data_buffer = bytearray()
        self.decoding_complete = False
        self.image_info = None

    def read_data(self, ctxt, max_bytes=None):
        """Decode JPEG data to raw image samples"""
        if self.eof_reached:
            return b''

        # Check for DCT availability
        if not self.available:
            print(f"DCT filters require jpeglib. Install with: pip install jpeglib")
            print(f"Error: {DCT_IMPORT_ERROR}")
            return ps_error.e(ctxt, ps_error.IOERROR, "DCTDecode")

        # Parameter validation not needed for DCTDecode (deferred to usage)

        try:
            # If we haven't decoded yet, read all JPEG data and decode
            if not self.decoding_complete:
                self._read_and_decode_jpeg(ctxt)

            # Return decoded data in chunks
            if not self.decoded_data_buffer:
                self.eof_reached = True
                return b''

            # Determine how much data to return
            bytes_to_return = max_bytes or min(1024, len(self.decoded_data_buffer))
            bytes_to_return = min(bytes_to_return, len(self.decoded_data_buffer))

            result = bytes(self.decoded_data_buffer[:bytes_to_return])
            self.decoded_data_buffer = self.decoded_data_buffer[bytes_to_return:]

            if not self.decoded_data_buffer:
                self.eof_reached = True

            return result

        except Exception as e:
            return ps_error.e(ctxt, ps_error.IOERROR, "DCTDecode")

    def _read_and_decode_jpeg(self, ctxt):
        """Read all JPEG data from source and decode it"""
        # Read all JPEG data from data source
        while not self.data_source.at_eof():
            chunk = self.data_source.read_data(ctxt, 8192)

            if not chunk:
                break
            self.jpeg_data_buffer.extend(chunk)

            # Safety check: limit total JPEG data size (64MB max)
            if len(self.jpeg_data_buffer) > 64 * 1024 * 1024:
                break

        if not self.jpeg_data_buffer:
            self.eof_reached = True
            return

        # Decode JPEG using jpeglib
        jpeg_bytes = bytes(self.jpeg_data_buffer)

        try:
            # Use jpeglib to decode JPEG to spatial (RGB) data
            import tempfile
            import os

            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp_file:
                temp_file.write(jpeg_bytes)
                temp_filename = temp_file.name

            try:
                jpeg_obj = jpeglib.read_spatial(temp_filename)

                # Adobe APP14 marker parsing for ColorTransform
                color_transform_from_adobe = self._parse_adobe_app14_marker(jpeg_obj)

                # Extract spatial data from SpatialJPEG object
                if hasattr(jpeg_obj, 'spatial'):
                    image = jpeg_obj.spatial
                elif hasattr(jpeg_obj, 'Y') and hasattr(jpeg_obj, 'Cb') and hasattr(jpeg_obj, 'Cr'):
                    # YUV components - combine them
                    image = np.stack([jpeg_obj.Y, jpeg_obj.Cb, jpeg_obj.Cr], axis=-1)
                elif hasattr(jpeg_obj, 'data'):
                    image = jpeg_obj.data
                else:
                    # Try to convert to array directly
                    image = np.array(jpeg_obj)

            finally:
                # Clean up temporary file
                if os.path.exists(temp_filename):
                    os.unlink(temp_filename)

        except Exception as e:
            # JPEG decoding failed - probably invalid JPEG data
            raise ValueError("Invalid JPEG data")

        # Get image information
        height, width = image.shape[:2]
        components = image.shape[2] if len(image.shape) > 2 else 1

        self.image_info = {
            'width': width,
            'height': height,
            'components': components
        }

        # Ensure image is uint8
        if image.dtype != np.uint8:
            image = image.astype(np.uint8)

        # Apply ColorTransform if needed
        color_transform_param = self.dct_params.color_transform if self.dct_params else None

        # Check for Adobe marker override
        effective_color_transform = color_transform_param

        # jpeglib automatically converts JPEG YUV to RGB spatial data
        # Only apply ColorTransform if explicitly requested or if JPEG was stored in RGB
        jpeg_was_yuv = (jpeg_obj.jpeg_color_space.name == 'JCS_YCbCr')

        # If JPEG was YUV and jpeglib converted to RGB, don't apply additional transform
        # unless explicitly requested via ColorTransform parameter
        should_transform = False
        if not jpeg_was_yuv:
            # JPEG stored in RGB - apply default PostScript ColorTransform logic
            should_transform = DCTColorTransform.should_apply_transform(components, effective_color_transform)
        elif effective_color_transform is not None:
            # ColorTransform explicitly set - honor it regardless of JPEG format
            should_transform = bool(effective_color_transform)

        if should_transform:
            image = DCTColorTransform.apply_decode_transform(
                image, components, effective_color_transform)

        # Convert to interleaved byte stream (PLRM requirement)
        # PostScript expects samples interleaved on per-sample basis
        if len(image.shape) == 3:
            # Multi-component image: flatten to interleaved format
            decoded_bytes = image.tobytes()
        else:
            # Single component: already in correct format
            decoded_bytes = image.tobytes()


        self.decoded_data_buffer.extend(decoded_bytes)
        self.decoding_complete = True

    def _parse_adobe_app14_marker(self, jpeg_obj):
        """
        Parse Adobe APP14 marker to extract ColorTransform value.

        Args:
            jpeg_obj: jpeglib SpatialJPEG object

        Returns:
            int or None: ColorTransform value (0, 1, 2) or None if no Adobe marker
        """
        try:
            # Check if jpeglib provides marker access
            if not hasattr(jpeg_obj, 'markers'):
                return None

            # Look for Adobe APP14 marker
            for marker in jpeg_obj.markers:
                # Check if this is an APP14 marker
                marker_type = str(marker).upper()
                if 'APP14' in marker_type or 'JPEG_APP14' in marker_type:
                    # Extract marker data
                    marker_data = self._get_marker_data(marker)
                    if marker_data and len(marker_data) >= 12:
                        # Parse Adobe APP14 marker structure
                        color_transform = self._parse_adobe_marker_data(marker_data)
                        return color_transform

            return None

        except Exception as e:
            # Don't fail if marker parsing has issues, just return None
            return None

    def _get_marker_data(self, marker):
        """Extract raw data from jpeglib marker object"""
        try:
            # jpeglib Marker objects have .content attribute with bytes
            if hasattr(marker, 'content'):
                return marker.content
            elif hasattr(marker, 'data'):
                return marker.data
            elif hasattr(marker, 'bytes'):
                return marker.bytes
            else:
                return None
        except Exception as e:
            return None

    def _parse_adobe_marker_data(self, marker_data):
        """
        Parse Adobe APP14 marker data structure.

        Adobe APP14 marker format:
        - Bytes 0-4: "Adobe" identifier
        - Bytes 5-6: Version (typically 100)
        - Bytes 7-8: Flags0
        - Bytes 9-10: Flags1
        - Byte 11: ColorTransform (0=none, 1=YUV/YUVK, 2=YCCK)

        Args:
            marker_data: Raw marker data bytes

        Returns:
            int or None: ColorTransform value
        """
        try:
            if len(marker_data) < 12:
                return None

            # Check for "Adobe" identifier at start
            if marker_data[:5] != b'Adobe':
                return None

            # Extract ColorTransform value (byte 11, 0-indexed)
            color_transform = marker_data[11]

            # Validate ColorTransform value
            if color_transform in (0, 1, 2):
                return color_transform
            else:
                return None

        except Exception as e:
            return None


class DCTEncodeFilter(FilterBase):
    """DCTEncode filter - JPEG baseline encoding per PLRM Section 3.13"""

    def __init__(self, data_target, params):
        super().__init__(data_target, params)

        # Check for DCT filter availability
        if not DCT_AVAILABLE:
            self.available = False
            return

        self.available = True

        # Extract parameters (validation already done in ps_filter)
        param_dict = params.val
        self.columns = param_dict[b'Columns']
        self.rows = param_dict[b'Rows']
        self.colors = param_dict[b'Colors']

        # Calculate exact data requirements
        columns_val = self.columns.val if hasattr(self.columns, 'val') else self.columns
        rows_val = self.rows.val if hasattr(self.rows, 'val') else self.rows
        colors_val = self.colors.val if hasattr(self.colors, 'val') else self.colors
        self.required_bytes = columns_val * rows_val * colors_val

        # State for JPEG encoding
        self.bytes_received = 0
        self.image_buffer = bytearray()
        self.encoding_complete = False

    def write_data(self, ctxt, data):
        """Buffer image data and encode when complete"""
        # Check for DCT availability
        if not self.available:
            print(f"DCT filters require jpeglib. Install with: pip install jpeglib")
            print(f"Error: {DCT_IMPORT_ERROR}")
            return ps_error.e(ctxt, ps_error.IOERROR, "DCTEncode")

        # Parameters already validated in ps_filter

        # Check if already complete
        if self.encoding_complete:
            return ps_error.e(ctxt, ps_error.IOERROR, "DCTEncode")

        try:
            # Buffer incoming data
            self.image_buffer.extend(data)
            self.bytes_received += len(data)

            # PLRM: DCTEncode requires exact byte count
            if self.bytes_received > self.required_bytes:
                return ps_error.e(ctxt, ps_error.IOERROR, "DCTEncode")

            # Encode when we have exactly the right amount
            if self.bytes_received == self.required_bytes:
                return self._encode_and_write(ctxt)

            # Still waiting for more data
            return None

        except Exception as e:
            return ps_error.e(ctxt, ps_error.IOERROR, "DCTEncode")

    def _encode_and_write(self, ctxt):
        """Encode buffered image data to JPEG"""
        try:
            # Reshape raw bytes to image array
            image_data = np.frombuffer(self.image_buffer, dtype=np.uint8)

            # Reshape to proper dimensions
            if self.dct_params.colors == 1:
                # Grayscale
                image_array = image_data.reshape(
                    (self.dct_params.rows, self.dct_params.columns))
            else:
                # Multi-component (RGB, CMYK, etc.)
                image_array = image_data.reshape(
                    (self.dct_params.rows, self.dct_params.columns, self.dct_params.colors))

            # Apply ColorTransform if needed (before encoding)
            if DCTColorTransform.should_apply_transform(
                self.dct_params.colors, self.dct_params.color_transform):
                image_array = DCTColorTransform.apply_encode_transform(
                    image_array, self.dct_params.colors, self.dct_params.color_transform)

            # Prepare jpeglib encoding parameters
            encode_params = self._prepare_jpeglib_params()

            # Use jpeglib to encode
            if self.dct_params.colors == 1:
                # Grayscale encoding
                jpeg_data = jpeglib.from_spatial(
                    image_array,
                    colorspace=jpeglib.JCS_GRAYSCALE,
                    **encode_params
                )
            elif self.dct_params.colors == 3:
                # RGB encoding
                jpeg_data = jpeglib.from_spatial(
                    image_array,
                    colorspace=jpeglib.JCS_RGB,
                    **encode_params
                )
            elif self.dct_params.colors == 4:
                # CMYK encoding
                jpeg_data = jpeglib.from_spatial(
                    image_array,
                    colorspace=jpeglib.JCS_CMYK,
                    **encode_params
                )
            else:
                # 2-component not directly supported by JPEG
                return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")

            # Write JPEG data to target
            jpeg_bytes = jpeg_data.write()
            result = self._write_to_target(ctxt, jpeg_bytes)

            self.encoding_complete = True
            return result

        except Exception as e:
            return ps_error.e(ctxt, ps_error.IOERROR, "DCTEncode")

    def _prepare_jpeglib_params(self):
        """Prepare jpeglib encoding parameters from PostScript parameters"""
        params = {}

        # Quality factor
        if self.dct_params.qfactor != 1.0:
            # Convert QFactor to quality (simplified mapping)
            quality = max(1, min(100, int(50 / self.dct_params.qfactor)))
            params['quality'] = quality

        # Sampling factors
        if self.dct_params.hsample or self.dct_params.vsample:
            # jpeglib may support subsampling - check documentation
            # For now, log that custom sampling was requested
            pass

        # Custom quantization tables
        if self.dct_params.quant_tables:
            # jpeglib supports custom quantization tables
            # This would require converting PostScript tables to jpeglib format
            pass

        # Custom Huffman tables
        if self.dct_params.huff_tables:
            # jpeglib supports custom Huffman tables
            # This would require converting PostScript tables to jpeglib format
            pass

        return params

    def _write_to_target(self, ctxt, jpeg_bytes):
        """Write JPEG bytes to data target"""
        # Write to underlying data target
        # This depends on the target type (file, string, procedure)

        # For now, simplified implementation
        # Full implementation would handle different target types
        if hasattr(self.data_source, 'write'):
            self.data_source.write(jpeg_bytes)

        return None

    def close(self, ctxt):
        """Close **filter** and ensure all data is flushed"""
        if not self.encoding_complete and self.bytes_received > 0:
            # Data was provided but encoding not complete
            if self.bytes_received != self.required_bytes:
                return ps_error.e(ctxt, ps_error.IOERROR, "DCTEncode")

        super().close(ctxt)
