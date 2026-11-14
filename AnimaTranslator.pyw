# -*- coding: utf-8 -*-
"""Anima Prompt Translator - Windows GUI アプリ。

日本語で書いた絵の説明から、画像生成モデル Anima の書き方に沿った
英語プロンプトを LLM（DeepSeek Platform・OpenCode Go・OpenAI 互換のローカル LLM）に書かせる。

.pyw なのでダブルクリックで起動でき、コンソールウィンドウは出ない。
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import threading
import tkinter as tk
import urllib.error
import webbrowser
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import config
import deepseek
import grounding
import mcpdict
import prompts

APP_TITLE = "Anima Prompt Translator"

THEMES = {
    # 眩しくないよう、真っ黒ではなく少し青みのある暗いグレーを土台にする
    "dark": {
        "bg": "#14161a",            # 窓の地色
        "panel": "#191c21",         # 入出力欄
        "surface": "#23272e",       # カード・操作面
        "surface_hover": "#2c313a",
        "line": "#333842",          # 罫線
        "line_strong": "#454c58",
        "fg": "#e8ebef",            # 本文
        "muted": "#98a0ac",         # 補助文字
        "accent": "#4f9cf7",
        "accent_hover": "#6bacfa",
        "accent_off": "#30455f",    # 無効時
        "accent_fg": "#ffffff",
        "accent_fg_off": "#aebdce",
        "danger": "#e06c75",
        "danger_soft": "#3b272d",
        "ok": "#5fc78a",
        "select": "#274d78",
        "btn": "#23272e",
        "btn_active": "#2c313a",
        "btn_pressed": "#20242a",
        "btn_off": "#1d2025",
        "scroll": "#454c58",
        "dark_titlebar": True,
    },
    "light": {
        "bg": "#f3f4f8",
        "panel": "#ffffff",
        "surface": "#f7f8fa",
        "surface_hover": "#eef1f5",
        "line": "#d7dae4",
        "line_strong": "#b9bfca",
        "fg": "#1b1d26",
        "muted": "#6b7180",
        "accent": "#5b4bd6",
        "accent_hover": "#6d5ee0",
        "accent_off": "#b7b0ea",
        "accent_fg": "#ffffff",
        "accent_fg_off": "#f2f0ff",
        "danger": "#c33a3a",
        "danger_soft": "#fbe9e9",
        "ok": "#1f7a3d",
        "select": "#d9d5f8",
        "btn": "#e7e9f0",
        "btn_active": "#dde0ea",
        "btn_pressed": "#d3d6e2",
        "btn_off": "#eeeff4",
        "scroll": "#e2e4ec",
        "dark_titlebar": False,
    },
}
THEME_LABELS = {"dark": "ダーク", "light": "ライト"}
DEFAULT_THEME = "dark"
FONT_DEFAULT_LABEL = "既定（自動）"

# 以下は apply_theme() が書き換える。ウィジェット生成時に読まれる。
BG = PANEL = SURFACE = SURFACE_HOVER = LINE = LINE_STRONG = ""
FG = MUTED = ACCENT = ACCENT_HOVER = ACCENT_OFF = ""
ACCENT_FG = ACCENT_FG_OFF = DANGER = OK = SELECT = ""
DANGER_SOFT = ""
BTN = BTN_ACTIVE = BTN_PRESSED = BTN_OFF = SCROLL = ""
DARK_TITLEBAR = False


def apply_theme(name: str) -> None:
    """配色を切り替える。ウィジェットを作る前に呼ぶこと。"""
    global BG, PANEL, SURFACE, SURFACE_HOVER, LINE, LINE_STRONG
    global FG, MUTED, ACCENT, ACCENT_HOVER, ACCENT_OFF
    global ACCENT_FG, ACCENT_FG_OFF, DANGER, OK, SELECT
    global DANGER_SOFT
    global BTN, BTN_ACTIVE, BTN_PRESSED, BTN_OFF, SCROLL, DARK_TITLEBAR

    palette = THEMES.get(name, THEMES[DEFAULT_THEME])
    BG, PANEL = palette["bg"], palette["panel"]
    SURFACE, SURFACE_HOVER = palette["surface"], palette["surface_hover"]
    LINE, LINE_STRONG = palette["line"], palette["line_strong"]
    FG, MUTED = palette["fg"], palette["muted"]
    ACCENT, ACCENT_HOVER = palette["accent"], palette["accent_hover"]
    ACCENT_OFF, ACCENT_FG = palette["accent_off"], palette["accent_fg"]
    ACCENT_FG_OFF = palette["accent_fg_off"]
    DANGER, OK, SELECT = palette["danger"], palette["ok"], palette["select"]
    DANGER_SOFT = palette["danger_soft"]
    BTN, BTN_ACTIVE = palette["btn"], palette["btn_active"]
    BTN_PRESSED, BTN_OFF = palette["btn_pressed"], palette["btn_off"]
    SCROLL = palette["scroll"]
    DARK_TITLEBAR = palette["dark_titlebar"]


apply_theme(DEFAULT_THEME)

STYLE_LABELS = {
    "hybrid": "タグ＋補足文",
    "tags": "タグのみ",
    "natural": "自然文",
}
REASONING_EFFORTS = ("low", "high", "max")

SOURCE_COMPOSITION_HEADER = "構図・背景"
SOURCE_MALE_HEADER = "男性キャラクター"
SOURCE_FEMALE_HEADER = "女性キャラクター"
SOURCE_HEADER_RE = re.compile(
    r"^【(構図・背景|男性キャラクター \d+|女性キャラクター \d+)】$")


def build_source_text(composition: str, male: list[str], female: list[str]) -> str:
    """分割された日本語入力を、問い合わせ・履歴用の1つの文章にまとめる。"""
    sections = []
    composition = composition.strip()
    if composition:
        sections.append(f"【{SOURCE_COMPOSITION_HEADER}】\n{composition}")
    for header, characters in ((SOURCE_MALE_HEADER, male),
                               (SOURCE_FEMALE_HEADER, female)):
        for index, character in enumerate((value.strip() for value in characters
                                           if value.strip()), 1):
            sections.append(f"【{header} {index}】\n{character}")
    return "\n\n".join(sections)


def split_source_text(text: str) -> tuple[str, list[str], list[str]]:
    """履歴用の文章を画面の構図・男性・女性入力欄へ戻す。"""
    text = text.strip()
    if not text:
        return "", [], []

    composition_parts: list[str] = []
    male: list[str] = []
    female: list[str] = []
    current = "composition"
    buffer: list[str] = []
    found_header = False

    def flush() -> None:
        value = "\n".join(buffer).strip()
        if not value:
            return
        if current == "male":
            male.append(value)
        elif current == "female":
            female.append(value)
        else:
            composition_parts.append(value)

    for line in text.splitlines():
        match = SOURCE_HEADER_RE.fullmatch(line.strip())
        if not match:
            buffer.append(line)
            continue
        flush()
        buffer = []
        found_header = True
        header = match.group(1)
        if header.startswith(SOURCE_MALE_HEADER):
            current = "male"
        elif header.startswith(SOURCE_FEMALE_HEADER):
            current = "female"
        else:
            current = "composition"
    flush()

    if not found_header:
        return text, [], []
    return "\n\n".join(composition_parts), male, female

ICON_ART = [
    "................",
    "................",
    "................",
    "......####......",
    "......#..#......",
    ".....##..##.....",
    ".....#....#.....",
    "....##....##....",
    "....########....",
    "...##......##...",
    "...#........#...",
    "..##........##..",
    "..#..........#..",
    "................",
    "................",
    "................",
]


def _enable_dpi_awareness() -> float:
    """高 DPI 対応を宣言し、96dpi を 1.0 とした拡大率を返す。

    Tk は既定で DPI 非対応なので、宣言しないと Windows に画面ごと引き伸ばされて
    文字がぼやける。Tk() を作る前に呼ぶ必要がある。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor aware
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()       # Windows 8 以前向け
        except (AttributeError, OSError):
            return 1.0
    for probe in (lambda: ctypes.windll.user32.GetDpiForSystem(),
                  lambda: ctypes.windll.gdi32.GetDeviceCaps(
                      ctypes.windll.user32.GetDC(0), 88)):
        try:
            dpi = probe()
            if dpi:
                return max(1.0, dpi / 96.0)
        except (AttributeError, OSError):
            continue
    return 1.0


SCALE = _enable_dpi_awareness()


def px(value: float) -> int:
    """96dpi 基準の寸法を、実際の画面の画素数に直す。"""
    return max(1, int(round(value * SCALE)))


def _rounded_rectangle(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
                       radius: int, **kwargs):
    """Canvas上へアンチエイリアス不要の小さな角丸矩形を描く。"""
    radius = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=16, **kwargs)


class FlatIconButton(tk.Canvas):
    """コンパクトなクリック領域を持つ線画ボタン。"""

    def __init__(self, parent, icon: str, command, *, background: str,
                 danger: bool = False):
        self._size = px(26)
        super().__init__(
            parent, width=self._size, height=self._size, bg=background,
            borderwidth=0, highlightthickness=0, cursor="hand2", takefocus=1)
        self._icon = icon
        self._command = command
        self._background = background
        self._danger = danger
        self._hovered = False
        self._focused = False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<Button-1>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<Return>", self._invoke)
        self._draw()

    def set_background(self, color: str):
        self._background = color
        self.configure(bg=color)
        self._draw()

    def _set_hover(self, value: bool):
        self._hovered = value
        self._draw()

    def _set_focus(self, value: bool):
        self._focused = value
        self._draw()

    def _invoke(self, _event=None):
        self.focus_set()
        self._command()
        return "break"

    def _draw(self):
        self.delete("all")
        inset = px(2)
        fill = self._background
        icon_color = MUTED
        if self._hovered:
            fill = DANGER_SOFT if self._danger else SURFACE_HOVER
            icon_color = DANGER if self._danger else FG
        outline = ACCENT if self._focused else fill
        _rounded_rectangle(
            self, inset, inset, self._size - inset, self._size - inset, px(7),
            fill=fill, outline=outline, width=px(1))
        center = self._size // 2
        arm = px(6)
        width = px(1.5)
        if self._icon == "plus":
            self.create_line(center - arm, center, center + arm, center,
                             fill=icon_color, width=width, capstyle=tk.ROUND)
            self.create_line(center, center - arm, center, center + arm,
                             fill=icon_color, width=width, capstyle=tk.ROUND)
        else:
            self.create_line(center - arm, center - arm, center + arm, center + arm,
                             fill=icon_color, width=width, capstyle=tk.ROUND)
            self.create_line(center + arm, center - arm, center - arm, center + arm,
                             fill=icon_color, width=width, capstyle=tk.ROUND)


class ModernCheckbutton(tk.Canvas):
    """ネイティブ外観に依存しないフラットなチェックボックス。"""

    def __init__(self, parent, variable: tk.BooleanVar, command, *, background: str):
        self._size = px(24)
        super().__init__(
            parent, width=self._size, height=self._size, bg=background,
            borderwidth=0, highlightthickness=0, cursor="hand2", takefocus=1)
        self._variable = variable
        self._command = command
        self._background = background
        self._hovered = False
        self._focused = False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self._draw()

    def set_background(self, color: str):
        self._background = color
        self.configure(bg=color)
        self._draw()

    def refresh(self):
        self._draw()

    def _set_hover(self, value: bool):
        self._hovered = value
        self._draw()

    def _set_focus(self, value: bool):
        self._focused = value
        self._draw()

    def _toggle(self, _event=None):
        self.focus_set()
        self._variable.set(not self._variable.get())
        self._draw()
        self._command()
        return "break"

    def _draw(self):
        self.delete("all")
        checked = self._variable.get()
        box = px(16)
        start = (self._size - box) // 2
        end = start + box
        fill = ACCENT if checked else self._background
        outline = ACCENT if checked or self._hovered or self._focused else LINE_STRONG
        _rounded_rectangle(
            self, start, start, end, end, px(4), fill=fill,
            outline=outline, width=px(1))
        if checked:
            self.create_line(
                start + px(4), start + px(8), start + px(7), start + px(11),
                end - px(3), start + px(5), fill=ACCENT_FG,
                width=px(1.5), capstyle=tk.ROUND, joinstyle=tk.ROUND)


class AddCharacterButton(tk.Canvas):
    """性別を直接指定して追加する横長ボタン。"""

    def __init__(self, parent, label: str, command, font):
        self._height = px(34)
        super().__init__(
            parent, width=1, height=self._height, bg=PANEL, borderwidth=0,
            highlightthickness=0, cursor="hand2", takefocus=1)
        self._command = command
        self._label = label
        self._font = font
        self._hovered = False
        self._focused = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<Button-1>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<Return>", self._invoke)

    def _set_hover(self, value: bool):
        self._hovered = value
        self._draw()

    def _set_focus(self, value: bool):
        self._focused = value
        self._draw()

    def _invoke(self, _event=None):
        self.focus_set()
        self._command()
        return "break"

    def _draw(self):
        self.delete("all")
        width = max(self.winfo_width(), px(80))
        inset = px(1)
        fill = SURFACE_HOVER if self._hovered else PANEL
        outline = ACCENT if self._focused else (LINE_STRONG if self._hovered else LINE)
        _rounded_rectangle(
            self, inset, inset, width - inset, self._height - inset, px(8),
            fill=fill, outline=outline, width=px(1))
        label = self._label
        label_width = tkfont.Font(font=self._font).measure(label)
        gap = px(8)
        icon_width = px(14)
        left = (width - icon_width - gap - label_width) // 2
        center_y = self._height // 2
        center_x = left + icon_width // 2
        arm = px(6)
        self.create_line(center_x - arm, center_y, center_x + arm, center_y,
                         fill=ACCENT, width=px(1.5), capstyle=tk.ROUND)
        self.create_line(center_x, center_y - arm, center_x, center_y + arm,
                         fill=ACCENT, width=px(1.5), capstyle=tk.ROUND)
        self.create_text(
            left + icon_width + gap, center_y, text=label, anchor="w",
            fill=FG, font=self._font)


def style_titlebar(window: tk.Misc) -> None:
    """タイトルバーの明暗を配色に合わせる（Windows 10 1809 以降）。

    Windows 自体がダークモードだと既定で暗いタイトルバーになるため、
    ライト配色にしたときは明示的に戻す必要がある。
    """
    try:
        window.update_idletasks()
        handle = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        value = ctypes.c_int(1 if DARK_TITLEBAR else 0)
        for attribute in (20, 19):     # DWMWA_USE_IMMERSIVE_DARK_MODE（旧ビルドは 19）
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    handle, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        # 枠を作り直させないと、暗→明の切り替えが画面に出ない
        ctypes.windll.user32.SetWindowPos(handle, 0, 0, 0, 0, 0, 0x0027)
    except (AttributeError, OSError, tk.TclError):
        pass


def pick_font(*candidates: str) -> str:
    available = {f.lower() for f in tkfont.families()}
    for name in candidates:
        if name.lower() in available:
            return name
    return "TkDefaultFont"


def make_icon() -> tk.PhotoImage:
    """紫地に白い A のアイコンを実行時に組み立てる（外部ファイル不要）。"""
    zoom = 3 if SCALE > 1.25 else 2
    size = len(ICON_ART) * zoom
    img = tk.PhotoImage(width=size, height=size)
    rows = []
    for line in ICON_ART:
        pixels = []
        for char in line:
            pixels += ["#ffffff" if char == "#" else ACCENT] * zoom
        row = "{" + " ".join(pixels) + "}"
        rows += [row] * zoom
    img.put(" ".join(rows))
    return img


# --------------------------------------------------------------- 設定ダイアログ

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: "App"):
        super().__init__(parent)
        self.parent = parent
        self.result: dict | None = None

        self.title("設定")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)

        body = ttk.Frame(self, padding=px(16))
        body.pack(fill="both", expand=True)
        # 項目が多く 1 枚に並べると 1080p・150% の画面に収まらないので、タブに分ける。
        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)
        connection = self._tab(tabs, "接続")
        prompt = self._tab(tabs, "プロンプト")
        display = self._tab(tabs, "表示")

        # 接続先ごとの (キーの欄, その入力欄, 結果の表示)
        self.keys: dict[str, tuple[tk.StringVar, ttk.Entry, ttk.Label]] = {}
        self._build_keyed(connection, 0, "deepseek", "DeepSeek Platform の API キー",
                          "キーを取得する（platform.deepseek.com）")
        self._build_keyed(connection, 3, "opencode_go", "OpenCode Go の API キー",
                          "キーを取得する（opencode.ai/console）", top=12)
        self._build_local(connection, row=6)
        self._build_prompt(prompt)
        self._build_mcp(prompt, row=3)
        self._build_display(display)

        # --- ボタン ---
        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(px(14), 0))
        ttk.Button(buttons, text="キャンセル", command=self._cancel).pack(
            side="left", padx=(0, px(8)))
        ttk.Button(buttons, text="保存", style="Accent.TButton",
                   command=self._save).pack(side="left")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._save())
        self.keys["deepseek"][1].focus_set()
        self.update_idletasks()
        parent.center_over_self(self)
        style_titlebar(self)
        self.grab_set()

    @staticmethod
    def _tab(tabs: ttk.Notebook, title: str) -> ttk.Frame:
        frame = ttk.Frame(tabs, padding=(px(4), px(12), px(4), 0))
        frame.columnconfigure(0, weight=1)
        tabs.add(frame, text=title)
        return frame

    @staticmethod
    def _head(parent, row: int, title: str, hint: str, top: int = 0):
        ttk.Label(parent, text=title, style="Head.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(px(top), 0))
        ttk.Label(parent, text=hint, style="Muted.TLabel", wraplength=px(560),
                  justify="left").grid(row=row + 1, column=0, columnspan=3,
                                       sticky="w", pady=(px(2), px(6)))

    @staticmethod
    def _result_label(parent, row: int) -> ttk.Label:
        label = ttk.Label(parent, text="", style="Muted.TLabel",
                          wraplength=px(560), justify="left")
        label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        return label

    def _secret_entry(self, parent, row: int, variable: tk.StringVar) -> ttk.Entry:
        """伏せ字の入力欄と［表示］を並べる。"""
        entry = ttk.Entry(parent, textvariable=variable, width=52, show="●")
        entry.grid(row=row, column=0, sticky="ew")
        shown = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            parent, text="表示", variable=shown,
            command=lambda: entry.configure(show="" if shown.get() else "●")).grid(
                row=row, column=1, padx=(px(8), 0))
        return entry

    def _build_keyed(self, parent, row: int, name: str, title: str, link_text: str,
                     top: int = 0):
        """キーが要る接続先の欄。3 行使う。"""
        provider = deepseek.PROVIDERS[name]
        ttk.Label(parent, text=title, style="Head.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(px(top), px(4)))
        link = ttk.Label(parent, text=link_text, style="Link.TLabel", cursor="hand2")
        link.grid(row=row, column=0, columnspan=3, sticky="e", pady=(px(top), px(4)))
        link.bind("<Button-1>", lambda _e: webbrowser.open(provider.key_page))
        variable = tk.StringVar(value=self.parent.cfg.get(provider.key_field, ""))
        entry = self._secret_entry(parent, row + 1, variable)
        ttk.Button(parent, text="接続テスト", command=lambda: self._test(name)).grid(
            row=row + 1, column=2, padx=(px(8), 0))
        label = self._result_label(parent, row + 2)
        self.keys[name] = (variable, entry, label)

    def _build_local(self, parent, row: int):
        cfg = self.parent.cfg
        self._head(parent, row, "ローカル LLM",
                   "OpenAI 互換の API を持つサーバー。URL は /v1 まで書きます。",
                   top=18)
        ttk.Label(parent, text="ベース URL", style="Muted.TLabel").grid(
            row=row + 2, column=0, columnspan=3, sticky="w")
        self.local_url_var = tk.StringVar(value=cfg.get("local_url", ""))
        ttk.Entry(parent, textvariable=self.local_url_var, width=52).grid(
            row=row + 3, column=0, sticky="ew")
        ttk.Label(parent, text="API キー（任意）", style="Muted.TLabel").grid(
            row=row + 4, column=0, columnspan=3, sticky="w", pady=(px(6), 0))
        self.local_key_var = tk.StringVar(value=cfg.get("local_api_key", ""))
        self._secret_entry(parent, row + 5, self.local_key_var)
        ttk.Label(parent, text="モデル", style="Muted.TLabel").grid(
            row=row + 6, column=0, columnspan=3, sticky="w", pady=(px(6), 0))
        self.local_model_var = tk.StringVar(
            value=cfg["model"] if cfg["endpoint"] == "local" else "")
        self.local_model_box = ttk.Combobox(
            parent, textvariable=self.local_model_var, width=50,
            values=[model.id for model in self.parent.discovered.get("local", [])])
        self.local_model_box.grid(row=row + 7, column=0, sticky="ew")
        ttk.Button(parent, text="接続してモデル一覧を取得",
                   command=self._fetch_models).grid(
            row=row + 7, column=1, columnspan=2, sticky="e", padx=(px(8), 0))
        self.local_label = self._result_label(parent, row + 8)

    def _build_mcp(self, parent, row: int):
        self._head(parent, row, "Danbooru MCP（タグ照合・任意）",
                   "繋がれば全 115 万タグで照合します。\n空欄または接続不可なら"
                   "ローカル辞書（tags/danbooru.csv）に自動で切り替わります。",
                   top=18)
        self.mcp_var = tk.StringVar(value=self.parent.cfg.get("mcp_url", ""))
        ttk.Entry(parent, textvariable=self.mcp_var, width=52).grid(
            row=row + 2, column=0, sticky="ew")
        ttk.Button(parent, text="接続テスト", command=self._test_mcp).grid(
            row=row + 2, column=2, padx=(px(8), 0))
        self.mcp_label = self._result_label(parent, row + 3)

    def _build_prompt(self, parent):
        cfg = self.parent.cfg
        self._head(parent, 0, "追加指示（任意）",
                   "毎回のシステムプロンプトに足す常設の指示。"
                   "例: 補足文は 2 文までにする")
        self.extra = tk.Text(parent, height=6, width=52, wrap="word", relief="flat",
                             borderwidth=0, bg=PANEL, fg=FG, insertbackground=ACCENT,
                             selectbackground=SELECT, selectforeground=FG,
                             highlightthickness=px(1), highlightbackground=LINE,
                             highlightcolor=ACCENT,
                             padx=px(8), pady=px(6), font=self.parent.ui_font)
        self.extra.insert("1.0", cfg.get("extra", ""))
        self.extra.grid(row=2, column=0, columnspan=3, sticky="ew")

    def _build_display(self, parent):
        cfg = self.parent.cfg
        appearance = ttk.Frame(parent)
        appearance.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(appearance, text="配色", style="Head.TLabel").pack(side="left")
        self.theme_var = tk.StringVar(
            value=THEME_LABELS.get(cfg.get("theme", DEFAULT_THEME),
                                   THEME_LABELS[DEFAULT_THEME]))
        ttk.Combobox(appearance, textvariable=self.theme_var, state="readonly", width=10,
                     values=list(THEME_LABELS.values())).pack(side="left", padx=(px(12), 0))
        ttk.Label(appearance, text="フォント", style="Head.TLabel").pack(
            side="left", padx=(px(18), 0))
        font_families = sorted(
            {name for name in tkfont.families() if not name.startswith("@")},
            key=str.casefold)
        self.font_var = tk.StringVar(value=cfg.get("font_family", "") or FONT_DEFAULT_LABEL)
        ttk.Combobox(
            appearance, textvariable=self.font_var, state="readonly", width=24,
            values=[FONT_DEFAULT_LABEL, *font_families]).pack(
                side="left", padx=(px(12), 0))

        where = ttk.Frame(parent)
        where.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(18), 0))
        ttk.Label(where, text=f"保存先: {config.CONFIG_FILE}", style="Muted.TLabel",
                  wraplength=px(440), justify="left").pack(side="left")
        ttk.Button(where, text="フォルダを開く", command=self._open_folder).pack(side="right")
        ttk.Label(parent, text="API キーは Windows DPAPI（現在の Windows ユーザ専用）で"
                               "暗号化して保存されます。",
                  style="Muted.TLabel", wraplength=px(560), justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(px(4), 0))

    def _cancel(self):
        self.destroy()

    def _open_folder(self):
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(config.CONFIG_DIR)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"フォルダを開けません: {exc}", parent=self)

    def _run(self, label: ttk.Label, task):
        """task を裏で走らせ、(成否, 文) を label に出す。"""
        label.configure(text="接続中…", style="Muted.TLabel")
        self.update_idletasks()

        def show(ok: bool, message: str):
            if self.winfo_exists():          # 応答前に閉じられている場合がある
                label.configure(text=message, style="Ok.TLabel" if ok else "Error.TLabel")

        def worker():
            ok, message = task()
            try:
                self.after(0, show, ok, message)
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _test(self, name: str):
        """キーを確かめる。モデルを API から取る接続先は、一覧も取り直す。"""
        provider = deepseek.PROVIDERS[name]
        variable, _entry, label = self.keys[name]
        endpoint = deepseek.resolve_endpoint({provider.key_field: variable.get()}, name)
        effort = self.parent.current_reasoning_effort()
        cfg = self.parent.cfg
        saved_model = cfg["model"] if cfg["endpoint"] == name else ""

        def task():
            if provider.models:
                return deepseek.check_key(endpoint, provider.models[0], effort)
            if not endpoint.api_key:
                return False, "API キーが空です。"
            try:
                models = deepseek.list_models(endpoint)
            except urllib.error.HTTPError as exc:
                return False, deepseek.error_text(exc)
            except Exception as exc:
                return False, f"接続に失敗しました: {exc}"
            if not models:
                return False, "使えるモデルがありません。"
            self.parent.queue.put(("models", (name, models)))
            model = next((m for m in models if m.id == saved_model), models[0])
            ok, message = deepseek.check_key(
                endpoint, model.id, deepseek.pick_effort(effort, model.efforts))
            if not ok:
                return ok, message
            return True, f"接続できました。{len(models)} 個のモデルが使えます。"

        self._run(label, task)

    def _fetch_models(self):
        if not self.local_url_var.get().strip():
            self.local_label.configure(text="ベース URL が空です。", style="Error.TLabel")
            return
        endpoint = deepseek.resolve_endpoint(
            {"local_url": self.local_url_var.get(),
             "local_api_key": self.local_key_var.get()}, "local")

        def task():
            try:
                models = [model.id for model in deepseek.list_models(endpoint)]
            except urllib.error.HTTPError as exc:
                return False, deepseek.error_text(exc)
            except Exception as exc:
                return False, f"接続に失敗しました: {exc}"
            try:
                self.after(0, self._fill_models, models)
            except (tk.TclError, RuntimeError):
                pass
            return True, f"接続できました。{len(models)} 個のモデルが見つかりました。"

        self._run(self.local_label, task)

    def _fill_models(self, models: list[str]):
        if not self.winfo_exists():
            return
        self.local_model_box.configure(values=models)
        if models and not self.local_model_var.get().strip():
            self.local_model_var.set(models[0])

    def _test_mcp(self):
        url = self.mcp_var.get().strip()
        self._run(self.mcp_label, lambda: (
            mcpdict.Client(url).check() if url else (False, "URL が空です。")))

    def _save(self):
        local_model = self.local_model_var.get().strip()
        endpoint = "local" if local_model else self.parent.cfg["endpoint"]
        if endpoint in self.keys and not self.keys[endpoint][0].get().strip():
            _variable, entry, label = self.keys[endpoint]
            label.configure(text="API キーが空です。", style="Error.TLabel")
            entry.focus_set()
            return
        theme = next((name for name, label in THEME_LABELS.items()
                      if label == self.theme_var.get()), DEFAULT_THEME)
        self.result = {
            **{deepseek.PROVIDERS[name].key_field: variable.get().strip()
               for name, (variable, _entry, _label) in self.keys.items()},
            "local_url": self.local_url_var.get().strip(),
            "local_api_key": self.local_key_var.get().strip(),
            "local_model": local_model,
            "extra": self.extra.get("1.0", "end-1c").strip(),
            "theme": theme,
            "font_family": ("" if self.font_var.get() == FONT_DEFAULT_LABEL
                            else self.font_var.get().strip()),
            "mcp_url": self.mcp_var.get().strip(),
        }
        self.destroy()


# --------------------------------------------------------------- 履歴ダイアログ

class HistoryDialog(tk.Toplevel):
    def __init__(self, parent: "App"):
        super().__init__(parent)
        self.parent = parent
        self.title("履歴")
        self.configure(bg=BG)
        self.transient(parent)
        self.geometry(f"{px(720)}x{px(400)}")
        self.minsize(px(480), px(260))

        body = ttk.Frame(self, padding=px(12))
        body.pack(fill="both", expand=True)

        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(wrap, activestyle="none", bg=PANEL, fg=FG,
                                  selectbackground=SELECT, selectforeground=FG,
                                  highlightthickness=1, highlightbackground=LINE,
                                  borderwidth=0, font=parent.ui_font)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=bar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(px(10), 0))
        ttk.Button(buttons, text="全消去", command=self._clear_all).pack(side="left")
        ttk.Button(buttons, text="閉じる", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="この内容を復元", style="Accent.TButton",
                   command=self._restore).pack(side="right", padx=(0, px(8)))

        self.listbox.bind("<Double-Button-1>", lambda _e: self._restore())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._reload()
        parent.center_over_self(self)
        style_titlebar(self)
        self.grab_set()

    def _reload(self):
        self.listbox.delete(0, "end")
        for item in self.parent.cfg.get("history", []):
            src = " ".join(item.get("src", "").split())[:60]
            out = " ".join(item.get("out", "").split())[:60]
            note = " ".join(item.get("note", "").split())[:40]
            label = f"  {src}   →   {out}"
            if note:
                label = f"  [修正: {note}] {label.lstrip()}"
            self.listbox.insert("end", label)
        if not self.parent.cfg.get("history"):
            self.listbox.insert("end", "  （履歴はまだありません）")

    def _restore(self):
        selection = self.listbox.curselection()
        history = self.parent.cfg.get("history", [])
        if not selection or not history:
            return
        item = history[selection[0]]
        self.parent.set_source(item.get("src", ""))
        self.parent.set_output(item.get("out", ""))
        self.parent.set_hint(item.get("note", ""))
        self.parent.set_notes(item.get("why", ""))
        self.destroy()

    def _clear_all(self):
        if messagebox.askyesno(APP_TITLE, "履歴をすべて消去しますか？", parent=self):
            self.parent.cfg["history"] = []
            self.parent.save_config()
            self._reload()


# ------------------------------------------------------------------- 本体

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = config.load()
        self.queue: queue.Queue = queue.Queue()
        self.cancel: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.reasoning = ""
        self.ground_note = ""
        self.to_notes = False       # 受信中の断片を回答欄に流しているか
        self.delta_tail = ""        # マーカー判定のために持ち越した末尾
        # 接続先ごとに Models API から得た一覧
        self.discovered: dict[str, list[deepseek.ModelInfo]] = {}

        self._configure_fonts()

        self.title(APP_TITLE)
        self.configure(bg=BG)
        # ポイント指定のフォントが実寸どおりに描かれるようにする
        self.tk.call("tk", "scaling", SCALE * 96.0 / 72.0)
        self.minsize(px(900), px(520))
        self.geometry(self.cfg.get("geometry") or f"{px(1280)}x{px(680)}")
        try:
            self.icon = make_icon()
            self.iconphoto(True, self.icon)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(config.APP_NAME)
        except (tk.TclError, AttributeError, OSError):
            pass

        apply_theme(self.cfg.get("theme", DEFAULT_THEME))
        self.configure(bg=BG)
        self._init_styles()
        self._build()
        self._bind_keys()
        style_titlebar(self)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(40, self._drain)
        self.after(0, self._refresh_model_lists)
        self.composition.focus_set()
        if self._missing_key():
            self.after(300, self._first_run_notice)

    # -------------------------------------------------------------- 外観

    def _configure_fonts(self):
        requested = self.cfg.get("font_family", "").strip()
        family = pick_font(requested, "Yu Gothic UI", "Meiryo UI", "Segoe UI")
        self.ui_font = (family, 10)
        self.mono_font = ((family, 10) if requested else
                          (pick_font("Cascadia Mono", "Consolas", "Courier New"), 10))
        self.head_font = (family, 10, "bold")

    def rebuild(self):
        """配色を切り替えて画面を組み直す（入力内容は保持する）。"""
        composition = self.composition.get("1.0", "end-1c")
        characters = [
            (entry["gender"], entry["text"].get("1.0", "end-1c"),
             entry["enabled"].get())
            for entry in self.character_order]
        output = self.out.get("1.0", "end-1c")
        hint = self.hint.get("1.0", "end-1c")
        notes = self.notes.get("1.0", "end-1c")
        message, message_style = self.status.cget("text"), str(self.status.cget("style"))
        for child in self.winfo_children():
            child.destroy()

        apply_theme(self.cfg.get("theme", DEFAULT_THEME))
        self.configure(bg=BG)
        self._init_styles()
        self._build()
        self._bind_keys()
        style_titlebar(self)

        self._set_source_fields(composition, characters)
        self.set_output(output)
        self.set_hint(hint)
        self.set_notes(notes)
        self.status.configure(text=message, style=message_style or "Muted.TLabel")
        if self.reasoning:
            self.reason_btn.pack(side="right", padx=(0, px(8)))

    def _init_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, font=self.ui_font,
                        borderwidth=0, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Head.TLabel", font=self.head_font)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Ok.TLabel", foreground=OK)
        style.configure("Error.TLabel", foreground=DANGER)
        style.configure("Link.TLabel", foreground=ACCENT)
        style.configure("Panel.TFrame", background=PANEL)

        style.configure("TButton", background=BTN, foreground=FG, relief="flat",
                        padding=(px(14), px(7)), borderwidth=0)
        style.map("TButton",
                  background=[("pressed", BTN_PRESSED), ("active", BTN_ACTIVE),
                              ("disabled", BTN_OFF)],
                  foreground=[("disabled", MUTED)])
        style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_FG,
                        font=(self.ui_font[0], 10, "bold"))
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT), ("active", ACCENT_HOVER),
                              ("disabled", ACCENT_OFF)],
                  foreground=[("disabled", ACCENT_FG_OFF)])

        style.configure("TCheckbutton", background=BG, foreground=FG,
                        indicatorbackground=PANEL, indicatorforeground=ACCENT,
                        bordercolor=LINE)
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  indicatorbackground=[("pressed", BTN_ACTIVE), ("active", PANEL)],
                  bordercolor=[("active", ACCENT), ("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=PANEL, background=BTN, foreground=FG,
                        bordercolor=LINE, arrowcolor=MUTED, padding=px(4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", FG)],
                  selectbackground=[("readonly", PANEL)],
                  selectforeground=[("readonly", FG)],
                  arrowcolor=[("active", FG)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=FG,
                        insertcolor=FG, bordercolor=LINE, padding=px(6))
        style.configure("TNotebook", background=BG, bordercolor=LINE,
                        lightcolor=BG, darkcolor=BG, tabmargins=0)
        style.configure("TNotebook.Tab", background=BTN, foreground=MUTED,
                        bordercolor=LINE, lightcolor=BTN, darkcolor=BTN,
                        padding=(px(14), px(6)))
        # clam は選んだタブの余白と色を固定で持つので、ここで上書きする
        style.map("TNotebook.Tab",
                  padding=[("selected", (px(14), px(6)))],
                  expand=[("selected", (0, 0, 0, 0))],
                  background=[("selected", SURFACE_HOVER), ("active", BTN_ACTIVE),
                              ("", BTN)],
                  lightcolor=[("selected", SURFACE_HOVER), ("", BTN)],
                  foreground=[("selected", FG), ("", MUTED)])
        style.configure("Vertical.TScrollbar", background=SCROLL, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED)
        style.map("Vertical.TScrollbar", background=[("active", LINE)])
        style.layout(
            "Minimal.Vertical.TScrollbar",
            [("Vertical.Scrollbar.trough", {
                "sticky": "ns",
                "children": [("Vertical.Scrollbar.thumb", {
                    "expand": "1", "sticky": "nswe"})],
            })])
        style.configure(
            "Minimal.Vertical.TScrollbar", width=px(6), background=LINE,
            troughcolor=PANEL, bordercolor=PANEL, relief="flat", borderwidth=0)
        style.map(
            "Minimal.Vertical.TScrollbar",
            background=[("pressed", ACCENT), ("active", MUTED)])

        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ACCENT_FG)
        self.option_add("*TCombobox*Listbox.font", self.ui_font)

    def _text_widget(self, parent, mono=False, auto_height=False,
                     min_lines=1, background=None):
        surface = background or PANEL
        wrap = tk.Frame(parent, bg=surface)
        # width/height を最小にして、要求サイズが親のレイアウトを押し出さないようにする
        text = tk.Text(wrap, width=1, height=1,
                       wrap="word", undo=True, relief="flat", borderwidth=0,
                       bg=surface, fg=FG, insertbackground=ACCENT,
                       selectbackground=SELECT,
                       selectforeground=FG, highlightthickness=0,
                       padx=px(12), pady=px(10), spacing1=px(1), spacing3=px(3),
                       font=self.mono_font if mono else self.ui_font)
        bar = ttk.Scrollbar(
            wrap, orient="vertical", command=text.yview,
            style="Minimal.Vertical.TScrollbar")

        def sync_scrollbar(first, last):
            bar.set(first, last)
            needs_scroll = float(first) > 0.0 or float(last) < 1.0
            if needs_scroll and not bar.winfo_manager():
                bar.pack(side="right", fill="y")
            elif not needs_scroll and bar.winfo_manager():
                bar.pack_forget()

        text.configure(yscrollcommand=sync_scrollbar)
        text.pack(side="left", fill="both", expand=True)
        text._scrollbar = bar
        text._auto_height = auto_height
        text._min_lines = min_lines
        text._surface_background = surface
        if auto_height:
            text.bind(
                "<Configure>", lambda _e: self._resize_text_to_content(text), add="+")
            self.after_idle(lambda: self._resize_text_to_content(text))
        return wrap, text

    def _resize_text_to_content(self, text):
        if not getattr(text, "_auto_height", False) or not text.winfo_exists():
            return
        measured = text.count("1.0", "end", "displaylines")
        lines = max(getattr(text, "_min_lines", 1), measured[0] if measured else 1)
        if int(text.cget("height")) != lines:
            text.configure(height=lines)
        if hasattr(self, "character_canvas"):
            self._schedule_character_layout()

    def _card(self, parent, title):
        outer = tk.Frame(parent, bg=LINE, padx=px(1), pady=px(1))     # 枠線
        inner = tk.Frame(outer, bg=PANEL)
        inner.pack(fill="both", expand=True)

        head = tk.Frame(inner, bg=PANEL)
        head.pack(fill="x")
        caption = tk.Label(head, text=title, bg=PANEL, fg=FG, font=self.head_font,
                           anchor="w")
        caption.pack(side="left", padx=px(12), pady=(px(8), px(6)))
        meta = tk.Label(head, text="0 文字", bg=PANEL, fg=MUTED, font=self.ui_font)
        meta.pack(side="right", padx=px(12))
        tk.Frame(inner, bg=LINE, height=px(1)).pack(fill="x")
        return outer, inner, meta, caption

    def _add_character(self, gender: str, value: str = "", enabled: bool = True,
                       focus: bool = True):
        """男性または女性のキャラクター入力欄を1つ追加する。"""
        labels = {"male": "男性", "female": "女性"}
        entries = self.character_inputs[gender]
        card = tk.Frame(self.character_list, bg=LINE, padx=px(1), pady=px(1))
        card.pack(fill="x", pady=(0, px(8)))
        inner = tk.Frame(card, bg=SURFACE)
        inner.pack(fill="both", expand=True)
        head = tk.Frame(inner, bg=SURFACE)
        head.pack(fill="x", padx=px(8), pady=(px(6), 0))
        number = tk.Label(head, text=f"{labels[gender]} {len(entries) + 1}",
                          bg=SURFACE, fg=FG, font=self.head_font, anchor="w")
        number.pack(side="left")
        enabled_var = tk.BooleanVar(value=enabled)
        wrap, text = self._text_widget(
            inner, auto_height=True, background=SURFACE)
        text.configure(height=1)
        wrap.pack(fill="x", expand=True)
        if value:
            text.insert("1.0", value)
        entry = {
            "gender": gender, "card": card, "inner": inner, "head": head,
            "wrap": wrap, "label": number, "text": text, "enabled": enabled_var,
        }
        delete_button = FlatIconButton(
            head, "delete", lambda: self._delete_character(gender, entry),
            background=SURFACE, danger=True)
        delete_button.pack(side="right")
        checkbutton = ModernCheckbutton(
            head, enabled_var, lambda: self._toggle_character(entry),
            background=SURFACE)
        checkbutton.pack(side="right", padx=(0, px(4)))
        entry["delete_button"] = delete_button
        entry["checkbutton"] = checkbutton
        entries.append(entry)
        self.character_order.append(entry)
        text.bind("<<Modified>>", self._on_modified)
        self._bind_text_shortcuts(text)
        self._toggle_character(entry, update=False)
        self._schedule_character_layout()
        if focus:
            text.focus_set()

    def _toggle_character(self, entry, update: bool = True):
        enabled = entry["enabled"].get()
        surface = SURFACE if enabled else PANEL
        for widget in (entry["inner"], entry["head"], entry["wrap"]):
            widget.configure(bg=surface)
        entry["label"].configure(bg=surface, fg=FG if enabled else MUTED)
        entry["text"].configure(
            state="normal" if enabled else "disabled",
            fg=FG if enabled else MUTED,
            bg=surface,
            insertbackground=ACCENT if enabled else MUTED)
        entry["delete_button"].set_background(surface)
        entry["checkbutton"].set_background(surface)
        entry["checkbutton"].refresh()
        if update:
            self.update_counts()

    def _delete_character(self, gender: str, entry):
        entries = self.character_inputs[gender]
        if entry not in entries:
            return
        entries.remove(entry)
        self.character_order.remove(entry)
        entry["card"].destroy()
        self.character_list.configure(height=1)
        label = "男性" if gender == "male" else "女性"
        for index, item in enumerate(entries, 1):
            item["label"].configure(text=f"{label} {index}")
        self.update_counts()
        self._schedule_character_layout()

    def _schedule_character_layout(self):
        self.after_idle(self._update_character_layout)

    def _update_character_layout(self):
        if not self.character_canvas.winfo_exists():
            return
        content_height = self.character_grid.winfo_reqheight()
        self.character_canvas.itemconfigure(
            self.character_window, height=content_height)
        bounds = self.character_canvas.bbox("all")
        self.character_canvas.configure(scrollregion=bounds)
        if content_height > self.character_canvas.winfo_height():
            self.character_bar.grid()
        else:
            self.character_bar.grid_remove()

    def _character_values(self, gender: str, active_only: bool = False) -> list[str]:
        return [entry["text"].get("1.0", "end-1c")
                for entry in self.character_inputs[gender]
                if not active_only or entry["enabled"].get()]

    def get_source_text(self) -> str:
        return build_source_text(
            self.composition.get("1.0", "end-1c"),
            self._character_values("male", active_only=True),
            self._character_values("female", active_only=True))

    # -------------------------------------------------------------- 画面

    def _build(self):
        root = ttk.Frame(self, padding=px(12))
        root.pack(fill="both", expand=True)

        # ツールバー
        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(0, px(10)))

        ttk.Label(bar, text="モデル", style="Muted.TLabel").pack(side="left")
        self.model_var = tk.StringVar(value=self._model_label())
        self.model_box = ttk.Combobox(
            bar, textvariable=self.model_var, state="readonly", width=38,
            values=list(self._model_choices()))
        self.model_box.pack(side="left", padx=(px(6), px(16)))
        self.model_box.bind("<<ComboboxSelected>>", self._on_model_changed)

        ttk.Label(bar, text="effort", style="Muted.TLabel").pack(side="left")
        effort = self.cfg.get("reasoning_effort", config.DEFAULTS["reasoning_effort"])
        if effort not in REASONING_EFFORTS:
            effort = config.DEFAULTS["reasoning_effort"]
        self.reasoning_effort_var = tk.StringVar(value=effort)
        self.reasoning_effort_box = ttk.Combobox(
            bar, textvariable=self.reasoning_effort_var, state="readonly", width=6,
            values=REASONING_EFFORTS)
        self.reasoning_effort_box.pack(side="left", padx=(px(6), px(16)))

        ttk.Label(bar, text="出力形式", style="Muted.TLabel").pack(side="left")
        self.style_var = tk.StringVar(value=STYLE_LABELS[self.cfg["style"]])
        style_box = ttk.Combobox(bar, textvariable=self.style_var, state="readonly", width=14,
                                 values=list(STYLE_LABELS.values()))
        style_box.pack(side="left", padx=(px(6), 0))
        style_box.bind("<<ComboboxSelected>>", lambda _e: self.save_config())

        ttk.Button(bar, text="設定", command=self.open_settings).pack(side="right")
        ttk.Button(bar, text="履歴", command=self.open_history).pack(
            side="right", padx=(0, px(8)))

        # 下段を先に確保してから入出力欄に残りを渡す
        # （pack は先に配置したものを優先するため、逆順だとボタン列が押し出される）
        self.status = ttk.Label(root, text="準備完了", style="Muted.TLabel", anchor="w")
        self.status.pack(side="bottom", fill="x", pady=(px(8), 0))

        actions = ttk.Frame(root)
        actions.pack(side="bottom", fill="x", pady=(px(10), 0))
        self.translate_btn = ttk.Button(actions, text="生成  (Ctrl+Enter)",
                                        style="Accent.TButton", command=self.translate)
        self.translate_btn.pack(side="left")
        self.revise_btn = ttk.Button(actions, text="指示で修正  (Ctrl+Shift+Enter)",
                                     command=self.revise)
        self.revise_btn.pack(side="left", padx=(px(8), 0))
        ttk.Button(actions, text="クリア", command=self.clear).pack(
            side="left", padx=(px(8), 0))
        self.reason_btn = ttk.Button(actions, text="推論過程", command=self.show_reasoning)
        self.copy_btn = ttk.Button(actions, text="コピー", style="Accent.TButton",
                                   command=self.copy_output)
        self.copy_btn.pack(side="right")

        # 入出力
        panes = ttk.Frame(root)
        panes.pack(fill="both", expand=True)
        # 指示欄は入力・出力より狭くてよい（3 : 3 : 2）
        panes.columnconfigure(0, weight=3, uniform="pane")
        panes.columnconfigure(1, weight=3, uniform="pane")
        panes.columnconfigure(2, weight=2, uniform="pane")
        panes.rowconfigure(0, weight=1)

        left, left_in, self.src_meta, _caption = self._card(panes, "日本語")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(6)))
        character_area = tk.Frame(left_in, bg=PANEL)
        character_area.pack(fill="both", expand=True)
        character_area.columnconfigure(0, weight=1)
        character_area.rowconfigure(0, weight=1)
        self.character_canvas = tk.Canvas(
            character_area, bg=PANEL, highlightthickness=0, borderwidth=0)
        self.character_bar = ttk.Scrollbar(
            character_area, orient="vertical", command=self.character_canvas.yview,
            style="Minimal.Vertical.TScrollbar")
        self.character_canvas.configure(yscrollcommand=self.character_bar.set)
        self.character_canvas.grid(row=0, column=0, sticky="nsew")
        self.character_bar.grid(row=0, column=1, sticky="ns")
        self.character_bar.grid_remove()

        self.character_grid = tk.Frame(self.character_canvas, bg=PANEL)
        self.character_grid.columnconfigure(0, weight=1)
        self.character_window = self.character_canvas.create_window(
            (0, 0), window=self.character_grid, anchor="nw")
        self.character_grid.bind(
            "<Configure>",
            lambda _e: self._schedule_character_layout())
        self.character_canvas.bind(
            "<Configure>",
            lambda event: (self.character_canvas.itemconfigure(
                self.character_window, width=event.width),
                self._schedule_character_layout()))

        self.character_inputs = {"male": [], "female": []}
        self.character_order = []

        self.composition_wrap, self.composition = self._text_widget(
            self.character_grid, auto_height=True, min_lines=4)
        self.composition.configure(height=4)
        self.composition_wrap.grid(row=0, column=0, sticky="ew")

        self.input_divider = tk.Frame(self.character_grid, bg=LINE, height=px(1))
        self.input_divider.grid(row=1, column=0, sticky="ew")

        self.character_header = tk.Frame(self.character_grid, bg=PANEL)
        self.character_header.grid(
            row=2, column=0, sticky="ew", padx=px(10), pady=(px(6), px(4)))
        tk.Label(
            self.character_header, text="キャラクター", bg=PANEL, fg=FG,
            font=self.head_font, anchor="w").pack(side="left")

        self.character_list = tk.Frame(self.character_grid, bg=PANEL)
        self.character_list.grid(
            row=3, column=0, sticky="ew", padx=px(10))

        character_actions = tk.Frame(self.character_grid, bg=PANEL)
        character_actions.columnconfigure(0, weight=1, uniform="character-action")
        character_actions.columnconfigure(1, weight=1, uniform="character-action")
        self.add_male_button = AddCharacterButton(
            character_actions, "男性を追加", lambda: self._add_character("male"),
            self.ui_font)
        self.add_male_button.grid(row=0, column=0, sticky="ew", padx=(0, px(4)))
        self.add_female_button = AddCharacterButton(
            character_actions, "女性を追加", lambda: self._add_character("female"),
            self.ui_font)
        self.add_female_button.grid(row=0, column=1, sticky="ew", padx=(px(4), 0))
        character_actions.grid(
            row=4, column=0, sticky="ew", padx=px(10), pady=(0, px(10)))

        right, right_in, self.out_meta, _caption = self._card(panes, "プロンプト（Anima）")
        right.grid(row=0, column=1, sticky="nsew", padx=px(6))
        wrap, self.out = self._text_widget(right_in, mono=True)
        wrap.pack(fill="both", expand=True)

        # 3 列目は上下 2 段。上に指示を書き、下に AI の回答が入る。
        aside = ttk.Frame(panes)
        aside.grid(row=0, column=2, sticky="nsew", padx=(px(6), 0))
        aside.columnconfigure(0, weight=1)
        aside.rowconfigure(0, weight=2)
        aside.rowconfigure(1, weight=3)

        upper, upper_in, self.hint_meta, _caption = self._card(aside, "AI への指示")
        upper.grid(row=0, column=0, sticky="nsew", pady=(0, px(6)))
        wrap, self.hint = self._text_widget(upper_in)
        wrap.pack(fill="both", expand=True)

        lower, lower_in, self.notes_meta, _caption = self._card(aside, "AI の回答")
        lower.grid(row=1, column=0, sticky="nsew")
        wrap, self.notes = self._text_widget(lower_in)
        wrap.pack(fill="both", expand=True)

        # 照合パスで動いたタグの目印。Text タグなので後の編集でも位置がずれない。
        self.marks = {}
        self.out.tag_configure("swap", foreground=OK)
        self.out.tag_configure("kept", foreground=DANGER)
        self.out.bind("<Motion>", self._on_out_motion)
        self.out.bind("<Leave>", lambda _e: self.update_counts())
        self.out.bind("<Button-3>", self._on_out_menu)

        # 初回起動時だけ、そのまま生成して試せる例文を入れておく（編集可）
        if not self.cfg.get("history"):
            self.composition.insert("1.0", "月夜の廃教会、俯瞰、全身")
            self._add_character("female", focus=False)
            self.character_inputs["female"][0]["text"].insert(
                "1.0", "銀髪ロングツインテール、赤い瞳、黒いゴシックドレス")

        for widget in (self.composition, self.out, self.hint, self.notes):
            widget.bind("<<Modified>>", self._on_modified)

    def _bind_keys(self):
        self.bind("<Control-Return>", self._on_translate_key)
        self.bind("<Control-Shift-Return>", self._on_revise_key)
        self.bind("<Escape>", lambda _e: self.cancel_translation())
        widgets = ([self.composition, self.out, self.hint, self.notes]
                   + [entry["text"] for entry in self.character_inputs["male"]]
                   + [entry["text"] for entry in self.character_inputs["female"]])
        for widget in widgets:
            self._bind_text_shortcuts(widget)

    def _bind_text_shortcuts(self, widget):
        widget.bind("<Control-Return>", self._on_translate_key)
        widget.bind("<Control-Shift-Return>", self._on_revise_key)
        widget.bind("<Control-a>", self._select_all)

    def _on_translate_key(self, _event):
        self.translate()
        return "break"          # Text に改行を挿入させない

    def _on_revise_key(self, _event):
        self.revise()
        return "break"

    @staticmethod
    def _select_all(event):
        event.widget.tag_add("sel", "1.0", "end-1c")
        return "break"

    # -------------------------------------------------------------- 状態

    def _on_modified(self, event):
        event.widget.edit_modified(False)
        self._resize_text_to_content(event.widget)
        self.update_counts()

    def update_counts(self):
        self._resize_text_to_content(self.composition)
        for gender in ("male", "female"):
            for entry in self.character_inputs[gender]:
                self._resize_text_to_content(entry["text"])
        source_length = len(self.composition.get("1.0", "end-1c"))
        source_length += sum(len(value) for value in
                             self._character_values("male", active_only=True))
        source_length += sum(len(value) for value in
                             self._character_values("female", active_only=True))
        self.src_meta.configure(text=f"{source_length} 文字")
        self.out_meta.configure(text=f"{len(self.out.get('1.0', 'end-1c'))} 文字",
                                fg=MUTED)
        self.hint_meta.configure(text=f"{len(self.hint.get('1.0', 'end-1c'))} 文字")
        self.notes_meta.configure(text=f"{len(self.notes.get('1.0', 'end-1c'))} 文字")

    def set_source(self, text: str):
        composition, male, female = split_source_text(text)
        characters = ([('male', value, True) for value in male]
                      + [('female', value, True) for value in female])
        self._set_source_fields(composition, characters)

    def _set_source_fields(self, composition: str,
                           characters: list[tuple[str, str, bool]]):
        self.composition.delete("1.0", "end")
        self.composition.insert("1.0", composition)
        for child in self.character_list.winfo_children():
            child.destroy()
        self.character_list.configure(height=1)
        self.character_inputs = {"male": [], "female": []}
        self.character_order = []
        for gender, value, enabled in characters:
            self._add_character(gender, value, enabled=enabled, focus=False)
        self.update_counts()
        self._schedule_character_layout()

    def set_output(self, text: str):
        self.out.delete("1.0", "end")
        self.marks = {}
        self.out.insert("1.0", text)
        self.update_counts()

    def set_hint(self, text: str):
        self.hint.delete("1.0", "end")
        self.hint.insert("1.0", text)
        self.update_counts()

    def set_notes(self, text: str):
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text)
        self.update_counts()

    # ----------------------------------------- プロンプトと回答の振り分け

    def _append_delta(self, chunk: str):
        """届いた断片を、WHY マーカーの前後で出力欄と回答欄に振り分ける。

        マーカーが断片の境目で割れても拾えるよう、末尾を少しだけ持ち越す。
        """
        if self.to_notes:
            self.notes.insert("end", chunk)
            self.notes.see("end")
            return

        buffer = self.delta_tail + chunk
        position = buffer.find(prompts.WHY_MARKER)
        if position >= 0:
            self.out.insert("end", buffer[:position])
            self.to_notes = True
            self.delta_tail = ""
            rest = buffer[position + len(prompts.WHY_MARKER):].lstrip("：: \t\r\n")
            if rest:
                self.notes.insert("end", rest)
            return

        hold = len(prompts.WHY_MARKER) - 1
        self.delta_tail = buffer[-hold:]
        emitted = buffer[:len(buffer) - len(self.delta_tail)]
        if emitted:
            self.out.insert("end", emitted)
            self.out.see("end")

    def _flush_delta(self):
        """持ち越していた末尾を書き出す。"""
        if not self.delta_tail:
            return
        target = self.notes if self.to_notes else self.out
        target.insert("end", self.delta_tail)
        self.delta_tail = ""

    def center_over_self(self, window: tk.Toplevel):
        window.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - window.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - window.winfo_height()) // 3
        window.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def current_endpoint(self) -> deepseek.Endpoint:
        return deepseek.resolve_endpoint(self.cfg, self.cfg["endpoint"])

    def current_model(self) -> str:
        return self.cfg["model"]

    def _missing_key(self) -> bool:
        endpoint = self.current_endpoint()
        return endpoint.needs_key and not endpoint.api_key

    def _model_label(self, name: str | None = None, model: str | None = None) -> str:
        """ツールバーに出す名前。同じ ID が別の接続先にもあり得るので接続先を添える。"""
        name = name or self.cfg["endpoint"]
        model = model or self.cfg["model"]
        return f"{model}（{deepseek.PROVIDERS[name].label}）"

    def _model_choices(self) -> dict[str, tuple[str, deepseek.ModelInfo]]:
        """ツールバーに並べる名前と、その (接続先, モデル)。"""
        choices = {}
        for provider in deepseek.PROVIDERS.values():
            current = provider.name == self.cfg["endpoint"]
            if provider.needs_key and not current and not self.cfg.get(provider.key_field):
                continue
            models = ([deepseek.ModelInfo(model) for model in provider.models]
                      or list(self.discovered.get(provider.name, [])))
            # 一覧が取れなくても、保存してあるモデルは選べるように残す
            if current and all(model.id != self.cfg["model"] for model in models):
                models.append(deepseek.ModelInfo(self.cfg["model"]))
            for model in models:
                choices.setdefault(self._model_label(provider.name, model.id),
                                   (provider.name, model))
        return choices

    def effective_reasoning_effort(self) -> str | None:
        """選んだ effort を、今のモデルが受け付ける値に寄せる。"""
        _name, model = self._model_choices()[self._model_label()]
        return deepseek.pick_effort(self.current_reasoning_effort(), model.efforts)

    def current_reasoning_effort(self) -> str:
        effort = self.reasoning_effort_var.get()
        return (effort if effort in REASONING_EFFORTS
                else config.DEFAULTS["reasoning_effort"])

    def current_style(self) -> str:
        for style, label in STYLE_LABELS.items():
            if label == self.style_var.get():
                return style
        return prompts.DEFAULT_STYLE

    def _on_model_changed(self, _event=None):
        name, model = self._model_choices()[self.model_var.get()]
        self.cfg["endpoint"], self.cfg["model"] = name, model.id
        self.save_config()

    def _update_model_box(self):
        self.model_box.configure(values=list(self._model_choices()))
        self.model_var.set(self._model_label())

    def _refresh_model_lists(self):
        """Models API を持つ接続先のモデル一覧を、画面を止めずに取得する。"""
        for provider in deepseek.PROVIDERS.values():
            endpoint = deepseek.resolve_endpoint(self.cfg, provider.name)
            if provider.models or (endpoint.needs_key and not endpoint.api_key):
                continue
            if provider.url_field and not self.cfg.get(provider.url_field, "").strip():
                continue
            threading.Thread(target=self._fetch_model_list, args=(endpoint,),
                             daemon=True).start()

    def _fetch_model_list(self, endpoint: deepseek.Endpoint):
        try:
            models = deepseek.list_models(endpoint)
        except Exception:
            return                  # 繋がらなければ保存済みのモデルだけを並べる
        self.queue.put(("models", (endpoint.name, models)))

    def _apply_discovered_models(self, name: str, models: list[deepseek.ModelInfo]):
        self.discovered[name] = models
        self._update_model_box()

    # ------------------------------------------------- 照合で動いたタグの目印

    def _apply_marks(self, marks):
        for name in self.marks:
            self.out.tag_delete(name)
        self.marks = {}
        for number, mark in enumerate(marks):
            name = f"mark{number}"
            start, end = f"1.0 + {mark.start} chars", f"1.0 + {mark.end} chars"
            self.out.tag_add(mark.kind, start, end)
            self.out.tag_add(name, start, end)
            self.marks[name] = mark

    def _mark_at(self, index: str):
        for name in self.out.tag_names(index):
            if name in self.marks:
                return name, self.marks[name]
        return None, None

    def _on_out_motion(self, event):
        """カーソル下のタグについて、寄せる前の語を出力欄の見出しに出す。"""
        _name, mark = self._mark_at(f"@{event.x},{event.y}")
        if mark is None:
            self.update_counts()
        elif mark.kind == "swap":
            self.out_meta.configure(text=f"元: {mark.original}", fg=OK)
        else:
            self.out_meta.configure(text=f"{mark.original} は Danbooru に無い", fg=DANGER)

    def _on_out_menu(self, event):
        name, mark = self._mark_at(f"@{event.x},{event.y}")
        if mark is None or mark.kind != "swap":
            return
        menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT,
                       activeforeground=ACCENT_FG, borderwidth=0, font=self.ui_font)
        menu.add_command(label=f"元に戻す（{mark.original}）",
                         command=lambda: self._revert(name, mark))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _revert(self, name: str, mark):
        span = self.out.tag_ranges(name)
        if not span:
            return
        start = str(span[0])
        self.out.delete(start, str(span[1]))
        # 戻した語は Danbooru に無いので、そのしるしを付け直す
        self.out.insert(start, mark.original, ("kept",))
        self.out.tag_delete(name)
        self.marks.pop(name, None)
        self.update_counts()

    def save_config(self):
        self.cfg["reasoning_effort"] = self.current_reasoning_effort()
        self.cfg["style"] = self.current_style()
        # 位置は保存しない（画面構成が変わると窓が画面外に出ることがあるため）
        self.cfg["geometry"] = f"{self.winfo_width()}x{self.winfo_height()}"
        try:
            config.save(self.cfg)
        except OSError as exc:
            self.status.configure(text=f"設定を保存できません: {exc}", style="Error.TLabel")

    # -------------------------------------------------------------- 操作

    def _first_run_notice(self):
        if not self._missing_key():
            return
        self.status.configure(text="API キーが未設定です。［設定］から登録してください。",
                              style="Error.TLabel")
        label = deepseek.PROVIDERS[self.cfg["endpoint"]].label
        if messagebox.askyesno(
                APP_TITLE,
                f"{label} の API キーがまだ登録されていません。\n今すぐ設定しますか？",
                parent=self):
            self.open_settings()

    def open_settings(self):
        dialog = SettingsDialog(self)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        result = dict(dialog.result)
        local_model = result.pop("local_model")
        theme_changed = result["theme"] != self.cfg.get("theme", DEFAULT_THEME)
        font_changed = result["font_family"] != self.cfg.get("font_family", "")
        self.cfg.update(result)
        if local_model:
            self.cfg["endpoint"], self.cfg["model"] = "local", local_model
        if font_changed:
            self._configure_fonts()
        self.save_config()
        if theme_changed or font_changed:
            self.rebuild()
        self._update_model_box()
        self._refresh_model_lists()
        missing_key = self._missing_key()
        self.status.configure(
            text=("API キーが未設定です。［設定］から登録してください。"
                  if missing_key else "設定を保存しました。"),
            style="Error.TLabel" if missing_key else "Muted.TLabel")

    def open_history(self):
        self.wait_window(HistoryDialog(self))

    def clear(self):
        self.set_source("")
        self.out.delete("1.0", "end")
        self.hint.delete("1.0", "end")
        self.notes.delete("1.0", "end")
        self.marks = {}
        self.reasoning = ""
        self.ground_note = ""
        self.reason_btn.pack_forget()
        self.status.configure(text="準備完了", style="Muted.TLabel")
        self.update_counts()
        self.composition.focus_set()

    def copy_output(self):
        text = self.out.get("1.0", "end-1c").strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="プロンプトをクリップボードにコピーしました。",
                              style="Muted.TLabel")

    def show_reasoning(self):
        window = tk.Toplevel(self)
        window.title("推論過程")
        window.configure(bg=BG)
        window.geometry(f"{px(700)}x{px(420)}")
        window.transient(self)
        frame = ttk.Frame(window, padding=px(12))
        frame.pack(fill="both", expand=True)
        wrap, text = self._text_widget(frame)
        wrap.pack(fill="both", expand=True)
        text.insert("1.0", self.reasoning)
        text.configure(state="disabled", fg=MUTED)
        ttk.Button(frame, text="閉じる", command=window.destroy).pack(
            anchor="e", pady=(px(10), 0))
        window.bind("<Escape>", lambda _e: window.destroy())
        self.center_over_self(window)
        style_titlebar(window)

    # -------------------------------------------------------- 生成 / 作り直し

    def translate(self):
        """日本語の説明からプロンプトを書き起こす。［AI への指示］は使わない。"""
        if self._stop_if_running():
            return
        text = self.get_source_text()
        if not text:
            self.status.configure(text="描きたい絵の説明を日本語で入力してください。",
                                  style="Error.TLabel")
            self.composition.focus_set()
            return
        if self._missing_key():
            self._first_run_notice()
            return

        system_prompt = prompts.build_system_prompt(
            self.current_style(), self.cfg.get("extra", ""))
        self._start(system_prompt, text, text, "")

    def revise(self):
        """今のプロンプトに［AI への指示］を当てて作り直す。"""
        if self._stop_if_running():
            return
        current = self.out.get("1.0", "end-1c").strip()
        instruction = self.hint.get("1.0", "end-1c").strip()
        if not current:
            self.status.configure(text="先に生成してください。作り直すプロンプトがありません。",
                                  style="Error.TLabel")
            return
        if not instruction:
            self.status.configure(text="［AI への指示］に直したい内容を書いてください。",
                                  style="Error.TLabel")
            self.hint.focus_set()
            return
        if self._missing_key():
            self._first_run_notice()
            return

        source = self.get_source_text()
        self._start(prompts.build_revise_prompt(self.current_style(),
                                                self.cfg.get("extra", "")),
                    prompts.build_revise_request(source, current, instruction),
                    source, instruction)

    def _stop_if_running(self) -> bool:
        """実行中なら中断して True を返す。"""
        if self.worker and self.worker.is_alive():
            self.cancel_translation()
            return True
        return False

    def _start(self, system_prompt: str, user_text: str, source_text: str, note: str):
        """プロンプトをストリーミングで受け取り、必要ならタグ照合まで通す。"""
        self.save_config()
        self.set_output("")
        self.set_notes("")
        self.reasoning = ""
        self.ground_note = ""
        self.to_notes = False
        self.delta_tail = ""
        self.reason_btn.pack_forget()

        style, extra = self.current_style(), self.cfg.get("extra", "")
        model = self.current_model()
        endpoint = self.current_endpoint()
        mcp_url = self.cfg.get("mcp_url", "")
        reasoning_effort = self.effective_reasoning_effort()
        self.cancel = threading.Event()
        cancel = self.cancel

        def worker():
            draft = []
            try:
                for kind, payload in deepseek.stream_chat(
                        endpoint, model, system_prompt, user_text, cancel,
                        reasoning_effort):
                    if cancel.is_set():
                        break
                    if kind == "delta":
                        draft.append(payload)
                    self.queue.put((kind, payload))

                # 説明は照合の対象外。プロンプト本体だけを渡す。
                translated, _notes = prompts.split_notes("".join(draft))
                translated = prompts.clean_translation_output(translated)
                if not cancel.is_set():
                    self.queue.put(("clean", translated))
                if translated and not cancel.is_set() and style in prompts.GROUNDED_STYLES:
                    self.queue.put(("status", "Danbooru タグを照合中…"))
                    result = grounding.ground(endpoint, model, style, extra,
                                              translated, cancel, mcp_url,
                                              reasoning_effort)
                    if not cancel.is_set():
                        self.queue.put(("ground", result))
            except Exception as exc:                     # 想定外でも UI を戻す
                self.queue.put(("error", f"予期しないエラー: {exc}"))
            finally:
                self.queue.put(("done", (source_text, note)))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()
        self._set_busy(True, model)

    def cancel_translation(self):
        if self.cancel and not self.cancel.is_set():
            self.cancel.set()
            self.status.configure(text="中断しました。", style="Muted.TLabel")
            self._set_busy(False)

    def _set_busy(self, busy: bool, model: str = ""):
        self.translate_btn.configure(text="中断  (Esc)" if busy else "生成  (Ctrl+Enter)")
        for button in (self.copy_btn, self.revise_btn):
            button.configure(state="disabled" if busy else "normal")
        if busy:
            self.status.configure(text=f"{model} で生成中…", style="Muted.TLabel")

    def _drain(self):
        """ワーカースレッドからのメッセージを UI に反映する。"""
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "delta":
                    self._append_delta(payload)
                elif kind == "reasoning":
                    self.reasoning += payload
                    if not self.reason_btn.winfo_manager():   # 未配置なら並べる
                        self.reason_btn.pack(side="right", padx=(0, px(8)))
                elif kind in ("status", "notice"):
                    self.status.configure(text=payload, style="Muted.TLabel")
                elif kind == "ground":
                    self._apply_ground(payload)
                elif kind == "clean":
                    # set_output replaces the streamed text with the complete
                    # cleaned result.  The marker detector's held-back suffix
                    # is therefore already included and must not be flushed
                    # again when the request finishes.
                    self.delta_tail = ""
                    self.set_output(payload)
                elif kind == "models":
                    self._apply_discovered_models(*payload)
                elif kind == "error":
                    self.status.configure(text=payload.replace("\n", "  "), style="Error.TLabel")
                    messagebox.showerror(APP_TITLE, payload, parent=self)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        finally:
            # 何が起きても次回を必ず予約する。ここで途切れると
            # 以降いくら生成しても画面に何も出てこなくなる。
            self.after(40, self._drain)

    def _apply_ground(self, result):
        """照合パスの結果を出力欄に反映する。失敗しても 1 パス目の出力は残す。"""
        if result.error:
            self.ground_note = "タグ照合に失敗（プロンプトはそのまま）"
            return
        if not result.checked:
            self.ground_note = "タグ表なし（refresh_tags.py で取得）"
            return
        backend = {"mcp": "Danbooru MCP", "csv": "ローカル辞書"}.get(result.backend, "")
        suffix = f"（{backend}）" if backend else ""
        # Grounding replaces the first-pass output, so its held-back tail is stale.
        self.delta_tail = ""
        self.set_output(result.text)
        self._apply_marks(result.marks)
        if result.swapped or result.kept:
            self.ground_note = (f"タグに寄せた {result.swapped} 件"
                                f"・原語のまま {result.kept} 件{suffix}")
        else:
            self.ground_note = f"全タグが Danbooru にあります{suffix}"

    def _finish(self, payload: tuple):
        source_text, note = payload
        cancelled = bool(self.cancel and self.cancel.is_set())
        self._flush_delta()
        self._set_busy(False)
        self.cancel = None

        raw = self.out.get("1.0", "end-1c")
        output = raw.strip()
        if cancelled or not output:
            return
        if raw != output:
            # 前後の空白を、付けた目印を消さないように削る（末尾から先に）
            lead = len(raw) - len(raw.lstrip())
            self.out.delete(f"1.0 + {lead + len(output)} chars", "end")
            self.out.delete("1.0", f"1.0 + {lead} chars")
            self.update_counts()

        if self.status.cget("style") != "Error.TLabel":
            suffix = f"　/　{self.ground_note}" if self.ground_note else ""
            self.status.configure(text=f"完了（{len(output)} 文字）{suffix}",
                                  style="Muted.TLabel")
        history = self.cfg.setdefault("history", [])
        entry = {"src": source_text, "out": output}
        if note:
            entry["note"] = note        # 作り直しのときの指示
        answer = self.notes.get("1.0", "end-1c").strip()
        if answer:
            entry["why"] = answer       # その指示に対する AI の回答
        history.insert(0, entry)
        del history[config.HISTORY_MAX:]
        self.save_config()

    # -------------------------------------------------------------- 終了

    def _on_close(self):
        if self.cancel:
            self.cancel.set()
        self.save_config()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
