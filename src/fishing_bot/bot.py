import atexit
import random
import signal
import threading
import time
import logging
import winsound

from .config import BotConfig
from .audio import AudioMonitor
from .input import find_wow_window, send_key, key_down, key_up
from .pixel import PixelReader, calibrate_pixel_positions, match_state
from .navigator import Navigator

logger = logging.getLogger(__name__)


class FishingBot:
    """State-driven fishing bot. Pixel 0 from the addon is the source of truth."""

    def __init__(self, config: BotConfig | None = None):
        self.config = config or BotConfig()
        self.audio = AudioMonitor(self.config.process_name)
        self.running = False
        self._hwnd: int | None = None
        self._pixel_reader: PixelReader | None = None
        self._nav_positions: list[tuple[int, int]] | None = None
        self._last_state: str | None = None

    def _humanize(self, base_delay: float) -> float:
        if self.config.humanize <= 0:
            return base_delay
        sigma = base_delay * self.config.humanize / 3
        return max(0.05, base_delay + random.gauss(0, sigma))

    def _sleep(self, base_delay: float) -> None:
        time.sleep(self._humanize(base_delay))

    def _read_state(self) -> str | None:
        if not self._pixel_reader:
            return None
        return self._pixel_reader.read_state()

    def _wait_for_state(self, target: str, timeout: float = 10.0) -> str | None:
        """Wait until pixel 0 shows the target state (or any other non-current state).

        Returns the new state, or None on timeout.
        """
        start = time.monotonic()
        while self.running and time.monotonic() - start < timeout:
            state = self._read_state()
            if state == target:
                return state
            time.sleep(0.15)
        return self._read_state()

    def _wait_for_not_state(self, avoid: str, timeout: float = 10.0) -> str | None:
        """Wait until pixel 0 is no longer the given state.

        Returns the new state, or None on timeout.
        """
        start = time.monotonic()
        while self.running and time.monotonic() - start < timeout:
            state = self._read_state()
            if state != avoid:
                return state
            time.sleep(0.15)
        return self._read_state()

    def _wait_for_silence(self) -> None:
        """Wait for cast sound to fade before listening for bites."""
        time.sleep(0.5)
        while self.running:
            peak = self.audio.get_peak_volume()
            if peak < self.config.audio_threshold:
                break
            time.sleep(self.config.poll_interval)

    def _find_wow(self) -> int:
        result = find_wow_window(self.config.process_name)
        if result is None:
            raise RuntimeError(
                f"Could not find {self.config.process_name}. "
                "Make sure World of Warcraft is running."
            )
        hwnd, pid = result
        logger.info("Found %s (PID %d, HWND %d)", self.config.process_name, pid, hwnd)
        return hwnd

    def _init_pixel_reader(self) -> None:
        self._nav_positions = calibrate_pixel_positions(self._hwnd)
        p0 = self._nav_positions[0]
        self._pixel_reader = PixelReader(self._hwnd, x=p0[0], y=p0[1])

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _run_nav(self) -> None:
        logger.info("NAV — navigating to saved position...")
        nav = Navigator(self._hwnd, self._pixel_reader, self._nav_positions)
        success = nav.navigate()
        logger.info("Navigation %s.", "complete" if success else "aborted")

    def _handle_idle(self) -> None:
        """Cast and wait for FISHING state."""
        self._sleep(0.7)  # natural pause before re-casting (0.5-0.9s range)
        logger.info("Casting [key=%s]", self.config.cast_key)
        send_key(self._hwnd, self.config.cast_key)

        # Wait for addon to confirm fishing channel started
        state = self._wait_for_state("FISHING", timeout=5.0)
        if state != "FISHING":
            logger.debug("Expected FISHING after cast, got %s", state)

    def _handle_fishing(self) -> None:
        """Wait for silence, then listen for audio bite."""
        self._wait_for_silence()
        logger.debug("Listening for bites...")

        consecutive_hits = 0
        while self.running:
            # Check state — if addon says we're no longer fishing, stop
            state = self._read_state()
            if state != "FISHING":
                logger.debug("Fishing ended (state=%s)", state)
                return

            peak = self.audio.get_peak_volume()
            if peak >= self.config.audio_threshold:
                consecutive_hits += 1
                if consecutive_hits >= self.config.confirm_polls:
                    self._sleep(0.8)  # human reaction to audio while distracted (0.5-1.1s range)
                    logger.info("Fish bite! Looting [key=%s]", self.config.loot_key)
                    send_key(self._hwnd, self.config.loot_key)
                    # Wait for state to leave FISHING (loot completes, channel ends)
                    self._wait_for_not_state("FISHING", timeout=5.0)
                    return
            else:
                consecutive_hits = 0

            time.sleep(self.config.poll_interval)

    def _handle_lure(self) -> None:
        """Addon wants us to press cast key to apply a lure via macro."""
        logger.info("Applying lure [key=%s]", self.config.cast_key)
        send_key(self._hwnd, self.config.cast_key)
        # Addon resets macro and goes IDLE after ~2s
        self._wait_for_not_state("LURE", timeout=5.0)

    def _handle_sell(self) -> None:
        """Handle sell sequence — addon controls macro, bot presses keys."""
        logger.info("SELL sequence started...")
        sell_start = time.monotonic()

        while self.running and time.monotonic() - sell_start < 60:
            state = self._read_state()

            if state == "SELL_ACTION":
                # Addon set the macro — press cast key, retry every 0.5s
                logger.info("Sell: pressing cast key (macro action)")
                send_key(self._hwnd, self.config.cast_key)
                time.sleep(0.5)

            elif state == "SELL_INTERACT":
                # Need to press interact key to open vendor
                self._sleep(0.5)
                logger.info("Sell: pressing interact key")
                send_key(self._hwnd, self.config.loot_key)
                # Wait for state to change
                self._wait_for_not_state("SELL_INTERACT", timeout=10.0)

            elif state == "SELL_WAIT":
                # Addon is processing, just wait
                time.sleep(0.5)

            elif state == "IDLE":
                logger.info("Sell sequence complete — resuming fishing.")
                return

            elif state == "NAV":
                # Sell triggered nav back to fishing spot
                self._run_nav()
                return

            else:
                # Unexpected state during sell — addon probably aborted
                logger.warning("Sell: unexpected state %s, exiting sell handler.", state)
                return

        # 60s global timeout
        logger.warning("Sell: global timeout (60s) — returning to main loop.")

    def _play_treasure_alarm(self) -> None:
        """Play a repeating alarm sound in a background thread."""
        def _alarm():
            for _ in range(6):
                # 800Hz for 300ms, pause 200ms — classic alarm pattern
                winsound.Beep(800, 300)
                time.sleep(0.2)
                winsound.Beep(1000, 300)
                time.sleep(0.2)
        threading.Thread(target=_alarm, daemon=True).start()

    def _read_all_pixels(self) -> tuple[str | None, int]:
        """Read all nav pixels in one capture.

        Returns (state, action) where state is from pixel 0 and
        action is the G channel of pixel 1 (0=stop, 1=turn_left).
        """
        pixels = self._pixel_reader.read_pixels(self._nav_positions)
        state = None
        if pixels[0]:
            r, g, b = pixels[0]
            state = match_state(r, g, b)
        action = 0
        if pixels[1]:
            action = pixels[1][1]  # G channel = nav action
        return state, action

    def _handle_treasure(self) -> None:
        """Spin + spam interact to find and loot treasure.

        Bot ALWAYS spams interact (F) every ~100ms during TREASURE_SPAWN.
        Addon controls turning via nav pixel 1:
          action=1 → hold left (spinning to scan)
          action=0 → release left (treasure found, click-to-move walking)
        Addon sets IDLE/NAV when treasure is looted or timed out.
        """
        logger.info("TREASURE SPAWNED — spinning + spamming interact...")
        if self.config.treasure_alarm:
            self._play_treasure_alarm()

        holding_left = False
        last_interact = 0.0
        try:
            start = time.monotonic()
            while self.running and time.monotonic() - start < 200:
                state, action = self._read_all_pixels()

                if state in ("IDLE", "NAV"):
                    logger.info("Treasure done (state=%s).", state)
                    if state == "NAV":
                        if holding_left:
                            key_up(self._hwnd, "left")
                            holding_left = False
                        self._run_nav()
                    return

                if state != "TREASURE_SPAWN":
                    # Unexpected state — don't get stuck
                    time.sleep(0.1)
                    continue

                # Turn control from addon
                if action >= 1 and not holding_left:
                    key_down(self._hwnd, "left")
                    holding_left = True
                elif action == 0 and holding_left:
                    key_up(self._hwnd, "left")
                    holding_left = False

                # Spam interact every 50ms — catches treasure while spinning,
                # triggers click-to-move, and loots when in range
                now = time.monotonic()
                if now - last_interact >= 0.05:
                    send_key(self._hwnd, self.config.loot_key)
                    last_interact = now

                time.sleep(0.02)

            logger.warning("Treasure global timeout (200s).")
        finally:
            if holding_left:
                key_up(self._hwnd, "left")

    # ------------------------------------------------------------------
    # Main loop — pure state machine
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._hwnd = self._find_wow()
        self._init_pixel_reader()
        self.audio.ensure_unmuted()
        self.running = True

        if self.config.silent:
            self.audio.set_muted(True)
            logger.info("Silent mode: WoW audio muted for you, bot still listening.")
            atexit.register(self._cleanup)
            signal.signal(signal.SIGTERM, lambda *_: self._cleanup())
            signal.signal(signal.SIGBREAK, lambda *_: self._cleanup())

        logger.info("Bot started. Press Ctrl+C to stop.")
        logger.info(
            "Config: loot=%s, cast=%s, threshold=%.3f, confirm=%d, "
            "humanize=%.0f%%, silent=%s",
            self.config.loot_key, self.config.cast_key,
            self.config.audio_threshold, self.config.confirm_polls,
            self.config.humanize * 100, self.config.silent,
        )

        try:
            while self.running:
                state = self._read_state()

                if state != self._last_state:
                    logger.debug("State: %s -> %s", self._last_state, state)
                    self._last_state = state

                if state == "IDLE":
                    self._handle_idle()

                elif state == "FISHING":
                    self._handle_fishing()

                elif state == "NAV":
                    self._run_nav()

                elif state == "TREASURE_SPAWN":
                    self._handle_treasure()

                elif state == "LURE":
                    self._handle_lure()

                elif state in ("SELL_ACTION", "SELL_INTERACT", "SELL_WAIT"):
                    self._handle_sell()

                else:
                    # TREASURE_TARGET, SPIRIT_SPAWN, CRAB_SPAWN, unknown
                    time.sleep(0.5)

        except KeyboardInterrupt:
            logger.info("Stopped by user.")
        finally:
            self._cleanup()
            self.running = False

    def _cleanup(self) -> None:
        if self.config.silent:
            self.audio.set_muted(False)
            logger.info("WoW audio unmuted.")

    def stop(self) -> None:
        self.running = False
