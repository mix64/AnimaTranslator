# -*- coding: utf-8 -*-
"""PNG のメタデータ読み取りと、画像を添えた作り直しの問い合わせのテスト。"""
from __future__ import annotations

import json
import struct
import sys
import unittest
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import deepseek
import pngmeta
import prompts


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def make_png(width: int = 2, height: int = 3, chunks: tuple[bytes, ...] = ()) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\0" + b"\0\0\0" * width for _ in range(height))
    return (pngmeta.PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + b"".join(chunks)
            + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b""))


def text_chunk(keyword: str, text: str) -> bytes:
    return _chunk(b"tEXt", keyword.encode("latin-1") + b"\0" + text.encode("utf-8"))


def itxt_chunk(keyword: str, text: str, compressed: bool) -> bytes:
    body = text.encode("utf-8")
    if compressed:
        body = zlib.compress(body)
    return _chunk(b"iTXt", keyword.encode("latin-1") + b"\0"
                  + bytes((1 if compressed else 0, 0)) + b"\0\0" + body)


COMFY_PROMPT = {
    "3": {"class_type": "KSampler",
          "inputs": {"seed": 42, "steps": 28, "cfg": 4.5, "sampler_name": "euler",
                     "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "anima.safetensors"}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "1girl, silver hair, ruined church", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "worst quality", "clip": ["4", 1]}},
}


class TestReadPngInfo(unittest.TestCase):
    def test_reads_size_and_every_text_chunk_kind(self):
        data = make_png(640, 960, (
            text_chunk("parameters", "1girl, 白い髪\nSteps: 20"),
            _chunk(b"zTXt", b"Comment\0\0" + zlib.compress(b"hello")),
            itxt_chunk("Title", "タイトル", compressed=True),
            itxt_chunk("Author", "me", compressed=False),
        ))
        info = pngmeta.read_png_info(data)
        self.assertEqual((info.width, info.height), (640, 960))
        self.assertEqual(info.texts["parameters"], "1girl, 白い髪\nSteps: 20")
        self.assertEqual(info.texts["Comment"], "hello")
        self.assertEqual(info.texts["Title"], "タイトル")
        self.assertEqual(info.texts["Author"], "me")

    def test_rejects_non_png(self):
        with self.assertRaises(ValueError):
            pngmeta.read_png_info(b"GIF89a....")

    def test_skips_broken_text_chunk(self):
        data = make_png(chunks=(_chunk(b"zTXt", b"Bad\0\0notzlib"),
                                text_chunk("ok", "yes")))
        self.assertEqual(pngmeta.read_png_info(data).texts, {"ok": "yes"})


class TestDescribe(unittest.TestCase):
    def test_comfy_prompt_marks_positive_and_negative_and_settings(self):
        info = pngmeta.read_png_info(make_png(chunks=(
            text_chunk("prompt", json.dumps(COMFY_PROMPT)),
            text_chunk("workflow", json.dumps({"nodes": []})),
        )))
        text = pngmeta.describe(info)
        self.assertIn("[ComfyUI prompt]", text)
        self.assertIn("#6 CLIPTextEncode（positive） text: 1girl, silver hair, ruined church", text)
        self.assertIn("#7 CLIPTextEncode（negative） text: worst quality", text)
        self.assertIn("seed=42", text)
        self.assertIn("ckpt_name=anima.safetensors", text)
        self.assertNotIn("[workflow]", text)

    def test_comfy_workflow_used_when_prompt_is_missing(self):
        workflow = {"nodes": [
            {"id": 6, "type": "CLIPTextEncode", "widgets_values": ["1girl, red eyes"]},
            {"id": 8, "type": "CLIPTextEncode", "mode": 4,
             "widgets_values": ["bypassed text here"]},
            {"id": 9, "type": "KSampler", "widgets_values": [42, "fixed", 20]},
        ]}
        info = pngmeta.read_png_info(make_png(chunks=(
            text_chunk("workflow", json.dumps(workflow)),)))
        text = pngmeta.describe(info)
        self.assertIn("#6 CLIPTextEncode: 1girl, red eyes", text)
        self.assertNotIn("bypassed", text)

    def test_a1111_parameters_kept_as_written(self):
        info = pngmeta.read_png_info(make_png(chunks=(
            text_chunk("parameters", "1girl\nNegative prompt: bad hands\nSteps: 20"),)))
        self.assertEqual(pngmeta.describe(info),
                         "[parameters]\n1girl\nNegative prompt: bad hands\nSteps: 20")

    def test_no_metadata_is_empty(self):
        self.assertEqual(pngmeta.describe(pngmeta.read_png_info(make_png())), "")

    def test_long_text_is_clipped(self):
        info = pngmeta.read_png_info(make_png(chunks=(
            text_chunk("parameters", "x" * 20000),)))
        self.assertLess(len(pngmeta.describe(info)), pngmeta.MAX_TEXT + 50)


class TestReviseWithImages(unittest.TestCase):
    def test_request_numbers_images_before_the_instruction(self):
        request = prompts.build_revise_request(
            "銀髪", "1girl, silver hair", "画像1の手を直して", [
                prompts.ImageNote("a.png", 1024, 1536, "[parameters]\n1girl", True),
                prompts.ImageNote("b.png", 512, 512, "", False),
            ])
        self.assertIn("Image 1 (a.png, 1024x1536; attached below):\n"
                      "Embedded PNG metadata:\n[parameters]\n1girl", request)
        self.assertIn("Image 2 (b.png, 512x512; picture not sent, metadata only): "
                      "no embedded metadata", request)
        self.assertLess(request.index("Image 2"), request.index("The user's instruction"))

    def test_request_without_images_is_unchanged(self):
        self.assertNotIn("Image", prompts.build_revise_request("", "1girl", "直して"))

    def test_revise_prompt_explains_attached_images(self):
        self.assertIn("# Attached images", prompts.build_revise_prompt())

    def test_user_content_adds_image_parts(self):
        self.assertEqual(deepseek.user_content("hi"), "hi")
        content = deepseek.user_content("hi", ["data:image/png;base64,AAA"])
        self.assertEqual(content, [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ])


if __name__ == "__main__":
    unittest.main()
