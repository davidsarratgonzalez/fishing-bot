from dataclasses import dataclass


@dataclass
class BotConfig:
    # Key sent to loot the fish (collect the bobber)
    loot_key: str = "f"
    # Key sent to cast the fishing rod
    cast_key: str = "1"
    # Audio volume threshold (0.0 - 1.0) to detect a fish bite
    audio_threshold: float = 0.01
    # Delay in seconds after looting before casting again
    loot_delay: float = 0.5
    # Delay in seconds after casting before listening for bites
    cast_delay: float = 1.5
    # How often (seconds) to poll the audio level
    poll_interval: float = 0.1
    # WoW process name to find
    process_name: str = "Wow.exe"
    # Mute WoW audio for the user (bot still detects sound)
    silent: bool = False
    # Random jitter added to delays (0.0 = robotic, 0.5 = +/- 50% of each delay)
    humanize: float = 0.0
