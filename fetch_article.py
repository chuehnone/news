#!/usr/bin/env python3
"""抓取新聞內文，輸出純文字段落。

用途：批次評分時 WebFetch 被擋的網站（如 BBC）改用此腳本。

用法：
    python3 fetch_article.py <url> [url ...]
    python3 fetch_article.py --max-chars 5000 <url>
"""

import argparse
import html
import re
import sys
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# 各站頁尾推薦區的起始標記：遇到就截斷，避免混入無關內容
TAIL_MARKERS = ["視頻,", "音頻加註文字", "頭條新聞", "熱門內容", "End of content"]


def fetch_article(url, max_chars=4000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    raw = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.S)
    raw = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.S)
    paragraphs = []
    for p in re.findall(r"<p[^>]*>(.*?)</p>", raw, flags=re.S):
        p = html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        if p:
            paragraphs.append(p)
    body = "\n".join(paragraphs)
    for marker in TAIL_MARKERS:
        idx = body.find(marker)
        if idx > 200:  # 開頭附近命中多半是誤判，只截尾部
            body = body[:idx]
    return body[:max_chars].strip()


def main():
    parser = argparse.ArgumentParser(description="抓取新聞內文")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--max-chars", type=int, default=4000, help="每篇最多輸出字元數（預設 4000）")
    args = parser.parse_args()

    failed = 0
    for url in args.urls:
        print(f"{'=' * 20} {url}")
        try:
            print(fetch_article(url, args.max_chars))
        except Exception as e:
            failed += 1
            print(f"FETCH_ERROR: {e}")
    sys.exit(1 if failed == len(args.urls) else 0)


if __name__ == "__main__":
    main()
