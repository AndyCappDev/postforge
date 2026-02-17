#!/bin/bash

# PostForge Uninstall Script
# Removes virtual environment, system commands, and cached data

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================="
echo "  PostForge Uninstall"
echo "=================================="
echo ""

# Remove system-wide launcher commands
INSTALL_DIR="/usr/local/bin"
REMOVED_CMDS=false

for cmd in pf postforge; do
    if [[ -f "$INSTALL_DIR/$cmd" ]]; then
        # Verify it's a PostForge launcher before removing
        if grep -q "postforge" "$INSTALL_DIR/$cmd" 2>/dev/null; then
            echo "Removing $INSTALL_DIR/$cmd..."
            if sudo rm "$INSTALL_DIR/$cmd"; then
                echo -e "${GREEN}Removed: $INSTALL_DIR/$cmd${NC}"
                REMOVED_CMDS=true
            else
                echo -e "${YELLOW}Could not remove $INSTALL_DIR/$cmd (sudo required).${NC}"
            fi
        fi
    fi
done

if [[ "$REMOVED_CMDS" == false ]]; then
    echo "No system commands found in $INSTALL_DIR."
fi
echo ""

# Remove virtual environment
if [[ -d "venv" ]]; then
    echo "Removing virtual environment..."
    rm -rf venv
    echo -e "${GREEN}Removed: venv/${NC}"
else
    echo "No virtual environment found."
fi
echo ""

# Remove font discovery cache
CACHE_DIR="$HOME/.cache/postforge"
if [[ -d "$CACHE_DIR" ]]; then
    echo "Removing font cache..."
    rm -rf "$CACHE_DIR"
    echo -e "${GREEN}Removed: $CACHE_DIR${NC}"
else
    echo "No font cache found."
fi
echo ""

# Remove build artifacts
CLEANED=false
if [[ -d "build" ]]; then
    rm -rf build
    echo -e "${GREEN}Removed: build/${NC}"
    CLEANED=true
fi
if [[ -d "postforge.egg-info" ]]; then
    rm -rf postforge.egg-info
    echo -e "${GREEN}Removed: postforge.egg-info/${NC}"
    CLEANED=true
fi
for so_file in postforge/operators/_control_cy*.so postforge/devices/common/_image_conv_cy*.so; do
    if [[ -f "$so_file" ]]; then
        rm -f "$so_file"
        echo -e "${GREEN}Removed: $so_file${NC}"
        CLEANED=true
    fi
done
if [[ "$CLEANED" == false ]]; then
    echo "No build artifacts found."
fi
echo ""

echo -e "${GREEN}=================================="
echo -e "  Uninstall Complete"
echo -e "==================================${NC}"
echo ""
echo "The PostForge source code is still in: $SCRIPT_DIR"
echo "To remove it entirely: rm -rf $SCRIPT_DIR"
echo ""
