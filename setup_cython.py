# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Standalone Cython build script for PostForge performance-critical modules.

Usage:
    python setup_cython.py build_ext --inplace

This compiles .pyx files into C extensions that are used as optional
accelerators. PostForge works without them — the pure Python fallback
is always available.
"""

from setuptools import setup, Extension
from Cython.Build import cythonize

extensions = [
    Extension(
        "postforge.operators._control_cy",
        ["postforge/operators/_control_cy.pyx"],
    ),
    Extension(
        "postforge.devices.common._image_conv_cy",
        ["postforge/devices/common/_image_conv_cy.pyx"],
    ),
]

setup(
    ext_modules=cythonize(extensions, language_level=3),
)
