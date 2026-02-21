# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import math

from ..operators import dict as ps_dict
from . import error as ps_error
from . import types as ps
from . import binary_token

# White-space characters (PLRM Table 3.1)
NULL = 0
TAB = 9
BACKSPACE = 8
LINE_FEED = 10
FORM_FEED = 12
RETURN = 13
SPACE = 32

# Delimeter characters
L_PAREN = 40
R_PAREN = 41
LESS_THAN = 60
GREATER_THAN = 62
L_SQR_BRACKET = 91
R_SQR_BRACKET = 93
L_CRLY_BRACKET = 123
R_CRLY_BRACKET = 125
SOLIDUS = 47
PERCENT = 37
ESCAPE = 92


# the delimiters set
delimiters = set(
    [
        L_PAREN,
        R_PAREN,
        LESS_THAN,
        GREATER_THAN,
        L_SQR_BRACKET,
        R_SQR_BRACKET,
        L_CRLY_BRACKET,
        R_CRLY_BRACKET,
        SOLIDUS,
        PERCENT,
    ]
)

# the white_space set (PLRM Table 3.1: Null, Tab, LF, FF, CR, Space)
white_space = set([NULL, SPACE, TAB, LINE_FEED, FORM_FEED, RETURN])

# the new_line set (actual newline characters only)
new_line = set([LINE_FEED, FORM_FEED, RETURN])


def handle_newline(source: ps.File, b: int, ctxt: ps.Context) -> bool:
    """
    Handle newline character and increment line_num appropriately.

    PLRM Section 3.2.2: "The combination of a carriage return followed
    immediately by a line feed is treated as one newline."

    Args:
        source: The source stream being read
        b: The current byte (should be a newline character)
        ctxt: The context object

    Returns:
        bool: True if a newline was handled, False otherwise
    """
    if b == RETURN:
        # CR - check if followed by LF (treat CR-LF as single newline)
        source.line_num += 1
        next_b = source.read(ctxt)
        if next_b is not None and next_b != LINE_FEED:
            # Not LF, put it back
            source.unread()
        # If it was LF, we consumed it as part of CR-LF (one newline)
        return True
    elif b == LINE_FEED or b == FORM_FEED:
        source.line_num += 1
        return True
    return False

# the escape dictionary
escapes = {
    ord("n"): LINE_FEED,
    ord("r"): RETURN,
    ord("t"): TAB,
    ord("b"): BACKSPACE,
    ord("f"): FORM_FEED,
    ESCAPE: ESCAPE,
    L_PAREN: L_PAREN,
    R_PAREN: R_PAREN,
}


def _decode_ascii85_group(values: list[int]) -> list[int]:
    """
    Decode ASCII85 5-tuple to binary bytes - PLRM algorithm for tokenizer.

    This function implements the same ASCII85 decoding algorithm as the
    ASCII85DecodeFilter but for use in the tokenizer where we decode
    ASCII85 strings directly into the string buffer.

    Args:
        values (list): List of 2-5 ASCII85 digit values (0-84 range)

    Returns:
        list: List of decoded bytes (1-4 bytes depending on input length)

    Raises:
        ValueError: If the group is invalid (wrong length, impossible value)
    """
    group_len = len(values)

    # PLRM: Partial final group must have at least 2 characters
    if group_len < 2 or group_len > 5:
        raise ValueError(f"Invalid ASCII85 group length: {group_len} (need 2-5)")

    # Pad to 5 values if needed (pad with 84 = 'u' - '!')
    padded_values = values[:]
    while len(padded_values) < 5:
        padded_values.append(84)

    # Convert base-85 to 32-bit integer: c1*85^4 + c2*85^3 + c3*85^2 + c4*85^1 + c5*85^0
    value = 0
    for digit in padded_values:
        if digit < 0 or digit > 84:
            raise ValueError(f"Invalid ASCII85 digit value: {digit} (must be 0-84)")
        value = value * 85 + digit

    # PLRM: Check for impossible combinations (value > 2^32-1)
    if value > 0xFFFFFFFF:
        raise ValueError(f"ASCII85 5-tuple value {value} exceeds 2^32-1 (impossible combination)")

    # Convert to 4 bytes (big-endian: most significant byte first)
    byte1 = (value >> 24) & 0xFF
    byte2 = (value >> 16) & 0xFF
    byte3 = (value >> 8) & 0xFF
    byte4 = value & 0xFF

    # Return appropriate number of bytes for partial groups
    if group_len == 2:
        return [byte1]  # 2 chars → 1 byte
    elif group_len == 3:
        return [byte1, byte2]  # 3 chars → 2 bytes
    elif group_len == 4:
        return [byte1, byte2, byte3]  # 4 chars → 3 bytes
    else:
        return [byte1, byte2, byte3, byte4]  # 5 chars → 4 bytes


def syntax_error(ctxt: ps.Context, source: ps.File, command: str) -> tuple[bool, int, str, None]:
    source.close()
    ctxt.o_stack.append(ps.Bool(False))
    ctxt.proc_count = 0

    # returns (success, error_code, command, do_exec)
    return (False, ps_error.SYNTAXERROR, command, None)


def TOKEN_FAIL(ctxt: ps.Context, stack: list[ps.PSObject]) -> tuple[bool, None, None, None]:
    # close the stream
    # NOTE: Stack may contain StringTokenizer objects (no TYPE attribute) or ps.File objects
    # Only close ps.File objects, not StringTokenizer wrappers
    if hasattr(stack[-1], 'TYPE') and stack[-1].TYPE == ps.T_FILE:
        stack[-1].close()
    # if we get here, the source is invalid - raise an error

    # pop the temp obj (created and pushed by token) off the operand stack
    # ctxt.o_stack.pop()

    # now push a boolean false onto the operand stack
    ctxt.o_stack.append(ps.Bool(False))

    # returns (success, er_name, command, do_exec)
    return (True, None, None, None)


def TOKEN_SUCCESS(ctxt: ps.Context, stack: list[ps.PSObject], do_exec: bool = True) -> tuple[bool, None, None, bool]:
    # the token is already on the stack
    # push a boolean true if stack is operand_stack

    ctxt.o_stack.append(ps.Bool(True))

    # returns (success, er_name, command, do_exec)
    return (True, None, None, do_exec)


def __token(ctxt: ps.Context, stack: list[ps.PSObject]) -> tuple[bool, int | None, str | None, bool | None]:
    """
    PostScript tokenizer - reads and parses the next token from a source stream.
    
    This is the core tokenization function that implements PostScript lexical analysis
    according to the PostScript Language Reference Manual. It handles all PostScript
    token types including numbers, names, strings, arrays, procedures, and comments.
    
    Parameters:
        ctxt (ps.Context): The PostScript execution context containing stacks, 
                          dictionaries, and VM allocation mode
        stack (list): The stack to operate on (typically ctxt.o_stack or ctxt.e_stack)
                     The last element must be a File object to read from
    
    Returns:
        tuple: (success, error_code, command, do_exec) where:
            - success (bool): True if tokenization succeeded, False on error
            - error_code (int|None): PostScript error code if success=False, else None  
            - command (str|None): Command name for error reporting, else None
            - do_exec (bool|None): Whether the token should be executed immediately
    
    Token Types Handled:
        - Integers: 123, -456, +789
        - Reals: 3.14, -2.5, 1.0e10
        - Radix numbers: 16#FF, 8#377, 2#1010  
        - Literal names: /name, /foo_bar
        - Immediate names: //name (looked up and value pushed)
        - Executable names: name, foo_bar
        - Literal strings: (hello world), (nested (parentheses))
        - Hex strings: <48656C6C6F>, <48 65 6C 6C 6F>
        - Arrays: [...] (creates mark and executable name ']')
        - Procedures: {...} (creates mark and executable name '}')
        - Dictionary marks: <<...>> (creates mark and executable name '>>')
        - Comments: % comment text (skipped until end of line)
    
    Error Conditions:
        - SYNTAXERROR: Unbalanced parentheses, brackets, or delimiters
        - UNDEFINED: Immediate name lookup fails (//name not found)
        - Source exhaustion: Returns TOKEN_FAIL (pushes false on operand stack)
    
    Side Effects:
        - Pushes parsed tokens onto ctxt.o_stack
        - Updates source line numbers for newlines
        - Manages ctxt.proc_count for deferred procedure execution
        - Stores string data in appropriate VM strings buffer (global or local)
        - May close source file on errors
    
    Implementation Details:
        - Skips whitespace and control characters < 32
        - Handles escape sequences in literal strings (\\n, \\r, \\t, \\b, \\f, \\\\, \\(, \\))
        - Supports line continuation with backslash-newline in strings
        - Validates hex string format and converts to binary
        - Tracks nested parentheses depth for literal strings
        - Uses VM allocation mode to determine global vs local storage
        - Implements PostScript's deferred execution model for procedures
    """
    source = stack[-1]
    data = bytearray()

    while True:
        strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings
        # read in the first byte,
        # skipping any white space characters
        b = source.read(ctxt)
        if b is None:
            # the source is empty so pop the temp object that we just
            # pushed onto the stack.
            return TOKEN_FAIL(ctxt, stack)

        while b in white_space or b < 32:
            if b in new_line:
                handle_newline(source, b, ctxt)
            b = source.read(ctxt)
            if b is None:
                return TOKEN_FAIL(ctxt, stack)

        # Binary token encoding (PLRM Section 3.14.1)
        if 128 <= b <= 159:
            return binary_token.parse_binary_token(ctxt, stack, source, b)

        if b == L_PAREN:
            paren = 1
            offset = len(strings)
            length = 0

            while paren:
                b = source.read(ctxt)
                if b is None:
                    return syntax_error(ctxt, source, "unbalanced (")

                if b == L_PAREN:
                    paren += 1
                    strings.append(b)
                    length += 1
                elif b == R_PAREN:
                    paren -= 1
                    if paren:
                        strings.append(b)
                        length += 1
                elif b == ESCAPE:
                    # handle the escape character
                    b = source.read(ctxt)
                    if b is None:
                        return TOKEN_FAIL(ctxt, stack)
                    if b in escapes:
                        strings.append(escapes[b])
                        length += 1
                    elif ord('0') <= b <= ord('7'):
                        # Handle octal escape sequences \nnn
                        octal_digits = [b]
                        # Read up to 2 more octal digits
                        for _ in range(2):
                            next_b = source.read(ctxt)
                            if next_b is None:
                                break
                            if ord('0') <= next_b <= ord('7'):
                                octal_digits.append(next_b)
                            else:
                                # Not an octal digit, put it back
                                source.unread()
                                break
                        
                        # Convert octal digits to byte value
                        # PLRM: "high-order overflow ignored" - mask to 8 bits
                        octal_str = ''.join(chr(d) for d in octal_digits)
                        byte_value = int(octal_str, 8) & 0xFF
                        strings.append(byte_value)
                        length += 1
                    else:
                        # PLRM: "If the character following the \ is not in the preceding
                        # list, the scanner ignores the \."
                        # If backslash is followed by newline, skip both (line continuation)
                        if b in new_line:
                            handle_newline(source, b, ctxt)
                        # For other unrecognized escapes, the backslash is ignored
                        # and the character is not added to the string
                else:
                    strings.append(b)
                    length += 1
            # push the string onto the operand stack
            ctxt.o_stack.append(
                ps.String(ctxt.id, offset, length, is_global=ctxt.vm_alloc_mode)
            )
            return TOKEN_SUCCESS(ctxt, stack)

        elif b == R_PAREN:
            return syntax_error(ctxt, source, "unbalanced )")

        elif b == SOLIDUS:
            # PLRM: "The token / (a slash followed by no regular characters)
            # is a valid literal name."
            b = source.read(ctxt)
            if b is None:
                # EOF after / - valid empty literal name
                ctxt.o_stack.append(ps.Name(data, is_global=ctxt.vm_alloc_mode))
                return TOKEN_SUCCESS(ctxt, stack)
            if b == SOLIDUS:
                immediate = True
            else:
                immediate = False
                # put the byte back
                source.unread()

            while True:
                b = source.read(ctxt)
                if b is None:
                    break
                if b in white_space or b in delimiters or 128 <= b <= 159:
                    source.unread()
                    break
                data.append(b)

            # Empty name is valid for literal names (PLRM)
            # For immediate names, we attempt to look up the empty name

            if immediate:
                # lookup the name and replace obj with the it's value
                # Per PLRM 3.12.2: "this process is a substitution and not an execution"
                # The value is pushed to operand stack, equivalent to "/name load"
                obj = ps_dict.lookup(ctxt, bytes(data))
                if obj is None:
                    ctxt.o_stack.append(ps.Bool(False))
                    return (False, ps_error.UNDEFINED, data.decode("ascii"), None)
                ctxt.o_stack.append(obj)
                return TOKEN_SUCCESS(ctxt, stack, do_exec=False)
            else:
                # This is a LITERAL name (after /) - Note: default attrib is ATTRIB_LIT (0)
                ctxt.o_stack.append(ps.Name(data, is_global=ctxt.vm_alloc_mode))
                return TOKEN_SUCCESS(ctxt, stack)

        elif b == L_SQR_BRACKET:
            data.append(b)
            ctxt.o_stack.append(ps.Mark(data))
            return TOKEN_SUCCESS(ctxt, stack)

        elif b == L_CRLY_BRACKET:
            data.append(b)
            ctxt.o_stack.append(ps.Mark(data))

            # add one to the proc_count - deferred execution
            if stack == ctxt.e_stack:
                ctxt.proc_count += 1

            return TOKEN_SUCCESS(ctxt, stack)

        elif b == R_SQR_BRACKET:
            data.append(b)
            ctxt.o_stack.append(
                ps.Name(data, attrib=ps.ATTRIB_EXEC, is_global=ctxt.vm_alloc_mode)
            )
            return TOKEN_SUCCESS(ctxt, stack)

        elif b == R_CRLY_BRACKET:
            data.append(b)
            ctxt.o_stack.append(
                ps.Name(data, attrib=ps.ATTRIB_EXEC, is_global=ctxt.vm_alloc_mode)
            )

            # subtract one from the proc_count - deferred execution
            if stack == ctxt.e_stack:
                ctxt.proc_count -= 1
                if ctxt.proc_count < 0:
                    ctxt.proc_count = 0

            return TOKEN_SUCCESS(ctxt, stack)

        elif b == LESS_THAN:
            data.append(b)
            # see if this is a dictionary mark
            b = source.read(ctxt)
            if b is None:
                return syntax_error(ctxt, source, "unbalanced <")
            if b == LESS_THAN:
                # a dictionary mark
                data.append(b)
                ctxt.o_stack.append(ps.Mark(data))
                return TOKEN_SUCCESS(ctxt, stack)
            if b == ord('~'):
                # an ASCII85 string - decode it according to PLRM Section 3.2
                offset = len(strings)
                length = 0
                ascii85_chars = []
                group_chars = []

                while True:
                    b = source.read(ctxt)
                    if b is None:
                        return syntax_error(ctxt, source, "unbalanced <~")

                    # Check for end of ASCII85 data '~>'
                    if b == ord('~'):
                        next_b = source.read(ctxt)
                        if next_b == ord('>'):
                            # End of ASCII85 string found

                            # Handle any remaining partial group (at least 2 chars needed)
                            if group_chars and len(group_chars) >= 2:
                                try:
                                    decoded_bytes = _decode_ascii85_group(group_chars)
                                    strings.extend(decoded_bytes)
                                    length += len(decoded_bytes)
                                except (ValueError, OverflowError):
                                    return syntax_error(ctxt, source, "invalid ASCII85 group")
                            elif group_chars and len(group_chars) == 1:
                                return syntax_error(ctxt, source, "invalid ASCII85 partial group")

                            # Create and push the string object
                            ctxt.o_stack.append(
                                ps.String(
                                    ctxt.id, offset, length, is_global=ctxt.vm_alloc_mode
                                )
                            )
                            return TOKEN_SUCCESS(ctxt, stack)
                        else:
                            # Single '~' without '>' - treat as invalid character and ignore
                            if next_b is not None:
                                source.unread()
                            continue

                    # Handle special 'z' case (four zero bytes)
                    if b == ord('z'):
                        # If we have a partial group, process it first
                        if group_chars:
                            try:
                                decoded_bytes = _decode_ascii85_group(group_chars)
                                strings.extend(decoded_bytes)
                                length += len(decoded_bytes)
                                group_chars = []
                            except (ValueError, OverflowError):
                                return syntax_error(ctxt, source, "invalid ASCII85 group")

                        # Add four zero bytes
                        strings.extend([0, 0, 0, 0])
                        length += 4
                        continue

                    # Check for valid ASCII85 character (!-u, 33-117)
                    if ord('!') <= b <= ord('u'):
                        group_chars.append(b - ord('!'))  # Convert to 0-84 range

                        # Process complete 5-character group
                        if len(group_chars) == 5:
                            try:
                                decoded_bytes = _decode_ascii85_group(group_chars)
                                strings.extend(decoded_bytes)
                                length += len(decoded_bytes)
                                group_chars = []
                            except (ValueError, OverflowError):
                                return syntax_error(ctxt, source, "invalid ASCII85 group")

                    # Ignore whitespace characters (space, tab, CR, LF, FF, null)
                    elif b in [ord(' '), ord('\t'), ord('\r'), ord('\n'), ord('\f'), 0]:
                        continue

                    # Any other character is invalid for ASCII85
                    else:
                        return syntax_error(ctxt, source, "invalid character in ASCII85 string")
            else:
                # This is a hex string - handle it
                # PLRM: "If a hexadecimal string contains characters outside
                # the allowed character set, a syntaxerror occurs."
                source.unread()
                offset = len(strings)
                length = 0
                hex_bytes = bytearray(2)
                byte_num = 0
                while True:
                    b = source.read(ctxt)
                    if b is None:
                        return syntax_error(ctxt, source, "unbalanced <")
                    if b in white_space:
                        continue
                    if b == GREATER_THAN:
                        if byte_num:
                            if byte_num == 1:
                                hex_bytes[1] = 48  # pad it with a '0' if necessary
                            strings.append(int(hex_bytes.decode(), 16))
                            length += 1
                        # push the string onto the operand stack
                        ctxt.o_stack.append(
                            ps.String(
                                ctxt.id, offset, length, is_global=ctxt.vm_alloc_mode
                            )
                        )
                        return TOKEN_SUCCESS(ctxt, stack)
                    # Validate hex digit (0-9, A-F, a-f)
                    if not ((48 <= b <= 57) or (65 <= b <= 70) or (97 <= b <= 102)):
                        return syntax_error(ctxt, source, "invalid character in hex string")
                    hex_bytes[byte_num] = b
                    byte_num += 1
                    if byte_num == 2:
                        strings.append(int(hex_bytes.decode(), 16))
                        length += 1
                        byte_num = 0

        elif b == GREATER_THAN:
            data.append(b)
            # see if this is a dictionary mark
            b = source.read(ctxt)
            if b is None:
                ctxt.o_stack.append(
                    ps.Name(data, attrib=ps.ATTRIB_EXEC, is_global=ctxt.vm_alloc_mode)
                )
                return TOKEN_SUCCESS(ctxt, stack)
            if b == GREATER_THAN:
                data.append(b)
                ctxt.o_stack.append(
                    ps.Name(data, attrib=ps.ATTRIB_EXEC, is_global=ctxt.vm_alloc_mode)
                )
                return TOKEN_SUCCESS(ctxt, stack)
            else:
                # Standalone '>' is a syntax error in PostScript
                source.unread()
                return syntax_error(ctxt, source, "unexpected '>' character")

        elif b == PERCENT:
            while b not in new_line:
                b = source.read(ctxt)
                if b is None:
                    return TOKEN_FAIL(ctxt, stack)
            handle_newline(source, b, ctxt)
            continue

        else:  # integer, radix number, real, or executable name
            while b not in white_space and b not in delimiters and not (128 <= b <= 159):
                data.append(b)
                b = source.read(ctxt)
                if b is None:
                    break
                if b in new_line:
                    handle_newline(source, b, ctxt)

            # check to see if the last byte read is a delimeter or binary token
            if b is not None and (b in delimiters or 128 <= b <= 159):
                source.unread()

            try:  # integer
                int_val = int(data)
                # PLRM: "If it exceeds the implementation limit for integers,
                # it is converted to a real object."
                if int_val < -2147483648 or int_val > 2147483647:
                    ctxt.o_stack.append(ps.Real(float(int_val)))
                else:
                    ctxt.o_stack.append(ps.Int(int_val))
                return TOKEN_SUCCESS(ctxt, stack)
            except ValueError:
                pass

            try:  # float
                real_val = float(data)
                # PLRM: "If it exceeds the implementation limit for real numbers,
                # a limitcheck error occurs."
                if math.isinf(real_val):
                    ctxt.o_stack.append(ps.Bool(False))
                    return (False, ps_error.LIMITCHECK, data.decode("ascii", errors="replace"), None)
                ctxt.o_stack.append(ps.Real(real_val))
                return TOKEN_SUCCESS(ctxt, stack)
            except ValueError:
                pass

            # try a radix number
            if b"#" in data:
                base_bytes, num = data.split(b"#", 1)
                # Only attempt radix parsing if base_bytes looks like a valid integer
                # Names can contain # (e.g., @#Stopped) - these should not be radix numbers
                try:
                    base = int(base_bytes)
                except ValueError:
                    # Base is not a valid integer, so this is a name containing #
                    pass  # Fall through to name handling below
                else:
                    # Base is a valid integer - this is a radix number attempt
                    # PLRM: "base is a decimal integer in the range 2 through 36"
                    if base < 2 or base > 36:
                        return syntax_error(ctxt, source, f"radix base {base} out of range")
                    try:
                        # Convert number in given base
                        value = int(num, base)
                        # PLRM: "The number is treated as an unsigned integer and is
                        # converted to an integer object having the same twos-complement
                        # binary representation. If the number exceeds the implementation
                        # limit for integers, a limitcheck error occurs."
                        if value > 0xFFFFFFFF:  # Exceeds 32-bit unsigned
                            ctxt.o_stack.append(ps.Bool(False))
                            return (False, ps_error.LIMITCHECK, data.decode("ascii", errors="replace"), None)
                        # Convert to signed 32-bit (twos-complement)
                        if value > 0x7FFFFFFF:
                            value = value - 0x100000000
                        ctxt.o_stack.append(ps.Int(value))
                        return TOKEN_SUCCESS(ctxt, stack)
                    except ValueError:
                        # Invalid digits for the base - this IS an error for radix numbers
                        return syntax_error(ctxt, source, "invalid radix number")

            # all else failed, it must be an executable name
            ctxt.o_stack.append(
                ps.Name(data, attrib=ps.ATTRIB_EXEC, is_global=ctxt.vm_alloc_mode)
            )
            return TOKEN_SUCCESS(ctxt, stack)
