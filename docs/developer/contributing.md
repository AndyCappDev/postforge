# Contributing to PostForge

This guide covers everything you need to start contributing to PostForge.
Each section gives a quick summary with links to the detailed guides.

## Getting Started

Clone and install:

```bash
git clone https://github.com/AndyCappDev/postforge.git
cd postforge
./install.sh
```

The install script checks for Python 3.13+ and Cairo, creates a virtual
environment, installs dependencies, and sets up the `pf` command. Verify the
installation:

```bash
pf samples/tiger.ps          # Opens a Qt window with the rendered tiger
```

If you prefer not to install the system-wide `pf` command, you can use the
launcher script or module directly:

```bash
./postforge.sh samples/tiger.ps       # Launcher script (activates venv)
python -m postforge samples/tiger.ps  # After pip install -e .
```

## Project Layout

| Directory | Purpose |
|-----------|---------|
| `postforge/core/` | PostScript execution infrastructure (types, tokenizer, error handling, color spaces) |
| `postforge/operators/` | PostScript language operators organized by functional area |
| `postforge/devices/` | Output devices (PNG, PDF, SVG, Qt) |
| `postforge/utils/` | System utilities (memory analysis, profiling) |
| `postforge/resources/` | PostScript resource files (fonts, encodings, initialization scripts, device configs) |
| `unit_tests/` | PostScript-based test suite |

See [Architecture Overview](architecture-overview.md) for a full description of
the execution engine, type system, memory model, and rendering pipeline.

## Code Conventions

### Import Style

- **Relative imports** within the `postforge/` package:
  `from ..core import types as ps`
- **PEP 8 ordering**: standard library, third-party, then local imports
- **All imports at the top of the file** — never import inside functions

### Parameter Ordering

Operator functions always receive the context first:

```python
def operator_name(ctxt, ostack):
    ...

def operator_with_estack(ctxt, e_stack, ostack):
    ...
```

### Level 2 Compatibility

PostForge maintains strict Level 2 compatibility. Level 3 features are welcome
as additive enhancements, but they must never break existing Level 2 programs.

For the full set of conventions see the
[Architecture Overview](architecture-overview.md).

## Adding a New Operator

The workflow in brief:

1. **Consult the PLRM** — Read the operator's entry in the PostScript Language
   Reference Manual (Second Edition first, then Third Edition for updates).
   Identify all error conditions and the exact stack effect.

2. **Implement** — Add the function to the appropriate file in
   `postforge/operators/`. Follow the validate-before-pop pattern:

   ```python
   def operator_name(ctxt, ostack):
       # 1. Check stack depth
       if len(ostack) < 1:
           return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, operator_name.__name__)
       # 2. Validate types (peek with negative indexing)
       if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
           return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name.__name__)
       # 3. Only after all validation passes — pop and execute
       val = ostack.pop()
       ...
   ```

3. **Register** — Add a tuple to the `ops` list in
   `postforge/operators/dict.py`:

   ```python
   ("operator_name", ps.Operator, ps_module.function_name),
   ```

4. **Write tests** — See the next section.

See [Operator Implementation Standards](operator-implementation.md) for the
full guide covering error handling, type group constants, naming conventions,
and the complete checklist.

## Writing Tests

Tests are written in PostScript using a custom framework. The `assert`
procedure (defined in `unit_tests/unittest.ps`) compares the operand stack
after executing an operator or procedure against expected values.

### Basic Format

```postscript
% Format: operand(s) /operator|{} [expected_results] assert
(hello) /length [5] assert                    % Simple test
5 {dup mul} [25] assert                       % Procedure test
```

### Error Conditions

When an operator fails validation, it must leave all original operands on the
stack. The error name is pushed on top:

```postscript
/add [/stackunderflow] assert                 % No operands
(string) 5 /get [(string) 5 /rangecheck] assert  % Operands preserved
```

### What to Cover

- Normal operation and boundary values
- Type variations (Int vs Real, Array vs PackedArray)
- Every error condition listed in the PLRM
- Access control violations where applicable

A well-tested operator typically has 10-20+ assertions.

### Running Tests

```bash
pf unit_tests/ps_tests.ps          # All tests
pf unit_tests/string_tests.ps      # Specific test file
```

See [Testing Guide](testing-guide.md) for the full framework documentation
including visual regression testing.

## Adding an Output Device

An output device consists of two parts:

1. **PostScript resource file** (`postforge/resources/OutputDevice/<name>.ps`) — a page
   device dictionary that configures page size, resolution, and rendering mode
2. **Python module** (`postforge/devices/<name>/`) — implements
   `showpage(ctxt, pd)` to render the display list

See [Adding an Output Device](adding-output-devices.md) for the complete
walkthrough with templates, display list element reference, and advanced
patterns.

## PR Workflow

1. **Branch from master** — Use a descriptive branch name
   (e.g., `add-charpath-operator`, `fix-arc-winding`)

2. **Keep commits focused** — Separate test commits from code changes so
   reviewers can see what moved. One logical change per commit.

3. **Commit messages** — Use imperative mood and a concise summary line.
   Add detail in the body when the "why" isn't obvious from the diff.

   ```
   Add charpath operator with strokepath support

   Implements charpath per PLRM Section 8.2. Handles both
   fill and stroke variants via the bool parameter.
   ```

4. **Run the full test suite** before submitting:

   ```bash
   pf unit_tests/ps_tests.ps
   ```

5. **Open a PR against master** with a description that summarizes the change
   and links to any relevant PLRM sections or issues.

## Code Review Expectations

Reviewers will check for:

- **PLRM compliance** — Operator behavior matches the specification
- **Complete error handling** — Every error condition from the PLRM is covered
- **Validate-before-pop** — No stack corruption on error paths
- **Test coverage** — Normal operation, error conditions, and boundary cases
- **No Level 2 regressions** — Existing PostScript programs still work

## Test Integrity Policy

The test suite is the project's safety net. These rules protect it:

- **PRs that modify existing assertions** require explicit justification for
  every changed expected value
- **Test count must never decrease** — removing or weakening assertions is a
  red flag that must be explained in the PR description
- **New operators must ship with tests** — not "will add tests later"
- **Separate test commits from code commits** so reviewers can see exactly
  what changed
- **Run the test suite locally** before approving a PR
