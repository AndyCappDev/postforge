# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
TIFF Output Device

Renders PostScript graphics to TIFF image files using Cairo for rendering
and Pillow for TIFF encoding. Supports single-page (one .tif per page) and
multi-page (all pages in one .tif) modes, with optional CMYK output via
ICC profile conversion.
"""

import os
import warnings
from typing import Any

import cairo
from PIL import Image

from ...core import icc_default
from ...core import types as ps
from ..common.cairo_renderer import render_display_list

try:
    from PIL import ImageCms
    _IMAGECMS_AVAILABLE = True
except ImportError:
    _IMAGECMS_AVAILABLE = False

# Anti-aliasing mode for Cairo rendering (same defaults as PNG device)
ANTIALIAS_MODE = cairo.ANTIALIAS_GRAY

ANTIALIAS_MAP: dict[str, int] = {
    "none": cairo.ANTIALIAS_NONE,
    "fast": cairo.ANTIALIAS_FAST,
    "good": cairo.ANTIALIAS_GOOD,
    "best": cairo.ANTIALIAS_BEST,
    "gray": cairo.ANTIALIAS_GRAY,
    "subpixel": cairo.ANTIALIAS_SUBPIXEL,
}

# Module-level state for multi-page accumulation
_accumulated_pages: list[Image.Image] = []
_multipage_output_path: str | None = None
_multipage_dpi: tuple[float, float] | None = None
_multipage_icc_bytes: bytes | None = None

# Cached CMYK transform (built once, reused per page)
_cmyk_transform: Any = None
_cmyk_transform_built: bool = False
_cmyk_icc_bytes: bytes | None = None


def _get_antialias_mode(pd: dict) -> int:
    """Get the Cairo anti-aliasing mode from the page device dictionary.

    Args:
        pd: Page device dictionary.

    Returns:
        Cairo antialias constant.
    """
    if b"AntiAliasMode" in pd:
        return ANTIALIAS_MAP.get(pd[b"AntiAliasMode"].python_string(), ANTIALIAS_MODE)
    return ANTIALIAS_MODE


def _build_cmyk_transform() -> Any:
    """Build an sRGB-to-CMYK ImageCms transform using the system CMYK profile.

    Uses icc_default._find_cmyk_profile() to locate the CMYK profile, then
    builds a transform from sRGB to that profile. Caches the result at module
    level so it's built once and reused for every page.

    Returns:
        ImageCms transform object, or None if no profile found or ImageCms
        unavailable.
    """
    global _cmyk_transform, _cmyk_transform_built, _cmyk_icc_bytes

    if _cmyk_transform_built:
        return _cmyk_transform

    _cmyk_transform_built = True

    if not _IMAGECMS_AVAILABLE:
        warnings.warn("CMYK output requested but Pillow ImageCms is not available. "
                       "Output will be RGB.", stacklevel=2)
        return None

    profile_path = icc_default._find_cmyk_profile()
    if profile_path is None:
        warnings.warn("CMYK output requested but no CMYK ICC profile found. "
                       "Output will be RGB. Use --cmyk-profile to specify one.", stacklevel=2)
        return None

    try:
        cmyk_profile = ImageCms.getOpenProfile(profile_path)
        srgb_profile = ImageCms.createProfile('sRGB')
        transform = ImageCms.buildTransform(
            srgb_profile, cmyk_profile, 'RGB', 'CMYK',
            renderingIntent=ImageCms.Intent.PERCEPTUAL
        )
        _cmyk_transform = transform

        # Read the ICC profile bytes for embedding in the TIFF
        with open(profile_path, 'rb') as f:
            _cmyk_icc_bytes = f.read()

        return transform
    except Exception as e:
        warnings.warn(f"Failed to build CMYK transform: {e}. Output will be RGB.", stacklevel=2)
        return None


def _cairo_surface_to_pil(surface: cairo.ImageSurface, cmyk: bool,
                          cmyk_transform: Any) -> Image.Image:
    """Convert a Cairo ImageSurface to a PIL Image.

    Uses direct buffer access (no PNG round-trip) for performance. Optionally
    applies ICC-based RGB-to-CMYK conversion.

    Args:
        surface: Cairo RGB24 image surface.
        cmyk: Whether to convert to CMYK.
        cmyk_transform: Pre-built ImageCms transform (sRGB->CMYK), or None.

    Returns:
        PIL Image in RGB or CMYK mode.
    """
    width = surface.get_width()
    height = surface.get_height()
    buf = surface.get_data()

    # Cairo FORMAT_RGB24 stores as BGRX (32-bit, X=unused alpha)
    img = Image.frombuffer("RGBA", (width, height), bytes(buf), "raw", "BGRA", 0, 1)
    img = img.convert("RGB")

    if cmyk and cmyk_transform is not None:
        # ImageCms.applyTransform with inPlace=False since modes differ (RGB→CMYK)
        img = ImageCms.applyTransform(img, cmyk_transform)

    return img


def showpage(ctxt: ps.Context, pd: dict) -> None:
    """Render the current page to a TIFF file (or accumulate for multi-page).

    Args:
        ctxt: PostScript context with display_list to render.
        pd: Page device dictionary containing MediaSize, PageCount, etc.
    """
    global _multipage_output_path, _multipage_dpi, _multipage_icc_bytes

    min_line_width = pd[b"LineWidthMin"].val

    # Get page dimensions from page device
    WIDTH = pd[b"MediaSize"].get(ps.Int(0))[1].val
    HEIGHT = pd[b"MediaSize"].get(ps.Int(1))[1].val

    # Create Cairo surface and context
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, WIDTH, HEIGHT)
    cc = cairo.Context(surface)
    cc.identity_matrix()
    cc.set_tolerance(ctxt.gstate.flatness / 10.0)

    # Fill white background
    cc.set_source_rgb(1.0, 1.0, 1.0)
    cc.rectangle(0, 0, WIDTH, HEIGHT)
    cc.fill()

    cc.set_antialias(_get_antialias_mode(pd))

    # Render display list using shared Cairo renderer
    render_display_list(ctxt, cc, HEIGHT, min_line_width)

    # Check CMYK mode
    cmyk = (b"CMYKOutput" in pd and hasattr(pd[b"CMYKOutput"], 'val')
            and pd[b"CMYKOutput"].val)
    cmyk_transform = _build_cmyk_transform() if cmyk else None

    # Convert surface to PIL Image
    img = _cairo_surface_to_pil(surface, cmyk, cmyk_transform)

    # Extract DPI for TIFF metadata
    hw_res = pd[b"HWResolution"]
    dpi_x = hw_res.get(ps.Int(0))[1].val
    dpi_y = hw_res.get(ps.Int(1))[1].val
    dpi = (float(dpi_x), float(dpi_y))

    # Get output naming
    page_num = pd[b"PageCount"].val

    if b"OutputBaseName" in pd:
        base_name = pd[b"OutputBaseName"].python_string()
    else:
        base_name = "page"

    if b"OutputDirectory" in pd:
        output_dir = pd[b"OutputDirectory"].python_string()
    else:
        output_dir = ps.OUTPUT_DIRECTORY

    # Build ICC profile bytes for embedding (if CMYK)
    icc_bytes = _cmyk_icc_bytes if cmyk and cmyk_transform is not None else None

    # Check multi-page mode
    multipage = (b"MultiPageTiff" in pd and hasattr(pd[b"MultiPageTiff"], 'val')
                 and pd[b"MultiPageTiff"].val)

    if multipage:
        _accumulated_pages.append(img)
        _multipage_dpi = dpi
        _multipage_icc_bytes = icc_bytes
        if _multipage_output_path is None:
            _multipage_output_path = os.path.join(
                os.getcwd(), output_dir, f"{base_name}.tif"
            )
    else:
        # Single-page mode: save immediately
        output_file = os.path.join(os.getcwd(), output_dir, f"{base_name}-{page_num:04d}.tif")

        save_kwargs: dict[str, Any] = {
            'format': 'TIFF',
            'compression': 'tiff_lzw',
            'dpi': dpi,
        }
        if icc_bytes:
            save_kwargs['icc_profile'] = icc_bytes

        img.save(output_file, **save_kwargs)


def finalize(pd: dict) -> None:
    """Finalize multi-page TIFF output.

    If pages have been accumulated (multi-page mode), saves them all to a
    single multi-page TIFF file. Called after all batch jobs complete.

    Args:
        pd: Page device dictionary.
    """
    global _multipage_output_path, _multipage_dpi, _multipage_icc_bytes

    if not _accumulated_pages:
        return

    output_path = _multipage_output_path
    if output_path is None:
        return

    dpi = _multipage_dpi or (300.0, 300.0)

    save_kwargs: dict[str, Any] = {
        'format': 'TIFF',
        'save_all': True,
        'append_images': _accumulated_pages[1:],
        'compression': 'tiff_lzw',
        'dpi': dpi,
    }
    if _multipage_icc_bytes:
        save_kwargs['icc_profile'] = _multipage_icc_bytes

    _accumulated_pages[0].save(output_path, **save_kwargs)

    print(f"Multi-page TIFF saved: {output_path} ({len(_accumulated_pages)} pages)")

    # Clear accumulated state
    _accumulated_pages.clear()
    _multipage_output_path = None
    _multipage_dpi = None
    _multipage_icc_bytes = None
