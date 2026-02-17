# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
DCT ColorTransform utilities for PostScript DCT filters.

This module implements the color space transformations required by PostScript
DCTDecode and DCTEncode filters as specified in PLRM Section 3.17.

PLRM ColorTransform values:
- 0: No transformation
- 1: RGB⟷YUV (3 components) or CMYK⟷YUVK (4 components)
"""

import numpy as np


class DCTColorTransform:
    """PostScript DCT ColorTransform implementation per PLRM Section 3.17"""

    @staticmethod
    def rgb_to_yuv(rgb_data):
        """Convert RGB to YUV per PLRM specification.

        Args:
            rgb_data: NumPy array of RGB values shape (height, width, 3) with values 0-255

        Returns:
            NumPy array of YUV values shape (height, width, 3) with values 0-255

        Note:
            PLRM: "The RGB and YUV used here have nothing to do with the color
            spaces defined as part of the PostScript language's imaging model.
            The purpose of converting from RGB to YUV is to separate luminance
            and chrominance information."
        """
        if rgb_data.dtype != np.uint8:
            rgb_data = rgb_data.astype(np.uint8)

        # Convert to float for calculations
        rgb_float = rgb_data.astype(np.float32)

        # JPEG-standard RGB to YUV conversion matrix
        # Y  = 0.299*R + 0.587*G + 0.114*B
        # U  = -0.168736*R - 0.331264*G + 0.5*B + 128
        # V  = 0.5*R - 0.418688*G - 0.081312*B + 128
        transform_matrix = np.array([
            [ 0.299,     0.587,     0.114    ],
            [-0.168736, -0.331264,  0.5      ],
            [ 0.5,      -0.418688, -0.081312 ]
        ], dtype=np.float32)

        # Apply transformation
        yuv_float = np.dot(rgb_float, transform_matrix.T)

        # Add offsets for U and V components
        yuv_float[:, :, 1] += 128  # U offset
        yuv_float[:, :, 2] += 128  # V offset

        # Clamp to valid range and convert back to uint8
        yuv_float = np.clip(yuv_float, 0, 255)
        return yuv_float.astype(np.uint8)

    @staticmethod
    def yuv_to_rgb(yuv_data):
        """Convert YUV to RGB per PLRM specification.

        Args:
            yuv_data: NumPy array of YUV values shape (height, width, 3) with values 0-255

        Returns:
            NumPy array of RGB values shape (height, width, 3) with values 0-255
        """
        if yuv_data.dtype != np.uint8:
            yuv_data = yuv_data.astype(np.uint8)

        # Convert to float for calculations
        yuv_float = yuv_data.astype(np.float32)

        # Remove U and V offsets
        yuv_float[:, :, 1] -= 128  # U offset
        yuv_float[:, :, 2] -= 128  # V offset

        # JPEG-standard YUV to RGB conversion matrix (inverse of above)
        # R = Y + 1.402*V
        # G = Y - 0.344136*U - 0.714136*V
        # B = Y + 1.772*U
        transform_matrix = np.array([
            [1.0,  0.0,      1.402    ],
            [1.0, -0.344136, -0.714136],
            [1.0,  1.772,     0.0     ]
        ], dtype=np.float32)

        # Apply transformation
        rgb_float = np.dot(yuv_float, transform_matrix.T)

        # Clamp to valid range and convert back to uint8
        rgb_float = np.clip(rgb_float, 0, 255)
        return rgb_float.astype(np.uint8)

    @staticmethod
    def cmyk_to_yuvk(cmyk_data):
        """Convert CMYK to YUVK per PLRM specification.

        Args:
            cmyk_data: NumPy array of CMYK values shape (height, width, 4) with values 0-255

        Returns:
            NumPy array of YUVK values shape (height, width, 4) with values 0-255

        Note:
            PLRM: "If Colors is 4, transform CMYK values to YUVK before encoding
            and from YUVK to CMYK after decoding."

            For CMYK, only the first 3 components (CMY) are transformed to YUV,
            while the K component remains unchanged.
        """
        if cmyk_data.dtype != np.uint8:
            cmyk_data = cmyk_data.astype(np.uint8)

        # Split CMYK into CMY and K components
        cmy_data = cmyk_data[:, :, :3]  # CMY components
        k_data = cmyk_data[:, :, 3:4]   # K component (unchanged)

        # Transform CMY using same RGB->YUV transformation
        # (treating CMY as RGB for the mathematical transformation)
        yuv_data = DCTColorTransform.rgb_to_yuv(cmy_data)

        # Recombine YUV with unchanged K component
        yuvk_data = np.concatenate([yuv_data, k_data], axis=2)

        return yuvk_data

    @staticmethod
    def yuvk_to_cmyk(yuvk_data):
        """Convert YUVK to CMYK per PLRM specification.

        Args:
            yuvk_data: NumPy array of YUVK values shape (height, width, 4) with values 0-255

        Returns:
            NumPy array of CMYK values shape (height, width, 4) with values 0-255
        """
        if yuvk_data.dtype != np.uint8:
            yuvk_data = yuvk_data.astype(np.uint8)

        # Split YUVK into YUV and K components
        yuv_data = yuvk_data[:, :, :3]  # YUV components
        k_data = yuvk_data[:, :, 3:4]   # K component (unchanged)

        # Transform YUV using same YUV->RGB transformation
        # (producing CMY values in place of RGB)
        cmy_data = DCTColorTransform.yuv_to_rgb(yuv_data)

        # Recombine CMY with unchanged K component
        cmyk_data = np.concatenate([cmy_data, k_data], axis=2)

        return cmyk_data

    @staticmethod
    def should_apply_transform(colors, color_transform_param):
        """Determine if ColorTransform should be applied based on PLRM rules.

        Args:
            colors: Number of color components (1, 2, 3, or 4)
            color_transform_param: ColorTransform parameter value (0, 1, or None)

        Returns:
            bool: True if transform should be applied

        PLRM Rules:
        - Default ColorTransform is 1 if Colors is 3, 0 otherwise
        - ColorTransform is ignored if Colors is 1 or 2
        - Adobe marker codes can override the parameter
        """
        # ColorTransform is ignored for 1 or 2 component images
        if colors in (1, 2):
            return False

        # Use explicit parameter if provided
        if color_transform_param is not None:
            return bool(color_transform_param)

        # Default: apply transform for 3-component images, not for others
        return colors == 3

    @staticmethod
    def apply_encode_transform(image_data, colors, color_transform_param):
        """Apply ColorTransform during encoding if needed.

        Args:
            image_data: NumPy array of image samples
            colors: Number of color components
            color_transform_param: ColorTransform parameter

        Returns:
            NumPy array with transform applied (if appropriate)
        """
        if not DCTColorTransform.should_apply_transform(colors, color_transform_param):
            return image_data

        if colors == 3:
            return DCTColorTransform.rgb_to_yuv(image_data)
        elif colors == 4:
            return DCTColorTransform.cmyk_to_yuvk(image_data)
        else:
            return image_data

    @staticmethod
    def apply_decode_transform(image_data, colors, color_transform_param):
        """Apply ColorTransform during decoding if needed.

        Args:
            image_data: NumPy array of image samples
            colors: Number of color components
            color_transform_param: ColorTransform parameter

        Returns:
            NumPy array with transform applied (if appropriate)
        """
        if not DCTColorTransform.should_apply_transform(colors, color_transform_param):
            return image_data

        if colors == 3:
            return DCTColorTransform.yuv_to_rgb(image_data)
        elif colors == 4:
            return DCTColorTransform.yuvk_to_cmyk(image_data)
        else:
            return image_data