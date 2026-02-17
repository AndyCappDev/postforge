# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
CharString Interpreter for Type 1 Font CharString Execution

This module implements the Type 1 CharString interpreter for PostForge's font system.
It provides CharString decryption (key=4330) and execution of Type 1 commands to
generate PostScript path operations for glyph rendering.

Architecture:
- CharString decryption using Adobe algorithm with key=4330, skip 1 random byte
- Type 1 command interpreter (rmoveto, rlineto, rrcurveto, etc.)
- Path generation: CharString → PostScript path operations
- Character width calculation from hsbw/sbw commands
- Integration with font Private dictionary for hinting parameters

Based on:
- Adobe Type 1 Font Format specification
"""

from . import types as ps
from . import color_space
from . import error as ps_error
from typing import List, Tuple, Optional, Union
from ..operators.matrix import _transform_point, _transform_delta
# Direct path manipulation - no longer import PostScript operators to avoid stack issues
from .display_list_builder import DisplayListBuilder
        

class CharStringInterpreter:
    """
    Type 1 CharString execution engine
    
    Executes encrypted CharString programs by adding path operations directly
    to the PostScript graphics state path, just like regular path operators.
    """
    
    def __init__(self, ctxt, private_dict: ps.Dict, font_dict: ps.Dict, width_only_mode: bool = False):
        """
        Initialize CharString interpreter
        
        Args:
            ctxt: PostScript context for graphics state access
            private_dict: Font's Private dictionary containing hinting parameters  
            font_dict: Font dictionary containing FontMatrix
            width_only_mode: If True, only calculate width without adding to path (for stringwidth)
        """
        self.ctxt = ctxt  # PostScript context
        self.width_only_mode = width_only_mode  # Skip path operations if True
        self.stack = []  # CharString operand stack (separate from PostScript stack)
        self.private = private_dict  # Font Private dictionary
        self.font_dict = font_dict  # Font dictionary
        self.current_point = (0.0, 0.0)  # Current point in glyph coordinate system
        self.advance_width = None  # Character advance width from hsbw/sbw
        self.left_sidebearing = None  # Left sidebearing from hsbw/sbw
        self.advance_width_y = 0.0  # Y-component of advance width (for vertical text)
        self.sidebearing_y = 0.0  # Y-component of sidebearing

        # Store the original currentpoint from the show operation (before any CharString modifications)
        self.show_origin = ctxt.gstate.currentpoint if ctxt and not width_only_mode else None

        # PostScript stack for OtherSubrs communication (separate from CharString stack)
        self.ps_stack = []

        # Flex hint state (OtherSubrs 0, 1, 2)
        self.flex_active = False  # True when in flex hint accumulation mode
        self.flex_points = []  # Accumulated flex points (up to 7 coordinate pairs)
        
    def execute_charstring_for_width(self, encrypted_charstring: bytes) -> Optional[float]:
        """
        Execute CharString, adding paths to graphics state and returning character width

        Path operations are added directly to ctxt.gstate.path during execution.
        This integrates with PostScript's standard path construction system.

        Args:
            encrypted_charstring: Encrypted CharString data from font CharStrings dictionary

        Returns:
            character_width: Character advance width for text positioning, or None if failed

        Raises:
            CharStringError: On decryption or execution failures
        """

        # try:
        # 1. Decrypt CharString data (key=4330, skip 1 random byte)
        decrypted_data = self._decrypt_charstring(encrypted_charstring)

        # 2. Parse CharString commands from decrypted bytes
        commands = self._parse_charstring_commands(decrypted_data)

        # 3. Execute Type 1 commands - paths added to ctxt.gstate.path automatically
        for command_code, args in commands:
            self._execute_type1_command(command_code, args)

        # 4. Return only character width (paths already in graphics state)
        return self.advance_width
            
        # except Exception as e:
        #     # Convert Python exceptions to PostScript-compatible errors
        #     raise CharStringError(f"CharString execution failed: {str(e)}")
    
    def _decrypt_charstring(self, encrypted_data: bytes) -> bytes:
        """
        Decrypt CharString data using Adobe Type 1 algorithm

        Adobe CharString decryption (different from eexec):
        - Initial key R = 4330
        - Constants: c1 = 52845, c2 = 22719
        - Skip n = lenIV random bytes (default 4, specified in Private dictionary)
        - Algorithm: plaintext[i] = ciphertext[i] XOR (R >> 8)
                    R = ((ciphertext[i] + R) * c1 + c2) & 0xFFFF

        Args:
            encrypted_data: Encrypted CharString bytes

        Returns:
            Decrypted CharString bytes (with lenIV random bytes removed)
        """
        if not encrypted_data:
            raise CharStringError("Empty CharString data")
        
        # Adobe CharString encryption constants
        R = 4330  # Initial key for CharString encryption
        C1 = 52845  # Encryption constant 1
        C2 = 22719  # Encryption constant 2
        
        decrypted_bytes = []
        
        # Decrypt each byte using Adobe algorithm
        for cipher_byte in encrypted_data:
            # Decrypt this byte
            plain_byte = cipher_byte ^ (R >> 8)
            decrypted_bytes.append(plain_byte)
            
            # Update encryption key for next byte
            R = ((cipher_byte + R) * C1 + C2) & 0xFFFF
        
        # Skip first n random bytes per Adobe CharString specification
        # n is specified by lenIV in Private dict (default 4)
        n_iv = 4  # Default lenIV for CharStrings
        if self.private:
            len_iv_obj = self.private.val.get(b'lenIV')
            if len_iv_obj is not None:
                n_iv = int(len_iv_obj.val)

        if len(decrypted_bytes) < n_iv:
            raise CharStringError(f"CharString too short (need {n_iv} random bytes to skip)")

        return bytes(decrypted_bytes[n_iv:])
    
    def _parse_charstring_commands(self, charstring_data: bytes) -> List[Tuple[int, List]]:
        """
        Parse Type 1 CharString commands from decrypted data
        
        Type 1 CharString format:
        - Numbers: 0-31 are commands, 32-255 are integer literals or multi-byte numbers
        - Multi-byte numbers: Special encoding for large integers
        - Commands may have 0 or more numeric arguments from the stack
        
        Args:
            charstring_data: Decrypted CharString bytes
            
        Returns:
            List of (command_code, arguments) tuples
        """
        commands = []
        i = 0
        current_numbers = []
        
        while i < len(charstring_data):
            byte = charstring_data[i]
            
            if byte <= 31:
                # This is a command byte
                if byte == 12:
                    # Two-byte command (escape sequence)
                    if i + 1 >= len(charstring_data):
                        raise CharStringError("Incomplete escape command at end of CharString")
                    escape_byte = charstring_data[i + 1]
                    command_code = (12, escape_byte)  # Store as tuple for escape commands
                    i += 2
                else:
                    # Single-byte command
                    command_code = byte
                    i += 1
                
                # Collect arguments for this command (numbers parsed before the command)
                command_args = current_numbers.copy()
                current_numbers.clear()
                
                commands.append((command_code, command_args))
                
            elif 32 <= byte <= 246:
                # Single-byte integer: value = byte - 139
                number = byte - 139
                current_numbers.append(number)
                i += 1
                
            elif 247 <= byte <= 250:
                # Two-byte positive integer: value = (byte - 247) * 256 + next_byte + 108
                if i + 1 >= len(charstring_data):
                    raise CharStringError("Incomplete two-byte number at end of CharString")
                next_byte = charstring_data[i + 1]
                number = (byte - 247) * 256 + next_byte + 108
                current_numbers.append(number)
                i += 2
                
            elif 251 <= byte <= 254:
                # Two-byte negative integer: value = -(byte - 251) * 256 - next_byte - 108
                if i + 1 >= len(charstring_data):
                    raise CharStringError("Incomplete two-byte number at end of CharString")
                next_byte = charstring_data[i + 1]
                number = -(byte - 251) * 256 - next_byte - 108
                current_numbers.append(number)
                i += 2
                
            elif byte == 255:
                # Five-byte signed integer (32-bit)
                if i + 4 >= len(charstring_data):
                    raise CharStringError("Incomplete five-byte number at end of CharString")
                # Read 4 bytes in big-endian format
                byte1 = charstring_data[i + 1]
                byte2 = charstring_data[i + 2]
                byte3 = charstring_data[i + 3]
                byte4 = charstring_data[i + 4]
                
                # Combine into 32-bit signed integer
                number = (byte1 << 24) | (byte2 << 16) | (byte3 << 8) | byte4
                
                # Handle signed representation (two's complement)
                if number >= 0x80000000:
                    number -= 0x100000000
                
                current_numbers.append(number)
                i += 5
                
            else:
                raise CharStringError(f"Invalid CharString byte: {byte}")
        
        return commands
    
    def _execute_type1_command(self, command_code: Union[int, Tuple[int, int]], args: List[int]):
        """
        Execute individual Type 1 CharString command
        
        Args:
            command_code: Command identifier (int for single-byte, tuple for escape commands)
            args: List of numeric arguments for the command
        """
        # Push arguments onto CharString stack
        for arg in args:
            self.stack.append(float(arg))
        
        # Execute command based on command code
        if command_code == 1:  # hstem
            self._cmd_hstem()
        elif command_code == 3:  # vstem
            self._cmd_vstem()
        elif command_code == 4:  # vmoveto
            self._cmd_vmoveto()
        elif command_code == 5:  # rlineto
            self._cmd_rlineto()
        elif command_code == 6:  # hlineto
            self._cmd_hlineto()
        elif command_code == 7:  # vlineto
            self._cmd_vlineto()
        elif command_code == 8:  # rrcurveto
            self._cmd_rrcurveto()
        elif command_code == 9:  # closepath
            self._cmd_closepath()
        elif command_code == 10:  # callsubr
            self._cmd_callsubr()
        elif command_code == 11:  # return
            self._cmd_return()
        elif command_code == (12, 0):  # dotsection
            self._cmd_dotsection()
        elif command_code == (12, 1):  # vstem3
            self._cmd_vstem3()
        elif command_code == (12, 2):  # hstem3
            self._cmd_hstem3()
        elif command_code == (12, 6):  # seac
            self._cmd_seac()
        elif command_code == (12, 7):  # sbw
            self._cmd_sbw()
        elif command_code == (12, 12):  # div
            self._cmd_div()
        elif command_code == (12, 16):  # callothersubr
            self._cmd_callothersubr()
        elif command_code == (12, 17):  # pop
            self._cmd_pop()
        elif command_code == (12, 33):  # setcurrentpoint
            self._cmd_setcurrentpoint()
        elif command_code == 13:  # hsbw
            self._cmd_hsbw()
        elif command_code == 14:  # endchar
            self._cmd_endchar()
        elif command_code == 21:  # rmoveto
            self._cmd_rmoveto()
        elif command_code == 22:  # hmoveto
            self._cmd_hmoveto()
        elif command_code == 30:  # vhcurveto
            self._cmd_vhcurveto()
        elif command_code == 31:  # hvcurveto
            self._cmd_hvcurveto()
        else:
            # Unknown command - log warning but continue
            # Some fonts may have undocumented or proprietary commands
            pass
    
    # Type 1 Command Implementations
    
    def _cmd_hsbw(self):
        """hsbw: Set horizontal sidebearing and width"""
        if len(self.stack) < 2:
            raise CharStringError("hsbw: stack underflow")
        
        self.advance_width = self.stack.pop()    # wx (top of stack)
        self.left_sidebearing = self.stack.pop()  # sbx (second from top)
        
        # Set current point to (left_sidebearing, 0) but don't add to path
        # This is the character's internal coordinate system - don't update gstate.currentpoint
        self.current_point = (self.left_sidebearing, 0.0)
        
    def _cmd_sbw(self):
        """sbw: Set sidebearing and width (both x and y components)"""
        if len(self.stack) < 4:
            raise CharStringError("sbw: stack underflow")
        
        self.advance_width_y = self.stack.pop()   # wy (top of stack)
        self.advance_width = self.stack.pop()     # wx (second from top)
        self.sidebearing_y = self.stack.pop()     # sby (third from top)
        self.left_sidebearing = self.stack.pop()  # sbx (fourth from top)
        
        # Set current point to (left_sidebearing, sidebearing_y) but don't add to path
        # This is the character's internal coordinate system - don't update gstate.currentpoint
        self.current_point = (self.left_sidebearing, self.sidebearing_y)
    
    def _cmd_rmoveto(self):
        """rmoveto: Relative move to"""
        if len(self.stack) < 2:
            raise CharStringError("rmoveto: stack underflow")
        
        dy = self.stack.pop()
        dx = self.stack.pop()
        
        # Update current point in glyph space
        old_point = self.current_point
        self.current_point = (self.current_point[0] + dx, self.current_point[1] + dy)
        
        # Skip path operations in width-only mode (for stringwidth)
        if self.width_only_mode:
            return

        # Skip path operations during flex accumulation - only track current_point
        if self.flex_active:
            return

        # Transform to device space and add to graphics state path
        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])

        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        # Per PLRM: consecutive movetos replace the previous moveto point
        if (self.ctxt.gstate.path and len(self.ctxt.gstate.path[-1]) == 1
                and isinstance(self.ctxt.gstate.path[-1][0], ps.MoveTo)):
            self.ctxt.gstate.path[-1][0] = ps.MoveTo(ps.Point(device_x, device_y))
        else:
            self.ctxt.gstate.path.append(ps.SubPath())
            self.ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(device_x, device_y)))

        # Update the currentpoint (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))

    def _cmd_hmoveto(self):
        """hmoveto: Horizontal move to (equivalent to dx 0 rmoveto)"""
        if len(self.stack) < 1:
            raise CharStringError("hmoveto: stack underflow")

        dx = self.stack.pop()

        # Update current point in glyph space
        self.current_point = (self.current_point[0] + dx, self.current_point[1])

        # Skip path operations in width-only mode
        if self.width_only_mode:
            return

        # Skip path operations during flex accumulation - only track current_point
        if self.flex_active:
            return

        # Transform to device space and add to graphics state path
        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])

        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        # Per PLRM: consecutive movetos replace the previous moveto point
        if (self.ctxt.gstate.path and len(self.ctxt.gstate.path[-1]) == 1
                and isinstance(self.ctxt.gstate.path[-1][0], ps.MoveTo)):
            self.ctxt.gstate.path[-1][0] = ps.MoveTo(ps.Point(device_x, device_y))
        else:
            self.ctxt.gstate.path.append(ps.SubPath())
            self.ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(device_x, device_y)))

        # Update the currentpoint (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))

    def _cmd_vmoveto(self):
        """vmoveto: Vertical move to (equivalent to 0 dy rmoveto)"""
        if len(self.stack) < 1:
            raise CharStringError("vmoveto: stack underflow")

        dy = self.stack.pop()

        # Update current point in glyph space
        self.current_point = (self.current_point[0], self.current_point[1] + dy)

        # Skip path operations in width-only mode
        if self.width_only_mode:
            return

        # Skip path operations during flex accumulation - only track current_point
        if self.flex_active:
            return

        # Transform to device space and add to graphics state path
        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])

        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        # Per PLRM: consecutive movetos replace the previous moveto point
        if (self.ctxt.gstate.path and len(self.ctxt.gstate.path[-1]) == 1
                and isinstance(self.ctxt.gstate.path[-1][0], ps.MoveTo)):
            self.ctxt.gstate.path[-1][0] = ps.MoveTo(ps.Point(device_x, device_y))
        else:
            self.ctxt.gstate.path.append(ps.SubPath())
            self.ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(device_x, device_y)))

        # Update the currentpoint (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))

    def _cmd_rlineto(self):
        """rlineto: Relative line to"""
        if len(self.stack) < 2:
            raise CharStringError("rlineto: stack underflow")
        
        dy = self.stack.pop()
        dx = self.stack.pop()
        
        # Update current point in glyph space
        old_point = self.current_point
        self.current_point = (self.current_point[0] + dx, self.current_point[1] + dy)
        
        # Skip path operations in width-only mode
        if self.width_only_mode:
            return
        
        # Transform to device space and add to graphics state path
        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])
        
        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        self.ctxt.gstate.path[-1].append(ps.LineTo(ps.Point(device_x, device_y)))
        
        # Update the currentpoint (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))
    
    def _cmd_hlineto(self):
        """hlineto: Horizontal line to (equivalent to dx 0 rlineto)"""
        if len(self.stack) < 1:
            raise CharStringError("hlineto: stack underflow")
        
        dx = self.stack.pop()
        
        # Update current point in glyph space
        self.current_point = (self.current_point[0] + dx, self.current_point[1])
        
        # Skip path operations in width-only mode
        if self.width_only_mode:
            return
        
        # Transform to device space and add to graphics state path
        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])
        
        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        self.ctxt.gstate.path[-1].append(ps.LineTo(ps.Point(device_x, device_y)))
        
        # Update the currentpoint (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))
    
    def _cmd_vlineto(self):
        """vlineto: Vertical line to (equivalent to 0 dy rlineto)"""
        if len(self.stack) < 1:
            raise CharStringError("vlineto: stack underflow")
        
        dy = self.stack.pop()
        
        # Update current point in glyph space
        self.current_point = (self.current_point[0], self.current_point[1] + dy)
        
        # Skip path operations in width-only mode
        if self.width_only_mode:
            return
        
        # Transform to device space and add to graphics state path
        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])
        
        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        self.ctxt.gstate.path[-1].append(ps.LineTo(ps.Point(device_x, device_y)))
        
        # Update the currentpoint (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))
    
    def _cmd_rrcurveto(self):
        """rrcurveto: Relative curve to with relative control points"""
        if len(self.stack) < 6:
            raise CharStringError("rrcurveto: stack underflow")
        
        dy3 = self.stack.pop()
        dx3 = self.stack.pop()
        dy2 = self.stack.pop()
        dx2 = self.stack.pop()
        dy1 = self.stack.pop()
        dx1 = self.stack.pop()
        
        # Calculate absolute control points in glyph space
        x1 = self.current_point[0] + dx1
        y1 = self.current_point[1] + dy1
        x2 = x1 + dx2
        y2 = y1 + dy2
        x3 = x2 + dx3
        y3 = y2 + dy3
        
        # Update current point to end of curve
        self.current_point = (x3, y3)
        
        # Skip path operations in width-only mode
        if self.width_only_mode:
            return
        
        # Transform all control points to device space
        device_x1, device_y1 = self._transform_glyph_to_device_space(x1, y1)
        device_x2, device_y2 = self._transform_glyph_to_device_space(x2, y2)
        device_x3, device_y3 = self._transform_glyph_to_device_space(x3, y3)
        
        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        self.ctxt.gstate.path[-1].append(ps.CurveTo(
            ps.Point(device_x1, device_y1),
            ps.Point(device_x2, device_y2), 
            ps.Point(device_x3, device_y3)
        ))
        
        # Update the currentpoint to end of curve (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x3), float(device_y3))
    
    def _cmd_hvcurveto(self):
        """hvcurveto: Horizontal-vertical curve to"""
        if len(self.stack) < 4:
            raise CharStringError("hvcurveto: stack underflow")
        
        dy3 = self.stack.pop()
        dy2 = self.stack.pop()
        dx2 = self.stack.pop()
        dx1 = self.stack.pop()
        
        # Equivalent to dx1 0 dx2 dy2 0 dy3 rrcurveto
        x1 = self.current_point[0] + dx1
        y1 = self.current_point[1] + 0
        x2 = x1 + dx2
        y2 = y1 + dy2
        x3 = x2 + 0
        y3 = y2 + dy3
        
        # Update current point
        self.current_point = (x3, y3)
        
        # Skip path operations in width-only mode
        if self.width_only_mode:
            return
        
        # Transform all control points to device space
        device_x1, device_y1 = self._transform_glyph_to_device_space(x1, y1)
        device_x2, device_y2 = self._transform_glyph_to_device_space(x2, y2)
        device_x3, device_y3 = self._transform_glyph_to_device_space(x3, y3)
        
        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        self.ctxt.gstate.path[-1].append(ps.CurveTo(
            ps.Point(device_x1, device_y1),
            ps.Point(device_x2, device_y2), 
            ps.Point(device_x3, device_y3)
        ))
        
        # Update the currentpoint to end of curve (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x3), float(device_y3))
    
    def _cmd_vhcurveto(self):
        """vhcurveto: Vertical-horizontal curve to"""
        if len(self.stack) < 4:
            raise CharStringError("vhcurveto: stack underflow")
        
        dx3 = self.stack.pop()
        dy2 = self.stack.pop()
        dx2 = self.stack.pop()
        dy1 = self.stack.pop()
        
        # Equivalent to 0 dy1 dx2 dy2 dx3 0 rrcurveto
        x1 = self.current_point[0] + 0
        y1 = self.current_point[1] + dy1
        x2 = x1 + dx2
        y2 = y1 + dy2
        x3 = x2 + dx3
        y3 = y2 + 0
        
        # Update current point
        self.current_point = (x3, y3)
        
        # Skip path operations in width-only mode
        if self.width_only_mode:
            return
        
        # Transform all control points to device space
        device_x1, device_y1 = self._transform_glyph_to_device_space(x1, y1)
        device_x2, device_y2 = self._transform_glyph_to_device_space(x2, y2)
        device_x3, device_y3 = self._transform_glyph_to_device_space(x3, y3)
        
        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        self.ctxt.gstate.path[-1].append(ps.CurveTo(
            ps.Point(device_x1, device_y1),
            ps.Point(device_x2, device_y2), 
            ps.Point(device_x3, device_y3)
        ))
        
        # Update the currentpoint to end of curve (essential for relative operations)
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x3), float(device_y3))
    
    def _cmd_closepath(self):
        """closepath: Close current subpath"""
        # Skip path operations in width-only mode
        if self.width_only_mode:
            return
        
        # Add to current path directly (bypass PostScript operators to avoid stack issues)
        self.ctxt.gstate.path[-1].append(ps.ClosePath())
        
        # Note: Type 1 closepath does NOT update current point (unlike PostScript closepath)
        # So we don't modify currentpoint here
    
    def _cmd_endchar(self):
        """endchar: End character definition"""
        # This command ends the CharString execution and renders the glyph
        # Rendering method depends on PaintType: 0=fill, 2=stroke
        
        # Skip rendering operations in width-only mode
        if self.width_only_mode:
            return

        # Skip rendering operations in charpath mode (for charpath operator)
        # This allows path construction but prevents the final fill/stroke
        if hasattr(self.ctxt, '_charpath_mode') and self.ctxt._charpath_mode:
            return

        # Ensure we have a DisplayListBuilder
        if not hasattr(self.ctxt, 'display_list_builder'):
            self.ctxt.display_list_builder = DisplayListBuilder(self.ctxt.display_list)
        
        # Add path element
        self.ctxt.display_list_builder.add_graphics_operation(self.ctxt, self.ctxt.gstate.path)

        # Check PaintType in font dictionary to determine fill vs stroke
        paint_type = self.font_dict.val.get(b'PaintType', ps.Int(0)).val  # Default to 0 (fill)
        
        if paint_type == 2:
            # PaintType 2: Stroke the character outline
            device_color = color_space.convert_to_device_color(self.ctxt, self.ctxt.gstate.color, self.ctxt.gstate.color_space)
            stroke_op = ps.Stroke(device_color, self.ctxt.gstate)
            self.ctxt.display_list_builder.add_graphics_operation(self.ctxt, stroke_op)
        else:
            # PaintType 0 (or other values): Fill the character with non-zero winding rule
            device_color = color_space.convert_to_device_color(self.ctxt, self.ctxt.gstate.color, self.ctxt.gstate.color_space)
            fill_op = ps.Fill(device_color, ps.WINDING_NON_ZERO)
            self.ctxt.display_list_builder.add_graphics_operation(self.ctxt, fill_op)
        
        # Clear the path after rendering (like newpath)
        # Skip path clearing in charpath mode (for charpath operator)
        if not (hasattr(self.ctxt, '_charpath_mode') and self.ctxt._charpath_mode):
            self.ctxt.gstate.path = ps.Path()
    
    # Hint Commands (store hint information but don't affect path)
    
    def _cmd_hstem(self):
        """hstem: Horizontal stem hint"""
        if len(self.stack) < 2:
            raise CharStringError("hstem: stack underflow")
        
        dy = self.stack.pop()
        y = self.stack.pop()
        
        # Store hint information (could be used for font hinting in future)
        # For now, just consume the arguments
    
    def _cmd_vstem(self):
        """vstem: Vertical stem hint"""
        if len(self.stack) < 2:
            raise CharStringError("vstem: stack underflow")
        
        dx = self.stack.pop()
        x = self.stack.pop()
        
        # Store hint information (could be used for font hinting in future)
        # For now, just consume the arguments
    
    def _cmd_hstem3(self):
        """hstem3: Three horizontal stem hints"""
        if len(self.stack) < 6:
            raise CharStringError("hstem3: stack underflow")
        
        # Consume 6 arguments
        for _ in range(6):
            self.stack.pop()
    
    def _cmd_vstem3(self):
        """vstem3: Three vertical stem hints"""
        if len(self.stack) < 6:
            raise CharStringError("vstem3: stack underflow")
        
        # Consume 6 arguments
        for _ in range(6):
            self.stack.pop()
    
    def _cmd_dotsection(self):
        """dotsection: Hint for dot sections (i, j, !)"""
        # No arguments, just a hint marker
        pass
    
    # Subroutine Commands (not implemented - would require Subrs array access)
    
    def _calc_subr_bias(self, subr_count):
        """
        Calculate subroutine bias for Type 2-style CharStrings.

        Some Type 1 fonts (especially those converted from or compatible with
        CFF/Type 2) use biased subroutine indices. The bias depends on the
        number of subroutines in the Subrs array.

        Args:
            subr_count: Number of subroutines in the Subrs array

        Returns:
            The bias to add to raw subroutine indices
        """
        if subr_count < 1240:
            return 107
        elif subr_count < 33900:
            return 1131
        else:
            return 32768

    def _cmd_callsubr(self):
        """
        callsubr: Call CharString subroutine

        Subroutines share the current interpreter state (stack, current_point).
        After execution, any values the subroutine pushed remain on the stack
        for the caller to use.

        Note: Some fonts use Type 2-style biased subroutine indices even in
        Type 1 CharStrings. We detect this by checking if the raw index is
        negative and apply bias accordingly.
        """
        if len(self.stack) < 1:
            raise CharStringError("callsubr: stack underflow")

        raw_subr_index = int(self.stack.pop())

        # Use self.private which was passed to the constructor
        # (this is more reliable than re-fetching from font_dict)
        if self.private is None:
            raise CharStringError("callsubr: Private dictionary not available")

        # Get Subrs array from Private dictionary
        subrs = self.private.val.get(b'Subrs')
        if subrs is None:
            # Try with Name key
            subrs = self.private.val.get(ps.Name(b'Subrs'))
        if subrs is None:
            raise CharStringError("callsubr: Subrs array not found in Private dictionary")

        subr_count = len(subrs.val)

        # Apply subroutine bias if the raw index is negative or out of range
        # This handles fonts that use Type 2-style biased indices
        subr_index = raw_subr_index
        if subr_index < 0 or subr_index >= subr_count:
            bias = self._calc_subr_bias(subr_count)
            subr_index = raw_subr_index + bias

        # If still out of range, try mapping to valid Subrs
        # Some dvips-generated fonts have incomplete Subrs arrays but CharStrings
        # that reference higher indices. Map to Subrs[0] as a fallback for flex hints.
        if subr_index < 0 or subr_index >= subr_count:
            # For small Subrs arrays (typically 4 for flex hints), use Subrs[0]
            # which is usually the flex endpoint handler
            if subr_count <= 4 and subr_count > 0:
                subr_index = 0
            else:
                raise CharStringError(f"callsubr: subroutine index {raw_subr_index} (biased: {subr_index}) out of range (0-{subr_count-1})")

        charstring_obj = subrs.val[subr_index]

        if charstring_obj.TYPE == ps.T_STRING:
            # Convert string to bytes for CharString interpreter
            if isinstance(charstring_obj.val, str):
                encrypted_charstring = charstring_obj.val.encode('latin-1')
            else:
                encrypted_charstring = charstring_obj.byte_string()
        else:
            raise CharStringError(f"callsubr: subroutine {subr_index} is not a string type")

        # Execute the subroutine - it modifies our stack and current_point
        # Note: We must NOT clear the stack after execution, as subroutines
        # may leave values on the stack for the caller to use
        self.execute_charstring_for_width(encrypted_charstring)
    
    def _cmd_return(self):
        """return: Return from subroutine"""
        # No-op: callsubr executes subroutines inline, so return is implicit
    
    # Arithmetic Commands
    
    def _cmd_div(self):
        """div: Divide two numbers"""
        if len(self.stack) < 2:
            raise CharStringError("div: stack underflow")
        
        num2 = self.stack.pop()
        num1 = self.stack.pop()
        
        if num2 == 0:
            raise CharStringError("div: division by zero")
        
        result = num1 / num2
        self.stack.append(result)
    
    # OtherSubrs Commands - Flex hints (0, 1, 2) and hint replacement (3)

    def _cmd_callothersubr(self):
        """
        callothersubr: Call OtherSubrs procedure

        OtherSubrs are used for:
        - Flex hints (OtherSubrs 0, 1, 2): Render shallow curves
        - Hint replacement (OtherSubrs 3): Change stem hints mid-glyph

        Stack on entry: arg1 arg2 ... argN N othersubr# callothersubr
        The CharString stack has: [args...] N othersubr#
        """
        if len(self.stack) < 2:
            raise CharStringError("callothersubr: stack underflow")

        othersubr_num = int(self.stack.pop())
        num_args = int(self.stack.pop())

        # Pop arguments from CharString stack
        args = []
        for _ in range(num_args):
            if len(self.stack) < 1:
                raise CharStringError("callothersubr: not enough arguments")
            args.insert(0, self.stack.pop())  # Insert at front to preserve order

        if othersubr_num == 0:
            # OtherSubrs 0: EndFlex - draw the flex curves
            # Args: flex_depth (controls when to use curves vs lines)
            # Uses the 7 accumulated flex_points to draw two Bézier curves
            self._othersubr_end_flex(args)

        elif othersubr_num == 1:
            # OtherSubrs 1: StartFlex - begin flex hint accumulation
            # No args expected
            self._othersubr_start_flex()

        elif othersubr_num == 2:
            # OtherSubrs 2: AddFlex - add current point to flex list
            # No args expected
            self._othersubr_add_flex()

        elif othersubr_num == 3:
            # OtherSubrs 3: Hint replacement - change active hints
            # Args: none, but pushes 3 onto PS stack for compatibility
            self.ps_stack.append(3)

        else:
            # Unknown OtherSubr - push args back for compatibility
            for arg in args:
                self.ps_stack.append(arg)

    def _othersubr_start_flex(self):
        """OtherSubrs 1: Initialize flex hint accumulation"""
        self.flex_active = True
        self.flex_points = []

    def _othersubr_add_flex(self):
        """OtherSubrs 2: Add current point to flex point list"""
        if self.flex_active:
            # Store current point as a flex control point
            self.flex_points.append(self.current_point)
        # Push current point coordinates onto PS stack for later pop commands
        self.ps_stack.append(self.current_point[1])  # y first (will be popped second)
        self.ps_stack.append(self.current_point[0])  # x second (will be popped first)

    def _othersubr_end_flex(self, args):
        """
        OtherSubrs 0: End flex and draw the curves

        Flex uses 7 points to define two connected Bézier curves:
        - Point 0: Reference point (usually same as start)
        - Points 1-3: First Bézier curve (start, control1, control2, end=point3)
        - Points 4-6: Second Bézier curve (start=point3, control1, control2, end=point6)

        The flex_depth argument controls whether to draw curves or a straight line
        based on the deviation from a straight path.
        """
        self.flex_active = False

        # We need at least 7 points for flex
        if len(self.flex_points) < 7:
            # Not enough points - push final position and return
            if self.flex_points:
                final_pt = self.flex_points[-1]
                self.ps_stack.append(final_pt[1])
                self.ps_stack.append(final_pt[0])
            return

        # Extract the 7 flex points
        # Point indices: 0=reference, 1-3=first curve, 4-6=second curve (3 is shared endpoint)
        p0 = self.flex_points[0]  # Reference point
        p1 = self.flex_points[1]  # First curve control point 1
        p2 = self.flex_points[2]  # First curve control point 2
        p3 = self.flex_points[3]  # First curve endpoint / Second curve start
        p4 = self.flex_points[4]  # Second curve control point 1
        p5 = self.flex_points[5]  # Second curve control point 2
        p6 = self.flex_points[6]  # Second curve endpoint (final position)

        # Skip path operations in width-only mode
        if not self.width_only_mode:
            # Draw first Bézier curve: current -> p1 -> p2 -> p3
            device_p1 = self._transform_glyph_to_device_space(p1[0], p1[1])
            device_p2 = self._transform_glyph_to_device_space(p2[0], p2[1])
            device_p3 = self._transform_glyph_to_device_space(p3[0], p3[1])

            self.ctxt.gstate.path[-1].append(ps.CurveTo(
                ps.Point(device_p1[0], device_p1[1]),
                ps.Point(device_p2[0], device_p2[1]),
                ps.Point(device_p3[0], device_p3[1])
            ))

            # Draw second Bézier curve: p3 -> p4 -> p5 -> p6
            device_p4 = self._transform_glyph_to_device_space(p4[0], p4[1])
            device_p5 = self._transform_glyph_to_device_space(p5[0], p5[1])
            device_p6 = self._transform_glyph_to_device_space(p6[0], p6[1])

            self.ctxt.gstate.path[-1].append(ps.CurveTo(
                ps.Point(device_p4[0], device_p4[1]),
                ps.Point(device_p5[0], device_p5[1]),
                ps.Point(device_p6[0], device_p6[1])
            ))

            # Update graphics state currentpoint
            self.ctxt.gstate.currentpoint = ps.Point(float(device_p6[0]), float(device_p6[1]))

        # Update CharString current_point to final position
        self.current_point = p6

        # Push final coordinates onto PS stack for subsequent pop commands
        self.ps_stack.append(p6[1])  # y
        self.ps_stack.append(p6[0])  # x

        # Clear flex points
        self.flex_points = []

    def _cmd_pop(self):
        """pop: Transfer value from PostScript stack to CharString stack"""
        if len(self.ps_stack) < 1:
            # If PS stack is empty, push 0 as fallback (some fonts expect this)
            self.stack.append(0)
        else:
            self.stack.append(self.ps_stack.pop())

    def _cmd_setcurrentpoint(self):
        """setcurrentpoint: Set current point explicitly"""
        if len(self.stack) < 2:
            raise CharStringError("setcurrentpoint: stack underflow")
        
        y = self.stack.pop()
        x = self.stack.pop()
        
        # Set current point without adding to path
        # This is the character's internal coordinate system - don't update gstate.currentpoint
        self.current_point = (x, y)
    
    def _cmd_seac(self):
        """seac: Standard encoding accented character

        Builds a composite glyph from a base character and an accent character.
        Both characters are referenced by their position in StandardEncoding.

        Stack: asb adx ady bchar achar seac
        - asb: left sidebearing of accent character (used for positioning)
        - adx: x offset of accent from origin
        - ady: y offset of accent from origin
        - bchar: StandardEncoding index of base character
        - achar: StandardEncoding index of accent character
        """
        if len(self.stack) < 5:
            raise CharStringError("seac: stack underflow")

        achar = int(self.stack.pop())  # accent character code in StandardEncoding
        bchar = int(self.stack.pop())  # base character code in StandardEncoding
        ady = self.stack.pop()         # accent y displacement
        adx = self.stack.pop()         # accent x displacement
        asb = self.stack.pop()         # accent sidebearing

        # Get StandardEncoding from systemdict
        if self.ctxt is None or self.ctxt.d_stack is None:
            raise CharStringError("seac: no context available")

        systemdict = self.ctxt.d_stack[0]
        std_encoding = systemdict.val.get(b'StandardEncoding')
        if std_encoding is None:
            raise CharStringError("seac: StandardEncoding not found in systemdict")

        # Look up character names in StandardEncoding
        if bchar < 0 or bchar >= len(std_encoding.val):
            raise CharStringError(f"seac: bchar index {bchar} out of range")
        if achar < 0 or achar >= len(std_encoding.val):
            raise CharStringError(f"seac: achar index {achar} out of range")

        bchar_name = std_encoding.val[bchar]  # Name object for base character
        achar_name = std_encoding.val[achar]  # Name object for accent character

        # Get CharStrings dictionary from font
        charstrings = self.font_dict.val.get(b'CharStrings')
        if charstrings is None or charstrings.TYPE != ps.T_DICT:
            raise CharStringError("seac: CharStrings dictionary not found")

        # Convert Name objects to bytes for lookup
        bchar_key = bchar_name.val if isinstance(bchar_name.val, bytes) else bchar_name.val.encode('latin-1')
        achar_key = achar_name.val if isinstance(achar_name.val, bytes) else achar_name.val.encode('latin-1')

        # Get CharStrings for base and accent
        bchar_cs = charstrings.val.get(bchar_key)
        achar_cs = charstrings.val.get(achar_key)

        if bchar_cs is None:
            raise CharStringError(f"seac: base character '{bchar_key}' not found in CharStrings")
        if achar_cs is None:
            raise CharStringError(f"seac: accent character '{achar_key}' not found in CharStrings")

        # Get charstring bytes
        if bchar_cs.TYPE == ps.T_STRING:
            bchar_data = bchar_cs.val if isinstance(bchar_cs.val, bytes) else bchar_cs.byte_string()
        else:
            raise CharStringError("seac: base CharString is not a string")

        if achar_cs.TYPE == ps.T_STRING:
            achar_data = achar_cs.val if isinstance(achar_cs.val, bytes) else achar_cs.byte_string()
        else:
            raise CharStringError("seac: accent CharString is not a string")

        # Save current state
        saved_stack = self.stack.copy()
        saved_current_point = self.current_point
        saved_advance_width = self.advance_width
        saved_left_sidebearing = self.left_sidebearing

        # Execute base character CharString
        self.stack = []
        self.current_point = (0.0, 0.0)
        self._execute_component_charstring(bchar_data)

        # Get base character's left sidebearing for accent positioning
        base_lsb = self.left_sidebearing if self.left_sidebearing is not None else 0.0

        # Calculate accent origin: adx - asb + base_lsb, ady
        # The accent is positioned relative to the base character's origin
        accent_origin_x = adx - asb + base_lsb
        accent_origin_y = ady

        # Execute accent character CharString with offset origin
        self.stack = []
        self.current_point = (accent_origin_x, accent_origin_y)
        self._execute_component_charstring(achar_data)

        # Restore state (keep the advance width from original hsbw/sbw)
        self.stack = saved_stack
        self.current_point = saved_current_point
        self.advance_width = saved_advance_width
        self.left_sidebearing = saved_left_sidebearing

    def _execute_component_charstring(self, encrypted_charstring: bytes):
        """Execute a component CharString (for seac), adding paths without finalizing.

        Similar to execute_charstring_for_width but doesn't return width and
        doesn't expect to be the top-level execution.
        """
        # Decrypt and parse
        decrypted_data = self._decrypt_charstring(encrypted_charstring)
        commands = self._parse_charstring_commands(decrypted_data)

        # Execute commands - paths are added to ctxt.gstate.path
        for command_code, args in commands:
            # Skip endchar in component characters - we handle finalization ourselves
            if command_code == 14:  # endchar
                continue
            # Don't recurse into seac within seac (prevent infinite recursion)
            if command_code == (12, 6):  # seac
                continue
            self._execute_type1_command(command_code, args)


    def _transform_glyph_to_device_space(self, glyph_x: float, glyph_y: float) -> Tuple[float, float]:
        """Transform coordinates from character space to device space
        
        Correct PostScript coordinate transformation:
        1. Character space coordinates × FontMatrix = User space coordinates
        2. User space coordinates × CTM = Device space coordinates  
        3. Position relative to currentpoint (stored in device space)
        """
        
        # 1. Apply FontMatrix to transform character space → user space
        font_matrix = self.font_dict.val.get(b'FontMatrix')
        if font_matrix and font_matrix.TYPE == ps.T_ARRAY:
            # Transform character space coordinates to user space
            user_x, user_y = _transform_point(font_matrix, glyph_x, glyph_y)
        else:
            # No FontMatrix - use character coordinates directly (should not happen in Type 1)
            user_x, user_y = glyph_x, glyph_y
        
        # 2. Apply CTM to transform user space → device space  
        device_rel_x, device_rel_y = _transform_delta(self.ctxt.gstate.CTM, user_x, user_y)
        
        # 3. Position relative to the original currentpoint from the show operation
        # Use the stored show_origin instead of the current (possibly modified) currentpoint
        if self.show_origin is not None:
            device_x = device_rel_x + self.show_origin.x  
            device_y = device_rel_y + self.show_origin.y

            # self.show_origin.x = device_x
            # self.show_origin.y = device_y
        else:
            device_x = device_rel_x  
            device_y = device_rel_y
        
        return device_x, device_y


class CharStringError(Exception):
    """Exception raised during CharString decryption or execution"""
    pass


def charstring_to_width(encrypted_charstring: bytes, ctxt, private_dict: ps.Dict, font_dict: ps.Dict, width_only: bool = False) -> Optional[float]:
    """
    Convenience function: Execute CharString and return character width in user space

    This is the main entry point for text rendering operators (show, stringwidth, etc.)
    Path operations are added directly to ctxt.gstate.path during execution (unless width_only=True).

    Args:
        encrypted_charstring: Encrypted CharString data from font CharStrings dictionary
        ctxt: PostScript context for graphics state access
        private_dict: Font's Private dictionary containing hinting parameters
        font_dict: Font dictionary containing FontMatrix
        width_only: If True, skip path operations (for stringwidth)

    Returns:
        character_width: Character advance width in user space, or None if failed

    Raises:
        CharStringError: On decryption or execution failures
    """
    interpreter = CharStringInterpreter(ctxt, private_dict, font_dict, width_only_mode=width_only)
    raw_width = interpreter.execute_charstring_for_width(encrypted_charstring)
    
    if raw_width is not None:
        # Apply FontMatrix to transform character space → user space
        font_matrix = font_dict.val.get(b'FontMatrix')
        if font_matrix and font_matrix.TYPE == ps.T_ARRAY:
            font_matrix_values = [m.val for m in font_matrix.val]
            user_width = raw_width * font_matrix_values[0]  # Assuming horizontal text
            return user_width
    
    return raw_width