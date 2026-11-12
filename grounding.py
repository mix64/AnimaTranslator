# -*- coding: utf-8 -*-
"""書き起こしたタグを Danbooru の語彙に寄せる照合パス。

流れは 3 段。

1. 1 パス目の出力のタグ行を、辞書（Danbooru MCP か tagdict）で判定する
2. Danbooru に無い / 稀なタグだけを、候補付きで LLM に再照会する
3. LLM が「これで意図が保てる」と答えた分だけ差し替える

決定論的な置換はしない。辞書は実在と一般度を答えるだけで、
意図を保てるかの判断は LLM に委ねる。実在しない候補を返してきた場合と、
LLM が KEEP と答えた場合は原語のまま残す。

プロンプト中のタグと辞書の見出しの表記の違いは、ここで一か所で吸収する。
Anima の制御用の語（品質・年代・レーティング・@作者）は辞書に引かない。
名前の一部の括弧はプロンプトでは fern \\(sousou no frieren\\) と逃がして書き、
辞書では fern (sousou no frieren) として引く。
"""
from __future__ import annotations

import re
from typing import NamedTuple

import deepseek
import mcpdict
import prompts
import tagdict


class Mark(NamedTuple):
    """出力全文の中で色を付ける範囲。"""
    start: int          # 先頭からの文字数
    end: int
    kind: str           # "swap"（寄せた） | "kept"（実在しないまま残した）
    original: str       # 寄せる前のタグ


class Result(NamedTuple):
    text: str           # 差し替え後の全文
    marks: list         # Mark の並び
    swapped: int
    kept: int
    error: str | None   # 照合パスの API エラー。原文はそのまま返す
    checked: bool       # 辞書があって照合を実行したか
    backend: str | None # "mcp"（Danbooru MCP） | "csv"（ローカル辞書） | None


# 名前 + 逃がした修飾 1 つ。例: fern \(sousou no frieren\)
_QUALIFIED = re.compile(r"(?P<name>[^\\()]+?)\s*\\\((?P<qualifier>[^\\()]+)\\\)")
# 上の形以外で括弧や \ を含むタグは強調構文などなので触らない。
_SYNTAX = re.compile(r"[()\\]")


def _lookup(tag: str) -> tuple[str, str | None] | None:
    """辞書で引く名前と、修飾を外した名前を返す。触らないタグは None。"""
    if prompts.is_control_tag(tag):
        return None
    match = _QUALIFIED.fullmatch(tag)
    if match:
        name = match.group("name").strip()
        return f"{name} ({match.group('qualifier').strip()})", name
    if _SYNTAX.search(tag):
        return None
    return tag, None


def _escape(name: str) -> str:
    """名前の一部の括弧を、強調構文と区別できるよう逃がす。"""
    return name.replace("(", r"\(").replace(")", r"\)")


def ground(endpoint: deepseek.Endpoint, model: str, style: str, extra: str,
           output: str, cancel=None, mcp_url: str = "",
           reasoning_effort: str | None = None) -> Result:
    """1 パス目の出力を受け取り、タグを寄せた全文を返す。

    mcp_url が設定されていて繋がれば Danbooru MCP（全 115 万タグ）で照合し、
    使えなければローカル CSV 辞書（tagdict）にフォールバックする。
    """
    client = mcpdict.load(mcp_url) if mcp_url else None
    index = client or tagdict.load()
    if index is None:
        return Result(output, [], 0, 0, None, False, None)
    lines = output.split("\n")
    parsed = {}
    for number in prompts.tag_lines(output, style):
        label, tags = prompts.split_tag_line(lines[number])
        if tags:
            parsed[number] = (label, tags)
    if not parsed:
        return Result(output, [], 0, 0, None, False, None)

    unique = list(dict.fromkeys(tag for _label, tags in parsed.values()
                                for tag in tags))
    lookups = {tag: _lookup(tag) for tag in unique}
    names = list(dict.fromkeys(name for lookup in lookups.values() if lookup
                               for name in lookup if name))
    found: dict[str, tagdict.Verdict] = {}
    if client is not None:
        try:
            found = dict(zip(names, client.resolve_many(names)))
        except mcpdict.McpError:
            # 疎通確認後に MCP が落ちた場合も、存在しないタグ扱いにはせず CSV へ戻す。
            client = None
            index = tagdict.load()
    if client is None:
        if index is None:
            return Result(output, [], 0, 0, None, False, None)
        found = {name: index.resolve(name) for name in names}
    backend = "mcp" if client is not None else "csv"

    verdicts = {tag: _verdict(tag, lookup, found) for tag, lookup in lookups.items()}
    queries = [verdict for verdict in verdicts.values()
               if verdict.status in ("rare", "unknown")]
    swaps: dict[str, str] = {}
    error = None
    if queries:
        reply, error = deepseek.complete(
            endpoint, model,
            prompts.build_ground_prompt(extra),
            prompts.build_ground_request(output, queries),
            cancel, reasoning_effort)
        if cancel is not None and cancel.is_set():
            return Result(output, [], 0, 0, None, True, backend)
        if reply:
            swaps = _parse_reply(reply, queries, index)

    marks: list[Mark] = []
    offset = 0
    for number, line in enumerate(lines):
        if number in parsed:
            label, tags = parsed[number]
            lines[number], line_marks = _rebuild(label, tags, verdicts, swaps, offset)
            marks += line_marks
        offset += len(lines[number]) + 1        # 改行 1 文字

    swapped = sum(1 for mark in marks if mark.kind == "swap")
    return Result("\n".join(lines), marks, swapped, len(marks) - swapped,
                  error, True, backend)


def _verdict(tag: str, lookup, found: dict) -> tagdict.Verdict:
    """辞書の判定を、プロンプト中の表記に戻す。"""
    if lookup is None:
        return tagdict.Verdict(tag, "skip", tag, 0, [])
    key, bare = lookup
    verdict = found[key]
    if verdict.status == "skip":
        return verdict._replace(tag=tag, canonical=tag)
    candidates = verdict.candidates
    if bare and verdict.status == "unknown":
        # 修飾付きの名前が無いとき、修飾を外した名前は種別を問わず候補に出す。
        # キャラ名に作品名を誤って付けた場合（frieren \(sousou no frieren\)）の正解になる。
        plain = found[bare]
        if plain.status in ("ok", "rare"):
            candidates = [(_escape(plain.canonical), plain.count)] + [
                item for item in candidates if item[0] != plain.canonical]
    return verdict._replace(tag=tag, canonical=_escape(verdict.canonical),
                            candidates=candidates)


def _rebuild(label: str, tags: list[str], verdicts: dict, swaps: dict,
             offset: int) -> tuple[str, list[Mark]]:
    """1 行を組み直し、色を付ける範囲を拾う。

    別名と下線表記の正規化は、意味が変わらないので印を付けずに直す。
    """
    marks: list[Mark] = []
    pieces: list[str] = []
    emitted: set[str] = set()
    position = offset + len(label)
    for tag in tags:
        verdict = verdicts[tag]
        replacement = swaps.get(tag)
        if replacement:
            replacement_tags = []
            for part in replacement.split(", "):
                if not part or part in emitted:
                    continue
                replacement_tags.append(part)
                emitted.add(part)
            if not replacement_tags:
                continue
            text, kind = ", ".join(replacement_tags), "swap"
        elif verdict.status in ("rare", "unknown"):
            text, kind = verdict.canonical, "kept"
        else:
            text, kind = verdict.canonical, ""
        if not replacement:
            if text in emitted:
                continue
            emitted.add(text)
        if kind:
            marks.append(Mark(position, position + len(text), kind, tag))
        pieces.append(text)
        position += len(text) + 2              # ", "
    return label + ", ".join(pieces), marks


def _parse_reply(reply: str, queries: list, index) -> dict:
    """"<元のタグ> => <寄せ先>" の行を読む。

    寄せ先が実在しなければ採用しない。LLM が架空のタグを返しても
    そのまま通さないための最後の関門。寄せ先は括弧を逃がしても逃がさなくても
    名前として読み、書き戻すときは逃がす。
    """
    wanted = {verdict.tag: verdict for verdict in queries}
    swaps: dict[str, str] = {}
    for line in reply.splitlines():
        if "=>" not in line:
            continue
        left, right = line.split("=>", 1)
        original = left.strip().strip("\"'`-* ")
        target = wanted.get(original)
        if target is None:
            continue
        right = right.strip().strip("\"'`")
        if not right or right.upper().startswith("KEEP"):
            continue

        resolved: list[str] = []
        for tag in right.split(","):
            verdict = index.resolve(tag.replace("\\", ""))
            if verdict.status not in ("ok", "rare"):
                resolved = []
                break
            resolved.append(_escape(verdict.canonical))
        if resolved and resolved != [target.canonical]:
            swaps[original] = ", ".join(dict.fromkeys(resolved))
    return swaps
