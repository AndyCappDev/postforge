# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
CLI argument parsing for PostForge.

Handles command-line argument definition, parsing, page range specifications,
and output file naming.
"""

from __future__ import annotations

import argparse
import os
import re


def _parse_page_ranges(spec: str) -> set[int]:
    """Parse a page range specification into a set of page numbers.

    Supports single pages (``3``), ranges (``1-5``), and comma-separated
    combinations (``1-3,7,10-12``).  Page numbers are 1-based.

    Args:
        spec: Page range string, e.g. ``"1-5,8,10-12"``

    Returns:
        Set of integer page numbers.

    Raises:
        ValueError: If the specification is malformed.
    """
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            if len(bounds) != 2 or not bounds[0].strip() or not bounds[1].strip():
                raise ValueError(f"Invalid page range: '{part}'")
            try:
                start = int(bounds[0])
                end = int(bounds[1])
            except ValueError:
                raise ValueError(f"Invalid page range: '{part}'")
            if start < 1 or end < 1:
                raise ValueError(f"Page numbers must be positive: '{part}'")
            if start > end:
                raise ValueError(f"Invalid page range (start > end): '{part}'")
            pages.update(range(start, end + 1))
        else:
            try:
                num = int(part)
            except ValueError:
                raise ValueError(f"Invalid page number: '{part}'")
            if num < 1:
                raise ValueError(f"Page numbers must be positive: '{part}'")
            pages.add(num)
    if not pages:
        raise ValueError("Empty page range specification")
    return pages


def get_output_base_name(outputfile: str, inputfiles: list[str]) -> str:
    """
    Derive output base name from command-line arguments.

    Args:
        outputfile: The -o argument value (or None)
        inputfiles: List of input files (or empty list)

    Returns:
        Base name for output files (without extension)
    """
    if outputfile:
        # Extract base name from -o argument (remove path and extension)
        base = os.path.basename(outputfile)
        return os.path.splitext(base)[0]
    elif inputfiles:
        # Derive from first input file
        first = inputfiles[0]
        if first == "-":
            return "stdin"
        base = os.path.basename(first)
        return os.path.splitext(base)[0]
    else:
        # Interactive mode - use default
        return "page"


def _get_version() -> str:
    """Read the PostForge version from sysdict.ps (sole source of truth)."""
    sysdict = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "resources", "Init", "sysdict.ps")
    with open(sysdict, "r") as f:
        for line in f:
            m = re.search(r'/revisionstring\s+\(([^)]+)\)', line)
            if m:
                return m.group(1)
    return "unknown"


def build_argument_parser(available_devices: list[str]) -> argparse.ArgumentParser:
    """
    Create and configure the PostForge argument parser.

    Args:
        available_devices: List of available output device names.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="postforge",
        description="PostForge - PostScript Interpreter",
        epilog="If no input file is provided, PostForge will run in interactive mode.",
    )

    parser.add_argument(
        "-V", "--version", action="version",
        version=f"PostForge {_get_version()}"
    )
    parser.add_argument("inputfiles", nargs="*", help="PostScript input files to process (each as separate job)")
    parser.add_argument(
        "-o", "--output", dest="outputfile", help="Specify output filename"
    )
    parser.add_argument(
        "-d",
        "--device",
        choices=available_devices,
        help=f'Specify output device ({", ".join(available_devices)})',
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "--memory-profile", action="store_true",
        help="Enable memory profiling and generate detailed memory usage report"
    )
    parser.add_argument(
        "--gc-analysis", action="store_true",
        help="Enable garbage collection analysis (implies --memory-profile)"
    )

    parser.add_argument(
        "--leak-analysis", action="store_true",
        help="Enable detailed memory leak analysis (implies --memory-profile)"
    )

    # Performance profiling options
    parser.add_argument(
        "--profile", action="store_true",
        help="Enable performance profiling (default: cprofile)"
    )
    parser.add_argument(
        "--profile-type",
        choices=['cprofile', 'none'],  # Future: 'line', 'memory', 'py-spy'
        default='cprofile',
        help="Specify profiling backend type (default: cprofile)"
    )
    parser.add_argument(
        "--profile-output",
        help="Specify output file for profiling results (default: auto-generated)"
    )
    parser.add_argument(
        "--no-glyph-cache", action="store_true",
        help="Disable glyph caching (useful for debugging font rendering)"
    )
    parser.add_argument(
        "--cache-stats", action="store_true",
        help="Print glyph cache statistics after job completion"
    )
    parser.add_argument(
        "--output-dir", dest="output_dir", default="pf_output",
        help="Specify output directory (default: pf_output)"
    )
    parser.add_argument(
        "-r", "--resolution", type=int,
        help="Set device resolution in DPI (overrides device default, e.g., 150, 300, 600)"
    )
    parser.add_argument(
        "--pages",
        help="Page range to output (e.g., 1-5, 3, 1-3,7,10-12)"
    )
    parser.add_argument(
        "--antialias",
        choices=["none", "fast", "good", "best", "gray", "subpixel"],
        help="Set anti-aliasing mode for Cairo rendering (default: gray)"
    )
    parser.add_argument(
        "--text-as-paths", action="store_true",
        help="Render text as path outlines instead of native text objects (primarily affects PDF/SVG; "
             "bitmap devices already render text as paths)"
    )
    parser.add_argument(
        "--no-icc", action="store_true",
        help="Disable ICC color management (use PLRM formulas)"
    )
    parser.add_argument(
        "--cmyk-profile",
        help="Path to CMYK ICC profile for color management"
    )
    parser.add_argument(
        "--rebuild-font-cache", action="store_true",
        help="Force rebuild of the system font discovery cache (font name to file path mapping) and exit"
    )

    return parser
