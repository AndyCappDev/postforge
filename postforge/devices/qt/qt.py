# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Qt Interactive Display Device

This device renders PostScript graphics to an interactive Qt window with:
- High-res rendering at 300 DPI internally
- Fit-to-window display with zoom/pan support
- Live rendering updates after paint operations
- Pause on showpage waiting for user input

Keyboard shortcuts:
- +/= : Zoom in
- -   : Zoom out
- 0   : Reset to fit-to-window
- Arrow keys: Pan when zoomed
- Q/Escape: Close window
"""

import cairo
import time

# Anti-aliasing mode for Cairo rendering (also used by glyph bitmap cache).
# Options: cairo.ANTIALIAS_NONE, ANTIALIAS_FAST, ANTIALIAS_GOOD,
#          ANTIALIAS_BEST, ANTIALIAS_GRAY, ANTIALIAS_SUBPIXEL
ANTIALIAS_MODE = cairo.ANTIALIAS_FAST

ANTIALIAS_MAP = {
    "none": cairo.ANTIALIAS_NONE,
    "fast": cairo.ANTIALIAS_FAST,
    "good": cairo.ANTIALIAS_GOOD,
    "best": cairo.ANTIALIAS_BEST,
    "gray": cairo.ANTIALIAS_GRAY,
    "subpixel": cairo.ANTIALIAS_SUBPIXEL,
}


def _get_antialias_mode(pd):
    if b"AntiAliasMode" in pd:
        return ANTIALIAS_MAP.get(pd[b"AntiAliasMode"].python_string(), ANTIALIAS_MODE)
    return ANTIALIAS_MODE

from ...core import types as ps
from ..common.cairo_renderer import render_display_list

# Check for PySide6 availability
try:
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter, QKeyEvent, QWheelEvent, QMouseEvent, QCursor
    PYSIDE6_AVAILABLE = True
except ImportError:
    PYSIDE6_AVAILABLE = False
    # Define dummy classes to avoid NameError when PySide6 not available
    QWidget = object
    QMainWindow = object
    QKeyEvent = object
    QWheelEvent = object
    QMouseEvent = object
    Qt = None

# Global state for Qt application and window
_app = None
_window = None
_canvas = None
_surface = None
_qimage = None
_render_dpi = 300  # High-res internal rendering
_ctxt = None  # Reference to PostScript context for quit signaling
_quit_sent = False  # Ensure quit is only pushed once

# View state
_zoom_level = 1.0
_pan_x = 0
_pan_y = 0
_waiting_for_key = False
_key_pressed = False
_window_closed = False
_user_advanced = False  # Track if user pressed key to advance (for auto-quit on last page)
_busy_mode = False  # Track busy state for cursor restoration on window re-entry

# Page dimensions (device space)
_page_width = 612
_page_height = 792


class PostForgeCanvas(QWidget if PYSIDE6_AVAILABLE else object):
    """Custom widget for rendering PostScript output with zoom/pan support."""

    def __init__(self):
        if not PYSIDE6_AVAILABLE:
            return
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(400, 300)
        self.setMouseTracking(True)
        self._dragging = False
        self._last_mouse_pos = None

    def paintEvent(self, event):
        """Paint the rendered image to the widget."""
        global _qimage
        if _qimage is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        # Calculate fit-to-window scale
        scale_x = self.width() / _qimage.width()
        scale_y = self.height() / _qimage.height()
        fit_scale = min(scale_x, scale_y)

        # Apply zoom and pan
        effective_scale = fit_scale * _zoom_level

        # Center the image when not zoomed or partially zoomed
        img_width = _qimage.width() * effective_scale
        img_height = _qimage.height() * effective_scale
        offset_x = (self.width() - img_width) / 2 if img_width < self.width() else 0
        offset_y = (self.height() - img_height) / 2 if img_height < self.height() else 0

        painter.translate(offset_x - _pan_x, offset_y - _pan_y)
        painter.scale(effective_scale, effective_scale)
        painter.drawImage(0, 0, _qimage)

    def wheelEvent(self, event: QWheelEvent):
        """Handle mouse wheel for zooming, centered on mouse position."""
        global _zoom_level, _pan_x, _pan_y

        if _qimage is None:
            return

        # Get mouse position
        mouse_x = event.position().x()
        mouse_y = event.position().y()

        # Calculate current transform parameters
        scale_x = self.width() / _qimage.width()
        scale_y = self.height() / _qimage.height()
        fit_scale = min(scale_x, scale_y)
        old_effective_scale = fit_scale * _zoom_level

        img_width = _qimage.width() * old_effective_scale
        img_height = _qimage.height() * old_effective_scale
        old_offset_x = (self.width() - img_width) / 2 if img_width < self.width() else 0
        old_offset_y = (self.height() - img_height) / 2 if img_height < self.height() else 0

        # Calculate image point under mouse cursor
        img_x = (mouse_x - old_offset_x + _pan_x) / old_effective_scale
        img_y = (mouse_y - old_offset_y + _pan_y) / old_effective_scale

        # Apply zoom - max zoom is 1:1 pixel ratio (native resolution)
        max_zoom = 1.0 / fit_scale  # When effective_scale = 1.0, we're at native res
        delta = event.angleDelta().y()
        if delta > 0:
            _zoom_level = min(_zoom_level * 1.25, max_zoom)
        else:
            _zoom_level = max(_zoom_level / 1.25, 0.1)

        # Calculate new transform parameters
        new_effective_scale = fit_scale * _zoom_level
        new_img_width = _qimage.width() * new_effective_scale
        new_img_height = _qimage.height() * new_effective_scale
        new_offset_x = (self.width() - new_img_width) / 2 if new_img_width < self.width() else 0
        new_offset_y = (self.height() - new_img_height) / 2 if new_img_height < self.height() else 0

        # Adjust pan so the same image point stays under the mouse
        _pan_x = (img_x * new_effective_scale) + new_offset_x - mouse_x
        _pan_y = (img_y * new_effective_scale) + new_offset_y - mouse_y

        self.update()

    def keyPressEvent(self, event: QKeyEvent):
        """Handle keyboard shortcuts for zoom/pan and navigation."""
        global _zoom_level, _pan_x, _pan_y, _key_pressed, _window_closed
        key = event.key()

        # View control keys - should NOT advance to next page
        view_control_keys = (
            Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus, Qt.Key_0,
            Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down
        )

        if key in (Qt.Key_Plus, Qt.Key_Equal):
            # Calculate max zoom (1:1 pixel ratio)
            if _qimage is not None:
                scale_x = self.width() / _qimage.width()
                scale_y = self.height() / _qimage.height()
                fit_scale = min(scale_x, scale_y)
                max_zoom = 1.0 / fit_scale
            else:
                max_zoom = 10.0
            _zoom_level = min(_zoom_level * 1.25, max_zoom)
        elif key == Qt.Key_Minus:
            _zoom_level = max(_zoom_level / 1.25, 0.1)
        elif key == Qt.Key_0:
            _zoom_level = 1.0
            _pan_x = _pan_y = 0
        elif key == Qt.Key_Left:
            _pan_x -= 50
        elif key == Qt.Key_Right:
            _pan_x += 50
        elif key == Qt.Key_Up:
            _pan_y -= 50
        elif key == Qt.Key_Down:
            _pan_y += 50
        elif key in (Qt.Key_Q, Qt.Key_Escape):
            _window_closed = True
            if _window:
                _window.close()
            return
        elif _waiting_for_key:
            # Only non-control keys advance to next page
            _key_pressed = True

        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Handle double-click to reset zoom and pan."""
        global _zoom_level, _pan_x, _pan_y
        if event.button() == Qt.LeftButton:
            _zoom_level = 1.0
            _pan_x = _pan_y = 0
            self.update()

    def mousePressEvent(self, event: QMouseEvent):
        """Handle mouse press to start panning."""
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._last_mouse_pos = event.position()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event: QMouseEvent):
        """Handle mouse release to stop panning."""
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self._last_mouse_pos = None
            # Unset widget cursor to let override cursor show through
            self.unsetCursor()

    def mouseMoveEvent(self, event: QMouseEvent):
        """Handle mouse move for panning."""
        global _pan_x, _pan_y
        if self._dragging and self._last_mouse_pos is not None:
            pos = event.position()
            delta_x = pos.x() - self._last_mouse_pos.x()
            delta_y = pos.y() - self._last_mouse_pos.y()
            _pan_x -= delta_x
            _pan_y -= delta_y
            self._last_mouse_pos = pos
            self.update()



class PostForgeWindow(QMainWindow if PYSIDE6_AVAILABLE else object):
    """Main window for PostForge interactive display."""

    def __init__(self):
        if not PYSIDE6_AVAILABLE:
            return
        super().__init__()
        self.setWindowTitle("PostForge")
        self._canvas = PostForgeCanvas()
        self.setCentralWidget(self._canvas)
        self.resize(800, 600)

    def closeEvent(self, event):
        """Handle window close event."""
        global _window_closed, _key_pressed, _busy_mode
        _window_closed = True
        _key_pressed = True  # Unblock any waiting
        _busy_mode = False
        event.accept()
        # Exit immediately - interpreter may be blocked waiting for input
        # Use os._exit() to avoid exception in Qt event handler
        import os
        print()  # Clean up partial prompt line
        os._exit(0)


def _ensure_app():
    """Ensure Qt application exists."""
    global _app
    if _app is None:
        _app = QApplication.instance()
        if _app is None:
            _app = QApplication([])
    return _app


def _ensure_window(page_width=None, page_height=None, image_width=None, image_height=None):
    """
    Ensure window exists and optionally resize it based on page aspect ratio.

    If the rendered image is smaller than what would fill the default window,
    the window shrinks to fit the image at 1:1 pixel size instead of enlarging it.

    Width is capped at 60% of screen width and height at 85% of screen height,
    so landscape content doesn't produce overly wide windows while portrait
    content still uses most of the vertical space.

    Args:
        page_width: Page width (any units, used for aspect ratio)
        page_height: Page height (any units, used for aspect ratio)
        image_width: Rendered image width in pixels
        image_height: Rendered image height in pixels
    """
    global _window, _canvas, _busy_mode
    _ensure_app()

    first_window = _window is None
    if first_window:
        _window = PostForgeWindow()
        _canvas = _window._canvas

    if page_width and page_height:
        # Get available screen geometry
        screen = _app.primaryScreen()
        available = screen.availableGeometry()
        screen_width = available.width()
        screen_height = available.height()

        # Calculate maximum window size — 60% width, 85% height
        max_width = int(screen_width * 0.60)
        max_height = int(screen_height * 0.85)

        if image_width and image_height:
            # Use exact image pixel dimensions, capped at max screen size
            win_width = image_width
            win_height = image_height

            # Scale down proportionally if image exceeds max screen size
            if win_width > max_width or win_height > max_height:
                scale = min(max_width / win_width, max_height / win_height)
                win_width = int(win_width * scale)
                win_height = int(win_height * scale)
        else:
            # No image dimensions available, fit page aspect ratio to max space
            page_aspect = page_width / page_height
            win_width = max_width
            win_height = int(win_width / page_aspect)
            if win_height > max_height:
                win_height = max_height
                win_width = int(win_height * page_aspect)

        # Enforce minimum size
        win_width = max(400, win_width)
        win_height = max(300, win_height)

        _window.resize(win_width, win_height)

    _window.show()
    return _window


def _cairo_surface_to_qimage(surface):
    """Convert Cairo ImageSurface to QImage."""
    width = surface.get_width()
    height = surface.get_height()
    stride = surface.get_stride()
    data = bytes(surface.get_data())

    # Cairo RGB24 is BGRX (32 bits per pixel), QImage expects RGB32 (ARGB)
    # Both use the same memory layout on little-endian systems
    return QImage(data, width, height, stride, QImage.Format_RGB32).copy()


def _process_qt_events():
    """Process Qt events to keep GUI responsive during interpretation.

    Called periodically from the PostScript execution loop.
    Also triggers quit if window was closed.
    """
    global _ctxt, _quit_sent
    if _app is not None:
        _app.processEvents()
        # If window was closed, trigger quit immediately (only once)
        if _window_closed and _ctxt is not None and not _quit_sent:
            _quit_sent = True
            _ctxt.e_stack.append(ps.Name(b"quit", attrib=ps.ATTRIB_EXEC))


def _wait_for_keypress(ctxt):
    """Block until user presses a key in the Qt window.

    Tracks the wait time in ctxt.user_wait_time so it can be excluded
    from job execution time measurements.
    """
    global _waiting_for_key, _key_pressed, _window_closed, _user_advanced, _busy_mode
    _waiting_for_key = True
    _key_pressed = False
    _user_advanced = False  # Reset at start of each wait

    if _window:
        _window.setWindowTitle("PostForge - Press any key to continue...")
        # Set normal cursor while waiting for user input
        _busy_mode = False
        while _app.overrideCursor():
            _app.restoreOverrideCursor()
        _app.processEvents()

    # Track time spent waiting for user input
    wait_start = time.perf_counter()

    while not _key_pressed and not _window_closed:
        _app.processEvents()

    # Add wait time to context for accurate job timing
    ctxt.user_wait_time += time.perf_counter() - wait_start

    # Track if user pressed key to advance (for auto-quit on last page)
    if _key_pressed and not _window_closed:
        _user_advanced = True

    _waiting_for_key = False
    if _window and not _window_closed:
        _window.setWindowTitle("PostForge")
        # Set busy cursor while interpreter is executing
        _busy_mode = True
        _app.setOverrideCursor(QCursor(Qt.WaitCursor))
        _app.processEvents()


def showpage(ctxt: ps.Context, pd: dict) -> None:
    """
    Render the current page to the Qt window.

    Args:
        ctxt: PostScript context with display_list to render
        pd: Page device dictionary containing MediaSize, PageCount, LineWidthMin, etc.
    """
    global _surface, _qimage, _page_width, _page_height, _zoom_level, _pan_x, _pan_y, _ctxt

    if not PYSIDE6_AVAILABLE:
        print("PySide6 not available. Install with: pip install PySide6")
        return

    # Store context reference for quit signaling from event loop
    _ctxt = ctxt

    # Register event loop callback to keep Qt responsive during interpretation
    ctxt.event_loop_callback = _process_qt_events

    min_line_width = pd[b"LineWidthMin"].val

    # MediaSize is already in device coordinates (at HWResolution)
    device_w = pd[b"MediaSize"].get(ps.Int(0))[1].val
    device_h = pd[b"MediaSize"].get(ps.Int(1))[1].val

    # Cap render dimensions to avoid Cairo surface allocation failures
    # on screen display. When the device resolution produces surfaces
    # larger than this limit, render to a smaller surface and scale the
    # Cairo context so the display list (in device coords) fits.
    MAX_SURFACE_PIXELS = 16384
    downscale = 1.0
    render_w = device_w
    render_h = device_h
    if device_w > MAX_SURFACE_PIXELS or device_h > MAX_SURFACE_PIXELS:
        downscale = MAX_SURFACE_PIXELS / max(device_w, device_h)
        render_w = int(device_w * downscale)
        render_h = int(device_h * downscale)

    # Calculate page dimensions in points for window sizing (from actual HWResolution)
    hw_dpi = pd[b"HWResolution"].get(ps.Int(0))[1].val
    scale = hw_dpi / 72.0
    _page_width = device_w / scale
    _page_height = device_h / scale

    # Create Cairo surface at (possibly capped) render resolution
    _surface = cairo.ImageSurface(cairo.FORMAT_RGB24, render_w, render_h)
    cc = cairo.Context(_surface)

    cc.identity_matrix()
    # Scale Cairo context to map device coordinates into the capped surface
    if downscale != 1.0:
        cc.scale(downscale, downscale)
    # Convert PostScript flatness to Cairo tolerance (PS default 1.0 → Cairo default 0.1)
    cc.set_tolerance(ctxt.gstate.flatness / 10.0)

    # Fill white background (in surface coordinates)
    cc.save()
    cc.identity_matrix()
    cc.set_source_rgb(1.0, 1.0, 1.0)
    cc.rectangle(0, 0, render_w, render_h)
    cc.fill()
    cc.restore()

    cc.set_antialias(_get_antialias_mode(pd))

    # Render display list using shared Cairo renderer (device_h for coordinate flip)
    render_display_list(ctxt, cc, device_h, min_line_width)

    # Convert Cairo surface to QImage
    _qimage = _cairo_surface_to_qimage(_surface)

    # Reset view state for new page
    _zoom_level = 1.0
    _pan_x = 0
    _pan_y = 0

    # Ensure window exists and update (use page dimensions in points for aspect ratio)
    _ensure_window(_page_width, _page_height, _qimage.width(), _qimage.height())
    _canvas.update()
    _app.processEvents()

    # Wait for user input before continuing to next page
    _wait_for_keypress(ctxt)

    # If window was closed, trigger PostScript quit to stop interpretation
    if _window_closed:
        # Push executable /quit onto execution stack
        ctxt.e_stack.append(ps.Name(b"quit", attrib=ps.ATTRIB_EXEC))


def refresh_display(ctxt: ps.Context) -> None:
    """
    Refresh the display with current display list contents.

    Called by live rendering hook to update display after paint operations.
    This provides immediate visual feedback in interactive mode.

    Args:
        ctxt: PostScript context with display_list to render
    """
    global _surface, _qimage, _zoom_level, _pan_x, _pan_y, _window, _page_width, _page_height, _ctxt

    if not PYSIDE6_AVAILABLE:
        return

    # Store context reference for quit signaling from event loop
    _ctxt = ctxt

    # Register event loop callback to keep Qt responsive during interpretation
    ctxt.event_loop_callback = _process_qt_events

    # Skip refresh if window is closed, but trigger quit first
    if _window_closed:
        ctxt.e_stack.append(ps.Name(b"quit", attrib=ps.ATTRIB_EXEC))
        return

    # Initialize view state when window is first created
    if _window is None:
        _zoom_level = 1.0
        _pan_x = 0
        _pan_y = 0

    # Get device dimensions from page device (display list is in device coordinates)
    pd = ctxt.gstate.page_device if hasattr(ctxt, 'gstate') and ctxt.gstate else None
    if pd and b"MediaSize" in pd and b"HWResolution" in pd:
        # MediaSize is already in device coordinates (at HWResolution)
        device_w = int(pd[b"MediaSize"].get(ps.Int(0))[1].val)
        device_h = int(pd[b"MediaSize"].get(ps.Int(1))[1].val)
        hw_dpi = pd[b"HWResolution"].get(ps.Int(0))[1].val
        min_line_width = pd[b"LineWidthMin"].val if b"LineWidthMin" in pd else 1
        # Calculate page dimensions in points for window sizing
        scale = hw_dpi / 72.0
        _page_width = device_w / scale
        _page_height = device_h / scale
    else:
        # Fallback to defaults if page device not available
        _page_width = 612
        _page_height = 792
        scale = _render_dpi / 72.0
        device_w = int(_page_width * scale)
        device_h = int(_page_height * scale)
        min_line_width = 1

    # Cap render dimensions to avoid Cairo surface allocation failures
    MAX_SURFACE_PIXELS = 16384
    downscale = 1.0
    render_w = device_w
    render_h = device_h
    if device_w > MAX_SURFACE_PIXELS or device_h > MAX_SURFACE_PIXELS:
        downscale = MAX_SURFACE_PIXELS / max(device_w, device_h)
        render_w = int(device_w * downscale)
        render_h = int(device_h * downscale)

    # Create Cairo surface at (possibly capped) render resolution
    _surface = cairo.ImageSurface(cairo.FORMAT_RGB24, render_w, render_h)
    cc = cairo.Context(_surface)

    cc.identity_matrix()
    # Scale Cairo context to map device coordinates into the capped surface
    if downscale != 1.0:
        cc.scale(downscale, downscale)
    if hasattr(ctxt, 'gstate') and ctxt.gstate:
        # Convert PostScript flatness to Cairo tolerance (PS default 1.0 → Cairo default 0.1)
        cc.set_tolerance(ctxt.gstate.flatness / 10.0)

    # Fill white background (in surface coordinates)
    cc.save()
    cc.identity_matrix()
    cc.set_source_rgb(1.0, 1.0, 1.0)
    cc.rectangle(0, 0, render_w, render_h)
    cc.fill()
    cc.restore()

    cc.set_antialias(ANTIALIAS_MODE)

    # Render display list using device_h for coordinate flip (display list is in device coords)
    render_display_list(ctxt, cc, device_h, min_line_width)

    # Convert and update
    _qimage = _cairo_surface_to_qimage(_surface)

    # Ensure window exists and update
    _ensure_window(_page_width, _page_height, _qimage.width() if _qimage else None, _qimage.height() if _qimage else None)
    if _app:
        _app.processEvents()  # Process resize events before painting
    if _canvas:
        _canvas.update()
    if _app:
        _app.processEvents()

    # If window was closed, trigger PostScript quit to stop interpretation
    if _window_closed:
        ctxt.e_stack.append(ps.Name(b"quit", attrib=ps.ATTRIB_EXEC))


def enter_event_loop():
    """
    Enter Qt event loop after PostScript execution completes.

    This keeps the window open until the user closes it, allowing
    viewing of the final rendered result. However, if the user pressed
    a key to advance past the last page, auto-quit instead of waiting.
    """
    global _window_closed

    if not PYSIDE6_AVAILABLE:
        return

    if _app and _window and not _window_closed:
        # Restore cursor to normal now that execution is complete
        global _busy_mode
        _busy_mode = False
        while _app.overrideCursor():
            _app.restoreOverrideCursor()
        _app.processEvents()

        # If user pressed key to advance past the last page, auto-quit
        if _user_advanced:
            _window.close()
            return

        _window.setWindowTitle("PostForge - Press Q or close window to exit")
        # Ensure canvas has focus for keyboard events
        if _canvas:
            _canvas.setFocus()
        _app.exec()
