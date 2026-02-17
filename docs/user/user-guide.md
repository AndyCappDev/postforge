# PostForge User Guide

## Introduction

PostForge is a Python implementation of a PostScript interpreter that provides
**strict Level 2 compatibility** while implementing Level 3 features. It can
render PostScript files to PNG, PDF, SVG, or display them in an interactive Qt
window.

This guide covers installation, command line usage, the interactive display,
output options, and debugging features.

## Getting Started

### Installation

```bash
git clone https://github.com/AndyCappDev/postforge.git
cd postforge
./install.sh
```

The install script checks for Python 3.13+ and Cairo, creates a virtual
environment, installs all dependencies, and installs the `pf` command.

### Running PostForge

```bash
pf                                       # Interactive mode
pf input_file.ps                         # Render to Qt display window
pf -d png input_file.ps                  # Render to PNG files
pf -d pdf input_file.ps                  # Render to PDF
pf -d svg input_file.ps                  # Render to SVG
```

The launcher scripts `./postforge.sh` (Linux/Mac) and `postforge.bat` (Windows)
are also available if the `pf` command was not installed system-wide.

### Reading from Standard Input

Use `-` as the input filename to read PostScript from stdin. This lets you
pipe content from other programs or shell pipelines:

```bash
cat document.ps | pf -d png -
generate_ps.py | pf -d pdf -
curl https://example.com/file.ps | pf -
```

Stdin input can be mixed with regular files. Each is processed as a separate
job:

```bash
pf -d png header.ps - footer.ps
```

Output files from stdin input use `stdin` as the base name (e.g.,
`pf_output/stdin-0001.png`). If no data is piped and `-` is specified,
PostForge prints an error and exits.

### Interactive Mode

When run without an input file, PostForge starts an interactive PostScript
prompt:

```bash
pf
PF[8 3 0]> 5 3 add ==
8
PF[8 3 0]> (Hello, PostScript!) ==
Hello, PostScript!
PF[8 3 0]> quit
```

The prompt `PF[8 3 0]>` shows the current interpreter state: execution stack
depth, dictionary stack depth, and operand stack item count. As you push values
onto the operand stack, the third number updates accordingly.

Type `quit` or press Ctrl-D to exit.

## Command Line Reference

### Output Options

| Option | Description |
|--------|-------------|
| `-d`, `--device` | Output device: `png`, `pdf`, `svg`, or `qt` (default: `qt` if available, otherwise `png`) |
| `-o`, `--output` | Output filename (base name for page numbering; device inferred from extension if `-d` not given) |
| `--output-dir` | Output directory (default: `pf_output`) |

### Rendering Options

| Option | Description |
|--------|-------------|
| `-r`, `--resolution` | Device resolution in DPI (default: 300 for PNG, 72 for PDF/SVG, screen resolution for Qt) |
| `--pages` | Page range to output (e.g., `1-5`, `3`, `1-3,7,10-12`) |
| `--antialias` | Anti-aliasing mode: `none`, `fast`, `good`, `best`, `gray`, `subpixel` (default: `gray`) |

### Color Management

| Option | Description |
|--------|-------------|
| `--no-icc` | Disable ICC color management and use PLRM conversion formulas instead |
| `--cmyk-profile` | Path to a CMYK ICC profile for DeviceCMYK color conversion |

### Font Options

| Option | Description |
|--------|-------------|
| `--no-glyph-cache` | Disable glyph caching (useful for debugging font rendering) |
| `--cache-stats` | Print glyph cache hit/miss statistics after job completion |
| `--rebuild-font-cache` | Force rebuild of the system font discovery cache and exit |

### Profiling and Debugging

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose output |
| `--profile` | Enable cProfile performance profiling |
| `--profile-type` | Profiling backend: `cprofile` or `none` (default: `cprofile`) |
| `--profile-output` | Output file for profiling results (default: auto-generated with timestamp) |
| `--memory-profile` | Enable memory usage reporting |
| `--gc-analysis` | Enable garbage collection analysis (implies `--memory-profile`) |
| `--leak-analysis` | Enable memory leak detection (implies `--memory-profile`) |

### General

| Option | Description |
|--------|-------------|
| `-h`, `--help` | Show help message and exit |

## Output Devices

### PNG (`-d png`)

Renders each page to a separate PNG file.

```bash
pf -d png document.ps                   # 300 DPI (default)
pf -d png -r 600 document.ps            # 600 DPI
```

### PDF (`-d pdf`)

Renders each page to a PDF file with embedded fonts. Type 1, TrueType
(Type 42), CID, and Type 3 fonts are embedded with subsetting.

```bash
pf -d pdf document.ps
```

### SVG (`-d svg`)

Renders each page to a separate SVG file.
Text is preserved as selectable text elements with CSS font-family fallbacks.

```bash
pf -d svg document.ps
```

### Qt Display (`-d qt`)

Opens an interactive display window. This is the default device when Qt is
available.

- In interactive mode, the window updates live as PostScript commands are
  entered.
- In batch mode (with an input file), the window updates on each `showpage`.

```bash
pf document.ps                           # Opens Qt window
```

See [Using the Qt Display Window](#using-the-qt-display-window) below for
controls and keybindings.

### Output File Naming

File-based devices (PNG, PDF, SVG) save output to a `pf_output` directory in
the current working directory. The directory is created automatically if it
does not exist. Use `--output-dir` to specify a different location.

Output files are named using the pattern:

```
{base_name}-{page_number}.{extension}
```

The base name comes from:
1. The `-o` filename (without extension), if specified
2. The input filename (without extension), otherwise
3. `page` in interactive mode

Examples:

| Command | Output Files |
|---------|--------------|
| `pf -d png input.ps` | `pf_output/input-0001.png` |
| `pf -o result.png input.ps` | `pf_output/result-0001.png` |
| `pf --output-dir renders -d png input.ps` | `renders/input-0001.png` |
| `pf -d pdf input.ps` | `pf_output/input-0001.pdf` |
| `pf -d svg input.ps` | `pf_output/input-0001.svg` |

### Multiple Input Files

Multiple PostScript files can be passed on the command line. Each file runs
as a separate job within the interpreter's job server, which provides VM
isolation between files via save/restore encapsulation:

```bash
pf -d png file1.ps file2.ps file3.ps
```

## Using the Qt Display Window

The Qt display window is PostForge's default output device. It renders
PostScript pages at high resolution and provides controls for navigating
multi-page documents, zooming, and panning.

### Page Navigation

Each `showpage` in the PostScript program pauses rendering and waits for input.
The window title changes to indicate the current state:

| Title | Meaning |
|-------|---------|
| **PostForge** | Rendering in progress |
| **PostForge - Press any key to continue...** | Waiting at a page break |
| **PostForge - Press Q or close window to exit** | Last page reached |

Press **any key** (other than the view controls listed below) to advance to
the next page. After the last page, the window stays open until you press
**Q**, **Escape**, or close it.

### Keyboard Controls

| Key | Action |
|-----|--------|
| **Any key** | Advance to next page (when waiting at a page break) |
| **+** or **=** | Zoom in (25% per step, up to native resolution) |
| **-** | Zoom out (25% per step, minimum 0.1x) |
| **0** | Reset zoom and pan (fit page to window) |
| **Arrow keys** | Pan the view (50 pixels per step) |
| **Q** | Quit PostForge |
| **Escape** | Quit PostForge |

The view control keys (**+**, **-**, **0**, arrow keys) do not advance the
page — they only adjust the view.

### Mouse Controls

| Action | Effect |
|--------|--------|
| **Scroll wheel up** | Zoom in, centered on the cursor |
| **Scroll wheel down** | Zoom out, centered on the cursor |
| **Click and drag** | Pan the view |
| **Double-click** | Reset zoom and pan (same as pressing **0**) |

### View Behavior

- Pages are automatically scaled to fit the window while preserving aspect
  ratio.
- Zooming centers on the mouse cursor position (scroll wheel) or the window
  center (keyboard).
- The window renders at screen resolution by default, or at the resolution
  specified with `-r`.
- Closing the window with the window's close button exits PostForge
  immediately.

## Resolution and Anti-Aliasing

### Resolution (`-r`)

The `-r` flag sets the device resolution in dots per inch. Higher values
produce larger, more detailed output.

```bash
pf -d png -r 150 document.ps            # 150 DPI
pf -d png -r 300 document.ps            # 300 DPI (print quality)
```

The default resolution depends on the device: 300 DPI for PNG, 72 DPI for
PDF and SVG, and screen resolution for the Qt display. The `-r` flag can
be used with any device, including Qt.

### Anti-Aliasing (`--antialias`)

Controls the anti-aliasing mode for rendered output. The default is
`gray`, which provides good quality for most use cases.

| Mode | Description |
|------|-------------|
| `none` | No anti-aliasing (sharp pixel edges) |
| `fast` | Fast, lower-quality anti-aliasing |
| `good` | Balanced quality/speed |
| `best` | Highest quality anti-aliasing |
| `gray` | Grayscale anti-aliasing (default) |
| `subpixel` | Subpixel anti-aliasing (LCD-optimized) |

```bash
pf -d png --antialias none document.ps
pf -d png --antialias best -r 300 document.ps
```

## Page Selection

The `--pages` flag selects which pages to render from a multi-page document.
The PostScript program executes fully regardless — only the device output is
filtered. This makes it fast to extract specific pages from large documents.

### Syntax

| Format | Meaning |
|--------|---------|
| `--pages 5` | Page 5 only |
| `--pages 1-5` | Pages 1 through 5 |
| `--pages 1,3,5` | Pages 1, 3, and 5 |
| `--pages 1-3,7,10-12` | Pages 1-3, 7, and 10-12 |

Page numbers are 1-based and refer to `showpage` invocations.

### Examples

```bash
pf -d png --pages 1 document.ps             # First page only
pf -d pdf --pages 2-5 document.ps           # Pages 2 through 5
pf -d png --pages 1,3,5 document.ps         # Specific pages
```

### Behavior

- **Full execution**: The PostScript program runs completely. Page filtering
  only skips the device rendering step, so page numbering, fonts, and
  graphics state are unaffected.
- **Early termination**: Once all selected pages have been rendered, PostForge
  stops execution early rather than processing the remainder of the document.
- **Multiple input files**: When processing multiple files, `--pages` applies
  independently to each file. `--pages 1-3` selects pages 1-3 from every
  input file.
- **PDF output**: Filtered pages are omitted from the PDF entirely — the
  resulting file contains only the selected pages.

## Color Management

PostForge includes ICC-based color management for accurate color conversion
between color spaces (DeviceGray, DeviceRGB, DeviceCMYK, CIEBased, ICCBased).

### Default Behavior

ICC color management is enabled by default. PostForge searches for a
system-installed CMYK ICC profile and uses it for DeviceCMYK color
conversion. If no profile is found, it falls back to the PLRM-specified
conversion formulas. Output is correct either way, but ICC profiles
produce more accurate CMYK colors.

### System Profile Locations

PostForge searches the following locations for a CMYK profile:

| Platform | Locations searched |
|---|---|
| **Linux** | `/usr/share/color/icc/ghostscript/`, `/usr/share/color/icc/colord/` (SWOP and FOGRA profiles) |
| **macOS** | `/Library/ColorSync/Profiles/`, `~/Library/ColorSync/Profiles/`, `/System/Library/ColorSync/Profiles/` |
| **Windows** | `%SYSTEMROOT%\System32\spool\drivers\color\` |

On Linux, CMYK profiles are typically installed with GhostScript or the
`colord` package. On macOS, profiles are included with the system or
installed by print drivers. On Windows, CMYK profiles are only present if
installed by printer drivers or added manually.

If no system CMYK profile is found, use `--cmyk-profile` to specify one
(see below), or PostForge will use the PLRM conversion formulas.

### Custom CMYK Profile (`--cmyk-profile`)

Specify a custom CMYK ICC profile for DeviceCMYK color conversion:

```bash
pf -d png --cmyk-profile /path/to/profile.icc document.ps
```

Free CMYK profiles such as SWOP or FOGRA can be downloaded from the
[ICC Profile Registry](https://www.color.org/registry/index.xalter) or
from your Linux distribution's `colord` package.

### Disabling ICC (`--no-icc`)

Use `--no-icc` to disable ICC color management entirely and use the PLRM
conversion formulas for all color space conversions:

```bash
pf -d png --no-icc document.ps
```

## Debugging PostScript Programs

### Execution History

PostForge tracks the last N operations processed by the execution engine. When
a PostScript error occurs, the history is displayed as a call stack trace
showing exactly what the interpreter was doing when the error happened.

#### Enabling Execution History

Execution history is **disabled** by default for performance. Enable it from
within your PostScript code:

```postscript
<< /ExecutionHistory true >> setuserparams
```

Configure the number of operations to track (default: 20):

```postscript
<<
    /ExecutionHistory true
    /ExecutionHistorySize 30
>> setuserparams
```

#### Example

Given this PostScript code:

```postscript
<< /ExecutionHistory true >> setuserparams

3 0 {div} exec    % Fails: division by zero
```

PostForge displays:

```
** EXECUTION HISTORY **
Operator - --stopped--
Array - {--cvx-- --exec--}
Operator - --cvx--
Array - {--exec--}
Operator - --exec--
Name - }
Operator - --procedure_from_mark--
Operator - --exec--
Array - {div}
Name - div
Operator - --div--
>>> ERROR OCCURRED HERE <<<

Error: /undefinedresult in --div--
```

Each line shows the object type and value. The sequence reads chronologically
from top to bottom, ending at the point of failure.

#### Programmatic Access

```postscript
10 array exechistorystack
% Returns a subarray containing execution history strings
```

#### Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ExecutionHistory` | boolean | `false` | Enable/disable tracking |
| `ExecutionHistorySize` | integer | `20` | Number of operations to track |

### Traditional Debugging

PostScript provides built-in operators for inspecting interpreter state:

```postscript
stack                           % Print the operand stack (non-destructive)
count ==                        % Print number of items on operand stack
(message) print                 % Print a string to stdout
somevalue ==                    % Print any value to stdout
```

### PostForge Extensions

PostForge adds several convenience procedures for interactive use:

| Command | Description |
|---------|-------------|
| `/operator help` | Print the PLRM documentation for a built-in operator |
| `pstack` | Print the operand stack using `==` format (non-destructive, shows composite object structure) |
| `ppstack` | Same as `pstack` but with a header |

```postscript
/moveto help                    % Show documentation for moveto
(hello) [1 2 3] pstack          % Prints: [1 2 3]\n(hello)
```

## Font Caching

PostForge uses two independent caches related to fonts:

### System Font Discovery Cache

PostForge maintains a cache of system font locations
(`~/.cache/postforge/system_fonts.json`) that maps PostScript font names to
installed font files. This cache is built automatically on first run and
rebuilds when font directories change.

To force a rebuild (e.g., after installing new system fonts):

```bash
pf --rebuild-font-cache
```

### Glyph Rendering Cache

PostForge caches rendered glyph paths and bitmaps to avoid re-interpreting
font data for repeated characters. This applies to all scalable font types
(Type 1, Type 2/CFF, Type 3, Type 42, and CID fonts). The bitmap cache
size is controlled by the `MaxFontCache` system parameter (default 64 MB).

To inspect glyph cache performance:

```bash
pf --cache-stats document.ps
```

To disable glyph caching (for debugging font rendering issues):

```bash
pf --no-glyph-cache document.ps
```
