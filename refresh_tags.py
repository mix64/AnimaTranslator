# -*- coding: utf-8 -*-
"""Danbooru からタグ表を取得して tags/danbooru.csv を作る。

読み取り API はキー不要。毎秒 10 リクエストまでなので、間隔を空けて叩く。

    python refresh_tags.py          # post_count 上位 10 万タグ
    python refresh_tags.py 30       # 上位 3 万タグ（1 ページ 1000 件）

出力は a1111-sd-webui-tagcomplete と同じ 4 列:
    name,category,post_count,"alias1,alias2"
"""
from __future__ import annotations

import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://danbooru.donmai.us"
PER_PAGE = 1000
DEFAULT_PAGES = 100
# 別名の総数は不定。深いページは順位が下がるだけなので上限で切る。
MAX_ALIAS_PAGES = 200
INTERVAL = 0.15                 # 秒。毎秒 10 リクエストの制限に対して余裕をとる
OUT_FILE = Path(__file__).with_name("tags") / "danbooru.csv"
USER_AGENT = "AnimaPromptTranslator/1.0 (tag list refresh)"


def fetch(path: str, params: dict) -> list:
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == 2:
                raise SystemExit(f"取得に失敗しました: {url}\n{exc}")
            time.sleep(2 * (attempt + 1))
    return []


def fetch_tags(pages: int) -> list[dict]:
    tags = []
    for page in range(1, pages + 1):
        batch = fetch("/tags.json", {
            "search[order]": "count",
            "search[hide_empty]": "yes",
            "limit": PER_PAGE,
            "page": page,
        })
        if not batch:
            break
        tags += batch
        print(f"\rタグ {len(tags)} 件…", end="", flush=True)
        time.sleep(INTERVAL)
    print()
    return tags


def fetch_aliases(names: set[str]) -> dict[str, list[str]]:
    """有効な別名のうち、取得済みタグに向いているものだけ集める。"""
    aliases: dict[str, list[str]] = {}
    page, seen = 1, 0
    while page <= MAX_ALIAS_PAGES:
        batch = fetch("/tag_aliases.json", {
            "search[status]": "active",
            "limit": PER_PAGE,
            "page": page,
        })
        if not batch:
            break
        for item in batch:
            target = item.get("consequent_name")
            source = item.get("antecedent_name")
            if target in names and source:
                aliases.setdefault(target, []).append(source)
        seen += len(batch)
        print(f"\r別名 {seen} 件…", end="", flush=True)
        page += 1
        time.sleep(INTERVAL)
    print()
    return aliases


def main() -> None:
    pages = DEFAULT_PAGES
    if len(sys.argv) > 1:
        if not sys.argv[1].isdigit():
            raise SystemExit("使い方: python refresh_tags.py [ページ数]")
        pages = max(1, int(sys.argv[1]))

    tags = fetch_tags(pages)
    if not tags:
        raise SystemExit("タグが 1 件も取得できませんでした。")
    names = {tag["name"] for tag in tags}
    aliases = fetch_aliases(names)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        for tag in tags:
            writer.writerow([tag["name"], tag.get("category", 0),
                             tag.get("post_count", 0),
                             ",".join(aliases.get(tag["name"], []))])

    size = OUT_FILE.stat().st_size / 1024 / 1024
    print(f"完成: {OUT_FILE}  （{len(tags)} タグ / {size:.1f} MB）")


if __name__ == "__main__":
    main()
