# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import glob
import os
import struct

from ..core import error as ps_error
from ..core import types as ps
from ..core.binary_token import _SYSTEM_NAME_TABLE
from ..core.tokenizer import FORM_FEED, LINE_FEED, RETURN


def _resolve_filename(ctxt, filename):
    """Resolve a relative filename against likely directories.

    Tries multiple locations in order:

    1. As-is (works for absolute paths and files relative to CWD)
    2. Relative to the package directory (resolves resources/Init/... etc.)
    3. Relative to the directory of the currently executing file (e_stack walk)
    4. Relative to the user's original working directory (safety net)

    Args:
        ctxt: PostScript execution context.
        filename: The filename string from PostScript code.

    Returns:
        Resolved filename path (may be the original if nothing matched).
    """
    # Absolute paths or already-resolvable files need no fixup
    if os.path.isabs(filename) or os.path.exists(filename):
        return filename

    # Try relative to the package directory (where postforge/ lives)
    package_dir = ctxt.system_params.get("PackageDir")
    if package_dir:
        candidate = os.path.join(package_dir, filename)
        if os.path.exists(candidate):
            return candidate

    # Try relative to the directory of the currently executing file
    for item in reversed(ctxt.e_stack):
        if isinstance(item, (ps.File, ps.Run)):
            if hasattr(item, 'name') and item.name and item.is_real_file:
                parent_dir = os.path.dirname(os.path.abspath(item.name))
                candidate = os.path.join(parent_dir, filename)
                if os.path.exists(candidate):
                    return candidate
                break  # only check the innermost real file

    # Try relative to user's original CWD
    user_cwd = getattr(ctxt, 'user_cwd', None)
    if user_cwd:
        candidate = os.path.join(user_cwd, filename)
        if os.path.exists(candidate):
            return candidate

    # Return original — let the caller handle the error
    return filename


def closefile(ctxt, ostack):
    """
    file **closefile** -


    closes file, breaking the association between the file object and the underlying file
    (see Section 3.8, "File Input and Output"). For an output file, **closefile** first performs
    a **flushfile** operation. It may also take device-dependent actions, such as
    truncating a disk file to the current position or transmitting an end-of-file indication.
    Executing **closefile** on a file that has already been closed has no effect; it does
    not cause an error.

    **Errors**:     **ioerror**, **stackunderflow**, **typecheck**
    **See Also**:   **file**, **filter**, **status**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, closefile.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_FILE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, closefile.__name__)

    file_obj = ostack[-1]
    
    # PLRM: "For an output file, closefile first performs a flushfile operation"
    if hasattr(file_obj, 'mode') and file_obj.mode and 'w' in file_obj.mode:
        try:
            file_obj.flush()
        except Exception:
            pass  # Ignore flush errors during close
    
    file_obj.close()
    ostack.pop()


def currentfile(ctxt, ostack):
    """
    - **currentfile** file


    returns the file object from which the PostScript interpreter is currently or was
    most recently reading program input—that is, the topmost file object on the execution
    stack. The returned file has the literal attribute.

    If there is no file object on the execution stack, **currentfile** returns an invalid file
    object that does not correspond to any file. This never occurs during execution of
    ordinary user programs.

    The file returned by **currentfile** is usually but not always the standard input file.
    An important exception occurs during interactive mode operation (see Section
    3.8.3, "Special Files"). In this case, the interpreter does not read directly from the
    standard input file; instead, it reads from a file representing an edited statement
    (each statement is represented by a different file).

    The **currentfile** operator is useful for obtaining images or other data residing in
    the program file itself (see the example below). At any given time, this file is positioned
    at the end of the last PostScript token read from the file by the interpreter.
    If that token was a number or a name immediately followed by a white-space
    character, the file is positioned after the white-space character (the first, if there
    are several); otherwise, it is positioned after the last character of the token.

    **Example**
        /str 100 string def
        **currentfile** str **readline**
        here is a line of text
        pop /textline exch def

    After execution of this example, the name textline is associated with the string
    here is a line of text.

    **Errors**:     **stackoverflow**
    **See Also**:   **exec**, **run**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, closefile.__name__)

    for i in range(len(ctxt.e_stack) - 1, -1, -1):
        if isinstance(ctxt.e_stack[i], (ps.File, ps.Run)):
            ostack.append(ctxt.e_stack[i])
            break


def ps_file(ctxt, ostack):
    """
    filename access **file** **file**


    creates a **file** object for the **file** identified by filename, accessing it as specified by
    access. Both operands are strings. Conventions for **file** names and access specifications
    depend on the operating system environment in which the PostScript interpreter
    is running. See Section 3.8.2, "Named Files."

    Once created and opened, the returned **file** object remains valid until the **file** is
    closed either explicitly (by invoking **closefile**) or implicitly (by encountering endof-
    **file** while reading or executing the **file**). A **file** is also closed by **restore** if the **file**
    object was created more recently than the **save** snapshot being restored, or is
    closed by garbage collection if the **file** object is no longer accessible. There is a limit
    on the number of files that can be open simultaneously; see Appendix B.

    If filename is malformed, or if the **file** does not exist and access does not permit
    creating a new **file**, an **undefinedfilename** error occurs. If access is malformed or
    the requested access is not permitted by the device, an **invalidfileaccess** error occurs.
    If the number of files opened by the current context exceeds an implementation
    limit, a **limitcheck** error occurs. If an environment-dependent error is
    detected, an **ioerror** occurs.

    **Examples**
        (%stdin) (r) **file**   -> % Standard input **file** object
        (myfile) (w) **file**   -> % Output **file** object, writing to named **file**

    **Errors**:     **invalidfileaccess**, **ioerror**, **limitcheck**, **stackunderflow**,
                **typecheck**, **undefinedfilename**
    **See Also**:   **closefile**, **currentfile**, **filter**, **status**
    """
    op = "file"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)
    
    # 2. TYPECHECK - Check operand types (filename access)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    
    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)

    filename = ostack[-2].python_string()
    access = ostack[-1].python_string()
    
    # Validate access string per PLRM Table 3.5
    # Valid access strings: r, w, a, r+, w+, a+ (optionally followed by OS-specific chars)
    if not access or access[0] not in 'rwa':
        return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, op)
    
    # Check for valid combinations with '+'
    if len(access) > 1 and access[1] == '+':
        if access[0] not in 'rwa':
            return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, op)
    
    # Handle special file names per PLRM Section 3.8.3
    if filename == "%stdin":
        if access != "r":
            return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, op)
        # Return the persistent stdin file object (make it literal)
        file_obj = ctxt.stdin_file
        file_obj.attrib = ps.ATTRIB_LIT  # Ensure it's literal
        
    elif filename == "%stdout":
        if access != "w":
            return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, op)
        # Return the persistent stdout file object (make it literal)
        file_obj = ctxt.stdout_file
        file_obj.attrib = ps.ATTRIB_LIT  # Ensure it's literal
        
    elif filename == "%stderr":
        if access != "w":
            return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, op)
        # Return the shared stderr file object (make it literal)
        file_obj = ps.global_resources.get_stderr_file()
        file_obj.attrib = ps.ATTRIB_LIT  # Ensure it's literal
        
    elif filename == "%lineedit":
        if access != "r":
            return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, op)
        # Create a temporary file for single line input (similar to %statementedit)
        file_obj = ps.File(
            ctxt.id,
            filename,
            access,
            attrib=ps.ATTRIB_LIT,
            is_global=True,  # Line edit files are global like %statementedit
        )
        err = file_obj.open()
        if err is not None:
            return ps_error.e(ctxt, err, op)
        
    else:
        # Regular file - resolve relative paths against user's CWD
        resolved = _resolve_filename(ctxt, filename)
        file_obj = ps.File(
            ctxt.id,
            resolved,
            access,
            attrib=ps.ATTRIB_LIT,
            is_global=ctxt.vm_alloc_mode,
        )
        err = file_obj.open()
        if err is not None:
            return ps_error.e(ctxt, err, op)

    ostack.pop()
    ostack[-1] = file_obj


def filename(ctxt, ostack):
    """
    file **filename** name


    returns the filname of file.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **closefile**, **currentfile**, **file**, **filter**, **status**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, filename.__name__)

    # Check if the object has a filename() method (File, Run, StandardFileProxy, FilterFile)
    if not hasattr(ostack[-1], 'filename') or not callable(getattr(ostack[-1], 'filename')):
        return ps_error.e(ctxt, ps_error.TYPECHECK, filename.__name__)

    # Get the file name using the filename() method
    file_obj = ostack[-1]
    name = file_obj.filename()  # Use filename() method instead of .name attribute
    
    # Convert name to bytes if it's a string, or use as-is if already bytes
    if isinstance(name, str):
        name_bytes = bytes(name, "ascii")
    elif isinstance(name, bytes):
        name_bytes = name
    else:
        # Handle None or other types by converting to string first
        name_str = str(name) if name is not None else "<no name>"
        name_bytes = bytes(name_str, "ascii")
    
    ostack[-1] = ps.Name(name_bytes)


def filenameforall(ctxt, ostack):
    """
    template proc scratch **filenameforall** -


    enumerates all files whose names match the specified template string. For each
    matching file, **filenameforall** copies the file’s name into the supplied scratch string,
    pushes a string object designating the substring of scratch actually used, and calls
    proc. **filenameforall** does not return any results of its own, but proc may do so.

    The details of template matching are device-dependent, but the typical convention
    is that all characters in template are case-sensitive and are treated literally,
    with the exception of the following:

        *   Matches zero or more consecutive characters.

        ?   Matches exactly one character.

        \\   Causes the next character of the template to be treated literally, even if it is
            *, ?, or \\. Note that \\ is treated as an escape character in a string literal.
            Thus, if template is a string literal, \\\\ must be used to represent \\ in the
            resulting string.

    If template does not begin with %, it is matched against device-relative file names
    of all devices in the search order (see Section 3.8.2, "Named Files"). When a
    match occurs, the file name passed to proc is likewise device-relative—in other
    words, it does not have a %device% prefix.

    If template does begin with %, it is matched against complete file names in the
    form %device%file. Template matching can be performed on device, file, or both
    parts of the name. When a match occurs, the file name passed to proc is likewise in
    the complete form %device%file.

    The order of enumeration is unspecified and device-dependent. There are no restrictions
    on what proc can do. However, if proc causes new files to be created, it is
    unspecified whether those files will be encountered later in the same enumeration.
    Likewise, the set of file names considered for template matching is devicedependent.

    **Errors**:     **invalidaccess**, **ioerror**, **rangecheck**, **stackoverflow**,
                **stackunderflow**, **typecheck**
    **See also**:   file, status
    """

    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, filenameforall.__name__)

    if not isinstance(ostack[-1], ps.String) or not isinstance(ostack[-3], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, filenameforall.__name__)

    if ostack[-2].TYPE not in ps.ARRAY_TYPES or ostack[-2].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, filenameforall.__name__)

    filename_forall_loop = ps.Loop(ps.LT_FILENAMEFORALL)
    filename_forall_loop.proc = ctxt.o_stack[-2]
    # the list of files
    filename_forall_loop.obj = glob.glob(ostack[-3].python_string())
    # create a generator for looping through the dictionary
    filename_forall_loop.generator = (
        bytes(fname, "ascii") for fname in filename_forall_loop.obj
    )
    # the scratch string
    filename_forall_loop.scratch = ostack[-1]

    # push the filename_forall_loop onto the execution stack
    ctxt.e_stack.append(filename_forall_loop)

    ctxt.o_stack.pop()
    ctxt.o_stack.pop()
    ctxt.o_stack.pop()


def fileposition(ctxt, ostack):
    """
    file **fileposition** position


    returns the current position in an existing open file. The result is a nonnegative
    integer interpreted as number of bytes from the beginning of the file. If the file
    object is not valid or the underlying file is not positionable, an ioerror occurs.

    **Errors**:     **ioerror**, **stackunderflow**, **typecheck**
    **See also**:   **setfileposition**, **file**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, fileposition.__name__)

    if not isinstance(ostack[-1], (ps.File, ps.Run)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, fileposition.__name__)

    file_obj = ostack[-1]

    # Check if it's a FilterFile (has 'filter' attribute)
    if hasattr(file_obj, 'filter'):
        # Try to get position from the filter itself
        filter_obj = file_obj.filter
        # SubFileDecodeFilter tracks byte_count in byte-count mode
        if hasattr(filter_obj, 'byte_count'):
            ostack[-1] = ps.Int(filter_obj.byte_count)
            return
        # Other filters may track position differently
        if hasattr(filter_obj, 'position'):
            ostack[-1] = ps.Int(filter_obj.position)
            return
        # PLRM: "If the file object is not valid or the underlying file is not
        # positionable, an ioerror occurs"
        return ps_error.e(ctxt, ps_error.IOERROR, fileposition.__name__)

    # Check for valid file handle
    if file_obj.val is None:
        return ps_error.e(ctxt, ps_error.IOERROR, fileposition.__name__)

    ostack[-1] = ps.Int(file_obj.val.tell())


def flush(ctxt, ostack):
    """
    - **flush** -


    causes any buffered characters for the standard output file to be delivered immediately.
    In general, a program requiring output to be sent immediately, such as
    during real-time, two-way interactions, should call **flush** after generating that output.

    **Errors**:     **ioerror**
    **See Also**:   **flushfile**, **print**
    """

    # Flush the standard output file
    try:
        ctxt.stdout_file.flush()
        # Also flush sys.stdout directly to ensure immediate delivery
        import sys
        sys.stdout.flush()
    except Exception as e:
        return ps_error.e(ctxt, ps_error.IOERROR, flush.__name__)


def flushfile(ctxt, ostack):
    """
    file **flushfile** -


    If file is an output file, **flushfile** causes any buffered characters for that file to be delivered
    immediately. In general, a program requiring output to be sent immediately,
    such as during real-time, two-way interactions, should call **flushfile** after
    generating that output.

    If file is an input file, **flushfile** reads and discards data from that file until the endof-
    file indication is encountered. This is useful during error recovery, and the
    PostScript job server uses it for that purpose. **flushfile** does not close the file, unless
    it is a decoding **filter** file.

    **Errors**:     **ioerror**, **stackunderflow**, **typecheck**
    **See Also**:   **flush**, **read**, **write**
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, flushfile.__name__)

    if not isinstance(ostack[-1], (ps.File, ps.StandardFileProxy)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, flushfile.__name__)

    file_obj = ostack[-1]
    if isinstance(file_obj, ps.StandardFileProxy):
        # Delegate to actual StandardFile
        actual_file = file_obj.get_standard_file()
        if actual_file:
            file_obj = actual_file

    if file_obj.mode in {"r", "rb"}:
        # For input files, read and discard data until EOF
        b = file_obj.read(ctxt)
        while b:
            b = file_obj.read(ctxt)
    else:
        # For output files, flush buffered characters
        file_obj.flush()
    
    ostack.pop()


def line(ctxt, ostack):
    """
    file   **line** linenumber
    string **line** linenumber


    returns the current line number of the file or string. A count of newline (\\n)
    characters is kept and returned by **line**. The accuracy of the result of calling
    **line** is not guaranteed, as what actually consitutes a newline varies by platform.

    **Errors**:     **ioerror**, **typecheck**
    **See Also**:   read, write
    """

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, line.__name__)

    if not isinstance(ostack[-1], ps.Stream):
        return ps_error.e(ctxt, ps_error.TYPECHECK, line.__name__)

    ostack[-1] = ps.Int(ostack[-1].line_num)


def ps_print(ctxt, ostack):
    """
    string **print** -


    writes the characters of string to the standard output file (see Section 3.8, "File Input
    and Output"). This operator provides the simplest means of sending text to
    an application or an interactive user. Note that **print** is a file operator; it has nothing
    to do with painting glyphs for characters on the current page (see **show**) or
    with sending the current page to a raster output device (see **showpage**).

    **Errors**:     **invalidaccess**, **ioerror**, **stackunderflow**, **typecheck**
    **See Also**:   **write**, **flush**, =, ==, **printobject**
    """
    op = "print"

    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    if not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)

    # Write to PostScript %stdout file instead of Python stdout
    string_obj = ostack.pop()
    string_bytes = string_obj.python_string().encode('utf-8')
    for byte_val in string_bytes:
        ctxt.stdout_file.write(byte_val, ctxt)
    # flush it
    ctxt.stdout_file.flush()


# custom operator
def printarray(ctxt, ostack):
    """
    array **printarray** –

    PostForge extension. Prints the contents of array to stdout in
    PostScript notation. The array is consumed from the operand stack.

    **Errors**: **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, printarray.__name__)

    if not isinstance(ostack[-1], ps.Array):
        return ps_error.e(ctxt, ps_error.TYPECHECK, printarray.__name__)

    print(ostack[-1], end="")
    ostack.pop()


def read(ctxt, ostack):
    """
    file **read** int true  (if not end-of-file)
              false     (if end-of-file)


    reads the next character from the input file file, pushes it on the operand stack as
    an integer, and pushes true as an indication of success. If an end-of-file indication
    is encountered before a character has been **read**, **read** returns false. If some other
    error indication is encountered (for example, a parity or checksum error), an
    **ioerror** occurs.

    **Errors**:     **invalidaccess**, **ioerror**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **readhexstring**, **readline**, **readstring**, **bytesavailable**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, read.__name__)

    if not isinstance(ostack[-1], ps.File):
        return ps_error.e(ctxt, ps_error.TYPECHECK, read.__name__)

    # Check if file has read access
    file_obj = ostack[-1]
    if file_obj.access() not in [ps.ACCESS_READ_ONLY, ps.ACCESS_UNLIMITED]:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, read.__name__)

    b = ostack[-1].read(ctxt)
    if b is None:
        # close the file
        ostack[-1].close()
        ostack[-1] = ps.Bool(False)
    else:
        ostack[-1] = ps.Int(b)
        ostack.append(ps.Bool(True))


def readstring(ctxt, ostack):
    """
    file string **readstring** substring bool

    reads characters from file and stores them into successive elements of string until 
    either the entire string has been filled or an end-of-file indication is encountered 
    in file. **readstring** then returns the substring of string that was filled and a boolean 
    indicating the outcome (true normally, false if end-of-file was encountered before 
    the string was filled).

    All character codes are treated the same—as integers in the range 0 to 255. There 
    are no special characters (in particular, the newline character is not treated specially). 
    However, the communication channel may usurp certain control characters.

    PLRM Section 8.2, Page 651 (Second Edition)
    **Errors**: **invalidaccess**, **ioerror**, **rangecheck**, **stackunderflow**, **typecheck**
    """
    
    # Validate stack depth and operand types
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, readstring.__name__)
    
    if not isinstance(ostack[-2], ps.File):
        return ps_error.e(ctxt, ps_error.TYPECHECK, readstring.__name__)
        
    if not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, readstring.__name__)
    
    # Check file has read access (>= ACCESS_READ_ONLY)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, readstring.__name__)
    
    # Check string has write access (>= ACCESS_WRITE_ONLY) for storing results
    if ostack[-1].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, readstring.__name__)

    # Pop operands
    target_string = ostack.pop()
    file_obj = ostack.pop()

    try:
        # Get string buffer to write into
        dst = ps.global_resources.global_strings if target_string.is_global else ctxt.local_strings
        
        # Read characters from file into string buffer
        chars_to_read = target_string.length
        chars_read = 0
        eof_encountered = False
        
        for i in range(chars_to_read):
            char_code = file_obj.read(ctxt)
            if char_code is None:  # EOF
                eof_encountered = True
                break
            # Write directly to string buffer
            dst[target_string.offset + target_string.start + i] = char_code
            chars_read += 1
        
        # Create substring representing what was actually read
        result_substring = copy.copy(target_string)
        result_substring.length = chars_read
        
        # Check for stack overflow
        if ctxt.MaxOpStack and len(ostack) + 2 > ctxt.MaxOpStack:
            return ps_error.e(ctxt, ps_error.STACKOVERFLOW, readstring.__name__)
        
        # Push results: substring and success boolean
        # PLRM: true normally (string completely filled), false if EOF encountered before filling
        success = (chars_read == chars_to_read)
        ostack.append(result_substring)
        ostack.append(ps.Bool(success))

    except Exception:
        return ps_error.e(ctxt, ps_error.IOERROR, readstring.__name__)


def readhexstring(ctxt, ostack):
    """
    file string **readhexstring** substring bool

    reads characters from file, expecting to encounter a sequence of hexadecimal 
    digits 0 through 9 and A through F (or a through f). **readhexstring** interprets each 
    successive pair of digits as a two-digit hexadecimal number representing an integer 
    value in the range 0 to 255. It then stores these values into successive elements 
    of string starting at index 0 until either the entire string has been filled or an 
    end-of-file indication is encountered in file. Finally, **readhexstring** returns the 
    substring of string that was filled and a boolean indicating the outcome (true normally, 
    false if end-of-file was encountered before the string was filled).

    **readhexstring** ignores any characters that are not valid hexadecimal digits, so the 
    data in file may be interspersed with spaces, newlines, etc., without changing the 
    interpretation of the data.

    PLRM Section 8.2, Page 645 (Second Edition)
    **Errors**: **invalidaccess**, **ioerror**, **rangecheck**, **stackunderflow**, **typecheck**
    """
    
    # Validate stack depth and operand types
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, readhexstring.__name__)
    
    if not isinstance(ostack[-2], ps.File):
        return ps_error.e(ctxt, ps_error.TYPECHECK, readhexstring.__name__)
        
    if not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, readhexstring.__name__)
    
    # Check file has read access (>= ACCESS_READ_ONLY)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, readhexstring.__name__)
    
    # Check string has write access (>= ACCESS_WRITE_ONLY) for storing results
    if ostack[-1].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, readhexstring.__name__)
    
    # Pop operands
    target_string = ostack.pop()
    file_obj = ostack.pop()
    
    try:
        # Get string buffer to write into
        dst = ps.global_resources.global_strings if target_string.is_global else ctxt.local_strings
        
        # Read hex characters and convert to binary
        bytes_to_read = target_string.length
        bytes_read = 0
        eof_encountered = False
        hex_digits = "0123456789ABCDEFabcdef"
        
        for i in range(bytes_to_read):
            # Read two hex digits to form one byte
            hex_chars = []
            
            # Read hex digits (ignoring non-hex characters)
            while len(hex_chars) < 2 and not eof_encountered:
                char_code = file_obj.read(ctxt)
                if char_code is None:  # EOF
                    eof_encountered = True
                    break
                
                char = chr(char_code)
                if char in hex_digits:
                    hex_chars.append(char)
                # Ignore non-hex characters (spaces, newlines, etc.)
            
            if len(hex_chars) == 2:
                # Convert two hex digits to byte value
                hex_string = ''.join(hex_chars)
                byte_value = int(hex_string, 16)
                # Write directly to string buffer
                dst[target_string.offset + target_string.start + i] = byte_value
                bytes_read += 1
            elif len(hex_chars) == 1:
                # Odd number of hex digits at EOF - treat as if followed by '0'
                hex_string = hex_chars[0] + '0'
                byte_value = int(hex_string, 16)
                dst[target_string.offset + target_string.start + i] = byte_value
                bytes_read += 1
                eof_encountered = True
                break
            else:
                # No hex digits found
                eof_encountered = True
                break
        
        # Create substring representing what was actually read
        result_substring = copy.copy(target_string)
        result_substring.length = bytes_read
        
        # Check for stack overflow
        if ctxt.MaxOpStack and len(ostack) + 2 > ctxt.MaxOpStack:
            return ps_error.e(ctxt, ps_error.STACKOVERFLOW, readhexstring.__name__)
        
        # Push results: substring and success boolean
        # PLRM: true normally (string completely filled), false if EOF encountered before filling
        success = (bytes_read == bytes_to_read)
        ostack.append(result_substring)
        ostack.append(ps.Bool(success))
        
    except Exception as e:
        return ps_error.e(ctxt, ps_error.IOERROR, readhexstring.__name__)


def readline(ctxt, ostack):
    """
    file string **readline** substring bool


    reads a line of characters (terminated by a newline character) from file and stores
    them into successive elements of string. **readline** returns the substring of string
    that was filled and a boolean value indicating the outcome (true normally, false if
    end-of-file was encountered before a newline character was read).

    A line of characters is a sequence of ASCII characters, including space, tab, and
    control characters, that terminates with a newline—a carriage return character, a
    line feed character, or both. See Sections 3.2, "Syntax," and 3.8, "File Input and
    Output."

    The terminating newline character is not stored into string or included at the end
    of the returned substring. If **readline** completely fills string before encountering a
    newline character, a **rangecheck** error occurs.

    **Errors**:     **invalidaccess**, **ioerror**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **read**, **readhexstring**, **readonly**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, read.__name__)

    if not isinstance(ostack[-2], ps.File) or not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, read.__name__)

    the_file = ostack[-2]
    the_string = ostack[-1]
    new_string = copy.copy(the_string)
    new_string.length = 0

    # Get string buffer to write into
    dst = ps.global_resources.global_strings if new_string.is_global else ctxt.local_strings

    index = new_string.offset + new_string.start
    newline_read = False
    while True:
        b = the_file.read(ctxt)
        if b is None:
            the_file.close()
            break
        if b in [LINE_FEED, RETURN, FORM_FEED]:
            newline_read = True
            while b in [LINE_FEED, RETURN, FORM_FEED]:
                b = the_file.read(ctxt)
                if b is None:
                    the_file.close()
                    break
            if b is not None:
                the_file.unread()
            else:
                the_file.close()
            break
        else:
            new_string.length += 1
            if new_string.length > the_string.length:
                return ps_error.e(ctxt, ps_error.RANGECHECK, readline.__name__)
            dst[index] = b
            index += 1

    ostack[-2] = new_string

    if newline_read:
        ostack[-1] = ps.Bool(True)
    else:
        ostack[-1] = ps.Bool(False)


def run(ctxt, ostack):
    """
    filename **run** -


    executes the contents of the specified file—in other words, interprets the characters
    in that file as a PostScript program. When **run** encounters end-of-file or terminates
    for some other reason (for example, execution of the **stop** operator), it
    closes the file.

    **run** is essentially a convenience operator for the sequence

        (r) file **cvx** exec

    except for its behavior upon abnormal termination. Also, the context of a **run** operator
    cannot be left by executing exit; an attempt to do so produces the error
    invalidexit. The **run** operator leaves no results on the operand stack, but the program
    executed by **run** may alter the stacks arbitrarily.

    **Errors**:     **ioerror**, **limitcheck**, **stackunderflow**, **typecheck**, **undefinedfilename**
    **See Also**:   **exec**, **file**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, run.__name__)

    if not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, run.__name__)

    resolved = _resolve_filename(ctxt, ostack[-1].python_string())
    f = ps.Run(
        ctxt.id,
        resolved,
        "r",
        attrib=ps.ATTRIB_EXEC,
        is_global=ctxt.vm_alloc_mode,
    )
    err = f.open()
    if err is not None:
        return ps_error.e(ctxt, err, run.__name__)

    ctxt.e_stack.append(f)
    ctxt.o_stack.pop()


def runlibfile(ctxt, ostack):
    """
    filename **runlibfile** -

    Searches for filename in a library path and executes it. This is a
    Ghostscript extension that searches for the file in:
    1. The current working directory
    2. The directory containing the currently executing file

    If the file is found, it is executed like the run operator.
    If the file is not found, an undefinedfilename error occurs.

    **Errors**:     **ioerror**, **limitcheck**, **stackunderflow**, **typecheck**, **undefinedfilename**
    **See Also**:   **run**, **file**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, runlibfile.__name__)

    if not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, runlibfile.__name__)

    resolved_path = _resolve_filename(ctxt, ostack[-1].python_string())

    # Create and open the file for execution
    f = ps.Run(
        ctxt.id,
        resolved_path,
        "r",
        attrib=ps.ATTRIB_EXEC,
        is_global=ctxt.vm_alloc_mode,
    )
    err = f.open()
    if err is not None:
        return ps_error.e(ctxt, err, runlibfile.__name__)

    ctxt.e_stack.append(f)
    ctxt.o_stack.pop()


def status(ctxt, ostack):
    """
        file **status** bool
    filename **status** pages bytes referenced created true     (if found)
                    false                                   (if not found)


    If the operand is a file object, **status** returns true if it is still valid (that is,
    is associated with an open file), or false otherwise.

    If the operand is a string, **status** treats it as a file name according to the conventions
    described in Section 3.8.2, "Named Files". If there is a file by that name,
    **status** pushes four integers of **status** information followed by the value true;
    otherwise, it pushes false. The four integer values are:

        pages           The storage space occupied by the file, in implementation
                        dependent units.

        bytes           The length of the file in bytes.

        referenced      The date and time the file was last referenced for reading or writing.
                        This value is interpreted according to the conventions of the
                        underlying operating system. The only assumption that a program
                        can make is that larger values indicate later times.

        created         The date and time the contents of the file were created.

    **Errors**:     **invalidaccess**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **file**, **closefile**, **filenameforall**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, status.__name__)

    if not isinstance(ostack[-1], (ps.File, ps.String, ps.StandardFileProxy)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, status.__name__)

    if isinstance(ostack[-1], (ps.File, ps.StandardFileProxy)):
        # Handle both File and StandardFileProxy objects
        if isinstance(ostack[-1], ps.StandardFileProxy):
            # StandardFileProxy objects are always valid (delegate to actual file)
            actual_file = ostack[-1].get_standard_file()
            if actual_file and hasattr(actual_file, 'val') and hasattr(actual_file.val, 'closed'):
                ostack[-1] = ps.Bool(not actual_file.val.closed)
            else:
                ostack[-1] = ps.Bool(True)  # Standard files are always open
        else:
            # Regular File objects
            file_obj = ostack[-1]
            # Check if it's a FilterFile (has 'filter' attribute) vs regular file
            if hasattr(file_obj, 'filter'):
                # FilterFile - check if filter has been explicitly closed
                # FilterFile doesn't have a Python file object in 'val', so we check
                # the 'closed' attribute that FilterFile.close() sets
                is_open = not getattr(file_obj, 'closed', False)
                ostack[-1] = ps.Bool(is_open)
            elif file_obj.val is None:
                # Regular file with no underlying file object
                ostack[-1] = ps.Bool(False)
            elif file_obj.val.closed:
                ostack[-1] = ps.Bool(False)
            else:
                ostack[-1] = ps.Bool(True)
    else:
        try:
            resolved = _resolve_filename(ctxt, ostack[-1].python_string())
            st = os.stat(resolved)
            ctime = int(os.path.getctime(resolved))
            ostack[-1] = ps.Int((st.st_size // 512) + 1)
            ostack.append(ps.Int(st.st_size))
            ostack.append(ps.Int(int(st.st_atime)))
            ostack.append(ps.Int(ctime))
            ostack.append(ps.Bool(True))
        except (OSError, FileNotFoundError, PermissionError):
            ostack[-1] = ps.Bool(False)


def write(ctxt, ostack):
    """
    file int **write** -

    Appends a single character to file, whose value is int. int must be
    in the range 0 to 255; **write** uses only the low-order 8 bits.

    PLRM Section 8.2
    **Errors**: **invalidaccess**, **ioerror**, **stackunderflow**, **typecheck**
    """
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, write.__name__)

    if not isinstance(ostack[-2], (ps.File, ps.StandardFileProxy)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, write.__name__)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, write.__name__)

    if ostack[-2].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, write.__name__)

    int_obj = ostack.pop()
    file_obj = ostack.pop()
    if isinstance(file_obj, ps.StandardFileProxy):
        file_obj = file_obj.get_standard_file()

    try:
        file_obj.write(ctxt, int_obj.val & 0xFF)
    except Exception:
        return ps_error.e(ctxt, ps_error.IOERROR, write.__name__)


def writestring(ctxt, ostack):
    """
    file string **writestring** -

    writes the characters of string to file. **writestring** does not append a newline 
    character or interpret the value of string, which can contain arbitrary binary data. 
    However, the communication channel may usurp certain control characters or 
    impose other restrictions.

    As is the case for all operators that write to files, the output produced by 
    **writestring** may accumulate in a buffer instead of being transmitted immediately. 
    To ensure immediate transmission, invoking **flushfile** is required.

    PLRM Section 8.2, Page 722 (Third Edition)
    **Errors**: **invalidaccess**, **ioerror**, **stackunderflow**, **typecheck**
    """
    
    # Validate stack depth and operand types
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, writestring.__name__)
    
    if not isinstance(ostack[-2], (ps.File, ps.StandardFileProxy)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, writestring.__name__)

    if not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, writestring.__name__)

    # Check file has write access (>= ACCESS_WRITE_ONLY)
    if ostack[-2].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, writestring.__name__)

    # Check string has read access (>= ACCESS_READ_ONLY)
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, writestring.__name__)

    # Pop operands
    string_obj = ostack.pop()
    file_obj = ostack.pop()
    if isinstance(file_obj, ps.StandardFileProxy):
        file_obj = file_obj.get_standard_file()
    
    try:
        # Write each byte from string to file
        string_bytes = string_obj.byte_string()
        for byte_val in string_bytes:
            file_obj.write(ctxt, byte_val)
        
    except Exception as e:
        return ps_error.e(ctxt, ps_error.IOERROR, writestring.__name__)


def writehexstring(ctxt, ostack):
    """
    file string **writehexstring** -

    writes all of the characters of string to file as hexadecimal digits. For each element 
    of string (an integer in the range 0 to 255), **writehexstring** appends a two-digit 
    hexadecimal number composed of the characters 0 to 9 and a through f.

    As is the case for all operators that write to files, the output produced by 
    **writehexstring** may accumulate in a buffer instead of being transmitted immediately. 
    To ensure immediate transmission, invoking **flushfile** is required.

    PLRM Section 8.2, Page 721 (Third Edition)
    **Errors**: **invalidaccess**, **ioerror**, **stackunderflow**, **typecheck**
    """
    
    # Validate stack depth and operand types
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, writehexstring.__name__)
    
    if not isinstance(ostack[-2], (ps.File, ps.StandardFileProxy)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, writehexstring.__name__)

    if not isinstance(ostack[-1], ps.String):
        return ps_error.e(ctxt, ps_error.TYPECHECK, writehexstring.__name__)

    # Check file has write access (>= ACCESS_WRITE_ONLY)
    if ostack[-2].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, writehexstring.__name__)

    # Check string has read access (>= ACCESS_READ_ONLY)
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, writehexstring.__name__)

    # Pop operands
    string_obj = ostack.pop()
    file_obj = ostack.pop()
    if isinstance(file_obj, ps.StandardFileProxy):
        file_obj = file_obj.get_standard_file()
    
    try:
        # Convert each byte to two hex digits and write
        string_bytes = string_obj.byte_string()
        for byte_val in string_bytes:
            # Convert byte to two-digit hex (lowercase)
            hex_string = f"{byte_val:02x}"
            
            # Write each hex digit
            for hex_char in hex_string:
                file_obj.write(ctxt, ord(hex_char))
        
    except Exception as e:
        return ps_error.e(ctxt, ps_error.IOERROR, writehexstring.__name__)


def bytesavailable(ctxt, ostack):
    """
    file **bytesavailable** int

    returns the number of bytes that are immediately available for reading from file without waiting. 
    The result is -1 if end-of-file has been encountered or if the number of bytes available cannot 
    be determined for other reasons.

    PLRM Section 8.2, Page 84 (Second Edition), Page 142 (Third Edition)
    Stack: file **bytesavailable** int
    **Errors**: **stackunderflow**, **typecheck**
    """
    
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, bytesavailable.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_FILE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, bytesavailable.__name__)
    
    file_obj = ostack[-1]
    
    # Handle StandardFileProxy objects (stdin, stdout, stderr)
    if isinstance(file_obj, ps.StandardFileProxy):
        actual_file = file_obj.get_standard_file()
        if actual_file is None:
            ostack[-1] = ps.Int(-1)
            return
        
        # For standard files, check the proxy name
        if hasattr(file_obj, 'name'):
            if file_obj.name == "%stdin":
                # For stdin, we typically can't determine bytes available
                # Return -1 as per PLRM for unknown status
                ostack[-1] = ps.Int(-1)
                return
            elif file_obj.name in ["%stdout", "%stderr"]:
                # For output files, bytes available concept doesn't apply
                ostack[-1] = ps.Int(-1)
                return
        
        # Use the actual file for further processing
        file_obj = actual_file
    
    # Check if file is closed
    if hasattr(file_obj, 'val') and hasattr(file_obj.val, 'closed') and file_obj.val.closed:
        ostack[-1] = ps.Int(-1)
        return
    
    # For regular files opened for reading, try to determine available bytes
    if (hasattr(file_obj, 'mode') and file_obj.mode and 'r' in file_obj.mode and 
        hasattr(file_obj, 'val') and hasattr(file_obj.val, 'seekable')):
        
        try:
            # Only try seek operations on seekable files
            if file_obj.val.seekable():
                current_pos = file_obj.val.tell()
                file_obj.val.seek(0, 2)  # Seek to end
                file_size = file_obj.val.tell()
                file_obj.val.seek(current_pos)  # Restore position
                
                available_bytes = file_size - current_pos
                # PLRM: Return -1 if EOF or if bytes cannot be determined
                if available_bytes <= 0:
                    available_bytes = -1
                    
                ostack[-1] = ps.Int(available_bytes)
                return
            else:
                # Non-seekable file (like stdin, pipes, etc.)
                ostack[-1] = ps.Int(-1)
                return
                
        except (OSError, IOError, AttributeError):
            # Cannot determine position/size for this file type
            # Return -1 per PLRM for unknown status
            ostack[-1] = ps.Int(-1)
            return
    
    # For all other cases (output files, special files, unseekable files, etc.)
    # Return -1 as specified in PLRM
    ostack[-1] = ps.Int(-1)


def setfileposition(ctxt, ostack):
    """
    file position **setfileposition** -

    repositions an existing open file to a new position so the next read or write operation 
    will commence at that position. The position operand is a non-negative integer interpreted 
    as number of bytes from the beginning of the file. For an output file, **setfileposition** 
    first performs an implicit **flushfile** operation.

    PLRM Section 8.2, Page 668 (Third Edition)
    Stack: file position **setfileposition** -
    **Errors**: **ioerror**, **rangecheck**, **stackunderflow**, **typecheck**
    """
    
    # Validate stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setfileposition.__name__)
    
    # Validate operand types
    if not isinstance(ostack[-2], (ps.File, ps.StandardFileProxy)):
        return ps_error.e(ctxt, ps_error.TYPECHECK, setfileposition.__name__)
        
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setfileposition.__name__)
    
    file_obj = ostack[-2]
    position = ostack[-1].val

    # Check position is non-negative
    if position < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setfileposition.__name__)

    # Check if it's a FilterFile (has 'filter' attribute) - not positionable
    if hasattr(file_obj, 'filter'):
        return ps_error.e(ctxt, ps_error.IOERROR, setfileposition.__name__)

    # Check for valid file handle
    if file_obj.val is None:
        return ps_error.e(ctxt, ps_error.IOERROR, setfileposition.__name__)

    # Handle StandardFileProxy objects  
    if isinstance(file_obj, ps.StandardFileProxy):
        actual_file = file_obj.get_standard_file()
        if actual_file is None:
            return ps_error.e(ctxt, ps_error.IOERROR, setfileposition.__name__)
        
        # Standard files (stdin, stdout, stderr) are typically not positionable
        if hasattr(file_obj, 'name') and file_obj.name in ["%stdin", "%stdout", "%stderr"]:
            return ps_error.e(ctxt, ps_error.IOERROR, setfileposition.__name__)
        
        file_obj = actual_file
    
    # Check if file is closed
    if hasattr(file_obj, 'val') and hasattr(file_obj.val, 'closed') and file_obj.val.closed:
        return ps_error.e(ctxt, ps_error.IOERROR, setfileposition.__name__)
    
    try:
        # PLRM: For output files, first perform implicit flushfile
        if (hasattr(file_obj, 'mode') and file_obj.mode and 
            ('w' in file_obj.mode or 'a' in file_obj.mode or '+' in file_obj.mode)):
            try:
                file_obj.val.flush()
            except (OSError, IOError):
                pass  # Continue with seek even if flush fails
        
        # Check if file is positionable (seekable)
        if not hasattr(file_obj.val, 'seekable') or not file_obj.val.seekable():
            return ps_error.e(ctxt, ps_error.IOERROR, setfileposition.__name__)
        
        # Perform the seek operation
        file_obj.val.seek(position, 0)  # 0 = from beginning of file
        
        # Pop both operands
        ostack.pop()  # position
        ostack.pop()  # file
        
    except (OSError, IOError, ValueError):
        return ps_error.e(ctxt, ps_error.IOERROR, setfileposition.__name__)
    except OverflowError:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setfileposition.__name__)


def resetfile(ctxt, ostack):
    """
    file **resetfile** -


    discards any characters buffered for the specified file. For output files, any
    buffered characters are sent to the file; for input files, any buffered characters
    are discarded without being read. Unlike **flushfile**, **resetfile** does not read and
    discard data from the file itself.

    **Errors**:     **invalidaccess**, **ioerror**, **stackunderflow**, **typecheck**
    **See Also**:   **flushfile**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, resetfile.__name__)

    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_FILE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, resetfile.__name__)

    file_obj = ostack[-1]

    # Resolve StandardFileProxy to underlying file
    if isinstance(file_obj, ps.StandardFileProxy):
        actual_file = file_obj.get_standard_file()
        if actual_file is not None:
            file_obj = actual_file

    if hasattr(file_obj, 'mode') and file_obj.mode and 'w' in file_obj.mode:
        # Output file: flush buffered output to the underlying file
        try:
            file_obj.flush()
        except Exception:
            pass
    else:
        # Input file: discard buffered data
        if hasattr(file_obj, 'reset'):
            # FilterFile - clear decoded-but-unconsumed buffer
            file_obj.reset()
        elif hasattr(file_obj, 'val') and file_obj.val is not None:
            # Plain File - clear any unread state by seeking back to current pos
            # (Python buffered I/O has no explicit buffer-discard, but we can
            # clear the 1-byte unread lookahead if present)
            pass

    ostack.pop()


def renamefile(ctxt, ostack):
    """
    oldname newname **renamefile** -

    changes the name of a file from oldname to newname, where oldname and newname are strings 
    that specify file names on the same storage device. If no such file exists, an undefinedfilename 
    error occurs. If the device does not allow this operation, an invalidfileaccess error occurs. 
    If an environment-dependent error is detected, an ioerror occurs. Whether or not an error 
    occurs if a file named newname already exists is environment dependent.

    PLRM Section 8.2, Page 644 (Third Edition)  
    Stack: oldname newname **renamefile** -
    **Errors**: **invalidfileaccess**, **ioerror**, **stackunderflow**, **typecheck**, **undefinedfilename**
    """
    
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, renamefile.__name__)
    
    # 2. TYPECHECK - Check operand types (newname oldname)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, renamefile.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, renamefile.__name__)
    
    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY or ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, renamefile.__name__)
    
    oldname = ostack[-2].python_string()
    newname = ostack[-1].python_string()

    # Check for special file names that cannot be renamed
    special_files = ["%stdin", "%stdout", "%stderr", "%statementedit", "%lineedit"]
    if oldname in special_files or newname in special_files:
        return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, renamefile.__name__)

    try:
        # Resolve relative paths against user's CWD
        oldname = _resolve_filename(ctxt, oldname)
        newname = _resolve_filename(ctxt, newname)

        # Check if source file exists
        if not os.path.exists(oldname):
            return ps_error.e(ctxt, ps_error.UNDEFINEDFILENAME, renamefile.__name__)

        # Perform the rename operation
        os.rename(oldname, newname)
        
        # Pop both operands
        ostack.pop()  # newname
        ostack.pop()  # oldname
        
    except PermissionError:
        return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, renamefile.__name__)
    except FileNotFoundError:
        return ps_error.e(ctxt, ps_error.UNDEFINEDFILENAME, renamefile.__name__)
    except OSError as e:
        # Handle various OS-level errors (device full, cross-device rename, etc.)
        return ps_error.e(ctxt, ps_error.IOERROR, renamefile.__name__)


def deletefile(ctxt, ostack):
    """
    filename **deletefile** -

    removes the specified file from its storage device. If no such file exists, an 
    undefinedfilename error occurs. If the device does not allow this operation, an 
    invalidfileaccess error occurs. If an environment dependent error is detected, 
    an ioerror occurs.

    PLRM Section 8.2, Page 247 (Second Edition)
    **Errors**: **invalidfileaccess**, **ioerror**, **stackunderflow**, **typecheck**, **undefinedfilename**
    """
    
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, deletefile.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, deletefile.__name__)
    
    # Pop filename
    filename_obj = ostack.pop()
    filename = filename_obj.python_string()

    # Check for special file names that cannot be deleted
    if filename in ["%stdin", "%stdout", "%stderr", "%statementedit", "%lineedit"]:
        return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, deletefile.__name__)

    try:
        # Resolve relative paths against user's CWD
        filename = _resolve_filename(ctxt, filename)

        # Check if file exists
        if not os.path.exists(filename):
            return ps_error.e(ctxt, ps_error.UNDEFINEDFILENAME, deletefile.__name__)

        # Try to delete the file
        os.remove(filename)
        
    except PermissionError:
        return ps_error.e(ctxt, ps_error.INVALIDFILEACCESS, deletefile.__name__)
    except OSError as e:
        return ps_error.e(ctxt, ps_error.IOERROR, deletefile.__name__)


def setobjectformat(ctxt, ostack):
    """
    int **setobjectformat** –

    Establishes the number representation to be used in binary object sequences
    written by subsequent execution of **printobject** and **writeobject**. The int
    operand is one of the following:
        0  Disable binary encodings
        1  IEEE high-order byte first
        2  IEEE low-order byte first
        3  Native high-order byte first
        4  Native low-order byte first

    Modifications to the object format parameter are subject to **save** and **restore**.

    Stack: int **setobjectformat** –
    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setobjectformat.__name__)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setobjectformat.__name__)

    val = ostack[-1].val
    if val < 0 or val > 4:
        return ps_error.e(ctxt, ps_error.RANGECHECK, setobjectformat.__name__)

    ostack.pop()
    ctxt.object_format = val


def currentobjectformat(ctxt, ostack):
    """
    – **currentobjectformat** int

    Returns the object format parameter currently in effect.

    Stack: – **currentobjectformat** int
    **Errors**: **stackoverflow**
    """
    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentobjectformat.__name__)
        return

    ostack.append(ps.Int(ctxt.object_format))


# ---------------------------------------------------------------------------
# Binary object sequence output (PLRM Section 3.14.2 / 3.14.6)
# ---------------------------------------------------------------------------

# Reverse lookup table: name_bytes -> system name index
_REVERSE_NAME_TABLE = None


def _build_reverse_name_table():
    """Build {name_bytes: index} from the system name table (one-time)."""
    global _REVERSE_NAME_TABLE
    if _REVERSE_NAME_TABLE is not None:
        return
    _REVERSE_NAME_TABLE = {}
    for idx, name in enumerate(_SYSTEM_NAME_TABLE):
        if name is not None:
            _REVERSE_NAME_TABLE[name] = idx


# Mapping from PostForge type constants to binary object type codes (Table 3.27)
_PS_TYPE_TO_BOS = {
    ps.T_NULL:   0,
    ps.T_INT:    1,
    ps.T_REAL:   2,
    ps.T_NAME:   3,
    ps.T_BOOL:   4,
    ps.T_STRING: 5,
    ps.T_ARRAY:  9,
    ps.T_MARK:  10,
}


def _serialize_binary_object_seq(ctxt, obj, tag, object_format):
    """
    Serialize a PostScript object as a binary object sequence (PLRM 3.14.2).

    Returns bytes on success, or a string error name on failure.
    """
    _build_reverse_name_table()

    # Byte order: 1,3 = big-endian; 2,4 = little-endian
    if object_format in (1, 3):
        endian = ">"
    else:
        endian = "<"

    # Token type byte = 127 + object_format (maps 1->128, 2->129, 3->130, 4->131)
    token_type = 127 + object_format

    # Collect all objects and string data
    # objects: list of (type_byte, tag_byte, length_u16, value_u32)
    # strings: bytearray of string/name data
    objects = []
    strings = bytearray()

    # Track nesting depth to detect limitcheck
    max_depth = 100

    def _encode_at(ps_obj, idx, depth):
        """Encode ps_obj into objects[idx]. May append subsidiary entries."""
        if depth > max_depth:
            return "limitcheck"

        t = ps_obj.TYPE

        if t not in _PS_TYPE_TO_BOS:
            return "typecheck"

        bos_type = _PS_TYPE_TO_BOS[t]

        # Literal/executable attribute: bit 7 of type byte
        if ps_obj.attrib == ps.ATTRIB_EXEC:
            type_byte = bos_type | 0x80
        else:
            type_byte = bos_type

        if t == ps.T_NULL:
            objects[idx] = (type_byte, 0, 0, 0)

        elif t == ps.T_INT:
            val = ps_obj.val & 0xFFFFFFFF
            objects[idx] = (type_byte, 0, 0, val)

        elif t == ps.T_REAL:
            float_bytes = struct.pack(endian + "f", ps_obj.val)
            val_u32 = struct.unpack(endian + "I", float_bytes)[0]
            objects[idx] = (type_byte, 0, 0, val_u32)

        elif t == ps.T_BOOL:
            objects[idx] = (type_byte, 0, 0, 1 if ps_obj.val else 0)

        elif t == ps.T_MARK:
            objects[idx] = (type_byte, 0, 0, 0)

        elif t == ps.T_NAME:
            name_bytes = ps_obj.val
            str_offset = len(strings)
            strings.extend(name_bytes)
            objects[idx] = (type_byte, 0, len(name_bytes), str_offset)

        elif t == ps.T_STRING:
            str_data = ps_obj.byte_string()
            str_offset = len(strings)
            strings.extend(str_data)
            objects[idx] = (type_byte, 0, len(str_data), str_offset)

        elif t == ps.T_ARRAY:
            # Reserve consecutive slots for direct children so they are
            # contiguous in the binary output (PLRM requirement).
            first_child = len(objects)
            for _ in range(ps_obj.length):
                objects.append(None)
            # Recursively encode each child (subsidiaries go after all slots)
            for i in range(ps_obj.length):
                child = ps_obj.val[ps_obj.start + i]
                err = _encode_at(child, first_child + i, depth + 1)
                if err is not None:
                    return err
            objects[idx] = (type_byte, 0, ps_obj.length, first_child * 8)

        return None

    # Encode the user object directly as the single top-level entry.
    # Per PLRM 3.14.6: "The binary object sequence contains a top-level array
    # consisting of one element that is the object being written."
    objects.append(None)  # reserve slot 0
    err = _encode_at(obj, 0, 0)
    if err is not None:
        return err

    # Place the tag on the top-level object (PLRM: "This tag is carried in
    # the second byte of the object, which is otherwise unused.")
    tb, _, length, value = objects[0]
    objects[0] = (tb, tag, length, value)

    # Now adjust string offsets: strings start after all object entries
    # Each object is 8 bytes; string region starts at len(objects) * 8
    string_base = len(objects) * 8
    adjusted_objects = []
    for (tb, tg, length, value) in objects:
        bos_type_code = tb & 0x7F
        if bos_type_code == 5:  # string
            adjusted_objects.append((tb, tg, length, value + string_base))
        elif bos_type_code == 3:  # name (text reference)
            adjusted_objects.append((tb, tg, length, value + string_base))
        else:
            adjusted_objects.append((tb, tg, length, value))

    # Build binary output — object entries first, then compute header
    obj_data = bytearray()
    for (tb, tg, length, value) in adjusted_objects:
        obj_data.extend(struct.pack("BB", tb, tg))
        obj_data.extend(struct.pack(endian + "H", length))
        obj_data.extend(struct.pack(endian + "I", value))

    # Header: PLRM 3.14.2 — normal (4 bytes) when overall_length <= 255,
    # extended (8 bytes) otherwise. Top-level count is always 1.
    body_size = len(obj_data) + len(strings)
    normal_length = 4 + body_size
    if normal_length <= 255:
        header = struct.pack("BB", token_type, normal_length) + struct.pack(endian + "H", 1)
    else:
        extended_length = 8 + body_size
        header = struct.pack("BB", token_type, 0) + struct.pack(endian + "H", 1) + struct.pack(endian + "I", extended_length)

    return bytes(header) + bytes(obj_data) + bytes(strings)


def printobject(ctxt, ostack):
    """
    obj tag **printobject** –

    Writes a binary object sequence representing obj to the standard output
    file. tag is an integer 0–255 associated with the top-level object.
    The number representation is determined by **setobjectformat**.

    **Errors**: **invalidaccess**, **ioerror**, **limitcheck**, **rangecheck**, **stackunderflow**,
            typecheck, undefined
    """
    # 1. STACKUNDERFLOW
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, printobject.__name__)

    # 2. TYPECHECK - tag must be integer
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, printobject.__name__)

    # 3. RANGECHECK - tag 0-255
    tag_val = ostack[-1].val
    if tag_val < 0 or tag_val > 255:
        return ps_error.e(ctxt, ps_error.RANGECHECK, printobject.__name__)

    # 4. UNDEFINED - object_format must not be 0
    if ctxt.object_format == 0:
        return ps_error.e(ctxt, ps_error.UNDEFINED, printobject.__name__)

    # All validation passed — pop operands
    tag_obj = ostack.pop()
    obj = ostack.pop()

    result = _serialize_binary_object_seq(ctxt, obj, tag_val, ctxt.object_format)
    if isinstance(result, str):
        # Error string returned
        return ps_error.e(ctxt, getattr(ps_error, result.upper()), printobject.__name__)

    # Write bytes to stdout
    try:
        for byte_val in result:
            ctxt.stdout_file.write(byte_val, ctxt)
    except Exception:
        return ps_error.e(ctxt, ps_error.IOERROR, printobject.__name__)


def writeobject(ctxt, ostack):
    """
    file obj tag **writeobject** –

    Writes a binary object sequence representing obj to file.
    Except for taking an explicit file operand, **writeobject** is
    identical to **printobject** in all respects.

    **Errors**: **invalidaccess**, **ioerror**, **limitcheck**, **rangecheck**, **stackunderflow**,
            typecheck, undefined
    """
    # 1. STACKUNDERFLOW
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, writeobject.__name__)

    # 2. TYPECHECK - file must be a file, tag must be integer
    if ostack[-3].TYPE != ps.T_FILE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, writeobject.__name__)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, writeobject.__name__)

    # 3. INVALIDACCESS - file must have write access
    if ostack[-3].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, writeobject.__name__)

    # 4. RANGECHECK - tag 0-255
    tag_val = ostack[-1].val
    if tag_val < 0 or tag_val > 255:
        return ps_error.e(ctxt, ps_error.RANGECHECK, writeobject.__name__)

    # 5. UNDEFINED - object_format must not be 0
    if ctxt.object_format == 0:
        return ps_error.e(ctxt, ps_error.UNDEFINED, writeobject.__name__)

    # All validation passed — pop operands
    tag_obj = ostack.pop()
    obj = ostack.pop()
    file_obj = ostack.pop()

    result = _serialize_binary_object_seq(ctxt, obj, tag_val, ctxt.object_format)
    if isinstance(result, str):
        return ps_error.e(ctxt, getattr(ps_error, result.upper()), writeobject.__name__)

    # Write bytes to file
    try:
        if isinstance(file_obj, ps.StandardFileProxy):
            for byte_val in result:
                file_obj.write(byte_val, ctxt)
        else:
            for byte_val in result:
                file_obj.write(ctxt, byte_val)
    except Exception:
        return ps_error.e(ctxt, ps_error.IOERROR, writeobject.__name__)
