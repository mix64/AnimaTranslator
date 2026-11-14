# -*- coding: utf-8 -*-
"""設定の永続化。

保存先は %APPDATA%\\AnimaPromptTranslator\\config.json。
API キーは Windows DPAPI（ユーザ単位）で暗号化して保存するので、
別ユーザや持ち出されたファイルからは復号できない。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

import deepseek
import prompts

APP_NAME = "AnimaPromptTranslator"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

# 環境ごとに違う接続先は、コードに書かず .env から既定値を取る。
# .exe にしたときは .exe の隣、スクリプトのときはこのファイルの隣に置く。
ENV_FILE = Path(sys.executable if getattr(sys, "frozen", False)
                else __file__).resolve().parent / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE の行を読む。# で始まる行は飛ばす。ファイルが無ければ空。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip("\"'")
    return values


def env_default(name: str, file_values: dict[str, str]) -> str:
    """環境変数を優先し、無ければ .env の値を返す。どちらも無ければ空。"""
    return (os.environ.get(name) or file_values.get(name, "")).strip()


_ENV = read_env_file(ENV_FILE)

DEFAULTS = {
    "api_key": "",          # DeepSeek Platform のキー
    "opencode_api_key": "",
    "endpoint": "deepseek",           # deepseek.PROVIDERS のキー
    "model": deepseek.DEEPSEEK_MODEL,
    "local_url": env_default("ANIMA_LOCAL_URL", _ENV),   # OpenAI 互換の /v1 まで
    "local_api_key": "",
    "reasoning_effort": "high",
    "style": prompts.DEFAULT_STYLE,
    "theme": "dark",
    "font_family": "",
    "extra": "",
    "send_images": True,    # 添付画像そのものも送るか。外すとメタデータだけ送る
    "mcp_url": env_default("ANIMA_MCP_URL", _ENV),       # 空なら CSV 辞書のみ
    "geometry": "",
    "history": [],
}
# 平文では書き出さず、<名前>_enc に暗号化して入れる。
SECRETS = ("api_key", "opencode_api_key", "local_api_key")
HISTORY_MAX = 30


# ------------------------------------------------------------------ DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _from_blob(blob: _Blob) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return out


def _crypt(func, data: bytes) -> bytes | None:
    """CryptProtectData / CryptUnprotectData の共通処理。失敗したら None。"""
    src, _keepalive = _to_blob(data)
    dst = _Blob()
    ok = func(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst))
    return _from_blob(dst) if ok else None


def encrypt(text: str) -> str | None:
    """文字列を DPAPI で暗号化し base64 で返す。使えない環境では None。"""
    if not text:
        return ""
    try:
        blob = _crypt(ctypes.windll.crypt32.CryptProtectData, text.encode("utf-8"))
        return base64.b64encode(blob).decode("ascii") if blob else None
    except (AttributeError, OSError):
        return None


def decrypt(token: str) -> str:
    """encrypt() の出力を復号する。復号できなければ空文字。"""
    if not token:
        return ""
    try:
        blob = _crypt(ctypes.windll.crypt32.CryptUnprotectData,
                      base64.b64decode(token.encode("ascii")))
        return blob.decode("utf-8") if blob else ""
    except (AttributeError, OSError, ValueError):
        return ""


# ----------------------------------------------------------------- load/save

def load() -> dict:
    cfg = dict(DEFAULTS)
    cfg["history"] = []
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}

    for key in DEFAULTS:
        if key in raw and isinstance(raw[key], type(DEFAULTS[key])):
            cfg[key] = raw[key]

    # 暗号化済みを優先する。DPAPI が使えない環境で書いた平文も読める。
    for name in SECRETS:
        if raw.get(f"{name}_enc"):
            cfg[name] = decrypt(raw[f"{name}_enc"]) or cfg[name]
    for provider in deepseek.PROVIDERS.values():
        if provider.key_env and not cfg[provider.key_field]:
            cfg[provider.key_field] = env_default(provider.key_env, _ENV)

    provider = deepseek.PROVIDERS.get(cfg["endpoint"])
    if provider is None or (not provider.models and not cfg["model"].strip()):
        provider = deepseek.PROVIDERS[DEFAULTS["endpoint"]]
        cfg["endpoint"] = provider.name
    # モデルが決まっている接続先（DeepSeek Platform）では、保存値がそれ以外なら直す。
    if provider.models and cfg["model"] not in provider.models:
        cfg["model"] = provider.models[0]
    if cfg["style"] not in prompts.STYLES:
        cfg["style"] = prompts.DEFAULT_STYLE
    return cfg


def save(cfg: dict) -> None:
    out = {k: cfg.get(k, v) for k, v in DEFAULTS.items() if k not in SECRETS}
    out["history"] = (cfg.get("history") or [])[:HISTORY_MAX]

    for name in SECRETS:
        secret = (cfg.get(name) or "").strip()
        token = encrypt(secret)
        if token is None:             # DPAPI が使えない環境では平文で保存する
            out[name] = secret
        else:
            out[f"{name}_enc"] = token

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)          # 書き込み途中で壊れないよう原子的に差し替え
