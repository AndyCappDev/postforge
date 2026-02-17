#!/bin/bash

# PostForge Installation Script
# Checks prerequisites and sets up the Python environment

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================="
echo "  PostForge Installation Script"
echo "=================================="
echo ""

# Detect OS
detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
        echo "windows"
    elif [[ -f /etc/os-release ]]; then
        . /etc/os-release
        if [[ "$ID" == "ubuntu" || "$ID" == "debian" || "$ID_LIKE" == *"debian"* ]]; then
            echo "debian"
        elif [[ "$ID" == "fedora" ]]; then
            echo "fedora"
        elif [[ "$ID" == "centos" || "$ID" == "rhel" || "$ID_LIKE" == *"rhel"* ]]; then
            echo "rhel"
        elif [[ "$ID" == "arch" || "$ID_LIKE" == *"arch"* ]]; then
            echo "arch"
        elif [[ "$ID" == "opensuse"* || "$ID_LIKE" == *"suse"* ]]; then
            echo "suse"
        else
            echo "linux"
        fi
    else
        echo "unknown"
    fi
}

OS=$(detect_os)
echo "Detected OS: $OS"
echo ""

# Check Python version
echo "Checking Python version..."
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo -e "${RED}Error: Python not found.${NC}"
    echo "Please install Python 3.13+ and try again."
    exit 1
fi

PYTHON_VERSION=$($PYTHON_CMD -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.minor)')

echo "Found Python $PYTHON_VERSION"

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 12 ]]; then
    echo -e "${RED}Error: Python 3.12+ required (3.13+ recommended).${NC}"
    echo "Found Python $PYTHON_VERSION"
    exit 1
elif [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 13 ]]; then
    echo -e "${YELLOW}Warning: Python 3.13+ recommended for best experience.${NC}"
fi
echo -e "${GREEN}Python version OK${NC}"
echo ""

# Check for Cairo library (not needed on Windows - pip handles it)
if [[ "$OS" != "windows" ]]; then
    echo "Checking for Cairo graphics library..."

    CAIRO_FOUND=false

    if command -v pkg-config &> /dev/null; then
        if pkg-config --exists cairo 2>/dev/null; then
            CAIRO_FOUND=true
        fi
    fi

    # Fallback checks
    if [[ "$CAIRO_FOUND" == false ]]; then
        if [[ "$OS" == "macos" ]]; then
            if [[ -d "/opt/homebrew/lib/pkgconfig" ]] && PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig" pkg-config --exists cairo 2>/dev/null; then
                CAIRO_FOUND=true
            elif [[ -d "/usr/local/lib/pkgconfig" ]] && PKG_CONFIG_PATH="/usr/local/lib/pkgconfig" pkg-config --exists cairo 2>/dev/null; then
                CAIRO_FOUND=true
            fi
        elif ldconfig -p 2>/dev/null | grep -q libcairo; then
            CAIRO_FOUND=true
        fi
    fi

    if [[ "$CAIRO_FOUND" == false ]]; then
        echo -e "${RED}Cairo graphics library not found.${NC}"
        echo ""
        echo "Please install Cairo and run ./install.sh again:"
        echo ""

        case "$OS" in
            debian)
                echo -e "  ${YELLOW}sudo apt-get update && sudo apt-get install libcairo2-dev pkg-config python3-dev${NC}"
                ;;
            fedora)
                echo -e "  ${YELLOW}sudo dnf install cairo-devel pkgconfig python3-devel${NC}"
                ;;
            rhel)
                echo -e "  ${YELLOW}sudo yum install cairo-devel pkgconfig python3-devel${NC}"
                ;;
            arch)
                echo -e "  ${YELLOW}sudo pacman -S cairo pkgconf${NC}"
                ;;
            suse)
                echo -e "  ${YELLOW}sudo zypper install cairo-devel pkg-config python3-devel${NC}"
                ;;
            macos)
                echo -e "  ${YELLOW}brew install cairo pkg-config${NC}"
                ;;
            *)
                echo "  Install cairo development libraries for your distribution."
                ;;
        esac
        echo ""
        exit 1
    fi

    echo -e "${GREEN}Cairo found${NC}"
    echo ""
fi

# Create virtual environment
echo "Setting up Python virtual environment..."

if [[ -d "venv" && -x "venv/bin/python" ]]; then
    echo "Virtual environment already exists."
else
    # Remove partial venv from a previous failed attempt
    rm -rf venv 2>/dev/null
    if ! $PYTHON_CMD -m venv venv 2>&1; then
        rm -rf venv 2>/dev/null
        echo ""
        echo -e "${RED}Failed to create virtual environment.${NC}"
        echo ""
        case "$OS" in
            debian)
                echo "On Debian/Ubuntu, install the venv package:"
                echo ""
                echo -e "  ${YELLOW}sudo apt install python${PYTHON_VERSION}-venv${NC}"
                ;;
            fedora)
                echo "On Fedora, install the venv package:"
                echo ""
                echo -e "  ${YELLOW}sudo dnf install python${PYTHON_VERSION//.}-venv${NC}"
                ;;
            arch)
                echo "On Arch Linux, venv is included with python. Ensure python is installed:"
                echo ""
                echo -e "  ${YELLOW}sudo pacman -S python${NC}"
                ;;
            suse)
                echo "On openSUSE, install the venv package:"
                echo ""
                echo -e "  ${YELLOW}sudo zypper install python${PYTHON_VERSION//.}-venv${NC}"
                ;;
            macos)
                echo "On macOS, reinstall Python via Homebrew:"
                echo ""
                echo -e "  ${YELLOW}brew install python@${PYTHON_VERSION}${NC}"
                ;;
            *)
                echo "Install the Python venv module for your distribution."
                ;;
        esac
        echo ""
        echo "Then run ./install.sh again."
        exit 1
    fi
    echo "Created virtual environment."
fi

# Install package with dependencies
echo "Installing Python dependencies..."
echo ""

./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -e ".[qt,dev,visual-test]"

# Build Cython accelerators (optional — PostForge runs without them)
echo ""
echo "Building Cython accelerators..."
if ./venv/bin/python setup_cython.py build_ext --inplace 2>&1; then
    echo -e "${GREEN}Cython build OK — execution loop accelerated (15-40% speedup)${NC}"
else
    echo -e "${YELLOW}Cython build failed — PostForge will use the pure Python fallback.${NC}"
    echo "To enable Cython acceleration, install a C compiler:"
    echo ""
    case "$OS" in
        debian)
            echo -e "  ${YELLOW}sudo apt install build-essential${NC}"
            ;;
        fedora)
            echo -e "  ${YELLOW}sudo dnf install gcc${NC}"
            ;;
        rhel)
            echo -e "  ${YELLOW}sudo yum install gcc${NC}"
            ;;
        arch)
            echo -e "  ${YELLOW}sudo pacman -S base-devel${NC}"
            ;;
        suse)
            echo -e "  ${YELLOW}sudo zypper install gcc${NC}"
            ;;
        macos)
            echo -e "  ${YELLOW}xcode-select --install${NC}"
            ;;
        *)
            echo "  Install a C compiler (gcc or clang) for your distribution."
            ;;
    esac
    echo ""
    echo "Then run ./install.sh again."
fi

# Install system-wide launcher commands
echo ""
echo "Installing system commands (pf, postforge)..."

INSTALL_DIR="/usr/local/bin"

# Create the pf launcher
TEMP_PF=$(mktemp)
cat > "$TEMP_PF" <<EOF
#!/bin/bash
# PostForge PostScript Interpreter
# Auto-generated by install.sh — do not edit
exec "$SCRIPT_DIR/venv/bin/python" -m postforge "\$@"
EOF
chmod +x "$TEMP_PF"

# Create the postforge launcher
TEMP_POSTFORGE=$(mktemp)
cat > "$TEMP_POSTFORGE" <<EOF
#!/bin/bash
# PostForge PostScript Interpreter
# Auto-generated by install.sh — do not edit
exec "$SCRIPT_DIR/venv/bin/python" -m postforge "\$@"
EOF
chmod +x "$TEMP_POSTFORGE"

# Install to /usr/local/bin (needs sudo)
if sudo mv "$TEMP_PF" "$INSTALL_DIR/pf" && sudo mv "$TEMP_POSTFORGE" "$INSTALL_DIR/postforge"; then
    echo -e "${GREEN}Installed: ${INSTALL_DIR}/pf${NC}"
    echo -e "${GREEN}Installed: ${INSTALL_DIR}/postforge${NC}"
else
    echo -e "${YELLOW}Could not install to ${INSTALL_DIR} (sudo required).${NC}"
    echo "You can still run PostForge with: ./postforge.sh"
    rm -f "$TEMP_PF" "$TEMP_POSTFORGE" 2>/dev/null
fi

echo ""
echo -e "${GREEN}=================================="
echo -e "  Installation Complete!"
echo -e "==================================${NC}"
echo ""
echo "Run PostForge with:"
echo ""
echo "  pf                                  # Interactive prompt"
echo "  pf samples/tiger.ps                 # Render the classic tiger"
echo "  pf -d png input.ps                  # Save to ./output directory"
echo ""
