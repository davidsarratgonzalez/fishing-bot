"""Navigator: reads nav pixels from the addon and sends arrow keys to WoW.

Pixel layout (set by the addon's navigation.lua):
  Pixel (0,0): State (existing — IDLE, FISHING, etc.)
  Pixel (1,0): Nav command   R=step(0-4), G=action(0-4), B=0
  Pixel (2,0): Distance      R=yards_int, G=yards_frac, B=0
  Pixel (3,0): Angle          R=degrees_int, G=degrees_frac, B=direction(0=right,1=left)

Steps:  0=IDLE, 1=ROTATE_TO_TARGET, 2=WALK, 3=ROTATE_TO_FACING, 4=DONE
Actions: 0=NONE, 1=TURN_LEFT, 2=TURN_RIGHT, 3=MOVE_FORWARD, 4=MOVE_BACKWARD

Precision strategy:
  - Large angle/distance: hold key continuously (fast)
  - Medium: pulsed taps with pauses (controlled)
  - Small: single short taps (fine adjustment)
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
ACTION_MOVE_BACKWARD = 4

# Keys
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_UP = "up"
KEY_DOWN = "down"

# Poll interval for reading nav pixels
NAV_POLL_INTERVAL = 0.05  # 50ms


class Navigator:
    """Reads navigation pixels and sends movement keys to WoW."""

    def __init__(self, hwnd: int, pixel_reader: PixelReader, pixel_positions: list[tuple[int, int]]):
        self.hwnd = hwnd
        self.reader = pixel_reader
        self.pixel_positions = pixel_positions
        self._held_keys: set[str] = set()

    def _hold_key(self, key: str) -> None:
        if key not in self._held_keys:
            key_down(self.hwnd, key)
            self._held_keys.add(key)

    def _release_key(self, key: str) -> None:
        if key in self._held_keys:
            key_up(self.hwnd, key)
            self._held_keys.discard(key)

    def _release_all(self) -> None:
        for key in list(self._held_keys):
            key_up(self.hwnd, key)
        self._held_keys.clear()

    def _tap_key(self, key: str, duration: float) -> None:
        """Press and release a key for a precise duration."""
        key_down(self.hwnd, key)
        time.sleep(duration)
        key_up(self.hwnd, key)

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

    def _get_turn_key(self, action: int) -> str | None:
        if action == ACTION_TURN_LEFT:
            return KEY_LEFT
        elif action == ACTION_TURN_RIGHT:
            return KEY_RIGHT
        return None

    def _do_rotation(self, action: int, angle_deg: float) -> None:
        """Turn with proportional control based on remaining angle."""
        turn_key = self._get_turn_key(action)
        if not turn_key:
            self._release_key(KEY_LEFT)
            self._release_key(KEY_RIGHT)
            return

        if angle_deg > 25:
            # Large angle: hold continuously for speed
            self._hold_key(turn_key)
            # Release opposite
            opposite = KEY_RIGHT if turn_key == KEY_LEFT else KEY_LEFT
            self._release_key(opposite)
            time.sleep(NAV_POLL_INTERVAL)
        elif angle_deg > 8:
            # Medium angle: pulsed taps (proportional duration)
            self._release_all()
            tap_time = max(0.03, min(0.12, angle_deg * 0.005))
            self._tap_key(turn_key, tap_time)
            time.sleep(0.08)  # pause to let addon recalculate
        else:
            # Small angle: very short single tap
            self._release_all()
            self._tap_key(turn_key, 0.02)
            time.sleep(0.10)  # longer pause for fine control

    def _do_walk(self, action: int, dist: float, angle_deg: float) -> None:
        """Walk with proportional control based on distance."""
        if action == ACTION_MOVE_BACKWARD:
            # Overshoot correction: short backward taps
            self._release_key(KEY_UP)
            self._release_key(KEY_LEFT)
            self._release_key(KEY_RIGHT)
            tap_time = max(0.03, min(0.15, dist * 0.08))
            self._tap_key(KEY_DOWN, tap_time)
            time.sleep(0.10)
            return

        # Forward movement with distance-based control
        if dist > 5:
            # Far: hold forward + correct heading
            self._hold_key(KEY_UP)
            self._release_key(KEY_DOWN)
            turn_key = self._get_turn_key(action)
            if turn_key:
                self._hold_key(turn_key)
                opposite = KEY_RIGHT if turn_key == KEY_LEFT else KEY_LEFT
                self._release_key(opposite)
            else:
                self._release_key(KEY_LEFT)
                self._release_key(KEY_RIGHT)
            time.sleep(NAV_POLL_INTERVAL)

        elif dist > 2:
            # Medium: pulsed forward taps with heading correction
            self._release_all()
            # Correct heading first if needed
            turn_key = self._get_turn_key(action)
            if turn_key and angle_deg > 5:
                self._tap_key(turn_key, 0.03)
                time.sleep(0.05)
            # Short forward tap proportional to distance
            tap_time = max(0.04, min(0.15, dist * 0.04))
            self._tap_key(KEY_UP, tap_time)
            time.sleep(0.10)

        else:
            # Close: very short forward taps
            self._release_all()
            self._tap_key(KEY_UP, 0.03)
            time.sleep(0.12)

    def navigate(self) -> bool:
        """Run the navigation loop until done or failed.

        Returns True if navigation completed, False if it was aborted.
        """
        logger.info("Navigator started — reading nav pixels...")
        idle_count = 0

        try:
            while True:
                nav = self._read_nav_pixels()
                if nav is None:
                    logger.warning("Failed to read nav pixels, retrying...")
                    time.sleep(NAV_POLL_INTERVAL)
                    continue

                step, action, dist, angle_deg = nav

                if step == STEP_IDLE:
                    idle_count += 1
                    if idle_count > 20:
                        self._release_all()
                        logger.info("Navigation complete (nav pixel idle).")
                        return True
                    self._release_all()
                    time.sleep(NAV_POLL_INTERVAL)
                    continue
                else:
                    idle_count = 0

                if step == STEP_DONE:
                    self._release_all()
                    logger.info("Navigation complete!")
                    return True

                if step in (STEP_ROTATE_TO_TARGET, STEP_ROTATE_TO_FACING):
                    self._release_key(KEY_UP)
                    self._release_key(KEY_DOWN)
                    self._do_rotation(action, angle_deg)
                    step_name = "ROT_TARGET" if step == STEP_ROTATE_TO_TARGET else "ROT_FACING"
                    logger.debug("%s angle=%.1f°, action=%d", step_name, angle_deg, action)

                elif step == STEP_WALK:
                    self._do_walk(action, dist, angle_deg)
                    dir_name = "BACKWARD" if action == ACTION_MOVE_BACKWARD else "FORWARD"
                    logger.debug("WALK %s dist=%.1f yds, angle=%.1f°", dir_name, dist, angle_deg)

                else:
                    time.sleep(NAV_POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Navigation cancelled by user.")
            return False
        finally:
            self._release_all()
