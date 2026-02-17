# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy

from ..core import error as ps_error
from ..core import types as ps
from ..core import tokenizer
from . import array as ps_array


def anchorsearch(ctxt, ostack):
    """
    string seek **anchorsearch** post match true    (if found)
                             string false       (if not found)


    determines whether the string seek matches the initial substring of string (that is,
    whether string is at least as long as seek and the corresponding characters are
    equal). If it matches, **anchorsearch** splits string into two segments—match, the
    portion of string that matches seek, and post, the remainder of string—and returns
    the string objects post and match followed by the boolean value true. Otherwise, it
    returns the original string followed by false. **anchorsearch** is a special case of the
    **search** operator.

    **Examples**
        (abbc) (ab) **anchorsearch**    -> (bc) (ab) true
        (abbc) (bb) **anchorsearch**    -> (abbc) false
        (abbc) (bc) **anchorsearch**    -> (abbc) false
        (abbc) (B) **anchorsearch**     -> (abbc) false

    **Errors**:     **invalidaccess**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **search**, **token**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, anchorsearch.__name__)
    # 2. TYPECHECK - Check operand types (string seek)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, anchorsearch.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, anchorsearch.__name__)
    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, anchorsearch.__name__)

    string = ostack[-2].byte_string()
    seek = ostack[-1].byte_string()

    if string.startswith(seek):
        if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
            return ps_error.e(ctxt, ps_error.STACKOVERFLOW, anchorsearch.__name__)

        post = copy.copy(ostack[-2])
        post.start += len(seek)
        post.length -= len(seek)

        match = copy.copy(ostack[-2])
        match.length = len(seek)

        ostack[-2] = post
        ostack[-1] = match
        ostack.append(ps.Bool(True))
    else:
        ostack[-1] = ps.Bool(False)


def join(ctxt, ostack):
    """
    joinstring stringarray string **join** substring

    PostForge extension. Joins the strings in stringarray using
    joinstring as the separator, writing the result into string.
    Returns a substring of string containing the joined result.
    A **rangecheck** error occurs if the result is longer than string.

    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**
    """

    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, join.__name__)

    if (
        ostack[-1].TYPE != ps.T_STRING
        or ostack[-2].TYPE not in ps.ARRAY_TYPES
        or ostack[-3].TYPE != ps.T_STRING
    ):
        return ps_error.e(ctxt, ps_error.TYPECHECK, join.__name__)

    if not all(item.TYPE == ps.T_STRING for item in ostack[-2].val):
        return ps_error.e(ctxt, ps_error.TYPECHECK, join.__name__)


    st = (
        ostack[-3].python_string().join([item.python_string() for item in ostack[-2].val])
    )

    if len(st) > ostack[-1].length:
        return ps_error.e(ctxt, ps_error.RANGECHECK, join.__name__)

    dst = ps.global_resources.global_strings if ostack[-1].is_global else ctxt.local_strings
    ss = copy.copy(ostack[-1])
    ss.length = len(st)
    dst[ss.offset + ss.start : ss.offset + ss.start + ss.length] = bytes(st, "ascii")

    ostack.pop()
    ostack.pop()
    ostack[-1] = ss


def search(ctxt, ostack):
    """
    string seek **search** post match pre true  (if found)
                       string false         (if not found)


    looks for the first occurrence of the string seek within string and returns the results
    of this **search** on the operand stack. The topmost result is a boolean value that indicates
    whether the **search** succeeded.

    If **search** finds a subsequence of string whose elements are equal to the elements of
    seek, it splits string into three segments: pre, the portion of string preceding the
    match; match, the portion of string that matches seek; and post, the remainder of
    string. It then pushes the string objects post, match, and pre on the operand stack,
    followed by the boolean value true. All three of these strings are substrings sharing
    intervals of the value of the original string.

    If **search** does not find a match, it pushes the original string followed by false.

    **Examples**
        (abbc) (ab) **search**      -> (bc) (ab) () true
        (abbc) (bb) **search**      -> (c) (bb) (a) true
        (abbc) (bc) **search**      -> ( ) (bc) (ab) true
        (abbc) (B) **search**       -> (abbc) false

    **Errors**:     **invalidaccess**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **anchorsearch**, **token**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, search.__name__)
    # 2. TYPECHECK - Check operand types (string seek)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, search.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, search.__name__)
    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, search.__name__)

    string = ostack[-2].byte_string()
    seek = ostack[-1].byte_string()
    found = string.find(seek)

    if found != -1:
        if ctxt.MaxOpStack and len(ostack) + 2 > ctxt.MaxOpStack:
            return ps_error.e(ctxt, ps_error.STACKOVERFLOW, anchorsearch.__name__)

        pre = copy.copy(ostack[-2])
        pre.length = found
        pre.is_substring = True

        match = copy.copy(ostack[-2])
        match.start += found
        match.length = ostack[-1].length
        match.is_substring = True

        post = copy.copy(ostack[-2])
        post.start += found + len(seek)
        post.length -= found + len(seek)
        post.is_substring = True

        ostack[-2] = post
        ostack[-1] = match
        ostack.append(pre)
        ostack.append(ps.Bool(True))
    else:
        ostack[-1] = ps.Bool(False)


def ps_string(ctxt, ostack):
    """
    int **string** **string**


    creates a **string** of length int, each of whose elements is initialized with the
    integer 0, and pushes this **string** on the operand stack. The int operand must
    be a nonnegative integer not greater than the maximum allowable **string** length
    (see Appendix B). The **string** is allocated in local or global VM according to
    the current VM allocation mode; see Section 3.7.2, "Local and Global VM."

    **Errors**:     **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**, **VMerror**
    **See Also**:   **length**, **type**
    """
    op = "string"

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    if ostack[-1].val < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, op)

    strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings

    offset = len(strings)
    length = ostack[-1].val
    strings += bytearray(length)

    ostack[-1] = ps.String(ctxt.id, offset, length, is_global=ctxt.vm_alloc_mode)


def token(ctxt, ostack):
    """
      file **token** any true       (if found)
                 false          (if not found)
    string **token** post any true  (if found)
                 false          (if not found)


    reads characters from file or string, interpreting them according to the PostScript
    language syntax rules (see Section 3.2, "Syntax"), until it has scanned and constructed
    an entire object.

    In the file case, **token** normally pushes the scanned object followed by true. If
    **token** reaches end-of-file before encountering any characters besides white-space
    characters, it returns false.

    In the string case, **token** normally pushes post (the substring of string beyond the
    portion consumed by **token**), the scanned object, and true. If **token** reaches the
    end of string before encountering any characters besides white-space characters, it
    simply returns false.

    In either case, the any result is an ordinary object. It may be a simple object—an
    integer, a real number, or a name—or a composite object—a string bracketed by
    (...) or a procedure bracketed by {...}. The object returned by **token** is the same
    as the object that would be encountered by the interpreter if file or string were executed
    directly. However, **token** scans just a single object and it always pushes that
    object on the operand stack rather than executing it.

    **token** consumes all characters of the **token** and sometimes the terminating character
    as well. If the **token** is a name or a number followed by a white-space character,
    **token** consumes the white-space character (only the first one if there are
    several). If the **token** is terminated by a special character that is part of the
    **token**—), >, or }—**token** consumes that character, but no following ones. If the
    **token** is terminated by a special character that is part of the next **token**—/, (, <, [,
    or {—**token** does not consume that character, but leaves it in the input sequence.
    If the **token** is a binary **token** or a binary object sequence, **token** consumes no additional
    characters.

    **Examples**
        (15 (St1) {1 2 add}) **token**  -> ((St1) {1 2 add}) 15 true
        ((St1) {1 2 add}) **token**     -> ({1 2 add}) (St1) true
        ({1 2 add}) **token**           -> ( ) {1 2 add} true
        ( ) **token**                   -> false

    **Errors**:     **invalidaccess**, **ioerror**, **limitcheck**, **stackoverflow**, **stackunderflow**,
                **syntaxerror**, **typecheck**, **undefinedresult**, **VMerror**
    **See Also**:   **search**, **anchorsearch**, **read**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, token.__name__)

    if ostack[-1].TYPE not in ps.TOKENIZABLE_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, token.__name__)

    source = ostack[-1]
    
    if source.TYPE == ps.T_FILE:
        return token_from_file(ctxt, ostack, source)
    else:  # ps.String
        return token_from_string(ctxt, ostack, source)


def token_from_file(ctxt, ostack, source):
    """Parse complete token from file"""
    if source.access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, token.__name__)
    
    # Get first token using __token
    success, error, cmd, do_exec = tokenizer.__token(ctxt, [source])
    
    if not success:
        source.close()  # PLRM: close file on EOF
        ostack[-1] = ps.Bool(False)
        return
    
    # Remove the boolean that __token added
    boolean_result = ctxt.o_stack.pop()
    first_token = ctxt.o_stack[-1]
    
    # Check if it's a composite object start
    if (first_token.TYPE == ps.T_MARK and 
        first_token.val in [bytearray(b'['), bytearray(b'{')]):
        
        # Parse until we get the matching closing bracket
        if first_token.val == bytearray(b'['):
            error_result = parse_array_tokens(ctxt, source)
            if error_result is not None:
                return error_result
            # Use existing array_from_mark logic - this replaces mark and tokens with array
            ps_array.array_from_mark(ctxt, ctxt.o_stack, b"[")
        else:  # bytearray(b'{')  
            error_result = parse_procedure_tokens(ctxt, source)
            if error_result is not None:
                return error_result
            # Use existing procedure_from_mark logic - this replaces mark and tokens with procedure
            ps_array.procedure_from_mark(ctxt, ctxt.o_stack)
        
        # After composite object construction, the result is on ctxt.o_stack
        parsed_token = ctxt.o_stack.pop()
    else:
        # Simple token case
        parsed_token = ctxt.o_stack.pop()
    
    # Stack overflow check
    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, token.__name__)
    
    # Move parsed token from o_stack to result
    ostack[-1] = parsed_token
    ostack.append(boolean_result)  # Use the boolean from __token


def token_from_string(ctxt, ostack, source):
    """Parse complete token from string with post calculation"""
    if source.access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, token.__name__)
    
    # Create string tokenizer wrapper that tracks position
    string_tokenizer = StringTokenizer(source)
    
    # Get first token using __token
    success, error, cmd, do_exec = tokenizer.__token(ctxt, [string_tokenizer])
    
    if not success:
        ostack[-1] = ps.Bool(False)
        return
    
    # Check what __token put on the stack
    result_from_token = ctxt.o_stack.pop()
    
    # If it's False, we hit EOF - just replace string with False
    if result_from_token.TYPE == ps.T_BOOL and not result_from_token.val:
        ostack[-1] = result_from_token
        return
        
    # Otherwise, we have a real token, get the actual parsed object
    first_token = ctxt.o_stack[-1]
    boolean_result = result_from_token  # This should be True
    
    # Check if it's a composite object start
    if (first_token.TYPE == ps.T_MARK and 
        first_token.val in [bytearray(b'['), bytearray(b'{')]):
        
        # Parse until we get the matching closing bracket
        if first_token.val == bytearray(b'['):
            error_result = parse_array_tokens(ctxt, string_tokenizer)
            if error_result is not None:
                return error_result
            # Use existing array_from_mark logic - this replaces mark and tokens with array
            ps_array.array_from_mark(ctxt, ctxt.o_stack, b"[")
        else:  # bytearray(b'{')
            error_result = parse_procedure_tokens(ctxt, string_tokenizer)  
            if error_result is not None:
                return error_result
            # Use existing procedure_from_mark logic - this replaces mark and tokens with procedure
            ps_array.procedure_from_mark(ctxt, ctxt.o_stack)
        
        # After composite object construction, the result is on ctxt.o_stack
        parsed_token = ctxt.o_stack.pop()
    else:
        # Simple token case
        parsed_token = ctxt.o_stack.pop()
    
    # Calculate post string (remaining portion)
    consumed = string_tokenizer.get_consumed_length()
    post = create_post_string(source, consumed)
    
    # Stack overflow check
    if ctxt.MaxOpStack and len(ostack) + 2 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, token.__name__)
    
    # Results: post, token, true
    ostack[-1] = post
    ostack.append(parsed_token)
    ostack.append(boolean_result)  # Use the boolean from __token


def parse_array_tokens(ctxt, source):
    """Parse tokens until matching ] is found"""
    bracket_count = 1
    
    while bracket_count > 0:
        success, error, cmd, do_exec = tokenizer.__token(ctxt, [source])
        if not success:
            return ps_error.e(ctxt, ps_error.SYNTAXERROR, token.__name__)
        
        # __token puts both token and boolean on o_stack, we need both
        boolean_result = ctxt.o_stack.pop()  # Remove the boolean
        token_obj = ctxt.o_stack[-1]         # Peek at the token
        
        # Handle nested composite objects
        if token_obj.TYPE == ps.T_MARK:
            if token_obj.val == bytearray(b'['):
                # Recursively parse nested arrays - DON'T increment bracket_count
                error_result = parse_array_tokens(ctxt, source)
                if error_result is not None:
                    return error_result
                ps_array.array_from_mark(ctxt, ctxt.o_stack, b"[")
            elif token_obj.val == bytearray(b'{'):
                # Recursively parse nested procedures
                error_result = parse_procedure_tokens(ctxt, source)
                if error_result is not None:
                    return error_result
                ps_array.procedure_from_mark(ctxt, ctxt.o_stack)
        elif (token_obj.TYPE == ps.T_NAME and 
              token_obj.val == bytearray(b']') and 
              token_obj.attrib == ps.ATTRIB_EXEC):
            bracket_count -= 1
            if bracket_count == 0:
                # Remove the final ] token (array_from_mark doesn't need it)
                ctxt.o_stack.pop()
                break
    
    return None  # Success


def parse_procedure_tokens(ctxt, source):
    """Parse tokens until matching } is found"""  
    bracket_count = 1
    
    while bracket_count > 0:
        success, error, cmd, do_exec = tokenizer.__token(ctxt, [source])
        if not success:
            return ps_error.e(ctxt, ps_error.SYNTAXERROR, token.__name__)
        
        # __token puts both token and boolean on o_stack, we need both
        boolean_result = ctxt.o_stack.pop()  # Remove the boolean
        token_obj = ctxt.o_stack[-1]         # Peek at the token
        
        # Handle nested composite objects
        if token_obj.TYPE == ps.T_MARK:
            if token_obj.val == bytearray(b'{'):
                # Recursively parse nested procedures - DON'T increment bracket_count
                error_result = parse_procedure_tokens(ctxt, source)
                if error_result is not None:
                    return error_result
                ps_array.procedure_from_mark(ctxt, ctxt.o_stack)
            elif token_obj.val == bytearray(b'['):
                # Recursively parse nested arrays
                error_result = parse_array_tokens(ctxt, source)
                if error_result is not None:
                    return error_result
                ps_array.array_from_mark(ctxt, ctxt.o_stack, b"[")
        elif (token_obj.TYPE == ps.T_NAME and 
              token_obj.val == bytearray(b'}') and 
              token_obj.attrib == ps.ATTRIB_EXEC):
            bracket_count -= 1
            if bracket_count == 0:
                # Remove the final } token (procedure_from_mark doesn't need it)
                ctxt.o_stack.pop()
                break
                
    return None  # Success


class StringTokenizer:
    """String wrapper that tracks read position for token operator"""
    
    def __init__(self, string_obj):
        self.string_obj = string_obj
        self.position = 0
        self.original_position = 0
        self.line_num = 1  # Required by __token
        
    def read(self, ctxt):
        """Read next byte from string, advance position"""
        if self.position >= self.string_obj.length:
            return None  # EOF
            
        # Get the byte from the string
        if self.string_obj.is_global:
            strings = ps.global_resources.global_strings
        else:
            strings = ctxt.local_strings
            
        byte_value = strings[self.string_obj.offset + self.string_obj.start + self.position]
        self.position += 1
        return byte_value
        
    def unread(self):
        """Move position back one byte"""
        if self.position > 0:
            self.position -= 1
            
    def get_consumed_length(self):
        """Return how many bytes were consumed"""
        return self.position - self.original_position
        
    def close(self):
        """No-op for string tokenizer"""
        pass


def create_post_string(original_string, consumed_length):
    """Create substring representing remaining portion after tokenization"""
    if consumed_length >= original_string.length:
        # All consumed, return empty string
        post = copy.copy(original_string)
        post.length = 0
        post.start = original_string.start + original_string.length
        return post
    
    # Create substring for remaining portion
    post = copy.copy(original_string)
    post.start = original_string.start + consumed_length  
    post.length = original_string.length - consumed_length
    return post
