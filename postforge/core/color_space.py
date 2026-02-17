# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript Color Space System - Core Infrastructure

This module provides the core color space management and conversion functionality
for PostForge's PostScript Level 2 color system implementation.

Architecture: PostScript execution infrastructure belongs in core/
PLRM: Sections 4.8 (Color Spaces) and 6.2 (Device Color Conversions)
"""

import math as _math

from . import icc_default
from . import icc_profile
from . import types as ps
from typing import List, Tuple, Union, Optional


class ColorSpaceEngine:
    """
    Core color space management and conversion functionality.
    
    Implements PLRM Section 6.2 device color conversion algorithms:
    - RGB ↔ Gray conversion (Section 6.2.1)
    - CMYK ↔ Gray conversion (Section 6.2.2) 
    - RGB → CMYK conversion (Section 6.2.3)
    - CMYK → RGB conversion (Section 6.2.4)
    - HSB ↔ RGB conversion (hexcone model)
    """
    
    # PostScript Level 2 device color spaces
    DEVICE_SPACES = {"DeviceGray", "DeviceRGB", "DeviceCMYK"}
    
    # Component counts per color space
    COMPONENT_COUNTS = {
        "DeviceGray": 1,
        "DeviceRGB": 3, 
        "DeviceCMYK": 4
    }
    
    # Default initial colors per color space (PLRM specifications)
    DEFAULT_COLORS = {
        "DeviceGray": [0.0],               # Black
        "DeviceRGB": [0.0, 0.0, 0.0],      # Black
        "DeviceCMYK": [0.0, 0.0, 0.0, 1.0], # Black (full black component)
        "Indexed": [0.0],                    # Index 0 (resolved during setcolorspace)
        "CIEBasedABC": [0.0, 0.0, 0.0],    # Black (3 components, resolved during setcolorspace)
        "CIEBasedA": [0.0],                 # Black (1 component, resolved during setcolorspace)
        "CIEBasedDEF": [0.0, 0.0, 0.0],    # Black (3 components, resolved during setcolorspace)
        "CIEBasedDEFG": [0.0, 0.0, 0.0, 1.0],  # Black (4 components, resolved during setcolorspace)
    }
    
    @classmethod
    def validate_color_space(cls, space_array: List) -> bool:
        """
        Validate color space array format per PLRM.
        
        Args:
            space_array: Color space array like ["DeviceRGB"] or ["/Pattern", ["/DeviceRGB"]]
            
        Returns:
            True if valid color space format, False otherwise
            
        Reference: PLRM Section 4.8, color space specifications
        """
        if not isinstance(space_array, list) or len(space_array) == 0:
            return False
        
        # First element must be color space name
        space_name = space_array[0]
        if isinstance(space_name, ps.Name):
            space_name = space_name.val.decode('ascii') if isinstance(space_name.val, bytes) else space_name.val
        
        # Check if it's a supported device space
        if space_name in cls.DEVICE_SPACES:
            return len(space_array) == 1  # Device spaces are single-element arrays

        # Separation: [/Separation name alternativeSpace tintTransform]
        if space_name == "Separation":
            return len(space_array) == 4

        # DeviceN: [/DeviceN names alternativeSpace tintTransform]
        if space_name == "DeviceN":
            return len(space_array) == 4

        # Indexed: [/Indexed base hival lookup]
        if space_name == "Indexed":
            return len(space_array) == 4

        # CIE-based spaces
        if space_name in ("CIEBasedABC", "CIEBasedA", "CIEBasedDEF", "CIEBasedDEFG"):
            return len(space_array) == 2

        # ICCBased: [/ICCBased stream]
        if space_name == "ICCBased":
            return len(space_array) == 2

        # Pattern: [/Pattern] or [/Pattern underlyingSpace]
        if space_name == "Pattern":
            return len(space_array) in (1, 2)

        return False
    
    @classmethod
    def get_component_count(cls, space_array: List) -> int:
        """
        Get expected component count for color space.

        Args:
            space_array: Color space array like ["DeviceRGB"]

        Returns:
            Number of color components required for this space

        Raises:
            ValueError: If color space is invalid or unsupported
        """
        if not space_array or not isinstance(space_array, list):
            raise ValueError(f"Invalid color space: {space_array}")

        space_name = space_array[0]
        if isinstance(space_name, ps.Name):
            space_name = space_name.val.decode('ascii') if isinstance(space_name.val, bytes) else space_name.val

        # Device color spaces
        if space_name in cls.COMPONENT_COUNTS:
            return cls.COMPONENT_COUNTS[space_name]

        # Separation: always 1 tint component
        if space_name == "Separation":
            return 1

        # DeviceN: number of colorants in the names array (element 1)
        if space_name == "DeviceN" and len(space_array) >= 2:
            names = space_array[1]
            if hasattr(names, 'length'):
                return names.length
            if hasattr(names, 'val') and isinstance(names.val, (list, tuple)):
                return len(names.val)
            return 0

        # Indexed: always 1 index component
        if space_name == "Indexed":
            return 1

        # CIE-based spaces
        if space_name == "CIEBasedA":
            return 1
        if space_name in ("CIEBasedABC", "CIEBasedDEF"):
            return 3
        if space_name == "CIEBasedDEFG":
            return 4

        # ICCBased: component count from the stream's /N entry
        if space_name == "ICCBased" and len(space_array) >= 2:
            stream = space_array[1]
            if hasattr(stream, 'val') and isinstance(stream.val, dict):
                n_obj = stream.val.get(b'N')
                if n_obj and hasattr(n_obj, 'val'):
                    return int(n_obj.val)
            return 3  # Fallback

        # Pattern: 0 components (pattern colors use underlying space)
        if space_name == "Pattern":
            return 0

        raise ValueError(f"Unsupported color space: {space_name}")
    
    @classmethod
    def resolve_iccbased_space(cls, space_array):
        """Resolve ICCBased to its alternate/fallback device space name.

        Checks the /Alternate key first; falls back to inferring from /N.
        Tier 2 will replace this with actual ICC profile processing.

        Args:
            space_array: Color space array like ["ICCBased", stream_obj]

        Returns:
            Device space name string (e.g. "DeviceRGB")
        """
        if len(space_array) < 2:
            return "DeviceRGB"

        stream = space_array[1]
        stream_dict = stream.val if hasattr(stream, 'val') and isinstance(stream.val, dict) else {}

        # Check /Alternate key
        alt = stream_dict.get(b'Alternate')
        if alt is not None:
            if hasattr(alt, 'TYPE') and alt.TYPE == ps.T_NAME:
                name = alt.val.decode('ascii') if isinstance(alt.val, bytes) else alt.val
                if name in cls.DEVICE_SPACES:
                    return name

        # Fallback: infer from /N
        n = cls.get_component_count(space_array)
        return {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(n, "DeviceRGB")

    @classmethod
    def get_default_color(cls, space_array: List) -> List[float]:
        """
        Get default initial color for color space per PLRM.

        Args:
            space_array: Color space array like ["DeviceRGB"]

        Returns:
            List of default color component values

        Reference: PLRM Section 4.8, setcolorspace operator behavior
        """
        if not cls.validate_color_space(space_array):
            return [0.0]  # Fallback to gray

        space_name = space_array[0]
        if isinstance(space_name, ps.Name):
            space_name = space_name.val.decode('ascii') if isinstance(space_name.val, bytes) else space_name.val

        # ICCBased: dynamic default based on /N
        if space_name == "ICCBased":
            n = cls.get_component_count(space_array)
            if n == 4:
                return [0.0, 0.0, 0.0, 1.0]  # CMYK black
            return [0.0] * n

        return cls.DEFAULT_COLORS.get(space_name, [0.0]).copy()

    # ==========================================================================
    # Device Color Conversion Algorithms (PLRM Section 6.2)
    # ==========================================================================

    @staticmethod
    def rgb_to_gray(red: float, green: float, blue: float) -> float:
        """
        Convert RGB to gray using NTSC video standard.
        
        Args:
            red, green, blue: RGB components (0.0-1.0)
            
        Returns:
            Gray value (0.0-1.0)
            
        Reference: PLRM Section 6.2.1
        Formula: gray = 0.3 * red + 0.59 * green + 0.11 * blue
        """
        return 0.3 * red + 0.59 * green + 0.11 * blue
    
    @staticmethod
    def gray_to_rgb(gray: float) -> Tuple[float, float, float]:
        """
        Convert gray to RGB (all components equal).
        
        Args:
            gray: Gray value (0.0-1.0)
            
        Returns:
            Tuple of (red, green, blue) values
            
        Reference: PLRM Section 6.2.1
        Formula: red = green = blue = gray
        """
        return (gray, gray, gray)
    
    @staticmethod
    def cmyk_to_gray(cyan: float, magenta: float, yellow: float, black: float) -> float:
        """
        Convert CMYK to gray.
        
        Args:
            cyan, magenta, yellow, black: CMYK components (0.0-1.0)
            
        Returns:
            Gray value (0.0-1.0)
            
        Reference: PLRM Section 6.2.2  
        Formula: gray = 1.0 - min(1.0, 0.3*cyan + 0.59*magenta + 0.11*yellow + black)
        """
        return 1.0 - min(1.0, 0.3 * cyan + 0.59 * magenta + 0.11 * yellow + black)
    
    @staticmethod
    def gray_to_cmyk(gray: float) -> Tuple[float, float, float, float]:
        """
        Convert gray to CMYK.
        
        Args:
            gray: Gray value (0.0-1.0)
            
        Returns:
            Tuple of (cyan, magenta, yellow, black) values
            
        Reference: PLRM Section 6.2.2
        Formula: cyan = magenta = yellow = 0.0, black = 1.0 - gray
        """
        return (0.0, 0.0, 0.0, 1.0 - gray)
    
    @staticmethod
    def rgb_to_cmyk(red: float, green: float, blue: float, 
                    bg_func: Optional = None, ucr_func: Optional = None) -> Tuple[float, float, float, float]:
        """
        Convert RGB to CMYK with black generation and undercolor removal.
        
        Args:
            red, green, blue: RGB components (0.0-1.0)
            bg_func: Black generation function (optional, defaults to identity)
            ucr_func: Undercolor removal function (optional, defaults to identity)
            
        Returns:
            Tuple of (cyan, magenta, yellow, black) values
            
        Reference: PLRM Section 6.2.3
        
        Algorithm:
        1. Basic CMY conversion: c = 1-r, m = 1-g, y = 1-b  
        2. Calculate k = min(c, m, y)
        3. Apply black generation: black = BG(k)
        4. Apply undercolor removal: c -= UCR(k), m -= UCR(k), y -= UCR(k)
        5. Clamp all values to [0, 1]
        """
        # Step 1: Basic CMY conversion
        c = 1.0 - red
        m = 1.0 - green  
        y = 1.0 - blue
        
        # Step 2: Calculate nominal black amount
        k = min(c, m, y)
        
        # Step 3: Apply black generation function (default: identity)
        if bg_func is not None:
            # TODO: Implement PostScript procedure calling for custom BG functions
            black = bg_func(k)
        else:
            black = k  # Default behavior
        
        # Step 4: Apply undercolor removal function (default: identity)
        if ucr_func is not None:
            # TODO: Implement PostScript procedure calling for custom UCR functions
            ucr_amount = ucr_func(k)
        else:
            ucr_amount = k  # Default behavior
            
        # Step 5: Final CMYK calculation with clamping
        cyan = max(0.0, min(1.0, c - ucr_amount))
        magenta = max(0.0, min(1.0, m - ucr_amount))
        yellow = max(0.0, min(1.0, y - ucr_amount))
        black = max(0.0, min(1.0, black))
        
        return (cyan, magenta, yellow, black)
    
    @staticmethod
    def cmyk_to_rgb(cyan: float, magenta: float, yellow: float, black: float) -> Tuple[float, float, float]:
        """
        Convert CMYK to RGB.
        
        Args:
            cyan, magenta, yellow, black: CMYK components (0.0-1.0)
            
        Returns:
            Tuple of (red, green, blue) values
            
        Reference: PLRM Section 6.2.4
        
        Algorithm:
        1. Add black to each color component
        2. Convert to RGB by subtracting from 1.0
        3. Clamp to [0, 1] range
        """
        red = 1.0 - min(1.0, cyan + black)
        green = 1.0 - min(1.0, magenta + black)
        blue = 1.0 - min(1.0, yellow + black)
        
        return (red, green, blue)

    # ==========================================================================
    # HSB ↔ RGB Conversion (Hexcone Model)
    # ==========================================================================
    
    @staticmethod
    def hsb_to_rgb(hue: float, saturation: float, brightness: float) -> Tuple[float, float, float]:
        """
        Convert HSB to RGB using hexcone model.
        
        Args:
            hue: Hue (0.0-1.0) - 0=red, 1/3=green, 2/3=blue, 1=red again
            saturation: Saturation (0.0-1.0) - 0=gray, 1=pure color  
            brightness: Brightness (0.0-1.0) - 0=black, 1=maximum brightness
            
        Returns:
            Tuple of (red, green, blue) values in range 0.0-1.0
            
        Reference: PLRM Section 6.2.1 and hexcone color model
        Note: This is the same algorithm implemented in graphics_state.py _hsb_to_rgb()
        """
        # Handle special cases
        if saturation == 0.0:
            # Achromatic (gray) - no hue
            return (brightness, brightness, brightness)
        
        if brightness == 0.0:
            # Always black regardless of hue/saturation
            return (0.0, 0.0, 0.0)
        
        # Normalize hue to [0, 6) range for hexcone sectors
        h = hue * 6.0
        if h >= 6.0:
            h = 0.0  # Wrap around
        
        # Determine which sector of the hexcone we're in
        sector = int(h)
        fractional = h - sector
        
        # Calculate intermediate values
        p = brightness * (1.0 - saturation)                        # Minimum component value
        q = brightness * (1.0 - saturation * fractional)           # Decreasing component
        t = brightness * (1.0 - saturation * (1.0 - fractional))   # Increasing component
        
        # Assign RGB based on hexcone sector
        if sector == 0:    # Red to Yellow
            return (brightness, t, p)
        elif sector == 1:  # Yellow to Green  
            return (q, brightness, p)
        elif sector == 2:  # Green to Cyan
            return (p, brightness, t)
        elif sector == 3:  # Cyan to Blue
            return (p, q, brightness)
        elif sector == 4:  # Blue to Magenta
            return (t, p, brightness)
        else:              # Magenta to Red (sector == 5)
            return (brightness, p, q)
    
    @staticmethod
    def rgb_to_hsb(red: float, green: float, blue: float) -> Tuple[float, float, float]:
        """
        Convert RGB to HSB using hexcone model.
        
        Args:
            red, green, blue: RGB components (0.0-1.0)
            
        Returns:
            Tuple of (hue, saturation, brightness) values in range 0.0-1.0
            
        Reference: Standard RGB to HSB conversion algorithm
        """
        max_val = max(red, green, blue)
        min_val = min(red, green, blue)
        diff = max_val - min_val
        
        # Brightness is the maximum component
        brightness = max_val
        
        # Saturation
        if max_val == 0.0:
            saturation = 0.0
        else:
            saturation = diff / max_val
        
        # Hue
        if diff == 0.0:
            hue = 0.0  # Undefined, but we use 0
        elif max_val == red:
            hue = (green - blue) / diff
            if hue < 0:
                hue += 6.0
        elif max_val == green:
            hue = (blue - red) / diff + 2.0
        else:  # max_val == blue
            hue = (red - green) / diff + 4.0
        
        # Convert from [0, 6) to [0, 1)
        hue = hue / 6.0
        
        return (hue, saturation, brightness)

    # ==========================================================================
    # CIE-Based Color Space Conversions
    # ==========================================================================

    # sRGB D65 XYZ → linear RGB matrix (IEC 61966-2-1)
    _XYZ_TO_LRGB = (
        ( 3.2404542, -1.5371385, -0.4985314),
        (-0.9692660,  1.8760108,  0.0415560),
        ( 0.0556434, -0.2040259,  1.0572252),
    )

    @staticmethod
    def _srgb_gamma(u):
        """Apply sRGB companding (linear → gamma-corrected)."""
        if u <= 0.0031308:
            return 12.92 * u
        return 1.055 * (u ** (1.0 / 2.4)) - 0.055

    @classmethod
    def cie_abc_to_rgb(cls, components, cie_dict):
        """
        Convert CIEBasedABC color to sRGB.

        Pipeline: input → DecodeABC → MatrixABC → DecodeLMN → MatrixLMN → XYZ → sRGB

        Args:
            components: list of 3 floats (A, B, C)
            cie_dict: the CIE dictionary (Python dict with bytes keys)

        Returns:
            (r, g, b) tuple with values in [0, 1]
        """
        a, b, c = (float(components[i]) if i < len(components) else 0.0 for i in range(3))

        # RangeABC clamp
        range_abc = _get_cie_float_array(cie_dict, b"RangeABC", [0, 1, 0, 1, 0, 1])
        a = max(range_abc[0], min(range_abc[1], a))
        b = max(range_abc[2], min(range_abc[3], b))
        c = max(range_abc[4], min(range_abc[5], c))

        # DecodeABC — apply procedures if present
        a, b, c = _apply_decode_array(cie_dict, b"DecodeABC", [a, b, c])

        # MatrixABC (column-major 3×3, default identity)
        mat_abc = _get_cie_float_array(cie_dict, b"MatrixABC",
                                       [1, 0, 0, 0, 1, 0, 0, 0, 1])
        lmn = [
            mat_abc[0] * a + mat_abc[3] * b + mat_abc[6] * c,
            mat_abc[1] * a + mat_abc[4] * b + mat_abc[7] * c,
            mat_abc[2] * a + mat_abc[5] * b + mat_abc[8] * c,
        ]

        # RangeLMN clamp
        range_lmn = _get_cie_float_array(cie_dict, b"RangeLMN", [0, 1, 0, 1, 0, 1])
        for i in range(3):
            lmn[i] = max(range_lmn[i * 2], min(range_lmn[i * 2 + 1], lmn[i]))

        # DecodeLMN — apply procedures if present
        lmn = _apply_decode_array(cie_dict, b"DecodeLMN", lmn)

        # MatrixLMN (column-major 3×3, default identity)
        mat_lmn = _get_cie_float_array(cie_dict, b"MatrixLMN",
                                       [1, 0, 0, 0, 1, 0, 0, 0, 1])
        xyz = [
            mat_lmn[0] * lmn[0] + mat_lmn[3] * lmn[1] + mat_lmn[6] * lmn[2],
            mat_lmn[1] * lmn[0] + mat_lmn[4] * lmn[1] + mat_lmn[7] * lmn[2],
            mat_lmn[2] * lmn[0] + mat_lmn[5] * lmn[1] + mat_lmn[8] * lmn[2],
        ]

        # XYZ → linear sRGB
        m = cls._XYZ_TO_LRGB
        lr = m[0][0] * xyz[0] + m[0][1] * xyz[1] + m[0][2] * xyz[2]
        lg = m[1][0] * xyz[0] + m[1][1] * xyz[1] + m[1][2] * xyz[2]
        lb = m[2][0] * xyz[0] + m[2][1] * xyz[1] + m[2][2] * xyz[2]

        # Clamp and apply sRGB gamma
        r = max(0.0, min(1.0, cls._srgb_gamma(max(0.0, lr))))
        g = max(0.0, min(1.0, cls._srgb_gamma(max(0.0, lg))))
        b_ = max(0.0, min(1.0, cls._srgb_gamma(max(0.0, lb))))

        return (r, g, b_)

    @classmethod
    def cie_a_to_rgb(cls, component, cie_dict):
        """
        Convert CIEBasedA color to sRGB.

        Pipeline: input → DecodeA → MatrixA → DecodeLMN → MatrixLMN → XYZ → sRGB

        Args:
            component: float value (single A component)
            cie_dict: the CIE dictionary (Python dict with bytes keys)

        Returns:
            (r, g, b) tuple with values in [0, 1]
        """
        a = float(component)

        # RangeA clamp
        range_a = _get_cie_float_array(cie_dict, b"RangeA", [0, 1])
        a = max(range_a[0], min(range_a[1], a))

        # DecodeA — apply procedure if present
        decoded = _apply_decode_array(cie_dict, b"DecodeA", [a])
        a = decoded[0]

        # MatrixA: 3-element vector, default [1 1 1] (maps A to equal L, M, N)
        mat_a = _get_cie_float_array(cie_dict, b"MatrixA", [1, 1, 1])
        lmn = [mat_a[0] * a, mat_a[1] * a, mat_a[2] * a]

        # RangeLMN clamp
        range_lmn = _get_cie_float_array(cie_dict, b"RangeLMN", [0, 1, 0, 1, 0, 1])
        for i in range(3):
            lmn[i] = max(range_lmn[i * 2], min(range_lmn[i * 2 + 1], lmn[i]))

        # DecodeLMN — apply procedures if present
        lmn = _apply_decode_array(cie_dict, b"DecodeLMN", lmn)

        # MatrixLMN
        mat_lmn = _get_cie_float_array(cie_dict, b"MatrixLMN",
                                       [1, 0, 0, 0, 1, 0, 0, 0, 1])
        xyz = [
            mat_lmn[0] * lmn[0] + mat_lmn[3] * lmn[1] + mat_lmn[6] * lmn[2],
            mat_lmn[1] * lmn[0] + mat_lmn[4] * lmn[1] + mat_lmn[7] * lmn[2],
            mat_lmn[2] * lmn[0] + mat_lmn[5] * lmn[1] + mat_lmn[8] * lmn[2],
        ]

        # XYZ → linear sRGB
        m = cls._XYZ_TO_LRGB
        lr = m[0][0] * xyz[0] + m[0][1] * xyz[1] + m[0][2] * xyz[2]
        lg = m[1][0] * xyz[0] + m[1][1] * xyz[1] + m[1][2] * xyz[2]
        lb = m[2][0] * xyz[0] + m[2][1] * xyz[1] + m[2][2] * xyz[2]

        r = max(0.0, min(1.0, cls._srgb_gamma(max(0.0, lr))))
        g = max(0.0, min(1.0, cls._srgb_gamma(max(0.0, lg))))
        b_ = max(0.0, min(1.0, cls._srgb_gamma(max(0.0, lb))))

        return (r, g, b_)

    @classmethod
    def cie_def_to_rgb(cls, components, cie_dict):
        """
        Convert CIEBasedDEF color to sRGB.

        Pipeline: input → RangeDEF clamp → Table lookup → ABC → cie_abc_to_rgb

        The Table is a 3D lookup table [m1 m2 m3 [strings...]] that maps
        DEF values to ABC values. Each string has m2*m3*3 bytes.

        Args:
            components: list of 3 floats (D, E, F)
            cie_dict: the CIE dictionary (Python dict with bytes keys)

        Returns:
            (r, g, b) tuple with values in [0, 1]
        """
        d, e, f = (float(components[i]) if i < len(components) else 0.0 for i in range(3))

        # RangeDEF clamp (default [0 1 0 1 0 1])
        range_def = _get_cie_float_array(cie_dict, b"RangeDEF", [0, 1, 0, 1, 0, 1])
        d = max(range_def[0], min(range_def[1], d))
        e = max(range_def[2], min(range_def[3], e))
        f = max(range_def[4], min(range_def[5], f))

        # Table lookup: DEF → ABC (byte values scaled to RangeABC per PLRM)
        table_obj = cie_dict.get(b"Table")
        range_abc = _get_cie_float_array(cie_dict, b"RangeABC", [0, 1, 0, 1, 0, 1])
        if table_obj and hasattr(table_obj, 'val') and len(table_obj.val) >= 4:
            abc = _table_3d_lookup(table_obj, d, e, f, range_def, range_abc)
        else:
            # No Table — pass DEF directly as ABC (fallback)
            abc = [d, e, f]

        # Continue through the standard ABC → XYZ → sRGB pipeline
        return cls.cie_abc_to_rgb(abc, cie_dict)

    @classmethod
    def cie_defg_to_rgb(cls, components, cie_dict):
        """
        Convert CIEBasedDEFG color to sRGB.

        Pipeline: input → RangeDEFG clamp → Table lookup → ABC → cie_abc_to_rgb

        The Table is a 4D lookup table [m1 m2 m3 m4 [strings...]] that maps
        DEFG values to ABC values.

        Args:
            components: list of 4 floats (D, E, F, G)
            cie_dict: the CIE dictionary (Python dict with bytes keys)

        Returns:
            (r, g, b) tuple with values in [0, 1]
        """
        d, e, f, g = (float(components[i]) if i < len(components) else 0.0 for i in range(4))

        # RangeDEFG clamp (default [0 1 0 1 0 1 0 1])
        range_defg = _get_cie_float_array(cie_dict, b"RangeDEFG",
                                          [0, 1, 0, 1, 0, 1, 0, 1])
        d = max(range_defg[0], min(range_defg[1], d))
        e = max(range_defg[2], min(range_defg[3], e))
        f = max(range_defg[4], min(range_defg[5], f))
        g = max(range_defg[6], min(range_defg[7], g))

        # Table lookup: DEFG → ABC (byte values scaled to RangeABC per PLRM)
        table_obj = cie_dict.get(b"Table")
        range_abc = _get_cie_float_array(cie_dict, b"RangeABC", [0, 1, 0, 1, 0, 1])
        if table_obj and hasattr(table_obj, 'val') and len(table_obj.val) >= 5:
            abc = _table_4d_lookup(table_obj, d, e, f, g, range_defg, range_abc)
        else:
            # No Table — treat as CMYK fallback
            return cls.cmyk_to_rgb(d, e, f, g)

        return cls.cie_abc_to_rgb(abc, cie_dict)

    # ==========================================================================
    # Validation Utilities
    # ==========================================================================
    
    @staticmethod
    def clamp_color_components(components: List[float]) -> List[float]:
        """
        Clamp color component values to valid range [0.0, 1.0].
        
        Args:
            components: List of color component values
            
        Returns:
            List of clamped values
            
        Reference: PLRM - all color components must be in range 0.0-1.0
        """
        return [max(0.0, min(1.0, value)) for value in components]
    
    @staticmethod  
    def validate_component_count(space_array: List, components: List[float]) -> bool:
        """
        Validate that component count matches color space requirements.
        
        Args:
            space_array: Color space array like ["DeviceRGB"]
            components: List of color component values
            
        Returns:
            True if component count is correct, False otherwise
        """
        try:
            expected_count = ColorSpaceEngine.get_component_count(space_array)
            return len(components) == expected_count
        except ValueError:
            return False


def convert_to_device_color(ctxt, gs_color, gs_color_space):
    """
    Convert PostScript color to device color space for display list.

    Args:
        ctxt: PostScript context with pagedevice
        gs_color: Graphics state color (list of PostScript objects or floats)
        gs_color_space: Graphics state color space (list, e.g. ["DeviceRGB"])

    Returns:
        List of Python float values in device color space
    """
    # Convert PostScript objects to Python floats first
    source_color = [float(component.val) if hasattr(component, 'val') else float(component)
                   for component in gs_color]

    # Get device color model from pagedevice (stored in gstate)
    if not (hasattr(ctxt.gstate, 'page_device') and ctxt.gstate.page_device):
        return source_color  # No pagedevice, return as-is

    device_color_model = ctxt.gstate.page_device.get(b"ColorModel")
    if not device_color_model:
        return source_color  # No ColorModel key, return as-is

    # Get source color space name
    source_space = gs_color_space[0] if isinstance(gs_color_space, list) else gs_color_space

    # For CIE-based spaces, color is already resolved to sRGB by setcolor
    if source_space in ("CIEBasedABC", "CIEBasedA", "CIEBasedDEF", "CIEBasedDEFG"):
        source_space = "DeviceRGB"

    # ICCBased: try Tier 2 ICC transform, fall back to Tier 1 device space
    if source_space == "ICCBased":
        stream_obj = gs_color_space[1] if len(gs_color_space) > 1 else None
        profile_hash = icc_profile.get_profile_hash(stream_obj) if stream_obj else None
        n = len(source_color)
        if profile_hash is not None:
            rgb = icc_profile.icc_convert_color(profile_hash, n, source_color)
            if rgb is not None:
                source_color = list(rgb)
                source_space = "DeviceRGB"
            else:
                source_space = ColorSpaceEngine.resolve_iccbased_space(gs_color_space)
        else:
            source_space = ColorSpaceEngine.resolve_iccbased_space(gs_color_space)

    # For Separation, DeviceN, and Indexed, the color values are already in the
    # base/alternative space (resolved in setcolor/setcolorspace)
    if source_space in ("Separation", "DeviceN", "Indexed"):
        # Get base/alternative space from color space array
        if source_space == "Indexed":
            alt_space_obj = gs_color_space[1]  # Base space at index 1
        else:
            alt_space_obj = gs_color_space[2]  # Alternative space at index 2
        if hasattr(alt_space_obj, 'TYPE'):
            # It's a PostScript object
            if alt_space_obj.TYPE == ps.T_NAME:
                source_space = alt_space_obj.val.decode('ascii') if isinstance(alt_space_obj.val, bytes) else alt_space_obj.val
            elif hasattr(alt_space_obj, 'val') and len(alt_space_obj.val) > 0:
                # Array - get first element
                first = alt_space_obj.val[0]
                if hasattr(first, 'val'):
                    source_space = first.val.decode('ascii') if isinstance(first.val, bytes) else first.val
        elif isinstance(alt_space_obj, str):
            source_space = alt_space_obj

    # Device color model mapping to PostScript color space names
    device_space_map = {
        b"DeviceGray": "DeviceGray",
        b"DeviceRGB": "DeviceRGB",
        b"DeviceCMYK": "DeviceCMYK"
    }

    # Handle both Name objects and raw values
    color_model_val = device_color_model.val if hasattr(device_color_model, 'val') else device_color_model
    target_space = device_space_map.get(color_model_val, "DeviceRGB")

    # If source and target are the same, return as-is
    if source_space == target_space:
        return source_color

    # Use ColorSpaceEngine for conversion
    color_engine = ColorSpaceEngine()

    try:
        # Convert based on source → target combinations
        if source_space == "DeviceGray" and target_space == "DeviceRGB":
            return list(color_engine.gray_to_rgb(source_color[0]))
        elif source_space == "DeviceGray" and target_space == "DeviceCMYK":
            return list(color_engine.gray_to_cmyk(source_color[0]))
        elif source_space == "DeviceRGB" and target_space == "DeviceGray":
            gray = color_engine.rgb_to_gray(source_color[0], source_color[1], source_color[2])
            return [gray]
        elif source_space == "DeviceRGB" and target_space == "DeviceCMYK":
            return list(color_engine.rgb_to_cmyk(source_color[0], source_color[1], source_color[2]))
        elif source_space == "DeviceCMYK" and target_space == "DeviceGray":
            gray = color_engine.cmyk_to_gray(source_color[0], source_color[1], source_color[2], source_color[3])
            return [gray]
        elif source_space == "DeviceCMYK" and target_space == "DeviceRGB":
            icc_rgb = icc_default.convert_cmyk_color(
                source_color[0], source_color[1], source_color[2], source_color[3])
            if icc_rgb is not None:
                return list(icc_rgb)
            return list(color_engine.cmyk_to_rgb(source_color[0], source_color[1], source_color[2], source_color[3]))
        else:
            # Unsupported conversion - fallback to source color
            return source_color

    except Exception as e:
        print(f"Color conversion error ({source_space} -> {target_space}): {e}")
        return source_color  # Fallback to original color


def _get_cie_float_array(d, key, default):
    """Extract a list of floats from a CIE dictionary entry.

    Handles both plain Python dicts and PostScript Dict.val dicts.
    """
    if not isinstance(d, dict):
        return default
    obj = d.get(key)
    if obj is None:
        return default
    if hasattr(obj, 'TYPE') and obj.TYPE in ps.ARRAY_TYPES:
        return [float(obj.val[i].val) for i in range(obj.start, obj.start + obj.length)]
    if isinstance(obj, (list, tuple)):
        return [float(x.val) if hasattr(x, 'val') else float(x) for x in obj]
    return default


def _table_3d_lookup(table_obj, d, e, f, range_def, range_abc):
    """Perform trilinear interpolation in a CIEBasedDEF 3D lookup table.

    Table format: [m1 m2 m3 [string1 ... string_m1]]
    Each string has m2*m3*3 bytes. Byte values (0-255) are linearly mapped
    to the RangeABC bounds per PLRM.
    """
    tv = table_obj.val
    m1 = int(tv[0].val) if hasattr(tv[0], 'val') else int(tv[0])
    m2 = int(tv[1].val) if hasattr(tv[1], 'val') else int(tv[1])
    m3 = int(tv[2].val) if hasattr(tv[2], 'val') else int(tv[2])
    strings_obj = tv[3]

    if hasattr(strings_obj, 'val'):
        strings = strings_obj.val
        s_start = getattr(strings_obj, 'start', 0)
    else:
        strings = strings_obj
        s_start = 0

    # Normalize DEF to [0, m-1] indices
    d_range = range_def[1] - range_def[0] if range_def[1] != range_def[0] else 1.0
    e_range = range_def[3] - range_def[2] if range_def[3] != range_def[2] else 1.0
    f_range = range_def[5] - range_def[4] if range_def[5] != range_def[4] else 1.0

    di = (d - range_def[0]) / d_range * (m1 - 1)
    ei = (e - range_def[2]) / e_range * (m2 - 1)
    fi = (f - range_def[4]) / f_range * (m3 - 1)

    # Clamp
    di = max(0.0, min(m1 - 1.0, di))
    ei = max(0.0, min(m2 - 1.0, ei))
    fi = max(0.0, min(m3 - 1.0, fi))

    # Integer indices and fractional parts for trilinear interpolation
    di0 = int(di)
    ei0 = int(ei)
    fi0 = int(fi)
    di1 = min(di0 + 1, m1 - 1)
    ei1 = min(ei0 + 1, m2 - 1)
    fi1 = min(fi0 + 1, m3 - 1)
    dd = di - di0
    de = ei - ei0
    df = fi - fi0

    # Precompute RangeABC scaling factors
    abc_min = [range_abc[0], range_abc[2], range_abc[4]]
    abc_scale = [(range_abc[1] - range_abc[0]) / 255.0,
                 (range_abc[3] - range_abc[2]) / 255.0,
                 (range_abc[5] - range_abc[4]) / 255.0]

    def _sample(d_idx, e_idx, f_idx):
        """Get ABC triple from the table, scaled to RangeABC."""
        string_obj = strings[s_start + d_idx]
        if hasattr(string_obj, 'byte_string'):
            data = string_obj.byte_string()
        elif hasattr(string_obj, 'val') and isinstance(string_obj.val, (bytes, bytearray)):
            data = string_obj.val
        else:
            data = bytes(string_obj) if not isinstance(string_obj, (bytes, bytearray)) else string_obj
        offset = (e_idx * m3 + f_idx) * 3
        if offset + 2 < len(data):
            return (abc_min[0] + data[offset] * abc_scale[0],
                    abc_min[1] + data[offset + 1] * abc_scale[1],
                    abc_min[2] + data[offset + 2] * abc_scale[2])
        return (abc_min[0], abc_min[1], abc_min[2])

    # Trilinear interpolation
    c000 = _sample(di0, ei0, fi0)
    c001 = _sample(di0, ei0, fi1)
    c010 = _sample(di0, ei1, fi0)
    c011 = _sample(di0, ei1, fi1)
    c100 = _sample(di1, ei0, fi0)
    c101 = _sample(di1, ei0, fi1)
    c110 = _sample(di1, ei1, fi0)
    c111 = _sample(di1, ei1, fi1)

    abc = []
    for comp in range(3):
        c00 = c000[comp] * (1 - df) + c001[comp] * df
        c01 = c010[comp] * (1 - df) + c011[comp] * df
        c10 = c100[comp] * (1 - df) + c101[comp] * df
        c11 = c110[comp] * (1 - df) + c111[comp] * df
        c0 = c00 * (1 - de) + c01 * de
        c1 = c10 * (1 - de) + c11 * de
        abc.append(c0 * (1 - dd) + c1 * dd)

    return abc


def _table_4d_lookup(table_obj, d, e, f, g, range_defg, range_abc):
    """Perform nearest-neighbor lookup in a CIEBasedDEFG 4D lookup table.

    Table format: [m1 m2 m3 m4 [string1 ... string_m1]]
    Each string has m2*m3*m4*3 bytes. Byte values (0-255) are linearly mapped
    to the RangeABC bounds per PLRM.
    """
    tv = table_obj.val
    m1 = int(tv[0].val) if hasattr(tv[0], 'val') else int(tv[0])
    m2 = int(tv[1].val) if hasattr(tv[1], 'val') else int(tv[1])
    m3 = int(tv[2].val) if hasattr(tv[2], 'val') else int(tv[2])
    m4 = int(tv[3].val) if hasattr(tv[3], 'val') else int(tv[3])
    strings_obj = tv[4]

    if hasattr(strings_obj, 'val'):
        strings = strings_obj.val
        s_start = getattr(strings_obj, 'start', 0)
    else:
        strings = strings_obj
        s_start = 0

    # Normalize DEFG to [0, m-1] indices
    d_range = range_defg[1] - range_defg[0] if range_defg[1] != range_defg[0] else 1.0
    e_range = range_defg[3] - range_defg[2] if range_defg[3] != range_defg[2] else 1.0
    f_range = range_defg[5] - range_defg[4] if range_defg[5] != range_defg[4] else 1.0
    g_range = range_defg[7] - range_defg[6] if range_defg[7] != range_defg[6] else 1.0

    di = max(0.0, min(m1 - 1.0, (d - range_defg[0]) / d_range * (m1 - 1)))
    ei = max(0.0, min(m2 - 1.0, (e - range_defg[2]) / e_range * (m2 - 1)))
    fi = max(0.0, min(m3 - 1.0, (f - range_defg[4]) / f_range * (m3 - 1)))
    gi = max(0.0, min(m4 - 1.0, (g - range_defg[6]) / g_range * (m4 - 1)))

    # Nearest-neighbor for 4D (quadrilinear would be 16 samples)
    di0 = min(int(di + 0.5), m1 - 1)
    ei0 = min(int(ei + 0.5), m2 - 1)
    fi0 = min(int(fi + 0.5), m3 - 1)
    gi0 = min(int(gi + 0.5), m4 - 1)

    string_obj = strings[s_start + di0]
    if hasattr(string_obj, 'byte_string'):
        data = string_obj.byte_string()
    elif hasattr(string_obj, 'val') and isinstance(string_obj.val, (bytes, bytearray)):
        data = string_obj.val
    else:
        data = bytes(string_obj) if not isinstance(string_obj, (bytes, bytearray)) else string_obj

    offset = (ei0 * m3 * m4 + fi0 * m4 + gi0) * 3
    if offset + 2 < len(data):
        return [range_abc[0] + (data[offset] / 255.0) * (range_abc[1] - range_abc[0]),
                range_abc[2] + (data[offset + 1] / 255.0) * (range_abc[3] - range_abc[2]),
                range_abc[4] + (data[offset + 2] / 255.0) * (range_abc[5] - range_abc[4])]
    return [range_abc[0], range_abc[2], range_abc[4]]


_CIE_DECODE_CACHE = {}  # id(proc) → list of pre-evaluated float values
_CIE_DECODE_CACHE_SIZE = 256  # Matches 8-bit images exactly (i/255 inputs)


def _eval_cie_decode_proc(proc_obj, input_val):
    """Evaluate a CIE Decode procedure (DecodeABC, DecodeLMN, etc.) on a single value.

    On first call for a given procedure, pre-evaluates at 256 points in [0, 1]
    and caches the results. Subsequent calls use fast linear interpolation in the
    cached table. Falls back to direct evaluation for inputs outside [0, 1].
    """
    if not hasattr(proc_obj, 'TYPE') or proc_obj.TYPE not in ps.ARRAY_TYPES:
        return input_val
    if proc_obj.attrib != ps.ATTRIB_EXEC:
        return input_val

    x = float(input_val)
    cache_key = id(proc_obj)
    table = _CIE_DECODE_CACHE.get(cache_key)

    if table is None:
        # Pre-evaluate procedure at N evenly-spaced points in [0, 1]
        N = _CIE_DECODE_CACHE_SIZE
        table = [0.0] * N
        for i in range(N):
            v = i / (N - 1)
            stack = [v]
            _exec_cie_tokens(proc_obj, stack)
            table[i] = float(stack[-1]) if stack and isinstance(stack[-1], (int, float)) else v
        _CIE_DECODE_CACHE[cache_key] = table

    # Fast path: linear interpolation in cached table for inputs in [0, 1]
    if 0.0 <= x <= 1.0:
        N = len(table)
        idx = x * (N - 1)
        i0 = min(int(idx), N - 2)
        frac = idx - i0
        return table[i0] + (table[i0 + 1] - table[i0]) * frac

    # Slow path: direct evaluation for out-of-range inputs
    stack = [x]
    _exec_cie_tokens(proc_obj, stack)
    return float(stack[-1]) if stack and isinstance(stack[-1], (int, float)) else x


_CIE_MARK = object()  # Sentinel for [ ... ] array construction in CIE procedures


def _exec_cie_tokens(proc_obj, stack):
    """Execute CIE procedure tokens on an existing stack.

    Handles: numbers, dup, pop, exch, add, sub, mul, div, exp, neg, abs,
    floor, ceiling, cvi, ge, gt, lt, le, eq, if, ifelse, bind (no-op),
    index, roll, array construction ([...]), and array/string table lookups.
    """
    tokens = proc_obj.val[proc_obj.start:proc_obj.start + proc_obj.length]

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1

        # Number literal
        if hasattr(tok, 'TYPE') and tok.TYPE in ps.NUMERIC_TYPES:
            stack.append(float(tok.val))
            continue

        # Mark token (the [ operator) — push sentinel for array construction
        if hasattr(tok, 'TYPE') and tok.TYPE == ps.T_MARK:
            stack.append(_CIE_MARK)
            continue

        # Array or string literal (push onto stack for table lookups)
        if hasattr(tok, 'TYPE') and tok.TYPE in ps.ARRAY_TYPES and tok.attrib == ps.ATTRIB_LIT:
            stack.append(tok)
            continue
        if hasattr(tok, 'TYPE') and tok.TYPE == ps.T_STRING:
            stack.append(tok)
            continue

        # Executable procedure (push for if/ifelse to consume)
        if hasattr(tok, 'TYPE') and tok.TYPE in ps.ARRAY_TYPES and tok.attrib == ps.ATTRIB_EXEC:
            stack.append(tok)
            continue

        # Bound operator (after bind): extract name from function's __name__
        if hasattr(tok, 'TYPE') and tok.TYPE == ps.T_OPERATOR:
            fn = tok.val
            name = getattr(fn, '__name__', '')
            if name.startswith('ps_'):
                name = name[3:]
        # Name token
        elif hasattr(tok, 'TYPE') and tok.TYPE == ps.T_NAME:
            name = tok.val
            if isinstance(name, bytes):
                name = name.decode('ascii', errors='replace')
        else:
            continue

        if name == 'dup':
            if stack:
                stack.append(stack[-1])
        elif name == 'pop':
            if stack:
                stack.pop()
        elif name == 'exch':
            if len(stack) >= 2:
                stack[-1], stack[-2] = stack[-2], stack[-1]
        elif name == 'index':
            if stack:
                n = int(stack.pop())
                if n < len(stack):
                    stack.append(stack[-(n + 1)])
        elif name == 'roll':
            if len(stack) >= 2:
                j = int(stack.pop())
                n = int(stack.pop())
                if n > 0 and n <= len(stack):
                    segment = stack[-n:]
                    j = j % n
                    stack[-n:] = segment[-j:] + segment[:-j]
        elif name == 'add':
            if len(stack) >= 2:
                b = stack.pop()
                a = stack.pop()
                stack.append(float(a) + float(b))
        elif name == 'sub':
            if len(stack) >= 2:
                b = stack.pop()
                a = stack.pop()
                stack.append(float(a) - float(b))
        elif name == 'mul':
            if len(stack) >= 2:
                b = stack.pop()
                a = stack.pop()
                stack.append(float(a) * float(b))
        elif name == 'div':
            if len(stack) >= 2:
                b = stack.pop()
                a = stack.pop()
                stack.append(float(a) / float(b) if float(b) != 0 else 0.0)
        elif name == 'exp':
            if len(stack) >= 2:
                b = stack.pop()
                a = stack.pop()
                try:
                    stack.append(float(a) ** float(b))
                except (ValueError, OverflowError):
                    stack.append(0.0)
        elif name == 'neg':
            if stack:
                stack[-1] = -float(stack[-1])
        elif name == 'abs':
            if stack:
                stack[-1] = abs(float(stack[-1]))
        elif name == 'floor':
            if stack:
                stack[-1] = float(_math.floor(float(stack[-1])))
        elif name == 'ceiling':
            if stack:
                stack[-1] = float(_math.ceil(float(stack[-1])))
        elif name == 'cvi':
            if stack:
                stack[-1] = float(int(float(stack[-1])))
        elif name == 'cvr':
            if stack:
                stack[-1] = float(stack[-1])
        elif name in ('ge', 'gt', 'lt', 'le', 'eq', 'ne'):
            if len(stack) >= 2:
                b = float(stack.pop())
                a = float(stack.pop())
                if name == 'ge':
                    stack.append(1.0 if a >= b else 0.0)
                elif name == 'gt':
                    stack.append(1.0 if a > b else 0.0)
                elif name == 'lt':
                    stack.append(1.0 if a < b else 0.0)
                elif name == 'le':
                    stack.append(1.0 if a <= b else 0.0)
                elif name == 'eq':
                    stack.append(1.0 if a == b else 0.0)
                elif name == 'ne':
                    stack.append(1.0 if a != b else 0.0)
        elif name == 'not':
            if stack:
                v = stack.pop()
                stack.append(0.0 if float(v) != 0.0 else 1.0)
        elif name == 'if':
            # bool {proc} if — execute proc on current stack if bool is true
            if len(stack) >= 2:
                proc = stack.pop()
                cond = stack.pop()
                if float(cond) != 0.0 and hasattr(proc, 'TYPE') and proc.TYPE in ps.ARRAY_TYPES:
                    _exec_cie_tokens(proc, stack)
        elif name == 'ifelse':
            # bool {true_proc} {false_proc} ifelse
            if len(stack) >= 3:
                false_proc = stack.pop()
                true_proc = stack.pop()
                cond = stack.pop()
                chosen = true_proc if float(cond) != 0.0 else false_proc
                if hasattr(chosen, 'TYPE') and chosen.TYPE in ps.ARRAY_TYPES:
                    _exec_cie_tokens(chosen, stack)
        elif name in (']', 'array_from_mark'):
            # Collect elements since last mark into a Python list
            arr_elems = []
            while stack and stack[-1] is not _CIE_MARK:
                arr_elems.append(stack.pop())
            if stack and stack[-1] is _CIE_MARK:
                stack.pop()  # Remove the mark sentinel
            arr_elems.reverse()
            stack.append(arr_elems)
        elif name == 'get':
            if len(stack) >= 2:
                idx = int(float(stack.pop()))
                arr = stack.pop()
                if isinstance(arr, list):
                    if 0 <= idx < len(arr):
                        val = arr[idx]
                        stack.append(float(val.val) if hasattr(val, 'val') else float(val))
                    else:
                        stack.append(0.0)
                elif hasattr(arr, 'TYPE') and arr.TYPE in ps.ARRAY_TYPES:
                    s = getattr(arr, 'start', 0)
                    if 0 <= idx < arr.length:
                        val = arr.val[s + idx]
                        stack.append(float(val.val) if hasattr(val, 'val') else float(val))
                    else:
                        stack.append(0.0)
                elif hasattr(arr, 'TYPE') and arr.TYPE == ps.T_STRING:
                    data = arr.byte_string() if hasattr(arr, 'byte_string') else arr.val
                    if 0 <= idx < len(data):
                        stack.append(float(data[idx]))
                    else:
                        stack.append(0.0)
                else:
                    stack.append(0.0)
        elif name == 'length':
            if stack:
                arr = stack.pop()
                if isinstance(arr, list):
                    stack.append(float(len(arr)))
                elif hasattr(arr, 'length'):
                    stack.append(float(arr.length))
                elif hasattr(arr, '__len__'):
                    stack.append(float(len(arr)))
                else:
                    stack.append(0.0)
        elif name in ('bind', 'readonly'):
            pass  # No-op in evaluation context
        # Unknown operators are silently ignored


def _apply_decode_array(cie_dict, key, values):
    """Apply a DecodeABC/DecodeLMN/DecodeA array of procedures to input values.

    DecodeABC and DecodeLMN are literal arrays of executable procedures:
        [{proc1} {proc2} {proc3}]
    DecodeA is a single executable procedure:
        {proc}

    Args:
        cie_dict: CIE dictionary (Python dict with bytes keys)
        key: dict key (e.g. b"DecodeABC", b"DecodeLMN", b"DecodeA")
        values: list of input values

    Returns:
        list of decoded values, or original values if no procedures found
    """
    decode_obj = cie_dict.get(key)
    if not decode_obj or not hasattr(decode_obj, 'TYPE') or decode_obj.TYPE not in ps.ARRAY_TYPES:
        return values

    # Single executable procedure (DecodeA): apply to first value
    if decode_obj.attrib == ps.ATTRIB_EXEC:
        result = list(values)
        if result:
            result[0] = _eval_cie_decode_proc(decode_obj, result[0])
        return result

    # Literal array of procedures (DecodeABC, DecodeLMN): apply each to its value
    procs = decode_obj.val[decode_obj.start:decode_obj.start + decode_obj.length]
    result = list(values)
    for i in range(min(len(procs), len(result))):
        proc = procs[i]
        if hasattr(proc, 'TYPE') and proc.TYPE in ps.ARRAY_TYPES and proc.attrib == ps.ATTRIB_EXEC:
            result[i] = _eval_cie_decode_proc(proc, result[i])

    return result