from dataclasses import dataclass


@dataclass
class BotConfig:
    # Key sent to loot the fish (collect the bobber)
    loot_key: str = "f"
    # Key sent to cast the fishing rod
    cast_key: str = "1"
    # Audio volume threshold (0.0 - 1.0) to detect a fish bite
    audio_threshold: float = 0.01
    # Consecutive polls above threshold needed to confirm a bite (reduces false positives)
    confirm_polls: int = 2
    # Delay in seconds after looting before next loop iteration
    loot_delay: float = 0.6
    # How often (seconds) to poll the audio level
    poll_interval: float = 0.1
    # WoW process name to find
    process_name: str = "Wow.exe"
    # Mute WoW audio for the user (bot still detects sound)
    silent: bool = False
    # Gaussian jitter on delays (0.0 = robotic, 0.3 = natural, 0.5 = erratic)
    humanize: float = 0.3
    # Play alarm sound on treasure spawn
    treasure_alarm: bool = False
    # Anti-detection: chance (0.0-1.0) of random AFK pause per cast cycle
    afk_chance: float = 0.01
    # Anti-detection: AFK pause duration range in seconds (min, max)
    afk_duration: tuple[float, float] = (30.0, 180.0)
    # Anti-detection: chance (0.0-1.0) of random jump between casts
    jump_chance: float = 0.03
