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
               summary=None, one_line=None, section="影響未來的趨勢", tags=None):
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
        "watch_next": ["指標"],
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
        future = (date.today() + timedelta(days=1)).isoformat()
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


if __name__ == "__main__":
    unittest.main()
