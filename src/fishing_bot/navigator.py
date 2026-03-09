"""Navigator: reads nav pixels from the addon and sends arrow keys to WoW.

Pixel layout (set by the addon's navigation.lua):
  Pixel (0,0): State (existing — IDLE, FISHING, etc.)
  Pixel (1,0): Nav command   R=step(0-4), G=action(0-3), B=0
  Pixel (2,0): Distance      R=yards_int, G=yards_frac, B=0
  Pixel (3,0): Angle          R=degrees_int, G=degrees_frac, B=direction(0=right,1=left)

Steps:  0=IDLE, 1=ROTATE_TO_TARGET, 2=WALK, 3=ROTATE_TO_FACING, 4=DONE
Actions: 0=NONE, 1=TURN_LEFT, 2=TURN_RIGHT, 3=MOVE_FORWARD
"""

import time
import logging

from .pixel import PixelReader
from .input import key_down, key_up

logger = logging.getLogger(__name__)

# Nav steps (matching addon constants)
STEP_IDLE = 0
STEP_ROTATE_TO_TARGET = 1
STEP_WALK = 2
STEP_ROTATE_TO_FACING = 3
STEP_DONE = 4

# Nav actions
ACTION_NONE = 0
ACTION_TURN_LEFT = 1
ACTION_TURN_RIGHT = 2
ACTION_MOVE_FORWARD = 3

# Keys
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_UP = "up"

# Poll interval for reading nav pixels
NAV_POLL_INTERVAL = 0.05  # 50ms — fast enough for smooth navigation


class Navigator:
    """Reads navigation pixels and sends movement keys to WoW."""

    def __init__(self, hwnd: int, pixel_reader: PixelReader, pixel_positions: list[tuple[int, int]]):
        self.hwnd = hwnd
        self.reader = pixel_reader
        self.pixel_positions = pixel_positions
        # Track which keys are currently held
        self._held_keys: set[str] = set()

    def _hold_key(self, key: str) -> None:
        """Start holding a key if not already held."""
        if key not in self._held_keys:
            key_down(self.hwnd, key)
            self._held_keys.add(key)

    def _release_key(self, key: str) -> None:
        """Release a key if currently held."""
        if key in self._held_keys:
            key_up(self.hwnd, key)
            self._held_keys.discard(key)

    def _release_all(self) -> None:
        """Release all held keys."""
        for key in list(self._held_keys):
            key_up(self.hwnd, key)
        self._held_keys.clear()

    def _read_nav_pixels(self) -> tuple[int, int, float, float] | None:
        """Read all 4 nav pixels in one capture.

        Returns (step, action, distance_yards, angle_degrees) or None on failure.
        """
        pixels = self.reader.read_pixels(self.pixel_positions)

        cmd_pixel = pixels[1]
        dist_pixel = pixels[2]
        angle_pixel = pixels[3]

        if cmd_pixel is None:
            return None

        step = cmd_pixel[0]    # R channel
        action = cmd_pixel[1]  # G channel

        # Decode distance
        dist = 0.0
        if dist_pixel:
            dist = dist_pixel[0] + dist_pixel[1] / 255.0

        # Decode angle
        angle_deg = 0.0
        if angle_pixel:
            angle_deg = angle_pixel[0] + angle_pixel[1] / 255.0

        return (step, action, dist, angle_deg)

    def navigate(self) -> bool:
        """Run the navigation loop until done or failed.

        Returns True if navigation completed, False if it was aborted.
        """
        logger.info("Navigator started — reading nav pixels...")

        try:
            while True:
                nav = self._read_nav_pixels()
                if nav is None:
                    logger.warning("Failed to read nav pixels, retrying...")
                    time.sleep(NAV_POLL_INTERVAL)
                    continue

                step, action, dist, angle_deg = nav

                if step == STEP_IDLE:
                    # Nav not started yet by addon
                    self._release_all()
                    time.sleep(NAV_POLL_INTERVAL)
                    continue

                if step == STEP_DONE:
                    self._release_all()
                    logger.info("Navigation complete!")
                    return True

                # Determine which keys to hold based on step + action
                want_forward = False
                want_left = False
                want_right = False

                if step == STEP_WALK:
                    want_forward = True
                    if action == ACTION_TURN_LEFT:
                        want_left = True
                    elif action == ACTION_TURN_RIGHT:
                        want_right = True

                elif step in (STEP_ROTATE_TO_TARGET, STEP_ROTATE_TO_FACING):
                    if action == ACTION_TURN_LEFT:
                        want_left = True
                    elif action == ACTION_TURN_RIGHT:
                        want_right = True

                # Apply key states
                if want_forward:
                    self._hold_key(KEY_UP)
                else:
                    self._release_key(KEY_UP)

                if want_left:
                    self._hold_key(KEY_LEFT)
                    self._release_key(KEY_RIGHT)
                elif want_right:
                    self._hold_key(KEY_RIGHT)
                    self._release_key(KEY_LEFT)
                else:
                    self._release_key(KEY_LEFT)
                    self._release_key(KEY_RIGHT)

                if step == STEP_WALK:
                    logger.debug("WALK dist=%.1f yds, angle=%.1f°, action=%d", dist, angle_deg, action)
                else:
                    step_name = {STEP_ROTATE_TO_TARGET: "ROT_TARGET", STEP_ROTATE_TO_FACING: "ROT_FACING"}.get(step, str(step))
                    logger.debug("%s angle=%.1f°, action=%d", step_name, angle_deg, action)

                time.sleep(NAV_POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Navigation cancelled by user.")
            return False
        finally:
            self._release_all()
