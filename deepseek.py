# -*- coding: utf-8 -*-
"""OpenAI 互換 Chat API クライアント（標準ライブラリのみ）。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import NamedTuple

DEEPSEEK_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
ENDPOINT_NAMES = ("deepseek", "local")

# 出力のブレを抑えるため低め。
TEMPERATURE = 0.3
TIMEOUT = 180
# 疎通確認とモデル一覧の取得は、設定画面で結果を待たせるので短くする。
CHECK_TIMEOUT = 20

# DeepSeek はしばしば 503（Service is too busy）を返す。待てば通るので、
# 本文が 1 文字も届いていないうちは自動で投げ直す。
RETRY_CODES = (429, 500, 502, 503, 504)
RETRY_WAITS = (2, 5, 12)          # 秒。最初の 1 回 + この回数だけ試す


class Endpoint(NamedTuple):
    """1 回の問い合わせに要る、接続先の情報。"""
    name: str           # "deepseek" | "local"
    chat_url: str
    models_url: str
    api_key: str
    needs_key: bool


def resolve_endpoint(cfg: dict, name: str) -> Endpoint:
    """設定と接続先の名前から、問い合わせ先を組み立てる。"""
    if name == "local":
        base = (cfg.get("local_url") or "").strip().rstrip("/")
        return Endpoint("local", base + "/chat/completions", base + "/models",
                        (cfg.get("local_api_key") or "").strip(), False)
    return Endpoint("deepseek", DEEPSEEK_URL + "/chat/completions",
                    DEEPSEEK_URL + "/models",
                    (cfg.get("api_key") or "").strip(), True)


def list_models(url: str, api_key: str = "",
                timeout: int = CHECK_TIMEOUT) -> list[str]:
    """OpenAI 互換の Models API から、選択可能なモデル ID を返す。"""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("Models API の応答に data 配列がありません。")

    models = []
    for item in data:
        model = item.get("id") if isinstance(item, dict) else None
        if isinstance(model, str) and model and model not in models:
            models.append(model)
    return models


def _post(endpoint: Endpoint, body: dict, timeout: int):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    req = urllib.request.Request(
        endpoint.chat_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def error_text(exc: urllib.error.HTTPError) -> str:
    """HTTP エラーを、利用者が次に何をすればよいか分かる文にする。"""
    detail = exc.read().decode("utf-8", "replace")[:1000]
    try:
        detail = json.loads(detail)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        pass
    hint = {
        401: "API キーが正しくないようです。設定の API キーを確認してください。",
        402: "残高が不足しています。DeepSeek Platform で確認してください。",
        429: "レート制限に達しました。少し待ってから再試行してください。",
        500: "API 側の一時的な障害です。少し待つと通ります。",
        503: "API が混雑しています。少し待つと通ります。",
    }.get(exc.code, "")
    return f"API エラー {exc.code}: {detail}" + (f"\n{hint}" if hint else "")


def _pause(seconds: float, cancel) -> bool:
    """待つ。中断されたら True を返す。"""
    if cancel is None:
        time.sleep(seconds)
        return False
    return cancel.wait(seconds)


def stream_chat(endpoint: Endpoint, model: str, system_prompt: str, user_text: str,
                cancel=None, reasoning_effort: str | None = None,
                timeout: int = TIMEOUT):
    """応答の本文を逐次 yield する。

    yield は (kind, payload)。kind は "delta"（本文の断片）、
    "reasoning"（推論過程）、"notice"（再試行などの経過）、
    "error"（メッセージ）のいずれか。
    cancel は threading.Event。set されたら速やかに打ち切る。
    """
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": True,
        "temperature": TEMPERATURE,
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort

    # 接続だけを再試行する。本文が流れ始めてからの切断は、
    # 投げ直すと本文が二重になるので再試行しない。
    response = None
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            response = _post(endpoint, body, timeout)
            break
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRY_CODES
            message = error_text(exc)
        except Exception as exc:
            retryable = True
            message = f"接続に失敗しました: {exc}"
        if not retryable or attempt == len(RETRY_WAITS):
            yield "error", message
            return
        wait = RETRY_WAITS[attempt]
        yield "notice", (f"API が混雑しています。{wait} 秒待って再試行します"
                         f"（{attempt + 2}/{len(RETRY_WAITS) + 1}）")
        if _pause(wait, cancel):
            return

    try:
        with response:
            for raw in response:
                if cancel is not None and cancel.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield "reasoning", reasoning
                if delta.get("content"):
                    yield "delta", delta["content"]
    except Exception as exc:
        if cancel is None or not cancel.is_set():
            yield "error", f"受信中に切断されました: {exc}"


def complete(endpoint: Endpoint, model: str, system_prompt: str, user_text: str,
             cancel=None, reasoning_effort: str | None = None,
             timeout: int = TIMEOUT) -> tuple[str, str | None]:
    """応答を一括で受け取る。返り値は (本文, エラーメッセージ)。

    途中経過を見せる必要のない短い問い合わせ（タグ照合など）に使う。
    """
    parts: list[str] = []
    error = None
    for kind, payload in stream_chat(endpoint, model, system_prompt, user_text,
                                     cancel, reasoning_effort, timeout):
        if kind == "delta":
            parts.append(payload)
        elif kind == "error":
            error = payload
    return "".join(parts).strip(), error


def check_key(endpoint: Endpoint, model: str, reasoning_effort: str | None = None,
              timeout: int = CHECK_TIMEOUT) -> tuple[bool, str]:
    """接続先とキーの疎通確認。最小のリクエストを 1 回だけ投げる。"""
    if endpoint.needs_key and not endpoint.api_key:
        return False, "API キーが空です。"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": True,
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    try:
        with _post(endpoint, body, timeout) as response:
            response.read()
        return True, "接続できました。"
    except urllib.error.HTTPError as exc:
        return False, error_text(exc)
    except Exception as exc:
        return False, f"接続に失敗しました: {exc}"
