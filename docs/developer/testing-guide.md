# Testing Guide

PostForge has two kinds of tests: **unit tests** written in PostScript that
verify operator behavior, and **visual regression tests** that compare
rendered output pixel-by-pixel against baselines.

## Unit Tests

### Framework

Unit tests use a custom PostScript testing framework defined in
`unit_tests/unittest.ps`. All tests use the `assert` procedure.

### Test Format

```postscript
% Format: operand(s) /operator|{} [expected_results] assert
(hello) /length [5] assert                    % Operator by name
1 2 {add dup} [3 3] assert                    % Procedure
```

The first argument to `assert` is either an operator name (`/length`) or a
procedure (`{add dup}`), followed by an array that contains the expected operand stack state
after execution. The `arrayeq` procedure (defined in unit_tests/unittest.ps) handles deep comparison of nested
arrays and procedures.

### Testing Patterns

**Simple operations:**
```postscript
42 /abs [42] assert
-17 /abs [17] assert
```

**Composite objects:**
```postscript
[1 2 3] /length [3] assert
```

**Nested structures:**
```postscript
([[1 2] [3 4]]) /reverse [[[3 4] [1 2]]] assert
```

**Error conditions:**
```postscript
/add [/stackunderflow] assert
(string) 5 /get [(string) 5 /rangecheck] assert
```

When an operator fails validation, it must leave all original operands on the
stack. The error name is pushed on top. Tests verify this:

```postscript
operand1 operand2 /operator [operand1 operand2 /errorname] assert
```

**Procedures:**
```postscript
5 {dup mul} [25] assert
10 20 {exch sub abs} [10] assert
[] {length 0 eq} [true] assert
```

**String operations:**
```postscript
(hello) /token [() /hello true] assert
() /token [false] assert
```

### Test File Organization

Tests live in `unit_tests/` as `*_tests.ps` files (e.g., `string_tests.ps`,
`array_tests.ps`). All test files are included by `ps_tests.ps`, the main
test runner.

### Adding Tests for a New Operator

1. Find the appropriate test file by operator category
2. Add a section with the operator name as a comment header
3. Cover all of the categories below

**What to test:**

- **Basic functionality** — the common case, the reason the operator exists
- **Boundary values** — zero, empty strings, empty arrays, single-element
  collections, maximum integers, negative numbers
- **Type variations** — if the operator accepts both Int and Real, test both;
  if it accepts Array and PackedArray, test both
- **Every error condition in the PLRM** — stackunderflow, typecheck,
  rangecheck, invalidaccess, etc. Each error listed in the PLRM entry should
  have at least one test. Verify that operands remain on the stack untouched.
- **Interaction with composite objects** — nested arrays, arrays containing
  procedures, dictionaries where applicable
- **Access control** — if the operator respects read-only or execute-only
  access, test that it rejects violations with invalidaccess

A well-tested operator typically has 10–20+ assertions. If you only have 2–3,
you almost certainly haven't covered the error conditions.

**Example:**

```postscript
%% foo %%
% Basic functionality
123 /foo [246] assert
0 /foo [0] assert
-5 /foo [-10] assert

% Boundary values
2147483647 /foo [4294967294] assert          % Max integer
1.5 /foo [3.0] assert                        % Real input

% Error conditions — every PLRM error
/foo [/stackunderflow] assert                % No operands
(string) /foo [(string) /typecheck] assert   % Wrong type
true /foo [true /typecheck] assert           % Wrong type (bool)
```

### Running Unit Tests

```bash
./postforge.sh unit_tests/ps_tests.ps       # All tests
./postforge.sh unit_tests/file_tests.ps     # Specific test file
```

On failure, the framework prints the line number, expected vs actual stack
state, and the test that failed.

Unit tests also run automatically on GitHub Actions for every push to master
and every pull request (see `.github/workflows/test.yml`).

## Visual Regression Tests

Visual regression tests render sample PostScript files and compare them
pixel-by-pixel against baseline images. This catches rendering regressions
that unit tests cannot detect. Three devices are tested by default: PNG,
PDF, and SVG.

### Setup

Each device has its own directory (`visual_tests_png/`, `visual_tests_pdf/`,
`visual_tests_svg/`) containing baseline, current, and diff subdirectories.
The tool is launched via `visual_test.sh`, which handles virtual environment
activation.

PDF and SVG testing require PyMuPDF for rasterizing vector output to PNG
for comparison.

### Generating Baselines

Before you can run comparisons, generate baseline images from a known-good
state:

```bash
./visual_test.sh --baseline                  # All devices
./visual_test.sh --baseline -d png           # PNG only
```

### Running Comparisons

```bash
./visual_test.sh                             # Compare all devices
./visual_test.sh -d png                      # PNG only
./visual_test.sh --threshold 0.5             # Custom threshold (default 0)
./visual_test.sh --samples tiger.ps escher.ps  # Test specific samples
./visual_test.sh -j 8                        # 8 parallel workers
```

The `--` separator passes flags through to PostForge itself:

```bash
./visual_test.sh -- --no-glyph-cache         # Test with glyph cache disabled
```

### Per-Sample Thresholds

Some samples have acceptable pixel differences (e.g., due to randomized
content). Each device has its own config file for threshold overrides:
`visual_tests_png.conf`, `visual_tests_pdf.conf`, `visual_tests_svg.conf`.

```
fern.ps 8
maze.ps 7
snowflak.ps 95
```

### HTML Reports

Each device generates a per-device report at
`visual_tests_{device}/report.html`. When multiple devices are tested, a
combined report is generated at `visual_tests_report.html` with a navigation
bar linking to each device section.

See [Visual Regression Testing](visual-regression-testing.md) for detailed
documentation on the comparison algorithm, PDF/SVG rasterization, and
report format.

## Requirements for New Features

Every new PostScript operator must ship with tests. Both kinds of testing
are important:

- **Unit tests** — verify operator behavior, error handling, and edge cases
  using the `assert` framework
- **Visual regression** — regenerate baselines after intentional rendering
  changes to confirm nothing else broke
