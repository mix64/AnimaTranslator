# -*- coding: utf-8 -*-
"""Danbooru タグ辞書。

日本語を訳すためのものではない。訳語が Danbooru に実在するか、
画像モデルが反応するほど一般的かを判定し、近い候補を並べるだけ。
どれを採るかは LLM が決める（grounding.py）。

タグ表は tags/danbooru.csv。refresh_tags.py で取得・更新する。
表が無い環境では load() が None を返し、照合を丸ごと省略する。
"""
from __future__ import annotations

import csv
import re
import sys
import threading
from difflib import get_close_matches
from pathlib import Path
from typing import NamedTuple

_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
TAGS_FILE = _BASE / "tags" / "danbooru.csv"

# post_count がこれ未満なら「実在するが稀」。学習例が少なく反応しにくい。
RARE_BELOW = 100
# 1 タグあたり LLM に見せる候補の上限
MAX_CANDIDATES = 20
# そのうち、構成語ごとに必ず確保する枠
PER_WORD_SHARE = 4
# 構成語 1 語あたりに覚える候補数（post_count 上位から）
PER_WORD = 60
# 綴り違いの照合に使う母集団（post_count 上位から）
FUZZY_POOL = 20000
# Danbooru のタグ種別。0 以外はキャラ名・作品名・絵師名・メタ情報で、
# 描写タグの言い換え候補にはならないので候補には出さない。
# LLM が自分の知識でキャラ名を挙げてきた場合は、実在するので採用する。
GENERAL = 0

# 触ってはいけない記法。強調構文 (tag) / (tag:1.2)、<lora:...>、BREAK / AND、
# ワイルドカード {a|b}、ラベル。名前の後ろに修飾が付いた正規タグ
# （fern (sousou no frieren)）は引ける。プロンプト中の逃がし記法を外して
# この形に直すのは grounding.py の役目。
_KEEP_AS_IS = re.compile(r"[<>{}\[\]:|\\]|^\s*\(|^[A-Z0-9 ]+$")
_SPACES = re.compile(r"\s+")


class Verdict(NamedTuple):
    """1 タグの判定結果。"""
    tag: str            # 入力された表記
    status: str         # "ok" | "rare" | "unknown" | "skip"
    canonical: str      # 採用すべき表記（別名・下線を解決した形）
    count: int          # post_count。unknown / skip は 0
    candidates: list    # (タグ名, post_count) の並び。rare / unknown のときだけ


def normalize(tag: str) -> str:
    """下線を空白に直し、小文字に畳む。Danbooru 側の表記とアプリ側を揃える。"""
    return _SPACES.sub(" ", tag.replace("_", " ")).strip().lower()


class TagIndex:
    def __init__(self):
        self.tags: dict[str, tuple[int, int]] = {}      # 正規名 -> (種別, post_count)
        self.aliases: dict[str, str] = {}               # 別名 -> 正規名
        self.by_word: dict[str, list[str]] = {}         # 構成語 -> 正規名（count 降順）
        self.popular: list[str] = []                    # count 降順の正規名

    # ------------------------------------------------------------- 構築

    def add(self, name: str, category: int, count: int, aliases: str) -> None:
        canonical = normalize(name)
        if not canonical:
            return
        known = self.tags.get(canonical)
        if known is None or known[1] < count:
            self.tags[canonical] = (category, count)
        for alias in aliases.split(","):
            key = normalize(alias)
            if key and key != canonical and key not in self.tags:
                self.aliases[key] = canonical

    def finish(self) -> "TagIndex":
        """post_count 降順の索引を組む。候補提示はここに依存する。"""
        self.popular = sorted(self.tags, key=lambda name: -self.tags[name][1])
        for name in self.popular:
            if self.tags[name][0] != GENERAL or "(" in name:
                continue
            for word in name.split():
                bucket = self.by_word.setdefault(word, [])
                if len(bucket) < PER_WORD:
                    bucket.append(name)
        # 別名が後から正規タグとして現れていたら別名側を捨てる
        for key in [k for k in self.aliases if k in self.tags]:
            del self.aliases[key]
        return self

    # ------------------------------------------------------------- 判定

    def resolve(self, tag: str) -> Verdict:
        raw = tag.strip()
        if not raw or _KEEP_AS_IS.search(raw):
            return Verdict(raw, "skip", raw, 0, [])

        name = normalize(raw)
        entry = self.tags.get(name)
        if entry is None:
            canonical = self.aliases.get(name)
            if canonical is None:
                return Verdict(raw, "unknown", raw, 0, self.candidates(name))
            name, entry = canonical, self.tags[canonical]

        count = entry[1]
        if count < RARE_BELOW:
            return Verdict(raw, "rare", name, count, self.candidates(name))
        return Verdict(raw, "ok", name, count, [])

    def candidates(self, name: str) -> list[tuple[str, int]]:
        """意味の近さは LLM に任せ、構成語が重なる実在タグを広めに集める。"""
        buckets = [self.by_word.get(word, ()) for word in name.split()]
        found: dict[str, int] = {}

        def take(candidate: str) -> None:
            if candidate != name:
                found[candidate] = self.tags[candidate][1]

        # まず語ごとに枠を確保する。"gothic dress" の "gothic" のような珍しい語が、
        # "dress" の人気タグに枠を全部取られてしまうのを防ぐ。
        for rank in range(PER_WORD_SHARE):
            for bucket in buckets:
                if rank < len(bucket):
                    take(bucket[rank])
        # 残りは件数順で埋める。"silver hair" に対する "grey hair" のように、
        # 一般的な語の少し下の順位に正解があることも多い。
        rest = sorted({c for bucket in buckets for c in bucket} - set(found),
                      key=lambda candidate: -self.tags[candidate][1])
        for candidate in rest[:max(0, MAX_CANDIDATES - len(found))]:
            take(candidate)

        if len(found) < 5:      # 語が 1 つも当たらない＝綴り違いの可能性
            for candidate in get_close_matches(name, self.popular[:FUZZY_POOL],
                                               n=8, cutoff=0.7):
                take(candidate)
        return sorted(found.items(), key=lambda item: -item[1])


# ------------------------------------------------------------------ 読み込み

_lock = threading.Lock()
_index: TagIndex | None = None
_tried = False


def load(path: Path = TAGS_FILE) -> TagIndex | None:
    """タグ表を読み込む（初回だけ）。無い・壊れている場合は None。

    10 万行の読み込みに 1 秒弱かかるので、UI スレッドから呼ばないこと。
    """
    global _index, _tried
    with _lock:
        if _tried:
            return _index
        _tried = True
        try:
            with open(path, encoding="utf-8", newline="") as stream:
                index = TagIndex()
                for row in csv.reader(stream):
                    if not row or not row[0]:
                        continue
                    category = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
                    count = int(row[2]) if len(row) > 2 and row[2].isdigit() else 0
                    index.add(row[0], category, count, row[3] if len(row) > 3 else "")
        except (OSError, ValueError, csv.Error):
            return None
        _index = index.finish() if index.tags else None
        return _index
