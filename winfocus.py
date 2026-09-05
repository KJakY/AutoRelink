"""ゲームウィンドウをフォアグラウンドに強制するためのユーティリティ。

多くのゲーム（特に Unreal Engine 製タイトル）は、OS 上でフォアグラウンド
（アクティブ）になっていないウィンドウへのマウス/キー入力を無視する。
AutoRelink の GUI がフォーカスを持ったままだと、座標的には正しい位置を
クリックしていてもゲームに入力が届かないため、アクションの直前に
ゲームウィンドウを強制的にフォアグラウンドへ切り替える。

pywin32 等の追加依存なしで動くよう ctypes のみで実装している。
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_RESTORE = 9

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


MIN_WINDOW_WIDTH = 400
MIN_WINDOW_HEIGHT = 300


def _get_window_rect_size(hwnd):
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0
    return rect.right - rect.left, rect.bottom - rect.top


def find_window_by_partial_title(substr: str, exclude_pid: int = None):
    """タイトルに substr (大文字小文字区別なし) を含む可視ウィンドウを探す。

    タスクトレイの小さな通知ウィンドウ等を誤検出しないよう、一定サイズ未満の
    ウィンドウは除外する。exclude_pid を指定すると、そのプロセス ID が所有する
    ウィンドウは除外する（AutoRelink 自身を誤って検出しないようにするため）。

    戻り値: (hwnd, title, width, height) のタプル。見つからなければ
    (None, None, 0, 0)。
    """
    if not substr:
        return None, None, 0, 0
    substr_lower = substr.lower()
    result = {"hwnd": None, "title": None, "w": 0, "h": 0}

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if exclude_pid is not None:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == exclude_pid:
                return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if substr_lower not in title.lower():
            return True
        w, h = _get_window_rect_size(hwnd)
        if w < MIN_WINDOW_WIDTH or h < MIN_WINDOW_HEIGHT:
            return True
        result["hwnd"] = hwnd
        result["title"] = title
        result["w"] = w
        result["h"] = h
        return False

    user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return result["hwnd"], result["title"], result["w"], result["h"]


def is_window_valid(hwnd) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def is_foreground(hwnd) -> bool:
    return bool(hwnd) and user32.GetForegroundWindow() == hwnd


def focus_window(hwnd) -> bool:
    """AttachThreadInput を使い、Windows のフォーカス制限を回避してウィンドウを前面化する。"""
    if not hwnd:
        return False
    try:
        if is_foreground(hwnd):
            return True

        foreground_hwnd = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = (
            user32.GetWindowThreadProcessId(foreground_hwnd, None) if foreground_hwnd else 0
        )

        attached_current = False
        attached_foreground = False
        if target_thread and target_thread != current_thread:
            attached_current = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        if foreground_thread and foreground_thread != target_thread:
            attached_foreground = bool(
                user32.AttachThreadInput(foreground_thread, target_thread, True)
            )

        user32.ShowWindow(hwnd, SW_RESTORE)
        result = user32.SetForegroundWindow(hwnd)

        if attached_current:
            user32.AttachThreadInput(current_thread, target_thread, False)
        if attached_foreground:
            user32.AttachThreadInput(foreground_thread, target_thread, False)

        return bool(result)
    except Exception:
        return False
