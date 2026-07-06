#!/usr/bin/env python3
"""新聞重要性評分的網頁介面（標準庫實作，無外部依賴）。

啟動：python3 news.py serve [--port 8765]
"""

import json
import re
import sqlite3
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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

STYLE = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #1a1d21; --muted: #6b7280;
  --border: #e5e7eb; --link: #2563eb;
  --s: #dc2626; --a: #ea580c; --b: #ca8a04; --c: #6b7280; --d: #9ca3af;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1b1f24; --text: #e6e8ea; --muted: #9aa3ad;
    --border: #2c3238; --link: #7ab0ff;
    --s: #f87171; --a: #fb923c; --b: #facc15; --c: #9ca3af; --d: #6b7280;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, "PingFang TC", "Noto Sans TC", sans-serif;
  line-height: 1.6;
}
.wrap { max-width: 860px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: .85rem; margin-bottom: 20px; }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 24px; }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; }
.date-filter select {
  padding: 5px 10px; border-radius: 999px; border: 1px solid var(--border);
  background: var(--card); color: var(--text); font-size: .85rem;
  font-family: inherit; cursor: pointer;
}
.tabs a {
  padding: 5px 14px; border-radius: 999px; border: 1px solid var(--border);
  text-decoration: none; color: var(--text); font-size: .85rem; background: var(--card);
}
.tabs a.active { background: var(--text); color: var(--bg); border-color: var(--text); }
.date-head { font-size: .9rem; color: var(--muted); margin: 28px 0 10px; font-weight: 600; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px 18px; margin-bottom: 12px;
}
.card-top { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.badge {
  font-weight: 700; font-size: .8rem; padding: 2px 10px; border-radius: 6px;
  color: #fff; flex-shrink: 0;
}
.badge.S { background: var(--s); } .badge.A { background: var(--a); }
.badge.B { background: var(--b); color: #1a1d21; }
.badge.C { background: var(--c); } .badge.D { background: var(--d); }
.score { font-weight: 700; font-variant-numeric: tabular-nums; }
.title { font-size: 1.05rem; font-weight: 650; margin: 6px 0 4px; }
.summary { color: var(--text); font-size: .92rem; margin: 4px 0; }
.one-line { color: var(--muted); font-size: .88rem; font-style: normal; margin: 6px 0 2px; }
.meta { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; font-size: .84rem; }
.meta a { color: var(--link); text-decoration: none; }
.meta a:hover { text-decoration: underline; }
.meta .section { color: var(--muted); }
details { margin-top: 10px; }
summary { cursor: pointer; font-size: .84rem; color: var(--muted); user-select: none; }
.detail { font-size: .88rem; margin-top: 10px; }
.detail h4 { margin: 12px 0 4px; font-size: .84rem; color: var(--muted); }
.detail p { margin: 2px 0; }
.detail ul { margin: 4px 0; padding-left: 20px; }
.dims { width: 100%; border-collapse: collapse; margin-top: 6px; }
.dims th, .dims td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
.dims th { font-size: .8rem; color: var(--muted); font-weight: 600; }
.dims td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
.empty { text-align: center; color: var(--muted); padding: 60px 0; }
"""


def query_news(grade=None, date=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:  # 若尚未 init（table 不存在），回傳空列表而非噴錯
        sql = "SELECT * FROM news"
        conds, params = [], []
        if grade:
            conds.append("grade = ?")
            params.append(grade)
        if date:
            conds.append("news_date = ?")
            params.append(date)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY news_date DESC, total_score DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def grade_counts(date=None):
    conn = sqlite3.connect(DB_PATH)
    try:
        sql = "SELECT grade, COUNT(*) FROM news"
        params = []
        if date:
            sql += " WHERE news_date = ?"
            params.append(date)
        rows = conn.execute(sql + " GROUP BY grade", params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return dict(rows)


def date_counts():
    """回傳 [(news_date, count), ...]，日期新到舊。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT news_date, COUNT(*) FROM news"
            " WHERE news_date IS NOT NULL AND news_date != ''"
            " GROUP BY news_date ORDER BY news_date DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def render_card(r):
    title = escape(r["title"])
    if r["url"]:
        title_html = f'<a href="{escape(r["url"])}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">{title}</a>'
    else:
        title_html = title

    parts = [
        '<div class="card">',
        '<div class="card-top">',
        f'<span class="badge {escape(r["grade"])}">{escape(r["grade"])} 級｜{GRADE_LABELS.get(r["grade"], "")}</span>',
        f'<span class="score">{r["total_score"]} / 100</span>',
        "</div>",
        f'<div class="title">{title_html}</div>',
    ]
    if r["summary"]:
        parts.append(f'<div class="summary">{escape(r["summary"])}</div>')
    if r["one_line"]:
        parts.append(f'<div class="one-line">💡 {escape(r["one_line"])}</div>')

    meta = []
    if r["url"]:
        meta.append(f'<a href="{escape(r["url"])}" target="_blank" rel="noopener">原始新聞 ↗</a>')
    if r["section"]:
        meta.append(f'<span class="section">📂 {escape(r["section"])}</span>')
    if meta:
        parts.append(f'<div class="meta">{"".join(f"<span>{m}</span>" for m in meta)}</div>')

    # 詳細評分
    detail = ['<details><summary>評分細節</summary><div class="detail">']
    detail.append('<table class="dims"><tr><th>面向</th><th>分數</th><th>理由</th></tr>')
    for key, label, max_score in DIMENSIONS:
        score = r[f"{key}_score"]
        reason = r[f"{key}_reason"] or ""
        score_txt = f"{score} / {max_score}" if score is not None else "—"
        detail.append(
            f"<tr><td>{label}</td><td class='num'>{score_txt}</td><td>{escape(reason)}</td></tr>"
        )
    detail.append("</table>")

    if r["why_important"]:
        detail.append(f"<h4>為什麼重要</h4><p>{escape(r['why_important'])}</p>")
    if r["affected"]:
        detail.append(f"<h4>可能影響誰</h4><p>{escape(r['affected'])}</p>")
    if r["watch_next"]:
        detail.append("<h4>接下來觀察</h4>")
        try:
            items = json.loads(r["watch_next"])
            if isinstance(items, list):
                detail.append("<ul>" + "".join(f"<li>{escape(str(i))}</li>" for i in items) + "</ul>")
            else:
                detail.append(f"<p>{escape(str(items))}</p>")
        except (json.JSONDecodeError, TypeError):
            detail.append(f"<p>{escape(r['watch_next'])}</p>")
    detail.append("</div></details>")
    parts.extend(detail)
    parts.append("</div>")
    return "".join(parts)


def render_page(grade=None, date=None):
    rows = query_news(grade, date)
    counts = grade_counts(date)
    total = sum(counts.values())

    date_qs = f"&date={escape(date)}" if date else ""
    tabs = [f'<a href="/?{date_qs.lstrip("&")}" class="{"active" if not grade else ""}">全部 {total}</a>']
    for g in "SABCD":
        n = counts.get(g, 0)
        active = "active" if grade == g else ""
        tabs.append(f'<a href="/?grade={g}{date_qs}" class="{active}">{g} 級 {n}</a>')

    # 日期下拉選單（選了自動送出，並保留 grade）
    options = ['<option value="">全部日期</option>']
    for d, n in date_counts():
        selected = " selected" if d == date else ""
        options.append(f'<option value="{escape(d)}"{selected}>{escape(d)}（{n}）</option>')
    date_filter = [
        '<form class="date-filter" method="get" action="/">',
        f'<input type="hidden" name="grade" value="{escape(grade)}">' if grade else "",
        f'<select name="date" onchange="this.form.submit()">{"".join(options)}</select>',
        "<noscript><button type=\"submit\">篩選</button></noscript>",
        "</form>",
    ]

    body = []
    if not rows:
        body.append('<div class="empty">目前沒有新聞。用 <code>/news-importance-score</code> 評分後，執行 <code>python3 news.py add</code> 寫入。</div>')
    else:
        current_date = object()
        for r in rows:
            if r["news_date"] != current_date:
                current_date = r["news_date"]
                body.append(f'<div class="date-head">📅 {escape(current_date or "未標日期")}</div>')
            body.append(render_card(r))

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日新聞重要性評分</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<h1>📰 每日新聞重要性評分</h1>
<div class="sub">依 /news-importance-score 五面向評分（100 分制）整理的新聞資料庫</div>
<div class="filters"><div class="tabs">{"".join(tabs)}</div>{"".join(date_filter)}</div>
{"".join(body)}
</div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self.send_error(404)
            return
        qs = parse_qs(parsed.query)
        grade = qs.get("grade", [None])[0]
        if grade:
            grade = grade.upper()
            if grade not in "SABCD":
                grade = None
        date = qs.get("date", [None])[0]
        if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = None
        html = render_page(grade, date)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # 安靜模式


def run(port=8765):
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"新聞評分網頁介面：http://127.0.0.1:{port}（Ctrl+C 結束）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    run()
