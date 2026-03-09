import atexit
import random
import signal
import time
import logging

from .config import BotConfig
from .audio import AudioMonitor
from .input import find_wow_window, send_key, key_down, key_up
from .pixel import PixelReader, calibrate_pixel_positions
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

    def _handle_sell(self) -> None:
        """Handle sell sequence — addon controls macro, bot presses keys."""
        logger.info("SELL sequence started...")

        while self.running:
            state = self._read_state()

            if state == "SELL_ACTION":
                # Addon set the macro — press cast key
                self._sleep(0.5)
                logger.info("Sell: pressing cast key (macro action)")
                send_key(self._hwnd, self.config.cast_key)
                # Wait for state to change (addon advances step)
                self._wait_for_not_state("SELL_ACTION", timeout=10.0)

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
                # Unexpected state during sell
                logger.debug("Sell: unexpected state %s, waiting...", state)
                time.sleep(0.5)

    def _handle_treasure(self) -> None:
        """Spin to find treasure → interact → wait for nav back."""
        logger.info("TREASURE SPAWNED — spinning to find it...")

        key_down(self._hwnd, "left")
        try:
            start = time.monotonic()
            while self.running and time.monotonic() - start < 30:
                state = self._read_state()
                if state == "TREASURE_TARGET":
                    break
                if state not in ("TREASURE_SPAWN", "FISHING", None):
                    logger.warning("State changed to %s during spin, aborting.", state)
                    return
                time.sleep(0.1)
            else:
                logger.warning("Treasure spin timeout — not found.")
                return
        finally:
            key_up(self._hwnd, "left")

        logger.info("TREASURE TARGETED — pressing interact...")
        time.sleep(0.3)
        send_key(self._hwnd, self.config.loot_key)

        # Wait for addon to set NAV (after loot) or IDLE
        logger.info("Waiting for treasure loot...")
        start = time.monotonic()
        while self.running and time.monotonic() - start < 45:
            state = self._read_state()
            if state == "NAV":
                self._run_nav()
                return
            if state == "IDLE":
                return
            time.sleep(0.5)
        logger.warning("Treasure wait timeout.")

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
