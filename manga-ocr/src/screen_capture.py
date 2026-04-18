"""アクティブウィンドウのキャプチャ機能"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import platform
import subprocess
from pathlib import Path

from PIL import ImageGrab


class ActiveWindowCaptureError(RuntimeError):
    """アクティブウィンドウのキャプチャ失敗"""


def _get_active_window_bbox_windows() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if hwnd == 0:
        raise ActiveWindowCaptureError("アクティブウィンドウを取得できませんでした")

    rect = ctypes.wintypes.RECT()
    ok = user32.GetWindowRect(hwnd, ctypes.byref(rect))
    if ok == 0:
        raise ActiveWindowCaptureError("アクティブウィンドウの位置情報を取得できませんでした")

    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _run_command(args: list[str]) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _get_active_window_bbox_macos() -> tuple[int, int, int, int]:
    script = (
        'tell application "System Events"\n'
        '  tell (first process whose frontmost is true)\n'
        '    set p to position of front window\n'
        '    set s to size of front window\n'
        '    return (item 1 of p as text) & "," & (item 2 of p as text) & "," & '
        '           (item 1 of s as text) & "," & (item 2 of s as text)\n'
        '  end tell\n'
        'end tell'
    )

    try:
        output = _run_command(["osascript", "-e", script])
        x, y, width, height = [int(v) for v in output.split(",")]
    except Exception as exc:  # noqa: BLE001
        raise ActiveWindowCaptureError(
            "macOSでアクティブウィンドウを取得できませんでした。"
            "アクセシビリティ権限を許可してください。"
        ) from exc

    return x, y, x + width, y + height


def _get_active_window_bbox_linux() -> tuple[int, int, int, int]:
    try:
        window_id = _run_command(["xdotool", "getactivewindow"])
        geometry = _run_command(["xdotool", "getwindowgeometry", "--shell", window_id])
    except Exception as exc:  # noqa: BLE001
        raise ActiveWindowCaptureError(
            "Linuxでアクティブウィンドウを取得できませんでした。"
            "xdotoolをインストールしてください。"
        ) from exc

    values: dict[str, int] = {}
    for line in geometry.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"X", "Y", "WIDTH", "HEIGHT"}:
            values[key] = int(value)

    if not all(k in values for k in ("X", "Y", "WIDTH", "HEIGHT")):
        raise ActiveWindowCaptureError("アクティブウィンドウの位置情報が不正です")

    x, y, width, height = values["X"], values["Y"], values["WIDTH"], values["HEIGHT"]
    return x, y, x + width, y + height


def get_active_window_bbox() -> tuple[int, int, int, int]:
    system = platform.system()

    if system == "Windows":
        return _get_active_window_bbox_windows()
    if system == "Darwin":
        return _get_active_window_bbox_macos()
    if system == "Linux":
        return _get_active_window_bbox_linux()

    raise ActiveWindowCaptureError(f"未対応のOSです: {system}")


def capture_active_window(output_path: Path) -> Path:
    """アクティブウィンドウをキャプチャしてファイル保存する"""
    bbox = get_active_window_bbox()
    image = ImageGrab.grab(bbox=bbox)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path
