# PostForge Code Quality & Consistency Audit

**Date:** 2026-02-20

---

## HIGH-PRIORITY — Consistency Issues Worth Fixing

### ~~1. Redundant `self.access = access` after `super().__init__()`~~ FIXED

Removed redundant `self.access = access` in String.__init__ and Dict.__init__ (already set by super()).

### ~~2. Primitive types bypass `super().__init__()`~~ FIXED

Added comment documenting that Bool/Null/Int/Real set attributes directly (bypassing super().__init__()) intentionally for performance — these are the most frequently created PS objects.

### ~~3. Unreachable code in Dict.__str__~~ FIXED

Removed unreachable dead code block (old alternative implementation) after early return in Dict.__str__.

### ~~4. Error handling pattern inconsistency in operators~~ FIXED

Converted 11 two-statement `ps_error.e(...); return` patterns to standard `return ps_error.e(...)` across 7 files: control_flow.py, file.py, font_ops.py, image.py, interpreter_params.py, job_control.py, text_show.py. Also fixed wrong operator name in job_control.py exitserver() (was "setsystemparams").

### ~~5. `ps_` prefix naming inconsistency~~ FIXED

Deeper analysis confirmed the rule is clear and consistently applied: `ps_` prefix is used when the operator name would shadow a Python builtin or keyword (26 operators, all compliant). Renamed the one outlier — `help` → `ps_help` — and switched its error calls to hardcoded `"help"` strings per convention. The PS operator name remains `help`.

---

## MEDIUM-PRIORITY — Structural Gaps

### ~~6. Missing `__copy__` on HardReturn~~ FIXED

Added `__copy__` method to HardReturn for consistency with Stopped and other control flow types.

### ~~7. Missing `__eq__`/`__hash__` on several types~~ NOT AN ISSUE

Loop, Operator, Save, Font — none implement `__eq__` or `__hash__`. Research confirmed this is correct: the PLRM defines no equality semantics for these types, they are never compared or used as dict keys in the codebase, and Python's default identity-based equality is appropriate for these internal infrastructure types.

### ~~8. Broad `except Exception:` in cairo_images.py~~ FIXED

Narrowed all 15 `except Exception` blocks to specific exception types: inner Cairo rendering blocks catch `(cairo.Error, ValueError)`, outer rendering function blocks catch `(cairo.Error, ValueError, TypeError, IndexError)`, and data conversion blocks catch appropriate subsets of `(ValueError, TypeError, IndexError, KeyError, ZeroDivisionError)`. MemoryError now propagates instead of being silently swallowed.

### ~~9. Large functions that could benefit from extraction~~ WON'T FIX

Both are hot-path interpreter loops where the long `if/elif` dispatch chain is the most efficient structure. Breaking them into helper functions would add per-token function call overhead with no functional benefit.

- `postforge/core/tokenizer.py` — `__token()` (459 lines): token-type dispatch loop
- `postforge/core/color_space.py` — `_exec_cie_tokens()` (202 lines): CIE procedure mini-interpreter

### ~~10. Color extraction logic duplicated 8 times~~ FIXED

Extracted `_safe_rgb()` utility into `cairo_utils.py` and replaced all 8 instances across `cairo_renderer.py` (6 instances: Fill, Stroke, text rendering, glyph replay Fill, _replay_glyph_elements Fill and Stroke), `cairo_patterns.py` (2 instances: Fill and Stroke), and `cairo_images.py` (1 instance: imagemask color normalization).

---

## LOW-PRIORITY — Minor Cleanup

### ~~11. Typo~~ FIXED

Fixed "actualy" → "actually" in String.__init__ comment.

### ~~12. Commented-out code~~ FIXED

Removed all three instances: dead `# self.is_global` in String, commented-out operator registration in dict.py, and dead try/except block in charstring_interpreter.py.

### 13. TODOs remaining

- `color_space.py:334,341` — Custom BG/UCR procedure calling not implemented
- `painting.py:351` — Encoded number strings not supported in rectfill
- `clipping.py:305` — Encoded number strings not supported in rectclip

### ~~14. Missing docstrings~~ FIXED

Added docstrings to 4 `NoOpBackend` methods in `postforge/utils/profiler.py` (the abstract base class methods already had them).

---

### 15. Type annotation inconsistency across codebase

~85% of files have no annotations, and the ~15% that do mix old-style (`typing.Dict`,
`Optional[X]`) with new-style (`dict`, `X | None`). Standardizing to Python 3.13 style
with `from __future__ import annotations` across all ~100 `.py` files.

**Convention:** `from __future__ import annotations`, lowercase builtins, `X | None`
union syntax, minimal `typing` imports. Cython `.pyx` excluded.

**Tracking:** Implemented in 6 code batches:
- Batch 1: Type system foundation (core/types/) — PENDING
- Batch 2: Core infrastructure (core/) — PENDING
- Batch 3: Simple operators (operators/) — PENDING
- Batch 4: Complex operators (operators/) — PENDING
- Batch 5: Devices (devices/) — PENDING
- Batch 6: CLI, utils, init files — PENDING

---

## AREAS OF EXCELLENCE

- **Validation order** — All operators follow correct STACKUNDERFLOW → TYPECHECK → INVALIDACCESS → pop sequence. No violations found.
- **Copyright headers** — 100% compliant across all files.
- **Import patterns** — Consistent relative imports, correct ordering, no violations.
- **Device interface** — All 4 output devices implement identical `showpage()` signatures with consistent delegation to shared Cairo renderer.
- **Documentation** — Operator docstrings are comprehensive with PLRM stack notation throughout.
- **Error propagation** — Consistent `ps_error.e()` usage across the entire codebase.
- **Architecture adherence** — Files are correctly placed in their architectural layers (core/, operators/, devices/, utils/).
- **Optional dependency handling** — PySide6, pypdf gracefully handled with availability flags.
