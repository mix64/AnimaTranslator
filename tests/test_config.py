# -*- coding: utf-8 -*-
"""設定の保存と読み込みの回帰テスト。実際の設定フォルダには触れない。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.file = folder / "config.json"
        for name, value in (("CONFIG_DIR", folder), ("CONFIG_FILE", self.file)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_local_api_key_is_not_written_in_plaintext(self):
        if config.encrypt("probe") is None:
            self.skipTest("DPAPI が使えない環境")
        cfg = config.load()
        cfg["local_api_key"] = "local-secret-1234"
        cfg["api_key"] = "deepseek-secret-5678"

        config.save(cfg)

        saved = self.file.read_text(encoding="utf-8")
        self.assertNotIn("local-secret-1234", saved)
        self.assertNotIn("deepseek-secret-5678", saved)
        loaded = config.load()
        self.assertEqual(loaded["local_api_key"], "local-secret-1234")
        self.assertEqual(loaded["api_key"], "deepseek-secret-5678")

    def test_unknown_saved_endpoint_and_style_fall_back_to_defaults(self):
        self.file.write_text(json.dumps({
            "endpoint": "retired-endpoint", "model": "old-local-model",
            "style": "retired-style"}), encoding="utf-8")

        cfg = config.load()

        self.assertEqual(cfg["endpoint"], "deepseek")
        self.assertEqual(cfg["model"], "deepseek-flash")
        self.assertEqual(cfg["style"], "hybrid")

    def test_saved_local_model_is_kept(self):
        self.file.write_text(json.dumps({
            "endpoint": "local", "model": "glm-5.3"}), encoding="utf-8")

        cfg = config.load()

        self.assertEqual((cfg["endpoint"], cfg["model"]), ("local", "glm-5.3"))


class TestEnvFile(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / ".env"

    def test_values_are_read_and_comments_and_quotes_are_dropped(self):
        self.file.write_text(
            "# 接続先\n"
            "ANIMA_LOCAL_URL=http://llm.invalid:8000/v1\n"
            "\n"
            'ANIMA_MCP_URL = "http://mcp.invalid/mcp"\n', encoding="utf-8")

        values = config.read_env_file(self.file)

        self.assertEqual(values, {"ANIMA_LOCAL_URL": "http://llm.invalid:8000/v1",
                                  "ANIMA_MCP_URL": "http://mcp.invalid/mcp"})

    def test_missing_file_gives_no_values(self):
        self.assertEqual(config.read_env_file(self.file), {})

    def test_environment_variable_wins_over_the_file(self):
        file_values = {"ANIMA_LOCAL_URL": "http://from-file.invalid/v1"}

        with mock.patch.dict("os.environ", {"ANIMA_LOCAL_URL": "http://from-env.invalid/v1"}):
            self.assertEqual(config.env_default("ANIMA_LOCAL_URL", file_values),
                             "http://from-env.invalid/v1")
        with mock.patch.dict("os.environ", clear=True):
            self.assertEqual(config.env_default("ANIMA_LOCAL_URL", file_values),
                             "http://from-file.invalid/v1")
            self.assertEqual(config.env_default("ANIMA_MCP_URL", file_values), "")


if __name__ == "__main__":
    unittest.main()
