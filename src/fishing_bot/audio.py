import comtypes
from ctypes import POINTER, cast
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation, ISimpleAudioVolume


class AudioMonitor:
    """Monitors audio output level for a specific process via Windows WASAPI."""

    def __init__(self, process_name: str):
        self.process_name = process_name
        self._meter: IAudioMeterInformation | None = None

    def _get_session(self):
        """Find the audio session for the target process."""
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process and session.Process.name() == self.process_name:
                return session
        return None

    def get_peak_volume(self) -> float:
        """Returns the current peak audio level (0.0 - 1.0) for the target process.

        Returns 0.0 if the process is not found or has no audio session.
        """
        try:
            session = self._get_session()
            if session is None:
                return 0.0

            meter = session._ctl.QueryInterface(IAudioMeterInformation)
            return meter.GetPeakValue()
        except comtypes.COMError:
            return 0.0

    def ensure_unmuted(self) -> None:
        """Make sure the WoW audio session is not muted (required for detection)."""
        try:
            session = self._get_session()
            if session is None:
                return

            volume = session._ctl.QueryInterface(ISimpleAudioVolume)
            if volume.GetMute():
                volume.SetMute(False, None)
        except comtypes.COMError:
            pass

    def set_muted(self, muted: bool) -> None:
        """Mute or unmute WoW's audio output for the user.

        The audio meter still reads peak values even when muted,
        so the bot continues to detect fish bites silently.
        """
        try:
            session = self._get_session()
            if session is None:
                return

            volume = session._ctl.QueryInterface(ISimpleAudioVolume)
            volume.SetMute(muted, None)
        except comtypes.COMError:
            pass
