#!/usr/bin/env python3
"""news.py / server.py 的回歸測試（標準庫 unittest，無外部依賴）。

執行：python3 -m unittest test_news -v

涵蓋的是「已經出過錯」的地方，而非追求覆蓋率：
- news_date 格式驗證（保留期用字面比較，未補零會被歸到錯誤層級）
- 保留期分層只在 export --retention 生效，不影響 db 與 JSON
- 匯出／匯入 round-trip 無損
- 靜態站產出的卡片數與資料筆數一致
"""

import importlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).parent


def make_score(title, news_date=None, url=None, scores=(10, 10, 10, 10, 9),
               summary=None, one_line=None, section="影響未來的趨勢", tags=None,
               watch_next=None):
    """產生一筆符合 add 格式的評分 JSON。預設總分 49（C 級）。

    summary / one_line 可個別指定：搜尋是跨這幾個欄位比對的，若全部用同一份
    固定字串，測不出「某個欄位漏掉」這種單邊改動。
    """
    keys = ["scope", "duration", "decision", "structural", "credibility"]
    return {
        "title": title,
        "url": url or f"http://example.com/{title}",
        "news_date": news_date,
        "summary": summary or f"{title}的摘要",
        "section": section,
        "tags": tags or [],
        "one_line": one_line or f"{title}的判斷",
        "why_important": "原因",
        "affected": "對象",
        "watch_next": watch_next or ["指標"],
        "dimensions": {
            k: {"score": s, "reason": "理由"} for k, s in zip(keys, scores)
        },
    }


class CLITestCase(unittest.TestCase):
    """在暫存目錄跑真正的 CLI，避免污染實際的 news.db。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for f in ("news.py", "server.py"):
            (self.dir / f).write_bytes((REPO / f).read_bytes())
        self.run_cli("init")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=None, check=False):
        return subprocess.run(
            [sys.executable, "news.py", *args],
            cwd=self.dir, input=stdin, capture_output=True, text=True, check=check,
        )

    def add(self, payload):
        return self.run_cli("add", "-", stdin=json.dumps(payload, ensure_ascii=False))

    def db_count(self):
        conn = sqlite3.connect(self.dir / "news.db")
        n = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        conn.close()
        return n

    def json_rows(self):
        p = self.dir / "data" / "news.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


@contextmanager
def load_modules(directory, *names):
    """import 指定目錄裡的模組，離開時還原 sys.path。

    需要 reload 是因為 sys.modules 會快取前一個測試載入的同名模組；
    直接 import 會拿到別的暫存目錄那份。finally 不可省——setUp 中途拋錯時
    若沒還原 sys.path，後續測試會 import 到已刪除的暫存目錄。
    """
    sys.path.insert(0, str(directory))
    try:
        yield tuple(importlib.reload(importlib.import_module(n)) for n in names)
    finally:
        sys.path.remove(str(directory))


class TestNewsDateValidation(CLITestCase):
    """news_date 會被保留期以字串字面比較，格式必須是正規的 YYYY-MM-DD。"""

    def test_rejects_unpadded_month_and_day(self):
        # 迴歸：'2026-7-5' 字面上大於 '2026-06-25'，會被誤判成近 30 天內。
        # strptime 本身接受這種寫法，所以必須額外比對正規化後的字串。
        for bad in ("2026-7-5", "2026-07-5", "2026-7-05"):
            with self.subTest(bad):
                # url 帶入 bad 以免各輪撞成同一筆，讓失敗原因變成「連結重複」
                r = self.add(make_score("t", bad, url=f"http://e.com/{bad}"))
                self.assertNotEqual(r.returncode, 0, f"{bad} 應被拒絕")
                self.assertIn("補零", r.stderr + r.stdout)
                self.assertEqual(self.db_count(), 0, "被拒絕的資料不該寫入 db")

    def test_rejects_malformed(self):
        for bad in ("20260705", "2026/07/05", "2026-13-01", "2026-02-30", "abc"):
            with self.subTest(bad):
                r = self.add(make_score("t", bad, url=f"http://e.com/{bad}"))
                self.assertNotEqual(r.returncode, 0, f"{bad} 應被拒絕")
                self.assertIn("格式不正確", r.stderr + r.stdout)
                self.assertEqual(self.db_count(), 0)

    def test_rejects_future_date(self):
        # 基準必須用台北時間，不能用 date.today()（取執行環境時區）。
        # 迴歸：CI 的 runner 是 UTC，台灣時間上午 8 點前跑時「UTC 的明天」
        # 其實已經是「台北的今天」，驗證正確放行卻讓測試誤判失敗。
        # 加 2 天而非 1 天，讓測試在任何時區下都明確落在未來。
        with load_modules(self.dir, "news") as (news,):
            future = (news.today_local() + timedelta(days=2)).isoformat()
        r = self.add(make_score("t", future))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未來日期", r.stderr + r.stdout)

    def test_accepts_valid_and_empty(self):
        # 空值代表「日期不明」，是允許的；保留期會一律保留這類資料
        for ok in (date.today().isoformat(), "2026-07-05", None, ""):
            with self.subTest(ok):
                r = self.add(make_score(f"t{ok}", ok, url=f"http://e.com/{ok}"))
                self.assertEqual(r.returncode, 0, r.stderr)


class TestExportJsonIsFullMirror(CLITestCase):
    """data/news.json 必須是 db 的完整鏡像，否則 import-json --replace 會毀資料。"""

    def test_add_exports_all_rows_regardless_of_age(self):
        old = (date.today() - timedelta(days=200)).isoformat()
        self.add(make_score("舊", old, url="http://e.com/1"))
        self.add(make_score("新", date.today().isoformat(), url="http://e.com/2"))
        self.assertEqual(self.db_count(), 2)
        self.assertEqual(len(self.json_rows()), 2, "過期資料仍須進 JSON")

    def test_add_of_old_entry_succeeds(self):
        # 迴歸：曾因保留期防呆寫在 export_news_json，導致 add 寫入成功卻 exit 1
        old = (date.today() - timedelta(days=200)).isoformat()
        r = self.add(make_score("舊", old))
        self.assertEqual(r.returncode, 0, f"補評舊新聞不該失敗：{r.stderr}")
        self.assertEqual(len(self.json_rows()), 1)

    def test_roundtrip_is_lossless(self):
        # 迴歸：JSON 若是子集，import-json --replace 會永久刪掉 db 內的資料
        old = (date.today() - timedelta(days=200)).isoformat()
        self.add(make_score("舊", old, url="http://e.com/1"))
        self.add(make_score("新", date.today().isoformat(), url="http://e.com/2"))
        before = self.db_count()
        self.run_cli("import-json", "--replace", check=True)
        self.assertEqual(self.db_count(), before, "round-trip 不該掉資料")

    def test_export_is_pure_function_of_db(self):
        """同一份 db 重複匯出結果必須相同（不依賴當下日期）。"""
        self.add(make_score("a", (date.today() - timedelta(days=40)).isoformat()))
        first = (self.dir / "data" / "news.json").read_text(encoding="utf-8")
        self.run_cli("export-json", check=True)
        second = (self.dir / "data" / "news.json").read_text(encoding="utf-8")
        self.assertEqual(first, second)


class TestRetention(CLITestCase):
    """保留期只在 export --retention 生效。"""

    def setUp(self):
        super().setUp()
        today = date.today()
        # 近 30 天內：不分等級都該保留
        self.add(make_score("近B", (today - timedelta(days=5)).isoformat(),
                            url="http://e.com/1"))
        # 30-90 天：只有 S/A 該保留。20/16/15/16/12 = 79 → A 級
        self.add(make_score("中A", (today - timedelta(days=50)).isoformat(),
                            url="http://e.com/2", scores=(20, 16, 15, 16, 12)))
        self.add(make_score("中C", (today - timedelta(days=50)).isoformat(),
                            url="http://e.com/3"))
        # 超過 90 天：一律不上站
        self.add(make_score("遠A", (today - timedelta(days=200)).isoformat(),
                            url="http://e.com/4", scores=(20, 16, 15, 16, 12)))

    def cards_in(self, out):
        html = (self.dir / out / "index.html").read_text(encoding="utf-8")
        return html.count('<div class="card ')

    def test_local_export_keeps_everything(self):
        self.run_cli("export", "--out", "d1", check=True)
        self.assertEqual(self.cards_in("d1"), 4, "不帶旗標時應輸出全部")

    def test_retention_export_applies_tiers(self):
        self.run_cli("export", "--out", "d2", "--retention", check=True)
        html = (self.dir / "d2" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(self.cards_in("d2"), 2, "應只剩「近B」與「中A」")
        self.assertIn("近B", html)
        self.assertIn("中A", html)
        self.assertNotIn("中C", html, "30-90 天的非 S/A 不該上站")
        self.assertNotIn("遠A", html, "超過 90 天一律不上站")

    def test_retention_does_not_touch_db_or_json(self):
        self.run_cli("export", "--out", "d3", "--retention", check=True)
        self.assertEqual(self.db_count(), 4, "輸出網站不該動到 db")
        self.assertEqual(len(self.json_rows()), 4, "輸出網站不該動到 JSON")

    def test_tab_counts_match_visible_cards(self):
        """分頁計數與實際卡片數必須一致（四個查詢都要套用保留期）。"""
        self.run_cli("export", "--out", "d4", "--retention", check=True)
        html = (self.dir / "d4" / "index.html").read_text(encoding="utf-8")
        total = re.search(r">全部 (\d+)<", html)
        self.assertIsNotNone(total)
        self.assertEqual(int(total.group(1)), self.cards_in("d4"))

    def test_aborts_when_everything_expired(self):
        """全部過期時應中止，而非部署一個空網站。"""
        r = self.run_cli("export", "--out", "d5", "--retention")
        # setUp 的資料都在保留期內，先換成一份全過期的 db
        conn = sqlite3.connect(self.dir / "news.db")
        conn.execute("UPDATE news SET news_date = '2020-01-01'")
        conn.commit()
        conn.close()
        r = self.run_cli("export", "--out", "d6", "--retention")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("中止", r.stderr + r.stdout)
        self.assertFalse((self.dir / "d6" / "index.html").exists(),
                         "中止時不該留下產出")


class TestFilterParity(CLITestCase):
    """動態站（SQL 篩選）與靜態站（前端 JS 篩選）的行為必須一致。

    兩者共用 render_card，但篩選各自實作：serve 走 query string 打 SQL，
    靜態站一次輸出全部卡片、用 data-* 屬性在前端切換顯示。這裡把靜態站的
    篩選條件用 Python 重跑一次，斷言它與 SQL 篩出來的結果相同——
    避免「改了一邊忘了另一邊」只能靠註解提醒。
    """

    def setUp(self):
        super().setUp()
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=3)).isoformat()
        # 涵蓋不同等級、日期、分類，讓各種篩選都有區辨力。
        # 「僅摘要詞」「僅判斷詞」只出現在單一欄位，用來驗證搜尋確實跨欄位比對。
        # 標籤刻意設計成「AI」與「AI晶片」並存：整值比對若退化成子字串比對，
        # 篩「AI」會多撈到「AI晶片」的那則，兩邊就會不一致。
        self.add(make_score("台積電法說會", today, url="http://e.com/1",
                            scores=(20, 16, 15, 16, 12),
                            tags=["台積電", "AI晶片"],
                            summary="僅摘要詞 出現在這裡"))          # 79 → A
        self.add(make_score("關稅生效", today, url="http://e.com/2",
                            scores=(24, 19, 19, 19, 14),
                            tags=["關稅", "AI"],
                            one_line="僅判斷詞 出現在這裡"))          # 95 → S
        self.add(make_score("天氣預報", old, url="http://e.com/3",
                            section="熱但未必重要"))                 # 49 → C（無標籤）
        self.add(make_score("關稅談判", old, url="http://e.com/4",
                            tags=["關稅"],
                            scores=(15, 12, 12, 12, 10)))          # 61 → B

    def sql_filter(self, **kw):
        """動態站的篩選結果（標題集合）。"""
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            return {r["title"] for r in server.query_news(**kw)}

    def js_filter(self, grade=None, date_=None, section=None, q=None, tag=None):
        """複刻 FILTER_JS 的 apply()，對靜態站輸出的 data-* 做同樣篩選。"""
        self.run_cli("export", "--out", "d", check=True)
        html = (self.dir / "d" / "index.html").read_text(encoding="utf-8")
        with load_modules(self.dir, "server") as (server,):
            sep = server.TAG_SEP
        titles = set()
        for m in re.finditer(
            r'data-grade="([^"]*)" data-date="([^"]*)" data-section="([^"]*)"'
            r' data-tags="([^"]*)" data-text="([^"]*)">.*?'
            r'<div class="title">(?:<a[^>]*>)?([^<]*)',
            html, re.S,
        ):
            g, d, sec, tags, text, title = m.groups()
            # 與 FILTER_JS 的 base 條件逐項對應
            if grade and g != grade:
                continue
            if date_ and d != date_:
                continue
            if section and sec != section:
                continue
            # 對應 JS 的 indexOf(SEP + tag + SEP)：整值比對而非子字串
            if tag and (sep + tag + sep) not in tags:
                continue
            if q and q.lower() not in text:
                continue
            titles.add(title)
        return titles

    def assert_parity(self, **kw):
        js_kw = {"grade": kw.get("grade"), "date_": kw.get("date"),
                 "section": kw.get("section"), "q": kw.get("q"),
                 "tag": kw.get("tag")}
        self.assertEqual(self.sql_filter(**kw), self.js_filter(**js_kw),
                         f"動態站與靜態站篩選結果不一致：{kw}")

    def test_no_filter(self):
        self.assert_parity()

    def test_by_grade(self):
        for g in ("S", "A", "B", "C"):
            with self.subTest(g):
                self.assert_parity(grade=g)

    def test_by_date(self):
        self.assert_parity(date=date.today().isoformat())
        self.assert_parity(date=(date.today() - timedelta(days=3)).isoformat())

    def test_by_section(self):
        self.assert_parity(section="影響未來的趨勢")

    def test_by_search(self):
        # 「關稅」同時命中 S 與 B 級，可驗證搜尋不受等級影響
        self.assert_parity(q="關稅")
        self.assert_parity(q="台積電")

    def test_search_covers_every_field(self):
        """搜尋須跨標題／一句話判斷／摘要，少比對任一欄位都要被抓到。"""
        for q, expected in [
            ("台積電法說會", {"台積電法說會"}),   # 命中標題
            ("僅判斷詞", {"關稅生效"}),           # 只在 one_line
            ("僅摘要詞", {"台積電法說會"}),        # 只在 summary
        ]:
            with self.subTest(q):
                self.assertEqual(self.sql_filter(q=q), expected,
                                 f"動態站搜尋「{q}」結果不如預期")
                self.assert_parity(q=q)

    def test_by_tag(self):
        for t in ("關稅", "台積電", "AI", "AI晶片"):
            with self.subTest(t):
                self.assert_parity(tag=t)

    def test_tag_match_is_exact_not_substring(self):
        """篩「AI」不得命中「AI晶片」——兩邊都必須是整值比對。

        迴歸防線：SQL 端若圖方便改用 LIKE '%AI%'、前端若少了分隔字元，
        這裡就會抓到（fixture 刻意讓兩個標籤有前綴包含關係）。
        """
        self.assertEqual(self.sql_filter(tag="AI"), {"關稅生效"})
        self.assertEqual(self.js_filter(tag="AI"), {"關稅生效"})
        self.assertEqual(self.sql_filter(tag="AI晶片"), {"台積電法說會"})
        self.assertEqual(self.js_filter(tag="AI晶片"), {"台積電法說會"})

    def test_tag_filter_is_pure_exact_match(self):
        """query_news 只做整值比對，不自己收斂別名。

        別名收斂屬於輸入處理層（Handler.do_GET），不是篩選層——篩選層若偷偷
        多做一層，動態站就會比靜態站多認得一種寫法，而 assert_parity 兩邊
        傳入同一個字串、發現不了「其中一邊多做了事」。
        """
        with load_modules(self.dir, "news", "server") as (news, server):
            news.DB_PATH = self.dir / "news.db"
            server.DB_PATH = self.dir / "news.db"
            conn = news.connect()
            conn.execute("INSERT OR REPLACE INTO tag_aliases (alias, canonical)"
                         " VALUES (?, ?)", ("台積", "台積電"))
            conn.commit()
            conn.close()
            self.assertEqual(server.query_news(tag="台積"), [],
                             "篩選層不該自己套別名，否則與靜態站行為不一致")
            self.assertEqual(
                {r["title"] for r in server.query_news(tag="台積電")},
                {"台積電法說會"})

    def test_url_layer_resolves_alias(self):
        """?tag=別名 仍要能用——收斂發生在 do_GET，而非篩選層。"""
        with load_modules(self.dir, "news", "server") as (news, server):
            news.DB_PATH = self.dir / "news.db"
            server.DB_PATH = self.dir / "news.db"
            conn = news.connect()
            conn.execute("INSERT OR REPLACE INTO tag_aliases (alias, canonical)"
                         " VALUES (?, ?)", ("台積", "台積電"))
            conn.commit()
            aliases = news.load_aliases(conn)
            conn.close()
            # do_GET 對 tag 做的處理就是這一步
            self.assertEqual(news.normalize_tag("台積", aliases), "台積電")

    def test_combined(self):
        self.assert_parity(grade="S", q="關稅")
        self.assert_parity(date=date.today().isoformat(), section="影響未來的趨勢")
        self.assert_parity(tag="關稅", grade="S")


class TestSchemaCommand(CLITestCase):
    """`news.py schema` 是 JSON 格式的唯一出處，必須由常數生成而非手抄。"""

    def test_reflects_dimension_limits(self):
        r = self.run_cli("schema", check=True)
        with load_modules(self.dir, "news") as (news,):
            for key, label, mx in news.DIMENSIONS:
                self.assertIn(f"{label}（0-{mx}）", r.stdout,
                              f"schema 未反映 {key} 的上限 {mx}")
            for sec in news.SECTIONS:
                self.assertIn(sec, r.stdout, f"schema 缺少 section「{sec}」")

    def test_reflects_grade_thresholds(self):
        """schema 印的門檻曾經是手抄的，改 grade_of() 不會同步到對外說明。"""
        r = self.run_cli("schema", check=True)
        with load_modules(self.dir, "news") as (news,):
            for grade, low in news.GRADE_THRESHOLDS:
                self.assertIn(f"{low}+ {grade}", r.stdout,
                              f"schema 未反映 {grade} 級門檻 {low}")
            self.assertIn(f"其餘 {news.FALLBACK_GRADE}", r.stdout)

    def test_documents_validation_rules(self):
        """schema 要講到實際會擋人的規則，否則使用者只能踩到才知道。"""
        r = self.run_cli("schema", check=True)
        for rule in ("補零", "未來日期", "--force"):
            self.assertIn(rule, r.stdout, f"schema 未說明「{rule}」相關規則")

    def test_output_is_valid_json_shape(self):
        """輸出的範例必須是合法 JSON，否則照抄會失敗。"""
        r = self.run_cli("schema", check=True)
        m = re.search(r"\{.*\}", r.stdout, re.S)
        self.assertIsNotNone(m, "找不到 JSON 區塊")
        json.loads(m.group(0))  # 解析失敗即測試失敗


class TestNoDuplicateConstants(CLITestCase):
    """CLAUDE.md 保證 schema 常數只有 news.py 一份，這裡讓它變成會失敗的測試。

    server.py 曾經自己抄一份 DIMENSIONS / GRADE_LABELS。兩份同值時一切正常，
    只改其中一份也不會報錯——網頁只是靜默地按舊上限畫分數條，沒有東西會叫。
    """

    def test_server_shares_news_constants(self):
        """比對身分而非值：抄一份同值的複本也要失敗，否則測不到真正的重複。"""
        with load_modules(self.dir, "news", "server") as (news, server):
            for name in ("DIMENSIONS", "GRADE_LABELS", "GRADES", "DB_PATH"):
                self.assertIs(getattr(server, name), getattr(news, name),
                              f"server.{name} 不是 news.{name}，應 import 而非另存一份")

    def test_server_defines_no_shadowing_constant(self):
        """規則本身：server.py 不得自行定義任何與 news.py 同名的常數。

        上面那個測試只認得列舉出來的四個名字；新增第五個共用常數時，
        那份手寫清單自己就會漂移——正是它要防的問題。改掃描原始碼，
        任何未來的重複定義都會被擋下，不必記得回來補清單。
        """
        with load_modules(self.dir, "news") as (news,):
            shared = {n for n in vars(news) if n.isupper() and not n.startswith("_")}
        src = (self.dir / "server.py").read_text(encoding="utf-8")
        # 逐行比對而非 assertNotRegex：後者失敗時會把整個 server.py 印進錯誤訊息
        assigned = {
            m.group(1)
            for line in src.splitlines()
            if (m := re.match(r"([A-Z_][A-Z0-9_]*)\s*=", line))
        }
        self.assertEqual(
            assigned & shared, set(),
            "server.py 自行定義了 news.py 已有的常數，應改為 import 自 news.py")

    def test_server_defines_no_shadowing_function(self):
        """同一條規則，但涵蓋函式。

        迴歸：`tag_counts` 一度在 news.py 與 server.py 各有一份實作，兩邊
        逐字相同、只差取資料的來源。上面那個測試只掃大寫的常數賦值，
        重複的函式定義整個穿過去了——而函式比常數更容易悄悄漂移
        （改了排序規則卻只改一邊，CLI 與網頁就會給出不同順序）。
        """
        with load_modules(self.dir, "news") as (news,):
            shared = {
                n for n, v in vars(news).items()
                if callable(v) and getattr(v, "__module__", None) == "news"
            }
        src = (self.dir / "server.py").read_text(encoding="utf-8")
        defined = {
            m.group(1)
            for line in src.splitlines()
            if (m := re.match(r"def ([a-zA-Z_][a-zA-Z0-9_]*)", line))
        }
        self.assertEqual(
            defined & shared, set(),
            "server.py 自行定義了 news.py 已有的函式，應改為 import 自 news.py")

    def test_no_naive_now_or_today(self):
        """時間一律走 now_local()／today_local()（台北時間），不得用執行環境時區。

        迴歸：export 有兩種執行環境——本機（CST）與 CI 的 Ubuntu runner（UTC）。
        `datetime.now()` 在兩邊給出不同基準，網頁的「更新於」因此一下 CST
        一下 UTC，看起來像時間倒退（本機 15:36 的下一版是 CI 的 22:52，
        實際是隔天早上 6:52）。`date.today()` 更麻煩：它是保留期的基準日，
        台灣上午 8 點前跑 CI 會拿到「昨天」，30 天界線整個挪一天。

        掃描原始碼而非行為比對——這種錯誤是「寫了就壞」，要擋在寫入當下。
        """
        banned = ("datetime.now()", "date_cls.today()", "datetime.utcnow()")
        for fname in ("news.py", "server.py"):
            src = (self.dir / fname).read_text(encoding="utf-8")
            for lineno, line in enumerate(src.splitlines(), 1):
                code = line.split("#", 1)[0]  # 註解裡提到這些名字是說明，不算違規
                for bad in banned:
                    self.assertNotIn(
                        bad, code,
                        f"{fname}:{lineno} 使用了 {bad}，應改用 news.now_local()"
                        "／news.today_local()（台北時間）")

    def test_local_time_is_taipei(self):
        """now_local()／today_local() 必須真的是 UTC+8，而非只是換個名字。"""
        with load_modules(self.dir, "news") as (news,):
            now = news.now_local()
            self.assertIsNotNone(now.tzinfo, "now_local() 應回傳帶時區的時間")
            self.assertEqual(now.utcoffset(), timedelta(hours=8),
                             "now_local() 應為台北時間（UTC+8）")
            # today_local() 要跟 now_local() 同基準，不能一個台北一個本地
            self.assertEqual(news.today_local(), now.date())

    def test_grade_of_matches_thresholds(self):
        """門檻常數必須真的驅動 grade_of()，而不只是拿來印說明。"""
        with load_modules(self.dir, "news") as (news,):
            for grade, low in news.GRADE_THRESHOLDS:
                self.assertEqual(news.grade_of(low), grade, f"總分 {low} 應為 {grade} 級")
                self.assertNotEqual(news.grade_of(low - 1), grade,
                                    f"總分 {low - 1} 不應仍是 {grade} 級")
            # 門檻以下一律 fallback；grade_of 必須是全函數，負分也不能落空
            lowest = news.GRADE_THRESHOLDS[-1][1]
            self.assertEqual(news.grade_of(lowest - 1), news.FALLBACK_GRADE)
            self.assertEqual(news.grade_of(-1), news.FALLBACK_GRADE)

    def test_grades_cover_thresholds_and_labels(self):
        """GRADES 是網頁 tab、參數驗證、封存層級的共同出處，三者不得各自漂移。"""
        with load_modules(self.dir, "news") as (news,):
            self.assertEqual(
                news.GRADES,
                [g for g, _ in news.GRADE_THRESHOLDS] + [news.FALLBACK_GRADE])
            self.assertEqual(set(news.GRADES), set(news.GRADE_LABELS),
                             "GRADE_LABELS 與 GRADES 不一致（新增等級忘了補標籤）")
            self.assertLessEqual(set(news.ARCHIVE_GRADES), set(news.GRADES))
            lows = [low for _, low in news.GRADE_THRESHOLDS]
            self.assertEqual(lows, sorted(lows, reverse=True),
                             "門檻必須由高到低，否則 grade_of() 會提早命中錯的等級")


class TestDigest(CLITestCase):
    def test_includes_uncategorised(self):
        """section 為空的項目要歸到「其他」，不能被靜默漏掉。

        迴歸：原本寫 `section != '不建議放入每日摘要'`，但 SQL 的
        NULL != '...' 結果是 NULL 而非 true，未分類的項目會整批消失。
        """
        d = date.today().isoformat()
        self.add(make_score("有分類", d, url="http://e.com/1",
                            section="今日最重要"))
        p = make_score("沒分類", d, url="http://e.com/2")
        p["section"] = None
        self.add(p)
        out = self.run_cli("digest", "--date", d, check=True).stdout
        self.assertIn("有分類", out)
        self.assertIn("沒分類", out, "未分類的項目被漏掉了")
        self.assertIn("## 其他", out)

    def test_excludes_not_recommended(self):
        d = date.today().isoformat()
        self.add(make_score("不該出現", d, url="http://e.com/3",
                            section="不建議放入每日摘要"))
        self.add(make_score("該出現", d, url="http://e.com/4",
                            section="今日最重要"))
        out = self.run_cli("digest", "--date", d, check=True).stdout
        self.assertIn("該出現", out)
        self.assertNotIn("不該出現", out)


class TestTags(CLITestCase):
    """標籤：關聯新聞的鍵，正規化與比對出錯就會把同主題的新聞拆散。"""

    def tags_of(self, *, id=None, title=None):
        """查某筆的標籤。跨 import-json --replace 的比對要用 title——
        重新匯入會配到新的 id（AUTOINCREMENT 不重用舊值）。"""
        col, val = ("id", id) if id is not None else ("title", title)
        conn = sqlite3.connect(self.dir / "news.db")
        row = conn.execute(f"SELECT tags FROM news WHERE {col} = ?", (val,)).fetchone()
        conn.close()
        return json.loads(row[0]) if row and row[0] else []

    def test_alias_normalized_on_write(self):
        """別名寫法在寫入時就收斂，db 內只存正規名。"""
        self.add(make_score("a", tags=["輝達", "TSMC"]))
        self.assertEqual(self.tags_of(id=1), ["NVIDIA", "台積電"])

    def test_alias_key_ignores_case_and_space(self):
        """比對鍵會去大小寫與空白，別名表不必為每種寫法各存一列。"""
        self.add(make_score("a", tags=["NVIDIA", "n v i d i a"], url="http://e.com/1"))
        # 兩種寫法都收斂到 NVIDIA，去重後只剩一個
        self.assertEqual(self.tags_of(id=1), ["NVIDIA"])

    def test_unknown_tag_kept_as_is(self):
        """不在別名表的標籤原樣保留——別名是收斂工具，不是白名單。"""
        self.add(make_score("a", tags=["某個全新主題"]))
        self.assertEqual(self.tags_of(id=1), ["某個全新主題"])

    def test_duplicate_tags_deduped_preserving_order(self):
        """去重要保留首次出現順序，否則每次寫入的排列不同會讓 JSON 產生雜訊 diff。"""
        self.add(make_score("a", tags=["台積電", "AI", "輝達", "台積電"]))
        self.assertEqual(self.tags_of(id=1), ["台積電", "AI", "NVIDIA"])

    def test_rejects_too_many_tags(self):
        with load_modules(self.dir, "news") as (news,):
            limit = news.MAX_TAGS
        r = self.add(make_score("a", tags=[f"標籤{i}" for i in range(limit + 1)]))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("標籤最多", r.stderr + r.stdout)
        self.assertEqual(self.db_count(), 0, "被拒絕的資料不該寫入 db")

    def test_tags_survive_json_roundtrip(self):
        """標籤必須進 data/news.json，否則 CI 從 JSON 重建時會整批消失。"""
        self.add(make_score("a", tags=["台積電", "AI"]))
        self.assertEqual(self.json_rows()[0].get("tags"), '["台積電", "AI"]')
        self.run_cli("import-json", "--replace", check=True)
        self.assertEqual(self.tags_of(title="a"), ["台積電", "AI"])

    def test_alias_command_retags_existing_rows(self):
        """新增別名要一併收斂既有資料，否則舊資料仍是分裂的兩個標籤。"""
        self.add(make_score("a", tags=["晶圓代工"], url="http://e.com/1"))
        self.add(make_score("b", tags=["台積電"], url="http://e.com/2"))
        self.run_cli("alias", "晶圓代工", "台積電", check=True)
        self.assertEqual(self.tags_of(id=1), ["台積電"])
        self.assertEqual(self.json_rows()[0].get("tags"), '["台積電"]',
                         "收斂後要同步更新 JSON")

    def test_alias_rejects_chain(self):
        """別名不得指向另一個別名，否則正規化結果取決於查表順序。"""
        r = self.run_cli("alias", "台積", "輝達")  # 輝達本身是 NVIDIA 的別名
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("別名", r.stderr + r.stdout)

    def test_alias_removal_is_not_resurrected(self):
        """刪掉種子別名後不該在下次連線時復活（種子只在表空時灌）。"""
        self.run_cli("alias", "--remove", "輝達", check=True)
        self.run_cli("tags")  # 觸發一次 connect()/migrate()
        out = self.run_cli("alias", check=True).stdout
        self.assertNotIn("輝達", out, "已刪除的別名被種子重新灌回來了")

    def test_tag_command_edits_tags(self):
        self.add(make_score("a", tags=["AI"]))
        self.run_cli("tag", "1", "台積電", "AI", check=True)
        self.assertEqual(self.tags_of(id=1), ["台積電", "AI"])
        self.run_cli("tag", "1", "--add", "輝達", check=True)
        self.assertEqual(self.tags_of(id=1), ["台積電", "AI", "NVIDIA"])
        self.run_cli("tag", "1", "--clear", check=True)
        self.assertEqual(self.tags_of(id=1), [])

    def test_migration_adds_column_to_existing_db(self):
        """既有 db（沒有 tags 欄位）要能自動補上，不是重建才有。

        CREATE TABLE IF NOT EXISTS 對已存在的表完全不動，
        沒有 migrate 的話舊 db 會在 add 時噴 no such column。
        """
        conn = sqlite3.connect(self.dir / "news.db")
        # 造出一個「舊版」db：把 tags 欄位拿掉（SQLite 3.35+ 支援 DROP COLUMN）
        conn.execute("ALTER TABLE news DROP COLUMN tags")
        conn.execute("DROP TABLE tag_aliases")
        conn.commit()
        conn.close()
        r = self.add(make_score("a", tags=["AI"]))
        self.assertEqual(r.returncode, 0, f"舊 db 應自動補欄位：{r.stderr}")
        self.assertEqual(self.tags_of(id=1), ["AI"])

    def test_related_news_shown_on_card(self):
        """同標籤的新聞要在卡片的評分細節裡互相看得到。"""
        self.add(make_score("台積電擴廠", url="http://e.com/1", tags=["台積電"]))
        self.add(make_score("台積電法說", url="http://e.com/2", tags=["台積電"]))
        self.add(make_score("無關新聞", url="http://e.com/3", tags=["體育"]))
        self.run_cli("export", "--out", "d", check=True)
        html = (self.dir / "d" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("相關新聞（同標籤）"), 2,
                         "只有兩則同標籤的新聞該出現相關新聞區塊")

    def test_related_excludes_self(self):
        """相關新聞不能把自己列進去。"""
        self.add(make_score("唯一一則", url="http://e.com/1", tags=["台積電"]))
        self.run_cli("export", "--out", "d", check=True)
        html = (self.dir / "d" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("相關新聞（同標籤）", html,
                         "只有一則時不該有相關新聞區塊")

    def test_tag_and_section_are_clickable_in_both_modes(self):
        """兩種模式的標籤／分類都要能點。

        迴歸：動態站原本沿用靜態站的 <button data-tag-pick>，但動態頁面
        根本沒載入 FILTER_JS，那顆按鈕按了完全沒反應。動態站要改成帶
        query string 的 <a>，靜態站才是 button。
        """
        self.add(make_score("a", tags=["台積電"], section="今日最重要"))
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"

            dynamic = server.render_page()
            self.assertIn('<a class="tag" href="/?tag=', dynamic,
                          "動態站的標籤必須是可點的連結")
            self.assertIn('<a class="section" href="/?section=', dynamic,
                          "動態站的分類必須是可點的連結")
            self.assertNotIn("data-tag-pick", dynamic,
                             "動態站沒有 JS，不該輸出只有 JS 才能用的按鈕")

            static = server.render_static_page()
            self.assertIn("data-tag-pick", static,
                          "靜態站的標籤由 FILTER_JS 處理，必須是 button")
            self.assertNotIn('<a class="tag" href=', static,
                             "靜態站沒有 server 可以處理 query string")

    def test_tags_listing(self):
        self.add(make_score("a", tags=["台積電", "AI"], url="http://e.com/1"))
        self.add(make_score("b", tags=["台積電"], url="http://e.com/2"))
        out = self.run_cli("tags", check=True).stdout
        self.assertRegex(out, r"2\s+台積電")
        self.assertRegex(out, r"1\s+AI")
        listed = self.run_cli("tags", "台積電", check=True).stdout
        self.assertIn("a", listed)
        self.assertIn("b", listed)


class TestShareMetadata(CLITestCase):
    """分享預覽（OG／description）——貼連結到 Slack／Threads 時的呈現。"""

    def setUp(self):
        super().setUp()
        self.add(make_score("台積電法說", date.today().isoformat(),
                            url="http://e.com/1", tags=["台積電"],
                            scores=(24, 19, 19, 19, 14)))  # 95 → S

    def test_head_has_share_metadata(self):
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            html = server.render_static_page()
        head = html[:html.find("</head>")]
        for tag in ('name="description"', 'property="og:title"',
                    'property="og:description"', 'property="og:image"',
                    'property="og:url"', 'rel="canonical"',
                    'name="twitter:card"'):
            self.assertIn(tag, head, f"<head> 缺少 {tag}")

    def test_update_check_only_in_static_page(self):
        """更新提示只屬於靜態站。

        動態 serve 完全沒有載入 JS，插了也不會執行；而靜態站的分頁若開著
        不動，GitHub Pages 的 max-age=600 會讓它十分鐘內完全不問伺服器，
        評分上線後沒有任何跡象。相關 CSS 則共用（兩邊同一份 STYLE）。
        """
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            static = server.render_static_page()
            dynamic = server.render_page(grade="", date="", section="", q="", tag="")
        self.assertIn("watchForUpdates", static, "靜態站應含更新檢查")
        self.assertIn("update-bar", server.STYLE, "更新提示的樣式應在共用 STYLE 中")
        self.assertNotIn("watchForUpdates", dynamic,
                         "動態站沒有載入 JS，不該含更新檢查")
        self.assertNotIn("__BUILD_ID__", static,
                         "build id 佔位字串沒有被取代，前端會拿它去比對而永遠不相等")

    def test_build_id_tracks_data_not_time(self):
        """build id 必須由資料算出，且與 build.txt 一致。

        取自時間戳的話，CI 因不相干改動重跑就會讓所有開著的分頁跳出
        「有新資料」，但實際一則都沒變——提示會變成雜訊而被忽略。
        """
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            rows = server.query_news()
            first = server.build_id_of(rows)
            # 同一份資料重算必須相同（時間流逝不影響）
            self.assertEqual(first, server.build_id_of(server.query_news()),
                             "同一份資料的 build id 應穩定不變")
            # 頁面內嵌的值要跟 build.txt 寫出的值一致，否則一載入就誤報
            out = self.dir / "dist_buildid"
            server.export_static(out)
            written = (out / server.BUILD_ID_NAME).read_text(encoding="utf-8").strip()
            html = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn(f"'{written}'", html,
                          "頁面內嵌的 build id 與 build.txt 不一致，會立刻誤報有更新")

    def test_build_id_survives_json_roundtrip(self):
        """build id 必須撐過 export-json → import-json --replace。

        CI 是從 `data/news.json` 重建 db 才產站的，而 JSON 刻意不含 id，
        重建時會重新編號。build id 若取自 id，本機與 CI 就會算出不同的值，
        且刪掉中間任何一則都會讓其後每一筆位移——內容沒變也被判定成新版。
        """
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            before = server.build_id_of(server.query_news())
        self.run_cli("export-json", check=True)
        self.run_cli("import-json", "--replace", check=True)
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            after = server.build_id_of(server.query_news())
        self.assertEqual(before, after,
                         "build id 撐不過 JSON round-trip，CI 產出的值會與本機不同")

    def test_build_id_changes_when_data_changes(self):
        """新增一則評分後 build id 必須改變，否則提示永遠不會觸發。"""
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            before = server.build_id_of(server.query_news())
            conn = sqlite3.connect(self.dir / "news.db")
            conn.execute(
                "INSERT INTO news (title, news_date, total_score, grade) "
                "VALUES ('新增測試', '2026-07-26', 90, 'S')")
            conn.commit()
            conn.close()
            after = server.build_id_of(server.query_news())
        self.assertNotEqual(before, after, "資料變了但 build id 沒變")

    def test_og_image_url_is_absolute_https(self):
        """og:image 必須是 https 絕對網址。

        絕對網址：相對路徑各平台一律抓不到圖。
        https：SITE_URL 一度填成 http，而該網域的 http 不會自動轉址（直接回
        200），所以預覽圖網址就真的是 http——有些平台會因混合內容不抓圖，
        搜尋引擎也會把兩種 scheme 當成不同頁面。
        """
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            html = server.render_static_page()
            name = server.OG_IMAGE_NAME
        url = re.search(r'property="og:image" content="([^"]+)"', html).group(1)
        self.assertRegex(url, r"^https://", "og:image 應為 https 絕對網址")
        self.assertTrue(url.endswith(name))

    def test_canonical_url_is_https(self):
        """canonical 與 og:url 同樣不該是 http（理由同 og:image）。"""
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            html = server.render_static_page()
        for pat in (r'<link rel="canonical" href="([^"]+)"',
                    r'property="og:url" content="([^"]+)"'):
            url = re.search(pat, html).group(1)
            self.assertRegex(url, r"^https://", f"{pat} 應為 https")

    def test_og_image_is_raster_not_svg(self):
        """預覽圖必須是點陣圖。

        迴歸：原本用 SVG，但 Slack／Threads 是在**它們的伺服器**上算縮圖，
        那裡沒有中文字型，整張圖的中文都變成顯示 Unicode 碼位的豆腐方塊。
        本機預覽看不出來（用的是自己的字型），是實際貼出去才發現的。
        """
        with load_modules(self.dir, "server") as (server,):
            self.assertFalse(server.OG_IMAGE_NAME.endswith(".svg"),
                             "SVG 的中文在對方伺服器上會變豆腐字，要用點陣圖")

    def test_description_reflects_real_counts(self):
        """描述用實際數字而非固定文案，且長度要在平台截斷前。"""
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            rows = server.query_news()
            desc = server.meta_description(rows, server.grade_counts(rows))
        self.assertIn("1 則", desc, "描述應含實際筆數")
        self.assertIn(date.today().isoformat(), desc, "描述應含最新日期")
        self.assertLessEqual(len(desc), 90, f"描述過長會被截斷（{len(desc)} 字元）")

    def test_export_copies_og_image(self):
        """預覽圖要與 index.html 同層輸出，否則 og:image 指到 404。

        圖是進版控的成品、export 只負責複製——CI 沒有中文字型，
        當場生成只會產出豆腐字。
        """
        with load_modules(self.dir, "server") as (server,):
            name, src = server.OG_IMAGE_NAME, server.OG_IMAGE_SRC
        # 暫存目錄沒有 assets/，複製一份真的圖進去模擬 repo 狀態
        assets = self.dir / "assets"
        assets.mkdir(exist_ok=True)
        (assets / name).write_bytes(
            src.read_bytes() if src.exists() else b"\x89PNG\r\n\x1a\n")
        self.run_cli("export", "--out", "d", check=True)
        out = self.dir / "d" / name
        self.assertTrue(out.exists(), f"export 沒有輸出 {name}")
        self.assertEqual(out.read_bytes()[:4], b"\x89PNG", "輸出的不是 PNG")

    def test_export_survives_missing_og_image(self):
        """缺圖只該警告不該中止——沒有大圖預覽，標題與描述仍在。"""
        r = self.run_cli("export", "--out", "d2")
        self.assertEqual(r.returncode, 0, f"缺預覽圖不該中止部署：{r.stderr}")
        self.assertIn("警告", r.stdout + r.stderr)

    def test_og_lines_survive_empty_db(self):
        """空 db 也要能算出圖上的文字（max() 對空序列會拋例外）。"""
        conn = sqlite3.connect(self.dir / "news.db")
        conn.execute("DELETE FROM news")
        conn.commit()
        conn.close()
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            rows = server.query_news()
            lines = server.og_image_lines(rows, server.grade_counts(rows))
        self.assertTrue(lines, "空 db 仍該回傳可畫的文字")


class TestStaticOutput(CLITestCase):
    """靜態站產出的自我驗證。"""

    def test_card_count_matches_rows(self):
        for i in range(3):
            self.add(make_score(f"t{i}", date.today().isoformat(),
                                url=f"http://e.com/{i}"))
        r = self.run_cli("export", "--out", "d", check=True)
        html = (self.dir / "d" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('<div class="card '), 3)
        self.assertIn("3 張卡片", r.stdout)

    def test_empty_db_exports_without_error(self):
        r = self.run_cli("export", "--out", "d")
        self.assertEqual(r.returncode, 0, "空 db 不該噴錯")


class TestDriftDetection(CLITestCase):
    """漂移偵測必須能區分「標準鬆動」與「主題組成改變」。

    少了主題控制，只要當期湧入重大主題就會誤報——工具會很快被當成
    狼來了而忽略，那比沒有工具更糟。
    """

    def _add_batch(self, prefix, dates_scores, tags):
        """dates_scores: [(日期, decision 分數)]，其餘面向固定。"""
        for i, (d, dec) in enumerate(dates_scores):
            self.add(make_score(f"{prefix}{i}", d, url=f"http://e.com/{prefix}{i}",
                                scores=(10, 10, dec, 10, 9), tags=tags))

    def test_detects_real_drift_within_same_tag(self):
        """同一標籤內分數上升 → 應判定為標準鬆動。"""
        self._add_batch("早", [("2026-01-01", 5)] * 6, ["主題A"])
        self._add_batch("晚", [("2026-03-01", 15)] * 6, ["主題A"])
        with load_modules(self.dir, "news") as (news,):
            conn = sqlite3.connect(self.dir / "news.db")
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute("SELECT * FROM news"))
            conn.close()
            early = [r for r in rows if r["news_date"] <= "2026-01-31"]
            late = [r for r in rows if r["news_date"] > "2026-01-31"]
            within = news.drift_within_tags(early, late, "decision")
        self.assertEqual(len(within), 1, "主題A 前後期都達門檻，應被納入")
        self.assertAlmostEqual(within[0]["delta"], 10.0, delta=0.1,
                               msg="同標籤內的漲幅應被如實反映")

    def test_topic_mix_change_is_not_reported_as_drift(self):
        """整體平均上升但各標籤內不變 → 是主題組成改變，不該被當成漂移。

        迴歸情境：前期都是低分主題、後期湧入高分主題，整體平均自然上升。
        若不控制主題，這會被誤報成標準鬆動。
        """
        # 前期：低分主題；後期：換成另一個天生高分的主題，但兩者各自穩定
        self._add_batch("低", [("2026-01-01", 5)] * 6, ["低分主題"])
        self._add_batch("低後", [("2026-03-01", 5)] * 6, ["低分主題"])
        self._add_batch("高", [("2026-03-02", 18)] * 6, ["高分主題"])
        with load_modules(self.dir, "news") as (news,):
            conn = sqlite3.connect(self.dir / "news.db")
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute("SELECT * FROM news"))
            conn.close()
            early = [r for r in rows if r["news_date"] <= "2026-01-31"]
            late = [r for r in rows if r["news_date"] > "2026-01-31"]
            overall = news.drift_by_dimension(early, late)["decision"]
            within = news.drift_within_tags(early, late, "decision")
        # 整體平均確實上升（高分主題灌進來）
        self.assertGreater(overall["delta"], 3,
                           "整體平均應因主題組成改變而上升")
        # 但唯一能前後對照的標籤沒有變化 → 不是標準鬆動
        self.assertEqual(len(within), 1, "只有低分主題在前後期都達門檻")
        self.assertAlmostEqual(within[0]["delta"], 0.0, delta=0.1,
                               msg="同標籤內沒有變化，不該被當成漂移")

    def test_small_tags_excluded_from_verification(self):
        """樣本不足的標籤不納入複驗，避免個別極端值主導結論。"""
        self._add_batch("多", [("2026-01-01", 5)] * 6, ["夠多"])
        self._add_batch("多後", [("2026-03-01", 5)] * 6, ["夠多"])
        self._add_batch("少", [("2026-01-01", 20)] * 2, ["太少"])
        self._add_batch("少後", [("2026-03-01", 0)] * 2, ["太少"])
        with load_modules(self.dir, "news") as (news,):
            conn = sqlite3.connect(self.dir / "news.db")
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute("SELECT * FROM news"))
            conn.close()
            early = [r for r in rows if r["news_date"] <= "2026-01-31"]
            late = [r for r in rows if r["news_date"] > "2026-01-31"]
            within = news.drift_within_tags(early, late, "decision")
        tags = {w["tag"] for w in within}
        self.assertIn("夠多", tags)
        self.assertNotIn("太少", tags, "每期不足 5 則的標籤不該納入")

    def test_drift_runs_on_real_shaped_data(self):
        """CLI 能跑完並輸出各面向與等級分布。"""
        # 需 20 則以上才會實際計算（少於此的樣本判不出漂移）
        for i in range(24):
            d = "2026-01-01" if i < 12 else "2026-03-01"
            self.add(make_score(f"n{i}", d, url=f"http://e.com/d{i}",
                                scores=(10, 10, 5 if i < 12 else 15, 10, 9),
                                tags=["T"]))
        r = self.run_cli("drift", check=True)
        self.assertIn("決策相關性", r.stdout)
        self.assertIn("等級分布", r.stdout)


class TestReviewCalibration(CLITestCase):
    """評分回顧的後續指標必須修掉兩個偏誤，否則測到的是雜訊而非判讀準確度。

    這兩項修正是整個功能的核心：少了任何一個，指標都會退化成
    「標籤有多大 × 評得多晚」，看起來有數字但完全測不到評分準不準。
    """

    def _stats(self, rows_spec):
        """rows_spec: [(title, date, tags)] → 寫入後回傳 followup_stats。"""
        for i, (title, d, tags) in enumerate(rows_spec):
            self.add(make_score(title, d, url=f"http://e.com/{i}", tags=tags))
        with load_modules(self.dir, "news") as (news,):
            news.DB_PATH = self.dir / "news.db"
            conn = sqlite3.connect(self.dir / "news.db")
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute("SELECT * FROM news"))
            conn.close()
            return news.followup_stats(rows, window_days=30), rows

    def test_corrects_big_tag_bias(self):
        """掛熱門標籤不該讓超額倍數變高——那是標籤大，不是這則重要。

        迴歸情境：「中國」有 60 則而「儲能」只有 1 則，若直接比後續次數，
        所有掛「中國」的新聞都會顯得判斷精準。
        """
        spec = [("熱門A", "2026-01-01", ["熱門"]), ("冷門A", "2026-01-01", ["冷門"])]
        # 讓「熱門」標籤在後續大量出現，「冷門」只出現一次
        spec += [(f"熱門後續{i}", "2026-01-10", ["熱門"]) for i in range(9)]
        spec += [("冷門後續", "2026-01-10", ["冷門"])]
        stats, rows = self._stats(spec)
        by_title = {r["title"]: stats[r["id"]] for r in rows if r["id"] in stats}
        hot, cold = by_title["熱門A"], by_title["冷門A"]
        # 原始後續數必然是熱門遠多（這正是偏誤來源）
        self.assertGreater(hot["followups"], cold["followups"])
        # 但修正後，兩者都只是「符合各自標籤的常態」，超額倍數應接近
        self.assertAlmostEqual(
            hot["excess"], cold["excess"], delta=0.5,
            msg="大標籤偏誤未修正：熱門標籤的超額倍數不該高於冷門")

    def test_marks_immature_rows(self):
        """窗口未走完的則必須標記，否則會被誤報成「高估」。

        今天評的新聞後續必為 0，混進報表就是假警訊。
        """
        spec = [("舊聞", "2026-01-01", ["X"]), ("新聞", "2026-03-01", ["X"])]
        stats, rows = self._stats(spec)
        by_title = {r["title"]: stats[r["id"]] for r in rows if r["id"] in stats}
        self.assertTrue(by_title["舊聞"]["mature"], "距最新日超過窗口的應為成熟")
        self.assertFalse(by_title["新聞"]["mature"], "最新的一則窗口未走完")

    def test_review_excludes_immature_from_report(self):
        """報表只能納入窗口走完的則，且要說明排除了幾則。"""
        for i in range(3):
            self.add(make_score(f"新{i}", date.today().isoformat(),
                                url=f"http://e.com/n{i}", tags=["X"]))
        r = self.run_cli("review", check=True)
        self.assertIn("窗口未滿", r.stdout,
                      "應明確告知有多少則因窗口未滿而未納入")

    def test_median_ties_excluded_from_both_groups(self):
        """等於中位數的則不歸入任一組。

        分數是整數且高度集中（實際資料有 51 則同為 13 分）。若用 >= 切組，
        同分者會全被塞進高分組，讓該組混入大量中間值而稀釋對比——測出來的
        差距會低於真實鑑別力。
        """
        with load_modules(self.dir, "news") as (news,):
            rows = [{"id": i, f"duration_score": s, "structural_score": s}
                    for i, s in enumerate([1, 2, 2, 2, 3])]
            # 用 dict 當 row：dimension_calibration 只讀 id 與 <key>_score
            stats = {i: {"excess": 1.0} for i in range(5)}
            cal = news.dimension_calibration(rows, stats)
        c = cal["duration"]
        self.assertEqual(c["median"], 2)
        self.assertEqual(c["ties"], 3, "三則同為中位數 2 分")
        self.assertEqual(c["high_n"], 1, "高分組只該有 >2 的那一則")
        self.assertEqual(c["low_n"], 1, "低分組只該有 <2 的那一則")

    def test_burst_topic_is_not_reported_as_underestimated(self):
        """正在延燒的主題不該因為「後續多」就被判為低估。

        迴歸情境：廣西水災連三則被評 C 44-48，超額倍數卻高達 ×3.0-3.4，
        報表把它們列為「可能低估」。但災害報導本來就會連續多天出現，
        後續多是主題在延燒，不是當初判斷精準——這個訊號指向的是
        「事件持續發酵」而非「評分準確」，兩者混為一談會讓報表反覆
        建議調高災害類的分數。

        基準率若用全期平均，六月安靜、七月爆發的主題會拿到過低的期望值，
        於是七月那幾則全部虛高。正確做法是用「窗口當期」的實際出現率當
        基準：主題自己在延燒時，基準也跟著抬高，超額倍數才會回到常態。
        """
        # 「水災」全期只佔少數（前段 20 天完全沒有），但在窗口內天天出現
        spec = [(f"背景{i}", f"2026-01-{i + 1:02d}", ["其他"]) for i in range(20)]
        spec += [("水災主則", "2026-01-21", ["水災"])]
        spec += [(f"水災後續{i}", f"2026-01-{22 + i:02d}", ["水災"]) for i in range(8)]
        stats, rows = self._stats(spec)
        by_title = {r["title"]: stats[r["id"]] for r in rows if r["id"] in stats}
        excess = by_title["水災主則"]["excess"]
        self.assertIsNotNone(excess)
        # 該則的後續密度與同期同標籤常態相同，超額倍數應接近 1 而非數倍
        self.assertLess(
            excess, 1.6,
            f"延燒中的主題被誤報為低估（excess={excess:.2f}）："
            "基準率必須取窗口當期而非全期平均")

    def test_optimized_stats_match_naive_computation(self):
        """前綴和最佳化的結果必須與逐筆比對的天真算法一致。

        followup_stats 為了效能改用日期索引與前綴和（原本 O(n²)，4300 筆
        要 12 秒）。這類最佳化最容易在邊界（窗口起訖日）算錯，故用天真
        實作當對照組驗證等價。
        """
        # 邊界要有資料才測得到 off-by-one：對 A 而言 1/31 是窗口最後一天
        # （第 30 天）、2/01 是剛好落在窗口外的第 31 天。少了這兩筆，
        # 窗口算成 29 天或 31 天都不會被發現。
        spec = [("A", "2026-01-01", ["X"]), ("B", "2026-01-05", ["X", "Y"]),
                ("C", "2026-01-20", ["Y"]),
                ("邊界內", "2026-01-31", ["X"]), ("邊界外", "2026-02-01", ["X"]),
                ("D", "2026-02-10", ["X"])]
        stats, rows = self._stats(spec)

        # 天真實作：對每則掃過全部資料，逐筆比對日期
        def naive(rows, window):
            from datetime import datetime
            def days(a, b):
                fmt = "%Y-%m-%d"
                return (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).days
            out = {}
            for r in rows:
                ts = set(json.loads(r["tags"] or "[]"))
                if not ts:
                    continue
                hits = sum(
                    1 for o in rows
                    if 0 < days(r["news_date"], o["news_date"]) <= window
                    and ts & set(json.loads(o["tags"] or "[]"))
                )
                out[r["id"]] = hits
            return out

        expected = naive(rows, 30)
        for rid, hits in expected.items():
            self.assertEqual(stats[rid]["followups"], hits,
                             f"id={rid} 的後續數與天真算法不符")

    def test_only_verifiable_dimensions_are_calibrated(self):
        """只校準 duration 與 structural——其餘三個面向與後續數無邏輯關聯。

        影響範圍、決策相關性、事實可信度評的是新聞當下的性質，硬用後續數
        檢驗會產生看似有據的假結論。
        """
        with load_modules(self.dir, "news") as (news,):
            keys = set(news.dimension_calibration([], {}).keys())
        self.assertEqual(keys, {"duration", "structural"})


class TestWatchVerification(CLITestCase):
    """watch_next 逐條驗證：這是唯一能直接檢驗「判斷準不準」的機制，
    但它只有在候選清單可信、判定語意清楚時才有價值。"""

    def _rows(self, news):
        conn = sqlite3.connect(self.dir / "news.db")
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT * FROM news"))
        verified = news.load_verified(conn)
        conn.close()
        return rows, verified

    def test_broad_shared_tag_is_not_a_candidate(self):
        """只共用寬標籤不算線索——那會把整份清單灌成雜訊。

        迴歸情境：「台灣防空演習」與「綜所稅退稅」都掛「台灣政策」，
        於是退稅新聞被列為「海纜備援建設進度」的線索。實測這種配對
        讓候選暴增到 881 條且幾乎全不可用，人根本讀不完也不會信。
        """
        # 「寬」標籤要夠多則才會超過佔比門檻，故灌一批背景資料
        for i in range(30):
            self.add(make_score(f"背景{i}", "2026-01-01", url=f"http://e.com/b{i}",
                                tags=["寬標籤"]))
        self.add(make_score("舊則", "2026-01-01", url="http://e.com/old",
                            tags=["寬標籤", "窄標籤"], watch_next=["某個指標"]))
        # 當天新聞只與舊則共用寬標籤
        self.add(make_score("今日無關", "2026-02-01", url="http://e.com/new1",
                            tags=["寬標籤"]))
        with load_modules(self.dir, "news") as (news,):
            news.DB_PATH = self.dir / "news.db"
            rows, verified = self._rows(news)
            cands = news.watch_candidates(rows, verified, "2026-02-01")
        self.assertEqual(
            cands, [],
            "只共用寬標籤不該成為候選——這正是讓清單退化成雜訊的原因")

    def test_narrow_shared_tag_is_a_candidate(self):
        """共用窄標籤才算線索，且要標示是哪個標籤讓它們相關。"""
        for i in range(30):
            self.add(make_score(f"背景{i}", "2026-01-01", url=f"http://e.com/b{i}",
                                tags=["寬標籤"]))
        self.add(make_score("舊則", "2026-01-01", url="http://e.com/old",
                            tags=["寬標籤", "窄標籤"], watch_next=["某個指標"]))
        self.add(make_score("今日相關", "2026-02-01", url="http://e.com/new2",
                            tags=["窄標籤"]))
        with load_modules(self.dir, "news") as (news,):
            news.DB_PATH = self.dir / "news.db"
            rows, verified = self._rows(news)
            cands = news.watch_candidates(rows, verified, "2026-02-01")
        self.assertEqual(len(cands), 1, "共用窄標籤應成為候選")
        self.assertEqual(cands[0]["key_tags"], ["窄標籤"],
                         "要標示是哪個標籤構成線索，否則無從判斷可信度")

    def test_too_recent_indicators_are_not_listed(self):
        """未滿 min_age 的不列出——太早看什麼都還沒發生。

        把「時候未到」記成 miss 會污染命中率，而 miss 是要用來
        檢討評分的訊號，不能混入雜訊。
        """
        self.add(make_score("昨天", "2026-01-31", url="http://e.com/y",
                            tags=["窄標籤"], watch_next=["某個指標"]))
        self.add(make_score("今日", "2026-02-01", url="http://e.com/t",
                            tags=["窄標籤"]))
        with load_modules(self.dir, "news") as (news,):
            news.DB_PATH = self.dir / "news.db"
            rows, verified = self._rows(news)
            cands = news.watch_candidates(rows, verified, "2026-02-01", min_age=7)
        self.assertEqual(cands, [], "只放了 1 天的指標不該被要求判定")

    def test_verified_items_are_not_listed_again(self):
        """判定過的不再出現，否則每天都要重看同一批。"""
        self.add(make_score("舊則", "2026-01-01", url="http://e.com/old",
                            tags=["窄標籤"], watch_next=["指標A", "指標B"]))
        self.add(make_score("今日", "2026-02-01", url="http://e.com/new",
                            tags=["窄標籤"]))
        self.run_cli("watch-verify", "http://e.com/old", "0", "hit", check=True)
        with load_modules(self.dir, "news") as (news,):
            news.DB_PATH = self.dir / "news.db"
            rows, verified = self._rows(news)
            cands = news.watch_candidates(rows, verified, "2026-02-01")
        self.assertEqual([c["idx"] for c in cands], [1],
                         "已判定的 #0 不該再列出，未判定的 #1 要留著")

    def test_moot_excluded_from_hit_rate(self):
        """moot 不列入分母——它是「無從判斷」，不是「預測錯」。

        混為一談會系統性低估命中率，而這兩者對校準的意涵完全不同：
        miss 該檢討判斷，moot 只是世界變了。
        """
        self.add(make_score("A", "2026-01-01", url="http://e.com/a",
                            tags=["X"], watch_next=["i0", "i1", "i2"]))
        self.run_cli("watch-verify", "http://e.com/a", "0", "hit", check=True)
        self.run_cli("watch-verify", "http://e.com/a", "1", "miss", check=True)
        self.run_cli("watch-verify", "http://e.com/a", "2", "moot", check=True)
        with load_modules(self.dir, "news") as (news,):
            news.DB_PATH = self.dir / "news.db"
            rows, verified = self._rows(news)
            acc = news.watch_accuracy(rows, verified)
        self.assertAlmostEqual(acc["overall"]["rate"], 0.5,
                               msg="命中率應為 1/(1+1)，moot 不進分母")

    def test_verify_survives_import_json_replace(self):
        """判定資料必須熬過 CI 的 import-json --replace。

        該指令會重建 news 表、id 由 AUTOINCREMENT 重新配發。判定若以 id
        關聯就會全部對錯人；以 url 關聯才穩。這也是判定要另存 JSON 的原因——
        news.db 不進版控，重新 clone 後只剩 JSON。
        """
        self.add(make_score("A", "2026-01-01", url="http://e.com/a",
                            tags=["X"], watch_next=["指標"]))
        self.run_cli("watch-verify", "http://e.com/a", "0", "hit", check=True)
        self.run_cli("import-json", "--replace", check=True)
        with load_modules(self.dir, "news") as (news,):
            news.DB_PATH = self.dir / "news.db"
            rows, verified = self._rows(news)
            acc = news.watch_accuracy(rows, verified)
        self.assertEqual(acc["overall"]["counts"]["hit"], 1,
                         "重建 news 表後判定仍要對得上原本那則")


class TestWatchHitsOnCard(CLITestCase):
    """卡片上的 watch_next 命中標示。

    網頁刻意只標命中：已判定的則僅約佔全站 5%，若把 miss 也標出來，
    「未判定」與「已驗證未發生」在視覺上難以區分，訪客會把前者誤讀成後者。
    """

    def _render(self, verify_items, static=True):
        import json as _json
        self.add(make_score("A", "2026-01-01", url="http://e.com/a",
                            tags=["X"], watch_next=["指標甲", "指標乙", "指標丙"]))
        vp = self.dir / "wv.json"
        vp.write_text(_json.dumps(verify_items, ensure_ascii=False), encoding="utf-8")
        with load_modules(self.dir, "news", "server") as (news, server):
            news.DB_PATH = self.dir / "news.db"
            conn = sqlite3.connect(self.dir / "news.db")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM news").fetchone()
            conn.close()
            hits = news.load_hits_from_json(vp)
            counts = news.verified_counts_from_json(vp)
            return server.render_card(
                row, static=static,
                hits=hits.get(row["url"]), verified=counts.get(row["url"]))

    def test_miss_is_not_marked_on_card(self):
        """miss 不得出現任何標記——否則與未判定的指標無從區分。"""
        html = self._render([
            {"news_url": "http://e.com/a", "idx": 0, "verdict": "hit",
             "note": "成真了", "evidence_url": "http://e.com/ev", "verified_date": "2026-02-01"},
            {"news_url": "http://e.com/a", "idx": 1, "verdict": "miss",
             "note": "沒發生", "evidence_url": None, "verified_date": "2026-02-01"},
        ])
        self.assertIn("指標甲", html)
        self.assertIn("✓", html, "hit 應標示 ✓")
        self.assertNotIn("✗", html, "miss 不該有標記")
        self.assertNotIn("沒發生", html, "miss 的判定說明不該外洩到網頁")

    def test_hit_summary_states_the_denominator(self):
        """只標 ✓ 時必須同時說明已驗證幾條，否則讀起來像全部命中。"""
        html = self._render([
            {"news_url": "http://e.com/a", "idx": 0, "verdict": "hit",
             "note": "", "evidence_url": None, "verified_date": "2026-02-01"},
            {"news_url": "http://e.com/a", "idx": 1, "verdict": "miss",
             "note": "", "evidence_url": None, "verified_date": "2026-02-01"},
        ])
        self.assertIn("已回頭驗證 2 條", html,
                      "分母要含 miss，否則選擇性呈現會讓訪客高估準確率")
        self.assertIn("成真的 1 條", html)

    def test_moot_excluded_from_denominator(self):
        """moot 是「無從判斷」，不進分母（與 watch_accuracy 一致）。"""
        html = self._render([
            {"news_url": "http://e.com/a", "idx": 0, "verdict": "hit",
             "note": "", "evidence_url": None, "verified_date": "2026-02-01"},
            {"news_url": "http://e.com/a", "idx": 1, "verdict": "moot",
             "note": "", "evidence_url": None, "verified_date": "2026-02-01"},
        ])
        self.assertIn("已回頭驗證 1 條", html, "moot 不該進分母")

    def test_unverified_card_is_unchanged(self):
        """沒有判定資料的卡片維持原樣——全站約 95% 是這種。"""
        html = self._render([])
        self.assertIn("指標甲", html)
        self.assertNotIn("✓", html)
        self.assertNotIn("已回頭驗證", html)

    def test_marking_is_identical_in_both_modes(self):
        """動態站與靜態站的命中標示必須一致（同 TestFilterParity 的精神）。"""
        items = [{"news_url": "http://e.com/a", "idx": 0, "verdict": "hit",
                  "note": "n", "evidence_url": "http://e.com/ev",
                  "verified_date": "2026-02-01"}]
        static_html = self._render(items, static=True)
        dynamic_html = self._render(items, static=False)
        for frag in ("✓", "佐證 ↗", "已回頭驗證 1 條"):
            self.assertIn(frag, static_html, f"靜態站缺少 {frag}")
            self.assertIn(frag, dynamic_html, f"動態站缺少 {frag}")


if __name__ == "__main__":
    unittest.main()
