"""Reads a pixel color from the WoW window, even when minimized.

Uses PrintWindow with PW_RENDERFULLCONTENT flag which can capture
DirectX windows that are minimized or in background.
"""

import ctypes
import ctypes.wintypes as wintypes
import logging

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# PrintWindow flag that requests full render content (works for some DX apps)
PW_RENDERFULLCONTENT = 0x00000002

# GDI constants
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


def calibrate_pixel_positions(hwnd: int) -> list[tuple[int, int]]:
    """Auto-detect nav pixel screen positions by scanning for the blue block.

    The addon renders 4 adjacent 1-WoW-pixel blocks. Pixel 0 is blue.
    We measure how many capture-pixels the blue block spans to find the
    DPI multiplier, then compute centers for all 4 blocks.
    """
    reader = PixelReader.__new__(PixelReader)
    reader.hwnd = hwnd
    reader.x = 0
    reader.y = 0

    # Scan top row to find where blue ends
    positions = [(x, 0) for x in range(40)]
    pixels = reader._capture_and_read(positions)

    blue_width = 0
    for p in pixels:
        if p and p[2] > 200 and p[0] < 30 and p[1] < 30:  # ~blue
            blue_width += 1
        else:
            break

    if blue_width < 1:
        logger.warning("Could not find blue calibration block, using defaults")
        blue_width = 2  # safe default

    # Each addon pixel = blue_width capture pixels, adjacent
    step = blue_width  # distance between pixel centers
    center = blue_width // 2  # center of first block

    result = [(i * step + center, center) for i in range(4)]
    logger.info("Calibrated: 1 WoW pixel = %d capture px, positions: %s", blue_width, result)
    return result


class PixelReader:
    """Reads pixels from a window handle using PrintWindow."""

    def __init__(self, hwnd: int, x: int = 3, y: int = 3):
        self.hwnd = hwnd
        self.x = x
        self.y = y

    def _capture_and_read(self, positions: list[tuple[int, int]]) -> list[tuple[int, int, int] | None]:
        """Capture window once and read multiple pixel positions.

        Returns a list of (R, G, B) tuples (or None) for each position.
        """
        results: list[tuple[int, int, int] | None] = [None] * len(positions)
        try:
            rect = wintypes.RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top

            if width <= 0 or height <= 0:
                return results

            hdc_window = user32.GetDC(self.hwnd)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
            hbm = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
            old_bm = gdi32.SelectObject(hdc_mem, hbm)

            result = user32.PrintWindow(self.hwnd, hdc_mem, PW_RENDERFULLCONTENT)
            if not result:
                gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_window, 0, 0, SRCCOPY)

            for i, (px, py) in enumerate(positions):
                color = gdi32.GetPixel(hdc_mem, px, py)
                if color != 0xFFFFFFFF:
                    r = color & 0xFF
                    g = (color >> 8) & 0xFF
                    b = (color >> 16) & 0xFF
                    results[i] = (r, g, b)

            gdi32.SelectObject(hdc_mem, old_bm)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(self.hwnd, hdc_window)

        except Exception as e:
            logger.debug("Pixel read failed: %s", e)

        return results

    def read_pixel(self) -> tuple[int, int, int] | None:
        """Read the pixel at (x, y) from the WoW window.

        Returns (R, G, B) tuple with values 0-255, or None on failure.
        """
        return self._capture_and_read([(self.x, self.y)])[0]

    def read_pixels(self, positions: list[tuple[int, int]]) -> list[tuple[int, int, int] | None]:
        """Read multiple pixels in a single capture. Returns list of (R,G,B) or None."""
        return self._capture_and_read(positions)

    def read_state(self) -> str | None:
        """Read the pixel and map it to an addon state string.

        Returns a state name or None if unrecognized.
        """
        rgb = self.read_pixel()
        if rgb is None:
            return None

        r, g, b = rgb
        return _match_state(r, g, b)


# State color map matching the addon's FA.PIXEL_COLORS
# Using tolerance because DX rendering may slightly alter colors
_STATE_COLORS = {
    "IDLE":            (0, 0, 255),
    "FISHING":         (0, 255, 0),
    "NAV":             (0, 255, 255),
    "TREASURE_SPAWN":  (255, 0, 255),
    "TREASURE_TARGET": (255, 255, 0),
    "SPIRIT_SPAWN":    (255, 0, 0),
    "CRAB_SPAWN":      (255, 128, 0),
    "SELL_ACTION":     (128, 0, 255),
    "SELL_INTERACT":   (128, 255, 0),
    "SELL_WAIT":       (128, 128, 0),
}

TOLERANCE = 30  # Color matching tolerance per channel


def _match_state(r: int, g: int, b: int) -> str | None:
    """Match an RGB value to the closest known state."""
    for state, (sr, sg, sb) in _STATE_COLORS.items():
        if abs(r - sr) <= TOLERANCE and abs(g - sg) <= TOLERANCE and abs(b - sb) <= TOLERANCE:
            return state
    return None
