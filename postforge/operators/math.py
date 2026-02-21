# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import math
import random

from ..core import error as ps_error
from ..core import types as ps


def ps_abs(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ **abs** num₂


    returns the absolute value of num1. The type of the result is the same as the type of
    num₁ unless num₁ is the smallest (most negative) integer, in which case the result
    is a real number.

    **Examples**
        4.5 **abs**     -> 4.5
        –3 **abs**      -> 3
        0 **abs**       -> 0

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **neg**
    """
    op = "abs"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    result = abs(ostack[-1].val)
    if isinstance(result, int):
        ostack[-1] = ps.Int(result)
    else:
        ostack[-1] = ps.Real(result)


def add(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ num₂ **add** sum


    returns the sum of num₁ and num₂. If both operands are integers and the result is
    within integer range, the result is an integer; otherwise, the result is a real number.

    **Examples**
        3 4 **add**         -> 7
        9.9 1.1 **add**     -> 11.0

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **div**, **mul**, **sub**, **idiv**, **mod**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, add.__name__)
    
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, add.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, add.__name__)

    result = ostack[-2].val + ostack[-1].val
    ostack.pop()
    if isinstance(result, int):
        ostack[-1] = ps.Int(result)
    else:
        ostack[-1] = ps.Real(result)


def atan(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num den **atan** angle


    returns the angle (in degrees between 0 and 360) whose tangent is num divided by
    den. Either num or den may be 0, but not both. The signs of num and den determine
    the quadrant in which the result will lie: a positive num yields a result in the
    positive y plane, while a positive den yields a result in the positive x plane.
    The result is a real number.

    **Examples**
        0 1 **atan**        -> 0.0
        1 0 **atan**        -> 90.0
        -100 0 **atan**     -> 270.0
        4 4 **atan**        -> 45.0

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **cos**, **sin**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, atan.__name__)
    
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, atan.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, atan.__name__)

    if ostack[-2].val == 0 and ostack[-1].val == 0:
        return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, atan.__name__)

    result = math.degrees(math.atan2(ostack[-2].val, ostack[-1].val))
    if result < 0:
        result = 360 - abs(result)
    ostack.pop()
    ostack[-1] = ps.Real(result)


def ceiling(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ **ceiling** num₂


    returns the least integer value greater than or equal to num₁. The type of
    the result is the same as the type of the operand.

    **Examples**
        3.2 **ceiling**     -> 4.0
        –4.8 **ceiling**    -> –4.0
        99 **ceiling**      -> 99

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **floor**, **round**, **truncate**, **cvi**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ceiling.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ceiling.__name__)

    result = math.ceil(ostack[-1].val)
    if isinstance(ostack[-1].val, int):
        ostack[-1] = ps.Int(int(result))
    else:
        ostack[-1] = ps.Real(float(result))


def cos(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    angle **cos** real


    returns the cosine of angle, which is interpreted as an angle in degrees.
    The result is a real number.

    **Examples**
        0 **cos**   -> 1.0
        90 **cos**  -> 0.0

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **atan**, **sin**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cos.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cos.__name__)

    ostack[-1] = ps.Real(math.cos(math.radians(ostack[-1].val)))


def div(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ num₂ **div** quotient


    divides num₁ by num₂, producing a result that is always a real number even if both
    operands are integers. Use **idiv** instead if the operands are integers and an integer
    result is desired.

    **Examples**
        3 2 **div**     -> 1.5
        4 2 **div**     -> 2.0

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **idiv**, **add**, **mul**, **sub**, **mod**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, div.__name__)
    
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, div.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, div.__name__)

    # Check for division by zero
    if ostack[-1].val == 0:
        return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, div.__name__)

    result = ostack[-2].val / ostack[-1].val
    ostack.pop()
    ostack[-1] = ps.Real(result)


def exp(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    base exponent **exp** real


    raises base to the exponent power. The operands may be either integers or real
    numbers. If the exponent has a fractional part, the result is meaningful only if the
    base is nonnegative. The result is always a real number.

    **Examples**
        9 0.5 **exp**   -> 3.0
        -9 -1 **exp**   -> -0.111111

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **sqrt**, **ln**, **log**, **mul**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, exp.__name__)
    
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, exp.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, exp.__name__)

    result = math.pow(ostack[-2].val, ostack[-1].val)
    ostack.pop()
    ostack[-1] = ps.Real(result)


def floor(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ **floor** num₂


    returns the greatest integer value less than or equal to num₁. The type of the result
    is the same as the type of the operand.

    **Examples**
        3.2 **floor**   -> 3.0
        -4.8 **floor**  -> -5.0
        99 **floor**    -> 99

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **ceiling**, **round**, **truncate**, **cvi**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, floor.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, floor.__name__)

    result = math.floor(ostack[-1].val)
    if isinstance(ostack[-1].val, int):
        ostack[-1] = ps.Int(int(result))
    else:
        ostack[-1] = ps.Real(float(result))


def idiv(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    int₁ int₂ **idiv** quotient


    divides int₁ by int₂ and returns the integer part of the quotient, with any fractional
    part discarded. Both operands of **idiv** must be integers and the result is an integer.

    **Examples**
        3 2 **idiv**    -> 1
        4 2 **idiv**    -> 2
        -5 2 **idiv**   -> -2

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **div**, **add**, **mul**, **sub**, **mod**, **cvi**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, idiv.__name__)
    # 2. TYPECHECK - Check operand types (int₁ int₂)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, idiv.__name__)
    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, idiv.__name__)

    # Check for division by zero
    if ostack[-1].val == 0:
        return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, idiv.__name__)

    result = int(ostack[-2].val / ostack[-1].val)
    ostack.pop()
    ostack[-1] = ps.Int(result)


def ln(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num **ln** real


    returns the natural logarithm (base e) of num. The result is a real number.

    **Examples**
        10 ln   -> 2.30259
        100 ln  -> 4.60517

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **log**, **exp**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ln.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ln.__name__)

    # 3. RANGECHECK - num must be positive
    if ostack[-1].val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, ln.__name__)

    ostack[-1] = ps.Real(math.log(ostack[-1].val))


def log(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num **log** real


    returns the common logarithm (base 10) of num. The result is a real number.

    **Examples**
        10 **log**      -> 1.0
        100 **log**     -> 2.0

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **ln**, **exp**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, log.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, log.__name__)

    # 3. RANGECHECK - num must be positive
    if ostack[-1].val <= 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, log.__name__)

    ostack[-1] = ps.Real(math.log(ostack[-1].val, 10))


def ps_max(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ num₂ **max** num₁|num₂


    returns one of num₁ or num₂, whichever is the larger number.

    **Examples**:
        1 2     -> 2
        15.0 5  -> 15.0

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **min**
    """
    op = "max"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    if ostack[-1].val > ostack[-2].val:
        ostack[-1], ostack[-2] = ostack[-2], ostack[-1]
    ostack.pop()


def ps_min(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ num₂ **min** num₁|num₂


    returns num₁ or num₂, whichever is the smaller number.

    **Examples**:
        1 2     -> 1
        15 5.0  -> 5.0

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **max**
    """
    op = "min"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    if ostack[-1].val < ostack[-2].val:
        ostack[-1], ostack[-2] = ostack[-2], ostack[-1]
    ostack.pop()


def mod(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    int₁ int₂ **mod** remainder


    returns the remainder that results from dividing int₁ by int₂. The sign of the result
    is the same as the sign of the dividend int₁. Both operands must be integers and
    the result is an integer.

    **Examples**
        5 3 **mod**     -> 2
        5 2 **mod**     -> 1
        -5 3 **mod**    -> -2

    The last example above demonstrates that **mod** is a remainder operation rather
    than a true modulo operation.

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **idiv**, **div**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, mod.__name__)
    # 2. TYPECHECK - Check operand types (int₁ int₂)
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, mod.__name__)
    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, mod.__name__)

    # Check for modulo by zero
    if ostack[-1].val == 0:
        return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, mod.__name__)

    int1 = ostack[-2].val  # dividend
    int2 = ostack[-1].val  # divisor
    result = abs(int1) % abs(int2)
    # PLRM: sign of result is the same as the sign of the dividend (int1)
    if int1 < 0:
        result = -result

    ostack.pop()
    ostack[-1] = ps.Int(result)


def mul(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ num₂ **mul** product


    returns the product of num₁ and num₂. If both operands are integers and
    the result is within integer range, the result is an integer; otherwise,
    the result is a real number.

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **div**, **idiv**, **add**, **sub**, **mod**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, mul.__name__)
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, mul.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, mul.__name__)

    result = ostack[-2].val * ostack[-1].val
    ostack.pop()
    if isinstance(result, int):
        ostack[-1] = ps.Int(result)
    else:
        ostack[-1] = ps.Real(result)


def neg(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ **neg** num₂


    returns the negative of num₁. The type of the result is the same as the type
    of num₁ unless num₁ is the smallest (most negative) integer, in which case
    the result is a real number.

    **Examples**
        4.5 **neg**     -> -4.5
        -3 **neg**      -> 3

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **abs**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, neg.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, neg.__name__)

    result = -ostack[-1].val
    if isinstance(result, int):
        ostack[-1] = ps.Int(result)
    else:
        ostack[-1] = ps.Real(result)


def rand(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **rand** int


    returns a random integer in the range 0 to 2**31 - 1, produced by a pseudo-random
    number generator. The random number generator’s state can be reset by **srand**
    and interrogated by **rrand**.

    **Errors**:     **stackoverflow**
    **See Also**:   **srand**, **rrand**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, rand.__name__)

    # Generate random integer in PostScript range [0, MAX_POSTSCRIPT_INTEGER - 1]
    ostack.append(ps.Int(random.randrange(ps.MAX_POSTSCRIPT_INTEGER)))


def rrand(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **rrand** int


    returns an integer representing the current state of the random number generator
    used by **rand**. This may later be presented as an operand to **srand** to reset the
    random number generator to the current position in the sequence of numbers
    produced.

    **Errors**:     **stackoverflow**
    **See Also**:   **rand**, **srand**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, rand.__name__)

    ostack.append(ps.Int(ctxt.random_seed))


def ps_round(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ **round** num₂


    returns the integer value nearest to num₁. If num₁ is equally close to its two nearest
    integers, **round** returns the greater of the two. The type of the result is the same as
    the type of the operand.

    **Examples**
        3.2 **round**       -> 3.0
        6.5 **round**       -> 7.0
        -4.8 **round**      -> -5.0
        -6.5 **round**      -> -6.0
        99 **round**        -> 99

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **ceiling**, **floor**, **truncate**, **cvi**
    """
    op = "round"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    result = math.floor(ostack[-1].val + 0.5)
    if isinstance(ostack[-1].val, int):
        ostack[-1] = ps.Int(int(result))
    else:
        ostack[-1] = ps.Real(float(result))


def sin(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    angle **sin** real


    returns the sine of angle, which is interpreted as an angle in degrees.
    The result is a real number.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **cos**, **atan**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, sin.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, sin.__name__)

    ostack[-1] = ps.Real(math.sin(math.radians(ostack[-1].val)))


def srand(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    int **srand** -


    initializes the random number generator with the seed int, which may be
    any integer value. Executing **srand** with a particular value causes subsequent
    invocations of **rand** to generate a reproducible sequence of results.

    In an interpreter that supports multiple execution contexts, the random number
    state is maintained separately for each context.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **rand**, **rrand**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, srand.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, srand.__name__)

    ctxt.random_seed = ostack[-1].val
    random.seed(ctxt.random_seed)
    ostack.pop()


def sqrt(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num **sqrt** real


    returns the square root of num, which must be a nonnegative number.
    The result is a real number.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **exp**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, sqrt.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, sqrt.__name__)

    if ostack[-1].val < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, sqrt.__name__)

    ostack[-1] = ps.Real(math.sqrt(ostack[-1].val))


def sub(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ num₂ **sub** difference


    returns the result of subtracting num₂ from num₁. If both operands are
    integers and the result is within integer range, the result is an integer;
    otherwise, the result is a real number.

    **Errors**:     **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **add**, **div**, **mul**, **idiv**, **mod**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, sub.__name__)
    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, sub.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, sub.__name__)

    result = ostack[-2].val - ostack[-1].val
    ostack.pop()
    if isinstance(result, int):
        ostack[-1] = ps.Int(result)
    else:
        ostack[-1] = ps.Real(result)


def truncate(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    num₁ **truncate** num₂


    truncates num₁ toward 0 by removing its fractional part. The type of
    the result is the same as the type of the operand.

    **Examples**
        3.2 **truncate**    -> 3.0
        -4.8 **truncate**   -> -4.0
        99 **truncate**     -> 99

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **ceiling**, **floor**, **round**, **cvi**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, truncate.__name__)
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, truncate.__name__)

    result = math.trunc(ostack[-1].val)
    if isinstance(ostack[-1].val, int):
        ostack[-1] = ps.Int(int(result))
    else:
        ostack[-1] = ps.Real(float(result))
