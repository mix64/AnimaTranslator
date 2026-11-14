# -*- coding: utf-8 -*-
"""PNG に埋め込まれた生成情報の読み取り（標準ライブラリのみ）。

画像生成ツールは、使ったプロンプトや設定を PNG のテキストチャンクに書き込む。

- ComfyUI: "prompt"（実行したノードの JSON）と "workflow"（画面上のノードの JSON）
- AUTOMATIC1111 / Forge など: "parameters"（プロンプトと設定を並べた文）

ここではそれを読み、LLM に渡せる短い文にまとめる。
"""
from __future__ import annotations

import json
import struct
import zlib
from typing import NamedTuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# LLM に渡す文の上限。workflow の JSON は数十 KB になるので、まとめずに渡すと溢れる。
MAX_TEXT = 6000
MAX_VALUE = 1500

# ComfyUI のノードの入力のうち、プロンプトの文として読むもの
_TEXT_INPUTS = ("text", "text_g", "text_l", "prompt", "positive", "negative",
                "string", "wildcard_text", "populated_text")
# 生成の設定として読むもの
_SETTING_INPUTS = (
    "ckpt_name", "unet_name", "model_name", "vae_name", "clip_name", "clip_name1",
    "clip_name2", "lora_name", "strength_model", "strength_clip",
    "seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler", "denoise",
    "width", "height", "batch_size", "shift",
)


class PngInfo(NamedTuple):
    width: int
    height: int
    texts: dict[str, str]       # チャンクのキーワード → 本文


def read_png_info(data: bytes) -> PngInfo:
    """PNG のバイト列から、寸法とテキストチャンクを読む。PNG でなければ ValueError。"""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("PNG ではありません。")
    width = height = 0
    texts: dict[str, str] = {}
    position = len(PNG_SIGNATURE)
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        body = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR" and len(body) >= 8:
            width, height = struct.unpack(">II", body[:8])
        elif kind in (b"tEXt", b"zTXt", b"iTXt"):
            try:
                keyword, text = _decode_text_chunk(kind, body)
            except (ValueError, zlib.error):
                continue
            texts[keyword] = (texts[keyword] + "\n" + text) if keyword in texts else text
        elif kind == b"IEND":
            break
    return PngInfo(width, height, texts)


def _decode_text_chunk(kind: bytes, body: bytes) -> tuple[str, str]:
    keyword, separator, rest = body.partition(b"\0")
    if not separator:
        raise ValueError("キーワードの区切りがありません。")
    name = keyword.decode("latin-1")
    if kind == b"tEXt":
        # 仕様は Latin-1 だが、UTF-8 で書くツールが多い
        return name, _decode_loose(rest)
    if kind == b"zTXt":
        return name, _decode_loose(zlib.decompress(rest[1:]))
    # iTXt: 圧縮フラグ, 圧縮方式, 言語タグ\0, 翻訳したキーワード\0, 本文
    if len(rest) < 2:
        raise ValueError("iTXt が短すぎます。")
    compressed = rest[0] == 1
    _language, _, rest = rest[2:].partition(b"\0")
    _translated, _, text = rest.partition(b"\0")
    if compressed:
        text = zlib.decompress(text)
    return name, text.decode("utf-8", "replace")


def _decode_loose(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# ------------------------------------------------------------ 読める形にまとめる

def _clip(text: str, limit: int = MAX_VALUE) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …（以下略）"


def _scalar(value) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def summarize_comfy_prompt(graph: dict) -> list[str]:
    """ComfyUI の "prompt"（API 形式）から、プロンプトの文と設定を抜き出す。

    サンプラーの positive / negative の配線をたどり、どの文がどちら側かを付ける。
    """
    side: dict[str, str] = {}
    for node in graph.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for name in ("positive", "negative"):
            link = inputs.get(name)
            if isinstance(link, list) and link:
                side[str(link[0])] = name

    lines = []
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        kind = node.get("class_type", "?")
        meta = node.get("_meta")
        title = meta.get("title") if isinstance(meta, dict) else None
        label = f"#{node_id} {kind}" + (f"「{title}」" if title and title != kind else "")
        if node_id in side:
            label += f"（{side[node_id]}）"
        texts = [(name, value) for name, value in node["inputs"].items()
                 if name in _TEXT_INPUTS and isinstance(value, str) and value.strip()]
        settings = [(name, value) for name, value in node["inputs"].items()
                    if name in _SETTING_INPUTS and _scalar(value)]
        for name, value in texts:
            lines.append(f"{label} {name}: {_clip(value)}")
        if settings:
            lines.append(f"{label} " + ", ".join(f"{name}={value}" for name, value in settings))
    return lines


def summarize_comfy_workflow(workflow: dict) -> list[str]:
    """ComfyUI の "workflow"（画面の形式）から、文の入ったノードだけを抜き出す。

    "prompt" が無い画像のための代わり。ウィジェットの値に名前が付いていないので、
    文らしい値（空白を含む文字列）だけを拾う。
    """
    lines = []
    for node in workflow.get("nodes") or []:
        if not isinstance(node, dict) or node.get("mode") in (2, 4):   # 無効・素通し
            continue
        values = node.get("widgets_values")
        if not isinstance(values, list):
            continue
        texts = [value for value in values
                 if isinstance(value, str) and " " in value.strip()]
        label = f"#{node.get('id', '?')} {node.get('type', '?')}"
        if node.get("title"):
            label += f"「{node['title']}」"
        for value in texts:
            lines.append(f"{label}: {_clip(value)}")
    return lines


def describe(info: PngInfo) -> str:
    """テキストチャンクを LLM に渡す文にする。メタデータが無ければ空文字。"""
    texts = dict(info.texts)
    sections = []

    if "parameters" in texts:
        sections.append("[parameters]\n" + _clip(texts.pop("parameters"), MAX_TEXT // 2))

    graph = _load_json(texts.get("prompt"))
    workflow = _load_json(texts.get("workflow"))
    if isinstance(graph, dict):
        texts.pop("prompt")
        texts.pop("workflow", None)
        lines = summarize_comfy_prompt(graph)
        if lines:
            sections.append("[ComfyUI prompt]\n" + "\n".join(lines))
    elif isinstance(workflow, dict):
        texts.pop("workflow")
        lines = summarize_comfy_workflow(workflow)
        if lines:
            sections.append("[ComfyUI workflow]\n" + "\n".join(lines))

    for keyword, text in texts.items():
        if text.strip():
            sections.append(f"[{keyword}]\n{_clip(text)}")

    joined = "\n\n".join(sections)
    return _clip(joined, MAX_TEXT) if joined else ""


def _load_json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
