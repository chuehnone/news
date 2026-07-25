#!/usr/bin/env python3
"""新聞重要性評分資料庫 CLI。

用法：
    python3 news.py init                # 建立資料庫
    python3 news.py add <file.json>    # 新增一筆評分結果（也可從 stdin 讀入：python3 news.py add -）
    python3 news.py list [--grade S]   # 快速列出資料庫內容
    python3 news.py serve [--port 8765]  # 啟動網頁介面
    python3 news.py fetch [--feeds feeds.txt]  # 抓取 RSS，把新連結存入待評分清單
    python3 news.py pending [--all] [--json] [--limit N]  # 列出待評分清單
    python3 news.py skip <id...>       # 把待評分項目標為略過

add 接受的 JSON 格式與驗證規則：python3 news.py schema
（格式由本檔的 DIMENSIONS / SECTIONS 生成，不另外手抄一份以免漂移）
"""

import argparse
import json
import sqlite3
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
# date 取別名：cmd_digest 有個叫 date 的區域變數，直接 import date 容易誤用
from datetime import datetime, date as date_cls
from email.utils import parsedate_to_datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "news.db"
FEEDS_PATH = Path(__file__).parent / "feeds.txt"
DATA_JSON_PATH = Path(__file__).parent / "data" / "news.json"

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

# 評分結果可填的 section。digest 依這個順序分節輸出，
# 「不建議放入每日摘要」是有效值但不進 digest。
SECTIONS = [
    "今日最重要",
    "影響未來的趨勢",
    "跟生活決策有關",
    "被忽略但重要",
    "熱但未必重要",
    "不建議放入每日摘要",
]

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

CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source TEXT,
    published TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    fetched_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending(status);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# RSS 連結常帶追蹤參數（BBC 的 at_medium、Google News 的 oc 等），
# 會讓 add 標記 pending、去重比對時對不上乾淨網址，一律先剝掉。
TRACKING_PREFIXES = ("utm_", "at_")
TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "oc", "cmpid", "spm"}

# 標題含這些詞的項目在 fetch 時直接標為 low（低優先），不進預設待評分清單。
# 只放高置信度的雜訊詞（每日盤勢、天氣短訊、體育賽果、運勢彩券），
# 寧可漏擋交給批次粗篩，也不要誤殺重要新聞。
LOWPRIO_KEYWORDS = [
    "盤前", "盤中", "收盤", "開盤", "早盤", "台指期", "台股盤",
    "目標價", "焦點股",
    "大雷雨", "豪雨特報", "天氣預報", "今日天氣",
    "世界盃", "金靴", "英超", "中職", "日職", "MLB", "NBA",
    "星座", "運勢", "統一發票", "威力彩", "大樂透", "今彩", "開獎", "頭獎",
]


def is_low_priority(title):
    return any(kw in title for kw in LOWPRIO_KEYWORDS)


def normalize_title(title):
    """去除空白與標點後的標題，用於跨來源重複比對。

    同一則新聞在不同來源常只差全半形標點與空格（中央社用全形頓號、
    科技新報用半形逗號加空格），正規化後全等即視為重複。
    """
    return "".join(
        ch for ch in title if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def normalize_url(url):
    if not url:
        return url
    parts = urllib.parse.urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urllib.parse.urlencode(query), "")
    )


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

    # news_date 會被靜態站的保留期以「字串字面」比較（'2026-7-5' 會大於
    # '2026-06-25'），未補零或其他格式會被歸到錯誤的保留層級，故在入口擋掉。
    news_date = data.get("news_date")
    if news_date:
        try:
            parsed = datetime.strptime(news_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            sys.exit(f"錯誤：news_date「{news_date}」格式不正確，須為 YYYY-MM-DD（月日要補零）")
        # strptime 接受未補零的 '2026-7-5'，但那樣存進 db 會讓字面比較出錯，
        # 故要求與正規化後的字串完全相同。
        if parsed.isoformat() != news_date:
            sys.exit(
                f"錯誤：news_date「{news_date}」須補零寫成 {parsed.isoformat()}"
            )
        if parsed > date_cls.today():
            sys.exit(f"錯誤：news_date「{news_date}」是未來日期，請確認是否誤植")

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
    url = normalize_url(data.get("url"))
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
    if url:
        conn.execute("UPDATE pending SET status = 'scored' WHERE url = ?", (url,))
    # 轉址型連結（如 Google News）對不上原始網址，改用標題比對：
    # pending 標題與評分標題相同，或僅多出「 - 媒體名」後綴，視為同一則
    title = data["title"]
    conn.execute(
        "UPDATE pending SET status = 'scored' WHERE status IN ('new', 'low', 'dup') AND (title = ? OR title LIKE ?)",
        (title, title + " - %"),
    )
    conn.commit()
    print(f"已新增 id={cur.lastrowid}：[{grade} 級 {total} 分] {data['title']}")
    conn.close()

    # 順手同步 data/news.json，否則 db 更新了但進版控的資料沒動，靜態站不會變。
    # 批次評分時每筆都重寫整份 JSON 是浪費，用 --no-export 跳過，最後再手動
    # 跑一次 export-json 即可。
    if not args.no_export:
        export_news_json(DATA_JSON_PATH)


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


def normalize_pub_date(raw):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return raw[:10]


def parse_feed(xml_bytes):
    """解析 RSS 2.0 或 Atom，回傳 [(title, link, published), ...]。"""
    # 防 XXE / billion-laughs：合法 feed 不需要 DTD，直接拒絕
    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        raise ValueError("feed 含 DTD/ENTITY 宣告，拒絕解析")
    root = ET.fromstring(xml_bytes)
    items = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = pub = None
        for child in el:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "title":
                title = "".join(child.itertext()).strip()
            elif ctag == "link" and not link:
                # RSS 的 link 是文字內容；Atom 的 link 是 href 屬性（只取 alternate）
                if child.get("rel") in (None, "alternate"):
                    link = (child.text or "").strip() or child.get("href")
            elif ctag in ("pubDate", "published", "updated"):
                pub = pub or (child.text or "").strip()
        if title and link:
            items.append((title, link, normalize_pub_date(pub)))
    return items


def read_feeds(path):
    """feeds.txt 每行一個 feed：「來源名稱 網址」，# 開頭為註解。"""
    feeds = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        name, url = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], parts[0])
        feeds.append((name, url))
    return feeds


def cmd_fetch(args):
    feeds_path = Path(args.feeds)
    if not feeds_path.exists():
        sys.exit(f"錯誤：找不到 feed 清單 {feeds_path}，請先建立（每行「來源名稱 網址」）")
    feeds = read_feeds(feeds_path)
    if not feeds:
        sys.exit(f"錯誤：{feeds_path} 內沒有任何 feed")

    conn = connect()
    seen_titles = {
        normalize_title(r[0])
        for table in ("news", "pending")
        for r in conn.execute(f"SELECT title FROM {table}").fetchall()
    }
    total_new = 0
    for name, feed_url in feeds:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0 (news-fetch)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                items = parse_feed(resp.read())
        except Exception as e:
            print(f"[{name}] 抓取失敗：{e}")
            continue
        added = lowprio = dup = 0
        for title, link, pub in items[: args.limit]:
            link = normalize_url(link)
            if conn.execute("SELECT 1 FROM news WHERE url = ?", (link,)).fetchone():
                continue
            norm = normalize_title(title)
            if norm in seen_titles:
                status = "dup"
            elif is_low_priority(title):
                status = "low"
            else:
                status = "new"
            cur = conn.execute(
                "INSERT OR IGNORE INTO pending (title, url, source, published, status) VALUES (?, ?, ?, ?, ?)",
                (title, link, name, pub, status),
            )
            if not cur.rowcount:
                continue
            seen_titles.add(norm)
            if status == "dup":
                dup += 1
            elif status == "low":
                lowprio += 1
            else:
                added += 1
        conn.commit()
        total_new += added
        notes = "".join(
            f"，{label} {n} 則" for label, n in (("預過濾", lowprio), ("重複", dup)) if n
        )
        print(f"[{name}] 取得 {len(items)} 則，新增 {added} 則{notes}")

    remaining = conn.execute("SELECT COUNT(*) FROM pending WHERE status = 'new'").fetchone()[0]
    print(f"完成：本次新增 {total_new} 則，待評分共 {remaining} 則（python3 news.py pending 檢視）")
    conn.close()


def cmd_pending(args):
    conn = connect()
    sql = "SELECT id, source, published, status, title, url FROM pending"
    if not args.all:
        sql += " WHERE status = 'new'"
    sql += " ORDER BY published DESC, id DESC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        conn.close()
        return
    if not rows:
        print("（待評分清單是空的，先跑 python3 news.py fetch）")
        return
    for r in rows:
        mark = "" if r["status"] == "new" else f" [{r['status']}]"
        print(f"{r['id']:>4}  {r['published'] or '----------'}  {r['source'] or '?'}{mark}  {r['title']}")
        print(f"      {r['url']}")
    conn.close()


# digest 不收「不建議放入每日摘要」，其餘沿用 SECTIONS 的順序
SECTION_ORDER = [s for s in SECTIONS if s != "不建議放入每日摘要"]


def cmd_digest(args):
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    conn = connect()
    rows = conn.execute(
        # NULL != '...' 在 SQL 裡是 NULL 而非 true，直接寫 != 會把未分類的整批漏掉
        "SELECT * FROM news WHERE news_date = ? "
        "AND (section IS NULL OR section != '不建議放入每日摘要') "
        "ORDER BY total_score DESC",
        (date,),
    ).fetchall()
    conn.close()
    if not rows:
        print(f"（{date} 沒有已評分的新聞，先跑批次評分）")
        return

    by_section = {}
    for r in rows:
        by_section.setdefault(r["section"] if r["section"] in SECTION_ORDER else "其他", []).append(r)

    lines = [f"# 每日新聞摘要 {date}", ""]
    for section in SECTION_ORDER + ["其他"]:
        items = by_section.get(section)
        if not items:
            continue
        lines += [f"## {section}", ""]
        for r in items:
            tag = f"[{r['grade']} {r['total_score']}]"
            link = f"[{r['title']}]({r['url']})" if r["url"] else r["title"]
            if r["grade"] in ("S", "A"):
                lines += [f"### {tag} {link}", ""]
                if r["one_line"]:
                    lines += [f"**{r['one_line']}**", ""]
                if r["why_important"]:
                    lines += [r["why_important"], ""]
            elif r["grade"] == "B":
                one = f" — {r['one_line']}" if r["one_line"] else ""
                lines.append(f"- **{tag}** {link}{one}")
            else:
                lines.append(f"- {tag} {link}")
        if lines[-1] != "":
            lines.append("")
    print("\n".join(lines).rstrip())


# news.db 不進版控（二進位檔每次 commit 都是整檔快照，repo 會無上限膨脹）。
# 改為匯出 JSON：git 能 diff、壓縮率高，CI 端再用 import-json 重建 db。
#
# 刻意不含 created_at：repo 是 public，而逐筆的評分時間會洩漏作業時段等
# 行為 metadata，對網站顯示又毫無用途（頁面只用 news_date）。import-json
# 匯入時該欄位會套用 schema 預設值（匯入當下時間），不影響任何功能。
NEWS_COLUMNS = [
    "title", "url", "summary", "news_date", "total_score", "grade", "section",
    "one_line", "why_important", "affected", "watch_next",
    "scope_score", "scope_reason", "duration_score", "duration_reason",
    "decision_score", "decision_reason", "structural_score", "structural_reason",
    "credibility_score", "credibility_reason",
]


def export_news_json(out):
    conn = connect()
    rows = conn.execute(
        f"SELECT {', '.join(NEWS_COLUMNS)} FROM news ORDER BY news_date, id"
    ).fetchall()
    conn.close()
    items = [{k: r[k] for k in NEWS_COLUMNS} for r in rows]
    text = json.dumps(items, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"已匯出 {len(items)} 筆到 {out}")


def cmd_export_json(args):
    export_news_json(args.out)


def cmd_import_json(args):
    src = Path(args.file)
    items = json.loads(src.read_text(encoding="utf-8"))
    conn = connect()
    if args.replace:
        conn.execute("DELETE FROM news")
    cols = ", ".join(NEWS_COLUMNS)
    marks = ", ".join("?" * len(NEWS_COLUMNS))
    added = 0
    # JSON 是重建 db 的真實來源，不在此做 url 去重：news 表允許同一 url 有多筆
    # （例如同篇文章重新評分過），去重是 add 指令的責任，import 只負責忠實還原。
    # 需要覆蓋既有資料時用 --replace，否則重跑會疊加。
    for it in items:
        conn.execute(
            f"INSERT INTO news ({cols}) VALUES ({marks})",
            [it.get(k) for k in NEWS_COLUMNS],
        )
        added += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    conn.close()
    print(f"已匯入 {added} 筆（來源 {len(items)} 筆），news 表現有 {total} 筆")


def cmd_schema(_args):
    """輸出 add 接受的 JSON 格式。

    這是格式的唯一出處：欄位上限、分級門檻、section 選項全部由本檔的常數生成，
    所以不會像手抄一份說明那樣跟實作漂移（CLAUDE.md 與 skill 都指向這裡）。
    """
    dims = ",\n".join(
        f'    "{k}":{" " * (12 - len(k))}{{"score": 0, "reason": "{label}（0-{mx}）理由"}}'
        for k, label, mx in DIMENSIONS
    )
    thresholds = " / ".join(
        f"{lo}+ {g}" for g, lo in [("S", 85), ("A", 70), ("B", 55), ("C", 40)]
    )
    print(f"""add 接受的 JSON 格式（/news-importance-score 的評分結果）：

{{
  "title": "新聞標題（必填）",
  "url": "原始新聞連結",
  "summary": "新聞摘要（2-3 句）",
  "news_date": "YYYY-MM-DD（新聞事件發生日，非評分日）",
  "section": "{" / ".join(SECTIONS)}",
  "one_line": "一句話判斷",
  "why_important": "為什麼重要",
  "affected": "可能影響誰",
  "watch_next": ["觀察指標 1", "觀察指標 2", "觀察指標 3"],
  "dimensions": {{
{dims}
  }}
}}

規則：
- total_score 與 grade 不用填，由 dimensions 加總並判定等級（{thresholds} / 其餘 D）。
- 各面向分數不得超過上限，超出會拒絕寫入。
- news_date 必須是補零的 YYYY-MM-DD（2026-7-5 會被擋），不接受不存在的日期
  與未來日期；可留空表示日期不明。
- 相同 url 預設拒絕重複寫入（--force 可覆寫）。""")


def cmd_export(args):
    from server import export_static

    export_static(Path(args.out), retention=args.retention)


def cmd_prune(args):
    conn = connect()
    cur = conn.execute(
        "DELETE FROM pending WHERE status != 'new' AND fetched_at < datetime('now', 'localtime', ?)",
        (f"-{int(args.days)} days",),
    )
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    print(f"已清除 {cur.rowcount} 筆 {args.days} 天前的已處理項目，pending 表剩 {remaining} 筆")
    conn.close()


def cmd_skip(args):
    conn = connect()
    for pid in args.ids:
        cur = conn.execute("UPDATE pending SET status = 'skipped' WHERE id = ? AND status IN ('new', 'low', 'dup')", (pid,))
        print(f"id={pid}：{'已略過' if cur.rowcount else '找不到或已處理'}")
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="新聞重要性評分資料庫")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="建立資料庫")

    p_add = sub.add_parser("add", help="新增一筆評分結果（JSON 檔或 - 表示 stdin）")
    p_add.add_argument("file")
    p_add.add_argument("--force", action="store_true", help="允許重複連結")
    p_add.add_argument(
        "--no-export", action="store_true",
        help="不要順手更新 data/news.json（批次評分時用，最後再跑一次 export-json）",
    )

    p_list = sub.add_parser("list", help="列出新聞")
    p_list.add_argument("--grade", help="只列出指定等級（S/A/B/C/D）")

    p_serve = sub.add_parser("serve", help="啟動網頁介面")
    p_serve.add_argument("--port", type=int, default=8765)

    p_fetch = sub.add_parser("fetch", help="抓取 RSS，把新連結存入待評分清單")
    p_fetch.add_argument("--feeds", default=str(FEEDS_PATH), help="feed 清單檔（預設 feeds.txt）")
    p_fetch.add_argument("--limit", type=int, default=40, help="每個 feed 最多取幾則（預設 40）")

    p_pending = sub.add_parser("pending", help="列出待評分清單")
    p_pending.add_argument("--all", action="store_true", help="包含已評分的項目")
    p_pending.add_argument("--json", action="store_true", help="以 JSON 輸出（給批次評分用）")
    p_pending.add_argument("--limit", type=int, help="最多列出幾則")

    p_skip = sub.add_parser("skip", help="把待評分項目標為略過")
    p_skip.add_argument("ids", nargs="+", type=int)

    p_digest = sub.add_parser("digest", help="輸出指定日期的每日摘要（markdown）")
    p_digest.add_argument("--date", help="YYYY-MM-DD，預設今天")

    p_prune = sub.add_parser("prune", help="清除 pending 中過期的已處理項目")
    p_prune.add_argument("--days", type=int, default=30, help="保留最近幾天（預設 30）")

    p_ejson = sub.add_parser("export-json", help="把 news 表匯出成 JSON（進版控用）")
    p_ejson.add_argument("--out", default=str(DATA_JSON_PATH), help="輸出路徑（預設 data/news.json）")

    p_ijson = sub.add_parser("import-json", help="從 JSON 重建 news 表（CI 用）")
    p_ijson.add_argument("file", nargs="?", default=str(DATA_JSON_PATH))
    p_ijson.add_argument("--replace", action="store_true", help="先清空 news 表再匯入")

    sub.add_parser("schema", help="輸出 add 接受的 JSON 格式與規則")

    p_export = sub.add_parser("export", help="輸出靜態網站（GitHub Pages 用）")
    p_export.add_argument("--out", default="dist", help="輸出目錄（預設 dist）")
    p_export.add_argument(
        "--retention",
        action="store_true",
        help="套用保留期分層（近 30 天全部、30-90 天限 S/A、90 天以上不輸出）；CI 用",
    )

    args = parser.parse_args()
    {
        "init": cmd_init,
        "add": cmd_add,
        "list": cmd_list,
        "serve": cmd_serve,
        "fetch": cmd_fetch,
        "pending": cmd_pending,
        "skip": cmd_skip,
        "digest": cmd_digest,
        "prune": cmd_prune,
        "export-json": cmd_export_json,
        "import-json": cmd_import_json,
        "schema": cmd_schema,
        "export": cmd_export,
    }[args.command](args)


if __name__ == "__main__":
    main()
