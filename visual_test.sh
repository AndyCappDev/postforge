#!/bin/bash
# PostForge Visual Regression Test Launcher
# Activates virtual environment and runs visual_test.py with passed arguments
#
# Usage:
#   ./visual_test.sh --baseline                  # Generate baseline
#   ./visual_test.sh                             # Compare against baseline
#   ./visual_test.sh --baseline -- -d pdf        # Pass -d pdf to postforge
#   ./visual_test.sh -- --glyph-cache            # Pass --glyph-cache to postforge

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if virtual environment exists
if [ ! -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    echo "Virtual environment not found. Please run ./install.sh first"
    exit 1
fi

# Activate virtual environment
source "$SCRIPT_DIR/venv/bin/activate"

# Split arguments at "--" separator: args before go to visual_test.py,
# args after get forwarded to postforge via --flags.
VISUAL_ARGS=()
POSTFORGE_ARGS=()
FOUND_SEP=false
for arg in "$@"; do
    if [ "$arg" = "--" ] && ! $FOUND_SEP; then
        FOUND_SEP=true
        continue
    fi
    if $FOUND_SEP; then
        POSTFORGE_ARGS+=("$arg")
    else
        VISUAL_ARGS+=("$arg")
    fi
done

if [ ${#POSTFORGE_ARGS[@]} -gt 0 ]; then
    python "$SCRIPT_DIR/visual_test.py" "${VISUAL_ARGS[@]}" --flags "${POSTFORGE_ARGS[@]}"
else
    python "$SCRIPT_DIR/visual_test.py" "${VISUAL_ARGS[@]}"
fi

# Deactivate virtual environment
deactivate
