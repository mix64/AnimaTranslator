# -*- coding: utf-8 -*-
"""OpenAI 互換 Chat API クライアント（標準ライブラリのみ）。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import NamedTuple

# 出力のブレを抑えるため低め。
TEMPERATURE = 0.3
TIMEOUT = 180
# 疎通確認とモデル一覧の取得は、設定画面で結果を待たせるので短くする。
CHECK_TIMEOUT = 20

# DeepSeek はしばしば 503（Service is too busy）を返す。待てば通るので、
# 本文が 1 文字も届いていないうちは自動で投げ直す。
RETRY_CODES = (429, 500, 502, 503, 504)
RETRY_WAITS = (2, 5, 12)          # 秒。最初の 1 回 + この回数だけ試す

# OpenCode Go は x-opencode-session の無い問い合わせを 400 で断る。
# 起動ごとに 1 つ決め、同じ起動中は使い回す。
# また urllib 既定の User-Agent は opencode.ai の Cloudflare に 403 で弾かれる。
SESSION_ID = str(uuid.uuid4())

# reasoning_effort の強さの順。モデルが選んだ値を受け付けないとき、近いものに寄せる。
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class Provider(NamedTuple):
    """OpenAI 互換の接続先の種類。"""
    name: str
    label: str              # モデル一覧に添える名前
    base_url: str           # /chat/completions の手前まで。空なら url_field の値を使う
    key_field: str          # 設定のうちキーを入れる項目
    needs_key: bool
    key_env: str = ""       # 設定が空のとき、キーを読む環境変数（.env も見る）
    key_page: str = ""
    models_url: str = ""    # 空なら base_url + "/models"
    url_field: str = ""
    models: tuple[str, ...] = ()    # 空なら Models API から取る
    headers: tuple[tuple[str, str], ...] = ()


PROVIDERS = {provider.name: provider for provider in (
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com", "api_key", True,
             key_env="DEEPSEEK_API_KEY",
             key_page="https://platform.deepseek.com/api_keys",
             models=("deepseek-flash",)),
    Provider("opencode_go", "OpenCode Go", "https://opencode.ai/inference/go/openai/v1",
             "opencode_api_key", True,
             key_env="OPENCODE_API_KEY",
             key_page="https://opencode.ai/console",
             models_url="https://opencode.ai/inference/go/v1/models",
             headers=(("x-opencode-session", SESSION_ID),
                      ("User-Agent", "AnimaPromptTranslator"))),
    Provider("local", "ローカル", "", "local_api_key", False, url_field="local_url"),
)}
ENDPOINT_NAMES = tuple(PROVIDERS)
DEEPSEEK_MODEL = PROVIDERS["deepseek"].models[0]


class Endpoint(NamedTuple):
    """1 回の問い合わせに要る、接続先の情報。"""
    name: str           # PROVIDERS のキー
    chat_url: str
    models_url: str
    api_key: str
    needs_key: bool
    headers: tuple[tuple[str, str], ...] = ()


class ModelInfo(NamedTuple):
    id: str
    # 受け付ける reasoning_effort。None は不明（選んだ値をそのまま送る）、
    # 空は推論の強さを指定できない（送らない）。
    efforts: tuple[str, ...] | None = None


def resolve_endpoint(cfg: dict, name: str) -> Endpoint:
    """設定と接続先の名前から、問い合わせ先を組み立てる。"""
    provider = PROVIDERS[name]
    base = provider.base_url or (cfg.get(provider.url_field) or "").strip().rstrip("/")
    return Endpoint(provider.name, base + "/chat/completions",
                    provider.models_url or base + "/models",
                    (cfg.get(provider.key_field) or "").strip(), provider.needs_key,
                    provider.headers)


def pick_effort(chosen: str, efforts: tuple[str, ...] | None) -> str | None:
    """モデルが受け付ける reasoning_effort のうち、chosen に最も近いものを返す。

    並びの上で等距離なら強い方を取る。None を返したら reasoning_effort は送らない。
    """
    if efforts is None:
        return chosen
    known = [effort for effort in efforts if effort in EFFORT_ORDER]
    if chosen in known:
        return chosen
    if not known or chosen not in EFFORT_ORDER:
        return None
    target = EFFORT_ORDER.index(chosen)
    return min(known, key=lambda effort: (abs(EFFORT_ORDER.index(effort) - target),
                                          -EFFORT_ORDER.index(effort)))


def _efforts(item: dict) -> tuple[str, ...] | None:
    capabilities = item.get("capabilities")
    reasoning = capabilities.get("reasoning") if isinstance(capabilities, dict) else None
    if not isinstance(reasoning, dict):
        return None
    effort = reasoning.get("effort")
    if not isinstance(effort, list):
        return ()
    return tuple(value for value in effort if isinstance(value, str))


def list_models(endpoint: Endpoint, timeout: int = CHECK_TIMEOUT) -> list[ModelInfo]:
    """OpenAI 互換の Models API から、Chat Completions で使えるモデルを返す。"""
    headers = {"Accept": "application/json", **dict(endpoint.headers)}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    request = urllib.request.Request(endpoint.models_url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("Models API の応答に data 配列がありません。")

    models: dict[str, ModelInfo] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model = item.get("id")
        api = item.get("api")
        # OpenCode Go は Responses や Anthropic Messages 形式だけのモデルも並べる。
        if isinstance(api, dict) and "chat-completions" not in api:
            continue
        if isinstance(model, str) and model and model not in models:
            models[model] = ModelInfo(model, _efforts(item))
    return list(models.values())


def _post(endpoint: Endpoint, body: dict, timeout: int):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
               **dict(endpoint.headers)}
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
        402: "残高が不足しています。接続先のサービスの管理画面で確認してください。",
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
