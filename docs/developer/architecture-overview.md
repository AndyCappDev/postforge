# PostForge Architecture Overview

This document describes how PostForge works internally. It is aimed at developers
who want to understand the system well enough to contribute code, fix bugs, or
add new features. It assumes familiarity with PostScript at a user level (you know
what `moveto`, `lineto`, `fill` do) but not with interpreter internals.

## High-Level Pipeline

Every PostScript program flows through the same pipeline:

```
                                                  ┌──────────────┐
PostScript Source ──► Tokenizer ──► Execution ──► │ Display List │──► Output Device
  (.ps file,          extracts       Engine       │  (per page)  │    (PNG, PDF,
   string, or         tokens from    (exec_exec)  └──────────────┘     SVG, Qt)
   interactive)       byte streams
```

1. **Source** — PostScript code arrives as a file, string, or interactive input.
   The CLI (`postforge/cli.py`) parses arguments, creates a `Context`, and pushes
   the input onto the execution stack.

2. **Tokenizer** (`postforge/core/tokenizer.py`) — Reads bytes from a stream one
   at a time, recognizing numbers, names, strings, procedures (delimited by `{}`),
   and special syntax like hex strings and ASCII85. The tokenizer is invoked on
   demand by the execution engine whenever a tokenizable object (File, Run, String)
   sits on top of the execution stack.

3. **Execution Engine** (`postforge/operators/control.py`, function `exec_exec`) —
   The heart of the interpreter. Continuously processes objects from the execution
   stack until it is empty. Described in detail in the next section.

4. **Display List** — A flat list of rendering commands (Fill, Stroke,
   ImageElement, TextObj, ClipElement, etc.) accumulated during a page. All
   coordinates are in device space — path operators transform user space
   coordinates through the CTM at the time of the call. Painting operators
   like `fill`, `stroke`, and `show` append elements here.

5. **Output Device** — When `showpage` fires, the accumulated display list is
   handed to a device for rendering. Devices live in `postforge/devices/` and
   the current implementation typically delegates to a shared Cairo rendering
   backend, but this is not a requirement — an output device can use whatever
   rendering method it wants to process the display list. After rendering,
   `showpage` erases the display list and reinitializes the graphics state for
   the next page. `copypage` follows the same rendering path but preserves
   both the display list and graphics state, allowing further drawing on top
   of the existing page contents.


## The Execution Engine

The execution engine is implemented in `exec_exec()` in
`postforge/operators/control.py`. It is a single `while` loop that drains
the execution stack (`e_stack`). On each iteration it inspects the top-of-stack
object and takes one of five paths:

### Path 1 — Literal Objects

Objects that are literal (their `attrib` is `ATTRIB_LIT`, or their type is in
`LITERAL_TYPES` — Int, Real, Bool, Null, Mark) are popped from the execution
stack and pushed onto the operand stack with no further processing.

### Path 2 — Operator Objects

An `Operator` wraps a Python function. The engine pops it from the execution
stack and calls `top.val(ctxt, o_stack)` directly. The operator function
receives the full context and the operand stack, pops its own arguments,
performs work, and pushes the results. All PostScript built-in operators are
registered this way during `create_system_dict()` in `postforge/operators/dict.py`.

### Path 3 — Name Objects

Executable names trigger a dictionary stack lookup via `ps_dict.lookup()`. The
lookup walks the dictionary stack from top to bottom, returning the first match.
The name on the execution stack is then *replaced* with the looked-up object
(copied via `__copy__()` for non-operators) and execution continues — the
replacement object will be processed on the next iteration. If the name is not
found, a PostScript `undefined` error is raised. Note that while a successful
name lookup never touches the operand stack, the error path does — per the
PLRM's error initiation mechanism (Section 3.11.1), the offending name is
pushed onto the operand stack before the error handler is invoked.

The copy-on-lookup behavior is important: it prevents one execution from
mutating a shared dictionary entry (e.g., changing a procedure's `attrib` from
executable to literal via `cvlit`). Operators are immutable and skip the copy.

### Path 4 — Tokenizable Objects (File, Run, String)

When a File, Run, or executable String sits on top, the engine calls the
tokenizer to extract the next PostScript token from the byte stream. If a token
is produced, it is pushed for execution. If the stream is exhausted, the
tokenizable object is popped and execution continues with whatever is below it.

### Path 5 — Executable Arrays (Procedures)

An executable array `{ ... }` is a procedure. The engine peels off one element
at a time from the front (tracked by a `start` index and `length` counter),
pushing each element for execution. When the array is exhausted it is discarded.
As an optimization, the last element replaces the procedure on the execution
stack directly instead of requiring a separate push/pop. This also acts as
tail-call optimization: when a procedure's last element is itself a procedure
call (e.g., a recursive invocation), the replacement prevents the execution
stack from growing on each call.

### Control Flow Objects

Beyond the five main paths, the execution engine also handles control flow
objects that appear on the execution stack:

- **Stopped** (`T_STOPPED`) — Pushed by the `stopped` operator to mark an
  error boundary. If execution reaches this marker without being interrupted,
  `false` is pushed to the operand stack (no error occurred). The `stop`
  operator unwinds the execution stack to the nearest Stopped marker.

- **Loop** (`T_LOOP`) — Loop contexts for `loop`, `repeat`, `for`, `forall`,
  and `pathforall`. Each iteration pushes a copy of the loop body onto the execution
  stack. The `exit` operator unwinds to the nearest Loop marker.

- **HardReturn** — Used by constructs like `execjob()` to mark an unwindable
  boundary on the execution stack.

### The Four Stacks

PostScript defines four stacks, all maintained in the `Context` object:

| Stack | Field | Purpose |
|-------|-------|---------|
| Operand | `o_stack` | Data values and arguments for operators |
| Execution | `e_stack` | Objects waiting to be executed |
| Dictionary | `d_stack` | Variable scoping — name lookup walks this top-to-bottom |
| Graphics State | `g_stack` | Saved graphics states from `gsave` / `save` |

Stacks are `Stack` objects (a list subclass with an optional capacity limit).
Default capacities are defined in `postforge/core/types/constants.py`
(O_STACK_MAX=500, E_STACK_MAX=250, D_STACK_MAX=250, G_STACK_MAX=10), though
these can be overridden via `setuserparams`.

### Cython Accelerator

A Cython-compiled copy of `exec_exec` exists at
`postforge/operators/_control_cy.pyx`. It provides 15–40% speedup by using
C-typed local variables and inlining the dictionary lookup for the Name path.
If the compiled `.so` is present it is loaded automatically; otherwise the pure
Python version runs. The two implementations must be kept functionally
equivalent — see `build_cython.sh` for compilation.


## Type System

All PostScript values are represented as Python objects inheriting from
`PSObject` (defined in `postforge/core/types/base.py`). Every PSObject carries:

- **`val`** — The Python value (int, float, bytes, list, dict, callable, etc.)
- **`attrib`** — Literal (`ATTRIB_LIT`) or executable (`ATTRIB_EXEC`)
- **`_access`** — Access control level (unlimited, read-only, execute-only, none)
- **`TYPE`** — Integer type constant for fast dispatch (e.g., `T_INT`, `T_NAME`)
- **`is_composite`** / **`is_global`** — VM allocation metadata

### Type Hierarchy

**Primitive types** (`postforge/core/types/primitive.py`):
`Int`, `Real`, `Bool`, `Name`, `String`, `Operator`, `Mark`, `Null`

**Composite types** (`postforge/core/types/composite/`):
`Array`, `Dict`, `PackedArray` — these hold references and participate in VM
save/restore semantics.

**Graphics types** (`postforge/core/types/graphics.py`):
`GraphicsState`, `GState` (gstate object), `Path`, `DisplayList`, plus all
display list element classes (Fill, Stroke, ImageElement, TextObj, etc.)

**File types** (`postforge/core/types/file_types.py`):
`File`, `Run`, `StandardFile`, `StandardFileProxy`, `FilterFileWrapper`

**Control flow types** (`postforge/core/types/control.py`):
`Stopped`, `Loop` (with variants for `loop`/`repeat`/`for`/`forall`/`pathforall`),
`HardReturn`

### Literal vs Executable

The `attrib` field controls how the execution engine treats an object. A literal
Name pushed on the execution stack is transferred to the operand stack; an
executable Name triggers a dictionary lookup. Operators `cvx` and `cvlit` flip
this attribute. The copy-on-lookup mechanism (Path 3 above) prevents these
mutations from corrupting shared dictionary entries.

### Type Constants and Groupings

Type constants (`T_INT`, `T_NAME`, `T_ARRAY`, etc.) and pre-computed type
groupings (`LITERAL_TYPES`, `TOKENIZABLE_TYPES`, `NUMERIC_TYPES`, `ARRAY_TYPES`)
live in `postforge/core/types/constants.py`. The execution engine uses these
groupings for fast `in` checks rather than isinstance calls.


## Memory Model

PostScript defines a dual virtual memory (VM) system that PostForge implements
faithfully.

### Local and Global VM

- **Local VM** (`ctxt.lvm`) — Per-context storage. Most objects live here.
  Subject to save/restore — a `restore` rolls back all changes made since the
  corresponding `save`.

- **Global VM** (`GlobalResources.gvm`) — Shared across contexts. Objects
  allocated here (via `true setglobal`) survive `restore`. Font dictionaries
  are typically stored in global VM.

The `vm_alloc_mode` flag in the context controls which VM new composite objects
are allocated in. The `setglobal` / `currentglobal` operators manipulate this
flag.

### Save and Restore

The `save` operator (`postforge/operators/vm.py`) snapshots the current local
VM state and pushes a save object. The snapshot uses a Copy-on-Write (COW)
strategy: composite objects are marked as protected, and mutations trigger a
copy of the original backing store before modification. On `restore`, the
protected originals are reinstated.

For objects that don't participate in COW (or as a fallback), pickle-based
snapshots store VM state in `_vm_snapshots` keyed by `(context_id, save_id)`.

`save` also pushes the current graphics state onto `g_stack` (like `gsave`),
and `restore` pops it back.

### Job Encapsulation and the Job Server

PostScript defines a *job server* model (PLRM Section 3.7.7) that controls
how successive PostScript programs share an interpreter. PostForge implements
this in two layers:

**`execjob()`** wraps every top-level PostScript job in a save/restore
boundary. This ensures that one job cannot affect the next — all local VM
changes are rolled back, the dictionary and graphics stacks are reset, and the
interpreter returns to a clean state. Jobs processed this way are called
*encapsulated* because their side effects are contained.

**`startjob`** (`postforge/operators/job_control.py`) allows a PostScript
program to break out of this encapsulation from within. It takes a boolean
and a password:

```postscript
true password startjob   % → true (success) or false (failure)
```

When `startjob` succeeds it performs a *job server sequence*:

1. **End the current job** — clear the operand stack, reset the dictionary
   stack to its initial three dictionaries (systemdict, globaldict, userdict),
   and if the current job was encapsulated, restore VM to the job-level save
   point.
2. **Begin a new job** — if the boolean was `true` (unencapsulated), skip the
   save so that VM changes persist for subsequent jobs. If `false`
   (encapsulated), perform a new save to create a fresh job boundary.

Three conditions must all be met for `startjob` to succeed (otherwise it
pushes `false` and does nothing):

- The interpreter supports job encapsulation (`ctxt.supports_job_encapsulation`)
- The password matches `StartJobPassword` in the system parameters
- The save nesting level equals the level when the current job started (no
  outstanding `save` operations deeper than the job boundary)

The save-level check is why the standalone tests call `false 0 startjob`
between test groups — it starts a fresh encapsulated job, resetting VM state
so the next group begins cleanly.

**`exitserver`** is the Level 1 equivalent. It is defined in `serverdict` and
behaves as `true password startjob` — always requesting an unencapsulated job.
On success it prints a standard message to stdout:

```
%%[exitserver: permanent state may be changed]%%
```

and removes `serverdict` from the dictionary stack. On failure it raises
`invalidaccess`.

**Job save tracking**: The context maintains a `job_save_level_stack` that
records the save object for each nested job. When `startjob` ends the current
job, it uses this stack to find the correct save point to restore.

**Testing note**: Successful `startjob` / `exitserver` calls destroy VM state
(clearing stacks and restoring saves), which wipes out the unit test
framework. For this reason, tests that exercise successful calls live in
`unit_tests/job_control_tests_standalone.ps` and write pass/fail results
directly to a stats file rather than using the `assert` framework.


## Graphics Pipeline

PostScript programs build up a page through a sequence of graphics operations.
The pipeline looks like:

```
Path Construction ──► Painting Operators ──► Display List ──► Device Rendering
(moveto, lineto,      (fill, stroke,         (flat list of     (Cairo/custom
 curveto, arc,         show, image)           Fill, Stroke,     backend)
 closepath)                                   TextObj, etc.)
```

### Graphics State

The `GraphicsState` object (`postforge/core/types/graphics.py`) holds all
per-page rendering state:

| Category | Fields |
|----------|--------|
| Transform | `CTM` (current transformation matrix), `iCTM` (inverse) |
| Path | `currentpoint`, `path` (list of SubPaths) |
| Clipping | `clip_path`, `clip_path_stack`, `clip_path_version` |
| Color | `color_space`, `color`, `transfer_function`, `overprint` |
| Line | `line_width`, `line_cap`, `line_join`, `miter_limit`, `dash_pattern` |
| Font | `font` (current font dictionary) |
| Other | `flatness`, `stroke_adjust`, `halftone`, `page_device` |

`gsave` copies the graphics state (via an optimized shallow-copy with selective
deep-copy for mutable containers) and pushes it onto `g_stack`. `grestore` pops
it back.

### Path Construction

Path operators (`postforge/operators/path.py`) build a `Path` — a list of
`SubPath` objects, where each SubPath is a list of elements: `MoveTo`, `LineTo`,
`CurveTo`, `ClosePath`. Coordinates are transformed from user space to device
space via the CTM at the time of the call. The current path lives in the
graphics state until a painting operator consumes it.

### Painting and the Display List

When a painting operator executes:

1. It captures the current path and relevant graphics state (color, line
   properties, CTM).
2. It creates a display list element — `Fill`, `Stroke`, `PatternFill`,
   `ImageElement`, `TextObj`, etc.
3. It appends the element via `DisplayListBuilder.add_graphics_operation()`.
4. It clears the current path (for `fill`/`stroke`) or advances the current
   point (for `show`).

The `DisplayListBuilder` (`postforge/core/display_list_builder.py`) manages
clipping path synchronization. `ClipElement` objects in the display list tell
the rendering device when and how to update its clip region.

### Display List Elements

The display list is a flat Python list containing instances of these classes
(all defined in `postforge/core/types/graphics.py`):

| Element | Created by | Purpose |
|---------|-----------|---------|
| `Fill` | `fill`, `eofill` | Filled path with color and winding rule |
| `Stroke` | `stroke` | Stroked path with line properties and CTM |
| `PatternFill` | `fill` with pattern color space | Pattern-tiled fill |
| `ImageElement` | `image`, `imagemask`, `colorimage` | Raster image data |
| `TextObj` | `show` (in TextObjs mode) | Text for native PDF output |
| `ClipElement` | `clip`, `eoclip`, `initclip` | Clipping path update |
| `GlyphRef` | show (cache hit) | Reference to cached glyph bitmap |
| `GlyphStart`/`GlyphEnd` | show (cache miss) | Glyph bitmap capture markers |
| `ErasePage` | `erasepage` | Page erase marker |


## Color Space System

PostScript has a rich color model with multiple color space families.
PostForge implements this in `postforge/core/color_space.py` (the
`ColorSpaceEngine` class) with operators in `postforge/operators/color_ops.py`
and `postforge/operators/device_color_state.py`.

### Supported Color Spaces

| Family | Spaces | Components |
|--------|--------|------------|
| Device | DeviceGray, DeviceRGB, DeviceCMYK | 1, 3, 4 |
| CIE-Based | CIEBasedA, CIEBasedABC, CIEBasedDEF, CIEBasedDEFG | 1, 3, 3, 4 |
| ICC | ICCBased | N (profile-dependent) |
| Special | Indexed, Separation, DeviceN, Pattern | varies |

Device spaces map directly to output components. CIE-based spaces go through
a calibrated pipeline (decode → matrix → XYZ → sRGB). ICCBased spaces use
lcms2 via Pillow's ImageCms module when available, falling back to the
alternate device space otherwise. Indexed, Separation, and DeviceN are all
defined in terms of an underlying base space — their color values are resolved
through a palette lookup or tint transform before reaching the display list.

### Color in the Graphics State

The graphics state stores two fields for color:

- **`color_space`** — Always a Python list, even for simple spaces
  (e.g., `["DeviceGray"]`, `["Separation", name, alt_space, tint_transform]`).
- **`color`** — A list of Python floats representing the current color. For
  most spaces this is the resolved device-space value. For ICCBased spaces
  it holds the raw profile components until rendering time.

Pattern color spaces store the pattern dictionary separately in
`gstate._current_pattern`.

### Operator Layers

PostScript provides two ways to set color:

**Level 2 general model** — `setcolorspace` sets the active space and
initializes the color to its default. `setcolor` then sets color components
within that space. For Separation and DeviceN, `setcolor` executes the tint
transform procedure to resolve the color into the alternative space. For
Indexed, it performs a palette lookup.

**Level 1 convenience operators** — `setgray`, `setrgbcolor`, `setcmykcolor`,
and `sethsbcolor` each set both the color space and the color in a single
call. These are equivalent to a `setcolorspace` / `setcolor` pair.

Query operators (`currentgray`, `currentrgbcolor`, `currentcmykcolor`) convert
the current color to the requested device space on the fly using standard PLRM
conversion formulas (NTSC weighting for gray, etc.).

### Color Conversion at Rendering Time

Color conversion is *lazy* — `setcolor` stores the color in the graphics
state, but the conversion to device color (RGB for the Cairo renderer) happens
only when a painting operator builds a display list element:

1. A painting operator (`fill`, `stroke`, etc.) calls
   `ColorSpaceEngine.convert_to_device_color()`.
2. The engine dispatches based on color space family — device spaces pass
   through (with cross-conversion if needed), CIE-based spaces run through
   their decode/matrix/XYZ pipeline, and ICCBased spaces apply an lcms2
   transform.
3. The resulting RGB values are stored in the display list element (`Fill`,
   `Stroke`, etc.).
4. The rendering device receives pre-converted RGB and passes it straight
   to Cairo.

### ICC Color Management Tiers

PostForge uses a tiered approach to ICC color management:

- **Tier 1** — PLRM formulas only. Device space cross-conversions (e.g.,
  CMYK → RGB) use the standard PostScript formulas. Always available.
- **Tier 2** — ICCBased profiles. When a PostScript program specifies an
  ICCBased color space with an embedded profile, PostForge extracts the
  profile and builds an lcms2 transform. Falls back to Tier 1 if the profile
  cannot be loaded.
- **Tier 3** — Default CMYK profile. PostForge searches the system for a CMYK
  ICC profile (GhostScript, colord, macOS, Windows locations) and uses it for
  DeviceCMYK → RGB conversion, producing more accurate on-screen rendering.
  Disabled via `--no-icc` or when no profile is found.

### Pattern Color Spaces

Patterns are a special color space where the "color" is a tiled or shaded
graphic rather than a flat value. `makepattern` instantiates a pattern by
executing its PaintProc and capturing the result as a display list.
`setpattern` then installs the pattern as the current color.

There are two paint types for tiling patterns. Colored patterns (PaintType 1)
carry their own colors — the color space is simply `["Pattern"]`. Uncolored
patterns (PaintType 2) act as a stencil — the color space is
`["Pattern", underlying_space]` and `setcolor` provides the color components
that fill the stencil. At rendering time, the pattern cell's display list
is tiled across the fill area based on the pattern's step size and bounding
box.


## Output Devices

Output devices render the display list into a final format. Each device
consists of two parts that work together: a PostScript configuration file
in `resources/OutputDevice/` (e.g., `png.ps`) that defines the page device
dictionary, and a Python module in `postforge/devices/` that implements a
`showpage(ctxt, pd)` function to perform the actual rendering. The built-in
devices use a shared Cairo rendering backend, but this is a convenience, not
a requirement — a custom device can use any rendering approach it wants
without involving Cairo at all.

### Device Architecture

```
                         ┌────────────────────────────────┐
                         │  Shared Cairo Rendering        │
                         │  (devices/common/)             │
                         │                                │
                         │  cairo_renderer.py  - dispatch │
                         │  cairo_images.py    - images   │
                         │  cairo_patterns.py  - patterns │
                         │  cairo_shading.py   - shading  │
                         └──────┬───────┬───────┬─────────┘
                                │       │       │
                    ┌───────────┘       │       └───────────┐
                    ▼                   ▼                   ▼
          ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
          │  PNG     │   │  PDF     │   │  SVG     │   │  Qt      │
          │  device  │   │  device  │   │  device  │   │  device  │
          └──────────┘   └──────────┘   └──────────┘   └──────────┘
                                        │
                                ┌───────┴─────────┐
                                │ Font embedding  │
                                │ (font_embedder, │
                                │  cid_font_      │
                                │  embedder,      │
                                │  pdf_injector)  │
                                └─────────────────┘
```

**PNG** (`postforge/devices/png/png.py`) — Creates a Cairo ImageSurface, calls
`render_display_list()`, writes a `.png` file. The simplest device and a good
starting point for understanding the rendering pipeline.

**PDF** (`postforge/devices/pdf/`) — Renders to a Cairo PDFSurface, then
post-processes the PDF with pypdf to inject embedded fonts. Text in PDF mode
uses `TextObj` elements that are written as native PDF text operators, producing
searchable/selectable text. Font embedding handles Type 1 reconstruction,
CID/TrueType extraction, and subsetting.

**SVG** (`postforge/devices/svg/svg.py`) — Renders to a Cairo SVGSurface,
then post-processes the SVG to convert text from outlines to selectable `<text>`
elements with CSS font-family fallback chains. Each page produces a separate
`.svg` file.

**Qt** (`postforge/devices/qt/qt.py`) — Interactive display window. Renders to
a Cairo ImageSurface and displays it in a Qt widget. In interactive mode, the
display updates live as display list elements are added; in batch mode, it
updates on `showpage`.

### PostScript-Side Device Setup

The PostScript configuration files (e.g., `png.ps`, `pdf.ps`, `svg.ps`) define the page
device dictionary — page size, resolution, margins, color space, and
Install/BeginPage/EndPage procedures. When a device is selected, this
dictionary is loaded and merged into the graphics state's `page_device`.

### Shared Cairo Renderer

`render_display_list()` in `postforge/devices/common/cairo_renderer.py` is the
main dispatch loop. It iterates over display list elements and delegates to
type-specific rendering functions:

- Path construction → Cairo `move_to`, `line_to`, `curve_to`, `close_path`
- Fill/Stroke → Cairo `fill` / `stroke` with color and line properties
- Images → Pixel format conversion and Cairo surface blitting (`cairo_images.py`)
- Patterns → Pattern surface tiling (`cairo_patterns.py`)
- Shading → Gradient and mesh rendering (`cairo_shading.py`)
- Clipping → Cairo `clip` / `reset_clip`
- Glyph cache → Bitmap surface blitting for cached Type 3 glyphs

**Stroke method**: For bitmap devices (PNG, Qt), strokes are converted to filled
paths by the interpreter before they reach the display list. This works around
bugs in Cairo's stroke rasterization, particularly with dashed lines. The PDF
device uses Cairo's native stroke rendering instead. This behavior is controlled
per-device by the `/StrokeMethod` entry in the page device dictionary (set in
each device's `.ps` configuration file).


## Resource System

PostScript's resource system provides a structured way to look up fonts,
encodings, color spaces, and other named objects. PostForge implements this
in `postforge/operators/resource.py`.

### Resource Categories

Resources are organized by category, stored in nested dictionaries within both
global and local VM:

```
gvm["resource"]["Category"]     →  Dict of category definitions
gvm["resource"]["Font"]         →  Dict of global font resources
gvm["resource"]["Encoding"]     →  Dict of encoding resources
lvm["resource"]["Font"]         →  Dict of local font resources
...
```

The `resources/` directory on disk contains the resource files organized by
category:

| Directory | Contents |
|-----------|----------|
| `Font/` | Font programs — Type 1 (.t1, .pfa, .pfb), Type 3 (.t3, .ps), TrueType/OpenType (.ttf, .otf) |
| `CIDFont/` | CID-keyed font definitions |
| `CMap/` | Character code to CID mapping tables |
| `Encoding/` | Character encoding vectors |
| `ColorSpace/`, `ColorRendering/` | Color space definitions |
| `Form/`, `Pattern/`, `Halftone/` | Graphics resources |
| `ProcSet/` | Procedure sets |
| `Init/` | Initialization scripts (sysdict.ps, resource categories) |
| `OutputDevice/` | Device configuration dictionaries |

### Font Loading

When `findfont` is called, the resource system searches:

1. `FontDirectory` (in-memory cache of already-loaded fonts)
2. Font resource files on disk (`resources/Font/`)
3. System fonts via font mapping (`resources/Init/fontmapping.ps`)
4. If all else fails, Helvetica is used as the fallback font

Font programs are PostScript files that, when executed, call `definefont` to
register the font dictionary. PostForge supports Type 1 (CharStrings with
charstring interpreter), Type 3 (BuildGlyph procedure), Type 0 (composite
CID-keyed fonts), and Type 42 (TrueType wrapped in PostScript).

### Initialization

At startup, `resources/Init/sysdict.ps` is executed. This PostScript program
defines operators and data structures that are more naturally expressed in
PostScript than Python — error handlers, encoding vectors, resource category
setup, device initialization procedures, and the interactive executive. The
resource category infrastructure is bootstrapped by
`resources/Init/resourcecategories.ps`.


## Module Map

A quick reference to the directory structure:

| Path | Purpose |
|------|---------|
| `postforge/cli.py` | Entry point, argument parsing, context creation |
| `postforge/core/types/` | Type system — PSObject, all PS types, Context, GraphicsState |
| `postforge/core/tokenizer.py` | Byte-stream tokenizer |
| `postforge/core/error.py` | PostScript error handling |
| `postforge/core/color_space.py` | Color space infrastructure |
| `postforge/core/charstring_interpreter.py` | Type 1 font charstring interpreter |
| `postforge/core/type2_charstring.py` | Type 2 (CFF/OpenType) charstring interpreter |
| `postforge/core/display_list_builder.py` | Display list construction + clip tracking |
| `postforge/core/glyph_cache.py` | Type 3 glyph path and bitmap caching |
| `postforge/core/binary_token.py` | Binary object/token encoding/decoding |
| `postforge/core/ps_function.py` | PostScript function evaluation (Type 0/2/3/4) |
| `postforge/core/unicode_mapping.py` | Glyph name → Unicode mapping |
| `postforge/operators/control.py` | Execution engine (`exec_exec`), `exec`, `stopped` |
| `postforge/operators/_control_cy.pyx` | Cython-compiled execution engine |
| `postforge/operators/dict.py` | Operator registration, dictionary operators |
| `postforge/operators/path.py` | Path construction operators |
| `postforge/operators/painting.py` | `fill`, `stroke`, `show` |
| `postforge/operators/graphics_state.py` | `gsave`, `grestore`, state operators |
| `postforge/operators/matrix.py` | CTM manipulation |
| `postforge/operators/vm.py` | `save`, `restore`, VM allocation |
| `postforge/operators/resource.py` | Resource system operators |
| `postforge/operators/image.py` | `image`, `imagemask`, `colorimage` |
| `postforge/operators/font_ops.py` | `definefont`, `findfont`, `scalefont` |
| `postforge/operators/text_show.py` | `show`, `ashow`, `widthshow`, `kshow` |
| `postforge/operators/filter.py` | Filter framework + core filters |
| `postforge/devices/common/` | Shared Cairo rendering backend |
| `postforge/devices/png/` | PNG output device |
| `postforge/devices/pdf/` | PDF output device + font embedding |
| `postforge/devices/qt/` | Interactive Qt display |
| `postforge/utils/` | Memory analysis, profiling |
| `resources/Init/` | PostScript initialization scripts |
| `resources/Font/` | Type 1 font programs |
| `resources/OutputDevice/` | Device configuration dictionaries |
| `unit_tests/` | PostScript-based test suite |
