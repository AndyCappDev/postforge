# PostForge Detailed Gap Analysis

**Date:** 2026-02-11
**Purpose:** Catalog remaining functional gaps beyond the high-level 99.7% operator coverage metric. Only incomplete or missing functionality is listed here.

---

## 1. Font Support

### Font Rendering Limitations

| Limitation | Severity | Detail |
|------------|----------|--------|
| **Font hinting not applied** | Low-Medium | Type 1 hint data is parsed and consumed during CharString execution but not applied to glyph outlines. Affects rendering quality at small sizes / low DPI. GhostScript also largely ignores hints for raster output, so practical impact is minimal. |

---

## 2. Color Spaces

### Remaining Gaps

| Color Space | Status | Impact |
|-------------|--------|--------|
| **Pattern via setcolorspace** | `[/Pattern] setcolorspace` raises UNDEFINED error (only works via name form or `setpattern`) | **LOW** — Most documents use `setpattern` directly. |

---

## 3. Filter Support

### Status

| Filter | Status | Notes |
|--------|--------|-------|
| **CCITTFaxEncode** | Not implemented | Encode direction (rarely needed for rendering) |
| **ReusableStreamDecode** | Not implemented | Name registered but no codec. Used for multi-pass stream reading. |

---

## 4. Halftone & Transfer Functions

### Halftones

| Type | Status | Detail |
|------|--------|--------|
| Types 2-5 | **Accepted, not processed** | Dictionary validation passes but no halftone-specific rendering behavior. Falls through to device defaults. |
| Types 6, 10, 16 | **Accepted, not processed** | Same as above |

**Practical impact:** Low. PostForge outputs to Cairo-backed devices (PNG, PDF, SVG) which handle their own halftoning.

### Transfer Functions

`settransfer`, `setcolortransfer`, `setblackgeneration`, `setundercolorremoval` — all **stored in graphics state but not applied during rendering**.

**Practical impact:** Low-Medium. Primarily relevant for physical print devices.

---

## 5. Other Gaps

### Pattern Minor Gaps

- TilingType-specific pixel-grid matrix adjustment not performed (may cause subtle seam artifacts)
- PaintProc not clipped to BBox during pattern cell capture

### setpagedevice Accepted-But-Ignored Keys

Most PLRM-defined page device keys (MediaClass, MediaColor, MediaWeight, MediaType, InsertSheet, LeadingEdge, ManualFeed, Duplex, Tumble, Collate, NumCopies, etc.) are accepted without error but have no effect on output.

**Practical impact:** Low. Printer-specific parameters irrelevant to screen/file output.

---

## 6. Priority Summary

### Low Priority

| Gap | Category | Notes |
|-----|----------|-------|
| Font hinting | Fonts | Minimal visual impact for raster output |
| Halftone Types 2-7 processing | Graphics | Only matters for physical print devices |
| Transfer function application | Graphics | Only matters for physical print devices |
| Pattern TilingType matrix adjustment | Graphics | Subtle rendering artifact |
| ReusableStreamDecode filter | Filters | Rarely used |

---

## 7. Feature Completeness

Taking into account both operator presence AND functional completeness:

| Area | Operator Presence | Functional Completeness | Notes |
|------|-------------------|------------------------|-------|
| Core Language | 100% | 100% | |
| Font Rendering | 100% | ~98% | Hinting not applied (minimal visual impact) |
| Font PDF Embedding | 100% | ~99% | Covers Type 1, Type 42, Type 0, CIDFont, CFF/Type1C |
| Color Spaces | 100% | ~99% | ICCBased Tier 2 + DeviceCMYK Tier 3 (ICC via lcms2); PLRM fallback |
| Filters | ~95% | ~95% | CCITTFaxDecode done; Encode not implemented |
| Images | 100% | ~98% | |
| Halftones | 100% | ~30% | Only Type 1 actually processed |
| Transfer/UCR/BG | 100% | 0% (stored only) | Values stored but never applied |
| Patterns/Forms | 100% | ~95% | Minor TilingType/BBox gaps |
| Shading | 100% | 100% | |
| Resources | 100% | 100% | |
| Page Devices | 100% | ~70% | Core keys work; media params ignored |

**Overall Functional Completeness: ~93%**

(vs 99.7% operator presence — the gap is in depth, not breadth)
