import atexit
import random
import signal
import time
import logging

from .config import BotConfig
from .audio import AudioMonitor
from .input import find_wow_window, send_key
from .pixel import PixelReader, calibrate_pixel_positions
from .navigator import Navigator

logger = logging.getLogger(__name__)


class FishingBot:
    """Main fishing bot that ties audio detection and input together."""

    def __init__(self, config: BotConfig | None = None):
        self.config = config or BotConfig()
        self.audio = AudioMonitor(self.config.process_name)
        self.running = False
        self._hwnd: int | None = None
        self._pixel_reader: PixelReader | None = None
        self._nav_positions: list[tuple[int, int]] | None = None

    def _humanize(self, base_delay: float) -> float:
        """Add Gaussian jitter to a delay. Most values cluster near the base,
        with rare outliers — mimicking human reaction time variance."""
        if self.config.humanize <= 0:
            return base_delay
        sigma = base_delay * self.config.humanize / 3  # 99.7% within ±humanize range
        return max(0.05, base_delay + random.gauss(0, sigma))

    def _sleep(self, base_delay: float) -> None:
        """Sleep with optional humanized jitter."""
        actual = self._humanize(base_delay)
        logger.debug("Sleeping %.2fs (base=%.2f)", actual, base_delay)
        time.sleep(actual)

    def _wait_for_silence(self) -> None:
        """Wait until audio drops below threshold (cast sound fades)."""
        logger.debug("Waiting for silence...")
        time.sleep(0.5)
        while self.running:
            peak = self.audio.get_peak_volume()
            if peak < self.config.audio_threshold:
                break
            time.sleep(self.config.poll_interval)
        logger.debug("Silence detected, now listening for bites.")

    def _detect_bite(self) -> bool:
        """Wait for a confirmed fish bite or timeout.

        Returns True if a bite was detected, False if the cast timed out.
        """
        cast_time = time.monotonic()
        consecutive_hits = 0

        while self.running:
            elapsed = time.monotonic() - cast_time
            if elapsed >= self.config.fishing_timeout:
                logger.info("Fishing timeout (%.0fs) — no bite, re-casting.", elapsed)
                return False

            peak = self.audio.get_peak_volume()

            if peak >= self.config.audio_threshold:
                consecutive_hits += 1
                if consecutive_hits >= self.config.confirm_polls:
                    logger.debug(
                        "Bite confirmed (%d consecutive peaks, last=%.4f)",
                        consecutive_hits, peak,
                    )
                    return True
            else:
                consecutive_hits = 0

            time.sleep(self.config.poll_interval)

        return False

    def _find_wow(self) -> int:
        """Locate the WoW window. Raises if not found."""
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
        """Initialize pixel reader and calibrate positions."""
        self._pixel_reader = PixelReader(self._hwnd)
        self._nav_positions = calibrate_pixel_positions(self._hwnd)

    def _check_nav(self) -> bool:
        """Check pixel 0 for NAV state. If detected, run navigation.

        Returns True if nav was executed, False otherwise.
        """
        if not self._pixel_reader or not self._nav_positions:
            return False

        state = self._pixel_reader.read_state()
        if state != "NAV":
            return False

        logger.info("NAV state detected on pixel 0 — entering navigation mode.")
        nav = Navigator(self._hwnd, self._pixel_reader, self._nav_positions)
        success = nav.navigate()
        if success:
            logger.info("Navigation complete — resuming fishing.")
        else:
            logger.warning("Navigation aborted.")
        return True

    def _cast(self) -> None:
        """Cast the fishing rod."""
        logger.info("Casting fishing rod [key=%s]", self.config.cast_key)
        send_key(self._hwnd, self.config.cast_key)

    def _loot(self) -> None:
        """Loot the fish (interact with bobber)."""
        logger.info("Fish detected! Looting [key=%s]", self.config.loot_key)
        send_key(self._hwnd, self.config.loot_key)

    def start(self) -> None:
        """Start the fishing loop. Blocks until stopped or interrupted."""
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
            "loot_delay=%.1fs, timeout=%.0fs, humanize=%.0f%%, silent=%s",
            self.config.loot_key,
            self.config.cast_key,
            self.config.audio_threshold,
            self.config.confirm_polls,
            self.config.loot_delay,
            self.config.fishing_timeout,
            self.config.humanize * 100,
            self.config.silent,
        )

        try:
            while self.running:
                # Check if addon requests navigation before each cast
                self._check_nav()

                self._cast()
                self._wait_for_silence()

                if self._detect_bite():
                    self._loot()
                    self._sleep(self.config.loot_delay)
                # If timeout (no bite), loop re-casts automatically

        except KeyboardInterrupt:
            logger.info("Stopped by user.")
        finally:
            self._cleanup()
            self.running = False

    def _cleanup(self) -> None:
        """Restore WoW audio. Safe to call multiple times."""
        if self.config.silent:
            self.audio.set_muted(False)
            logger.info("WoW audio unmuted.")

    def stop(self) -> None:
        """Signal the bot to stop."""
        self.running = False
