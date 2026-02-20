#!/usr/bin/env python3
# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge - PostScript Interpreter

This is the main entry point for PostForge, a PostScript interpreter implemented in Python.
PostForge provides both interactive and batch modes for executing PostScript files.

Architecture Overview:
    PostForge follows the PostScript language specification with a stack-based execution model:
    - Operand Stack: holds operands for operators
    - Execution Stack: holds objects to be executed
    - Dictionary Stack: provides variable scoping
    - Graphics State Stack: maintains graphics state for rendering

Key Components:
    - Context: Encapsulates the entire PostScript execution environment
    - System Parameters: Configuration paths and settings
    - Device Support: Pluggable output devices (PNG, PDF, SVG, etc.)
    - Error Handling: User-friendly error messages and recovery

Usage:
    Interactive Mode:
        postforge

    Batch Mode:
        postforge input.ps
        postforge -d png -o output.png input.ps

Author: Scott Bowman
License: AGPL-3.0-or-later
"""


import os
import sys
import tempfile

from .cli_args import build_argument_parser, _parse_page_ranges
from .cli_runner import run
from .core import icc_default
from .core import types as ps
from .core.system_font_cache import SystemFontCache
from .utils import profiler as ps_profiler


def main() -> int:
    """
    Main entry point for PostForge PostScript interpreter.

    Returns:
        Exit code: 0 for success, 1 for error
    """

    # Remember user's working directory — file paths on the command line
    # and in interactive mode should resolve relative to where pf was invoked
    package_dir = os.path.dirname(os.path.abspath(__file__))  # postforge/
    user_cwd = os.getcwd()

    # Get available devices from OutputDevice directory
    device_dir = os.path.join(package_dir, "resources", "OutputDevice")

    available_devices = []
    if os.path.exists(device_dir):
        for f in os.listdir(device_dir):
            if f.endswith(".ps"):
                available_devices.append(f[:-3])  # Remove .ps extension

    # Create argument parser and parse args
    parser = build_argument_parser(available_devices)
    args = parser.parse_intermixed_args()

    # Validate resolution range
    if args.resolution is not None:
        if args.resolution < 36 or args.resolution > 9600:
            print("PostForge Error: Resolution must be between 36 and 9600 DPI.")
            return 1

    # Validate --pages format early
    page_filter = None
    if args.pages:
        try:
            page_filter = _parse_page_ranges(args.pages)
        except ValueError as e:
            print(f"PostForge Error: {e}")
            print("Expected format: 1-5, 3, 1-3,7,10-12")
            return 1

    # Handle --rebuild-font-cache before context creation
    if args.rebuild_font_cache:
        cache = SystemFontCache.get_instance()
        cache.rebuild()
        print(f"System font cache rebuilt: {cache.font_count()} fonts found")
        return 0

    # Resolve input file paths to absolute (relative to user's CWD)
    inputfiles = [f if f == "-" else (os.path.join(user_cwd, f) if not os.path.isabs(f) else f)
                  for f in args.inputfiles]
    device = args.device
    memory_profile = args.memory_profile or args.gc_analysis or args.leak_analysis
    gc_analysis = args.gc_analysis
    leak_analysis = args.leak_analysis

    # Performance profiling setup
    performance_profile = args.profile
    profile_type = args.profile_type if performance_profile else 'none'
    profile_output = args.profile_output

    # Generate default output path if profiling enabled but no output specified
    if performance_profile and not profile_output:
        profile_output = ps_profiler.generate_default_output_path(profile_type)

    # Glyph cache control (enabled by default, disable with --no-glyph-cache)
    if args.no_glyph_cache:
        ps.global_resources.glyph_cache_disabled = True

    # ICC color management control
    if args.no_icc:
        icc_default.disable()
    if args.cmyk_profile:
        icc_default.set_custom_profile(args.cmyk_profile)
    # Initialize ICC eagerly so the profile message prints at startup
    icc_default.initialize()

    # Handle stdin input ("-" as filename)
    stdin_temp = None
    if "-" in inputfiles:
        if sys.stdin.isatty():
            print("PostForge Error: '-' specified but no data piped to stdin.")
            return 1
        stdin_temp = tempfile.NamedTemporaryFile(
            suffix=".ps", prefix="postforge_stdin_", delete=False
        )
        stdin_temp.write(sys.stdin.buffer.read())
        stdin_temp.close()
        inputfiles = [stdin_temp.name if f == "-" else f for f in inputfiles]

    try:
        return run(args, inputfiles, stdin_temp, user_cwd, package_dir,
                   available_devices, device, memory_profile,
                   gc_analysis, leak_analysis, performance_profile,
                   profile_type, profile_output, page_filter)
    finally:
        _cleanup_stdin_temp(stdin_temp)


def _cleanup_stdin_temp(stdin_temp):
    """Remove the temporary file created for stdin input."""
    if stdin_temp is not None:
        try:
            os.unlink(stdin_temp.name)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
