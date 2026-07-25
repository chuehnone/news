#!/usr/bin/env python3
"""新聞重要性評分的網頁介面（標準庫實作，無外部依賴）。

啟動：python3 news.py serve [--port 8765]
"""

import json
import re
import sqlite3
from datetime import datetime
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

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
  /* badge 底色：低飽和色底 + 深色字，避免實心飽和色搶過標題 */
  --s-bg: #fee2e2; --a-bg: #ffedd5; --b-bg: #fef3c7; --c-bg: #f3f4f6; --d-bg: #f3f4f6;
  --s-fg: #991b1b; --a-fg: #9a3412; --b-fg: #854d0e; --c-fg: #4b5563; --d-fg: #6b7280;
  /* one_line 細線色：淺色模式下與等級色同階即可 */
  --s-line: #dc2626; --a-line: #ea580c; --b-line: #ca8a04; --c-line: #9ca3af; --d-line: #b6bcc4;
  --sticky-bg: rgba(246, 247, 249, .92);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1b1f24; --text: #e6e8ea; --muted: #9aa3ad;
    --border: #2c3238; --link: #7ab0ff;
    /* B 級佔六成資料量，深色模式下把亮黃壓暗，避免整頁最常見元素最刺眼 */
    --s: #f87171; --a: #fb923c; --b: #d4a017; --c: #9ca3af; --d: #6b7280;
    --s-bg: #3f1d1d; --a-bg: #3d2411; --b-bg: #3a2f10; --c-bg: #262b31; --d-bg: #262b31;
    --s-fg: #fca5a5; --a-fg: #fdba74; --b-fg: #e3c26b; --c-fg: #c2c8cf; --d-fg: #9aa3ad;
    /* 暗背景上 2px 細線容易糊掉，故比等級色再亮一階 */
    --s-line: #fca5a5; --a-line: #fdba74; --b-line: #e8c877; --c-line: #b6bdc5; --d-line: #8a939c;
    --sticky-bg: rgba(17, 20, 24, .92);
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
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 10px; }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; }
.date-filter select, .section-filter select, .search input {
  padding: 5px 10px; border-radius: 999px; border: 1px solid var(--border);
  background: var(--card); color: var(--text); font-size: .85rem;
  font-family: inherit; cursor: pointer;
}
.search { flex: 1 1 180px; min-width: 160px; }
.search input { width: 100%; cursor: text; }
.tabs a {
  padding: 5px 14px; border-radius: 999px; border: 1px solid var(--border);
  text-decoration: none; color: var(--text); font-size: .85rem; background: var(--card);
}
.tabs a.active { background: var(--text); color: var(--bg); border-color: var(--text); }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 24px; }
.density { display: flex; gap: 0; border: 1px solid var(--border); border-radius: 999px; overflow: hidden; }
.density button {
  padding: 4px 12px; border: 0; background: var(--card); color: var(--muted);
  font-size: .8rem; font-family: inherit; cursor: pointer;
}
.density button.active { background: var(--text); color: var(--bg); }
.date-head {
  font-size: 1rem; color: var(--text); margin: 28px 0 10px; font-weight: 700;
  position: sticky; top: 0; z-index: 5;
  background: var(--sticky-bg); backdrop-filter: blur(6px);
  padding: 6px 0; border-bottom: 1px solid var(--border);
}
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px 18px; margin-bottom: 12px;
  border-left: 4px solid var(--grade-color, var(--border));
}
/* 等級決定卡片視覺重量：S/A 加重，C/D 降權。
   --grade-color 給左側色條，--accent 是亮一階的版本，供暗背景上的細線用 */
.card.S { --grade-color: var(--s); --accent: var(--s-line); box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.card.A { --grade-color: var(--a); --accent: var(--a-line); }
.card.B { --grade-color: var(--b); --accent: var(--b-line); }
.card.C { --grade-color: var(--c); --accent: var(--c-line); opacity: .78; padding: 12px 16px; }
.card.D { --grade-color: var(--d); --accent: var(--d-line); opacity: .62; padding: 12px 16px; }
.card.C .title, .card.D .title { font-size: 1.1rem; }
.card.C .summary, .card.D .summary { display: none; }
.card.C .score, .card.D .score { font-size: .95rem; color: var(--muted); }
.card-top { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.badge {
  font-weight: 700; font-size: .78rem; padding: 2px 9px; border-radius: 6px;
  flex-shrink: 0; letter-spacing: .02em;
}
.badge.S { background: var(--s-bg); color: var(--s-fg); }
.badge.A { background: var(--a-bg); color: var(--a-fg); }
.badge.B { background: var(--b-bg); color: var(--b-fg); }
.badge.C { background: var(--c-bg); color: var(--c-fg); }
.badge.D { background: var(--d-bg); color: var(--d-fg); }
.grade-label { color: var(--muted); font-size: .8rem; }
/* 分數是 100 檔的細粒度資訊（等級只有 5 檔），權重要高於 badge 與 label */
.score {
  font-weight: 700; font-variant-numeric: tabular-nums;
  color: var(--text); font-size: 1.05rem; margin-left: auto;
}
.title { font-size: 1.32rem; font-weight: 680; line-height: 1.4; margin: 8px 0 6px; }
/* one_line 是判斷而非複述，升格為卡片主角 */
.one-line {
  font-size: .95rem; color: var(--text); margin: 8px 0;
  /* padding 要夠寬，折行的第二行才不會看起來懸空在細線旁 */
  padding-left: 14px; border-left: 3px solid var(--accent, var(--border));
}
.summary { color: var(--muted); font-size: .88rem; margin: 6px 0; }
.meta { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 10px; font-size: .84rem; }
.meta a { color: var(--link); text-decoration: none; }
.meta a:hover { text-decoration: underline; }
.meta .section { color: var(--muted); }
.meta button.section {
  border: 0; background: none; padding: 0; font: inherit; font-size: .84rem;
  color: var(--muted); cursor: pointer; text-decoration: none;
}
.meta button.section:hover { text-decoration: underline; }
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
.footer { text-align: center; color: var(--muted); font-size: .8rem; margin-top: 32px; }
/* 精簡模式：一則一行，只留等級與標題 */
body.compact .summary, body.compact .one-line,
body.compact .meta, body.compact details { display: none; }
body.compact .card { padding: 8px 14px; margin-bottom: 6px; }
body.compact .title { font-size: .98rem; margin: 2px 0; }
body.compact .card.C, body.compact .card.D { opacity: .7; }
"""


def query_news(grade=None, date=None, section=None, q=None):
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
        if section:
            conds.append("section = ?")
            params.append(section)
        if q:
            conds.append(
                "(LOWER(title) LIKE ? OR LOWER(COALESCE(one_line,'')) LIKE ?"
                " OR LOWER(COALESCE(summary,'')) LIKE ?)"
            )
            params += [f"%{q.lower()}%"] * 3
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY news_date DESC, total_score DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def grade_counts(date=None, section=None, q=None):
    """等級以外的條件下各級筆數（與前端 apply() 的 perGrade 行為一致）。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        sql = "SELECT grade, COUNT(*) FROM news"
        conds, params = [], []
        if date:
            conds.append("news_date = ?")
            params.append(date)
        if section:
            conds.append("section = ?")
            params.append(section)
        if q:
            conds.append(
                "(LOWER(title) LIKE ? OR LOWER(COALESCE(one_line,'')) LIKE ?"
                " OR LOWER(COALESCE(summary,'')) LIKE ?)"
            )
            params += [f"%{q.lower()}%"] * 3
        if conds:
            sql += " WHERE " + " AND ".join(conds)
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


def section_counts():
    """回傳 [(section, count), ...]，筆數多的在前。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT section, COUNT(*) c FROM news"
            " WHERE section IS NOT NULL AND section != ''"
            " GROUP BY section ORDER BY c DESC"
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

    grade = escape(r["grade"])
    section = r["section"] or ""
    # 搜尋比對用的純文字（標題 + 一句話判斷 + 摘要），小寫化交給前端
    haystack = " ".join(filter(None, [r["title"], r["one_line"], r["summary"], section]))

    parts = [
        # data-* 供靜態站的前端篩選使用（動態 server 端不需要，但無害）
        f'<div class="card {grade}" data-grade="{grade}"'
        f' data-date="{escape(r["news_date"] or "")}"'
        f' data-section="{escape(section)}"'
        f' data-text="{escape(haystack.lower())}">',
        '<div class="card-top">',
        f'<span class="badge {grade}">{grade}</span>',
        f'<span class="grade-label">{GRADE_LABELS.get(r["grade"], "")}</span>',
        f'<span class="score">{r["total_score"]}</span>',
        "</div>",
        f'<div class="title">{title_html}</div>',
    ]
    # one_line 是判斷、summary 是複述，故一句話判斷排在摘要之前
    if r["one_line"]:
        parts.append(f'<div class="one-line">{escape(r["one_line"])}</div>')
    if r["summary"]:
        parts.append(f'<div class="summary">{escape(r["summary"])}</div>')

    meta = []
    if r["url"]:
        meta.append(f'<a href="{escape(r["url"])}" target="_blank" rel="noopener">原始新聞 ↗</a>')
    if section:
        # 靜態站可點擊篩選；動態站沒有對應 handler，退化為純文字
        meta.append(
            f'<button type="button" class="section" data-section-pick="{escape(section)}">'
            f"📂 {escape(section)}</button>"
        )
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


def render_page(grade=None, date=None, section=None, q=None):
    rows = query_news(grade, date, section, q)
    counts = grade_counts(date, section, q)
    total = sum(counts.values())

    keep = ""
    if date:
        keep += f"&date={quote(date)}"
    if section:
        keep += f"&section={quote(section)}"
    if q:
        keep += f"&q={quote(q)}"
    tabs = [f'<a href="/?{keep.lstrip("&")}" class="{"active" if not grade else ""}">全部 {total}</a>']
    for g in "SABCD":
        n = counts.get(g, 0)
        active = "active" if grade == g else ""
        tabs.append(f'<a href="/?grade={g}{keep}" class="{active}">{g} 級 {n}</a>')

    # 篩選表單：任一欄位變動就送出，並以 hidden 欄位保留 grade
    options = ['<option value="">全部日期</option>']
    for d, n in date_counts():
        selected = " selected" if d == date else ""
        options.append(f'<option value="{escape(d)}"{selected}>{escape(d)}（{n}）</option>')
    sec_options = ['<option value="">全部分類</option>']
    for s, n in section_counts():
        selected = " selected" if s == section else ""
        sec_options.append(f'<option value="{escape(s)}"{selected}>{escape(s)}（{n}）</option>')
    controls = [
        '<form class="toolbar" method="get" action="/">',
        f'<input type="hidden" name="grade" value="{escape(grade)}">' if grade else "",
        f'<div class="date-filter"><select name="date" onchange="this.form.submit()">{"".join(options)}</select></div>',
        f'<div class="section-filter"><select name="section" onchange="this.form.submit()">{"".join(sec_options)}</select></div>',
        '<div class="search">'
        f'<input name="q" type="search" value="{escape(q or "")}"'
        ' placeholder="搜尋標題／判斷／摘要…" autocomplete="off"></div>',
        '<button type="submit" style="display:none">篩選</button>',
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
<div class="filters"><div class="tabs">{"".join(tabs)}</div></div>
{"".join(controls)}
{"".join(body)}
</div>
</body>
</html>"""


# 靜態站的前端篩選：伺服器端的 grade/date 篩選靠 query string，
# 靜態主機沒有 server 可以處理，故改為一次輸出全部卡片、用 JS 切換顯示。
FILTER_JS = """
(function () {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var heads = Array.prototype.slice.call(document.querySelectorAll('.date-head'));
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs a'));
  var dateSel = document.getElementById('date-select');
  var sectionSel = document.getElementById('section-select');
  var searchBox = document.getElementById('search-box');
  var empty = document.getElementById('empty');
  var densityBtns = Array.prototype.slice.call(document.querySelectorAll('.density button'));
  var state = { grade: '', date: '', section: '', q: '', density: '' };

  // 篩選狀態存進 hash，讓靜態站的篩選結果可分享、可用上一頁還原
  function readHash() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h) return;
    h.split('&').forEach(function (kv) {
      var i = kv.indexOf('=');
      if (i < 0) return;
      var k = kv.slice(0, i), v = decodeURIComponent(kv.slice(i + 1));
      if (k in state) state[k] = v;
    });
  }

  function writeHash() {
    var parts = [];
    Object.keys(state).forEach(function (k) {
      if (state[k]) parts.push(k + '=' + encodeURIComponent(state[k]));
    });
    var h = parts.join('&');
    // replaceState 避免每次打字都塞一筆歷史紀錄
    history.replaceState(null, '', h ? '#' + h : location.pathname);
  }

  function syncControls() {
    if (dateSel) dateSel.value = state.date;
    if (sectionSel) sectionSel.value = state.section;
    if (searchBox) searchBox.value = state.q;
    document.body.classList.toggle('compact', state.density === 'compact');
    densityBtns.forEach(function (b) {
      b.classList.toggle('active', (b.dataset.density || '') === state.density);
    });
  }

  function apply() {
    var q = state.q.trim().toLowerCase();
    var shown = 0;
    var perGrade = {};
    cards.forEach(function (c) {
      var g = c.dataset.grade;
      // 等級以外的條件先算，才能讓分頁計數反映「其他條件下各級有幾筆」
      var base = (!state.date || c.dataset.date === state.date)
        && (!state.section || c.dataset.section === state.section)
        && (!q || (c.dataset.text || '').indexOf(q) !== -1);
      if (base) perGrade[g] = (perGrade[g] || 0) + 1;
      var ok = base && (!state.grade || g === state.grade);
      c.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    // 日期標題只在其底下還有可見卡片時顯示
    heads.forEach(function (h) {
      var any = cards.some(function (c) {
        return c.dataset.date === h.dataset.date && c.style.display !== 'none';
      });
      h.style.display = any ? '' : 'none';
    });
    // 分頁計數隨其他篩選連動（與 server 端的 grade_counts(date) 行為一致）
    var total = 0;
    Object.keys(perGrade).forEach(function (k) { total += perGrade[k]; });
    tabs.forEach(function (t) {
      var g = t.dataset.grade;
      var n = g ? (perGrade[g] || 0) : total;
      t.textContent = (g ? g + ' 級 ' : '全部 ') + n;
      t.classList.toggle('active', g === state.grade);
    });
    empty.style.display = shown ? 'none' : '';
  }

  function update() { syncControls(); apply(); writeHash(); }

  tabs.forEach(function (t) {
    t.addEventListener('click', function (e) {
      e.preventDefault();
      state.grade = t.dataset.grade || '';
      update();
    });
  });
  if (dateSel) dateSel.addEventListener('change', function () {
    state.date = dateSel.value; update();
  });
  if (sectionSel) sectionSel.addEventListener('change', function () {
    state.section = sectionSel.value; update();
  });
  if (searchBox) searchBox.addEventListener('input', function () {
    state.q = searchBox.value; update();
  });
  densityBtns.forEach(function (b) {
    b.addEventListener('click', function () {
      state.density = b.dataset.density || ''; update();
    });
  });
  // 卡片上的分類可直接點成篩選條件
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-section-pick]');
    if (!btn) return;
    var v = btn.dataset.sectionPick;
    state.section = (state.section === v) ? '' : v;
    update();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  window.addEventListener('hashchange', function () {
    readHash(); syncControls(); apply();
  });

  readHash();
  update();
})();
"""


def render_static_page():
    """輸出含全部卡片的單一頁面，篩選交給前端 JS。"""
    rows = query_news()
    counts = grade_counts()
    total = sum(counts.values())

    tabs = ['<a href="#" data-grade="" class="active">全部 %d</a>' % total]
    for g in "SABCD":
        tabs.append(f'<a href="#" data-grade="{g}">{g} 級 {counts.get(g, 0)}</a>')

    options = ['<option value="">全部日期</option>']
    for d, n in date_counts():
        options.append(f'<option value="{escape(d)}">{escape(d)}（{n}）</option>')
    sec_options = ['<option value="">全部分類</option>']
    for s, n in section_counts():
        sec_options.append(f'<option value="{escape(s)}">{escape(s)}（{n}）</option>')
    controls = (
        '<div class="toolbar">'
        f'<div class="date-filter"><select id="date-select">{"".join(options)}</select></div>'
        f'<div class="section-filter"><select id="section-select">{"".join(sec_options)}</select></div>'
        '<div class="search">'
        '<input id="search-box" type="search" placeholder="搜尋標題／判斷／摘要…" autocomplete="off">'
        "</div>"
        '<div class="density">'
        '<button type="button" data-density="" class="active">完整</button>'
        '<button type="button" data-density="compact">精簡</button>'
        "</div>"
        "</div>"
    )

    body = []
    current_date = object()
    for r in rows:
        if r["news_date"] != current_date:
            current_date = r["news_date"]
            body.append(
                f'<div class="date-head" data-date="{escape(current_date or "")}">'
                f'📅 {escape(current_date or "未標日期")}</div>'
            )
        body.append(render_card(r))

    empty_msg = "目前沒有符合條件的新聞。" if rows else "目前沒有新聞。"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

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
<div class="sub">依 /news-importance-score 五面向評分（100 分制）整理的新聞資料庫｜更新於 {generated}</div>
<div class="filters"><div class="tabs">{"".join(tabs)}</div></div>
{controls}
{"".join(body)}
<div class="empty" id="empty" style="display:none">{empty_msg}</div>
<div class="footer">共 {total} 則評分紀錄</div>
</div>
<script>{FILTER_JS}</script>
</body>
</html>"""


def verify_html(html, expected):
    """檢查產出的頁面確實含有 expected 張卡片。

    export 最清楚自己生了什麼，故由這裡把關而非讓 CI 比對 HTML 字串——
    CI 的比對條件會跟 markup 脫鉤（曾因卡片改成 class="card S" 而誤判成 0）。
    回傳實際卡片數，不符則 raise。
    """
    found = len(re.findall(r'<div class="card[^"]*" data-grade=', html))
    if found != expected:
        raise SystemExit(
            f"產出異常：頁面有 {found} 張卡片，但資料庫有 {expected} 筆。"
            "（import 失敗或 render_card 壞掉時會出現）"
        )
    return found


def export_static(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    html = render_static_page()
    n = verify_html(html, len(query_news()))
    index.write_text(html, encoding="utf-8")
    kb = len(html.encode("utf-8")) / 1024
    print(f"已輸出靜態網站到 {index}（{kb:.0f} KB、{n} 張卡片）")


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
        section = qs.get("section", [None])[0] or None
        q = qs.get("q", [None])[0] or None
        html = render_page(grade, date, section, q)
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
