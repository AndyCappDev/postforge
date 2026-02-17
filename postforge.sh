#!/bin/bash
# PostForge PostScript Interpreter Launcher
# Activates virtual environment and runs PostForge with passed arguments

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if virtual environment exists
if [ ! -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    echo "Virtual environment not found. Please run ./install.sh first"
    exit 1
fi

# Activate virtual environment
source "$SCRIPT_DIR/venv/bin/activate"

# Run PostForge with all passed arguments
python -m postforge "$@"
POSTFORGE_EXIT=$?

# Deactivate virtual environment
deactivate
exit $POSTFORGE_EXIT
