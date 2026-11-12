# -*- coding: utf-8 -*-
"""Danbooru 照合パスの回帰テスト。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import deepseek
import grounding
import mcpdict
import tagdict

ENDPOINT = deepseek.Endpoint("local", "http://llm.invalid/v1/chat/completions",
                             "http://llm.invalid/v1/models", "llm-key", False)

# Danbooru 側の表記（下線区切り）, 種別, post_count
DANBOORU = {
    "1girl": (0, 6000000),
    "frieren": (4, 30000),
    "fern_(sousou_no_frieren)": (4, 20000),
    "sousou_no_frieren": (3, 40000),
    "sparkle": (0, 90000),
}


def _csv_index() -> tagdict.TagIndex:
    index = tagdict.TagIndex()
    for name, (category, count) in DANBOORU.items():
        index.add(name, category, count, "")
    return index.finish()


def _mcp_client() -> mcpdict.Client:
    client = mcpdict.Client("http://mcp.invalid/mcp")
    client.asked: list[str] = []

    def call(name, arguments):
        if name != "get_tags":
            return {"results": []}
        client.asked += arguments["names"]
        entries = []
        for db_name in arguments["names"]:
            if db_name in DANBOORU:
                entries.append({"id": 1, "resolved_name": db_name,
                                "post_count": DANBOORU[db_name][1]})
            else:
                entries.append({"query": db_name, "resolved": False})
        return {"resolved": entries}

    client.call = call
    return client


def _ground(backend: str, output: str, reply: str = "", style: str = "tags"):
    """辞書を差し替えて照合を 1 回通す。返り値は (結果, LLM への問い合わせ本文)。"""
    patches = [mock.patch.object(grounding.mcpdict, "load",
                                 return_value=_mcp_client() if backend == "mcp" else None),
               mock.patch.object(grounding.tagdict, "load", return_value=_csv_index()),
               mock.patch.object(grounding.deepseek, "complete",
                                 return_value=(reply, None))]
    with patches[0], patches[1], patches[2] as complete:
        result = grounding.ground(ENDPOINT, "model", style, "", output,
                                  mcp_url="http://mcp.invalid/mcp")
    request = complete.call_args.args[3] if complete.called else ""
    return result, request


def _marked(result) -> list[tuple[str, str, str]]:
    return [(result.text[mark.start:mark.end], mark.kind, mark.original)
            for mark in result.marks]


class _RecordingIndex:
    """1girl と sparkle だけが実在する辞書。引かれた名前を覚える。"""

    def __init__(self):
        self.asked: list[str] = []

    def resolve(self, tag):
        self.asked.append(tag)
        if tag.strip() in ("1girl", "sparkle"):
            return tagdict.Verdict(tag, "ok", tag.strip(), 1000, [])
        return tagdict.Verdict(tag, "unknown", tag, 0, [])


class TestEscapedQualifier(unittest.TestCase):
    def test_existing_qualified_tag_is_written_back_escaped(self):
        for backend in ("csv", "mcp"):
            with self.subTest(backend=backend):
                result, request = _ground(backend, r"1girl, Fern \(Sousou no Frieren\)")

                self.assertEqual(result.text, r"1girl, fern \(sousou no frieren\)")
                self.assertEqual(result.marks, [])
                self.assertEqual(request, "")

    def test_wrong_qualifier_is_unknown_and_offers_the_bare_name(self):
        tag = r"frieren \(sousou no frieren\)"
        for backend in ("csv", "mcp"):
            with self.subTest(backend=backend):
                result, request = _ground(backend, f"1girl, {tag}",
                                          reply=f"{tag} => frieren")

                self.assertIn(f'"{tag}" (not a Danbooru tag)', request)
                self.assertIn("frieren (30000)", request)
                self.assertEqual(result.text, "1girl, frieren")
                self.assertEqual(_marked(result), [("frieren", "swap", tag)])

    def test_replacement_with_parentheses_is_escaped(self):
        for backend in ("csv", "mcp"):
            with self.subTest(backend=backend):
                result, _request = _ground(
                    backend, "1girl, fern from frieren",
                    reply="fern from frieren => fern (sousou no frieren)")

                self.assertEqual(result.text, r"1girl, fern \(sousou no frieren\)")
                self.assertEqual(_marked(result), [
                    (r"fern \(sousou no frieren\)", "swap", "fern from frieren")])

    def test_weight_lora_break_and_wildcards_stay_untouched(self):
        line = "1girl, (chibi:1.2), (smile), <lora:style:0.8>, BREAK, {standing|sitting}"
        for backend in ("csv", "mcp"):
            with self.subTest(backend=backend):
                result, request = _ground(backend, line)

                self.assertEqual(result.text, line)
                self.assertEqual(result.marks, [])
                self.assertEqual(request, "")


class TestControlTags(unittest.TestCase):
    def test_control_tags_pass_through_unlooked_and_unmarked(self):
        head = "masterpiece, best quality, score_7, safe, newest, year 2024, @kantoku"
        output = f"{head}, 1girl, shiny thing"
        index = _RecordingIndex()
        with mock.patch.object(grounding.tagdict, "load", return_value=index), \
                mock.patch.object(grounding.deepseek, "complete",
                                  return_value=("shiny thing => KEEP", None)) as complete:
            result = grounding.ground(ENDPOINT, "model", "tags", "", output)

        self.assertTrue(result.text.startswith(head + ", "))
        self.assertEqual(_marked(result), [("shiny thing", "kept", "shiny thing")])
        self.assertEqual(index.asked, ["1girl", "shiny thing"])
        queried = complete.call_args.args[3].split("Queried tags:")[1]
        self.assertNotIn("masterpiece", queried)
        self.assertNotIn("score_7", queried)
        self.assertNotIn("@kantoku", queried)


class TestHybrid(unittest.TestCase):
    def test_only_the_tag_line_is_grounded_and_marks_point_at_it(self):
        prose = "A girl holds a shiny thing. The odd hat sits on the shiny thing."
        output = f"masterpiece, safe, 1girl, shiny thing, odd hat\n\n{prose}"
        index = _RecordingIndex()
        with mock.patch.object(grounding.tagdict, "load", return_value=index), \
                mock.patch.object(grounding.deepseek, "complete",
                                  return_value=("shiny thing => sparkle\n"
                                                "odd hat => KEEP", None)):
            result = grounding.ground(ENDPOINT, "model", "hybrid", "", output)

        self.assertEqual(result.text,
                         f"masterpiece, safe, 1girl, sparkle, odd hat\n\n{prose}")
        self.assertEqual(_marked(result), [("sparkle", "swap", "shiny thing"),
                                           ("odd hat", "kept", "odd hat")])
        self.assertNotIn(prose, index.asked)


class TestBackend(unittest.TestCase):
    class _Index:
        def resolve(self, tag):
            return tagdict.Verdict(tag, "ok", tag, 100, [])

        def resolve_many(self, tags):
            return [self.resolve(tag) for tag in tags]

    class _BrokenMcp:
        def resolve_many(self, _tags):
            raise mcpdict.McpError("切断")

    def test_selects_mcp_when_available(self):
        with mock.patch.object(grounding.mcpdict, "load", return_value=self._Index()), \
                mock.patch.object(grounding.tagdict, "load") as csv_load:
            result = grounding.ground(ENDPOINT, "model", "tags", "", "1girl",
                                      mcp_url="http://mcp.invalid/mcp")

        self.assertEqual(result.backend, "mcp")
        csv_load.assert_not_called()

    def test_selects_csv_when_mcp_is_unreachable(self):
        with mock.patch.object(grounding.mcpdict, "load", return_value=None), \
                mock.patch.object(grounding.tagdict, "load", return_value=self._Index()):
            result = grounding.ground(ENDPOINT, "model", "tags", "", "1girl",
                                      mcp_url="http://mcp.invalid/mcp")

        self.assertEqual(result.backend, "csv")
        self.assertTrue(result.checked)

    def test_falls_back_to_csv_if_mcp_drops_after_check(self):
        with mock.patch.object(grounding.mcpdict, "load", return_value=self._BrokenMcp()), \
                mock.patch.object(grounding.tagdict, "load", return_value=self._Index()):
            result = grounding.ground(ENDPOINT, "model", "tags", "", "1girl",
                                      mcp_url="http://mcp.invalid/mcp")

        self.assertEqual(result.backend, "csv")
        self.assertTrue(result.checked)

    def test_grounding_query_goes_to_the_selected_endpoint(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter([b"data: [DONE]\n"])
        with mock.patch.object(grounding.tagdict, "load", return_value=_RecordingIndex()), \
                mock.patch.object(deepseek.urllib.request, "urlopen",
                                  return_value=response) as urlopen:
            grounding.ground(ENDPOINT, "glm", "tags", "", "unknown tag",
                             reasoning_effort="max")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT.chat_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer llm-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual((body["model"], body["reasoning_effort"]), ("glm", "max"))


class TestRebuild(unittest.TestCase):
    def test_deduplicates_tags_within_multi_tag_swap(self):
        tags = ["male hugging female from behind", "hug from behind"]
        verdicts = {
            "male hugging female from behind": tagdict.Verdict(
                "male hugging female from behind", "unknown",
                "male hugging female from behind", 0, []),
            "hug from behind": tagdict.Verdict(
                "hug from behind", "ok", "hug from behind", 0, []),
        }
        swaps = {"male hugging female from behind": "hug from behind, male, female"}

        line, _marks = grounding._rebuild("Tags: ", tags, verdicts, swaps, 0)

        self.assertEqual(line, "Tags: hug from behind, male, female")

    def test_deduplicates_different_source_tags_with_same_swap(self):
        tags = ["male hugging female from behind", "male_focus", "female_focus",
                "1boy", "1girl", "embrace from behind", "unmapped pose"]
        verdicts = {
            tag: tagdict.Verdict(tag, "unknown" if " " in tag else "ok", tag, 0, [])
            for tag in tags
        }
        swaps = {
            "male hugging female from behind": "hug_from_behind",
            "embrace from behind": "hug_from_behind",
        }

        line, marks = grounding._rebuild("Tags: ", tags, verdicts, swaps, 7)

        self.assertEqual(
            line,
            "Tags: hug_from_behind, male_focus, female_focus, 1boy, 1girl, "
            "unmapped pose")
        swap_start = 7 + line.index("hug_from_behind")
        kept_start = 7 + line.index("unmapped pose")
        self.assertEqual(marks, [
            grounding.Mark(swap_start, swap_start + len("hug_from_behind"), "swap",
                           "male hugging female from behind"),
            grounding.Mark(kept_start, kept_start + len("unmapped pose"), "kept",
                           "unmapped pose"),
        ])


if __name__ == "__main__":
    unittest.main()
