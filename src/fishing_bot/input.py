import ctypes
import ctypes.wintypes as wintypes

# Windows API constants
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

# Virtual key codes for common keys
VK_MAP: dict[str, int] = {
    **{str(i): 0x30 + i for i in range(10)},          # 0-9
    **{chr(i): i for i in range(ord("A"), ord("Z") + 1)},  # A-Z
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "escape": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}

user32 = ctypes.windll.user32

EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
PostMessageW = user32.PostMessageW
IsWindowVisible = user32.IsWindowVisible


def _key_to_vk(key: str) -> int:
    """Convert a key string to a Windows virtual key code."""
    normalized = key.strip().upper()
    if normalized in VK_MAP:
        return VK_MAP[normalized]
    # Try lowercase lookup for function keys etc.
    lower = key.strip().lower()
    if lower in VK_MAP:
        return VK_MAP[lower]
    raise ValueError(f"Unknown key: {key!r}. Supported: {', '.join(sorted(VK_MAP.keys()))}")


def _find_window_by_pid(pid: int) -> int | None:
    """Find the main window handle for a given process ID."""
    result = []

    def callback(hwnd, _lparam):
        proc_id = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid and IsWindowVisible(hwnd):
            result.append(hwnd)
        return True

    EnumWindows(EnumWindowsProc(callback), 0)
    return result[0] if result else None


def find_wow_window(process_name: str) -> tuple[int, int] | None:
    """Find WoW's window handle and process ID.

    Returns (hwnd, pid) or None if not found.
    """
    import psutil

    for proc in psutil.process_iter(["name", "pid"]):
        if proc.info["name"] and proc.info["name"].lower() == process_name.lower():
            hwnd = _find_window_by_pid(proc.info["pid"])
            if hwnd:
                return (hwnd, proc.info["pid"])
    return None


def send_key(hwnd: int, key: str) -> None:
    """Send a key press + release to a window handle via PostMessage.

    This works even if the window is minimized/in background.
    """
    vk = _key_to_vk(key)
    PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    PostMessageW(hwnd, WM_KEYUP, vk, 0)


def key_down(hwnd: int, key: str) -> None:
    """Hold a key down (no release). Use key_up() to release."""
    vk = _key_to_vk(key)
    PostMessageW(hwnd, WM_KEYDOWN, vk, 0)


def key_up(hwnd: int, key: str) -> None:
    """Release a held key."""
    vk = _key_to_vk(key)
    PostMessageW(hwnd, WM_KEYUP, vk, 0)
