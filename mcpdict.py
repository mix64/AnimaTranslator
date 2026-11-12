# -*- coding: utf-8 -*-
"""Danbooru MCP をバックエンドにしたタグ辞書（標準ライブラリのみ）。

tagdict.py と同じ Verdict インターフェースを提供する。照合先が
10 万タグのローカル CSV ではなく、MCP サーバーの全 115 万タグ＋
エイリアス・Wiki・FTS になる。使えない環境では load() が None を返し、
呼び出し側（grounding.py）がローカル辞書へフォールバックする。

MCP は streamable-http トランスポートの JSON-RPC。セッションは
initialize → notifications/initialized の手順で張る。応答は SSE 形式
（event: message / data: {...}）なので、data: 行を組み立てて JSON にする。
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import tagdict
from tagdict import Verdict

TIMEOUT = 10                       # 1 リクエストあたりの上限（秒）
RARE_BELOW = tagdict.RARE_BELOW    # 実在するが稀とみなす件数閾値（CSV と同じ）
MAX_CANDIDATES = tagdict.MAX_CANDIDATES

_KEEP_AS_IS = tagdict._KEEP_AS_IS  # 強調構文など照合対象外の記法

_SPACES = re.compile(r"\s+")


def _db_name(raw: str) -> str:
    """MCP サーバー（DB）側の正規表記に畳む。

    Danbooru のタグ名は空白の代わりに下線を使う（blue_archive など）。
    タグ辞書 CSV は下線を空白に直して持つので、その逆変換になる。
    """
    return _SPACES.sub("_", raw.strip().lower())


# ------------------------------------------------------------------ MCP 通信

class McpError(Exception):
    pass


def _post_json(url: str, payload: dict, session_id: str | None = None) -> tuple[bytes, str | None]:
    """JSON-RPC を POST し、(body, 新しい session_id) を返す。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read(), response.headers.get("Mcp-Session-Id")


def _parse_sse(body: bytes) -> dict:
    """SSE の本文から data: 行を 1 つの JSON に組み立てる。"""
    data_lines = []
    for raw in body.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        raise McpError("MCP サーバーが空の応答を返しました")
    return json.loads("\n".join(data_lines))


class Client:
    """MCP サーバーへの薄いクライアント。1 回の照合ごとにセッションを張る。"""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    # ------------------------------------------------------------- セッション

    def _session(self) -> str:
        """initialize → initialized まで済ませた session_id を返す。"""
        body, sid = _post_json(self.url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "AnimaPromptTranslator",
                                      "version": "1.0"}},
        })
        if not sid:
            raise McpError("MCP サーバーがセッション ID を返しません")
        _post_json(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        return sid

    def call(self, name: str, arguments: dict) -> dict:
        """ツールを呼び、ツールの返り値（dict）を返す。"""
        sid = self._session()
        try:
            body, _ = _post_json(self.url, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, sid)
        finally:
            _end_session(self.url, sid)
        message = _parse_sse(body)
        error = message.get("error")
        if error:
            raise McpError(f"MCP ツール {name} エラー: {error.get('message', error)}")
        result = message.get("result") or {}
        content = result.get("content") or []
        text = "".join(part.get("text", "") for part in content
                       if part.get("type") == "text")
        if not text:
            raise McpError(f"MCP ツール {name} が空の結果を返しました")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise McpError(f"MCP ツール {name} の結果が JSON でない: {exc}") from exc

    # ------------------------------------------------------------- 辞書操作

    def resolve(self, tag: str) -> Verdict:
        return self.resolve_many([tag])[0]

    def resolve_many(self, tags: list[str]) -> list[Verdict]:
        """複数タグをまとめて get_tags で解決する。順序は入力と同じ。"""
        result_map: dict[str, Verdict] = {}
        pending: list[tuple[str, str]] = []      # (原表記, DB 正規表記)

        for tag in tags:
            raw = tag.strip()
            if not raw or _KEEP_AS_IS.search(raw):
                result_map[raw] = Verdict(raw, "skip", raw, 0, [])
                continue
            name = _db_name(raw)
            result_map[raw] = Verdict(raw, "pending", raw, 0, [])
            pending.append((raw, name))

        # サーバー側の get_tags は 1 回 50 件までなので分割する。
        for start in range(0, len(pending), 50):
            batch = pending[start:start + 50]
            try:
                entries = self.call("get_tags", {
                    "names": [name for _raw, name in batch],
                })["resolved"]
                if len(entries) != len(batch):
                    raise McpError("MCP サーバーのタグ件数が要求と一致しません")
            except (McpError, urllib.error.URLError, OSError, ValueError,
                    KeyError, TypeError) as exc:
                # 通信失敗と「存在しないタグ」を混同せず、呼び出し側で CSV に戻す。
                if isinstance(exc, McpError):
                    raise
                raise McpError(f"MCP タグ照合に失敗しました: {exc}") from exc
            for (raw, _name), entry in zip(batch, entries):
                verdict = result_map[raw]
                if entry.get("id") is None:      # {"query": ..., "resolved": False}
                    result_map[raw] = verdict._replace(
                        status="unknown", canonical=_name.replace("_", " "))
                    continue
                canonical = entry["resolved_name"].replace("_", " ")
                count = int(entry.get("post_count") or 0)
                if count < RARE_BELOW:
                    result_map[raw] = verdict._replace(
                        status="rare", canonical=canonical, count=count)
                else:
                    result_map[raw] = verdict._replace(
                        status="ok", canonical=canonical, count=count)

        # rare / unknown にだけ候補を付ける。候補が取れなくても判定は残す。
        for raw, verdict in list(result_map.items()):
            if verdict.status in ("rare", "unknown"):
                candidates = self._candidates(verdict.canonical)
                result_map[raw] = verdict._replace(candidates=candidates)

        return [result_map.get(tag.strip()) or Verdict(tag, "unknown", tag, 0, [])
                for tag in tags]

    def _candidates(self, name: str) -> list[tuple[str, int]]:
        """構成語が重なる実在タグを FTS で集める。一般タグのみ・括弧付きは除外。"""
        try:
            result = self.call("find_tags", {
                "query": _db_name(name), "limit": MAX_CANDIDATES,
            })
        except (McpError, urllib.error.URLError, OSError, ValueError):
            return []
        found: list[tuple[str, int]] = []
        for row in result.get("results", []):
            candidate = str(row.get("name") or "")
            if candidate == name or candidate == _db_name(name):
                continue
            if row.get("category") != tagdict.GENERAL or "(" in candidate:
                continue
            found.append((candidate, int(row.get("post_count") or 0)))
        return found[:MAX_CANDIDATES]

    def check(self) -> tuple[bool, str]:
        """疎通確認。get_context を 1 回呼ぶ。"""
        try:
            context = self.call("get_context", {})
            tag_db = context.get("tag_database", {})
            return True, (f"接続できました。タグ {tag_db.get('tags', '?')} 件・"
                          f"スナップショット {tag_db.get('source_snapshot', '?')}")
        except Exception as exc:
            return False, f"接続に失敗しました: {exc}"


def _end_session(url: str, session_id: str) -> None:
    """Streamable HTTP の DELETE でセッションを閉じる。"""
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id,
            },
            method="DELETE",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read()
    except Exception:
        # 次回は必ず新しいセッションを張るので、終了失敗は処理結果に影響させない。
        pass


# ------------------------------------------------------------------ 読み込み

_lock = threading.Lock()
_client: Client | None = None
_client_url = ""


def load(url: str) -> Client | None:
    """設定された URL でクライアントを組む。成功するまで毎回試す。

    URL が空・繋がらない環境では None を返し、呼び出し側はローカル辞書へ
    フォールバックする。成功後はキャッシュを返す。UI スレッドから呼ばないこと
    （ネットワーク I/O）。
    """
    global _client, _client_url
    url = (url or "").strip()
    if not url:
        return None
    with _lock:
        if _client is not None and _client_url == url:
            return _client
        candidate = Client(url)
        ok, _message = candidate.check()
        if ok:
            _client = candidate
            _client_url = url
            return _client
        return None
