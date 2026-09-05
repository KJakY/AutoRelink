"""画面キャプチャ用ユーティリティ。mss を使ってフルスクリーン/ボーダーレスの
ゲーム画面をキャプチャする。ゲームがフォアグラウンドにあれば動作する。"""

import cv2
import numpy as np
import mss


class ScreenCapture:
    def __init__(self, monitor_index: int = 1):
        self.sct = mss.mss()
        monitors = self.sct.monitors
        if monitor_index >= len(monitors):
            monitor_index = 1
        self.monitor = monitors[monitor_index]

    def grab_bgr(self) -> np.ndarray:
        shot = self.sct.grab(self.monitor)
        frame = np.array(shot)[:, :, :3]  # BGRA -> BGR
        return frame

    def grab_gray(self) -> np.ndarray:
        frame = self.grab_bgr()
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
