# -*- coding: utf-8 -*-
"""Danbooru MCP タグ辞書の回帰テスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import deepseek
import grounding
import mcpdict


class TestLiveMcp(unittest.TestCase):
    """ローカル MCP が動いているときだけ実データで判定を確認する。"""

    @classmethod
    def setUpClass(cls):
        cls.client = mcpdict.Client("http://127.0.0.1:8766/mcp")
        ok, message = cls.client.check()
        if not ok:
            raise unittest.SkipTest(message)

    def test_resolve_many_verdicts(self):
        verdicts = self.client.resolve_many([
            " 1girl ", "blue archive", "obscure", "zzz_nonexistent_tag", "BREAK",
        ])

        self.assertEqual([v.status for v in verdicts],
                         ["ok", "ok", "rare", "unknown", "skip"])
        self.assertEqual(verdicts[1].canonical, "blue archive")
        self.assertLess(verdicts[2].count, mcpdict.RARE_BELOW)
        self.assertEqual(verdicts[3].canonical, "zzz nonexistent tag")
        self.assertEqual(verdicts[4].canonical, "BREAK")


class TestMcpClient(unittest.TestCase):
    def test_multiword_canonical_uses_spaces_in_verdict_and_grounding(self):
        client = mcpdict.Client("http://example.invalid/mcp")
        resolved = {
            "resolved": [{
                "id": 1,
                "resolved_name": "hatsune_miku",
                "post_count": mcpdict.RARE_BELOW,
            }],
        }

        with mock.patch.object(client, "call", return_value=resolved):
            verdict = client.resolve("hatsune_miku")

        self.assertNotIn("_", verdict.canonical)
        self.assertEqual(verdict.canonical, "hatsune miku")

        with mock.patch.object(mcpdict, "load", return_value=client), \
                mock.patch.object(client, "call", return_value=resolved):
            result = grounding.ground(
                deepseek.resolve_endpoint({}, "local"), "model", "tags", "",
                "hatsune_miku", mcp_url=client.url)

        self.assertNotIn("_", result.text)
        self.assertEqual(result.text, "hatsune miku")

    def test_session_is_terminated_with_delete(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch.object(mcpdict.urllib.request, "urlopen",
                               return_value=response) as urlopen:
            mcpdict._end_session("http://example.invalid/mcp", "session-id")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.get_header("Mcp-session-id"), "session-id")

    def test_resolve_many_splits_server_batches(self):
        client = mcpdict.Client("http://example.invalid/mcp")

        def call(name, arguments):
            self.assertEqual(name, "get_tags")
            return {"resolved": [
                {"id": number + 1, "resolved_name": tag, "post_count": 100}
                for number, tag in enumerate(arguments["names"])
            ]}

        tags = [f"tag {number}" for number in range(51)]
        with mock.patch.object(client, "call", side_effect=call) as mocked:
            verdicts = client.resolve_many(tags)

        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(all(verdict.status == "ok" for verdict in verdicts))

    def test_load_cache_is_scoped_to_url(self):
        old_client, old_url = mcpdict._client, mcpdict._client_url
        self.addCleanup(setattr, mcpdict, "_client", old_client)
        self.addCleanup(setattr, mcpdict, "_client_url", old_url)
        mcpdict._client = None
        mcpdict._client_url = ""

        with mock.patch.object(mcpdict.Client, "check", return_value=(True, "ok")):
            first = mcpdict.load("http://first.invalid/mcp")
            second = mcpdict.load("http://second.invalid/mcp")

        self.assertEqual(first.url, "http://first.invalid/mcp")
        self.assertEqual(second.url, "http://second.invalid/mcp")
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
