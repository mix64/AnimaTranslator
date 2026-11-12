# -*- coding: utf-8 -*-
"""システムプロンプトの組み立てと、GUI の入力欄まわりの回帰テスト。"""
from __future__ import annotations

import queue
import runpy
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import prompts


def _example_outputs(prompt: str) -> list[str]:
    """システムプロンプトの手本から、出力の部分だけを取り出す。"""
    section = prompt.split("# Examples", 1)[1]
    return [chunk.split("\n\n---\n\n")[0] for chunk in section.split("Output:\n")[1:]]


class TestSystemPrompt(unittest.TestCase):
    def test_every_style_includes_the_anima_guide(self):
        guide = prompts.anima_guide()
        self.assertTrue(guide)
        for style in prompts.STYLES:
            with self.subTest(style=style):
                self.assertIn(guide, prompts.build_system_prompt(style))
                self.assertIn(guide, prompts.build_revise_prompt(style))

    def test_no_example_output_carries_a_quality_or_rating_tag(self):
        for style in prompts.STYLES:
            with self.subTest(style=style):
                outputs = _example_outputs(prompts.build_system_prompt(style))

                self.assertTrue(outputs)
                for output in outputs:
                    tags = output.split("\n")[0].split(", ")
                    self.assertFalse([tag for tag in tags if prompts.is_control_tag(tag)])

    def test_examples_use_the_layout_of_the_selected_style(self):
        for style in prompts.STYLES:
            prompt = prompts.build_system_prompt(style)
            for output in _example_outputs(prompt):
                with self.subTest(style=style, output=output[:40]):
                    lines = output.split("\n")
                    if style == "tags":
                        self.assertEqual(len(lines), 1)
                        self.assertNotIn(". ", output)
                    elif style == "natural":
                        self.assertEqual(len(lines), 1)
                        self.assertTrue(output.endswith("."))
                    else:
                        self.assertEqual(len(lines), 3)
                        self.assertRegex(lines[0], r"^\d(girl|boy)")
                        self.assertEqual(lines[1], "")
                        self.assertTrue(lines[2].endswith("."))

    def test_missing_anima_guide_still_builds_the_selected_style(self):
        with mock.patch.object(prompts, "ANIMA_GUIDE", APP_DIR / "missing-guide.txt"):
            for style in prompts.STYLES:
                with self.subTest(style=style):
                    prompt = prompts.build_system_prompt(style)
                    self.assertIn(prompts.STYLES[style].strip(), prompt)
                    self.assertTrue(_example_outputs(prompt))

    def test_clean_translation_output_removes_structural_labels(self):
        natural = prompts.clean_translation_output(
            "Composition/background: A moonlit church.\n"
            "Male character 1: A man in a blue coat.\n"
            "Female character 1: A woman in a white dress.")
        tags = prompts.clean_translation_output(
            "night church, male character 1: black hair, "
            "female character 1: white dress")
        japanese = prompts.clean_translation_output(
            "【構図・背景】\n月夜の教会\n【女性キャラクター 1】\n白いドレス")

        self.assertEqual(
            natural,
            "A moonlit church.\nA man in a blue coat.\nA woman in a white dress.")
        self.assertEqual(tags, "night church, black hair, white dress")
        self.assertEqual(japanese, "月夜の教会\n白いドレス")


class TestGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = runpy.run_path(str(APP_DIR / "AnimaTranslator.pyw"))

    def test_separate_inputs_round_trip_through_source_text(self):
        source = self.gui["build_source_text"](
            "夜の街、ローアングル",
            ["黒髪、青いコート", "金髪、眼鏡"],
            ["赤い長髪、白いドレス"])

        self.assertEqual(
            self.gui["split_source_text"](source),
            ("夜の街、ローアングル",
             ["黒髪、青いコート", "金髪、眼鏡"],
             ["赤い長髪、白いドレス"]))
        self.assertIn("【男性キャラクター 2】", source)
        self.assertIn("【女性キャラクター 1】", source)

    def test_every_style_has_a_label(self):
        self.assertEqual(list(self.gui["STYLE_LABELS"]), list(prompts.STYLES))

    def test_clean_result_discards_streamed_suffix_before_finish(self):
        app_class = self.gui["App"]
        app = app_class.__new__(app_class)
        app.queue = queue.Queue()
        app.queue.put(("clean", "Her upper body is visible only as a shadow "
                                  "through the curtain."))
        app.delta_tail = "in."
        app.set_output = mock.Mock()
        app.after = mock.Mock()

        app_class._drain(app)

        self.assertEqual(app.delta_tail, "")
        app.set_output.assert_called_once_with(
            "Her upper body is visible only as a shadow through the curtain.")


if __name__ == "__main__":
    unittest.main()
