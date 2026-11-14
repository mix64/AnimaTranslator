# -*- coding: utf-8 -*-
"""接続先の解決と Chat Completions クライアントの回帰テスト。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import deepseek


def _json_response(payload):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload).encode("utf-8")
    return response


CHAT = {"chat-completions": {"base_url": "https://opencode.ai/inference/go/openai/v1"}}
# https://opencode.ai/inference/go/v1/models の応答から、形の違う項目を抜き出したもの。
GO_MODELS = {"object": "list", "data": [
    {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash 1017", "api": CHAT,
     "capabilities": {"tools": {"supported": True},
                      "reasoning": {"supported": True, "effort": ["low", "high", "max"]}}},
    {"id": "grok-4.7", "name": "Grok 4.7",
     "api": {"responses": {"base_url": "https://opencode.ai/inference/go/openai/v1"}},
     "capabilities": {"reasoning": {"supported": True,
                                    "effort": ["low", "medium", "high", "xhigh"]}}},
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5",
     "api": {"messages": {"base_url": "https://opencode.ai/inference/go/anthropic/v1"}},
     "capabilities": {"reasoning": {"supported": True, "toggle": True,
                                    "effort": ["low", "medium", "high", "xhigh", "max"]}}},
    {"id": "mimo-v2.6-flash", "name": "MiMo V2.6 Flash", "api": CHAT,
     "capabilities": {"reasoning": {"supported": True}}},
    {"id": "kimi-k3", "name": "Kimi K3", "api": CHAT,
     "capabilities": {"reasoning": {"supported": True, "effort": ["max"]}}},
    {"id": "kimi-k3", "name": "Kimi K3 (dup)", "api": CHAT},
]}


def _stream_response():
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.__iter__.return_value = iter([
        b'data: {"choices":[{"delta":{"content":"1girl"}}]}\n',
        b"data: [DONE]\n",
    ])
    return response


class TestResolveEndpoint(unittest.TestCase):
    def test_deepseek_uses_platform_api_and_requires_its_key(self):
        endpoint = deepseek.resolve_endpoint(
            {"api_key": " ds-key ", "local_url": "http://other.invalid/v1",
             "local_api_key": "local-key"}, "deepseek")

        self.assertEqual(endpoint.chat_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(endpoint.models_url, "https://api.deepseek.com/models")
        self.assertEqual(endpoint.api_key, "ds-key")
        self.assertTrue(endpoint.needs_key)

    def test_local_is_derived_from_base_url_and_key_is_optional(self):
        endpoint = deepseek.resolve_endpoint(
            {"api_key": "ds-key", "local_url": "http://llm.invalid:8000/v1/",
             "local_api_key": "local-key"}, "local")

        self.assertEqual(endpoint.chat_url, "http://llm.invalid:8000/v1/chat/completions")
        self.assertEqual(endpoint.models_url, "http://llm.invalid:8000/v1/models")
        self.assertEqual(endpoint.api_key, "local-key")
        self.assertFalse(endpoint.needs_key)


    def test_opencode_go_uses_its_own_urls_key_and_headers(self):
        endpoint = deepseek.resolve_endpoint(
            {"api_key": "ds-key", "opencode_api_key": " go-key "}, "opencode_go")

        self.assertEqual(endpoint.chat_url,
                         "https://opencode.ai/inference/go/openai/v1/chat/completions")
        self.assertEqual(endpoint.models_url, "https://opencode.ai/inference/go/v1/models")
        self.assertEqual(endpoint.api_key, "go-key")
        self.assertTrue(endpoint.needs_key)

    def test_every_provider_resolves_to_an_endpoint_of_its_name(self):
        for name in deepseek.ENDPOINT_NAMES:
            self.assertEqual(deepseek.resolve_endpoint({}, name).name, name)


class TestPickEffort(unittest.TestCase):
    def test_unknown_capabilities_send_the_choice_unchanged(self):
        self.assertEqual(deepseek.pick_effort("max", None), "max")

    def test_model_without_effort_levels_gets_none(self):
        self.assertIsNone(deepseek.pick_effort("high", ()))

    def test_supported_choice_is_kept(self):
        self.assertEqual(deepseek.pick_effort("low", ("low", "high", "max")), "low")

    def test_unsupported_choice_moves_to_the_nearest_level(self):
        self.assertEqual(deepseek.pick_effort("low", ("high", "max")), "high")
        self.assertEqual(deepseek.pick_effort("max", ("none", "low", "high")), "high")
        self.assertEqual(deepseek.pick_effort("low", ("max",)), "max")

    def test_tie_goes_to_the_stronger_level(self):
        self.assertEqual(deepseek.pick_effort("high", ("medium", "xhigh")), "xhigh")


class TestClient(unittest.TestCase):
    local = deepseek.Endpoint("local", "http://local.invalid/v1/chat/completions",
                              "http://local.invalid/v1/models", "", False)

    def test_models_api_deduplicates_ids_and_sends_key_when_given(self):
        response = _json_response({"data": [
            {"id": "local/model-a"},
            {"id": "local/model-a"},
            {"id": "local/model-b"},
            {"object": "model"},
        ]})
        with mock.patch.object(deepseek.urllib.request, "urlopen",
                               return_value=response) as urlopen:
            keyless = deepseek.list_models(self.local)
            keyless_request = urlopen.call_args.args[0]
            deepseek.list_models(self.local._replace(api_key="secret"))
            keyed_request = urlopen.call_args.args[0]

        self.assertEqual(keyless, [deepseek.ModelInfo("local/model-a", None),
                                   deepseek.ModelInfo("local/model-b", None)])
        self.assertEqual(keyless_request.full_url, "http://local.invalid/v1/models")
        self.assertEqual(keyless_request.get_method(), "GET")
        self.assertIsNone(keyless_request.get_header("Authorization"))
        self.assertEqual(keyed_request.get_header("Authorization"), "Bearer secret")

    def test_opencode_go_models_keep_only_chat_completions_with_their_efforts(self):
        endpoint = deepseek.resolve_endpoint({"opencode_api_key": "go-key"}, "opencode_go")
        with mock.patch.object(deepseek.urllib.request, "urlopen",
                               return_value=_json_response(GO_MODELS)) as urlopen:
            models = deepseek.list_models(endpoint)

        self.assertEqual(models, [
            deepseek.ModelInfo("deepseek-v4.1-flash", ("low", "high", "max")),
            deepseek.ModelInfo("mimo-v2.6-flash", ()),
            deepseek.ModelInfo("kimi-k3", ("max",)),
        ])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://opencode.ai/inference/go/v1/models")
        self.assertTrue(request.get_header("X-opencode-session"))

    def test_opencode_go_chat_sends_session_and_client_name(self):
        endpoint = deepseek.resolve_endpoint({"opencode_api_key": "go-key"}, "opencode_go")
        with mock.patch.object(deepseek.urllib.request, "urlopen",
                               side_effect=[_stream_response(), _stream_response()]) as urlopen:
            list(deepseek.stream_chat(endpoint, "glm-5.3", "system", "user"))
            first = urlopen.call_args.args[0]
            list(deepseek.stream_chat(endpoint, "glm-5.3", "system", "user"))
            second = urlopen.call_args.args[0]

        self.assertEqual(first.full_url,
                         "https://opencode.ai/inference/go/openai/v1/chat/completions")
        self.assertEqual(first.get_header("Authorization"), "Bearer go-key")
        self.assertEqual(first.get_header("User-agent"), "AnimaPromptTranslator")
        session = first.get_header("X-opencode-session")
        self.assertTrue(session)
        self.assertEqual(second.get_header("X-opencode-session"), session)

    def test_stream_chat_posts_to_endpoint_with_its_key(self):
        endpoint = self.local._replace(api_key="local-key")
        with mock.patch.object(deepseek.urllib.request, "urlopen",
                               return_value=_stream_response()) as urlopen:
            chunks = list(deepseek.stream_chat(
                endpoint, "glm", "system", "user", reasoning_effort="max"))

        self.assertEqual(chunks, [("delta", "1girl")])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://local.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer local-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "glm")
        self.assertEqual(body["reasoning_effort"], "max")

    def test_keyless_endpoint_sends_no_authorization(self):
        with mock.patch.object(deepseek.urllib.request, "urlopen",
                               return_value=_stream_response()) as urlopen:
            list(deepseek.stream_chat(self.local, "glm", "system", "user"))

        self.assertIsNone(urlopen.call_args.args[0].get_header("Authorization"))

    def test_check_key_tests_the_given_endpoint_and_model(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"data: [DONE]\n"
        with mock.patch.object(deepseek.urllib.request, "urlopen",
                               return_value=response) as urlopen:
            ok, _message = deepseek.check_key(self.local, "glm", "low")

        self.assertTrue(ok)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, self.local.chat_url)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "glm")
        self.assertEqual(body["max_tokens"], 1)
        self.assertEqual(body["reasoning_effort"], "low")

    def test_check_key_refuses_empty_deepseek_key_without_a_request(self):
        endpoint = deepseek.resolve_endpoint({"api_key": ""}, "deepseek")
        with mock.patch.object(deepseek.urllib.request, "urlopen") as urlopen:
            ok, _message = deepseek.check_key(endpoint, deepseek.DEEPSEEK_MODEL)

        self.assertFalse(ok)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
