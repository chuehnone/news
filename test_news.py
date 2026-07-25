#!/usr/bin/env python3
"""news.py / server.py 的回歸測試（標準庫 unittest，無外部依賴）。

執行：python3 -m unittest test_news -v

涵蓋的是「已經出過錯」的地方，而非追求覆蓋率：
- news_date 格式驗證（保留期用字面比較，未補零會被歸到錯誤層級）
- 保留期分層只在 export --retention 生效，不影響 db 與 JSON
- 匯出／匯入 round-trip 無損
- 靜態站產出的卡片數與資料筆數一致
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
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
        import re
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
        sys.path.insert(0, str(self.dir))
        try:
            import importlib, server
            importlib.reload(server)
            server.DB_PATH = self.dir / "news.db"
            return {r["title"] for r in server.query_news(**kw)}
        finally:
            sys.path.remove(str(self.dir))

    def js_filter(self, grade=None, date_=None, section=None, q=None):
        """複刻 FILTER_JS 的 apply()，對靜態站輸出的 data-* 做同樣篩選。"""
        self.run_cli("export", "--out", "d", check=True)
        html = (self.dir / "d" / "index.html").read_text(encoding="utf-8")
        import re
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
