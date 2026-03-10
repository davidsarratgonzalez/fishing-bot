"""Navigator: reads nav pixels from the addon and sends arrow keys to WoW.

Pixel layout (set by the addon's navigation.lua):
  Pixel 0: State (IDLE, NAV, etc.)
  Pixel 1: R=step(0-4), G=action(0-3), B=frameCounter(0-255)
  Pixel 2: R=yards_int, G=yards_frac, B=flags(close/veryclose/behind)
  Pixel 3: R=degrees_int, G=degrees_frac, B=direction(0=right,1=left)

Steps:  0=IDLE, 1=ROTATE_TO_TARGET, 2=WALK, 3=ROTATE_TO_FACING, 4=DONE
Actions: 0=NONE, 1=TURN_LEFT, 2=TURN_RIGHT, 3=MOVE_FORWARD

Physics:
  Turn speed ≈ 210°/s → 1° = 4.76ms
  Run speed  ≈ 7 yd/s → 1 yd = 143ms
  Min reliable pulse ≈ 8ms → min turn ≈ 1.7°

Control strategy:
  Dead-beat proportional — each pulse aims to cover 85% of remaining error,
  converging in 1-3 cycles. Frame counter prevents acting on stale data.
"""

import time
import logging

from .pixel import PixelReader
from .input import key_down, key_up

logger = logging.getLogger(__name__)

# Nav steps
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
KEY_DOWN = "down"

# Physics constants
TURN_SPEED = 210.0  # degrees per second
WALK_SPEED = 7.0    # yards per second
MIN_PULSE = 0.008   # 8ms — minimum reliable PostMessage hold
SETTLE = 0.028      # time for addon to update (~1.5 frames at 60fps)

# Distance flags (from addon pixel 2 B channel)
FLAG_CLOSE = 1      # dist < 2 yards
FLAG_ARRIVED = 2    # dist < 0.5 yards
FLAG_BEHIND = 4     # target > 90° behind player


class Navigator:
    """Reads navigation pixels and sends proportional movement keys to WoW."""

    def __init__(self, hwnd: int, pixel_reader: PixelReader, pixel_positions: list[tuple[int, int]]):
        self.hwnd = hwnd
        self.reader = pixel_reader
        self.positions = pixel_positions
        self._held: set[str] = set()
        self._last_frame_id: int = -1

    # ------------------------------------------------------------------
    # Key control
    # ------------------------------------------------------------------

    def _hold(self, key: str) -> None:
        if key not in self._held:
            key_down(self.hwnd, key)
            self._held.add(key)

    def _release(self, key: str) -> None:
        if key in self._held:
            key_up(self.hwnd, key)
            self._held.discard(key)

    def _release_all(self) -> None:
        for k in list(self._held):
            key_up(self.hwnd, k)
        self._held.clear()

    def _pulse(self, key: str, duration: float) -> None:
        """Hold key for exact duration then release. Not tracked in _held."""
        self._release(key)  # ensure clean start
        key_down(self.hwnd, key)
        time.sleep(max(duration, MIN_PULSE))
        key_up(self.hwnd, key)

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

    def _read_nav(self) -> tuple[int, int, float, int, float, int] | None:
        """Read nav pixels. Returns (step, action, dist, flags, angle_deg, frame_id) or None."""
        pixels = self.reader.read_pixels(self.positions)
        cmd = pixels[1]
        if cmd is None:
            return None

        step = cmd[0]
        action = cmd[1]
        frame_id = cmd[2]

        dist = 0.0
        flags = 0
        if pixels[2]:
            dist = pixels[2][0] + pixels[2][1] / 255.0
            flags = pixels[2][2]

        angle_deg = 0.0
        if pixels[3]:
            angle_deg = pixels[3][0] + pixels[3][1] / 255.0

        return step, action, dist, flags, angle_deg, frame_id

    def _read_fresh(self, timeout: float = 0.1) -> tuple[int, int, float, int, float, int] | None:
        """Read pixels, skipping stale frames (same frame_id as last read)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            nav = self._read_nav()
            if nav is None:
                time.sleep(0.005)
                continue
            frame_id = nav[5]
            if frame_id != self._last_frame_id:
                self._last_frame_id = frame_id
                return nav
            # Stale frame — wait briefly and retry
            time.sleep(0.005)
        # Timeout: return whatever we have
        return self._read_nav()

    # ------------------------------------------------------------------
    # Proportional rotation — dead-beat controller
    # ------------------------------------------------------------------

    def _do_turn(self, action: int, angle_deg: float) -> None:
        """Single rotation cycle. Aims to cover 85% of remaining angle."""
        tk = self._turn_key(action)
        if not tk:
            self._release(KEY_LEFT)
            self._release(KEY_RIGHT)
            time.sleep(SETTLE)
            return

        self._release(self._opposite(tk))

        if angle_deg > 50:
            # Large: hold key continuously, re-check next cycle
            self._hold(tk)
            time.sleep(0.030)
        elif angle_deg > 3.5:
            # Medium: dead-beat pulse — cover 85% of remaining angle
            target = angle_deg * 0.85
            hold_s = target / TURN_SPEED
            self._release(tk)
            self._pulse(tk, hold_s)
            time.sleep(SETTLE)
        else:
            # Small (<3.5°): minimum pulse, converges in 1-2 cycles
            self._release(tk)
            self._pulse(tk, MIN_PULSE)
            time.sleep(SETTLE)

    # ------------------------------------------------------------------
    # Proportional walking
    # ------------------------------------------------------------------

    def _do_walk(self, action: int, dist: float, flags: int, angle_deg: float) -> None:
        """Single walk cycle with live steering."""
        tk = self._turn_key(action)

        # Steering while walking (proportional pulse)
        if tk and angle_deg > 2.0:
            self._hold(KEY_UP)
            self._release(KEY_DOWN)
            self._release(self._opposite(tk))
            if angle_deg > 12:
                # Strong correction: hold turn key while walking
                self._hold(tk)
            else:
                # Light correction: proportional steering pulse
                steer_s = (angle_deg * 0.8) / TURN_SPEED
                self._pulse(tk, max(MIN_PULSE, steer_s))
            time.sleep(0.030)
            return

        # No turn keys needed
        self._release(KEY_LEFT)
        self._release(KEY_RIGHT)

        if not (flags & FLAG_CLOSE):
            # Far (>2 yd): hold forward continuously
            self._hold(KEY_UP)
            self._release(KEY_DOWN)
            time.sleep(0.035)
        else:
            # Close (<2 yd): pulsed forward — cover 70% of remaining distance
            self._release_all()
            target_dist = dist * 0.7
            hold_s = target_dist / WALK_SPEED
            hold_s = max(0.012, min(hold_s, 0.12))
            self._pulse(KEY_UP, hold_s)
            time.sleep(SETTLE)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def navigate(self) -> bool:
        """Run the navigation loop until done or failed."""
        logger.info("Navigator started.")
        idle_count = 0
        self._last_frame_id = -1

        try:
            while True:
                nav = self._read_fresh(timeout=0.08)
                if nav is None:
                    logger.warning("Failed to read nav pixels, retrying...")
                    time.sleep(0.030)
                    continue

                step, action, dist, flags, angle_deg, _ = nav

                # Idle detection (addon finished nav)
                if step == STEP_IDLE:
                    idle_count += 1
                    if idle_count > 15:
                        self._release_all()
                        logger.info("Navigation complete (idle).")
                        return True
                    self._release_all()
                    time.sleep(0.030)
                    continue
                else:
                    idle_count = 0

                if step == STEP_DONE:
                    self._release_all()
                    logger.info("Navigation complete!")
                    return True

                # Rotation steps
                if step in (STEP_ROTATE_TO_TARGET, STEP_ROTATE_TO_FACING):
                    self._release(KEY_UP)
                    self._release(KEY_DOWN)
                    self._do_turn(action, angle_deg)

                    if logger.isEnabledFor(logging.DEBUG):
                        name = "ROT_TARGET" if step == STEP_ROTATE_TO_TARGET else "ROT_FACING"
                        logger.debug("%s angle=%.1f° action=%d", name, angle_deg, action)

                # Walk step
                elif step == STEP_WALK:
                    self._do_walk(action, dist, flags, angle_deg)

                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("WALK dist=%.1f angle=%.1f° flags=%d", dist, angle_deg, flags)

                else:
                    time.sleep(0.030)

        except KeyboardInterrupt:
            logger.info("Navigation cancelled by user.")
            return False
        finally:
            self._release_all()
