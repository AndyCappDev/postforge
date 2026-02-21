# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PNG Output Device

This device renders PostScript graphics to PNG image files using Cairo.
It uses the shared cairo_renderer module for display list rendering.
"""

import os

import cairo

from ...core import types as ps
from ..common.cairo_renderer import render_display_list

# Anti-aliasing mode for Cairo rendering (also used by glyph bitmap cache).
# Options: cairo.ANTIALIAS_NONE, ANTIALIAS_FAST, ANTIALIAS_GOOD,
#          ANTIALIAS_BEST, ANTIALIAS_GRAY, ANTIALIAS_SUBPIXEL
ANTIALIAS_MODE = cairo.ANTIALIAS_GRAY

ANTIALIAS_MAP = {
    "none": cairo.ANTIALIAS_NONE,
    "fast": cairo.ANTIALIAS_FAST,
    "good": cairo.ANTIALIAS_GOOD,
    "best": cairo.ANTIALIAS_BEST,
    "gray": cairo.ANTIALIAS_GRAY,
    "subpixel": cairo.ANTIALIAS_SUBPIXEL,
}


def _get_antialias_mode(pd: dict) -> int:
    if b"AntiAliasMode" in pd:
        return ANTIALIAS_MAP.get(pd[b"AntiAliasMode"].python_string(), ANTIALIAS_MODE)
    return ANTIALIAS_MODE


def showpage(ctxt: ps.Context, pd: dict) -> None:
    """
    Render the current page to a PNG file.

    Args:
        ctxt: PostScript context with display_list to render
        pd: Page device dictionary containing MediaSize, PageCount, LineWidthMin, etc.
    """
    min_line_width = pd[b"LineWidthMin"].val

    # Get page dimensions from page device
    WIDTH = pd[b"MediaSize"].get(ps.Int(0))[1].val
    HEIGHT = pd[b"MediaSize"].get(ps.Int(1))[1].val

    # Create Cairo surface and context
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, WIDTH, HEIGHT)
    cc = cairo.Context(surface)
    cc.identity_matrix()
    # Convert PostScript flatness to Cairo tolerance (PS default 1.0 → Cairo default 0.1)
    cc.set_tolerance(ctxt.gstate.flatness / 10.0)

    # Fill in the white background
    cc.set_source_rgb(1.0, 1.0, 1.0)
    cc.rectangle(0, 0, WIDTH, HEIGHT)
    cc.fill()

    cc.set_antialias(_get_antialias_mode(pd))

    # Render display list using shared Cairo renderer
    render_display_list(ctxt, cc, HEIGHT, min_line_width)

    # Write PNG output with configurable base name and directory
    page_num = pd[b"PageCount"].val

    # Get base name from page device, default to "page"
    if b"OutputBaseName" in pd:
        base_name = pd[b"OutputBaseName"].python_string()
    else:
        base_name = "page"

    # Get output directory from page device, default to OUTPUT_DIRECTORY
    if b"OutputDirectory" in pd:
        output_dir = pd[b"OutputDirectory"].python_string()
    else:
        output_dir = ps.OUTPUT_DIRECTORY

    output_file = os.path.join(os.getcwd(), output_dir, f"{base_name}-{page_num:04d}.png")
    surface.write_to_png(output_file)

    # TODO -
    # 1) check NumCopies in the page device dictionary
    #    if it exists and is not null
    #       a) output n copies of the page
    #    if it does not exist
    #       a) check #copies in the context of the current dict stack
    #          and output that number of copies of the page
