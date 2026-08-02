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
import math
import sqlite3
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from statistics import median
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "news.db"
FEEDS_PATH = Path(__file__).parent / "feeds.txt"
DATA_JSON_PATH = Path(__file__).parent / "data" / "news.json"
# watch_next 的驗證結果。與 news.json 分開存：它不是網站資料（靜態站不用它），
# 但必須進版控——news.db 不進版控，只存在 db 裡的話一次重新 clone 就全沒了，
# 而這是要累積數月才有意義的校準資料。
WATCH_VERIFY_JSON = Path(__file__).parent / "data" / "watch_verify.json"

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

-- watch_next 的逐條驗證結果。idx 是該則 watch_next 陣列中的位置，
-- 故 (news_id, idx) 唯一。verdict 見 WATCH_VERDICTS。
-- 刻意用 news_url 而非只存 news_id 當關聯鍵：CI 的 import-json --replace
-- 會整個重建 news 表，id 由 AUTOINCREMENT 重新配發，只存 id 會在重建後
-- 全部對錯人。url 是評分資料裡穩定且唯一的識別。
CREATE TABLE IF NOT EXISTS watch_verify (
    news_url TEXT NOT NULL,
    idx INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    note TEXT,
    evidence_url TEXT,
    verified_date TEXT NOT NULL,
    PRIMARY KEY (news_url, idx)
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 投資觀察的表定義在檔案後段（緊鄰它的邏輯），故分兩段建立。
    # 外鍵約束要逐連線開啟，SQLite 預設是關的——predictions 的
    # ON DELETE CASCADE 少了這行不會生效，刪 position 會留下孤兒預測。
    conn.executescript(SCHEMA + POSITIONS_SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
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

    # predictions 的 source_hint 於 2026-07-31 加入，既有 db 要補上
    # （CREATE TABLE IF NOT EXISTS 對已存在的表完全不動）。
    have_p = {r["name"] for r in conn.execute("PRAGMA table_info(predictions)")}
    if have_p and "source_hint" not in have_p:
        conn.execute("ALTER TABLE predictions ADD COLUMN source_hint TEXT")

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


def validate_date_string(value, field="date", allow_future=False):
    """檢查日期是補零的 YYYY-MM-DD，不合格就中止。

    日期會被拿去做「字串字面」比較（保留期分層、到期判定），
    '2026-7-5' 會大於 '2026-06-25'，未補零會被歸到錯誤的層級，故在入口擋掉。

    allow_future 給 due_date 用——預測的到期日本來就在未來，
    但 news_date 填到未來幾乎一定是誤植。

    這是唯一出處：news_date 與投資線的日期共用同一套規則，
    各驗一次遲早會有一邊漏掉補零檢查（那正是這個函式存在的原因）。
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        sys.exit(f"錯誤：{field}「{value}」格式不正確，須為 YYYY-MM-DD（月日要補零）")
    # strptime 接受未補零的 '2026-7-5'，但那樣存進 db 會讓字面比較出錯，
    # 故要求與正規化後的字串完全相同。
    if parsed.isoformat() != value:
        sys.exit(f"錯誤：{field}「{value}」須補零寫成 {parsed.isoformat()}")
    # 用台北時間判斷：日期填的是台灣的日期，若拿 UTC 比對，
    # 台灣上午 8 點前寫入今天的資料會被誤判成「未來日期」而擋下
    if not allow_future and parsed > today_local():
        sys.exit(f"錯誤：{field}「{value}」是未來日期，請確認是否誤植")
    return parsed


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

    news_date = data.get("news_date")
    if news_date:
        validate_date_string(news_date, field="news_date")

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

    # 寫入後對「當日累計」做最粗的紅線提醒。
    #
    # 只印一行、只在超過門檻時印：細節交給 `news.py calibrate`。
    # 這一層存在的理由是「分開做就不會做」——單靠記得跑 calibrate 已經
    # 證明會漏（2026-07-25~28 那四天 S/A 衝到 28-48% 而每天都回報「一致」）。
    # 檢查範圍是當日累計而非單一批次：add 沒有批次概念，同一天分多批評分時
    # 只看得到合計值。
    if news_date:
        _warn_if_batch_drifts(conn, news_date)
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

    run(port=args.port, host=args.host)


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


# 一次 fetch 新增少於這個數就提示「可考慮稍後再評」。
#
# 2026-08-02 實測：距上次抓取僅數小時就再跑一次，只新增 5 則且 4 則是社會
# 事件，最後只評到 1 則——但那是跑完 fetch 才知道的。抓內文與評分是整個
# 流程最貴的步驟，值得在最前面就給出「這輪划不划算」的訊號。
#
# 門檻取 10：三天實測 fetch→評分的轉換率約 16-27%，10 則大約對應 2-3 則
# 可評，低於這個數就不值得走完整套流程（讀錨點、抓內文、校準、watch）。
THIN_BATCH_THRESHOLD = 10


def warn_if_thin_batch(conn, total_new):
    """新增太少時提示可以稍後再跑，並附上距上次抓取的間隔。

    只是提示不是阻擋——使用者可能就是想看有沒有新東西。
    """
    if total_new >= THIN_BATCH_THRESHOLD:
        return
    row = conn.execute(
        "SELECT MAX(fetched_at) AS t FROM pending WHERE fetched_at IS NOT NULL"
    ).fetchone()
    gap = ""
    prev = conn.execute(
        "SELECT fetched_at FROM pending WHERE fetched_at IS NOT NULL "
        "ORDER BY fetched_at DESC LIMIT 1 OFFSET ?", (max(total_new, 1),)
    ).fetchone()
    if prev and row and row["t"]:
        try:
            a = datetime.strptime(prev["fetched_at"][:19], "%Y-%m-%d %H:%M:%S")
            b = datetime.strptime(row["t"][:19], "%Y-%m-%d %H:%M:%S")
            hours = (b - a).total_seconds() / 3600
            if hours >= 0:
                gap = f"（距上一批約 {hours:.0f} 小時）"
        except ValueError:
            pass
    print(f"  ℹ️  本次新增偏少{gap}。抓內文與評分是最貴的步驟，"
          f"若非特意查看可考慮稍後再跑。")


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
    warn_if_thin_batch(conn, total_new)
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


WATCH_VERIFY_COLUMNS = [
    "news_url", "idx", "verdict", "note", "evidence_url", "verified_date",
]


def export_watch_verify(out, quiet=True):
    """把 watch_verify 表寫進版控用的 JSON。

    與 export_news_json 分開：news.json 的完整鏡像保證只涵蓋 news 表，
    把別的表混進去會讓 import-json --replace 的語意變模糊。
    """
    conn = connect()
    rows = conn.execute(
        f"SELECT {', '.join(WATCH_VERIFY_COLUMNS)} FROM watch_verify "
        "ORDER BY news_url, idx"
    ).fetchall()
    conn.close()
    items = [{k: r[k] for k in WATCH_VERIFY_COLUMNS} for r in rows]
    text = json.dumps(items, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    if not quiet:
        print(f"已匯出 {len(items)} 筆判定到 {out}")


def import_watch_verify(path):
    """從 JSON 回灌 watch_verify（重新 clone 後重建 db 用）。"""
    path = Path(path)
    if not path.exists():
        return 0
    items = json.loads(path.read_text(encoding="utf-8"))
    conn = connect()
    conn.executemany(
        f"INSERT OR REPLACE INTO watch_verify "
        f"({', '.join(WATCH_VERIFY_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(WATCH_VERIFY_COLUMNS))})",
        [tuple(it.get(k) for k in WATCH_VERIFY_COLUMNS) for it in items],
    )
    conn.commit()
    conn.close()
    return len(items)


def cmd_export_json(args):
    export_news_json(args.out)
    export_watch_verify(WATCH_VERIFY_JSON)


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
    # watch_verify 以 url 關聯 news，重建後 id 會變但 url 不變，故可獨立回灌。
    # 重新 clone 後只跑 import-json 也能把判定資料一起帶回來。
    n = import_watch_verify(WATCH_VERIFY_JSON)
    if n:
        print(f"另回灌 {n} 筆 watch_next 判定")


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

# watch_next 逐條驗證的判定。
#   hit    ——指標明確發生了，且有後續報導佐證
#   miss   ——窗口已走完但沒發生（這是真訊號：當初的預測落空）
#   moot   ——前提本身消失，指標變得無從判斷（如「觀察某談判進展」但談判取消）
# moot 必須與 miss 分開：把「無從判斷」算成「預測錯」會系統性低估命中率，
# 而這兩者對評分校準的意涵完全不同——miss 該檢討判斷，moot 只是世界變了。
WATCH_VERDICTS = ("hit", "miss", "moot")

# 一條 watch_next 至少要等這麼多天才值得判定。太早看什麼都還沒發生，
# 會把「時候未到」誤記成 miss，而 miss 是要用來檢討判斷的訊號。
WATCH_MIN_AGE_DAYS = 7

# ── 投資觀察（positions）────────────────────────────────────────────
#
# 與新聞評分共用 WATCH_VERDICTS 的判定語意（hit/miss/moot），刻意不另立一套：
# 「無從判斷 ≠ 判斷錯」這條規則在兩邊完全相同，各存一份遲早會漂。
#
# 但**不共用評分面向**：新聞的 5 個面向評的是「值不值得被理解」，
# 投資觀察評的是「這個推論成不成立」，硬套會讓兩邊的統計都失去意義。
# 投資線刻意沒有分數與等級——它的品質由命中率直接衡量，不需要事前打分。

# 一次觀點底下的預測分類。命中率要**依類型分開看**：
# 兩類的可驗證程度不同（fundamental 有客觀數字、structural 要人判讀事件是否發生），
# 混在一起算總命中率會得到一個無法行動的數字，那正是 review 的失敗模式。
#
# **刻意沒有「市場類」（價格或相對表現）**，2026-07-31 廢除，理由有二：
#   1. 沒有資料源。feeds.txt 只有中央社／BBC／科技新報，且 LOWPRIO_KEYWORDS
#      還主動過濾「收盤／盤中／盤前」——價格資訊本來就不在這個系統裡。
#      實測 7 條市場類預測全部無法判定，連 miss 都算不上（進不了分母）。
#   2. **更根本的是它測不出判斷力**。「Meta 落後標普 8 個百分點」的結果混雜
#      利率、地緣、大盤情緒，推論正確與否只佔很小部分——與 review 用「標籤
#      後續數」當代理訊號是同一種病（分不清事件延燒與判斷正確）。
# 原本想測的「資訊是否已被 price in」，用基本面預測加新聞就能觀察
#（例：台積電毛利率創高的同時股價表現如何，新聞會報導）。
# 由 test_no_market_prediction_kind 守著，避免日後「補回來比較完整」而復活。
PREDICTION_KINDS = [
    ("fundamental", "基本面", "公司自己會揭露的數字（營收、財報、產能、訂單）"),
    ("structural", "結構", "產業或政策事件本身是否發生"),
]
PREDICTION_KIND_KEYS = tuple(k for k, _, _ in PREDICTION_KINDS)

# 投資線的判定值域 = 新聞線的三種，外加 void。
#
# void（作廢）與 moot（前提消失）刻意分開，**差別在歸因**：
#   moot ——世界變了（談判取消、政策撤回），不是我判斷錯
#   void ——我當初設計了無法驗證的指標，是我的問題
# 混用會讓 moot 失去診斷價值（同 CLAUDE.md 對 moot/miss 分開的堅持）。
# 兩者都不進命中率分母，但 void 額外代表「這條當初就不該這樣寫」。
#
# 只給投資線：新聞線的 watch_next 不依賴外部資料源，沒有這個問題，
# 加進去只會多一個永遠用不到的選項，而多餘的選項會被誤用。
POSITION_VERDICTS = WATCH_VERDICTS + ("void",)

# source_hint 填這些等於沒填——它們指不出到期時該打開哪一份資料。
# 刻意只擋最明顯的幾個而非做語意判斷：這道檢查的目的是讓人停下來想
# 「到底去哪查」，不是要精準攔截所有敷衍。填得出具體來源的人不會被擋到。
VAGUE_SOURCE_HINTS = frozenset({
    "財報", "新聞", "市場數據", "公司公告", "官方資料", "報導",
    "財報數字", "市場", "股價", "價格", "資料", "公開資訊",
})

# 一條投資預測至少要放這麼多天才值得判定。比新聞的 7 天長，因為
# 基本面預測的驗證點（月營收、財報）本來就以月為單位，太早看必然是「還沒發生」。
POSITION_MIN_AGE_DAYS = 14

POSITIONS_SCHEMA = """
-- 一次「觀點」：某個時點對某個標的的判斷，含推論與依據。
-- 同一標的可以有多次觀點，形成時間序列——這是刻意的：事後檢討時最想知道的
-- 不是「猜錯了」而是「當時為什麼那樣想」，而看法的演變本身就是資料。
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT,
    market TEXT,
    obs_date TEXT NOT NULL,
    thesis TEXT NOT NULL,
    rationale TEXT,
    source_url TEXT,
    tags TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);
CREATE INDEX IF NOT EXISTS idx_positions_date ON positions(obs_date);

-- 一次觀點底下的可驗證預測。kind 見 PREDICTION_KINDS。
-- verdict 為 NULL 代表尚未判定；已判定的值域見 POSITION_VERDICTS。
--
-- source_hint 是必填的「這條要去哪裡查」，不是備註。2026-07-31 加入，
-- 因為 7 條市場類預測寫得很工整卻沒有任何資料源可驗證——寫的當下沒人問過
-- 「這個數字從哪來」。填不出具體來源，代表這條預測當下就該重寫。
--
-- 這裡用 position_id 當外鍵（而非像 watch_verify 用 url）：positions 不進版控、
-- 沒有 import-json --replace 那種「整表重建、id 重配」的流程，
-- id 是穩定的。若哪天投資線也要進版控重建，這裡要跟著改成穩定鍵。
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    source_hint TEXT,
    due_date TEXT,
    verdict TEXT,
    note TEXT,
    verified_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_predictions_position ON predictions(position_id);
"""


def _days_between(earlier, later):
    """兩個 YYYY-MM-DD 字串相差幾天。"""
    a = datetime.strptime(earlier, "%Y-%m-%d").date()
    b = datetime.strptime(later, "%Y-%m-%d").date()
    return (b - a).days


def followup_stats(rows, window_days=REVIEW_WINDOW_DAYS):
    """算出每則的「超額後續」——後續密度相對於同主題、同時期常態的倍數。

    回傳 {news_id: {"followups": 實際後續數, "expected": 期望值, "excess": 超額倍數}}。

    expected 的算法把三個偏誤一起處理：
      期望值 = 該則所有標籤在「窗口當期」的平均出現率 × 窗口內的總評分量
    其中出現率 = 該標籤在窗口內的出現數 / 窗口內的總則數，代表「這段期間隨機
    抓一則有多大機會掛到這個標籤」。乘上窗口內的總量，就是「若這則毫不特別，
    預期會有幾則後續」。

    基準率刻意取「窗口當期」而非全期平均。全期平均會讓正在延燒的主題虛高：
    廣西水災在六月完全沒出現、七月連日洗版，用全期算基準會低估它的當期常態，
    於是那幾則的超額倍數衝到 ×3 以上被誤報為「低估」。但後續多是主題在延燒，
    不是當初判斷精準——這兩者混為一談，報表會反覆建議調高災害類的分數。
    改用當期基準後，主題自己延燒時基準也跟著抬高，超額倍數才回到常態，
    留下的高倍數才是真訊號。由 test_burst_topic_is_not_reported_as_underestimated 守著。

    excess = 實際 / 期望。大於 1 代表後續比同類新聞的常態更密集。
    期望值為 0（窗口內沒有任何新評分）時回傳 None，代表無從判斷而非表現差——
    這兩者混為一談會讓最近評的新聞全被誤判成高估。
    """
    dated = [r for r in rows if r["news_date"]]
    total = len(dated)
    if not total:
        return {}

    # 依「距最早日期的天數」建索引，讓窗口查詢變成陣列切片而非逐筆比對日期。
    # 直接對每一則掃過全部資料是 O(n²)——實測 4300 筆要 12 秒，且資料是
    # 每天增加的，天真寫法會越用越慢。
    origin = min(r["news_date"] for r in dated)
    latest = max(r["news_date"] for r in dated)
    span = _days_between(origin, latest)

    # day_rows[d] = 第 d 天所有則的標籤集合；day_count[d] = 該天則數
    day_rows = [[] for _ in range(span + 1)]
    day_count = [0] * (span + 1)
    for r in dated:
        d = _days_between(origin, r["news_date"])
        day_rows[d].append(set(tags_of(r)))
        day_count[d] += 1

    # 前綴和讓「窗口內共有幾則」變成 O(1) 相減，取代原本的逐筆日期比對
    prefix_count = [0] * (span + 2)
    for d in range(span + 1):
        prefix_count[d + 1] = prefix_count[d] + day_count[d]

    # 每個標籤各自的日期前綴和，讓「窗口內這個標籤出現幾次」同樣是 O(1)。
    # 只為實際出現過的標籤配置，且僅在其出現的日子累加——直接對每個標籤
    # 都開一條 span 長度的陣列，在標籤數量成長後會變成記憶體與時間的浪費。
    tag_days = defaultdict(list)
    for d in range(span + 1):
        for other_tags in day_rows[d]:
            for t in other_tags:
                tag_days[t].append(d)

    def tag_count_in(t, lo, hi):
        """標籤 t 在第 lo..hi 天（含）的出現次數。tag_days[t] 已依日期遞增。"""
        days_sorted = tag_days.get(t)
        if not days_sorted:
            return 0
        return bisect_right(days_sorted, hi) - bisect_left(days_sorted, lo)

    out = {}
    for r in dated:
        my_tags = set(tags_of(r))
        if not my_tags:
            continue  # 沒有標籤就沒有後續訊號可算，略過而非給 0
        d0 = _days_between(origin, r["news_date"])
        lo, hi = d0 + 1, min(d0 + window_days, span)  # 只看「之後」，故從 d0+1 起
        if hi < lo:
            window_total = hits = 0
        else:
            window_total = prefix_count[hi + 1] - prefix_count[lo]
            # 命中以「聯集」計：一則後續沾到任一標籤就算一次，不因掛多個
            # 標籤而重複計算——否則 hits 可能超過窗口總數讓 excess 虛高。
            # 只掃窗口內的日子，不再對每則掃過全部資料。
            hits = sum(
                1
                for dd in range(lo, hi + 1)
                for other_tags in day_rows[dd]
                if my_tags & other_tags
            )
        # 用該則標籤在「窗口當期」的平均出現率——取平均而非總和，避免掛越多
        # 標籤期望值越高。當期而非全期，是為了不讓延燒中的主題虛高（見 docstring）。
        if window_total > 0:
            rate = sum(
                tag_count_in(t, lo, hi) / window_total for t in my_tags
            ) / len(my_tags)
        else:
            rate = 0
        expected = rate * window_total
        # 窗口是否已走完。基準刻意用「資料中最新的評分日」而非今天：後續資料
        # 只在有評分時才會累積，若停評一個月，用今天判斷會把那個月的則全部
        # 標記成成熟，但它們的後續其實根本沒機會發生。用資料自己的時間軸，
        # 停評期間就只是不再產生新的成熟項，不會製造假成熟。
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
        #
        # 分數是整數且高度集中（結構性意義有 51 則同為 13 分），若用 s >= mid
        # 切組，所有同分者會全被歸到高分組，讓「高分組」混入大量中間值而稀釋
        # 對比。故改為排除等於中位數的則——寧可少用一些樣本，也不要讓兩組的
        # 界線模糊到測不出鑑別力。
        scores = sorted(s for s, _ in pairs)
        mid = scores[len(scores) // 2]
        high = [e for s, e in pairs if s > mid]
        low = [e for s, e in pairs if s < mid]
        ties = sum(1 for s, _ in pairs if s == mid)
        result[key] = {
            "label": label,
            "limit": limit,
            "n": len(pairs),
            "median": mid,
            "ties": ties,
            "high_n": len(high),
            "low_n": len(low),
            "high_excess": (sum(high) / len(high)) if high else None,
            "low_excess": (sum(low) / len(low)) if low else None,
        }
    return result


# ── 評分標準漂移偵測 ──────────────────────────────────────────
#
# `review` 檢驗「判斷準不準」，但它假設評分標準前後一致。標準若漂了，
# review 的結論就不可信——所以漂移偵測是 review 的前提而非補充。
#
# 2026-07-28 首次實測就發現漂移已在發生：決策相關性 8.18 → 10.82
# （滿分的 +13.2%），A 級佔比 7% → 25%。這不是新聞變重要，是標準鬆了。
#
# 判別依據有二，缺一不可：
#   1. 事實可信度幾乎沒動（-1.1%）。它是最有客觀依據的面向（有無具名來源、
#      有無數據），若新聞本質真的改變，它也該跟著變。它不動而主觀面向大動，
#      指向的是判斷鬆動。
#   2. 控制主題後漂移仍在：同樣掛「中國」的新聞，決策相關性 6.20 → 9.78。
#      這排除了「近期剛好都是大事」的解釋。
#
# 第 2 點是這個工具與天真實作的關鍵差異——不控制主題的話，只要當期主題
# 組成改變就會誤報，工具很快會被當成狼來了而忽略。

# 各面向漂移超過滿分的多少比例就警示。取 8% 是因為實測中事實可信度
# （公認最穩定的面向）波動在 1% 上下，而已知有問題的決策相關性是 13%，
# 8% 落在兩者之間且留有雜訊餘裕。
DRIFT_ALERT_PCT = 8.0

# 控制主題比較時，單一標籤在前後期各自至少要有幾則才納入。
# 太少會讓個別極端值主導結論。
DRIFT_MIN_TAG_SAMPLE = 5


def _mean(values):
    return sum(values) / len(values) if values else None


def drift_by_dimension(early, late):
    """比對前後兩期各面向的平均分，回傳漂移幅度。"""
    out = {}
    for key, label, limit in DIMENSIONS:
        a = _mean([r[f"{key}_score"] for r in early if r[f"{key}_score"] is not None])
        b = _mean([r[f"{key}_score"] for r in late if r[f"{key}_score"] is not None])
        if a is None or b is None:
            continue
        delta = b - a
        out[key] = {
            "label": label,
            "limit": limit,
            "early": a,
            "late": b,
            "delta": delta,
            # 換算成滿分的百分比，不同量表的面向才能互相比較
            "pct": delta / limit * 100,
        }
    return out


def drift_within_tags(early, late, dim_key, min_sample=DRIFT_MIN_TAG_SAMPLE):
    """只比較「同一標籤內」的前後期，控制掉主題組成改變的干擾。

    近期若剛好湧入重大主題（如 AI、半導體），整體平均自然上升——那不是
    標準鬆動。看同一標籤內部的變化才能區分兩者：若掛「中國」的新聞在近期
    也拿到更高的分，那就與主題無關了。
    """
    def by_tag(rows):
        acc = defaultdict(list)
        for r in rows:
            v = r[f"{dim_key}_score"]
            if v is None:
                continue
            for t in tags_of(r):
                acc[t].append(v)
        return acc

    ea, la = by_tag(early), by_tag(late)
    out = []
    for tag in set(ea) & set(la):
        if len(ea[tag]) < min_sample or len(la[tag]) < min_sample:
            continue
        out.append({
            "tag": tag,
            "early": _mean(ea[tag]),
            "late": _mean(la[tag]),
            "delta": _mean(la[tag]) - _mean(ea[tag]),
            "n_early": len(ea[tag]),
            "n_late": len(la[tag]),
        })
    return sorted(out, key=lambda x: -abs(x["delta"]))


# 錨點期間：評分標準的基準區間。skill 的「固定錨點」表由這段資料算出。
#
# 刻意寫死日期而非「最早 N 則」：錨點的意義就在於它固定不動。若隨資料
# 滾動，標準會跟著近期評分一起漂，等於沒有錨點——而漂移正是要防的事。
ANCHOR_START = "2026-06-22"
ANCHOR_END = "2026-07-10"

# 分數段的切法，與 skill 的錨點表一致
ANCHOR_BANDS = [
    (70, 100, "A 70+"),
    (65, 69, "B 65-69"),
    (60, 64, "B 60-64"),
    (55, 59, "B 55-59"),
    (48, 54, "C 48-54"),
    (40, 47, "C 40-47"),
]


def anchor_table(rows, start=ANCHOR_START, end=ANCHOR_END):
    """算出錨點期間各分數段的面向中位數。

    回傳 [{"label":..., "n":..., "scope":..., "decision":...}]。
    skill 的「固定錨點」表就是這個輸出，`news.py anchors` 可重新產生它核對。
    """
    early = [
        r for r in rows
        if r["news_date"] and start <= r["news_date"] <= end
    ]
    out = []
    for lo, hi, label in ANCHOR_BANDS:
        g = [r for r in early if lo <= r["total_score"] <= hi]
        if not g:
            continue
        out.append({
            "label": label,
            "n": len(g),
            "scope": median([r["scope_score"] for r in g]),
            "decision": median([r["decision_score"] for r in g]),
        })
    return out


# calibrate 的警告門檻：本批 S/A 佔比達錨點期的幾倍才示警。
#
# 2026-07-31 用全部 22 個批次實測各門檻的觸發率：
#   1.5x → 50%、2x → 41%、2.5x → 32%、3x → 27%
# 取 2.5x 是因為它精準命中 7/25-7/28 那段真正異常的區間（S/A 28-48%），
# 同時放行 7/20、7/30 這類 10-12% 的正常波動。1.5x 與 2x 會讓半數批次
# 都示警，那等於沒有警告；3x 則會漏掉 7/28 的 28%。
CALIBRATE_SA_MULTIPLE = 2.5

# 批次小於這個數就不檢查：10 則裡有 2 則 A 就是 20%，佔比本身不穩定。
# 實測 7/13、7/14 各只有 10 則卻都達 20%，正是這種誤觸發。
CALIBRATE_MIN_BATCH = 10


def sa_rate(rows):
    """S/A 佔比。空集合回 None 而非 0——「沒有資料」與「都不是 S/A」不同。"""
    rows = [r for r in rows if r["grade"]]
    if not rows:
        return None
    return sum(1 for r in rows if r["grade"] in ARCHIVE_GRADES) / len(rows)


def anchor_sa_rate(rows, start=ANCHOR_START, end=ANCHOR_END):
    """錨點期間的 S/A 佔比，calibrate 的比較基準。

    刻意即時從 db 算而非寫死常數：寫死的數字與實際資料脫鉤後沒有任何
    機制會發現。改由 TestCalibrateBaseline 斷言它等於實測值，
    錨點期資料若被改動測試會失敗——那件事本身就該被知道。
    """
    return sa_rate([
        r for r in rows
        if r["news_date"] and start <= r["news_date"] <= end
    ])


def calibration_report(rows, batch):
    """比對一批評分與錨點期的標準。

    回傳 {"baseline":…, "batch_rate":…, "ratio":…, "warn":…, "dims":…}；
    batch 太小或錨點無資料時 warn 為 False 並附上 skipped 原因。

    只做「這批有沒有異常」的粗篩，刻意不做主題控制——每批 10-20 則湊不出
    足夠的同標籤樣本（drift 要求前後期各 5 則）。代價是分不出「標準鬆了」
    與「新聞真的變重要」，這點由報表明說，主題控制交給 drift。
    """
    base = anchor_sa_rate(rows)
    rate = sa_rate(batch)
    out = {
        "baseline": base, "batch_rate": rate, "n": len(batch),
        "ratio": None, "warn": False, "skipped": None,
        "dims": dimension_medians(batch),
        "anchor_dims": dimension_medians([
            r for r in rows
            if r["news_date"] and ANCHOR_START <= r["news_date"] <= ANCHOR_END
        ]),
    }
    if base is None:
        out["skipped"] = f"錨點期間（{ANCHOR_START} ~ {ANCHOR_END}）沒有評分資料"
    elif rate is None:
        out["skipped"] = "這批沒有可比對的評分"
    elif len(batch) < CALIBRATE_MIN_BATCH:
        out["skipped"] = (f"樣本僅 {len(batch)} 則"
                          f"（少於 {CALIBRATE_MIN_BATCH} 則不檢查，佔比不穩定）")
    else:
        # 基準為 0 時任何 S/A 都算偏離，用無限大表示而非除以零
        out["ratio"] = (rate / base) if base else (float("inf") if rate else 1.0)
        out["warn"] = out["ratio"] >= CALIBRATE_SA_MULTIPLE
    return out


def _warn_if_batch_drifts(conn, on_date):
    """add 寫入後的紅線提醒：當日累計的 S/A 佔比超過門檻就印一行。

    刻意只印一行而非完整報表：評分當下看到長篇分析也來不及改，
    這裡的作用只是讓人停下來，細節用 `news.py calibrate` 看。
    """
    rows = list(conn.execute("SELECT * FROM news"))
    batch = [r for r in rows if r["news_date"] == on_date]
    rep = calibration_report(rows, batch)
    if not rep["warn"]:
        return
    # 錨點為 0% 時倍數是無限大，印「inf 倍」讀起來像壞掉而不像訊息。
    # 真實資料不會走到這條（實測錨點 5.7%），但 fixture 與未來的資料都可能。
    ratio = (f"{rep['ratio']:.1f} 倍" if math.isfinite(rep["ratio"])
             else "遠高於")
    print(f"  ⚠️  {on_date} 累計 {rep['n']} 則，S/A 佔比 {rep['batch_rate']:.0%}"
          f"　{ratio}錨點（{rep['baseline']:.0%}）"
          f"　→ python3 news.py calibrate")


def dimension_medians(rows):
    """各面向的中位數，供 calibrate 診斷「是哪個面向在推高分數」。"""
    if not rows:
        return {}
    return {
        key: median([r[f"{key}_score"] for r in rows
                     if r[f"{key}_score"] is not None])
        for key, _label, _mx in DIMENSIONS
        if any(r[f"{key}_score"] is not None for r in rows)
    }


def cmd_calibrate(args):
    """比對指定日期的評分與錨點期的標準（漂移的日常粗篩）。

    與 drift 的分工：drift 跑全量資料且做主題控制，回答「標準有沒有鬆」；
    calibrate 只看一批，回答「今天這批要不要停下來看一眼」。
    """
    conn = connect()
    rows = list(conn.execute("SELECT * FROM news"))
    conn.close()
    if not rows:
        print("（資料庫沒有評分紀錄）")
        return

    on_date = args.date or max(
        (r["news_date"] for r in rows if r["news_date"]), default=None)
    if not on_date:
        print("（沒有任何帶日期的評分）")
        return
    batch = [r for r in rows if r["news_date"] == on_date]

    rep = calibration_report(rows, batch)
    print(f"評分校準：{on_date}（{rep['n']} 則）"
          f" vs 錨點 {ANCHOR_START} ~ {ANCHOR_END}\n")

    if rep["baseline"] is None:
        print(f"  {rep['skipped']}")
        return
    print(f"  S/A 佔比   錨點 {rep['baseline']:.1%}"
          f"　本批 {rep['batch_rate']:.1%}", end="")
    if rep["ratio"] is None:
        print()
    elif math.isfinite(rep["ratio"]):
        print(f"（{rep['ratio']:.1f}x）")
    else:
        print("（錨點為 0%，無法計算倍數）")

    if rep["dims"]:
        print("\n  各面向中位數（錨點 → 本批）")
        for key, label, _mx in DIMENSIONS:
            if key not in rep["dims"]:
                continue
            a = rep["anchor_dims"].get(key)
            b = rep["dims"][key]
            flag = "" if a is None or b <= a else "  ↑"
            base_txt = f"{a:g}" if a is not None else "—"
            print(f"    {label:<12} {base_txt:>4} → {b:g}{flag}")

    print()
    if rep["skipped"]:
        print(f"  ⏭  {rep['skipped']}")
    elif rep["warn"]:
        over = (f"達錨點的 {rep['ratio']:.1f} 倍（門檻 {CALIBRATE_SA_MULTIPLE}x）"
                if math.isfinite(rep["ratio"]) else "遠高於錨點（錨點為 0%）")
        print(f"  ⚠️  S/A 佔比{over}，回頭核對錨定範例再確認這批。")
        print("     注意：這個指標分不出「標準鬆了」與「當期新聞真的更重要」。")
        print("     要區分兩者看 `news.py drift`（它做主題控制，只比同標籤內的前後期）。")
    else:
        print("  ✅ 未達警告門檻。")
        print("     但這只是粗篩：單批樣本小、且分不出標準鬆動與主題組成改變，")
        print("     真正的漂移判斷仍以 `news.py drift` 為準。")


def cmd_anchors(_args):
    """重新產生 skill 的「固定錨點」表，用來核對它有沒有跟資料脫節。"""
    conn = connect()
    rows = list(conn.execute("SELECT * FROM news"))
    conn.close()
    table = anchor_table(rows)
    if not table:
        print(f"錨點期間（{ANCHOR_START} ~ {ANCHOR_END}）沒有評分資料")
        return
    total = sum(t["n"] for t in table)
    print(f"固定錨點：{ANCHOR_START} ~ {ANCHOR_END}（{total} 則）\n")
    print("| 分數段 | n | 影響範圍 | 決策相關性 |")
    print("|--------|---|---------|-----------|")
    for t in table:
        print(f"| {t['label']} | {t['n']} | {t['scope']:g} | {t['decision']:g} |")
    print("\n這張表是評分的絕對門檻基準，skill 的「固定錨點」章節應與此一致。")
    print("**不要因為近期新聞看起來更重要而更新它**——那正是漂移本身。")


def cmd_drift(args):
    """偵測評分標準是否隨時間漂移。"""
    conn = connect()
    rows = [r for r in conn.execute(
        "SELECT * FROM news WHERE news_date IS NOT NULL ORDER BY news_date")]
    conn.close()
    if len(rows) < 20:
        print(f"（資料僅 {len(rows)} 則，不足以判斷漂移）")
        return

    # 切分點預設取中位日期，讓前後期樣本量接近；也可用 --split 指定。
    # 切分是「<= split 為前期」，故若中位日剛好是最後一天，後期會是空的
    # （資料只有兩個日期時必然如此）。改取「不是最後一天」的候選日，
    # 讓預設行為在日期種類很少時也能運作。
    if args.split:
        split = args.split
    else:
        dates = [r["news_date"] for r in rows]
        split = dates[len(dates) // 2]
        last = dates[-1]
        if split >= last:
            earlier = [d for d in dates if d < last]
            split = earlier[len(earlier) // 2] if earlier else split
    early = [r for r in rows if r["news_date"] <= split]
    late = [r for r in rows if r["news_date"] > split]
    if not early or not late:
        print(f"（以 {split} 切分後有一期是空的，請用 --split 指定其他日期）")
        return

    print(f"評分標準漂移偵測：以 {split} 切分")
    print(f"  前期 {early[0]['news_date']} ~ {split}（{len(early)} 則）")
    print(f"  後期 ~ {late[-1]['news_date']}（{len(late)} 則）")

    dims = drift_by_dimension(early, late)
    print(f"\n各面向平均分（漂移超過滿分 {DRIFT_ALERT_PCT}% 標記 ⚠️）")
    alerted = []
    for key, d in dims.items():
        flag = ""
        if abs(d["pct"]) >= DRIFT_ALERT_PCT:
            flag = "  ⚠️"
            alerted.append(key)
        print(f"  {d['label']:>6}（滿分 {d['limit']:>2}）"
              f"  {d['early']:5.2f} → {d['late']:5.2f}"
              f"   {d['delta']:+.2f} 分（{d['pct']:+.1f}%）{flag}")

    # 等級分布：漂移最直觀的後果
    print("\n等級分布")
    for name, rs in (("前期", early), ("後期", late)):
        counts = Counter(r["grade"] for r in rs)
        parts = [f"{g}={counts[g] / len(rs) * 100:.0f}%" for g in GRADES if counts.get(g)]
        print(f"  {name}：{'  '.join(parts)}")

    if not alerted:
        print("\n✅ 未偵測到顯著漂移")
        return

    # 對每個警示面向做主題控制——這才是判斷「是不是真漂移」的關鍵
    print(f"\n控制主題後的複驗（排除「近期剛好都是大事」的可能）")
    print(f"只比同一標籤內的前後期，各期至少 {DRIFT_MIN_TAG_SAMPLE} 則")
    for key in alerted:
        label = dims[key]["label"]
        within = drift_within_tags(early, late, key)
        if not within:
            print(f"\n  {label}：沒有標籤在前後期都達到樣本門檻，無法複驗")
            continue
        same_dir = sum(1 for w in within if (w["delta"] > 0) == (dims[key]["delta"] > 0))
        print(f"\n  {label}（{same_dir}/{len(within)} 個標籤與整體同方向）")
        for w in within[:args.limit]:
            print(f"    {w['tag']:>10}  {w['early']:5.2f} → {w['late']:5.2f}"
                  f"  {w['delta']:+.2f}   n={w['n_early']}/{w['n_late']}")
        if same_dir >= len(within) * 0.7:
            print(f"    → 多數標籤同向，漂移**不是**主題組成造成的，標準確實鬆動")
        else:
            print(f"    → 各標籤方向分歧，較可能是主題組成改變而非標準漂移")

    print("\n提醒：漂移會同時毀掉資料的前後可比性與 `review` 的意義"
          "（它假設標準一致）。")
    print("      校準做法是回頭看前期的錨定範例，而不是調整門檻遷就現況。")


# 共用標籤要夠「窄」才算得上線索。「台灣政策」有 29 則、幾乎與所有內政新聞
# 相交，共用它完全不代表某條具體指標發生了；「荷莫茲海峽」只有 13 則，共用它
# 就相當有指向性。門檻取佔比而非絕對則數，資料量成長時才不會失效。
WATCH_TAG_MAX_SHARE = 0.05
# 但佔比在資料少時會失真：只有 30 則時，一個只出現 2 次的標籤就佔了 6.7%，
# 明明夠窄卻被擋掉。故另給絕對則數的下限，兩者取寬鬆者。
WATCH_TAG_MIN_ABS = 3


def watch_candidates(rows, verified, on_date, min_age=WATCH_MIN_AGE_DAYS):
    """列出「當天的新聞可能命中哪些舊的 watch_next」。

    回傳 [{"row": 舊則, "idx": 條目序號, "text": 指標文字, "age": 天數,
           "related": [...], "key_tags": [共用的窄標籤]}]，依線索強度排序。

    配對只靠標籤交集，刻意不做語意比對——這裡的目的是把候選縮到人能讀完的
    範圍，最終判定由讀的人下。用關鍵字或相似度自動判定「指標是否發生」會產出
    大量似是而非的 hit，而這張表的全部價值就在於判定是可信的。

    但光有交集不夠：只共用寬標籤（「台灣政策」「美國」）的配對是雜訊，
    實測會讓「綜所稅退稅」被列為「海纜備援進度」的線索。故只採共用標籤中
    佔比低於 WATCH_TAG_MAX_SHARE 的那些，並以最窄的共用標籤決定排序。

    已判定過的 (url, idx) 不再列出；未滿 min_age 的也不列，太早看什麼都還沒
    發生，會把「時候未到」誤記成 miss。
    """
    dated = [r for r in rows if r["news_date"]]
    if not dated:
        return []
    tag_share = Counter()
    for r in dated:
        for t in tags_of(r):
            tag_share[t] += 1
    total = len(dated)
    narrow = {
        t for t, n in tag_share.items()
        if n / total < WATCH_TAG_MAX_SHARE or n <= WATCH_TAG_MIN_ABS
    }

    on_rows = [(r, set(tags_of(r))) for r in rows if r["news_date"] == on_date]
    if not on_rows:
        return []

    out = []
    for r in rows:
        if not r["news_date"] or r["news_date"] >= on_date:
            continue  # 只回顧比當天更早的則
        age = _days_between(r["news_date"], on_date)
        if age < min_age:
            continue
        my_tags = set(tags_of(r))
        if not my_tags:
            continue
        # 只保留「共用了窄標籤」的當日新聞，並記下是哪些標籤讓它們相關
        related, key_tags = [], set()
        for other, other_tags in on_rows:
            shared = my_tags & other_tags & narrow
            if shared:
                related.append(other)
                key_tags |= shared
        if not related:
            continue
        # 線索強度＝最窄的共用標籤（佔比越低越有指向性）
        strength = min(tag_share[t] for t in key_tags)
        for idx, text in enumerate(watch_list_of(r)):
            if (r["url"], idx) in verified:
                continue
            out.append({
                "row": r, "idx": idx, "text": text, "age": age,
                "related": related, "key_tags": sorted(key_tags),
                "strength": strength,
            })
    # 窄標籤優先，其次新的在前——最有指向性的線索要出現在清單頂端
    out.sort(key=lambda c: (c["strength"], -_days_between("2000-01-01",
                                                          c["row"]["news_date"]),
                            c["idx"]))
    return out


def watch_list_of(row):
    """取出某則的 watch_next 陣列。存的是 JSON 字串，壞掉時回空陣列而非炸開。"""
    raw = row["watch_next"]
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return val if isinstance(val, list) else []


def load_verified(conn):
    """回傳 {(news_url, idx): row}，供 watch_candidates 排除已判定的條目。"""
    return {
        (r["news_url"], r["idx"]): r
        for r in conn.execute("SELECT * FROM watch_verify")
    }


def load_hits_from_json(path=None):
    """從 data/watch_verify.json 讀出「命中」的判定，供靜態站標示 ✓。

    回傳 {news_url: {idx: row}}，只含 verdict == "hit" 的條目。

    刻意讀 JSON 而非 db：CI 建站時只有版控裡的 JSON，沒有 news.db。
    這也是判定資料要另存 JSON 並進版控的原因之一。

    只回傳 hit 是網頁端的呈現決定（見 render_card 的說明），不是資料本身
    的過濾——命中率統計一律用 watch-stats，那裡 miss 與 moot 都在。
    """
    path = Path(path or WATCH_VERIFY_JSON)
    if not path.exists():
        return {}
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for it in items:
        if it.get("verdict") != "hit":
            continue
        url = it.get("news_url")
        if url is None or it.get("idx") is None:
            continue
        out.setdefault(url, {})[it["idx"]] = it
    return out


def verified_counts_from_json(path=None):
    """回傳 {news_url: (hit數, 已判定總數)}，用來標示「N 條中 M 條成真」。

    分母含 miss 但排除 moot——moot 是「無從判斷」不是判錯，
    放進分母會讓比例失真（與 watch_accuracy 的處理一致）。
    """
    path = Path(path or WATCH_VERIFY_JSON)
    if not path.exists():
        return {}
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for it in items:
        v = it.get("verdict")
        if v == "moot" or v not in WATCH_VERDICTS:
            continue
        url = it.get("news_url")
        if url is None:
            continue
        h, t = out.get(url, (0, 0))
        out[url] = (h + (1 if v == "hit" else 0), t + 1)
    return out


def cmd_watch(args):
    """列出當天新聞可能命中的舊 watch_next，供批次評分時順手判定。"""
    conn = connect()
    rows = list(conn.execute("SELECT * FROM news"))
    verified = load_verified(conn)
    conn.close()
    if not rows:
        print("（資料庫沒有評分紀錄）")
        return

    on_date = args.date or max(
        (r["news_date"] for r in rows if r["news_date"]), default=None)
    if not on_date:
        print("（沒有任何帶日期的評分）")
        return

    cands = watch_candidates(rows, verified, on_date, args.min_age)
    if args.json:
        print(json.dumps([
            {
                "news_url": c["row"]["url"],
                "idx": c["idx"],
                "text": c["text"],
                "source_title": c["row"]["title"],
                "source_date": c["row"]["news_date"],
                "source_grade": c["row"]["grade"],
                "age_days": c["age"],
                "key_tags": c["key_tags"],
                "related_titles": [x["title"] for x in c["related"]],
            }
            for c in cands[:args.limit]
        ], ensure_ascii=False, indent=1))
        return

    if not cands:
        print(f"{on_date}：沒有可判定的 watch_next 候選"
              f"（已判定的不再列出，未滿 {args.min_age} 天的也不列）")
        return

    print(f"{on_date} 的新聞可能命中以下舊指標（共 {len(cands)} 條，"
          f"顯示前 {min(args.limit, len(cands))} 條）")
    print(f"判定後用：news.py watch-verify <url> <idx> <hit|miss|moot> [--note ...]\n")
    for c in cands[:args.limit]:
        r = c["row"]
        print(f"[{r['news_date']} {r['grade']}{r['total_score']} "
              f"{c['age']}天前] {r['title'][:44]}")
        print(f"  #{c['idx']} {c['text']}")
        print(f"     共用標籤：{'、'.join(c['key_tags'])}")
        for other in c["related"][:3]:
            print(f"     ↳ 今日相關：{other['title'][:40]}")
        print(f"     url={r['url']}")
        print()


def cmd_watch_verify(args):
    """記錄一條 watch_next 的判定結果。"""
    if args.verdict not in WATCH_VERDICTS:
        sys.exit(f"verdict 必須是 {'/'.join(WATCH_VERDICTS)} 之一")
    conn = connect()
    row = conn.execute("SELECT * FROM news WHERE url = ?", (args.url,)).fetchone()
    if not row:
        conn.close()
        sys.exit(f"找不到 url={args.url} 的評分")
    items = watch_list_of(row)
    if not 0 <= args.idx < len(items):
        conn.close()
        sys.exit(f"idx 超出範圍：該則有 {len(items)} 條 watch_next（0-{len(items) - 1}）")
    # 佐證必須是資料庫裡真實存在的評分連結。
    #
    # 2026-07-28 踩過：判定時憑印象拼湊網址（technews.tw/2026/07/27/<自己造的 slug>），
    # 37 條佐證裡 24 條指向不存在的頁面、其餘 13 條格式對但編號撞到別篇無關報導，
    # 沒有一條可信，而它們正掛在一個宣稱「判斷可信」的網站上。
    # 允許自由填寫等於允許編造，故限定只能引用已在庫的報導。
    if args.evidence:
        ev = conn.execute(
            "SELECT title FROM news WHERE url = ?", (args.evidence,)
        ).fetchone()
        if not ev:
            conn.close()
            sys.exit(
                f"佐證連結不在資料庫：{args.evidence}\n"
                "  佐證只能引用已評分的報導（用 news.py list 或 tags <標籤> 找出正確網址），\n"
                "  不要自行拼湊網址——實測憑印象組的網址幾乎全是死連結或指到別篇。\n"
                "  找不到合適的已評分報導時，就省略 --evidence，把依據寫進 --note。"
            )
    today = today_local().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO watch_verify "
        "(news_url, idx, verdict, note, evidence_url, verified_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (args.url, args.idx, args.verdict, args.note, args.evidence, today),
    )
    conn.commit()
    conn.close()
    export_watch_verify(WATCH_VERIFY_JSON)
    print(f"已記錄 [{args.verdict}] {items[args.idx][:50]}")


def hit_rate(counts):
    """由 {verdict: 次數} 算命中率。沒有可判定的條目時回 None。

    moot 一律排除在分母外——它代表「無從判斷」而非「預測錯」，
    算進去會系統性低估命中率（見 WATCH_VERDICTS 的說明）。
    新聞線與投資線共用這條規則，兩邊各算一次遲早會漂。
    """
    judged = counts["hit"] + counts["miss"]
    return (counts["hit"] / judged) if judged else None


def accuracy_by(verified, key_of, group_of):
    """通用的命中率彙總：新聞線與投資線共用。

    verified 是 {(關聯鍵, idx): 判定 row}；key_of 從判定 row 取出關聯鍵，
    group_of 把關聯鍵映射到分組名（新聞是等級、投資是預測類型），
    回傳 None 代表該條無從歸類（來源已刪除或鍵改過）而略過。

    刻意不讓這個函式自己查 db：取資料的範圍要留給呼叫端決定
    （與 tag_counts 同一個理由，見 CLAUDE.md 的「schema 常數只能有一份」）。
    """
    overall = Counter()
    by_group = defaultdict(Counter)
    for k, v in verified.items():
        group = group_of(key_of(k, v))
        if group is None:
            continue
        overall[v["verdict"]] += 1
        by_group[group][v["verdict"]] += 1
    return {
        "overall": {"counts": overall, "rate": hit_rate(overall)},
        "by_group": {
            g: {"counts": c, "rate": hit_rate(c)} for g, c in by_group.items()
        },
    }


def watch_accuracy(rows, verified):
    """彙總新聞線的命中率。回傳 {"overall": {...}, "by_grade": {...}}。"""
    by_url = {r["url"]: r for r in rows if r["url"]}
    acc = accuracy_by(
        verified,
        key_of=lambda k, _v: k[0],
        group_of=lambda url: by_url[url]["grade"] if url in by_url else None,
    )
    return {"overall": acc["overall"], "by_grade": acc["by_group"]}


def cmd_watch_stats(args):
    """顯示 watch_next 的命中率統計。"""
    conn = connect()
    rows = list(conn.execute("SELECT * FROM news"))
    verified = load_verified(conn)
    conn.close()
    if not verified:
        print("（還沒有任何 watch_next 判定紀錄，用 news.py watch 開始）")
        return

    acc = watch_accuracy(rows, verified)
    o = acc["overall"]
    c = o["counts"]
    judged = c["hit"] + c["miss"]
    print(f"watch_next 命中率：已判定 {sum(c.values())} 條"
          f"（hit {c['hit']}、miss {c['miss']}、moot {c['moot']}）")
    if o["rate"] is None:
        print("  尚無可計算命中率的條目（moot 不列入分母）")
    else:
        print(f"  命中率 {o['rate']:.0%}（{c['hit']}/{judged}，moot 不列入分母）")
    if judged < 20:
        print(f"  ⚠️  樣本僅 {judged} 條，結論參考價值有限（建議 20 條以上）")

    if acc["by_grade"]:
        print("\n依等級")
        for g in GRADES:
            if g not in acc["by_grade"]:
                continue
            gc = acc["by_grade"][g]
            gj = gc["counts"]["hit"] + gc["counts"]["miss"]
            if not gj:
                continue
            print(f"  {g} 級  命中率 {gc['rate']:.0%}"
                  f"（{gc['counts']['hit']}/{gj}）")
        print("\n  高分級的命中率若低於低分級，代表高分那些「還會有後續」的")
        print("  宣稱撐不住，是評分過鬆的直接證據。")


# ── 投資觀察指令 ────────────────────────────────────────────────────

def normalize_ticker(raw):
    """標的代號一律轉大寫去空白。

    台股代號是數字（2330）、美股是字母（TSM），統一大寫讓 tsm/TSM 不會分裂成
    兩個標的——這與標籤別名是同一類問題，但代號的收斂規則單純到不需要別名表。
    """
    return "".join((raw or "").split()).upper()


def validate_position_payload(data, aliases):
    """檢查 add-position 的 JSON 並回傳正規化後的欄位。

    驗證集中在這裡（與 add 的作法一致）：欄位散落各處各驗一次，
    遲早會有一條路徑漏驗。
    """
    ticker = normalize_ticker(data.get("ticker"))
    if not ticker:
        sys.exit("ticker 不可為空")

    thesis = (data.get("thesis") or "").strip()
    if not thesis:
        sys.exit("thesis 不可為空——沒有推論的預測事後無從檢討，那正是這條線要避免的")

    obs_date = (data.get("obs_date") or "").strip() or today_local().isoformat()
    validate_date_string(obs_date, field="obs_date")

    preds = data.get("predictions") or []
    if not isinstance(preds, list) or not preds:
        sys.exit("至少要有一條 predictions——觀點沒有可驗證的預測就只是感想")

    out_preds = []
    for i, p in enumerate(preds):
        if not isinstance(p, dict):
            sys.exit(f"predictions[{i}] 必須是物件")
        kind = (p.get("kind") or "").strip()
        if kind not in PREDICTION_KIND_KEYS:
            sys.exit(
                f"predictions[{i}].kind 必須是 "
                f"{'/'.join(PREDICTION_KIND_KEYS)} 之一（收到 {kind!r}）")
        text = (p.get("text") or "").strip()
        if not text:
            sys.exit(f"predictions[{i}].text 不可為空")
        # source_hint 必填：擋的是「寫得很工整但沒人問過資料從哪來」。
        # 2026-07-31 有 7 條市場類預測就是這樣寫出來的，全部無法判定。
        hint = (p.get("source_hint") or "").strip()
        if not hint:
            sys.exit(
                f"predictions[{i}].source_hint 不可為空——要寫「這條到期時去哪裡查」。\n"
                "  可執行的例子：台積電法說會簡報／公開資訊觀測站月營收／"
                "三星財報 DS 部門別數字\n"
                "  太模糊而等於沒填：財報、新聞、市場數據\n"
                "  寫不出具體來源，代表這條預測現在就該重寫（見 position-schema）。")
        if hint in VAGUE_SOURCE_HINTS:
            sys.exit(
                f"predictions[{i}].source_hint「{hint}」太模糊，要指名到可執行的來源。\n"
                "  例：不是「財報」而是「三星財報 DS 部門別營業利益」。")
        due = (p.get("due_date") or "").strip() or None
        if due:
            validate_date_string(due, field=f"predictions[{i}].due_date",
                                 allow_future=True)
        out_preds.append({"kind": kind, "text": text,
                          "source_hint": hint, "due_date": due})

    return {
        "ticker": ticker,
        "name": (data.get("name") or "").strip() or None,
        "market": (data.get("market") or "").strip() or None,
        "obs_date": obs_date,
        "thesis": thesis,
        "rationale": (data.get("rationale") or "").strip() or None,
        "source_url": normalize_url(data.get("source_url")) or None,
        "tags": parse_tags(data.get("tags"), aliases),
        "predictions": out_preds,
    }


def cmd_add_position(args):
    """新增一次投資觀點（JSON 檔或 - 表示 stdin）。"""
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    data = json.loads(raw)
    conn = connect()
    payload = validate_position_payload(data, load_aliases(conn))

    cur = conn.execute(
        "INSERT INTO positions "
        "(ticker, name, market, obs_date, thesis, rationale, source_url, tags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (payload["ticker"], payload["name"], payload["market"], payload["obs_date"],
         payload["thesis"], payload["rationale"], payload["source_url"],
         json.dumps(payload["tags"], ensure_ascii=False) if payload["tags"] else None),
    )
    pid = cur.lastrowid
    conn.executemany(
        "INSERT INTO predictions (position_id, kind, text, source_hint, due_date) "
        "VALUES (?, ?, ?, ?, ?)",
        [(pid, p["kind"], p["text"], p["source_hint"], p["due_date"])
         for p in payload["predictions"]],
    )
    conn.commit()
    conn.close()
    print(f"已寫入 #{pid} {payload['ticker']} {payload['obs_date']}"
          f"（{len(payload['predictions'])} 條預測）")
    print(f"  {payload['thesis'][:60]}")


def load_positions(conn, ticker=None, pending_only=False):
    """讀出觀點與其預測，回傳 [(position_row, [prediction_row, ...]), ...]。

    一次撈完再在 Python 側分組，不逐筆查 predictions——資料量雖小，
    但 N+1 查詢是那種寫起來最順手、之後才發現要改的寫法。
    """
    sql = "SELECT * FROM positions"
    params = []
    if ticker:
        sql += " WHERE ticker = ?"
        params.append(normalize_ticker(ticker))
    sql += " ORDER BY obs_date DESC, id DESC"
    positions = list(conn.execute(sql, params))

    by_pos = defaultdict(list)
    for p in conn.execute("SELECT * FROM predictions ORDER BY id"):
        by_pos[p["position_id"]].append(p)

    out = [(pos, by_pos.get(pos["id"], [])) for pos in positions]
    if pending_only:
        out = [(pos, ps) for pos, ps in out if any(p["verdict"] is None for p in ps)]
    return out


def verdict_mark(verdict):
    """判定的單字元標記。三個指令共用，各存一份遲早會漂。"""
    return {"hit": "✓", "miss": "✗", "moot": "—", "void": "⊘"}.get(verdict, "·")


def kind_label_of(kind):
    """預測類型的中文標籤。查不到就原樣回傳（例如已廢除的 market）。"""
    return {k: label for k, label, _ in PREDICTION_KINDS}.get(kind, kind)


def cmd_positions(args):
    """列出投資觀點與其預測狀態。"""
    conn = connect()
    items = load_positions(conn, args.ticker, args.pending)
    conn.close()
    if not items:
        print("（沒有符合的投資觀點，用 news.py add-position 新增）")
        return

    for pos, preds in items[:args.limit]:
        head = f"#{pos['id']} {pos['ticker']}"
        if pos["name"]:
            head += f"（{pos['name']}）"
        print(f"{head}  {pos['obs_date']}")
        print(f"  {pos['thesis']}")
        if args.verbose and pos["rationale"]:
            print(f"  依據：{pos['rationale']}")
        for p in preds:
            due = f" [{p['due_date']} 前]" if p["due_date"] else ""
            print(f"   {verdict_mark(p['verdict'])} #{p['id']} "
                  f"[{kind_label_of(p['kind'])}] {p['text']}{due}")
            if args.verbose and p["source_hint"]:
                print(f"       查：{p['source_hint']}")
            if args.verbose and p["note"]:
                print(f"       ↳ {p['note']}")
        tags = tags_of(pos)
        if tags:
            print(f"  標籤：{'、'.join(tags)}")
        print()

    if len(items) > args.limit:
        print(f"（另有 {len(items) - args.limit} 筆未顯示，用 --limit 調整）")


def cmd_position_due(args):
    """列出到期該判定的預測。

    兩種到期：明確標了 due_date 且已過，或放超過 POSITION_MIN_AGE_DAYS。
    後者是保底——沒填 due_date 的預測若不主動列出，就會永遠停在未判定，
    而未判定的預測不進命中率，等於默默地把不利的結果排除在統計外。
    """
    conn = connect()
    items = load_positions(conn, args.ticker, pending_only=True)
    conn.close()
    today = today_local().isoformat()

    due = []
    for pos, preds in items:
        for p in preds:
            if p["verdict"] is not None:
                continue
            age = _days_between(pos["obs_date"], today)
            by_date = p["due_date"] and p["due_date"] <= today
            if by_date or age >= args.min_age:
                due.append((pos, p, age, bool(by_date)))

    if not due:
        print(f"（沒有到期的預測；未滿 {args.min_age} 天且未標到期日的不列出）")
        return

    due.sort(key=lambda x: (not x[3], -x[2]))
    print(f"到期待判定 {len(due)} 條")
    print(f"判定後用：news.py position-verify <預測id> "
          f"<{'|'.join(POSITION_VERDICTS)}> [--note ...]\n")
    for pos, p, age, by_date in due[:args.limit]:
        why = f"到期日 {p['due_date']}" if by_date else f"已放 {age} 天"
        print(f"#{p['id']} {pos['ticker']} [{kind_label_of(p['kind'])}] ({why})")
        print(f"   {p['text']}")
        if p["source_hint"]:
            print(f"   查：{p['source_hint']}")
        print(f"   觀點 #{pos['id']}（{pos['obs_date']}）：{pos['thesis'][:50]}")
        print()


def cmd_position_verify(args):
    """記錄一條投資預測的判定結果。"""
    conn = connect()
    row = conn.execute(
        "SELECT p.*, o.ticker, o.obs_date FROM predictions p "
        "JOIN positions o ON o.id = p.position_id WHERE p.id = ?",
        (args.pred_id,),
    ).fetchone()
    if not row:
        conn.close()
        sys.exit(f"找不到 id={args.pred_id} 的預測（用 news.py position-due 查看）")
    if row["verdict"] is not None and not args.force:
        conn.close()
        sys.exit(
            f"#{args.pred_id} 已判定為 {row['verdict']}"
            f"（{row['verified_date']}）\n"
            "  改判定要加 --force。事後改判定會讓命中率失去意義，"
            "只在確認當初判錯時才用。"
        )
    conn.execute(
        "UPDATE predictions SET verdict = ?, note = ?, verified_date = ? WHERE id = ?",
        (args.verdict, args.note, today_local().isoformat(), args.pred_id),
    )
    conn.commit()
    conn.close()
    print(f"已記錄 [{args.verdict}] {row['ticker']} #{args.pred_id} {row['text'][:50]}")


def position_accuracy(conn):
    """投資預測的命中率，依預測類型分組。

    分組刻意用 kind 而非標的或等級：各類的可驗證程度差異極大
    （見 PREDICTION_KINDS 的說明），混算會得到無法行動的數字。

    **void 完全排除在統計外**（連 counts 都不進）——它代表「這條當初就不該
    這樣寫」，留在報表裡只會讓人以為那是一種判定結果。moot 則仍計入 counts
    但不進分母，因為「前提消失」本身是關於世界的資訊，值得看見。
    """
    preds = [
        p for p in conn.execute(
            "SELECT id, kind, verdict FROM predictions WHERE verdict IS NOT NULL")
        if p["verdict"] != "void"
    ]
    verified = {(p["id"], 0): p for p in preds}
    kind_by_id = {p["id"]: p["kind"] for p in preds}
    acc = accuracy_by(
        verified,
        key_of=lambda k, _v: k[0],
        group_of=lambda pid: kind_by_id.get(pid),
    )
    voided = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE verdict = 'void'").fetchone()[0]
    return {"overall": acc["overall"], "by_kind": acc["by_group"], "void": voided}


def cmd_position_stats(_args):
    """投資預測的命中率統計。"""
    conn = connect()
    acc = position_accuracy(conn)
    pending = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE verdict IS NULL").fetchone()[0]
    conn.close()

    c = acc["overall"]["counts"]
    total = sum(c.values())
    void_note = (f"　另有 {acc['void']} 條作廢（void，不列入統計）"
                 if acc["void"] else "")
    if not total:
        print("（還沒有任何投資預測判定，用 news.py position-due 開始）")
        if pending:
            print(f"  目前有 {pending} 條預測尚未判定")
        if acc["void"]:
            print(f"  {acc['void']} 條已作廢（當初寫成無法驗證的形式）")
        return

    judged = c["hit"] + c["miss"]
    print(f"投資預測命中率：已判定 {total} 條"
          f"（hit {c['hit']}、miss {c['miss']}、moot {c['moot']}）{void_note}")
    if acc["overall"]["rate"] is None:
        print("  尚無可計算命中率的條目（moot 不列入分母）")
    else:
        print(f"  命中率 {acc['overall']['rate']:.0%}"
              f"（{c['hit']}/{judged}，moot 不列入分母）")
    if judged < 20:
        print(f"  ⚠️  樣本僅 {judged} 條，結論參考價值有限（建議 20 條以上）")
    if pending:
        print(f"  另有 {pending} 條尚未判定")

    if acc["by_kind"]:
        print("\n依預測類型")
        for k, label, _desc in PREDICTION_KINDS:
            if k not in acc["by_kind"]:
                continue
            kc = acc["by_kind"][k]
            kj = kc["counts"]["hit"] + kc["counts"]["miss"]
            if not kj:
                continue
            print(f"  {label}  命中率 {kc['rate']:.0%}（{kc['counts']['hit']}/{kj}）")
        print("\n  兩類要分開看：基本面有客觀數字可查，結構要人判讀事件是否發生。")
        print("  基本面命中但結構落空，代表數字對了但推論的機制沒發生。")


def cmd_position_schema(_args):
    """輸出 add-position 接受的 JSON 格式。

    與 cmd_schema 同一個理由：格式由常數生成，不另抄一份說明。
    """
    kinds = "\n".join(
        f'    "{k}"{" " * (12 - len(k))}— {label}：{desc}'
        for k, label, desc in PREDICTION_KINDS)
    print(f"""add-position 接受的 JSON：

{{
  "ticker":     "TSM",              // 必填。自動轉大寫（台股用代號如 "2330"）
  "name":       "台積電",            // 選填
  "market":     "US",               // 選填（US / TW）
  "obs_date":   "{today_local().isoformat()}",       // 選填，預設今天。YYYY-MM-DD 補零
  "thesis":     "一句話的判斷",        // 必填。沒有推論的預測事後無從檢討
  "rationale":  "為什麼這樣想",        // 選填但強烈建議：這是事後檢討時最有價值的欄位
  "source_url": "https://...",      // 選填，觸發這個判斷的資料來源
  "tags":       ["台積電", "CoWoS"],  // 選填，最多 {MAX_TAGS} 個，套用與新聞相同的別名表
  "predictions": [                  // 必填，至少一條
    {{
      "kind":        "fundamental",  // 必填，見下
      "text":        "8 月營收年增 >30%",   // 必填。要能明確判定 hit/miss
      "source_hint": "公開資訊觀測站月營收",  // 必填！見下
      "due_date":    "2026-09-10"    // 選填。沒填則放滿 {POSITION_MIN_AGE_DAYS} 天後列入待判定
    }}
  ]
}}

predictions.kind 的兩類：
{kinds}

  ⚠️ 刻意沒有「市場類」（股價、相對報酬）。2026-07-31 廢除：
     (1) 沒有資料源——feeds.txt 只有中央社／BBC／科技新報，價格資訊不在系統裡，
         實測 7 條市場類預測全部無法判定，連 miss 都算不上；
     (2) 更根本的是它測不出判斷力——股價結果混雜利率、地緣、大盤情緒，
         推論正確與否只佔很小部分。
     想測「資訊是否已被 price in」，用基本面預測搭配新聞觀察即可。

source_hint（必填）：這條到期時**去哪裡查**。
  可執行：台積電法說會簡報／公開資訊觀測站月營收／三星財報 DS 部門別營業利益
  等於沒填：財報、新聞、市場數據（CLI 會擋下）
  **寫不出具體來源，代表這條預測現在就該重寫**——那 7 條被廢除的市場類
  預測寫得很工整，問題正是沒人在寫的當下問過「這個數字從哪來」。

判定值域：{'/'.join(POSITION_VERDICTS)}
  hit  — 明確發生了
  miss — 到期但沒發生
  moot — 前提消失，無從判斷（不列入分母，但仍顯示——那是關於世界的資訊）
  void — 當初就寫成無法驗證的形式（完全排除在統計外，是我的問題不是世界的）

寫預測的原則（沿用 watch_next 的實測結果）：
  可驗證性比精確度重要。「營收年增 >30%」可判定，「營收表現不錯」不可判定。
  實測「機制延續型」命中率 48%、「特定事件型」29%、「來源不涵蓋型」僅 15%。""")


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
    immature = sum(1 for r in rows if r["id"] in stats and not stats[r["id"]]["mature"])
    print(f"評分回顧：觀察窗口 {args.window} 天，"
          f"已走完窗口的有 {len(mature)} 則"
          f"（另有 {immature} 則窗口未滿，不納入）")
    # --since 的過濾要在報完全體數字之後——否則標頭的「已走完 N 則」是篩過的
    # 子集，而「另有 M 則未滿」是全體，兩個數字基準不同卻並列在同一句話裡。
    if args.since:
        mature = [r for r in mature if r["news_date"] >= args.since]
        print(f"　　　　　{args.since} 起：{len(mature)} 則")
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
        tie_note = f"，{c['ties']} 則同為 {c['median']} 分未列入" if c.get("ties") else ""
        print(f"  {c['label']}（滿分 {c['limit']}，以 {c['median']} 分切組{tie_note}）")
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
    # 預設 127.0.0.1（只有本機看得到）。要用手機看才加 --host 0.0.0.0，
    # 那會讓同網段的所有裝置都能開啟，包含 /positions 的投資判斷。
    p_serve.add_argument(
        "--host", default="127.0.0.1",
        help="綁定位址。預設只有本機可連；用手機看建議設 tailscale"
             "（只有自己的裝置連得到），或 0.0.0.0（同網段皆可見）")

    p_fetch = sub.add_parser("fetch", help="抓取 RSS，把新連結存入待評分清單")
    p_fetch.add_argument("--feeds", default=str(FEEDS_PATH), help="feed 清單檔（預設 feeds.txt）")
    p_fetch.add_argument("--limit", type=int, default=40, help="每個 feed 最多取幾則（預設 40）")

    p_pending = sub.add_parser("pending", help="列出待評分清單")
    p_pending.add_argument("--all", action="store_true", help="包含已評分的項目")
    p_pending.add_argument("--json", action="store_true", help="以 JSON 輸出（給批次評分用）")
    p_pending.add_argument("--limit", type=int, help="最多列出幾則")

    p_skip = sub.add_parser("skip", help="把待評分項目標為略過")
    p_skip.add_argument("ids", nargs="+", type=int)

    p_drift = sub.add_parser(
        "drift", help="偵測評分標準是否隨時間漂移（review 的前提）")
    p_drift.add_argument("--split", help="切分日期（YYYY-MM-DD，預設取中位日）")
    p_drift.add_argument("--limit", type=int, default=8, help="每個面向最多列幾個標籤")

    p_review = sub.add_parser(
        "review", help="評分回顧校準：用後續資料檢驗判讀是否準確")
    p_review.add_argument(
        "--window", type=int, default=REVIEW_WINDOW_DAYS,
        help=f"觀察窗口天數（預設 {REVIEW_WINDOW_DAYS}）")
    p_review.add_argument("--since", help="只回顧此日期之後評的（YYYY-MM-DD）")
    p_review.add_argument("--limit", type=int, default=10, help="每個清單最多列幾則")

    p_cal = sub.add_parser(
        "calibrate", help="比對某日評分與錨點期的標準（漂移的日常粗篩）")
    p_cal.add_argument("--date", help="要檢查的日期（預設取最新評分日）")

    p_watch = sub.add_parser(
        "watch", help="列出當天新聞可能命中的舊 watch_next（批次評分時順手判定）")
    p_watch.add_argument("--date", help="以哪天的新聞回頭比對（預設取最新評分日）")
    p_watch.add_argument(
        "--min-age", type=int, default=WATCH_MIN_AGE_DAYS, dest="min_age",
        help=f"指標至少要放多少天才列出（預設 {WATCH_MIN_AGE_DAYS}）")
    p_watch.add_argument("--limit", type=int, default=15, help="最多列幾條")
    p_watch.add_argument("--json", action="store_true", help="輸出 JSON")

    p_wv = sub.add_parser("watch-verify", help="記錄一條 watch_next 的判定結果")
    p_wv.add_argument("url", help="該則新聞的 url")
    p_wv.add_argument("idx", type=int, help="watch_next 陣列中的序號（0 起算）")
    p_wv.add_argument("verdict", choices=WATCH_VERDICTS,
                      help="hit=發生了／miss=窗口過了沒發生／moot=前提消失無從判斷")
    p_wv.add_argument("--note", help="判定說明")
    p_wv.add_argument("--evidence", help="佐證報導的網址")

    p_ws = sub.add_parser("watch-stats", help="watch_next 命中率統計")

    sub.add_parser(
        "anchors", help="重新產生評分的固定錨點表（核對 skill 是否脫節）")

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

    # ── 投資觀察 ──
    p_ap = sub.add_parser(
        "add-position", help="新增一次投資觀點（JSON 檔或 - 表示 stdin）")
    p_ap.add_argument("file")

    p_pos = sub.add_parser("positions", help="列出投資觀點與預測狀態")
    p_pos.add_argument("ticker", nargs="?", help="只看某個標的")
    p_pos.add_argument("--pending", action="store_true", help="只列出還有未判定預測的")
    p_pos.add_argument("--limit", type=int, default=20, help="最多列幾筆（預設 20）")
    p_pos.add_argument("-v", "--verbose", action="store_true", help="顯示推論依據與判定說明")

    p_pdue = sub.add_parser("position-due", help="列出到期該判定的投資預測")
    p_pdue.add_argument("ticker", nargs="?", help="只看某個標的")
    p_pdue.add_argument(
        "--min-age", type=int, default=POSITION_MIN_AGE_DAYS, dest="min_age",
        help=f"沒標到期日的至少放幾天才列出（預設 {POSITION_MIN_AGE_DAYS}）")
    p_pdue.add_argument("--limit", type=int, default=20, help="最多列幾條")

    p_pv = sub.add_parser("position-verify", help="記錄一條投資預測的判定")
    p_pv.add_argument("pred_id", type=int, help="預測 id（position-due 會顯示）")
    p_pv.add_argument("verdict", choices=POSITION_VERDICTS,
                      help="hit=發生了 / miss=沒發生 / moot=前提消失 / "
                           "void=當初寫成無法驗證的形式")
    p_pv.add_argument("--note", help="判定說明")
    p_pv.add_argument("--force", action="store_true", help="覆寫已有的判定")

    sub.add_parser("position-stats", help="投資預測命中率統計")
    sub.add_parser("position-schema", help="輸出 add-position 的 JSON 格式")

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
        "drift": cmd_drift,
        "review": cmd_review,
        "watch": cmd_watch,
        "watch-verify": cmd_watch_verify,
        "watch-stats": cmd_watch_stats,
        "anchors": cmd_anchors,
        "calibrate": cmd_calibrate,
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
        "add-position": cmd_add_position,
        "positions": cmd_positions,
        "position-due": cmd_position_due,
        "position-verify": cmd_position_verify,
        "position-stats": cmd_position_stats,
        "position-schema": cmd_position_schema,
    }[args.command](args)


if __name__ == "__main__":
    main()
