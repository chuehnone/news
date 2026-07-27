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
    python3 news.py tags [標籤]        # 列出所有標籤／某標籤底下的新聞
    python3 news.py tag <id> <標籤...>  # 修改某則新聞的標籤
    python3 news.py alias [別名 正規名] # 管理標籤別名（輝達 → NVIDIA）

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
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "news.db"
FEEDS_PATH = Path(__file__).parent / "feeds.txt"
DATA_JSON_PATH = Path(__file__).parent / "data" / "news.json"

# 一律以台北時間為準，不使用 datetime.now()／date.today() 的執行環境時區。
#
# 這個站的讀者與新聞的 news_date 都在台灣，但 export 有兩種執行環境：
# 本機（CST）與 CI 的 Ubuntu runner（UTC）。兩者混用時，同一個「更新於」
# 欄位會一下 CST 一下 UTC——曾經上一版顯示 15:36（本機）、下一版顯示
# 22:52（CI 的 UTC，實際是隔天早上 6:52），看起來像時間倒退或沒更新。
#
# 保留期的基準日（export --retention）同樣受影響：UTC 比台北慢 8 小時，
# 台灣時間上午 8 點前跑 CI，date.today() 會拿到「昨天」，30 天的界線
# 因此整個往前挪一天。
TZ_TAIPEI = timezone(timedelta(hours=8))


def now_local():
    """現在時刻（台北時間）。所有對外顯示的時間都應該經過這裡。"""
    return datetime.now(TZ_TAIPEI)


def today_local():
    """今天的日期（台北時間）。保留期與 digest 的預設日期都用這個。"""
    return now_local().date()

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

# 等級門檻（分數下限，由高到低）。grade_of() 與 schema 的說明都由這裡生成，
# 曾經是 grade_of() 一份、schema 輸出手抄一份，改門檻時會讓對外說明與實際評分不一致。
# FALLBACK_GRADE 不列進門檻：D 是「其餘」而非某個分數帶，寫成 ("D", 0) 會讓
# grade_of(-1) 落空，schema 也會印出無意義的「0+ D」。
GRADE_THRESHOLDS = [("S", 85), ("A", 70), ("B", 55), ("C", 40)]
FALLBACK_GRADE = "D"

# 全部等級，由高到低。網頁的 tab 順序、grade 參數驗證、封存層級都取這份，
# 不要再寫 "SABCD" 字面值——那是字串，`grade not in "SABCD"` 會讓 "AB" 通過驗證。
GRADES = [g for g, _ in GRADE_THRESHOLDS] + [FALLBACK_GRADE]

# 保留期第二層只留這些等級（近 30 天全留，30-90 天僅此，見 server.py 的 RECENT_DAYS）。
ARCHIVE_GRADES = ("S", "A")

# digest 裡展開完整段落（含一句話判斷與理由）的等級；其餘只列一行。
# 與 ARCHIVE_GRADES 目前同值但語意不同，各自獨立調整。
DIGEST_DETAILED_GRADES = ("S", "A")

# 標籤別名的初始種子。同一個主題在不同新聞裡的寫法幾乎一定會漂
# （輝達／NVIDIA、301 關稅／美國 301 關稅），分裂成多個標籤就失去
# 「關聯新聞」的意義。
#
# 別名本身存在 db 的 tag_aliases 表（`news.py alias` 管理），這裡只是
# init 時的種子，不是執行時的查詢來源——別名是會持續長出來的資料，
# 每發現一組新寫法就要改一次原始碼並不合理。
#
# 正規化只發生在寫入時（db 內存的一律是正規名），所以 CI 從 JSON 重建
# 靜態站時完全不需要這張表，它純粹是本機評分時的輔助資料。
TAG_ALIAS_SEED = {
    "nvidia": "NVIDIA",
    "輝達": "NVIDIA",
    "nvda": "NVIDIA",
    "tsmc": "台積電",
    "301關稅": "美國301關稅",
    "美國301": "美國301關稅",
    "301調查": "美國301關稅",
    "chatgpt": "OpenAI",
    "facebook": "Meta",
    "微軟": "Microsoft",
    "蘋果": "Apple",
    "人工智慧": "AI",
    "生成式ai": "AI",
    "美中貿易戰": "美中貿易",
    "中美貿易": "美中貿易",
    "關稅戰": "關稅",
    "央行": "貨幣政策",
    "升息": "貨幣政策",
    "降息": "貨幣政策",
    "fed": "聯準會",
    "台海": "台海情勢",
    "兩岸": "台海情勢",
    "烏克蘭": "俄烏戰爭",
    "中東": "中東局勢",
    "以色列": "中東局勢",
    "淨零": "氣候變遷",
    "缺電": "能源政策",
    "核電": "能源政策",
    "電價": "能源政策",
    "少子化": "人口結構",
    "高齡化": "人口結構",
    "個資": "資安",
    "駭客": "資安",
    "房價": "房市",
    "囤房稅": "房市",
    "勞保": "年金制度",
    "年金": "年金制度",
}

# 一則新聞最多幾個標籤。標籤是為了「找到相關的其他新聞」，
# 掛太多會讓每個標籤都變得不具區辨力（極端情況：每則都掛「AI」）。
MAX_TAGS = 5

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
    tags TEXT,
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

-- 標籤別名 → 正規名。alias 是已正規化的比對鍵（小寫、去空白），
-- 由 alias_key() 產生，故 PRIMARY KEY 就足以保證不會有兩種寫法對到同一個鍵。
CREATE TABLE IF NOT EXISTS tag_aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn):
    """補上既有 db 缺少的欄位。

    CREATE TABLE IF NOT EXISTS 對已存在的表完全不動，所以新增欄位時舊 db
    不會自動跟上（news.db 不進版控，各機器的 db 是各自長出來的）。
    比對實際欄位，缺的才補，重跑安全。

    只處理「補上可為 NULL 的新欄位」這種最單純的情況。若哪天需要改名、
    回填或跨表遷移，那是改用 PRAGMA user_version 分版本執行的時機，
    不要把複雜的遷移塞進這裡。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(news)")}
    if "tags" not in have:
        conn.execute("ALTER TABLE news ADD COLUMN tags TEXT")

    # 別名種子只在表是空的時候灌入。用 INSERT OR IGNORE 逐筆補會讓
    # 「刻意刪掉某個種子別名」在下次連線時復活，等於刪不掉。
    if not conn.execute("SELECT 1 FROM tag_aliases LIMIT 1").fetchone():
        conn.executemany(
            "INSERT OR IGNORE INTO tag_aliases (alias, canonical) VALUES (?, ?)",
            [(alias_key(a), c) for a, c in TAG_ALIAS_SEED.items()],
        )
    conn.commit()


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


def alias_key(tag):
    """別名表的比對鍵：小寫、去掉所有空白。

    讓「NVIDIA」「nvidia」「301 關稅」都收斂到同一個鍵，
    別名表因此不必為大小寫與空格各存一列。
    """
    return "".join((tag or "").lower().split())


def load_aliases(conn):
    """讀出 {比對鍵: 正規名}。表不存在時回空 dict（舊 db 尚未 migrate）。"""
    try:
        return {r["alias"]: r["canonical"] for r in conn.execute(
            "SELECT alias, canonical FROM tag_aliases")}
    except sqlite3.OperationalError:
        return {}


def normalize_tag(tag, aliases):
    """把一個標籤收斂成正規名；無法識別的原樣保留（只去頭尾空白）。

    aliases 是 load_aliases() 的結果，由呼叫端讀一次後傳入。刻意不提供
    「省略就自己開連線」的預設值：那會讓迴圈內的呼叫每筆開一次 db，
    而且是寫起來最順手的那個寫法。
    """
    tag = (tag or "").strip()
    return aliases.get(alias_key(tag), tag) if tag else ""


def parse_tags(value, aliases):
    """把 add 傳入的 tags（list 或逗號分隔字串）正規化成標籤 list。

    去重時保留首次出現的順序（set 會讓每次寫入的排列不同，
    導致 data/news.json 產生無意義的 diff）。
    """
    if not value:
        return []
    if isinstance(value, str):
        raw = value.replace("，", ",").split(",")
    else:
        raw = list(value)
    out = []
    for item in raw:
        tag = normalize_tag(str(item), aliases)
        if tag and tag not in out:
            out.append(tag)
    return out


def tags_of(row):
    """讀出一筆資料的標籤 list。db 存的是 JSON 字串，空值一律回空 list。"""
    raw = row["tags"] if "tags" in row.keys() else None
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(i) for i in items] if isinstance(items, list) else []


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
    for grade, low in GRADE_THRESHOLDS:
        if total >= low:
            return grade
    return FALLBACK_GRADE


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
        # 用台北時間判斷：news_date 填的是台灣的日期，若拿 UTC 比對，
        # 台灣上午 8 點前寫入今天的新聞會被誤判成「未來日期」而擋下
        if parsed > today_local():
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
    tags = parse_tags(data.get("tags"), load_aliases(conn))
    if len(tags) > MAX_TAGS:
        conn.close()
        sys.exit(f"錯誤：標籤最多 {MAX_TAGS} 個，收到 {len(tags)} 個（{'、'.join(tags)}）")

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
        "tags": json.dumps(tags, ensure_ascii=False) if tags else None,
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
    tag_note = f"　🏷 {'、'.join(tags)}" if tags else ""
    print(f"已新增 id={cur.lastrowid}：[{grade} 級 {total} 分] {data['title']}{tag_note}")
    conn.close()

    # 順手同步 data/news.json，否則 db 更新了但進版控的資料沒動，靜態站不會變。
    # 批次評分時每筆都重寫整份 JSON 是浪費，用 --no-export 跳過，最後再手動
    # 跑一次 export-json 即可。
    if not args.no_export:
        export_news_json(DATA_JSON_PATH)


def format_news_row(r):
    """CLI 列表的一行。list 與 tags 共用同一種格式，避免兩處各印各的。"""
    return (f"{r['id']:>4}  {r['news_date'] or '----------'}  "
            f"{r['grade']} {r['total_score']:>3}  {r['title']}")


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
        print(format_news_row(r))
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
    date = args.date or today_local().isoformat()
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
            if r["grade"] in DIGEST_DETAILED_GRADES:
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
    "one_line", "why_important", "affected", "watch_next", "tags",
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
    thresholds = " / ".join(f"{lo}+ {g}" for g, lo in GRADE_THRESHOLDS)
    # 別名存在 db，取幾組實際的當範例（表是空的就退回種子），
    # 讓人知道「會被收斂」這件事；完整清單用 `news.py alias` 看
    conn = connect()
    rows = conn.execute(
        "SELECT alias, canonical FROM tag_aliases ORDER BY canonical LIMIT 4").fetchall()
    conn.close()
    pairs = [(r["alias"], r["canonical"]) for r in rows] or list(TAG_ALIAS_SEED.items())[:4]
    alias_sample = "、".join(f"{a}→{c}" for a, c in pairs)
    print(f"""add 接受的 JSON 格式（/news-importance-score 的評分結果）：

{{
  "title": "新聞標題（必填）",
  "url": "原始新聞連結",
  "summary": "新聞摘要（2-3 句）",
  "news_date": "YYYY-MM-DD（新聞事件發生日，非評分日）",
  "section": "{" / ".join(SECTIONS)}",
  "tags": ["主題標籤 1", "主題標籤 2"],
  "one_line": "一句話判斷",
  "why_important": "為什麼重要",
  "affected": "可能影響誰",
  "watch_next": ["觀察指標 1", "觀察指標 2", "觀察指標 3"],
  "dimensions": {{
{dims}
  }}
}}

規則：
- total_score 與 grade 不用填，由 dimensions 加總並判定等級（{thresholds} / 其餘 {FALLBACK_GRADE}）。
- 各面向分數不得超過上限，超出會拒絕寫入。
- news_date 必須是補零的 YYYY-MM-DD（2026-7-5 會被擋），不接受不存在的日期
  與未來日期；可留空表示日期不明。
- 相同 url 預設拒絕重複寫入（--force 可覆寫）。
- tags 是主題標籤，用來把講同一件事的新聞串起來（如 NVIDIA、美國301關稅）。
  最多 {MAX_TAGS} 個，超過會拒絕；寫入時會套用別名表收斂成正規名
  （{alias_sample} …），所以不必擔心大小寫或慣用寫法不同。
  取「未來還會有後續報導」的主題（公司、政策、事件、地區），不要用
  「重要」「值得關注」這種形容詞，也不要用只會出現一次的具體事件名。
  已用過的標籤看 `news.py tags`，優先沿用既有的；別名表看 `news.py alias`，
  發現同一主題分裂成兩個標籤時用 `news.py alias <別名> <正規名>` 收斂
  （會一併修正既有資料）。""")


def cmd_export(args):
    from server import export_static

    export_static(Path(args.out), retention=args.retention)


def cmd_og(args):
    """重產分享預覽圖（需要 ImageMagick 與系統中文字型）。

    圖片進版控、由 export 直接複製，所以只有想更新圖上的數字時才需要跑這個。
    """
    from server import OG_IMAGE_SRC, build_og_image

    out = Path(args.out) if args.out else OG_IMAGE_SRC
    out.parent.mkdir(parents=True, exist_ok=True)
    build_og_image(out)
    kb = out.stat().st_size / 1024
    print(f"已產生分享預覽圖 {out}（{kb:.0f} KB）")
    print("記得 commit，靜態站是複製這份成品上線的")


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


def tag_counts(rows):
    """回傳 [(tag, count), ...]，筆數多的在前、同筆數依標籤名排序。

    吃已取回的 rows 而非自己查 db：CLI 要數全部，網頁只數保留期內的，
    差別留給呼叫端決定。兩邊各抄一份實作的話，改了排序規則卻只改一邊
    完全不會報錯——CLI 列表與網頁下拉選單就會靜默地不一致。

    tags 存成 JSON 字串而非另開關聯表：一則最多 5 個標籤、總量是數百筆的
    規模，SQL 端的 GROUP BY 省下來的時間遠不及多一張表的複雜度。
    """
    counts = {}
    for r in rows:
        for t in tags_of(r):
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def cmd_tags(args):
    conn = connect()
    if args.tag:
        # 指定標籤時列出該標籤底下的新聞（這就是「關聯新聞」的 CLI 版）
        target = normalize_tag(args.tag, load_aliases(conn))
        items = [
            r for r in conn.execute(
                "SELECT id, news_date, grade, total_score, title, tags FROM news"
                " ORDER BY news_date DESC, total_score DESC"
            ) if target in tags_of(r)
        ]
        conn.close()
        if not items:
            print(f"（沒有標籤「{target}」的新聞）")
            return
        print(f"🏷 {target}（{len(items)} 則）")
        for r in items:
            print(format_news_row(r))
        return
    rows = tag_counts(conn.execute("SELECT tags FROM news"))
    conn.close()
    if not rows:
        print("（目前沒有任何標籤，評分時在 JSON 加 tags 欄位即可）")
        return
    for tag, n in rows:
        print(f"{n:>4}  {tag}")
    print(f"\n共 {len(rows)} 個標籤（`news.py tags <標籤>` 列出該標籤的新聞）")


def retag_existing(conn, aliases):
    """把 news 表內已存的標籤重跑一次正規化，回傳異動筆數。

    新增別名時，先前用舊寫法存進去的資料不會自己收斂——「輝達」與「NVIDIA」
    仍是兩個標籤。這裡把既有資料一起帶過去，別名才真的有把新聞關聯起來。
    """
    changed = 0
    for r in conn.execute("SELECT id, tags FROM news WHERE tags IS NOT NULL AND tags != ''"):
        before = tags_of(r)
        # 走 parse_tags 而非自己再寫一次正規化＋保序去重：兩份實作漂移時，
        # add 寫入與 alias 收斂會產出不同結果，正是標籤分裂要防的事
        after = parse_tags(before, aliases)
        if after != before:
            conn.execute(
                "UPDATE news SET tags = ? WHERE id = ?",
                (json.dumps(after, ensure_ascii=False) if after else None, r["id"]),
            )
            changed += 1
    conn.commit()
    return changed


def cmd_alias(args):
    """管理標籤別名（列出／新增／刪除）。"""
    conn = connect()

    if args.remove:
        key = alias_key(args.remove)
        cur = conn.execute("DELETE FROM tag_aliases WHERE alias = ?", (key,))
        conn.commit()
        conn.close()
        print(f"{'已刪除別名' if cur.rowcount else '找不到別名'}：{args.remove}")
        return

    if args.alias:
        if not args.canonical:
            conn.close()
            sys.exit("錯誤：新增別名需要兩個參數——`news.py alias <別名> <正規名>`")
        key = alias_key(args.alias)
        canonical = args.canonical.strip()
        if not key or not canonical:
            conn.close()
            sys.exit("錯誤：別名與正規名都不能是空字串")
        # 別名指向另一個別名會讓正規化結果取決於查表順序，直接擋掉；
        # 使用者要的多半是「兩者都指向同一個正規名」。
        existing = load_aliases(conn)
        if alias_key(canonical) in existing and existing[alias_key(canonical)] != canonical:
            target = existing[alias_key(canonical)]
            conn.close()
            sys.exit(
                f"錯誤：「{canonical}」本身是「{target}」的別名，"
                f"請直接指向正規名：news.py alias {args.alias} {target}"
            )
        conn.execute(
            "INSERT INTO tag_aliases (alias, canonical) VALUES (?, ?)"
            " ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical",
            (key, canonical),
        )
        conn.commit()
        print(f"已設定別名：{args.alias} → {canonical}")
        changed = retag_existing(conn, load_aliases(conn))
        conn.close()
        if changed:
            print(f"已一併收斂 {changed} 筆既有新聞的標籤")
            if not args.no_export:
                export_news_json(DATA_JSON_PATH)
        return

    rows = conn.execute(
        "SELECT alias, canonical FROM tag_aliases ORDER BY canonical, alias").fetchall()
    conn.close()
    if not rows:
        print("（沒有任何別名）")
        return
    width = max(len(r["alias"]) for r in rows)
    for r in rows:
        print(f"{r['alias']:<{width}}  →  {r['canonical']}")
    print(f"\n共 {len(rows)} 組別名（`news.py alias <別名> <正規名>` 新增）")


def cmd_tag(args):
    """手動修改既有新聞的標籤（補標、改標、清空）。"""
    conn = connect()
    row = conn.execute("SELECT id, title, tags FROM news WHERE id = ?", (args.id,)).fetchone()
    if not row:
        conn.close()
        sys.exit(f"錯誤：找不到 id={args.id}")

    aliases = load_aliases(conn)
    before = tags_of(row)
    if args.clear:
        tags = []
    elif args.add:
        # before 已是正規名，再過一次 parse_tags 是無操作，順便共用保序去重
        tags = parse_tags(before + args.add, aliases)
    else:
        tags = parse_tags(args.tags, aliases)
    if len(tags) > MAX_TAGS:
        conn.close()
        sys.exit(f"錯誤：標籤最多 {MAX_TAGS} 個，會變成 {len(tags)} 個（{'、'.join(tags)}）")

    conn.execute(
        "UPDATE news SET tags = ? WHERE id = ?",
        (json.dumps(tags, ensure_ascii=False) if tags else None, args.id),
    )
    conn.commit()
    conn.close()
    print(f"id={args.id}：{'、'.join(before) or '（無）'} → {'、'.join(tags) or '（無）'}")
    print(f"  {row['title']}")
    if not args.no_export:
        export_news_json(DATA_JSON_PATH)


def cmd_skip(args):
    conn = connect()
    for pid in args.ids:
        cur = conn.execute("UPDATE pending SET status = 'skipped' WHERE id = ? AND status IN ('new', 'low', 'dup')", (pid,))
        print(f"id={pid}：{'已略過' if cur.rowcount else '找不到或已處理'}")
    conn.commit()
    conn.close()


# ── 評分回顧校準 ──────────────────────────────────────────────
#
# 「影響時間」與「結構性意義」是評分中最像預測的兩個面向：它們宣稱這則新聞
# 之後還會有後續、還值得追蹤。這裡回頭用實際資料驗證那個宣稱。
#
# 訊號是「後續關聯度」：一則評分後，它的標籤在往後的日子裡又出現幾次。
# 但原始次數不能直接用，有兩個會讓指標退化成雜訊的偏誤：
#
#   1. 標籤規模差異：「中國」有 62 則、「儲能」只有 1 則。掛大標籤的新聞
#      天生後續多，與它本身重不重要無關。
#   2. 每日評分量差異：6/22 只評 2 則、7/27 評 49 則。晚期評的新聞
#      天生有更多後續機會。
#
# 兩者都修正後，指標才反映「這則的後續是否超出它所屬主題與時期的常態」。

# 回顧窗口：評分後觀察多少天的後續。與 server.py 的 RECENT_DAYS 無關——
# 那是網站的保留期，這是判斷「後續是否發生」的觀察期，語意不同故各自定義。
REVIEW_WINDOW_DAYS = 30


def _days_between(earlier, later):
    """兩個 YYYY-MM-DD 字串相差幾天。"""
    a = datetime.strptime(earlier, "%Y-%m-%d").date()
    b = datetime.strptime(later, "%Y-%m-%d").date()
    return (b - a).days


def followup_stats(rows, window_days=REVIEW_WINDOW_DAYS):
    """算出每則的「超額後續」——後續密度相對於同主題、同時期常態的倍數。

    回傳 {news_id: {"followups": 實際後續數, "expected": 期望值, "excess": 超額倍數}}。

    expected 的算法把兩個偏誤一起處理：
      期望值 = 該則所有標籤的平均基準率 × 窗口內的總評分量
    其中基準率 = 該標籤的總出現數 / 全部則數，代表「隨機抓一則有多大機會
    掛到這個標籤」。乘上窗口內的總量，就是「若這則毫不特別，預期會有幾則後續」。

    excess = 實際 / 期望。大於 1 代表後續比同類新聞的常態更密集。
    期望值為 0（窗口內沒有任何新評分）時回傳 None，代表無從判斷而非表現差——
    這兩者混為一談會讓最近評的新聞全被誤判成高估。
    """
    dated = [r for r in rows if r["news_date"]]
    total = len(dated)
    if not total:
        return {}

    # 各標籤的基準出現率
    tag_total = Counter()
    for r in dated:
        for t in tags_of(r):
            tag_total[t] += 1
    base_rate = {t: n / total for t, n in tag_total.items()}

    # 依日期分組，方便算窗口內的量體
    by_date = defaultdict(list)
    for r in dated:
        by_date[r["news_date"]].append(r)

    # 資料庫裡最新的評分日。窗口尚未走完的則要標記出來——它們的後續數
    # 天生被截斷（今天評的則後續必為 0），拿去跟窗口完整的則比較毫無意義。
    latest = max(r["news_date"] for r in dated)

    out = {}
    for r in dated:
        my_tags = set(tags_of(r))
        if not my_tags:
            continue  # 沒有標籤就沒有後續訊號可算，略過而非給 0
        window = [
            other
            for d, items in by_date.items()
            if 0 < _days_between(r["news_date"], d) <= window_days
            for other in items
        ]
        hits = sum(1 for o in window if my_tags & set(tags_of(o)))
        # 用該則標籤的平均基準率——取平均而非總和，避免掛越多標籤期望值越高
        rate = sum(base_rate.get(t, 0) for t in my_tags) / len(my_tags)
        expected = rate * len(window)
        # 窗口是否已走完：距今不足 window_days 的則，後續還沒機會發生完
        elapsed = _days_between(r["news_date"], latest)
        out[r["id"]] = {
            "followups": hits,
            "expected": expected,
            "excess": (hits / expected) if expected > 0 else None,
            "mature": elapsed >= window_days,
            "elapsed": elapsed,
        }
    return out


def dimension_calibration(rows, stats):
    """比對各面向的給分與實際後續，找出系統性偏高或偏低。

    只檢驗 duration（影響時間）與 structural（結構性意義）——這兩個面向
    本質是對未來的預測，可以用後續資料驗證。其餘三個面向（影響範圍、
    決策相關性、事實可信度）評的是新聞當下的性質，後續數多寡與它們是否
    評得準沒有邏輯關聯，硬套只會產生看似有據的假結論。
    """
    verifiable = ("duration", "structural")
    result = {}
    for key in verifiable:
        label = next(lb for k, lb, _ in DIMENSIONS if k == key)
        limit = next(mx for k, _, mx in DIMENSIONS if k == key)
        # 只取算得出 excess 的（窗口內有評分量）
        pairs = [
            (r[f"{key}_score"], stats[r["id"]]["excess"])
            for r in rows
            if r["id"] in stats and stats[r["id"]]["excess"] is not None
            and r[f"{key}_score"] is not None
        ]
        if len(pairs) < 2:
            result[key] = {"label": label, "limit": limit, "n": len(pairs)}
            continue
        # 以該面向給分的中位數切成高分組與低分組，比較兩組的後續表現。
        # 用中位數而非固定門檻，量表不同的面向才能一致處理。
        scores = sorted(s for s, _ in pairs)
        mid = scores[len(scores) // 2]
        high = [e for s, e in pairs if s >= mid]
        low = [e for s, e in pairs if s < mid]
        result[key] = {
            "label": label,
            "limit": limit,
            "n": len(pairs),
            "median": mid,
            "high_n": len(high),
            "low_n": len(low),
            "high_excess": (sum(high) / len(high)) if high else None,
            "low_excess": (sum(low) / len(low)) if low else None,
        }
    return result


def cmd_review(args):
    """回顧指定期間的評分，用後續資料檢驗判讀是否準確。"""
    conn = connect()
    rows = list(conn.execute("SELECT * FROM news"))
    conn.close()
    if not rows:
        print("（資料庫沒有評分紀錄）")
        return

    stats = followup_stats(rows, args.window)

    # 只回顧窗口已走完的則。成熟與否由 followup_stats 判定（以資料中最新的
    # 評分日為基準），不在這裡另算一次日期——兩份判斷漂移時，報表會納入
    # 窗口未滿的則，它們的後續天生偏低而被誤報成「高估」。
    mature = [
        r for r in rows
        if r["id"] in stats and stats[r["id"]]["mature"]
        and stats[r["id"]]["excess"] is not None
    ]
    if args.since:
        mature = [r for r in mature if r["news_date"] >= args.since]
    immature = sum(1 for r in rows if r["id"] in stats and not stats[r["id"]]["mature"])

    print(f"評分回顧：觀察窗口 {args.window} 天，"
          f"已走完窗口的有 {len(mature)} 則"
          f"（另有 {immature} 則窗口未滿，不納入）")
    if not mature:
        print(f"\n（沒有滿足條件的資料——最早的評分距今可能還不到 {args.window} 天。"
              f"\n  這個工具要等資料累積過一個完整窗口才有意義。）")
        return
    if len(mature) < 20:
        print(f"⚠️  樣本僅 {len(mature)} 則，結論參考價值有限（建議 20 則以上）")

    ranked = sorted(mature, key=lambda r: stats[r["id"]]["excess"])

    over = [r for r in ranked if r["total_score"] >= 70 and stats[r["id"]]["excess"] < 1]
    if over:
        print(f"\n▼ 可能高估（A 級以上但後續低於同類常態）")
        for r in over[:args.limit]:
            s = stats[r["id"]]
            print(f"  {r['news_date']} {r['grade']}{r['total_score']:>3}  "
                  f"後續 {s['followups']:>3} 則（期望 {s['expected']:.1f}）"
                  f" ×{s['excess']:.2f}  {r['title'][:32]}")

    under = [r for r in reversed(ranked)
             if r["total_score"] < 55 and stats[r["id"]]["excess"] >= 1.5]
    if under:
        print(f"\n▲ 可能低估（C 級以下但後續遠超同類常態）")
        for r in under[:args.limit]:
            s = stats[r["id"]]
            print(f"  {r['news_date']} {r['grade']}{r['total_score']:>3}  "
                  f"後續 {s['followups']:>3} 則（期望 {s['expected']:.1f}）"
                  f" ×{s['excess']:.2f}  {r['title'][:32]}")

    print("\n面向校準（只驗可用後續檢證的兩個面向）")
    for key, c in dimension_calibration(mature, stats).items():
        if c.get("n", 0) < 2:
            print(f"  {c['label']}：樣本不足（{c.get('n', 0)} 則）")
            continue
        hi, lo = c["high_excess"], c["low_excess"]
        if hi is None or lo is None:
            print(f"  {c['label']}：分組後樣本不足")
            continue
        gap = hi - lo
        # 給高分的那組，後續本來就該比低分組多。差距越大代表這個面向越有鑑別力。
        verdict = "有鑑別力" if gap > 0.3 else ("鑑別力偏弱" if gap > 0 else "方向相反⚠️")
        print(f"  {c['label']}（滿分 {c['limit']}，以 {c['median']} 分切組）")
        print(f"    高分組 {c['high_n']:>3} 則 → 平均超額 ×{hi:.2f}")
        print(f"    低分組 {c['low_n']:>3} 則 → 平均超額 ×{lo:.2f}")
        print(f"    差距 {gap:+.2f} → {verdict}")

    print("\n說明：「超額」是後續數相對於同標籤、同時期常態的倍數。")
    print("      ×1 表示與常態相同；已修正大標籤與每日評分量的偏誤。")


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

    p_review = sub.add_parser(
        "review", help="評分回顧校準：用後續資料檢驗判讀是否準確")
    p_review.add_argument(
        "--window", type=int, default=REVIEW_WINDOW_DAYS,
        help=f"觀察窗口天數（預設 {REVIEW_WINDOW_DAYS}）")
    p_review.add_argument("--since", help="只回顧此日期之後評的（YYYY-MM-DD）")
    p_review.add_argument("--limit", type=int, default=10, help="每個清單最多列幾則")

    p_tags = sub.add_parser("tags", help="列出所有標籤；帶標籤名則列出該標籤的新聞")
    p_tags.add_argument("tag", nargs="?", help="標籤名（省略則列出全部標籤與筆數）")

    p_alias = sub.add_parser("alias", help="管理標籤別名（不帶參數則列出全部）")
    p_alias.add_argument("alias", nargs="?", help="別名寫法（如 輝達）")
    p_alias.add_argument("canonical", nargs="?", help="正規名（如 NVIDIA）")
    p_alias.add_argument("--remove", help="刪除指定別名")
    p_alias.add_argument("--no-export", action="store_true", help="不要順手更新 data/news.json")

    p_tag = sub.add_parser("tag", help="修改某則新聞的標籤")
    p_tag.add_argument("id", type=int)
    p_tag.add_argument("tags", nargs="*", help="要設定的標籤（覆蓋原有）")
    p_tag.add_argument("--add", nargs="+", help="附加標籤而非覆蓋")
    p_tag.add_argument("--clear", action="store_true", help="清空標籤")
    p_tag.add_argument("--no-export", action="store_true", help="不要順手更新 data/news.json")

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

    p_og = sub.add_parser("og", help="重產分享預覽圖（需 ImageMagick，圖片要 commit）")
    p_og.add_argument("--out", help="輸出路徑（預設 assets/og.png）")

    args = parser.parse_args()
    {
        "init": cmd_init,
        "add": cmd_add,
        "list": cmd_list,
        "serve": cmd_serve,
        "fetch": cmd_fetch,
        "pending": cmd_pending,
        "skip": cmd_skip,
        "review": cmd_review,
        "tags": cmd_tags,
        "tag": cmd_tag,
        "alias": cmd_alias,
        "digest": cmd_digest,
        "prune": cmd_prune,
        "export-json": cmd_export_json,
        "import-json": cmd_import_json,
        "schema": cmd_schema,
        "export": cmd_export,
        "og": cmd_og,
    }[args.command](args)


if __name__ == "__main__":
    main()
