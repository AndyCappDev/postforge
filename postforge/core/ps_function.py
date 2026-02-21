# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PostScript Function Evaluation Engine

Implements PostScript function types 0 (Sampled), 2 (Exponential Interpolation),
and 3 (Stitching) as defined in PLRM Section 3.9.

Used by shfill and other operators that reference PostScript function dictionaries.
"""

from . import types as ps


def evaluate_function(func: ps.PSObject | list | tuple, inputs: list[float]) -> list[float]:
    """
    Evaluate a PostScript function dictionary or array of functions.

    Args:
        func: A PostScript Dict (single function) or Array of Dicts
              (one per output component).
        inputs: List of float input values.

    Returns:
        List of float output values.
    """
    # Handle array-of-functions case: each function maps 1 input to 1 output
    if isinstance(func, (list, tuple)):
        results = []
        for f in func:
            results.extend(_evaluate_single(f, inputs))
        return results

    if hasattr(func, 'TYPE') and func.TYPE in ps.ARRAY_TYPES:
        # PostScript array of function dicts
        results = []
        for i in range(func.start, func.start + func.length):
            sub_func = func.val[i]
            results.extend(_evaluate_single(sub_func, inputs))
        return results

    return _evaluate_single(func, inputs)


def _evaluate_single(func_dict: ps.PSObject | dict, inputs: list[float]) -> list[float]:
    """Evaluate a single PostScript function dictionary."""
    d = func_dict.val if hasattr(func_dict, 'val') else func_dict

    func_type = _get_int(d, b"FunctionType")

    # Clamp inputs to Domain
    domain = _get_float_array(d, b"Domain")
    clamped_inputs = []
    for i, val in enumerate(inputs):
        lo = domain[i * 2]
        hi = domain[i * 2 + 1]
        clamped_inputs.append(max(lo, min(hi, val)))

    if func_type == 0:
        outputs = _eval_type0(d, clamped_inputs)
    elif func_type == 2:
        outputs = _eval_type2(d, clamped_inputs)
    elif func_type == 3:
        outputs = _eval_type3(d, clamped_inputs)
    else:
        # Unsupported function type - return zeros
        range_arr = _get_float_array(d, b"Range", None)
        n_out = len(range_arr) // 2 if range_arr else 1
        return [0.0] * n_out

    # Clamp outputs to Range if present
    range_arr = _get_float_array(d, b"Range", None)
    if range_arr:
        for i in range(len(outputs)):
            lo = range_arr[i * 2]
            hi = range_arr[i * 2 + 1]
            outputs[i] = max(lo, min(hi, outputs[i]))

    return outputs


def _eval_type2(d: dict, inputs: list[float]) -> list[float]:
    """
    Type 2: Exponential Interpolation.
    y_j = C0_j + x^N * (C1_j - C0_j)
    """
    x = inputs[0]
    n = _get_float(d, b"N")
    c0 = _get_float_array(d, b"C0", [0.0])
    c1 = _get_float_array(d, b"C1", [1.0])

    # x^N - handle special cases
    if n == 1.0:
        factor = x
    elif n == 0.0:
        factor = 1.0
    elif x == 0.0:
        factor = 0.0
    elif x == 1.0:
        factor = 1.0
    else:
        factor = x ** n

    return [c0[j] + factor * (c1[j] - c0[j]) for j in range(len(c0))]


def _eval_type0(d: dict, inputs: list[float]) -> list[float]:
    """
    Type 0: Sampled function with multi-dimensional linear interpolation.
    Supports m-input functions with m-dimensional grids.
    For 1-input: standard linear interpolation between 2 samples.
    For 2-input: bilinear interpolation using 4 samples.
    For m-input: multilinear interpolation using 2^m samples.
    """
    size = _get_int_array(d, b"Size")
    bps = _get_int(d, b"BitsPerSample")
    domain = _get_float_array(d, b"Domain")
    range_arr = _get_float_array(d, b"Range")
    encode = _get_float_array(d, b"Encode", None)
    decode = _get_float_array(d, b"Decode", None)

    m = len(inputs)  # number of input dimensions
    n_outputs = len(range_arr) // 2

    # Get sample data from DataSource (string or bytes)
    data_source = _get_value(d, b"DataSource")
    if hasattr(data_source, 'byte_string'):
        # PS String object — data stored in VM, accessed via byte_string()
        sample_bytes = data_source.byte_string()
    elif hasattr(data_source, 'val'):
        if isinstance(data_source.val, (bytes, bytearray)):
            sample_bytes = data_source.val
        elif isinstance(data_source.val, str):
            sample_bytes = data_source.val.encode('latin-1')
        else:
            sample_bytes = bytes(data_source.val) if data_source.val is not None else b''
    elif isinstance(data_source, (bytes, bytearray)):
        sample_bytes = data_source
    else:
        return [0.0] * n_outputs

    # Encode each input: map from Domain to [0, Size_i - 1]
    encoded_inputs = []
    for i in range(m):
        x = inputs[i]
        if encode:
            e_min, e_max = encode[i * 2], encode[i * 2 + 1]
        else:
            e_min, e_max = 0.0, float(size[i] - 1)

        d_min, d_max = domain[i * 2], domain[i * 2 + 1]
        if d_max != d_min:
            enc = e_min + ((x - d_min) / (d_max - d_min)) * (e_max - e_min)
        else:
            enc = e_min

        # Clamp to valid sample range
        enc = max(0.0, min(float(size[i] - 1), enc))
        encoded_inputs.append(enc)

    # Compute integer indices and fractional parts for each dimension
    indices_lo = []
    indices_hi = []
    fracs = []
    for i in range(m):
        i0 = int(encoded_inputs[i])
        i1 = min(i0 + 1, size[i] - 1)
        indices_lo.append(i0)
        indices_hi.append(i1)
        fracs.append(encoded_inputs[i] - i0)

    # Compute stride for each dimension (first dimension varies fastest)
    # stride[0] = 1, stride[i] = stride[i-1] * size[i-1]
    strides = [0] * m
    strides[0] = 1
    for i in range(1, m):
        strides[i] = strides[i - 1] * size[i - 1]

    # Multi-dimensional linear interpolation using 2^m corner samples
    # Iterate over all 2^m corners of the hypercube
    n_corners = 1 << m
    interpolated = [0.0] * n_outputs

    for corner in range(n_corners):
        # Compute the linear index for this corner and its weight
        linear_idx = 0
        weight = 1.0
        for dim in range(m):
            if corner & (1 << dim):
                linear_idx += indices_hi[dim] * strides[dim]
                weight *= fracs[dim]
            else:
                linear_idx += indices_lo[dim] * strides[dim]
                weight *= (1.0 - fracs[dim])

        # Read samples at this corner
        samples = _read_samples(sample_bytes, linear_idx * n_outputs, n_outputs, bps)

        # Accumulate weighted contribution
        for j in range(n_outputs):
            interpolated[j] += weight * samples[j]

    # Decode: map from [0, max_sample] to [Decode_2j, Decode_2j+1]
    max_sample = (1 << bps) - 1
    if not decode:
        decode = list(range_arr)

    outputs = []
    for j in range(n_outputs):
        dec_min = decode[j * 2]
        dec_max = decode[j * 2 + 1]
        if max_sample > 0:
            val = dec_min + (interpolated[j] / max_sample) * (dec_max - dec_min)
        else:
            val = dec_min
        outputs.append(val)

    return outputs


def _eval_type3(d: dict, inputs: list[float]) -> list[float]:
    """
    Type 3: Stitching function.
    Chains sub-functions across domain partitions.
    """
    x = inputs[0]
    functions = _get_value(d, b"Functions")
    bounds = _get_float_array(d, b"Bounds")
    encode = _get_float_array(d, b"Encode")
    domain = _get_float_array(d, b"Domain")

    # Build list of sub-function objects
    if hasattr(functions, 'TYPE') and functions.TYPE in ps.ARRAY_TYPES:
        func_list = [functions.val[i] for i in range(functions.start, functions.start + functions.length)]
    elif isinstance(functions, (list, tuple)):
        func_list = list(functions)
    else:
        return [0.0]

    n = len(func_list)

    # Find which subdomain the input falls in
    # bounds = [b0, b1, ..., b_{n-2}] where n = number of functions
    # Subdomains: [Domain[0], bounds[0]], [bounds[0], bounds[1]], ..., [bounds[-1], Domain[1]]
    k = 0
    for i in range(len(bounds)):
        if x < bounds[i]:
            break
        k = i + 1
    k = min(k, n - 1)

    # Get subdomain bounds
    if k == 0:
        sub_lo = domain[0]
    else:
        sub_lo = bounds[k - 1]

    if k == len(bounds):
        sub_hi = domain[1]
    else:
        sub_hi = bounds[k]

    # Encode input for the sub-function
    e_min = encode[k * 2]
    e_max = encode[k * 2 + 1]
    if sub_hi != sub_lo:
        encoded_x = e_min + ((x - sub_lo) / (sub_hi - sub_lo)) * (e_max - e_min)
    else:
        encoded_x = e_min

    return _evaluate_single(func_list[k], [encoded_x])


def _read_samples(data: bytes | bytearray, sample_offset: int, count: int, bps: int) -> list[float]:
    """Read `count` samples starting at sample_offset from byte data."""
    results = []
    if bps == 8:
        for i in range(count):
            idx = sample_offset + i
            if idx < len(data):
                results.append(float(data[idx]))
            else:
                results.append(0.0)
    elif bps == 16:
        for i in range(count):
            byte_idx = (sample_offset + i) * 2
            if byte_idx + 1 < len(data):
                results.append(float((data[byte_idx] << 8) | data[byte_idx + 1]))
            else:
                results.append(0.0)
    elif bps == 32:
        for i in range(count):
            byte_idx = (sample_offset + i) * 4
            if byte_idx + 3 < len(data):
                val = (data[byte_idx] << 24) | (data[byte_idx + 1] << 16) | (data[byte_idx + 2] << 8) | data[byte_idx + 3]
                results.append(float(val))
            else:
                results.append(0.0)
    elif bps <= 8:
        # Sub-byte: 1, 2, 4 bits per sample
        for i in range(count):
            bit_pos = (sample_offset + i) * bps
            byte_idx = bit_pos // 8
            bit_offset = bit_pos % 8
            if byte_idx < len(data):
                mask = (1 << bps) - 1
                shift = 8 - bit_offset - bps
                if shift >= 0:
                    results.append(float((data[byte_idx] >> shift) & mask))
                else:
                    results.append(0.0)
            else:
                results.append(0.0)
    elif bps == 12:
        for i in range(count):
            bit_pos = (sample_offset + i) * 12
            byte_idx = bit_pos // 8
            bit_offset = bit_pos % 8
            if bit_offset == 0 and byte_idx + 1 < len(data):
                results.append(float((data[byte_idx] << 4) | (data[byte_idx + 1] >> 4)))
            elif bit_offset == 4 and byte_idx + 1 < len(data):
                results.append(float(((data[byte_idx] & 0x0F) << 8) | data[byte_idx + 1]))
            else:
                results.append(0.0)
    else:
        results = [0.0] * count

    return results


# Helper functions for extracting values from PostScript dictionaries

def _get_value(d: dict, key: bytes) -> object | None:
    """Get raw value from dict (handles both PS Dict.val and plain dict)."""
    if isinstance(d, dict):
        return d.get(key)
    return None


def _get_int(d: dict, key: bytes, default: int = 0) -> int:
    """Get integer value from PS dict."""
    obj = d.get(key) if isinstance(d, dict) else None
    if obj is None:
        return default
    if hasattr(obj, 'val'):
        return int(obj.val)
    return int(obj)


def _get_float(d: dict, key: bytes, default: float = 0.0) -> float:
    """Get float value from PS dict."""
    obj = d.get(key) if isinstance(d, dict) else None
    if obj is None:
        return default
    if hasattr(obj, 'val'):
        return float(obj.val)
    return float(obj)


def _get_int_array(d: dict, key: bytes, default: list[int] | None = None) -> list[int]:
    """Get list of ints from PS array in dict."""
    obj = d.get(key) if isinstance(d, dict) else None
    if obj is None:
        return default or []
    if hasattr(obj, 'TYPE') and obj.TYPE in ps.ARRAY_TYPES:
        return [int(obj.val[i].val) for i in range(obj.start, obj.start + obj.length)]
    if isinstance(obj, (list, tuple)):
        return [int(x.val) if hasattr(x, 'val') else int(x) for x in obj]
    return default or []


def _get_float_array(d: dict, key: bytes, default: list[float] | None = None) -> list[float]:
    """Get list of floats from PS array in dict."""
    obj = d.get(key) if isinstance(d, dict) else None
    if obj is None:
        return default if default is not None else []
    if hasattr(obj, 'TYPE') and obj.TYPE in ps.ARRAY_TYPES:
        return [float(obj.val[i].val) for i in range(obj.start, obj.start + obj.length)]
    if isinstance(obj, (list, tuple)):
        return [float(x.val) if hasattr(x, 'val') else float(x) for x in obj]
    return default if default is not None else []
