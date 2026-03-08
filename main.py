"""WoW Fishing Bot - Detects fish bites via audio and sends keystrokes."""

import argparse
import logging

from src.fishing_bot.config import BotConfig
from src.fishing_bot.bot import FishingBot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WoW Fishing Bot - auto-loot fish by detecting audio from the game process."
    )
    parser.add_argument("--loot-key", default="f", help="Key to loot/interact with bobber (default: f)")
    parser.add_argument("--cast-key", default="1", help="Key to cast the fishing rod (default: 1)")
    parser.add_argument("--threshold", type=float, default=0.01, help="Audio peak threshold 0.0-1.0 (default: 0.01)")
    parser.add_argument("--cast-delay", type=float, default=2.0, help="Seconds to wait after looting before re-casting (default: 2.0)")
    parser.add_argument("--poll-interval", type=float, default=0.1, help="Seconds between audio checks (default: 0.1)")
    parser.add_argument("--process", default="Wow.exe", help="WoW process name (default: Wow.exe)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = BotConfig(
        loot_key=args.loot_key,
        cast_key=args.cast_key,
        audio_threshold=args.threshold,
        cast_delay=args.cast_delay,
        poll_interval=args.poll_interval,
        process_name=args.process,
    )

    bot = FishingBot(config)
    bot.start()


if __name__ == "__main__":
    main()
