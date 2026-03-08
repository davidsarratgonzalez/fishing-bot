from dataclasses import dataclass, field


@dataclass
class BotConfig:
    # Key sent to loot the fish (collect the bobber)
    loot_key: str = "f"
    # Key sent to cast the fishing rod
    cast_key: str = "1"
    # Audio volume threshold (0.0 - 1.0) to detect a fish bite
    audio_threshold: float = 0.01
    # Delay in seconds after looting before casting again
    cast_delay: float = 2.0
    # How often (seconds) to poll the audio level
    poll_interval: float = 0.1
    # WoW process name to find
    process_name: str = "Wow.exe"
