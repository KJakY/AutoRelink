"""AutoRelink GUI アプリ。

GRANBLUE FANTASY: Relink のリザルト画面を自動で進めるツール。
テンプレート作成（キャリブレーション）と自動実行を1つの画面で行う。

起動:
    python app.py
"""

import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import keyboard
import pydirectinput
from PIL import Image, ImageTk

from capture import ScreenCapture
from detector import find_template
from winfocus import find_window_by_partial_title, focus_window, is_window_valid


def _get_base_dir():
    """設定・テンプレートを読み書きする永続フォルダ。

    exe化（PyInstaller）した場合、実行中の一時展開フォルダ（sys._MEIPASS）ではなく
    exe 本体があるフォルダを使う。そうしないと再キャリブレーションの結果が
    次回起動時に失われてしまう。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_bundle_dir(base_dir):
    """exe に同梱された初期データ（デフォルトのテンプレート等）の場所。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", base_dir)
    return base_dir


BASE_DIR = _get_base_dir()
BUNDLE_DIR = _get_bundle_dir(BASE_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
ICON_PATH = os.path.join(BASE_DIR, "assets", "app_icon.png")


def resolve_path(rel_path):
    """config.json に保存された相対パス（例: "templates/next_button.png"）を
    永続フォルダ (BASE_DIR) 基準の絶対パスに変換する。"""
    if not rel_path:
        return rel_path
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(BASE_DIR, rel_path)


def ensure_bundled_assets():
    """exe化されている場合、同梱されたデフォルトの config/テンプレート/アイコンを
    exe と同じフォルダにまだ無ければコピーする（初回起動時のみ）。"""
    if BUNDLE_DIR == BASE_DIR:
        return
    for name in ("config.json", "templates", "assets"):
        src = os.path.join(BUNDLE_DIR, name)
        dst = os.path.join(BASE_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

DEFAULT_CONFIG = {
    "monitor_index": 1,
    "poll_interval": 0.4,
    "post_key_delay": 0.8,
    "post_click_delay": 1.2,
    "match_threshold": 0.87,
    "next_button_template": "templates/next_button.png",
    "retry_unselected_template": "templates/retry_unselected.png",
    "confirm_yes_template": "templates/confirm_yes.png",
    "pause_hotkey": "f8",
    "stop_hotkey": "f10",
    "capture_hotkey": "f9",
    "game_window_title": "GRANBLUE FANTASY",
    "confirm_key": "enter",
    "select_up_key": "w",
    "continue_confirm_template": "templates/continue_confirm.png",
}

TEMPLATE_DEFS = [
    {
        "key": "next_button",
        "config_key": "next_button_template",
        "label": "① 「次へ」表示",
        "instruction": "リザルト画面などで「次へ」（マウスアイコン＋文字）が表示されている状態にする。",
    },
    {
        "key": "retry_unselected",
        "config_key": "retry_unselected_template",
        "label": "② 「③ 再挑戦する」（未選択）",
        "instruction": "報酬画面で「③ 再挑戦する」（まだ選択されていない状態）が表示されている状態にする。",
    },
    {
        "key": "confirm_yes",
        "config_key": "confirm_yes_template",
        "label": "③ 確認ポップアップ「はい」",
        "instruction": "「リザルトの確認を完了しますか？」のポップアップで、ハイライトされた「はい」部分が"
        "表示されている状態にする（「いいえ」は含めない）。",
    },
    {
        "key": "continue_confirm",
        "config_key": "continue_confirm_template",
        "label": "④ 継続確認ポップアップ",
        "instruction": "「引き続きこのクエストに挑戦しますか？」のポップアップ（一定回数ごとに表示、"
        "「いいえ」がデフォルトで選択されている）が表示されている状態にする。「再挑戦確認」の"
        "タイトルなど、①〜③のポップアップと紛らわしくない部分を選ぶ。",
    },
]

THUMB_SIZE = (150, 56)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class AutoRelinkApp:
    def __init__(self, root: tk.Tk):
        ensure_bundled_assets()

        self.root = root
        root.title("AutoRelink")
        root.geometry("780x680")
        root.minsize(680, 560)
        self._set_app_icon()

        self.cfg = load_config()
        os.makedirs(TEMPLATES_DIR, exist_ok=True)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.running_event = threading.Event()
        self.paused_event = threading.Event()
        self.automation_thread = None

        self.calib_cancel_event = threading.Event()
        self.calib_active = False

        self._hotkey_handles = []
        self.thumb_labels = {}
        self.thumb_images = {}
        self.capture_buttons = {}

        self._build_ui()
        self._refresh_all_thumbnails()
        self._register_global_hotkeys()
        self._poll_log_queue()

        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _set_app_icon(self):
        if not os.path.exists(ICON_PATH):
            return
        try:
            img = Image.open(ICON_PATH)
            self._icon_image = ImageTk.PhotoImage(img)
            self.root.iconphoto(True, self._icon_image)
        except Exception:
            pass

    # ---------------- UI ----------------
    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        calib_tab = ttk.Frame(notebook)
        run_tab = ttk.Frame(notebook)
        settings_tab = ttk.Frame(notebook)
        notebook.add(calib_tab, text="テンプレート作成")
        notebook.add(run_tab, text="実行")
        notebook.add(settings_tab, text="設定")

        self._build_calib_tab(calib_tab)
        self._build_run_tab(run_tab)
        self._build_settings_tab(settings_tab)

    def _build_calib_tab(self, parent):
        ttk.Label(
            parent,
            text=(
                "各状態の画面をテンプレートとして登録します。\n"
                "「キャプチャ開始」を押した後、ゲーム画面をフォーカスして対象の画面を表示させ、"
                f"{self.cfg.get('capture_hotkey', 'f9').upper()} キーを押してください。\n"
                "スクリーンショット取得後、選択ウィンドウでドラッグして範囲を選び Enter で確定します。"
            ),
            justify="left",
            wraplength=720,
        ).pack(anchor="w", padx=10, pady=(10, 8))

        for tdef in TEMPLATE_DEFS:
            row = ttk.Labelframe(parent, text=tdef["label"])
            row.pack(fill="x", padx=10, pady=6)

            thumb = ttk.Label(row, relief="groove", width=20, anchor="center", text="未作成")
            thumb.grid(row=0, column=0, rowspan=2, padx=8, pady=8)
            self.thumb_labels[tdef["key"]] = thumb

            ttk.Label(row, text=tdef["instruction"], wraplength=440, justify="left").grid(
                row=0, column=1, sticky="w", padx=8, pady=(8, 0)
            )

            btn = ttk.Button(
                row,
                text=f"キャプチャ開始 ({self.cfg.get('capture_hotkey', 'f9').upper()})",
                command=lambda t=tdef: self.start_calibration(t),
            )
            btn.grid(row=1, column=1, sticky="w", padx=8, pady=8)
            self.capture_buttons[tdef["key"]] = btn

            row.columnconfigure(1, weight=1)

        status_row = ttk.Frame(parent)
        status_row.pack(fill="x", padx=10, pady=(4, 10))
        self.calib_status_var = tk.StringVar(value="")
        ttk.Label(status_row, textvariable=self.calib_status_var, foreground="#0a5").pack(side="left")
        self.calib_cancel_btn = ttk.Button(
            status_row, text="キャンセル", command=self.cancel_calibration, state="disabled"
        )
        self.calib_cancel_btn.pack(side="right")

    def _build_run_tab(self, parent):
        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", padx=10, pady=10)

        self.start_btn = ttk.Button(btn_row, text="開始", command=self.start_automation)
        self.start_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(btn_row, text="一時停止", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_row, text="停止", command=self.stop_automation, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btn_row, text="スコア確認", command=self.check_scores).pack(side="left", padx=(16, 4))

        self.status_var = tk.StringVar(value="停止中")
        ttk.Label(parent, textvariable=self.status_var, font=("", 12, "bold")).pack(anchor="w", padx=10)

        hint = (
            f"ゲーム画面をフォーカスした状態で開始してください。 "
            f"{self.cfg.get('pause_hotkey','f8').upper()}: 一時停止/再開　"
            f"{self.cfg.get('stop_hotkey','f10').upper()}: 停止"
        )
        ttk.Label(parent, text=hint).pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Label(
            parent,
            text="「スコア確認」は現在の画面と各テンプレートの一致度を1回だけログに表示します"
            "（クリックやキー入力は行いません。しきい値調整やテンプレート不良の確認用）。",
            foreground="#666",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        log_frame = ttk.Frame(parent)
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log_text = tk.Text(log_frame, height=20, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_settings_tab(self, parent):
        self.setting_vars = {}
        fields = [
            ("match_threshold", "一致しきい値 (0〜1)"),
            ("poll_interval", "確認間隔 (秒)"),
            ("post_key_delay", "キー入力後の待機 (秒)"),
            ("post_click_delay", "クリック後の待機 (秒)"),
            ("monitor_index", "モニタ番号"),
            ("pause_hotkey", "一時停止キー"),
            ("stop_hotkey", "終了キー"),
            ("capture_hotkey", "テンプレート撮影キー"),
            ("game_window_title", "ゲームウィンドウ名（部分一致）"),
            ("confirm_key", "決定キー（次へ/はい 用）"),
            ("select_up_key", "上移動キー（継続確認ポップアップ用）"),
        ]
        container = ttk.Frame(parent)
        container.pack(anchor="nw", padx=10, pady=10)
        for i, (key, label) in enumerate(fields):
            ttk.Label(container, text=label).grid(row=i, column=0, sticky="w", padx=6, pady=6)
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            ttk.Entry(container, textvariable=var, width=22).grid(row=i, column=1, sticky="w", padx=6, pady=6)
            self.setting_vars[key] = var

        ttk.Button(container, text="保存", command=self.save_settings).grid(
            row=len(fields), column=0, columnspan=2, pady=(12, 4)
        )
        ttk.Button(container, text="ウィンドウ確認", command=self.check_game_window).grid(
            row=len(fields) + 1, column=0, columnspan=2, pady=(0, 12)
        )
        ttk.Label(
            container,
            text="「ウィンドウ確認」で、現在の設定でどのウィンドウが検出されるかログに表示します。\n"
            "ゲーム以外のウィンドウ（ブラウザ等）がヒットする場合は、ウィンドウ名をより具体的にしてください。",
            foreground="#666",
            justify="left",
        ).grid(row=len(fields) + 2, column=0, columnspan=2, sticky="w")

    # ---------------- ログ ----------------
    def log(self, message: str):
        self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {message}")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log_queue)

    # ---------------- サムネイル ----------------
    def _refresh_all_thumbnails(self):
        for tdef in TEMPLATE_DEFS:
            self._refresh_thumbnail(tdef["key"], tdef["config_key"])

    def _refresh_thumbnail(self, key, config_key):
        path = resolve_path(self.cfg.get(config_key))
        label = self.thumb_labels.get(key)
        if not label:
            return
        if path and os.path.exists(path):
            try:
                img = Image.open(path)
                img.thumbnail(THUMB_SIZE)
                photo = ImageTk.PhotoImage(img)
                self.thumb_images[key] = photo
                label.configure(image=photo, text="")
            except Exception as e:
                label.configure(image="", text="読込失敗")
                self.log(f"[エラー] テンプレート読込失敗 ({key}): {e}")
        else:
            label.configure(image="", text="未作成")

    # ---------------- キャリブレーション ----------------
    def start_calibration(self, tdef):
        if self.calib_active:
            messagebox.showinfo("AutoRelink", "他のテンプレート作成が進行中です。")
            return
        if self.running_event.is_set():
            messagebox.showinfo("AutoRelink", "自動実行中はテンプレート作成できません。先に停止してください。")
            return

        self.calib_active = True
        self.calib_cancel_event.clear()
        self.calib_cancel_btn.configure(state="normal")
        for b in self.capture_buttons.values():
            b.configure(state="disabled")

        capture_key = self.cfg.get("capture_hotkey", "f9")
        self.calib_status_var.set(
            f"[{tdef['label']}] ゲーム画面をフォーカスし、対象の画面を表示させて {capture_key.upper()} を押してください..."
        )

        thread = threading.Thread(target=self._calibration_worker, args=(tdef, capture_key), daemon=True)
        thread.start()

    def cancel_calibration(self):
        self.calib_cancel_event.set()

    def _calibration_worker(self, tdef, capture_key):
        pressed = threading.Event()

        def on_press():
            pressed.set()

        handle = keyboard.add_hotkey(capture_key, on_press)
        try:
            while not pressed.is_set() and not self.calib_cancel_event.is_set():
                time.sleep(0.05)
        finally:
            keyboard.remove_hotkey(handle)

        if self.calib_cancel_event.is_set():
            self.root.after(0, self._calibration_finished, tdef, None)
            return

        cap = ScreenCapture(self.cfg.get("monitor_index", 1))
        frame = cap.grab_bgr()
        self.root.after(0, self._show_roi_selector, tdef, frame)

    def _show_roi_selector(self, tdef, frame):
        self.calib_status_var.set(f"[{tdef['label']}] 範囲をドラッグして選択し、Enterで確定（Cでキャンセル）してください。")
        window_name = f"Select ROI: {tdef['key']}"
        roi = cv2.selectROI(window_name, frame, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(window_name)
        x, y, w, h = roi
        if w > 0 and h > 0:
            crop = frame[y : y + h, x : x + w]
            saved_path = os.path.join(TEMPLATES_DIR, f"{tdef['key']}.png")
            cv2.imwrite(saved_path, crop)
            rel_path = os.path.relpath(saved_path, BASE_DIR).replace("\\", "/")
            self.cfg[tdef["config_key"]] = rel_path
            save_config(self.cfg)
            self.log(f"[テンプレート作成] {tdef['label']} -> {saved_path} ({w}x{h})")
        else:
            self.log(f"[テンプレート作成] {tdef['label']} はキャンセルされました。")
            saved_path = None
        self._calibration_finished(tdef, saved_path)

    def _calibration_finished(self, tdef, saved_path):
        self.calib_active = False
        self.calib_cancel_btn.configure(state="disabled")
        for b in self.capture_buttons.values():
            b.configure(state="normal")
        self.calib_status_var.set("")
        self._refresh_thumbnail(tdef["key"], tdef["config_key"])

    # ---------------- 自動実行 ----------------
    def _load_template_or_none(self, path):
        path = resolve_path(path)
        if not path or not os.path.exists(path):
            return None
        return cv2.imread(path, cv2.IMREAD_GRAYSCALE)

    def start_automation(self):
        if self.calib_active:
            messagebox.showinfo("AutoRelink", "テンプレート作成中は開始できません。")
            return

        missing = [
            tdef["label"]
            for tdef in TEMPLATE_DEFS
            if not os.path.exists(resolve_path(self.cfg.get(tdef["config_key"], "")))
        ]
        if missing:
            if not messagebox.askyesno(
                "AutoRelink",
                "未作成のテンプレートがあります:\n" + "\n".join(missing) + "\n\nそのまま開始しますか？"
                "（未作成の項目は検出されません）",
            ):
                return

        self.running_event.set()
        self.paused_event.clear()
        self.automation_thread = threading.Thread(target=self._automation_worker, daemon=True)
        self.automation_thread.start()

        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="一時停止")
        self.stop_btn.configure(state="normal")
        self.status_var.set("実行中")
        self.log("自動実行を開始しました。")

    def toggle_pause(self):
        if not self.running_event.is_set():
            return
        if self.paused_event.is_set():
            self.paused_event.clear()
            self.pause_btn.configure(text="一時停止")
            self.status_var.set("実行中")
            self.log("再開しました。")
        else:
            self.paused_event.set()
            self.pause_btn.configure(text="再開")
            self.status_var.set("一時停止中")
            self.log("一時停止しました。")

    def stop_automation(self):
        if not self.running_event.is_set():
            return
        self.running_event.clear()
        self.paused_event.clear()
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="一時停止")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("停止中")
        self.log("自動実行を停止しました。")

    def check_scores(self):
        threading.Thread(target=self._check_scores_worker, daemon=True).start()

    def _check_scores_worker(self):
        cfg = self.cfg
        cap = ScreenCapture(cfg.get("monitor_index", 1))
        frame_gray = cap.grab_gray()
        threshold = float(cfg.get("match_threshold", 0.87))
        self.log(f"[スコア確認] しきい値={threshold:.2f} で判定中...")
        for tdef in TEMPLATE_DEFS:
            path = cfg.get(tdef["config_key"])
            tpl = self._load_template_or_none(path)
            if tpl is None:
                self.log(f"[スコア確認] {tdef['label']}: テンプレート未作成")
                continue
            _, _, _, _, score = find_template(frame_gray, tpl, 0.0)
            judged = "検出可" if score >= threshold else "検出不可"
            self.log(f"[スコア確認] {tdef['label']}: score={score:.3f} -> {judged}")

    def _automation_worker(self):
        cfg = self.cfg
        cap = ScreenCapture(cfg.get("monitor_index", 1))

        next_path = cfg.get("next_button_template")
        retry_path = cfg.get("retry_unselected_template")
        confirm_path = cfg.get("confirm_yes_template")
        continue_path = cfg.get("continue_confirm_template")

        next_tpl = self._load_template_or_none(next_path)
        retry_tpl = self._load_template_or_none(retry_path)
        confirm_tpl = self._load_template_or_none(confirm_path)
        continue_tpl = self._load_template_or_none(continue_path)
        threshold = float(cfg.get("match_threshold", 0.87))
        confirm_key = cfg.get("confirm_key", "enter")
        select_up_key = cfg.get("select_up_key", "w")

        pydirectinput.PAUSE = 0.02
        pydirectinput.FAILSAFE = False

        game_title = cfg.get("game_window_title", "GRANBLUE FANTASY")
        my_pid = os.getpid()
        game_hwnd, matched_title, mw, mh = find_window_by_partial_title(game_title, exclude_pid=my_pid)
        if game_hwnd:
            self.log(f"[情報] ゲームウィンドウを検出: '{matched_title}' ({mw}x{mh})")
        else:
            self.log(
                f"[警告] ゲームウィンドウが見つかりません（検索文字列: '{game_title}'）。"
                "設定タブの「ゲームウィンドウ名」を確認してください。フォーカス制御なしで続行します。"
            )

        def ensure_game_focus():
            nonlocal game_hwnd
            if game_hwnd and not is_window_valid(game_hwnd):
                game_hwnd = None
            if not game_hwnd:
                game_hwnd, _, _, _ = find_window_by_partial_title(game_title, exclude_pid=my_pid)
            if game_hwnd:
                focus_window(game_hwnd)
                time.sleep(0.05)

        while self.running_event.is_set():
            if self.paused_event.is_set():
                time.sleep(0.2)
                continue

            frame_gray = cap.grab_gray()

            if continue_tpl is not None:
                found, top_left, w, h, score = find_template(frame_gray, continue_tpl, threshold)
                if found:
                    self.log(
                        f"[検出] 継続確認ポップアップ (score={score:.2f}) -> "
                        f"{select_up_key}キー→{confirm_key}キーで「はい」を選択"
                    )
                    ensure_game_focus()
                    pydirectinput.press(select_up_key)
                    time.sleep(0.2)
                    pydirectinput.press(confirm_key)
                    time.sleep(float(cfg.get("post_click_delay", 1.2)))
                    continue

            if confirm_tpl is not None:
                found, top_left, w, h, score = find_template(frame_gray, confirm_tpl, threshold)
                if found:
                    self.log(f"[検出] 確認ポップアップ「はい」 (score={score:.2f}) -> {confirm_key}キーを押下")
                    ensure_game_focus()
                    pydirectinput.press(confirm_key)
                    time.sleep(float(cfg.get("post_click_delay", 1.2)))
                    continue

            if retry_tpl is not None:
                found, top_left, w, h, score = find_template(frame_gray, retry_tpl, threshold)
                if found:
                    self.log(f"[検出] 再挑戦 未選択 (score={score:.2f}) -> 3キーを押下")
                    ensure_game_focus()
                    pydirectinput.press("3")
                    time.sleep(float(cfg.get("post_key_delay", 0.8)))
                    continue

            if next_tpl is not None:
                found, top_left, w, h, score = find_template(frame_gray, next_tpl, threshold)
                if found:
                    self.log(f"[検出] 「次へ」表示 (score={score:.2f}) -> {confirm_key}キーを押下")
                    ensure_game_focus()
                    pydirectinput.press(confirm_key)
                    time.sleep(float(cfg.get("post_click_delay", 1.2)))
                    continue

            time.sleep(float(cfg.get("poll_interval", 0.4)))

        self.root.after(0, lambda: self.log("自動実行ループを終了しました。"))

    # ---------------- 設定 ----------------
    def save_settings(self):
        try:
            new_cfg = dict(self.cfg)
            new_cfg["match_threshold"] = float(self.setting_vars["match_threshold"].get())
            new_cfg["poll_interval"] = float(self.setting_vars["poll_interval"].get())
            new_cfg["post_key_delay"] = float(self.setting_vars["post_key_delay"].get())
            new_cfg["post_click_delay"] = float(self.setting_vars["post_click_delay"].get())
            new_cfg["monitor_index"] = int(self.setting_vars["monitor_index"].get())
            new_cfg["pause_hotkey"] = self.setting_vars["pause_hotkey"].get().strip() or "f8"
            new_cfg["stop_hotkey"] = self.setting_vars["stop_hotkey"].get().strip() or "f10"
            new_cfg["capture_hotkey"] = self.setting_vars["capture_hotkey"].get().strip() or "f9"
            new_cfg["game_window_title"] = (
                self.setting_vars["game_window_title"].get().strip() or "GRANBLUE FANTASY"
            )
            new_cfg["confirm_key"] = self.setting_vars["confirm_key"].get().strip() or "enter"
            new_cfg["select_up_key"] = self.setting_vars["select_up_key"].get().strip() or "w"
        except ValueError as e:
            messagebox.showerror("AutoRelink", f"入力値が不正です: {e}")
            return

        self.cfg = new_cfg
        save_config(self.cfg)
        self._register_global_hotkeys()

        capture_key = self.cfg.get("capture_hotkey", "f9").upper()
        for btn in self.capture_buttons.values():
            btn.configure(text=f"キャプチャ開始 ({capture_key})")

        self.log("設定を保存しました。")
        messagebox.showinfo("AutoRelink", "設定を保存しました。")

    def check_game_window(self):
        title = self.setting_vars["game_window_title"].get().strip() or self.cfg.get(
            "game_window_title", "GRANBLUE FANTASY"
        )
        hwnd, matched_title, w, h = find_window_by_partial_title(title, exclude_pid=os.getpid())
        if hwnd:
            msg = f"検索文字列 '{title}' -> 検出: '{matched_title}' ({w}x{h})"
        else:
            msg = f"検索文字列 '{title}' -> 見つかりませんでした。"
        self.log(f"[ウィンドウ確認] {msg}")
        messagebox.showinfo("ウィンドウ確認", msg)

    # ---------------- ホットキー ----------------
    def _register_global_hotkeys(self):
        for h in self._hotkey_handles:
            try:
                keyboard.remove_hotkey(h)
            except KeyError:
                pass
        self._hotkey_handles = []

        pause_key = self.cfg.get("pause_hotkey", "f8")
        stop_key = self.cfg.get("stop_hotkey", "f10")

        self._hotkey_handles.append(
            keyboard.add_hotkey(pause_key, lambda: self.root.after(0, self.toggle_pause))
        )
        self._hotkey_handles.append(
            keyboard.add_hotkey(stop_key, lambda: self.root.after(0, self.stop_automation))
        )

    # ---------------- 終了処理 ----------------
    def on_close(self):
        if self.calib_active:
            self.calib_cancel_event.set()
        self.running_event.clear()
        for h in self._hotkey_handles:
            try:
                keyboard.remove_hotkey(h)
            except KeyError:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    AutoRelinkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
