# PRD: Documentation Reorganization

## Problem

PostForge's documentation has several gaps:

1. **No index or signposting**: Files are organized by category in subdirectories
   but there is no index file to help users find what they need.

2. **Architecture docs live in CLAUDE.md**: The most comprehensive description of
   PostForge's architecture, module organization, coding conventions, and development
   workflow is in `CLAUDE.md` — an AI instruction file. Human developers shouldn't
   need to read an AI prompt to understand the codebase.

3. **Minimal README**: The `README.md` is 45 lines with a bare-bones quick start,
   a 5-bullet feature list, and no screenshots, examples, or comparison with
   alternatives like GhostScript.

4. **Missing key developer docs**: There's no guide for:
   - How to add a new output device (despite 4 existing devices as examples)
   - Architecture overview for human readers
   - Contributing guide (PR workflow, code conventions, test requirements)
   - How the PostScript execution model works in PostForge specifically

5. **Existing docs not reviewed for accuracy**: Documents may reference old file
   paths, deleted modules, or describe plans that were implemented differently.

## Background

### Current `docs/` Structure

```
docs/
  user/                              # 1 file
    user-guide.md
  developer/                         # 4 files + 2 subdirectories
    operator-implementation.md
    testing-guide.md
    profiling.md
    visual-regression-testing.md
    architecture/
      standardfile-system.md
    diagrams/
      (6 .mmd files + 3 .md files)
  design/                            # 40 files (PRDs, plans, analyses)
    PRD_*.md, prd-*.md               # PRDs
    *_Implementation_Plan.md         # Implementation plans
    *_Analysis.md                    # Analysis docs
    TODO.md, STUB_RESOURCES.md, etc. # Misc design docs
  reference/                         # 7 files
    *.pdf                            # PostScript/font spec PDFs
    *.txt                            # Spec text files, URW license
```

### Current README.md (45 lines)

Contains: project description (1 paragraph), quick start (6 lines), feature list
(5 bullets), requirements (3 lines), documentation links (2), license (2 lines).

Missing: screenshots, rendered output examples, feature depth, architecture
overview, comparison with GhostScript, contribution guidelines, badge/status
indicators.

### CLAUDE.md as Architecture Documentation

`CLAUDE.md` contains the most detailed architecture documentation:
- Complete module organization with descriptions of every directory and key file
- Import conventions and code patterns
- Graphics state management details
- Memory management (dual VM system)
- PostScript execution model explanation
- Development workflow (running, testing, building)
- Operator implementation standards

This information needs to exist in human-readable developer docs as well.

## Approach

Expand `README.md` to be a proper project landing page, review existing docs for
accuracy, and create the missing developer guides.

**Non-goals**: This PRD does not cover writing comprehensive API documentation or
generating docs from code. The focus is on filling the most critical gaps.

## Implementation Steps

### Step 1: Review All Existing Documents — COMPLETED

35 completed/stale design docs deleted (including prd-cpython-performance,
prd-binary-token-encoding, prd-type3-font-caching, TODO, and outdated diagrams
directory). Remaining design docs (gap-analysis, compliance-assessment,
benchmark-suite) reviewed for stale `src/` paths and outdated info (CCITTFax
status, ReusableStreamDecode, PPM references).

Developer docs reviewed and rewritten:
- `operator-implementation.md` — fixed import paths, added function naming
- `testing-guide.md` — restructured, added visual regression section
- `visual-regression-testing.md` — added missing CLI options
- `profiling.md` — reduced to 99 lines, fixed paths, added memory analysis
- `standardfile-system.md` — fixed file paths
- Outdated `diagrams/` directory deleted (9 files)

All existing documents reviewed.

### Step 2: Expand README.md — COMPLETED

Expanded from 45 lines to a full project landing page with: shields.io badges
(license, Python version), 3-paragraph project description, hero sample image
with gallery link, 7 detailed feature bullets with specifics (547+ operators,
font embedding, ICC, Cython, 47 test files), usage examples (interactive, PNG,
PDF, Qt), comparison table (PostForge vs GhostScript), organized documentation
links (users/developers/design), contributing section, and requirements list.

### Step 3: Create Architecture Overview — COMPLETED

Created as `docs/developer/architecture-overview.md`. Covers the high-level
pipeline, execution engine (five execution paths), type system, memory model,
graphics pipeline, output devices, resource system, and module map.

### Step 4: Create Contributing Guide — COMPLETED

Created as `docs/developer/contributing.md`. Covers getting started (clone,
install, `pf` command), project layout table, code conventions (copyright
header, import style, parameter ordering, Level 2 compatibility), adding a new
operator (validate-before-pop summary with code example, registration tuple),
writing tests (`assert` format, error condition testing, coverage expectations),
adding an output device (two-part summary with link), PR workflow (branching,
commit messages, test suite), code review expectations, and test integrity
policy.

### Step 5: Create "Adding an Output Device" Guide — COMPLETED

Created as `docs/developer/adding-output-devices.md`. Covers the showpage
dispatch flow, device discovery, PS resource file template with all key entries
explained, Python module structure, display list rendering (both Cairo-based and
direct processing), complete display list element reference table, advanced
patterns (multi-page state, job finalization, custom text handling, interactive
rendering), existing device summaries, and an end-to-end checklist.

### Step 6: Add Index Files — COMPLETED

Created `docs/README.md` as a documentation index with three sections: For
Users (user guide, sample gallery), For Developers (contributing guide,
architecture overview, operator implementation, testing, visual regression,
output devices, profiling, StandardFile system), and Design Documents (all
four remaining design docs linked individually).

## Dependencies

- **No hard blockers**: Documentation work is independent of code changes
- **README screenshots**: Need to render a few sample PS files and save the PNG
  output as `docs/images/` for the README
- **CI badge**: README badge for CI status depends on prd-testing-and-ci.md
- **pip install instructions**: README install section depends on prd-cli-packaging.md
  (can add a placeholder and update later)

## Key Files

| File | Role |
|------|------|
| `README.md` | Project landing page — needs major expansion |
| `CLAUDE.md` | AI instructions — architecture info to extract |
| `docs/user/user-guide.md` | Current user guide — needs expansion |
| `docs/developer/operator-implementation.md` | Developer guide (content is good) |
| `docs/developer/testing-guide.md` | Developer guide (content is good) |
| `docs/design/` | 40 design docs (PRDs, plans, analyses) |
| `docs/reference/` | 7 external spec/reference files |
| `postforge/devices/png/png.py` | Example for "adding output devices" guide |
| `postforge/devices/pdf/pdf.py` | Complex device example |

## Verification

- [ ] All existing docs reviewed for accuracy — outdated content corrected or flagged
- [ ] PRDs marked with status (Completed/Partially completed/Superseded/Abandoned)
- [ ] Documents with no remaining value deleted
- [ ] `docs/README.md` index lists all documents by category
- [ ] `README.md` has rendered output examples (actual images)
- [ ] `README.md` has expanded feature list, usage examples, and comparison section
- [x] `docs/developer/architecture-overview.md` exists with module organization and
  execution model
- [x] `docs/developer/contributing.md` exists with setup, conventions, and workflow
- [x] `docs/developer/adding-output-devices.md` exists with walkthrough and examples
- [ ] No broken links across all documentation files

## Priority

**Medium** — This doesn't affect functionality, but it's critical for community
building and contributor onboarding. Should be done before any public release or
promotion.

## Estimated Effort

- Step 1 (review all existing docs): ~~Large~~ Reduced — 32 stale docs deleted,
  `operator-implementation.md` reviewed. Remaining: ~8 developer docs + user guide.
- Step 2 (README expansion): Medium — needs rendered example images
- Step 3 (architecture doc): ~~Medium~~ **DONE**
- Step 4 (contributing guide): ~~Small~~ **DONE**
- Step 5 (output device guide): ~~Medium~~ **DONE**
- Step 6 (index): Small
