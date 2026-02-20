# Adding an Output Device

This guide walks through creating a new output device for PostForge. An output
device takes the PostScript display list — the accumulated graphics operations
from a page — and renders it to some output format (image file, PDF, screen
display, etc.).

PostForge ships with four devices:

| Device | Output | Complexity |
|--------|--------|------------|
| **PNG** | One PNG file per page | Simple — stateless, single-page |
| **PDF** | Single multi-page PDF | Complex — persistent state, font embedding |
| **SVG** | One SVG file per page | Moderate — post-processing for text elements |
| **Qt** | Interactive window | Complex — live rendering, zoom/pan |

The PNG device is the simplest and is used as the primary example throughout
this guide.


## How Devices Work

A device consists of two files:

1. **PostScript resource file** (`postforge/resources/OutputDevice/<name>.ps`) — a page
   device dictionary that configures the device from the PostScript side
2. **Python module** (`postforge/devices/<name>/`) — a Python package that
   renders the display list to output

### The showpage Flow

When PostScript code calls `showpage`, the following sequence executes:

```
PostScript showpage
  → device_output.py showpage()
    → Execute EndPage procedure from page device dict
    → If EndPage returns true:
        → Increment PageCount
        → Create output directory
        → importlib.import_module(f"postforge.devices.{device_name}")
        → device.showpage(ctxt, pd)
    → erasepage (clear display list)
    → initgraphics (reset graphics state)
    → Execute BeginPage procedure
```

The key dispatch code in `postforge/operators/device_output.py` (lines 313–314):

```python
device = importlib.import_module(f"postforge.devices.{device_name}")
device.showpage(ctxt, pd)
```

The device name comes from the `/OutputDevice` key in the page device
dictionary. PostForge dynamically imports the Python package matching that name.

### Device Discovery

When PostScript code calls `setpagedevice` with an `/OutputDevice` name,
PostForge loads the matching `.ps` file from `postforge/resources/OutputDevice/` via the
resource system. This file evaluates to a dictionary that becomes the page
device dictionary — the configuration and state bridge between PostScript and
Python.


## Creating the PostScript Resource File

Create `postforge/resources/OutputDevice/<name>.ps`. This file must evaluate to a
dictionary (using `<< >>` syntax). Here is a minimal template based on the PNG
device:

```postscript
%% pagedevice dictionary for mydevice output device

<<
    /OutputDeviceName (mydevice)
    /OutputDevice /mydevice
    /PageSize [612 792]
    /HWResolution [300 300]
    /Margins [0.0 0.0]
    /PageOffset [0 0]
    /NumCopies null
    /Install {
        .9 setflat
        {} settransfer
        /DeviceRGB setcolorspace
    }
    /BeginPage {pop}
    /EndPage {
        dup 0 eq {
            pop (showpage: Creating output for page ) print 1 add == true
        } {
            1 eq {
                pop (copypage: Creating output for page ) print 1 add == true
            } {
                pop false
            } ifelse
        } ifelse
    }
    /InputAttributes <<>>
    /OutputAttributes <<>>
    /ColorModel /DeviceRGB
    /Policies <<>>
    /PageCount 0
    /.IsPageDevice true
    /Colors 3
    /LineWidthMin 1
    /TextRenderingMode /GlyphPaths
    /StrokeMethod /StrokePathFill
>>
```

### Key Dictionary Entries

**Identity and configuration:**

| Key | Type | Description |
|-----|------|-------------|
| `/OutputDeviceName` | string | Human-readable device name |
| `/OutputDevice` | name | Device identifier — must match the Python package name under `postforge/devices/` |
| `/PageSize` | array | `[width height]` in PostScript points (1/72 inch) |
| `/HWResolution` | array | `[xdpi ydpi]` — device resolution. PostForge computes `MediaSize` as `PageSize * HWResolution / 72` |
| `/.IsPageDevice` | boolean | Must be `true` for `setpagedevice` to run `initgraphics`/`erasepage` |
| `/PageCount` | integer | Page counter, starts at 0. Incremented by `showpage` before calling the device |
| `/LineWidthMin` | number | Minimum rendered line width in device pixels. Set to 1 for bitmap devices, smaller (e.g., 0.001) for vector devices like PDF |

**Procedures:**

| Key | Type | Description |
|-----|------|-------------|
| `/Install` | procedure | Runs when device is activated. Use to set flatness, transfer function, and color space |
| `/BeginPage` | procedure | Runs at the start of each page. Receives page count on the stack. Usually `{pop}` |
| `/EndPage` | procedure | Runs at the end of each page. Receives page count and a reason code (0=showpage, 1=copypage, 2=device deactivation). Must return a boolean — `true` to output the page, `false` to skip |

**Rendering mode:**

| Key | Values | Description |
|-----|--------|-------------|
| `/TextRenderingMode` | `/GlyphPaths` or `/TextObjs` | Controls how text appears in the display list. `/GlyphPaths` converts all text to path operations (fills/strokes) — use for bitmap devices. `/TextObjs` emits `TextObj` elements with font and string data — use when the device needs structured text (e.g., PDF for searchable/selectable text) |
| `/StrokeMethod` | `/StrokePathFill` or `/Stroke` | `/StrokePathFill` converts strokes to filled outlines via `strokepath` — works around Cairo bitmap rendering artifacts. `/Stroke` uses native stroke rendering |


## Creating the Python Module

### Package Structure

Create a Python package under `postforge/devices/`. The convention is to name
the inner module the same as the package (e.g., `png/png.py`, `pdf/pdf.py`):

```
postforge/devices/svg/
    __init__.py
    svg.py
```

The `__init__.py` re-exports `showpage` from the inner module:

```python
# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from .svg import showpage
```

This is needed because `device_output.py` imports the package
(`postforge.devices.svg`) and calls `showpage` on it directly.

### The showpage Function

The only required entry point is `showpage(ctxt, pd)`:

```python
# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import os

from ...core import types as ps
from ..common.cairo_renderer import render_display_list


def showpage(ctxt: ps.Context, pd: dict) -> None:
    """
    Render the current page to output.

    Args:
        ctxt: PostScript context — ctxt.display_list contains all
              graphics operations for the current page
        pd: Page device dictionary (Python dict with bytes keys
            and PSObject values)
    """
    ...
```

**Parameters:**

- `ctxt` — The PostScript execution context. The key attribute is
  `ctxt.display_list`, a Python list of display list elements (paths, fills,
  strokes, images, text, etc.) representing everything painted on the current
  page.
- `pd` — The page device dictionary. This is a Python `dict` with `bytes` keys
  (e.g., `b"MediaSize"`) and PostScript type values. Use it to read device
  configuration.

### Reading Page Device Parameters

Page device values are PostScript objects. Extract Python values with `.val` or
type-specific accessors:

```python
# Page dimensions in device pixels (already scaled by HWResolution)
width = pd[b"MediaSize"].get(ps.Int(0))[1].val
height = pd[b"MediaSize"].get(ps.Int(1))[1].val

# Device resolution
dpi_x = pd[b"HWResolution"].get(ps.Int(0))[1].val
dpi_y = pd[b"HWResolution"].get(ps.Int(1))[1].val

# Minimum line width
min_line_width = pd[b"LineWidthMin"].val

# Page number (already incremented by device_output.py before calling device)
page_num = pd[b"PageCount"].val

# String parameters
if b"OutputBaseName" in pd:
    base_name = pd[b"OutputBaseName"].python_string()
else:
    base_name = "page"
```

### Output Path Construction

Follow the established pattern for file output:

```python
# Get base name (set by CLI -o flag)
if b"OutputBaseName" in pd:
    base_name = pd[b"OutputBaseName"].python_string()
else:
    base_name = "page"

# Get output directory (set by CLI --output-dir flag)
if b"OutputDirectory" in pd:
    output_dir = pd[b"OutputDirectory"].python_string()
else:
    output_dir = ps.OUTPUT_DIRECTORY

page_num = pd[b"PageCount"].val
output_file = os.path.join(os.getcwd(), output_dir, f"{base_name}-{page_num:04d}.ext")
```

The output directory is created by `device_output.py` before calling your
device, so you don't need to handle `os.makedirs`.


## Rendering the Display List

You have two approaches for rendering: use the shared Cairo renderer, or
process the display list directly.

### Option A: Use the Shared Cairo Renderer

The simplest approach. Create a Cairo surface and context, then call
`render_display_list`. This is what the PNG and Qt devices do:

```python
import cairo

from ..common.cairo_renderer import render_display_list

def showpage(ctxt, pd):
    width = pd[b"MediaSize"].get(ps.Int(0))[1].val
    height = pd[b"MediaSize"].get(ps.Int(1))[1].val
    min_line_width = pd[b"LineWidthMin"].val

    # 1. Create Cairo surface and context
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    cc = cairo.Context(surface)

    # 2. Set Cairo properties
    cc.set_tolerance(ctxt.gstate.flatness / 10.0)

    # 3. Fill white background
    cc.set_source_rgb(1.0, 1.0, 1.0)
    cc.rectangle(0, 0, width, height)
    cc.fill()

    # 4. Render the display list
    render_display_list(ctxt, cc, height, min_line_width)

    # 5. Write output
    surface.write_to_png(output_file)
```

The `render_display_list` function signature:

```python
def render_display_list(
    ctxt: ps.Context,
    cairo_ctx,
    page_height: int,
    min_line_width: float = 1,
    deferred_text_objs: list = None
) -> None
```

- `page_height` is needed for the PostScript-to-Cairo coordinate system flip
  (PostScript origin is bottom-left, Cairo is top-left)
- `deferred_text_objs` is used by the PDF device to collect text objects for
  later font embedding. Pass `None` for bitmap devices.

### Option B: Process the Display List Directly

For devices that don't use Cairo, iterate `ctxt.display_list` and handle each
element type directly. This approach gives full control over rendering but
requires handling every element type yourself.

```python
def showpage(ctxt, pd):
    for item in ctxt.display_list:
        if isinstance(item, ps.Path):
            # Path elements contain subpaths with MoveTo, LineTo, CurveTo, ClosePath
            for subpath in item:
                for element in subpath:
                    if isinstance(element, ps.MoveTo):
                        # element.p.x, element.p.y
                        ...
                    elif isinstance(element, ps.LineTo):
                        # element.p.x, element.p.y
                        ...
                    elif isinstance(element, ps.CurveTo):
                        # element.p1, element.p2, element.p3 (control points)
                        ...
                    elif isinstance(element, ps.ClosePath):
                        ...
        elif isinstance(item, ps.Fill):
            # item.color — device RGB values
            # item.winding_rule — WINDING_NON_ZERO or WINDING_EVEN_ODD
            ...
        elif isinstance(item, ps.Stroke):
            # item.color, item.line_width, item.line_cap, item.line_join
            # item.miter_limit, item.dash_pattern, item.ctm
            ...
        elif isinstance(item, ps.ClipElement):
            # item.path, item.winding_rule, item.is_initclip
            ...
```


## Display List Elements

All display list element types are defined in
`postforge/core/types/graphics.py`. The display list is a flat Python list
where path elements precede their fill/stroke operations.

### Path Construction

| Element | Attributes | Description |
|---------|-----------|-------------|
| `Path` | (is a `list` of `SubPath`) | Container for subpaths |
| `SubPath` | (is a `list` of path elements) | A connected series of path commands |
| `MoveTo` | `.p` (Point) | Start new subpath |
| `LineTo` | `.p` (Point) | Line to point |
| `CurveTo` | `.p1`, `.p2`, `.p3` (Points) | Cubic Bezier curve |
| `ClosePath` | — | Close current subpath |

All coordinates in path elements are in **device space** (already transformed
by the CTM).

### Paint Operations

| Element | Key Attributes | Description |
|---------|---------------|-------------|
| `Fill` | `.color`, `.winding_rule` | Fill the preceding path |
| `PatternFill` | `.pattern_dict`, `.winding_rule`, `.ctm` | Fill with a tiling pattern |
| `Stroke` | `.color`, `.line_width`, `.line_cap`, `.line_join`, `.miter_limit`, `.dash_pattern`, `.ctm` | Stroke the preceding path |

### Clipping

| Element | Key Attributes | Description |
|---------|---------------|-------------|
| `ClipElement` | `.path`, `.winding_rule`, `.is_initclip` | Set clipping region. If `.is_initclip` is true, reset to default clip |

### Images

| Element | Key Attributes | Description |
|---------|---------------|-------------|
| `ImageElement` | `.sample_data`, `.width`, `.height`, `.bits_per_component`, `.image_matrix`, `.ctm`, `.decode_array`, `.interpolate` | Raster image |
| `ImageMaskElement` | (inherits ImageElement) `.polarity`, `.color` | 1-bit stencil mask painted in current color |
| `ColorImageElement` | (inherits ImageElement) `.components`, `.color_space_name` | Multi-component color image |

### Text

| Element | Key Attributes | Description |
|---------|---------------|-------------|
| `TextObj` | `.text`, `.start_x`, `.start_y`, `.font_dict`, `.font_name`, `.font_size`, `.color`, `.ctm` | Structured text (only emitted when `TextRenderingMode` is `/TextObjs`) |
| `ActualTextStart` | `.unicode_text`, `.start_x`, `.start_y`, `.font_size`, `.ctm` | Start of searchable text span (for fonts rendered as paths in TextObjs mode) |
| `ActualTextEnd` | — | End of searchable text span |

### Glyph Cache

| Element | Key Attributes | Description |
|---------|---------------|-------------|
| `GlyphRef` | `.cache_key`, `.position_x`, `.position_y` | Reference to cached glyph bitmap |
| `GlyphStart` | `.cache_key`, `.position_x`, `.position_y` | Begin glyph capture (cache miss) |
| `GlyphEnd` | — | End glyph capture |

### Shading

| Element | Key Attributes | Description |
|---------|---------------|-------------|
| `AxialShadingFill` | `.x0`, `.y0`, `.x1`, `.y1`, `.color_stops`, `.ctm` | Linear gradient (Type 2) |
| `RadialShadingFill` | `.x0`, `.y0`, `.r0`, `.x1`, `.y1`, `.r1`, `.color_stops`, `.ctm` | Radial gradient (Type 3) |
| `MeshShadingFill` | `.triangles`, `.ctm` | Triangle mesh (Types 4/5) |
| `PatchShadingFill` | `.patches`, `.ctm` | Coons/tensor-product patches (Types 6/7) |
| `FunctionShadingFill` | `.pixel_data`, `.width`, `.height`, `.matrix`, `.ctm` | Function-based shading (Type 1) |


## Advanced Patterns

### Multi-Page State (PDF)

For devices that need to maintain state across pages (e.g., producing a single
multi-page output file), the page device dictionary is the right place to store
it. The `pd` dict that your `showpage` receives is the **same dictionary
instance** on every call — it persists for the lifetime of the job. This means
you can store arbitrary Python objects in it (class instances, lists,
open file handles, etc.) using a bytes key, and retrieve them on subsequent
`showpage` calls.

The PDF device uses this to maintain a `PDFDocumentState` that holds the Cairo
PDF surface, font tracker, deferred text objects, and page counter across all
pages:

```python
PDF_STATE_KEY = b'_PDFDocumentState'

def showpage(ctxt, pd):
    pdf_state = pd.get(PDF_STATE_KEY)
    if pdf_state is None:
        # First page — initialize and store in page device dict
        pdf_state = PDFDocumentState(file_path, width, height)
        pd[PDF_STATE_KEY] = pdf_state

    # Render current page using persistent state
    pdf_state.start_new_page(...)
    render_display_list(ctxt, pdf_state.context, ...)
    pdf_state.finish_page()
```

This works because the page device dictionary is just a Python `dict` — you can
add any key/value pair to it. Use a leading underscore or dot in your key name
(e.g., `b'_MyDeviceState'`) to avoid colliding with standard PostScript page
device parameters.

**save/restore safety:** The page device dictionary is shallow-copied during
both `gsave` and `save` — meaning the saved and current graphics states share
the **same dict instance**. Any state you store in `pd` is visible across
gsave/grestore and save/restore boundaries. Additionally, the page device dict
is a plain Python `dict` (not a PostScript VM object), so the VM
snapshot/restore mechanism doesn't touch it either. This means your device
state survives all forms of state saving. The only operations that replace the
dict entirely are `setpagedevice` (which rebuilds it from scratch) and
`nulldevice`.

### Job Finalization (PDF)

Multi-page devices may need a finalization step after the last page. The PDF
device uses a `finalize_document` function that closes the Cairo surface and
injects embedded fonts. This is called from the job control code in
`postforge/operators/control.py`:

```python
# In control.py job cleanup:
from ..devices.pdf.pdf import PDF_STATE_KEY, finalize_document
if PDF_STATE_KEY in pd:
    finalize_document(pd)
```

If your device needs finalization, follow the same pattern: export a
finalization function and add a call in the job cleanup path. The finalization
hook checks for a device-specific key in the page device dictionary to
determine if there is state to finalize.

### Custom Text Handling (PDF)

The PDF device uses `/TextRenderingMode /TextObjs` to receive structured text
data instead of rendered glyph paths. It then:

1. Tracks font usage across all pages via `FontTracker`
2. Collects deferred `TextObj` elements that need font embedding
3. At finalization, reconstructs Type 1 fonts and injects them into the PDF

This pattern is only needed for devices that require structured text
information (e.g., for searchability or font embedding).

### Interactive Rendering (Qt)

The Qt device has additional hooks beyond `showpage`:

- **`refresh_display(ctxt)`** — Registered as the interpreter's
  `on_paint_callback` when running in interactive mode. Called after each paint
  operation to provide live rendering feedback
- **`enter_event_loop()`** — Called after job completion to keep the window open
- **`_process_qt_events()`** — Registered as `ctxt.event_loop_callback` to keep
  the GUI responsive during PostScript execution

These hooks are registered in `cli_runner.py` when the Qt device is active.


## Existing Devices as Reference

### PNG (`postforge/devices/png/`)

The simplest device. Creates a Cairo `ImageSurface`, calls
`render_display_list`, writes to PNG. Stateless — each page is an independent
file. ~60 lines of code.

Key features: anti-alias mode support, configurable output path.

### PDF (`postforge/devices/pdf/`)

Complex multi-page device. Maintains a `PDFDocumentState` across pages.
Uses Cairo `PDFSurface` for graphics, then post-processes with pypdf for
font embedding. Applies a scaling transform to convert from device coordinates
(at `HWResolution`) to PDF points (72 DPI).

Key features: persistent state, font tracking and embedding (Type 1 and
CID/TrueType), deferred text rendering, document finalization, stream
compression.

### SVG (`postforge/devices/svg/`)

Renders to a Cairo SVGSurface, then post-processes the SVG to replace text
outlines with `<text>` elements. Text uses CSS font-family fallback chains
for font matching. Each page is a separate `.svg` file.

Key features: text as selectable/searchable elements, CSS font-family
fallbacks, Cairo-based vector rendering.

### Qt (`postforge/devices/qt/`)

The default preview device — when you run `./postforge.sh samples/tiger.ps`
without specifying `-d`, this is the device that opens a window to display the
result. Uses Cairo for rendering (same as PNG), then converts the Cairo surface
to a Qt `QImage` for display. Manages global module-level state for the Qt
application, window, and canvas.

Key features: live rendering updates, zoom/pan, keyboard navigation,
busy/waiting cursor states, event loop integration.


## Checklist

Follow these steps to add a new output device end-to-end:

1. **Create the PostScript resource file**
   `postforge/resources/OutputDevice/<name>.ps` — define the page device dictionary with
   at minimum: `/OutputDeviceName`, `/OutputDevice`, `/PageSize`,
   `/HWResolution`, `/Install`, `/BeginPage`, `/EndPage`, `/PageCount`,
   `/.IsPageDevice`, `/LineWidthMin`, `/TextRenderingMode`, `/StrokeMethod`

2. **Create the Python package**
   `postforge/devices/<name>/__init__.py` — export `showpage`
   `postforge/devices/<name>/<name>.py` — implement `showpage(ctxt, pd)`

3. **Implement showpage**
   Read page device parameters, render the display list (via Cairo renderer or
   custom processing), write output

4. **Test the device**
   ```bash
   ./postforge.sh -d <name> samples/tiger.ps
   ```

5. **(Optional) Add job finalization**
   If your device needs cleanup after the last page, export a finalization
   function and hook it into `postforge/operators/control.py`

6. **(Optional) Add CLI support**
   If the device needs special CLI flags or setup, add handling in
   `postforge/cli.py`
