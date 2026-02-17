# PostScript Operator Implementation Standards

This document covers how to implement new PostScript operators in PostForge.
Every operator must follow these patterns for PostScript compliance.

## Operator Registration

### Where to Add

1. **Implementation**: Add the operator function to the appropriate file in
   `postforge/operators/` (e.g., math operators in `math.py`, path operators
   in `path.py`).
2. **Registration**: Add a tuple to the `ops` list in
   `postforge/operators/dict.py`, inside `create_system_dict()`, under the
   appropriate commented section and in alphabetical order.

### Registration Pattern

```python
# In create_system_dict(), in the ops list:
("operator_name", ps.Operator, ps_module.function_name),
```

For example, in the file operators section:

```python
("read", ps.Operator, ps_file.read),
("readline", ps.Operator, ps_file.readline),
("readstring", ps.Operator, ps_file.readstring),
("run", ps.Operator, ps_file.run),
```

### Function Naming

Most operator functions are named after the PostScript operator they implement
(e.g., `def showpage(ctxt, ostack)`). When the PostScript name conflicts with
a Python keyword or built-in, prefix the function with `ps_`:

- `ps_def` (Python keyword `def`)
- `ps_dict` (Python built-in `dict`)
- `ps_exec`, `ps_exit`, `ps_if`, `ps_for` (Python keywords)
- `ps_and`, `ps_not`, `ps_copy` (Python built-ins)
- `ps_image`, `ps_imagemask`, `ps_colorimage` (convention for image operators)

This prefix affects error reporting — see the Error Handling section below.

## Implementation Requirements

### 1. Consult the PLRM First

Before writing any code:

- Read the operator's entry in the PostScript Language Reference Manual
  (start with the Second Edition, then check the Third Edition for updates)
- Identify **all** error conditions listed for the operator
- Note the exact stack effect notation
- Document any special behaviors or edge cases

### 2. Validate Before Popping

Every operator must validate **all** operands before popping anything from the
stack. This is a fundamental PostScript compliance requirement — when an error
occurs, the operand stack must be in its original state so error handlers can
inspect the operands that caused the failure.

Use negative indexing to peek at operands without modifying the stack:

```python
def operator_name(ctxt, ostack):
    """
    [Exact PLRM description from Section 8.2]

    Stack: [exact PLRM stack notation]
    Errors: [complete list from PLRM]
    """
    # STEP 1: Validate stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, operator_name.__name__)

    # STEP 2: Validate operand types (peek with negative indexing)
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name.__name__)
    if ostack[-2].TYPE != ps.T_ARRAY:
        return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name.__name__)

    # STEP 3: Validate access permissions (if applicable)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, operator_name.__name__)

    # STEP 4: Any operator-specific validations from PLRM

    # STEP 5: ONLY after all validation passes — pop and execute
    value = ostack.pop()
    array = ostack.pop()
    # ... perform the actual work ...
```

**Never** pop operands before validation is complete:

```python
# WRONG — stack is corrupted if the second check fails
operand1 = ostack.pop()
if ostack[-1].TYPE != ps.T_ARRAY:
    return ps_error.e(ctxt, ps_error.TYPECHECK, ...)
```

### 3. Error Conditions

For every operator, check the applicable conditions from this list:

| Error | Condition |
|-------|-----------|
| `stackunderflow` | Not enough operands on the stack |
| `stackoverflow` | Result would exceed operand stack capacity |
| `typecheck` | Wrong operand type |
| `invalidaccess` | Access permission violation |
| `rangecheck` | Value outside acceptable range |
| `limitcheck` | Implementation limit exceeded |

Plus any operator-specific errors documented in the PLRM.

### 4. Error Handling

Use `ps_error.e()` for all PostScript errors. Never raise Python exceptions
for PostScript error conditions.

```python
return ps_error.e(ctxt, ps_error.TYPECHECK, operator_name.__name__)
```

The third argument is the operator name string for error reporting. Normally
use `__name__` to avoid redundant string allocations. However, for functions
with a `ps_` prefix, use a hardcoded string of the actual PostScript operator
name:

```python
# Normal case — __name__ gives the correct PostScript name
def showpage(ctxt, ostack):
    return ps_error.e(ctxt, ps_error.TYPECHECK, showpage.__name__)  # "showpage"

# ps_ prefix — __name__ would give "ps_image", but PostScript name is "image"
def ps_image(ctxt, ostack):
    return ps_error.e(ctxt, ps_error.TYPECHECK, "image")

# Helper called on behalf of a parent operator — pass parent name as parameter
def _image_helper(ctxt, ostack, op_name):
    return ps_error.e(ctxt, ps_error.TYPECHECK, op_name)
```

### 5. Type Group Constants

Use the pre-defined type groupings from `postforge/core/types/constants.py`
for efficient type checking:

| Constant | Types | Use for |
|----------|-------|---------|
| `NUMERIC_TYPES` | Int, Real | Numeric operands |
| `ARRAY_TYPES` | Array, PackedArray | Array operands |
| `COMPOSITE_TYPES` | Array, Dict, String, PackedArray | Composite objects |
| `STREAM_TYPES` | File, String | I/O operations |
| `CONTAINER_TYPES` | Array, Dict, PackedArray | Container objects |
| `IMMUTABLE_TYPES` | Int, Real, Bool, Null, Name | Read-only objects |

```python
# Preferred — use type groups
if ostack[-1].TYPE in ps.NUMERIC_TYPES:

# Avoid — multiple individual comparisons
if ostack[-1].TYPE == ps.T_INT or ostack[-1].TYPE == ps.T_REAL:
```

### 6. Parameter Ordering Convention

Operator functions always receive `ctxt` (the execution context) as the first
parameter, followed by `ostack` (the operand stack):

```python
def operator_name(ctxt, ostack):
```

Some operators also receive the execution stack, in which case the order is
`ctxt`, `e_stack`, `ostack`. Follow existing operators in the same file for
the correct signature.

### 7. Docstring Format

Each operator must include a docstring with the exact PLRM description. This
is what the `help` operator displays in interactive mode:

```python
def readstring(ctxt, ostack):
    """
    file string **readstring** substring bool

    Reads characters from file into string until either the
    entire string has been filled or an end-of-file indication
    is encountered...

    Stack: file string readstring substring bool
    Errors: invalidaccess, ioerror, rangecheck, stackunderflow, typecheck
    """
```

Use the exact PLRM wording — do not paraphrase. This ensures specification
compliance and makes it easy to verify against the manual.

## Operator Checklist

Before considering an operator complete:

- [ ] PLRM consulted — all error conditions identified
- [ ] All validation implemented before any pops
- [ ] Every PLRM error condition handled
- [ ] No Python exceptions raised during PostScript error conditions
- [ ] PostScript tests written (see [Testing Guide](testing-guide.md))
- [ ] Registered in `create_system_dict()` in `postforge/operators/dict.py`
