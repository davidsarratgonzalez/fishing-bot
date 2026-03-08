import atexit
import random
import signal
import time
import logging

from .config import BotConfig
from .audio import AudioMonitor
from .input import find_wow_window, send_key

logger = logging.getLogger(__name__)


class FishingBot:
    """Main fishing bot that ties audio detection and input together."""

    def __init__(self, config: BotConfig | None = None):
        self.config = config or BotConfig()
        self.audio = AudioMonitor(self.config.process_name)
        self.running = False
        self._hwnd: int | None = None

    def _humanize(self, base_delay: float) -> float:
        """Add random jitter to a delay. Returns the actual sleep duration."""
        if self.config.humanize <= 0:
            return base_delay
        jitter = base_delay * self.config.humanize
        return max(0, base_delay + random.uniform(-jitter, jitter))

    def _sleep(self, base_delay: float) -> None:
        """Sleep with optional humanized jitter."""
        actual = self._humanize(base_delay)
        logger.debug("Sleeping %.2fs (base=%.2f)", actual, base_delay)
        time.sleep(actual)

    def _wait_for_silence(self) -> None:
        """Wait until audio drops below threshold (cast sound fades)."""
        logger.debug("Waiting for silence...")
        # First wait a minimum for the cast animation to start
        time.sleep(0.5)
        # Then drain any residual audio
        while self.running:
            peak = self.audio.get_peak_volume()
            if peak < self.config.audio_threshold:
                break
            time.sleep(self.config.poll_interval)
        logger.debug("Silence detected, now listening for bites.")

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
            "Config: loot=%s, cast=%s, threshold=%.3f, loot_delay=%.1fs, "
            "cast_delay=%.1fs, humanize=%.0f%%, silent=%s",
            self.config.loot_key,
            self.config.cast_key,
            self.config.audio_threshold,
            self.config.loot_delay,
            self.config.cast_delay,
            self.config.humanize * 100,
            self.config.silent,
        )

        # Initial cast
        self._cast()
        self._wait_for_silence()

        try:
            while self.running:
                peak = self.audio.get_peak_volume()

                if peak >= self.config.audio_threshold:
                    self._loot()
                    self._sleep(self.config.loot_delay)
                    self._cast()
                    self._wait_for_silence()

                time.sleep(self.config.poll_interval)
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
