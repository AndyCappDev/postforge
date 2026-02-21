# Visual Regression Testing

PostForge includes a visual regression testing tool that renders all sample
PostScript files and compares them pixel-by-pixel against stored baselines.
This catches unintended visual changes introduced by code modifications.

Three output devices are tested: **PNG** (direct pixel output), **PDF**
(rasterized via PyMuPDF at 300 DPI), and **SVG** (rasterized via PyMuPDF
at 300 DPI). All three are tested by default. The TIFF device uses the same
Cairo rendering pipeline as PNG and is not separately tested.

## Requirements

- Python virtual environment set up via `./install.sh`
- **Pillow** — required for image comparison
- **PyMuPDF** — required for PDF and SVG rasterization

## Quick Start

*Linux/Mac:*
```bash
# 1. Generate baseline images (all devices)
./visual_test.sh --baseline

# 2. Make code changes, then compare against baseline
./visual_test.sh
```

*Windows:*
```cmd
visual_test.bat --baseline
visual_test.bat
```

The comparison renders all samples fresh, diffs them against the baseline,
and generates an HTML report for each device plus a combined report at
`visual_tests_report.html`.

## Command Reference

All commands are run from the project root directory using `./visual_test.sh`
(Linux/Mac) or `visual_test.bat` (Windows), which handles virtual environment
activation automatically. The examples below use `./visual_test.sh` but
`visual_test.bat` accepts the same arguments.

### Generate Baseline

```bash
./visual_test.sh --baseline                  # All devices (png, pdf, svg)
./visual_test.sh --baseline -d png           # PNG only
./visual_test.sh --baseline -d pdf svg       # PDF and SVG only
```

Renders all `samples/*.ps` and `samples/*.eps` files and stores them under
per-device baseline directories. This also saves per-file render timings for
later comparison.

**Warning:** This wipes the entire baseline directory for the selected
device(s) and regenerates everything.

**Tip:** Generate your baseline *before* making code changes. If you
regenerate the baseline after a buggy change, the bug becomes the new
baseline and the comparison is worthless.

### Compare Against Baseline

```bash
./visual_test.sh                             # All devices
./visual_test.sh -d png                      # PNG only
./visual_test.sh -d pdf svg                  # PDF and SVG only
```

Renders all samples to the current directory for each device, compares
against the baseline, and produces HTML reports. The exit code is `1` if
any files fail, `0` otherwise.

### Options

| Option | Description |
|--------|-------------|
| `--baseline` | Generate baseline images instead of comparing |
| `-d`, `--device` | Device(s) to test: `png`, `pdf`, `svg`, or `all` (default: all three) |
| `--threshold N` | Global max allowed pixel difference percentage (default: `0` = exact match) |
| `--timeout N` | Per-sample render timeout in seconds (default: `600`) |
| `--samples file1.ps file2.ps` | Test only specific sample files |
| `--exclude file1.ps file2.ps` | Exclude specific sample files |
| `--html path/to/report.html` | Custom HTML report path |
| `-j N` / `--jobs N` | Number of parallel render/compare processes (default: `4`) |
| `-- --flag1 --flag2` | Extra flags to pass through to postforge (everything after `--`) |

### Examples

```bash
# Test specific samples only
./visual_test.sh --samples tiger.ps escher.ps

# Exclude slow or problematic samples
./visual_test.sh --exclude EazyBBS.ps JavaPlatform.ps

# Test only PDF device
./visual_test.sh -d pdf

# Pass postforge flags after --
./visual_test.sh -- --no-glyph-cache

# Custom HTML report location
./visual_test.sh --html reports/my_report.html

# Increase timeout for slow samples
./visual_test.sh --timeout 300

# Run with 8 parallel workers
./visual_test.sh -j 8
```

## How PDF and SVG Comparison Works

PNG output is compared directly. For PDF and SVG, an extra rasterization
step converts the vector output to PNG at 300 DPI before comparison:

- **PDF**: Rasterized using PyMuPDF. Each page of the PDF is converted to
  a separate PNG. The original PDF is also kept in the output directory.
- **SVG**: Rasterized using PyMuPDF. Each SVG file is converted to a PNG.
  The original SVG is also kept in the output directory.

This means PDF and SVG comparisons test the full pipeline including font
embedding, text positioning, and vector rendering accuracy.

## Configuration Files

Each device has its own configuration file for per-sample threshold
overrides:

| Device | Config File |
|--------|-------------|
| PNG | `visual_tests_png.conf` |
| PDF | `visual_tests_pdf.conf` |
| SVG | `visual_tests_svg.conf` |

### Format

```
# Lines starting with # are comments
# Format: filename.ps  threshold_percentage
filename.ps  threshold
```

- **`threshold`** is the maximum allowed pixel difference percentage
  (floating point)
- **`100`** = render-only check (any pixel diff is OK, just verify the
  file renders without errors)
- **`0`** = exact pixel match required (the default for files not listed)

### Example

```
# Fully random output - just check it renders
snowflak.ps   95

# Contains some randomized elements
fern.ps       8
maze.ps       7

# Minor floating-point rendering differences
ppst32.ps     0.15
```

Samples not listed in the config file use the global threshold (default `0`,
or whatever `--threshold` specifies).

## HTML Reports

### Per-Device Reports

Each device generates its own HTML report at
`visual_tests_{device}/report.html` containing:

- **Summary section** — total sample count, pass/fail/error/skip counts,
  wall-clock and summed render times for baseline and current runs
- **Per-sample details** — sample name, page count, threshold, pixel
  difference percentage, status, baseline and current render times, and
  images with a lightbox viewer

### Combined Report

When multiple devices are tested, a combined report is generated at
`visual_tests_report.html` with a navigation bar linking to each device
section. Each section contains the full per-device report.

### Image Display Rules

- **Single page, pass**: shows the rendered page
- **Multi-page, pass**: shows only the first page
- **Multi-page, fail**: shows only the failed pages with page numbers and
  diff images

### Status Types

| Status | Meaning |
|--------|---------|
| **PASS** | Pixel difference is within the threshold |
| **FAIL** | Pixel difference exceeds the threshold |
| **ERROR** | Render failed with a PostScript error or Python traceback (shown in report) |
| **SKIP** | Sample produced no pages and no errors (by design) |
| **NEW** | Sample has no corresponding baseline |
| **MISSING** | Baseline directory exists but contains no PNGs |

## Adding New Samples

When adding a new `.ps` file to `samples/`:

1. Run `./visual_test.sh --baseline` to regenerate all baselines
2. If the sample contains non-deterministic output, add an entry to each
   device config file with an appropriate threshold

## Directory Structure

```
visual_tests_png/
├── baseline/             # Baseline PNGs (per-sample subdirectories)
├── current/              # Current run PNGs (regenerated each comparison)
├── diff/                 # Amplified diff images for changed samples
├── baseline_timings.txt  # Saved baseline render timings
└── report.html           # HTML comparison report

visual_tests_pdf/
├── baseline/             # Baseline PDFs + rasterized PNGs
├── current/              # Current PDFs + rasterized PNGs
├── diff/                 # Diff images
├── baseline_timings.txt
└── report.html

visual_tests_svg/
├── baseline/             # Baseline SVGs + rasterized PNGs
├── current/              # Current SVGs + rasterized PNGs
├── diff/                 # Diff images
├── baseline_timings.txt
└── report.html

visual_tests_report.html    # Combined report (when multiple devices tested)
visual_tests_png.conf       # Per-sample thresholds for PNG
visual_tests_pdf.conf       # Per-sample thresholds for PDF
visual_tests_svg.conf       # Per-sample thresholds for SVG
visual_test.py              # Test script
visual_test.sh              # Launcher for Linux/Mac (activates venv)
visual_test.bat             # Launcher for Windows (activates venv)
```

The `visual_tests_*` directories are in `.gitignore` as the generated images
are large and machine-specific. The config files are checked into the
repository.
