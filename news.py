#!/usr/bin/env python3
"""新聞重要性評分資料庫 CLI。

用法：
    python3 news.py init                # 建立資料庫
    python3 news.py add <file.json>    # 新增一筆評分結果（也可從 stdin 讀入：python3 news.py add -）
    python3 news.py list [--grade S]   # 快速列出資料庫內容
    python3 news.py serve [--port 8765]  # 啟動網頁介面

JSON 格式（/news-importance-score 的評分結果）：
{
  "title": "新聞標題",
  "url": "https://...",
  "summary": "新聞摘要",
  "news_date": "2026-07-05",
  "total_score": 82,
  "grade": "A",
  "section": "影響未來的趨勢",
  "one_line": "一句話判斷",
  "why_important": "為什麼重要",
  "affected": "可能影響誰",
  "watch_next": ["觀察指標 1", "觀察指標 2"],
  "dimensions": {
    "scope":       {"score": 20, "reason": "影響範圍理由"},
    "duration":    {"score": 16, "reason": "影響時間理由"},
    "decision":    {"score": 15, "reason": "決策相關性理由"},
    "structural":  {"score": 16, "reason": "結構性意義理由"},
    "credibility": {"score": 12, "reason": "事實可信度理由"}
  }
}
total_score 與 grade 可省略，會自動由 dimensions 加總、依分級表判定。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "news.db"

DIMENSIONS = [
    ("scope", "影響範圍", 25),
    ("duration", "影響時間", 20),
    ("decision", "決策相關性", 20),
    ("structural", "結構性意義", 20),
    ("credibility", "事實可信度", 15),
]

GRADE_LABELS = {
    "S": "今日必讀",
    "A": "重要新聞",
    "B": "值得追蹤",
    "C": "可簡短提及",
    "D": "多半是噪音",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT,
    summary TEXT,
    news_date TEXT,
    total_score INTEGER NOT NULL,
    grade TEXT NOT NULL,
    section TEXT,
    one_line TEXT,
    why_important TEXT,
    affected TEXT,
    watch_next TEXT,
    scope_score INTEGER, scope_reason TEXT,
    duration_score INTEGER, duration_reason TEXT,
    decision_score INTEGER, decision_reason TEXT,
    structural_score INTEGER, structural_reason TEXT,
    credibility_score INTEGER, credibility_reason TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_news_date ON news(news_date);
CREATE INDEX IF NOT EXISTS idx_news_grade ON news(grade);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def grade_of(total):
    if total >= 85:
        return "S"
    if total >= 70:
        return "A"
    if total >= 55:
        return "B"
    if total >= 40:
        return "C"
    return "D"


def cmd_init(_args):
    connect().close()
    print(f"已建立資料庫：{DB_PATH}")


def cmd_add(args):
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    data = json.loads(raw)

    if not data.get("title"):
        sys.exit("錯誤：缺少 title")

    dims = data.get("dimensions", {})
    dim_values = {}
    for key, label, max_score in DIMENSIONS:
        d = dims.get(key, {})
        score = d.get("score")
        if score is not None and not (0 <= score <= max_score):
            sys.exit(f"錯誤：{label}（{key}）分數 {score} 超出範圍 0–{max_score}")
        dim_values[f"{key}_score"] = score
        dim_values[f"{key}_reason"] = d.get("reason")

    total = data.get("total_score")
    if total is None:
        scores = [dim_values[f"{k}_score"] for k, _, _ in DIMENSIONS]
        if any(s is None for s in scores):
            sys.exit("錯誤：缺少 total_score，且 dimensions 分數不完整，無法自動加總")
        total = sum(scores)
    grade = data.get("grade") or grade_of(total)

    watch = data.get("watch_next")
    if isinstance(watch, list):
        watch = json.dumps(watch, ensure_ascii=False)

    conn = connect()
    url = data.get("url")
    if url:
        dup = conn.execute("SELECT id, title FROM news WHERE url = ?", (url,)).fetchone()
        if dup and not args.force:
            sys.exit(f"錯誤：相同連結已存在（id={dup['id']}：{dup['title']}），如要重複新增請加 --force")

    row = {
        "title": data["title"],
        "url": url,
        "summary": data.get("summary"),
        "news_date": data.get("news_date"),
        "total_score": total,
        "grade": grade,
        "section": data.get("section"),
        "one_line": data.get("one_line"),
        "why_important": data.get("why_important"),
        "affected": data.get("affected"),
        "watch_next": watch,
        **dim_values,
    }
    cols = ", ".join(row)
    placeholders = ", ".join(f":{k}" for k in row)
    cur = conn.execute(f"INSERT INTO news ({cols}) VALUES ({placeholders})", row)
    conn.commit()
    print(f"已新增 id={cur.lastrowid}：[{grade} 級 {total} 分] {data['title']}")
    conn.close()


def cmd_list(args):
    conn = connect()
    sql = "SELECT id, news_date, grade, total_score, title FROM news"
    params = []
    if args.grade:
        sql += " WHERE grade = ?"
        params.append(args.grade.upper())
    sql += " ORDER BY news_date DESC, total_score DESC"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("（資料庫內沒有符合的新聞）")
        return
    for r in rows:
        print(f"{r['id']:>4}  {r['news_date'] or '----------'}  {r['grade']} {r['total_score']:>3}  {r['title']}")
    conn.close()


def cmd_serve(args):
    from server import run

    run(port=args.port)


def main():
    parser = argparse.ArgumentParser(description="新聞重要性評分資料庫")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="建立資料庫")

    p_add = sub.add_parser("add", help="新增一筆評分結果（JSON 檔或 - 表示 stdin）")
    p_add.add_argument("file")
    p_add.add_argument("--force", action="store_true", help="允許重複連結")

    p_list = sub.add_parser("list", help="列出新聞")
    p_list.add_argument("--grade", help="只列出指定等級（S/A/B/C/D）")

    p_serve = sub.add_parser("serve", help="啟動網頁介面")
    p_serve.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()
    {"init": cmd_init, "add": cmd_add, "list": cmd_list, "serve": cmd_serve}[args.command](args)


if __name__ == "__main__":
    main()
