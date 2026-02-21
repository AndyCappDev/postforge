# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Graphics Classes Module

This module contains graphics and display list PostScript types that manage
rendering operations, path construction, and graphics state. These types 
handle PostScript's graphics model including paths, fills, strokes, and
image rendering elements.
"""

import copy
import math
import time
from typing import Union

from .. import color_space

# Import base classes and constants
from .base import PSObject
from .constants import (
    LINE_CAP_BUTT, LINE_JOIN_MITER, WINDING_NON_ZERO, T_GSTATE
)

# Import composite and context types for dependencies
from .composite import Array
from .context import Stack


# Graphics classes extracted from legacy file

# GSTATE
class GraphicsState(PSObject):
    def __init__(self, ctxt_id: int) -> None:
        super().__init__(None)

        self.created = time.monotonic_ns()  # creation time for this composite object

        self.CTM = Array(ctxt_id)  # the current transformation matrix
        self.iCTM = Array(ctxt_id)  # the inverse of the current transformation matrix
        # it is recalculated every time the CTM changes

        self.currentpoint = None  # a Point when not None
        self.path = Path()  # a list of SubPaths
        self.clip_currentpoint = None
        self.clip_path = Path()
        self.clip_path_stack = Stack(10)
        self.color_space = ["DeviceGray"]  # default should be DeviceGray
        self.color = [0.0]                 # Single gray component for black per PLRM
        self.transfer_function = None  # PostScript procedure for transfer function
        self.black_generation = None   # PostScript procedure for black generation (CMYK)
        self.undercolor_removal = None # PostScript procedure for undercolor removal (CMYK)
        self.font = None  # a dictionary
        self.line_width = 1.0
        self.line_cap = LINE_CAP_BUTT
        self.line_join = LINE_JOIN_MITER
        self.miter_limit = 10.0
        self.dash_pattern = [[], 0]
        self.stroke_adjust = False
        self.overprint = False
        self.flatness = 1.0
        self.bbox = None  # user path bounding box set by setbbox
        self.halftone = None  # halftone dictionary set by sethalftone
        self.screen_params = None        # (freq, angle, proc_or_dict) from setscreen
        self.color_screen_params = None  # ((freq,ang,proc), ...) x4 from setcolorscreen
        self.color_transfer = None       # (red_proc, green_proc, blue_proc, gray_proc)
        self.color_rendering = None      # Dict from setcolorrendering
        self.page_device = {}

        # Clipping path version tracking for display list optimization
        self.clip_path_version = 0  # Increments on any clip path change

        # Pattern support - current pattern dictionary when color space is Pattern
        self._current_pattern = None  # Pattern dictionary set by setpattern

        self.saved = False  # True means this gstate was saved using the save
                            # as opposed to the gsave operator

    def update_clipping_path(self, new_clip_path, winding_rule: int):
        """
        Update clipping path and increment version for display list tracking.
        
        Args:
            new_clip_path: New clipping path (already in device coordinates)
            winding_rule: Winding rule used to create this clipping path (for reference)
        """
    
        self.clip_path = new_clip_path
        self.clip_path_version += 1
        # Note: We don't store winding_rule - it was used during construction only

    # Attributes that need deep copy (mutable containers that could be modified)
    _DEEPCOPY_ATTRS = frozenset({
        'CTM', 'iCTM', 'path', 'clip_path', 'clip_path_stack',
        'color', 'color_space', 'dash_pattern', 'currentpoint'
    })

    _ALL_ATTRS = (
        'val', 'access', 'attrib', 'is_composite', 'is_global',
        'created', 'CTM', 'iCTM', 'currentpoint', 'path',
        'clip_currentpoint', 'clip_path', 'clip_path_stack',
        'color_space', 'color', 'transfer_function', 'black_generation',
        'undercolor_removal', 'font', 'line_width', 'line_cap', 'line_join',
        'miter_limit', 'dash_pattern', 'stroke_adjust', 'overprint',
        'flatness', 'bbox', 'halftone', 'screen_params',
        'color_screen_params', 'color_transfer', 'color_rendering',
        'page_device', 'clip_path_version',
        '_current_pattern', 'saved'
    )

    def copy(self):  # -> GraphicsState
        """
        Optimized copy for gsave/save - shallow copy most attrs, deep copy only mutables.

        Attributes in _DEEPCOPY_ATTRS are deep copied; all others are shallow copied.
        When adding new attributes to GraphicsState, add to _DEEPCOPY_ATTRS if mutable.
        Also add any new attributes to _ALL_ATTRS.
        """
        new_gs = object.__new__(GraphicsState)
        for attr in GraphicsState._ALL_ATTRS:
            value = getattr(self, attr)
            if attr in GraphicsState._DEEPCOPY_ATTRS:
                setattr(new_gs, attr, copy.deepcopy(value))
            else:
                setattr(new_gs, attr, value)
        return new_gs


class DisplayList(list):
    def __init__(self, width: int = 0, height: int = 0) -> None:
        super().__init__()

        self.width = width
        self.height = height

    """
    This is a list of elements like Paths, Fills, Strokes, etc...
    A Path is a list of SubPaths.
    SubPaths consist of path construction operators like, MoveTo, LineTo, CurveTo, ClosePath, etc...
    """


# Path Elements
class Path(list):
    def __init__(self) -> None:
        super().__init__()


class SubPath(list):
    def __init__(self) -> None:
        super().__init__()



class Fill:
    def __init__(self, device_color: list, winding_rule: int) -> None:
        # Store device-ready color values as primitive Python floats
        self.color = device_color
        self.winding_rule = winding_rule


class PatternFill:
    """Display list element for pattern-filled areas."""
    def __init__(self, pattern_dict, winding_rule: int, gs: "GraphicsState",
                 underlying_color: list = None) -> None:
        """
        Create a pattern fill element.

        Args:
            pattern_dict: Pattern dictionary with Implementation entry
            winding_rule: WINDING_NON_ZERO or WINDING_EVEN_ODD
            gs: Current graphics state (for CTM)
            underlying_color: For uncolored patterns (PaintType 2), the underlying
                             color components in device color space
        """
        self.pattern_dict = pattern_dict
        self.winding_rule = winding_rule
        self.underlying_color = underlying_color or []

        # Store CTM at time of fill for proper pattern placement
        ctm = gs.CTM.val
        self.ctm = (ctm[0].val, ctm[1].val, ctm[2].val, ctm[3].val, ctm[4].val, ctm[5].val)


class Stroke:
    def __init__(self, device_color: list, gs: "GraphicsState") -> None:
        # Store device-ready color values as primitive Python floats
        self.color = device_color

        # Store line width and dash pattern in USER SPACE
        # The CTM is also stored so the renderer can apply the transformation
        # This allows correct anisotropic line widths when X and Y scales differ
        self.line_width = gs.line_width

        self.line_cap = gs.line_cap
        self.line_join = gs.line_join
        self.miter_limit = gs.miter_limit

        # Store dash pattern in user space
        user_dashes, user_offset = gs.dash_pattern
        self.dash_pattern = [list(user_dashes), user_offset]

        self.stroke_adjust = gs.stroke_adjust

        # Store CTM for correct anisotropic stroke rendering
        # The renderer will use this to transform lines correctly
        ctm = gs.CTM.val
        self.ctm = (ctm[0].val, ctm[1].val, ctm[2].val, ctm[3].val, ctm[4].val, ctm[5].val)


class ErasePage:
    def __init__(self, gs: "GraphicsState") -> None:
        pass


class ShowPage:
    def __init__(self, gs: "GraphicsState") -> None:
        pass


class ClipElement:
    def __init__(self, gs: "GraphicsState", winding_rule: int = WINDING_NON_ZERO, is_initclip: bool = False):
        """
        Display list element for clipping operations.
        
        Stores the current clipping path (already in device coordinates)
        and winding rule to be applied by rendering devices via Cairo.
        
        Args:
            gs: Graphics state containing clipping path
            winding_rule: WINDING_NON_ZERO or WINDING_EVEN_ODD
        """
        # Store copy of clipping path (already in device coordinates)
        self.path = copy.deepcopy(gs.clip_path) if gs.clip_path else Path()
        # Store winding rule in effect when clip was called
        self.winding_rule = winding_rule
        self.is_initclip = is_initclip


# SubPath Elements
class Point(object):
    def __init__(self, x: Union[int, float], y: Union[int, float]) -> None:
        self.x = x
        self.y = y


class MoveTo(object):
    def __init__(self, p: Point) -> None:
        self.p = p


class LineTo(object):
    def __init__(self, p: Point) -> None:
        self.p = p


class CurveTo(object):
    def __init__(self, p1: Point, p2: Point, p3: Point) -> None:
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3


class ClosePath(object):
    def __init__(self):
        pass


# Glyph Bitmap Cache Display List Elements
class GlyphRef:
    """Cache hit: lightweight reference to a cached glyph bitmap.

    On cache hit, the interpreter emits this instead of running BuildGlyph.
    The renderer blits the cached Cairo surface at the given position.
    """
    __slots__ = ('cache_key', 'position_x', 'position_y')

    def __init__(self, cache_key, position_x, position_y):
        self.cache_key = cache_key
        self.position_x = position_x
        self.position_y = position_y


class GlyphStart:
    """Cache miss: marks beginning of glyph display elements for capture.

    On cache miss, the interpreter emits this before BuildGlyph runs.
    The renderer uses this to begin tracking glyph elements for bitmap capture.
    """
    __slots__ = ('cache_key', 'position_x', 'position_y')

    def __init__(self, cache_key, position_x, position_y):
        self.cache_key = cache_key
        self.position_x = position_x
        self.position_y = position_y


class GlyphEnd:
    """Cache miss: marks end of glyph display elements.

    The renderer uses this to finalize bitmap capture and store in cache.
    """
    __slots__ = ()


# Image Display List Elements for PostScript image processing operators
class ImageElement:
    """PostScript image display list element with device space coordinate handling"""
    
    def __init__(self, device_color: list, gs: "GraphicsState", image_type: str):
        # Copy relevant graphics state at time of image call - PRIMITIVE VALUES ONLY
        self.CTM = [elem.val for elem in gs.CTM.val]  # Extract primitive values
        self.color_space = gs.color_space.copy()      # Primitive list 
        self.color = device_color                     # Device-ready color values
        self.image_type = image_type                  # String
        
        # Image-specific parameters (PRIMITIVE VALUES ONLY)
        self.width = 0                    # int
        self.height = 0                   # int  
        self.bits_per_component = 0       # int
        self.components = 0               # int
        self.image_matrix = None          # list of floats [a, b, c, d, tx, ty]
        self.ctm = None                   # the current transformation matrix
        self.ictm = None                  # the current inverse transformation matrix
        self.decode_array = None          # list of floats
        self.interpolate = False          # boolean
        self.sample_data = None           # bytes - ALL IMAGE DATA STORED HERE
        
        # imagemask specific
        self.polarity = None              # boolean
        
        # colorimage specific  
        self.color_space_name = None      # string
        self.multi_data_sources = False   # boolean
    
    def get_device_image_matrix(self):
        """
        Calculate final image matrix for device space rendering.
        
        PostScript image matrices expect user space input, but PostForge display list
        contains device space coordinates. Pre-compose matrices to bridge this gap:
        
        Final Matrix = PostScript Image Matrix ∘ Inverse CTM
        """
        inverse_ctm = self._calculate_inverse_matrix(self.CTM)
        return self._matrix_multiply(self.image_matrix, inverse_ctm)
    
    def _calculate_inverse_matrix(self, matrix):
        """Calculate inverse of 6-element matrix [a b c d tx ty] - primitive values"""
        a, b, c, d, tx, ty = matrix  # Already primitive values
        determinant = a * d - b * c
        
        if abs(determinant) < 1e-10:
            raise ValueError("Singular matrix - cannot invert")
        
        inv_det = 1.0 / determinant
        return [
            d * inv_det,                    # a'
            -b * inv_det,                   # b'
            -c * inv_det,                   # c'
            a * inv_det,                    # d'
            (c * ty - d * tx) * inv_det,    # tx'
            (b * tx - a * ty) * inv_det     # ty'
        ]
    
    def _matrix_multiply(self, m1, m2):
        """Multiply two PostScript 6-element matrices: m1 ∘ m2"""
        a1, b1, c1, d1, tx1, ty1 = m1
        a2, b2, c2, d2, tx2, ty2 = m2
        
        return [
            a1*a2 + b1*c2,                # a
            a1*b2 + b1*d2,                # b
            c1*a2 + d1*c2,                # c
            c1*b2 + d1*d2,                # d
            tx1*a2 + ty1*c2 + tx2,        # tx
            tx1*b2 + ty1*d2 + ty2         # ty
        ]


class ImageMaskElement(ImageElement):
    """Specialized element for imagemask operator"""
    
    def __init__(self, device_color: list, gs: "GraphicsState"):
        super().__init__(device_color, gs, 'imagemask')
        self.bits_per_component = 1    # Always 1 for imagemask
        self.components = 1            # Always 1 for imagemask


class ColorImageElement(ImageElement):
    """Specialized element for colorimage operator"""
    
    def __init__(self, device_color: list, gs: "GraphicsState", ncomp: int):
        super().__init__(device_color, gs, 'colorimage')
        self.components = ncomp
        # Color space determined by ncomp: 1=Gray, 3=RGB, 4=CMYK
        color_space_map = {1: 'DeviceGray', 3: 'DeviceRGB', 4: 'DeviceCMYK'}
        self.color_space_name = color_space_map.get(ncomp, 'DeviceGray')


# Text Rendering Elements for Font System

# GlyphPathElement removed - now using standard PostScript Path/Fill/Stroke elements

class AxialShadingFill:
    """Display list element for Type 2 axial (linear) gradient shading."""
    __slots__ = ('x0', 'y0', 'x1', 'y1', 'color_stops', 'extend_start', 'extend_end', 'ctm', 'bbox')

    def __init__(self, x0, y0, x1, y1, color_stops, extend_start, extend_end, ctm, bbox=None):
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.color_stops = color_stops  # list of (t, (r, g, b)) where t in [0,1]
        self.extend_start = extend_start
        self.extend_end = extend_end
        self.ctm = ctm  # tuple of 6 floats (a, b, c, d, tx, ty)
        self.bbox = bbox  # (x0, y0, x1, y1) in user space or None


class RadialShadingFill:
    """Display list element for Type 3 radial (circular) gradient shading."""
    __slots__ = ('x0', 'y0', 'r0', 'x1', 'y1', 'r1', 'color_stops', 'extend_start', 'extend_end', 'ctm', 'bbox')

    def __init__(self, x0, y0, r0, x1, y1, r1, color_stops, extend_start, extend_end, ctm, bbox=None):
        self.x0 = x0
        self.y0 = y0
        self.r0 = r0
        self.x1 = x1
        self.y1 = y1
        self.r1 = r1
        self.color_stops = color_stops  # list of (t, (r, g, b)) where t in [0,1]
        self.extend_start = extend_start
        self.extend_end = extend_end
        self.ctm = ctm  # tuple of 6 floats (a, b, c, d, tx, ty)
        self.bbox = bbox  # (x0, y0, x1, y1) in user space or None


class MeshShadingFill:
    """Display list element for Type 4/5 triangle mesh shading."""
    __slots__ = ('triangles', 'ctm', 'bbox')

    def __init__(self, triangles, ctm, bbox=None):
        self.triangles = triangles  # list of (v0, v1, v2) where each v is ((x,y), (r,g,b))
        self.ctm = ctm
        self.bbox = bbox


class PatchShadingFill:
    """Display list element for Type 6/7 Coons/tensor-product patch shading."""
    __slots__ = ('patches', 'ctm', 'bbox')

    def __init__(self, patches, ctm, bbox=None):
        self.patches = patches  # list of (points, colors) — 12 or 16 control points + 4 RGB colors
        self.ctm = ctm
        self.bbox = bbox


class FunctionShadingFill:
    """Display list element for Type 1 function-based shading.

    The function is pre-rasterized to an ARGB32 pixel buffer during shfill
    processing.  The renderer paints it as an image with the stored matrix.
    """
    __slots__ = ('pixel_data', 'width', 'height', 'matrix', 'ctm', 'bbox')

    def __init__(self, pixel_data, width, height, matrix, ctm, bbox=None):
        self.pixel_data = pixel_data  # bytearray of ARGB32 pixels (BGRA on LE)
        self.width = width            # raster width
        self.height = height          # raster height
        self.matrix = matrix          # 6-float tuple: maps raster coords → domain coords
        self.ctm = ctm                # 6-float tuple: user → device transform
        self.bbox = bbox              # optional (x0, y0, x1, y1) clip in user space


class TextObj:
    """
    Display list element for text rendering (TextObjs mode).

    Emitted by interpreter when TextRenderingMode is /TextObjs.
    Device handles all rendering (Cairo text, font embedding, bitmap+ActualText).

    Used for PDF output with native, searchable text instead of path-based glyphs.
    """
    __slots__ = ('text', 'start_x', 'start_y', 'font_dict', 'font_name',
                 'font_size', 'color', 'color_space', 'ctm', 'font_matrix')

    def __init__(self, text: bytes, start_x: float, start_y: float,
                 font_dict, font_name: bytes, font_size: float,
                 color: tuple, color_space: list, ctm: list,
                 font_matrix: list = None):
        """
        Initialize TextObj for text rendering.

        Args:
            text: Original string bytes from show operation
            start_x: Starting X position in device space
            start_y: Starting Y position in device space
            font_dict: PostScript font dictionary (read-only reference)
            font_name: Font name bytes (e.g., b'Times-Roman')
            font_size: Effective size in device space
            color: Device color tuple (RGB floats 0.0-1.0)
            color_space: Color space specification list
            ctm: CTM at render time (list of 6 floats)
            font_matrix: User-space font matrix (6 floats, FontMatrix scaled
                         to point units) for non-uniform scaling support
        """
        self.text = text                # Original string bytes
        self.start_x = start_x          # Device space X
        self.start_y = start_y          # Device space Y
        self.font_dict = font_dict      # PostScript font dictionary (read-only reference)
        self.font_name = font_name      # e.g., b'Times-Roman'
        self.font_size = font_size      # Device space size
        self.color = color              # Device color tuple
        self.color_space = color_space  # Color space specification
        self.ctm = ctm                  # CTM for transforms
        self.font_matrix = font_matrix  # User-space font matrix (point units)


# Keep TextElement as alias for backward compatibility
TextElement = TextObj


# ActualText Display List Elements for PDF searchability of Type 3 fonts
class ActualTextStart:
    """Marks beginning of a searchable text span in PDF output.

    When rendering Type 3 fonts in TextObjs mode, the interpreter executes
    BuildChar normally (paths/fills/bitmaps + glyph cache) but brackets the
    output with ActualText markers. The PDF injector creates invisible text
    operators (text render mode 3) for searchability.
    """
    __slots__ = ('unicode_text', 'start_x', 'start_y', 'font_size', 'ctm',
                 'font_matrix', 'font_bbox', 'visual_start_x', 'visual_width',
                 'advance_width')

    def __init__(self, unicode_text: str, start_x: float, start_y: float,
                 font_size: float, ctm: list, font_matrix: list = None,
                 font_bbox: list = None, visual_start_x: float = None,
                 visual_width: float = 0.0, advance_width: float = 0.0):
        self.unicode_text = unicode_text
        self.start_x = start_x
        self.start_y = start_y
        self.font_size = font_size
        self.ctm = ctm
        self.font_matrix = font_matrix
        self.font_bbox = font_bbox
        self.visual_start_x = visual_start_x
        self.visual_width = visual_width
        self.advance_width = advance_width


class ActualTextEnd:
    """Marks end of a searchable text span in PDF output."""
    __slots__ = ()