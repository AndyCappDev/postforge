# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import math
from decimal import Decimal, getcontext
from typing import Tuple, Union

from ..core import error as ps_error
from ..core import types as ps

# Set high precision for decimal arithmetic
getcontext().prec = 50


def _setCTM(ctxt, m) -> None:
    """
    Sets the CTM to the matrix list m
    Also sets iCTM to the inverse of m
    """

    ctxt.gstate.CTM.setval(list(m))

    invt = _matrix_inverse(
        [[m[0].val, m[1].val, 0], [m[2].val, m[3].val, 0], [m[4].val, m[5].val, 1]]
    )
    if invt is None:
        invt = [[0.0, 0.0, 0], [0.0, 0.0, 0], [0.0, 0.0, 1]]

    ctxt.gstate.iCTM.setval(
        [
            ps.Real(invt[0][0]),
            ps.Real(invt[0][1]),
            ps.Real(invt[1][0]),
            ps.Real(invt[1][1]),
            ps.Real(invt[2][0]),
            ps.Real(invt[2][1]),
        ]
    )


def _transform_point(
    transformation_matrix, x: Union[int, float], y: Union[int, float]
) -> Tuple[Union[int, float], Union[int, float]]:
    # Use high-precision decimal arithmetic to minimize rounding errors
    x_dec = Decimal(str(x))
    y_dec = Decimal(str(y))
    m00_dec = Decimal(str(transformation_matrix.val[0].val))
    m01_dec = Decimal(str(transformation_matrix.val[1].val))
    m10_dec = Decimal(str(transformation_matrix.val[2].val))
    m11_dec = Decimal(str(transformation_matrix.val[3].val))
    m20_dec = Decimal(str(transformation_matrix.val[4].val))
    m21_dec = Decimal(str(transformation_matrix.val[5].val))

    xt_dec = m00_dec * x_dec + m10_dec * y_dec + m20_dec
    yt_dec = m01_dec * x_dec + m11_dec * y_dec + m21_dec

    # Round to eliminate tiny precision errors before converting to float
    # Round to 10 decimal places to eliminate floating-point artifacts
    xt = float(xt_dec.quantize(Decimal('0.0000000001')))
    yt = float(yt_dec.quantize(Decimal('0.0000000001')))

    return xt, yt


def _transform_delta(
    transformation_matrix, x: Union[int, float], y: Union[int, float]
) -> Tuple[Union[int, float], Union[int, float]]:
    # Use high-precision decimal arithmetic to minimize rounding errors
    x_dec = Decimal(str(x))
    y_dec = Decimal(str(y))
    m00_dec = Decimal(str(transformation_matrix.val[0].val))
    m01_dec = Decimal(str(transformation_matrix.val[1].val))
    m10_dec = Decimal(str(transformation_matrix.val[2].val))
    m11_dec = Decimal(str(transformation_matrix.val[3].val))

    xt_dec = m00_dec * x_dec + m10_dec * y_dec
    yt_dec = m01_dec * x_dec + m11_dec * y_dec

    # Round to eliminate tiny precision errors before converting to float
    # Round to 10 decimal places to eliminate floating-point artifacts
    xt = float(xt_dec.quantize(Decimal('0.0000000001')))
    yt = float(yt_dec.quantize(Decimal('0.0000000001')))

    return xt, yt


def _matmult(mat1, mat2, dst) -> None:
    """
    Multiplies mat1 by mat2 and deposits the results into the dst matrix.
    Uses high-precision decimal arithmetic to minimize rounding errors.
    Assumes the length of dst is 6 (PostScript transformation matrix format).
    """
    # Convert all matrix elements to high-precision Decimal
    m1_00 = Decimal(str(mat1.val[0].val))
    m1_01 = Decimal(str(mat1.val[1].val))
    m1_10 = Decimal(str(mat1.val[2].val))
    m1_11 = Decimal(str(mat1.val[3].val))
    m1_20 = Decimal(str(mat1.val[4].val))
    m1_21 = Decimal(str(mat1.val[5].val))

    m2_00 = Decimal(str(mat2.val[0].val))
    m2_01 = Decimal(str(mat2.val[1].val))
    m2_10 = Decimal(str(mat2.val[2].val))
    m2_11 = Decimal(str(mat2.val[3].val))
    m2_20 = Decimal(str(mat2.val[4].val))
    m2_21 = Decimal(str(mat2.val[5].val))

    # Perform matrix multiplication using Decimal arithmetic
    # Result matrix elements: [a b c d tx ty]
    # Matrix multiplication for 3x3 homogeneous matrices:
    # [m1_00 m1_01  0 ]   [m2_00 m2_01  0 ]
    # [m1_10 m1_11  0 ] × [m2_10 m2_11  0 ]
    # [m1_20 m1_21  1 ]   [m2_20 m2_21  1 ]

    result_00 = m1_00 * m2_00 + m1_01 * m2_10  # a
    result_01 = m1_00 * m2_01 + m1_01 * m2_11  # b
    result_10 = m1_10 * m2_00 + m1_11 * m2_10  # c
    result_11 = m1_10 * m2_01 + m1_11 * m2_11  # d
    result_20 = m1_20 * m2_00 + m1_21 * m2_10 + m2_20  # tx
    result_21 = m1_20 * m2_01 + m1_21 * m2_11 + m2_21  # ty

    # Round and convert back to float, then store in PostScript Real objects
    if not dst.is_global and hasattr(dst, '_cow_check'):
        dst._cow_check()
    dst.val[0] = ps.Real(float(result_00.quantize(Decimal('0.0000000001'))))
    dst.val[1] = ps.Real(float(result_01.quantize(Decimal('0.0000000001'))))
    dst.val[2] = ps.Real(float(result_10.quantize(Decimal('0.0000000001'))))
    dst.val[3] = ps.Real(float(result_11.quantize(Decimal('0.0000000001'))))
    dst.val[4] = ps.Real(float(result_20.quantize(Decimal('0.0000000001'))))
    dst.val[5] = ps.Real(float(result_21.quantize(Decimal('0.0000000001'))))


def _matrix_deternminant(m):
    """
    Calculate matrix determinant using high-precision decimal arithmetic.
    Recursive function that handles matrices of any size.
    """
    # Convert matrix elements to Decimal for high-precision calculation
    def to_decimal_matrix(matrix):
        return [[Decimal(str(element)) for element in row] for row in matrix]

    def calculate_determinant_decimal(dec_matrix):
        # base case for 2x2 matrix
        if len(dec_matrix) == 2:
            return dec_matrix[0][0] * dec_matrix[1][1] - dec_matrix[0][1] * dec_matrix[1][0]

        determinant = Decimal('0')
        for c in range(len(dec_matrix)):
            # Create minor matrix by removing row 0 and column c
            minor = [row[:c] + row[c + 1:] for row in dec_matrix[1:]]
            # Recursive call with alternating signs
            determinant += (Decimal('-1') ** c) * dec_matrix[0][c] * calculate_determinant_decimal(minor)
        return determinant

    # Convert input matrix to Decimal and calculate
    decimal_matrix = to_decimal_matrix(m)
    result = calculate_determinant_decimal(decimal_matrix)

    # Round and convert back to float
    return float(result.quantize(Decimal('0.0000000001')))


def _matrix_inverse(m):
    """
    Calculate inverse of a 2D affine transformation matrix using
    high-precision decimal arithmetic with the direct formula.
    Intermediate results are kept at full Decimal precision to avoid
    compounding quantization errors; only the final results are rounded.

    Input: 3x3 matrix [[a, b, 0], [c, d, 0], [tx, ty, 1]]
    Returns: 3x3 inverse matrix
    """
    a = Decimal(str(m[0][0]))
    b = Decimal(str(m[0][1]))
    c = Decimal(str(m[1][0]))
    d = Decimal(str(m[1][1]))
    tx = Decimal(str(m[2][0]))
    ty = Decimal(str(m[2][1]))

    det = a * d - b * c

    # Handle singular matrix case
    if abs(det) < Decimal('1e-15'):
        return None

    q = Decimal('0.0000000001')
    return [
        [float((d / det).quantize(q)), float((-b / det).quantize(q)), 0],
        [float((-c / det).quantize(q)), float((a / det).quantize(q)), 0],
        [float(((c * ty - d * tx) / det).quantize(q)),
         float(((b * tx - a * ty) / det).quantize(q)), 1],
    ]


def concat(ctxt, ostack):
    """
    matrix **concat** -


    applies the transformation represented by matrix to the user coordinate space.
    **concat** accomplishes this by concatenating matrix with the current transformation
    matrix (CTM); that is, it replaces the CTM with the matrix product matrix X CTM
    (see Section 4.3, "Coordinate Systems and Transformations").

    **Example**
        [72 0 0 72 0 0] **concat**
        72 72 **scale**

    Both lines of code above have the same effect on the user coordinate space.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **concatmatrix**, **setmatrix**, **currentmatrix**, **translate**, **scale**, **rotate**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, concat.__name__)
    # 2. TYPECHECK - Check operand type (matrix array)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, concat.__name__)

    if ostack[-1].length != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, concat.__name__)

    if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-1].val):
        return ps_error.e(ctxt, ps_error.TYPECHECK, concat.__name__)

    mat = ps.Array(ctxt.id)
    mat.setval(copy.copy(ctxt.gstate.CTM.val))
    mat.length = 6

    _matmult(ostack[-1], mat, ctxt.gstate.CTM)
    _setCTM(ctxt, ctxt.gstate.CTM.val)  # we do this so that the iCTM is also updated
    ostack.pop()


def concatmatrix(ctxt, ostack):
    """
    matrix₁ matrix₂ matrix₃ **concatmatrix** matrix₃


    replaces the value of matrix₃ with the matrix product matrix₁ X matrix₂
    and pushes the result back on the operand stack. The current transformation
    matrix is not affected.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **concat**, **setmatrix**, **currentmatrix**, **translate**, **scale**, **rotate**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, concatmatrix.__name__)
    # 2. TYPECHECK - Check operand types (matrix₁ matrix₂ matrix₃)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, concatmatrix.__name__)
    if ostack[-2].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, concatmatrix.__name__)
    if ostack[-3].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, concatmatrix.__name__)

    if ostack[-1].length != 6 or ostack[-2].length != 6 or ostack[-3].length != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, concatmatrix.__name__)

    if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-2].val):
        return ps_error.e(ctxt, ps_error.TYPECHECK, concatmatrix.__name__)

    if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-3].val):
        return ps_error.e(ctxt, ps_error.TYPECHECK, concatmatrix.__name__)

    _matmult(ostack[-3], ostack[-2], ostack[-1])
    mat = ostack[-1]
    ostack.pop()
    ostack.pop()
    ostack[-1] = mat


def currentmatrix(ctxt, ostack):
    """
    matrix **currentmatrix** matrix


    replaces the value of matrix with the current transformation matrix (CTM) in the
    graphics state and pushes this modified matrix back on the operand stack (see
    Section 4.3.2, "Transformations").

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **setmatrix**, **initmatrix**, **defaultmatrix**, **translate**, **scale**, **rotate**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, currentmatrix.__name__)
    # 2. TYPECHECK - Check operand type (matrix array)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, currentmatrix.__name__)

    if ostack[-1].length != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, identmatrix.__name__)

    # Write CTM values directly into the matrix's underlying storage.
    # This bypasses put()'s access check, which is correct because
    # currentmatrix is a system operator that must fill the operand matrix
    # regardless of its access level (e.g., matrices captured via //
    # inside bind-ed procedures are made read-only by bind).
    mat = ostack[-1]
    if not mat.is_global and hasattr(mat, '_cow_check'):
        mat._cow_check()
    start = mat.start
    for i, val in enumerate(ctxt.gstate.CTM.val):
        mat.val[start + i] = copy.copy(val)


def dtransform(ctxt, ostack):
    """
           dx dy **dtransform** dx' dy'
    dx dy matrix **dtransform** dx' dy'


    (delta **transform**) applies a transformation matrix to the distance vector (dx, dy),
    returning the transformed distance vector (dx', dy'). The first form of the operator
    uses the current transformation matrix in the graphics state to **transform** the
    distance vector from user space to device space coordinates. The second form applies
    the transformation specified by the matrix operand rather than the CTM.

    A delta transformation is similar to an ordinary transformation (see Section 4.3,
    "Coordinate Systems and Transformations"), but does not use the translation
    components tx and ty of the transformation matrix. The distance vectors are thus
    positionless in both the original and target coordinate spaces, making this operator
    useful for determining how distances map from user space to device space.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **transform**, **idtransform**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, dtransform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES and len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, dtransform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        if ostack[-2].TYPE not in ps.NUMERIC_TYPES or ostack[-3].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, dtransform.__name__)

        if ostack[-1].length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, dtransform.__name__)

        if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-1].val):
            return ps_error.e(ctxt, ps_error.TYPECHECK, dtransform.__name__)

        x, y = _transform_delta(ostack[-1], ostack[-3].val, ostack[-2].val)
        ostack.pop()
        ostack[-2] = ps.Real(float(x))
        ostack[-1] = ps.Real(float(y))
    else:
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES or ostack[-2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, dtransform.__name__)

        x, y = _transform_delta(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

        ostack[-2] = ps.Real(x)
        ostack[-1] = ps.Real(y)


def defaultmatrix(ctxt, ostack):
    """
    matrix **defaultmatrix** matrix

    replaces the value of matrix with the default transformation matrix for the current output device and pushes this modified matrix back on the operand stack.

    PLRM Section 8.2, Page 247 (Second Edition)
    Stack: matrix → matrix
    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**
    """
    
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, defaultmatrix.__name__)
    # 2. TYPECHECK - Check operand type (matrix array)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, defaultmatrix.__name__)
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, defaultmatrix.__name__)

    # STEP 2: Check matrix length (must be 6 elements)
    if ostack[-1].length != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, defaultmatrix.__name__)
    
    # STEP 3: Calculate the default matrix (same logic as initmatrix)
    pdm = ctxt.gstate.page_device

    # Null device: default matrix is identity (PLRM p.459)
    if b".NullDevice" in pdm:
        default_matrix = ostack[-1]
        for i, v in enumerate([1.0, 0.0, 0.0, 1.0, 0.0, 0.0]):
            default_matrix.put(ps.Int(i), ps.Real(v))
        return

    width = int(
        pdm[b"PageSize"].get(ps.Int(0))[1].val
        * (pdm[b"HWResolution"].get(ps.Int(0))[1].val)
        / ps.PPI
    )
    height = int(
        pdm[b"PageSize"].get(ps.Int(1))[1].val
        * (pdm[b"HWResolution"].get(ps.Int(1))[1].val)
        / ps.PPI
    )
    
    # STEP 4: Fill matrix with default transformation values
    default_matrix = ostack[-1]
    default_matrix.put(ps.Int(0), ps.Real(pdm[b"HWResolution"].get(ps.Int(0))[1].val / ps.PPI))
    default_matrix.put(ps.Int(1), ps.Real(0.0))
    default_matrix.put(ps.Int(2), ps.Real(0.0))
    default_matrix.put(ps.Int(3), ps.Real(-pdm[b"HWResolution"].get(ps.Int(1))[1].val / ps.PPI))
    default_matrix.put(ps.Int(4), ps.Real(0.0))
    default_matrix.put(ps.Int(5), ps.Real(float(height)))


def identmatrix(ctxt, ostack):
    """
    matrix **identmatrix** matrix


    replaces the value of matrix with the identity matrix

        [1 0 0 1 0 0]

    and pushes the result back on the operand stack. This matrix represents the identity
    transformation, which leaves all coordinates unchanged.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **matrix**, **initmatrix**, **defaultmatrix**, **setmatrix**, **currentmatrix**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, identmatrix.__name__)
    # 2. TYPECHECK - Check operand type (matrix array)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, identmatrix.__name__)

    if ostack[-1].length != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, identmatrix.__name__)

    for i, val in enumerate([1, 0, 0, 1, 0, 0]):
        ostack[-1].put(ps.Int(i), ps.Int(val))


def idtransform(ctxt, ostack):
    """
           dx' dy' **idtransform** dx dy
    dx' dy' matrix **idtransform** dx dy


    (inverse delta **transform**) applies the inverse of a transformation matrix to the distance
    vector (dx', dy'), returning the transformed distance vector (dx, dy). The
    first form of the operator uses the inverse of the current transformation matrix in
    the graphics state to **transform** the distance vector from device space to user space
    coordinates. The second form applies the inverse of the transformation specified
    by the matrix operand rather than that of the CTM.

    A delta transformation is similar to an ordinary transformation (see Section 4.3,
    "Coordinate Systems and Transformations"), but does not use the translation
    components tx and ty of the transformation matrix. The distance vectors are thus
    positionless in both the original and target coordinate spaces, making this operator
    useful for determining how distances map from device space to user space.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **dtransform**, **itransform**, **invertmatrix**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, idtransform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES and len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, idtransform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        if ostack[-2].TYPE not in ps.NUMERIC_TYPES or ostack[-3].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, idtransform.__name__)

        if ostack[-1].length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, idtransform.__name__)

        if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-1].val):
            return ps_error.e(ctxt, ps_error.TYPECHECK, idtransform.__name__)

        m = ostack[-1].val
        invt = _matrix_inverse(
            [[m[0].val, m[1].val, 0], [m[2].val, m[3].val, 0], [m[4].val, m[5].val, 1]]
        )

        if invt is None:
            return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, idtransform.__name__)

        imat = ps.Array(ctxt.id)
        imat.setval(
            [
                ps.Real(invt[0][0]),
                ps.Real(invt[0][1]),
                ps.Real(invt[1][0]),
                ps.Real(invt[1][1]),
                ps.Real(invt[2][0]),
                ps.Real(invt[2][1]),
            ]
        )

        x, y = _transform_delta(imat, ostack[-3].val, ostack[-2].val)
        ostack.pop()
        ostack[-2] = ps.Real(float(x))
        ostack[-1] = ps.Real(float(y))
    else:
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES or ostack[-2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, idtransform.__name__)

        x, y = _transform_delta(ctxt.gstate.iCTM, ostack[-2].val, ostack[-1].val)

        ostack[-2] = ps.Real(x)
        ostack[-1] = ps.Real(y)


def initmatrix(ctxt, ostack):
    """
    - **initmatrix** -


    sets the current transformation matrix (CTM) in the graphics state to the default
    matrix for the current output device. This matrix transforms the default user coordinate
    system to device space (see Section 4.3.1, "User Space and Device
    Space"). For a page device, the default matrix is initially established by the
    **setpagedevice** operator.

    There are few situations in which a PostScript program should invoke **initmatrix**
    explicitly. A page description that invokes **initmatrix** usually produces incorrect
    results if it is embedded within another, composite page.

    **Errors**:     none
    **See Also**:   **defaultmatrix**, **setmatrix**, **currentmatrix**
    """

    # calculate the CTM
    pdm = ctxt.gstate.page_device

    # Null device: default matrix is identity (PLRM p.459)
    if b".NullDevice" in pdm:
        _setCTM(ctxt, [ps.Real(1.0), ps.Real(0.0), ps.Real(0.0),
                        ps.Real(1.0), ps.Real(0.0), ps.Real(0.0)])
        return

    width = int(
        pdm[b"PageSize"].get(ps.Int(0))[1].val
        * (pdm[b"HWResolution"].get(ps.Int(0))[1].val)
        / ps.PPI
    )
    height = int(
        pdm[b"PageSize"].get(ps.Int(1))[1].val
        * (pdm[b"HWResolution"].get(ps.Int(1))[1].val)
        / ps.PPI
    )
    ctm = [0] * 6
    ctm[0] = ps.Real(pdm[b"HWResolution"].get(ps.Int(0))[1].val / ps.PPI)
    ctm[1] = ps.Real(0.0)
    ctm[2] = ps.Real(0.0)
    ctm[3] = ps.Real(-pdm[b"HWResolution"].get(ps.Int(1))[1].val / ps.PPI)
    ctm[4] = ps.Real(0.0)
    ctm[5] = ps.Real(float(height))

    _setCTM(ctxt, ctm)

    # store the size of the physical media in the pagedevice dictionary
    media_size = ps.Array(ctxt.id)
    media_size.setval([ps.Int(width), ps.Int(height)])
    pdm[b"MediaSize"] = media_size


def invertmatrix(ctxt, ostack):
    """
    matrix1 matrix2 **invertmatrix** matrix2


    replaces the value of matrix2 with the inverse of matrix1 and pushes the result back
    on the operand stack. If matrix1 transforms coordinates (x, y) to (x', y'), then its
    inverse transforms (x', y') to (x, y) (see Section 4.3.3, "Matrix Representation and
    Manipulation").

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **itransform**, **idtransform**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, invertmatrix.__name__)

    if ostack[-1].TYPE not in ps.ARRAY_TYPES or ostack[-2].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, invertmatrix.__name__)

    if len(ostack[-1].val) != 6 or len(ostack[-2].val) != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, invertmatrix.__name__)

    if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-2].val):
        return ps_error.e(ctxt, ps_error.TYPECHECK, invertmatrix.__name__)

    m = ostack[-2].val
    invt = _matrix_inverse(
        [[m[0].val, m[1].val, 0], [m[2].val, m[3].val, 0], [m[4].val, m[5].val, 1]]
    )

    if invt is None:
        return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, invertmatrix.__name__)

    index = 0
    for row in invt:
        ostack[-1].put(ps.Int(index), ps.Real(row[0]))
        index += 1
        ostack[-1].put(ps.Int(index), ps.Real(row[1]))
        index += 1

    ostack[-1], ostack[-2] = ostack[-2], ostack[-1]
    ostack.pop()


def itransform(ctxt, ostack):
    """
           x' y' **itransform** x y
    x' y' matrix **itransform** x y


    (inverse **transform**) applies the inverse of a transformation matrix to the coordinates
    (x', y'), returning the transformed coordinates (x, y). The first form of the
    operator uses the inverse of the current transformation matrix in the graphics
    state to **transform** device space coordinates to user space. The second form applies
    the inverse of the transformation specified by the matrix operand rather than that
    of the CTM.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**, **undefinedresult**
    **See Also**:   **transform**, **idtransform**, **invertmatrix**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, itransform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES and len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, itransform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        if ostack[-2].TYPE not in ps.NUMERIC_TYPES or ostack[-3].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, itransform.__name__)

        if ostack[-1].length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, itransform.__name__)

        if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-1].val):
            return ps_error.e(ctxt, ps_error.TYPECHECK, itransform.__name__)

        m = ostack[-1].val
        invt = _matrix_inverse(
            [[m[0].val, m[1].val, 0], [m[2].val, m[3].val, 0], [m[4].val, m[5].val, 1]]
        )

        if invt is None:
            return ps_error.e(ctxt, ps_error.UNDEFINEDRESULT, itransform.__name__)

        imat = ps.Array(ctxt.id)
        imat.setval(
            [
                ps.Real(invt[0][0]),
                ps.Real(invt[0][1]),
                ps.Real(invt[1][0]),
                ps.Real(invt[1][1]),
                ps.Real(invt[2][0]),
                ps.Real(invt[2][1]),
            ]
        )

        x, y = _transform_point(imat, ostack[-3].val, ostack[-2].val)
        ostack.pop()
        ostack[-2] = ps.Real(float(x))
        ostack[-1] = ps.Real(float(y))
    else:
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES or ostack[-2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, itransform.__name__)

        x, y = _transform_point(ctxt.gstate.iCTM, ostack[-2].val, ostack[-1].val)

        ostack[-2] = ps.Real(x)
        ostack[-1] = ps.Real(y)


def matrix(ctxt, ostack):
    """
    - **matrix** **matrix**


    returns a six-element array object filled with the identity **matrix**

        [1 0 0 1 0 0]

    This **matrix** represents the identity transformation, which leaves all coordinates
    unchanged. The array is allocated in local or global VM according to the current
    VM allocation mode (see Section 3.7.2, "Local and Global VM").

    **Example**
        **matrix**
        6 array **identmatrix**

    Both lines of code above return the same result on the stack.

    **Errors**:     **stackoverflow**, **VMerror**
    **See Also**:   **identmatrix**, **defaultmatrix**, **setmatrix**, **currentmatrix**, **array**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, matrix.__name__)

    ostack.append(ps.Array(ctxt.id))
    ostack[-1].setval(
        [ps.Int(1), ps.Int(0), ps.Int(0), ps.Int(1), ps.Int(0), ps.Int(0)]
    )


def rotate(ctxt, ostack):
    """
           angle **rotate** -
    angle matrix **rotate** matrix


    rotates the axes of the user coordinate space by angle degrees counterclockwise
    about the origin, or returns a matrix representing this transformation. The position
    of the coordinate origin and the sizes of the coordinate units are unaffected.

    The transformation is represented by the matrix

          **cos**°  **sin**°  0
    R =  -**sin**°  **cos**°  0
            0     0   1

    where ° is the angle specified by the angle operand. The first form of the operator
    applies this transformation to the user coordinate system by concatenating matrix
    R with the current transformation matrix (CTM); that is, it replaces the CTM
    with the matrix product R X CTM. The second form replaces the value of the
    matrix operand with an array representing matrix R and pushes the result back on
    the operand stack without altering the CTM. See Section 4.3.3, "Matrix Representation
    and Manipulation," for a discussion of how matrices are represented as
    arrays.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **setmatrix**, **currentmatrix**, **translate**, **scale**, **concat**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rotate.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES and len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, rotate.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, rotate.__name__)

        if ostack[-1].length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, rotate.__name__)

        mat = ostack[-1]
        for i, val in enumerate(
            [
                ps.Real(math.cos(math.radians(ostack[-2].val))),
                ps.Real(math.sin(math.radians(ostack[-2].val))),
                ps.Real(-math.sin(math.radians(ostack[-2].val))),
                ps.Real(math.cos(math.radians(ostack[-2].val))),
                ps.Real(0.0),
                ps.Real(0.0),
            ]
        ):
            mat.put(ps.Int(i), val)

        ostack.pop()
        ostack[-1] = mat
    else:
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, rotate.__name__)

        mat1 = ps.Array(ctxt.id)
        mat1.setval(
            [
                ps.Real(math.cos(math.radians(ostack[-1].val))),
                ps.Real(math.sin(math.radians(ostack[-1].val))),
                ps.Real(-math.sin(math.radians(ostack[-1].val))),
                ps.Real(math.cos(math.radians(ostack[-1].val))),
                ps.Real(0.0),
                ps.Real(0.0),
            ]
        )
        mat2 = ps.Array(ctxt.id)
        ctm = ctxt.gstate.CTM.val
        mat2.setval(
            [
                ps.Real(float(ctm[0].val)),
                ps.Real(float(ctm[1].val)),
                ps.Real(float(ctm[2].val)),
                ps.Real(float(ctm[3].val)),
                ps.Real(float(ctm[4].val)),
                ps.Real(float(ctm[5].val)),
            ]
        )
        _matmult(mat1, mat2, ctxt.gstate.CTM)
        _setCTM(ctxt, ctxt.gstate.CTM.val)

        ostack.pop()


def setmatrix(ctxt, ostack):
    """
    matrix **setmatrix** -


    sets the current transformation matrix (CTM) in the graphics state to matrix
    without reference to the former CTM. Except in device setup procedures,
    the use of this operator should be very rare. PostScript programs should
    ordinarily modify the CTM with the **translate**, **scale**, **rotate**, and **concat**
    operators rather than replace it.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **currentmatrix**, **initmatrix**, **translate**, **scale**, **rotate**, **concat**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setmatrix.__name__)

    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setmatrix.__name__)

    if ostack[-1].length != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setmatrix.__name__)

    if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-1].val):
        return ps_error.e(ctxt, ps_error.TYPECHECK, invertmatrix.__name__)

    _setCTM(ctxt, ostack[-1].val)

    ostack.pop()


def scale(ctxt, ostack):
    """
           sx sy **scale** -
    sx sy matrix **scale** matrix


    scales the units of the user coordinate space by a factor of sx units horizontally and
    sy units vertically, or returns a matrix representing this transformation. The position
    of the coordinate origin and the orientation of the axes are unaffected.

    The transformation is represented by the matrix

         sx  0  0
    S =  0  sy  0
         0   0  1

    The first form of the operator applies this transformation to the user coordinate
    system by concatenating matrix S with the current transformation matrix (CTM);
    that is, it replaces the CTM with the matrix product S X CTM. The second form
    replaces the value of the matrix operand with an array representing matrix S and
    pushes the result back on the operand stack without altering the CTM. See
    Section 4.3.3, "Matrix Representation and Manipulation," for a discussion of how
    matrices are represented as arrays.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **setmatrix**, **currentmatrix**, **translate**, **rotate**, **concat**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, scale.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES and len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, scale.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        if ostack[-2].TYPE not in ps.NUMERIC_TYPES or ostack[-3].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, scale.__name__)

        if ostack[-1].length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, scale.__name__)

        mat = ostack[-1]
        for i, val in enumerate(
            [
                ps.Real(float(ostack[-3].val)),
                ps.Real(0),
                ps.Real(0),
                ps.Real(float(ostack[-2].val)),
                ps.Real(0),
                ps.Real(0),
            ]
        ):
            mat.put(ps.Int(i), val)

        ostack.pop()
        ostack.pop()
        ostack[-1] = mat
    else:
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES or ostack[-2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, scale.__name__)

        mat1 = ps.Array(ctxt.id)
        mat1.setval(
            [
                ps.Real(float(ostack[-2].val)),
                ps.Real(0),
                ps.Real(0),
                ps.Real(float(ostack[-1].val)),
                ps.Real(0),
                ps.Real(0),
            ]
        )
        mat2 = ps.Array(ctxt.id)
        ctm = ctxt.gstate.CTM.val
        mat2.setval(
            [
                ps.Real(float(ctm[0].val)),
                ps.Real(float(ctm[1].val)),
                ps.Real(float(ctm[2].val)),
                ps.Real(float(ctm[3].val)),
                ps.Real(float(ctm[4].val)),
                ps.Real(float(ctm[5].val)),
            ]
        )
        _matmult(mat1, mat2, ctxt.gstate.CTM)
        _setCTM(ctxt, ctxt.gstate.CTM.val)

        ostack.pop()
        ostack.pop()


def transform(ctxt, ostack):
    """
           x y **transform** x' y'
    x y matrix **transform** x' y'


    applies a transformation matrix to the coordinates (x, y), returning
    the transformed coordinates (x', y'). The first form of the operator
    uses the current transformation matrix in the graphics state to **transform**
    user space coordinates to device space. The second form applies the
    transformation specified by the matrix operand rather than the CTM.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **itransform**, **dtransform**, **idtransform**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, transform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES and len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, transform.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        if ostack[-2].TYPE not in ps.NUMERIC_TYPES or ostack[-3].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, transform.__name__)

        if ostack[-1].length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, transform.__name__)

        if not all(item.TYPE in ps.NUMERIC_TYPES for item in ostack[-1].val):
            return ps_error.e(ctxt, ps_error.TYPECHECK, transform.__name__)

        x, y = _transform_point(ostack[-1], ostack[-3].val, ostack[-2].val)
        ostack.pop()
        ostack[-2] = ps.Real(float(x))
        ostack[-1] = ps.Real(float(y))
    else:
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES or ostack[-2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, transform.__name__)

        x, y = _transform_point(ctxt.gstate.CTM, ostack[-2].val, ostack[-1].val)

        ostack[-2] = ps.Real(x)
        ostack[-1] = ps.Real(y)


def translate(ctxt, ostack):
    """
           tx ty **translate** -
    tx ty matrix **translate** matrix


    moves the origin of the user coordinate space by tx units horizontally and ty units
    vertically, or returns a matrix representing this transformation. The orientation of
    the axes and the sizes of the coordinate units are unaffected.

    The transformation is represented by the matrix

          1   0   0
    T =   0   1   0
         tx  ty   1

    The first form of the operator applies this transformation to the user coordinate
    system by concatenating matrix T with the current transformation matrix (CTM);
    that is, it replaces the CTM with the matrix product T X CTM. The second form
    replaces the value of the matrix operand with an array representing matrix T and
    pushes the result back on the operand stack without altering the CTM. See
    Section 4.3.3, "Matrix Representation and Manipulation," for a discussion of how
    matrices are represented as arrays.

    **Errors**:     **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **setmatrix**, **currentmatrix**, **scale**, **rotate**, **concat**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, translate.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES and len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, translate.__name__)

    if ostack[-1].TYPE in ps.ARRAY_TYPES:
        if ostack[-2].TYPE not in ps.NUMERIC_TYPES or ostack[-3].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, translate.__name__)

        if ostack[-1].length != 6:
            return ps_error.e(ctxt, ps_error.RANGECHECK, translate.__name__)

        mat = ostack[-1]
        for i, val in enumerate(
            [
                ps.Real(1),
                ps.Real(0),
                ps.Real(0),
                ps.Real(1),
                ps.Real(float(ostack[-3].val)),
                ps.Real(float(ostack[-2].val)),
            ]
        ):
            mat.put(ps.Int(i), val)

        ostack.pop()
        ostack.pop()
        ostack[-1] = mat
    else:
        if ostack[-1].TYPE not in ps.NUMERIC_TYPES or ostack[-2].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, translate.__name__)

        mat1 = ps.Array(ctxt.id)
        mat1.setval(
            [
                ps.Real(1),
                ps.Real(0),
                ps.Real(0),
                ps.Real(1),
                ps.Real(float(ostack[-2].val)),
                ps.Real(float(ostack[-1].val)),
            ]
        )
        mat2 = ps.Array(ctxt.id)
        ctm = ctxt.gstate.CTM.val
        mat2.setval(
            [
                ps.Real(float(ctm[0].val)),
                ps.Real(float(ctm[1].val)),
                ps.Real(float(ctm[2].val)),
                ps.Real(float(ctm[3].val)),
                ps.Real(float(ctm[4].val)),
                ps.Real(float(ctm[5].val)),
            ]
        )
        _matmult(mat1, mat2, ctxt.gstate.CTM)
        _setCTM(ctxt, ctxt.gstate.CTM.val)

        ostack.pop()
        ostack.pop()
