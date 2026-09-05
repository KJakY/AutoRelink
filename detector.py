"""テンプレートマッチングによる画面状態検出。"""

import cv2
import numpy as np


def find_template(frame_gray: np.ndarray, template_gray: np.ndarray, threshold: float = 0.87):
    """frame_gray の中から template_gray を探す。

    戻り値: (found, top_left(x, y), width, height, score)
    """
    result = cv2.matchTemplate(frame_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    h, w = template_gray.shape[:2]
    found = max_val >= threshold
    return found, max_loc, w, h, max_val
