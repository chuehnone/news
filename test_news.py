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
               summary=None, one_line=None, section="影響未來的趨勢"):
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
        self.add(make_score("台積電法說會", today, url="http://e.com/1",
                            scores=(20, 16, 15, 16, 12),
                            summary="僅摘要詞 出現在這裡"))          # 79 → A
        self.add(make_score("關稅生效", today, url="http://e.com/2",
                            scores=(24, 19, 19, 19, 14),
                            one_line="僅判斷詞 出現在這裡"))          # 95 → S
        self.add(make_score("天氣預報", old, url="http://e.com/3",
                            section="熱但未必重要"))                 # 49 → C
        self.add(make_score("關稅談判", old, url="http://e.com/4",
                            scores=(15, 12, 12, 12, 10)))          # 61 → B

    def sql_filter(self, **kw):
        """動態站的篩選結果（標題集合）。"""
        with load_modules(self.dir, "server") as (server,):
            server.DB_PATH = self.dir / "news.db"
            return {r["title"] for r in server.query_news(**kw)}

    def js_filter(self, grade=None, date_=None, section=None, q=None):
        """複刻 FILTER_JS 的 apply()，對靜態站輸出的 data-* 做同樣篩選。"""
        self.run_cli("export", "--out", "d", check=True)
        html = (self.dir / "d" / "index.html").read_text(encoding="utf-8")
        titles = set()
        for m in re.finditer(
            r'data-grade="([^"]*)" data-date="([^"]*)" data-section="([^"]*)"'
            r' data-text="([^"]*)">.*?<div class="title">(?:<a[^>]*>)?([^<]*)',
            html, re.S,
        ):
            g, d, sec, text, title = m.groups()
            # 與 FILTER_JS 的 base 條件逐項對應
            if grade and g != grade:
                continue
            if date_ and d != date_:
                continue
            if section and sec != section:
                continue
            if q and q.lower() not in text:
                continue
            titles.add(title)
        return titles

    def assert_parity(self, **kw):
        js_kw = {"grade": kw.get("grade"), "date_": kw.get("date"),
                 "section": kw.get("section"), "q": kw.get("q")}
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

    def test_combined(self):
        self.assert_parity(grade="S", q="關稅")
        self.assert_parity(date=date.today().isoformat(), section="影響未來的趨勢")


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
