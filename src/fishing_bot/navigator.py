"""Navigator: reads nav pixels from the addon and sends arrow keys to WoW.

Pixel layout (set by the addon's navigation.lua):
  Pixel (0,0): State (existing — IDLE, FISHING, etc.)
  Pixel (1,0): Nav command   R=step(0-4), G=action(0-4), B=0
  Pixel (2,0): Distance      R=yards_int, G=yards_frac, B=0
  Pixel (3,0): Angle          R=degrees_int, G=degrees_frac, B=direction(0=right,1=left)

Steps:  0=IDLE, 1=ROTATE_TO_TARGET, 2=WALK, 3=ROTATE_TO_FACING, 4=DONE
Actions: 0=NONE, 1=TURN_LEFT, 2=TURN_RIGHT, 3=MOVE_FORWARD, 4=MOVE_BACKWARD

Proportional control:
  - Hold duration is proportional to the remaining error (angle or distance).
  - WoW keyboard turn speed ≈ 180°/s → 1° ≈ 5.5ms of key hold.
  - Each cycle: read → hold key for proportional time → release → brief settle → repeat.
  - Large errors: hold continuously (smooth and fast).
  - Small errors: short pulses that converge in 1-3 cycles without oscillation.
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

# Cycle timing
CYCLE_MIN = 0.035  # minimum cycle time (read + act)


class Navigator:
    """Reads navigation pixels and sends proportional movement keys to WoW."""

    def __init__(self, hwnd: int, pixel_reader: PixelReader, pixel_positions: list[tuple[int, int]]):
        self.hwnd = hwnd
        self.reader = pixel_reader
        self.pixel_positions = pixel_positions
        self._held_keys: set[str] = set()

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _hold(self, key: str) -> None:
        if key not in self._held_keys:
            key_down(self.hwnd, key)
            self._held_keys.add(key)

    def _release(self, key: str) -> None:
        if key in self._held_keys:
            key_up(self.hwnd, key)
            self._held_keys.discard(key)

    def _release_all(self) -> None:
        for key in list(self._held_keys):
            key_up(self.hwnd, key)
        self._held_keys.clear()

    def _pulse(self, key: str, duration: float) -> None:
        """Hold key for exact duration, then release."""
        key_down(self.hwnd, key)
        time.sleep(duration)
        key_up(self.hwnd, key)
        # Don't track in _held_keys since we already released

    @staticmethod
    def _turn_key(action: int) -> str | None:
        if action == ACTION_TURN_LEFT:
            return KEY_LEFT
        if action == ACTION_TURN_RIGHT:
            return KEY_RIGHT
        return None

    @staticmethod
    def _opposite(key: str) -> str:
        return KEY_RIGHT if key == KEY_LEFT else KEY_LEFT

    # ------------------------------------------------------------------
    # Pixel reading
    # ------------------------------------------------------------------

    def _read_nav(self) -> tuple[int, int, float, float] | None:
        pixels = self.reader.read_pixels(self.pixel_positions)
        cmd = pixels[1]
        if cmd is None:
            return None
        step, action = cmd[0], cmd[1]
        dist = 0.0
        if pixels[2]:
            dist = pixels[2][0] + pixels[2][1] / 255.0
        angle = 0.0
        if pixels[3]:
            angle = pixels[3][0] + pixels[3][1] / 255.0
        return step, action, dist, angle

    # ------------------------------------------------------------------
    # Proportional rotation
    # ------------------------------------------------------------------

    def _do_turn(self, action: int, angle_deg: float) -> None:
        """Single rotation cycle with proportional hold time.

        At ~180°/s, to turn X°: hold for X/180 seconds.
        We aim to cover ~55% of remaining angle per cycle to converge
        smoothly in 2-4 cycles without overshooting.
        """
        tk = self._turn_key(action)
        if not tk:
            self._release(KEY_LEFT)
            self._release(KEY_RIGHT)
            time.sleep(CYCLE_MIN)
            return

        self._release(self._opposite(tk))

        if angle_deg > 35:
            # Large: hold continuously, addon will update next cycle
            self._hold(tk)
            time.sleep(0.04)
        else:
            # Proportional pulse: cover ~55% of remaining angle
            hold_s = angle_deg * 0.55 / 180.0
            hold_s = max(0.006, min(hold_s, 0.10))

            # Release any held state first for clean pulse
            self._release(tk)
            self._pulse(tk, hold_s)

            # Settle: let the addon see the new facing before next cycle
            settle = max(0.025, 0.06 - hold_s)
            time.sleep(settle)

    # ------------------------------------------------------------------
    # Proportional walking
    # ------------------------------------------------------------------

    def _do_walk(self, action: int, dist: float, angle_deg: float) -> None:
        """Single walk cycle: forward with live steering, proportional to distance."""

        if action == ACTION_MOVE_BACKWARD:
            # Overshoot: short backward pulse
            self._release(KEY_UP)
            self._release(KEY_LEFT)
            self._release(KEY_RIGHT)
            hold_s = max(0.015, min(0.12, dist * 0.05))
            self._pulse(KEY_DOWN, hold_s)
            time.sleep(0.06)
            return

        tk = self._turn_key(action)

        if dist > 4.0:
            # Far: hold forward continuously with live steering
            self._hold(KEY_UP)
            self._release(KEY_DOWN)

            if tk and angle_deg > 3:
                self._release(self._opposite(tk))
                if angle_deg > 20:
                    # Heavy correction: hold turn
                    self._hold(tk)
                else:
                    # Light steering pulse while walking
                    self._release(tk)
                    steer_s = max(0.006, angle_deg * 0.4 / 180.0)
                    self._pulse(tk, steer_s)
            else:
                self._release(KEY_LEFT)
                self._release(KEY_RIGHT)

            time.sleep(0.04)

        elif dist > 1.5:
            # Approaching: pulsed forward with heading correction
            self._release_all()

            # Correct heading first if off
            if tk and angle_deg > 4:
                steer_s = max(0.006, angle_deg * 0.4 / 180.0)
                self._pulse(tk, steer_s)
                time.sleep(0.025)

            # Forward pulse proportional to distance
            # At ~7 yd/s run speed: 1 yard ≈ 140ms
            fwd_s = max(0.02, min(0.12, dist * 0.03))
            self._pulse(KEY_UP, fwd_s)
            time.sleep(0.06)

        else:
            # Very close: micro-pulses
            self._release_all()
            self._pulse(KEY_UP, 0.015)
            time.sleep(0.08)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def navigate(self) -> bool:
        """Run the navigation loop until done or failed."""
        logger.info("Navigator started — reading nav pixels...")
        idle_count = 0

        try:
            while True:
                nav = self._read_nav()
                if nav is None:
                    logger.warning("Failed to read nav pixels, retrying...")
                    time.sleep(CYCLE_MIN)
                    continue

                step, action, dist, angle_deg = nav

                # Idle detection
                if step == STEP_IDLE:
                    idle_count += 1
                    if idle_count > 20:
                        self._release_all()
                        logger.info("Navigation complete (nav pixel idle).")
                        return True
                    self._release_all()
                    time.sleep(CYCLE_MIN)
                    continue
                else:
                    idle_count = 0

                if step == STEP_DONE:
                    self._release_all()
                    logger.info("Navigation complete!")
                    return True

                # Rotation steps (no forward/backward movement)
                if step in (STEP_ROTATE_TO_TARGET, STEP_ROTATE_TO_FACING):
                    self._release(KEY_UP)
                    self._release(KEY_DOWN)
                    self._do_turn(action, angle_deg)

                    if logger.isEnabledFor(logging.DEBUG):
                        name = "ROT_TARGET" if step == STEP_ROTATE_TO_TARGET else "ROT_FACING"
                        logger.debug("%s angle=%.1f° action=%d", name, angle_deg, action)

                # Walk step
                elif step == STEP_WALK:
                    self._do_walk(action, dist, angle_deg)

                    if logger.isEnabledFor(logging.DEBUG):
                        d = "BACK" if action == ACTION_MOVE_BACKWARD else "FWD"
                        logger.debug("WALK %s dist=%.1f angle=%.1f°", d, dist, angle_deg)

                else:
                    time.sleep(CYCLE_MIN)

        except KeyboardInterrupt:
            logger.info("Navigation cancelled by user.")
            return False
        finally:
            self._release_all()
