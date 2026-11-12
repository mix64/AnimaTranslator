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


class TestClient(unittest.TestCase):
    local = deepseek.Endpoint("local", "http://local.invalid/v1/chat/completions",
                              "http://local.invalid/v1/models", "", False)

    def test_models_api_deduplicates_ids_and_sends_key_when_given(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "data": [
                {"id": "local/model-a"},
                {"id": "local/model-a"},
                {"id": "local/model-b"},
                {"object": "model"},
            ],
        }).encode("utf-8")
        with mock.patch.object(deepseek.urllib.request, "urlopen",
                               return_value=response) as urlopen:
            keyless = deepseek.list_models("http://local.invalid/v1/models")
            keyless_request = urlopen.call_args.args[0]
            deepseek.list_models("http://local.invalid/v1/models", "secret")
            keyed_request = urlopen.call_args.args[0]

        self.assertEqual(keyless, ["local/model-a", "local/model-b"])
        self.assertEqual(keyless_request.get_method(), "GET")
        self.assertIsNone(keyless_request.get_header("Authorization"))
        self.assertEqual(keyed_request.get_header("Authorization"), "Bearer secret")

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
