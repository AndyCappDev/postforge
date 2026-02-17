#!/bin/bash
# Build Cython extensions for PostForge
#
# Usage: ./build_cython.sh
#
# This compiles the Cython .pyx files into shared libraries (.so/.pyd)
# that are loaded as optional accelerators by PostForge.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment
source venv/bin/activate

echo "Building Cython extensions..."
python setup_cython.py build_ext --inplace

echo "Build complete."

# Verify the modules can be imported
python -c "from postforge.operators._control_cy import exec_exec; print('  Cython exec_exec: OK')" 2>/dev/null || echo "  Cython exec_exec: FAILED"
python -c "from postforge.devices.common._image_conv_cy import gray8_to_bgrx; print('  Cython image_conv: OK')" 2>/dev/null || echo "  Cython image_conv: FAILED"
