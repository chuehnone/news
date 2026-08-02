#!/usr/bin/env python3
"""tariff.py / tariff_page.py 的回歸測試。

涵蓋的是「已經出過錯」的地方：第一版把多套並存的關稅制度混在一起取最低值，
算出中國 24.0% 比孟加拉 26.5% 便宜——與實際情況（中國 12.5%、孟加拉 10%）
完全相反。**一個會讓人賠錢的工具比沒有工具更糟**，所以那條規則要有測試守著。

執行：python3 -m unittest test_tariff
"""

import json
import tempfile
import unittest
from pathlib import Path

import tariff
import tariff_page


class TestSeriesIsolation(unittest.TestCase):
    """只採 9903.05 系列——混入其他制度會讓比較結果失真。

    第 99 章至少有四套並存的關稅制度：
      9903.88.* / 9903.91.*  2018 年起的對中 301，依產品清單 7.5%~100%
      9903.01.* / 9903.02.*  對等關稅
      9903.05.*              強迫勞動 301，逐國單一稅率
    第一版混在一起取最低，中國因 9903.88.15 的 7.5% 被算成比孟加拉便宜。
    """

    def test_only_target_series_is_collected(self):
        self.assertEqual(tariff.SERIES_PREFIX, "9903.05")

    def test_other_series_would_break_comparison(self):
        """用實際踩過的資料重現：混入 9903.88 會讓中國看起來最便宜。"""
        rows = [
            # 強迫勞動 301（要採用的）
            {"htsno": "9903.05.31", "general": "The duty provided in the "
             "applicable subheading + 12.5%",
             "description": "articles the product of China, as provided for in "
                            "U.S. note 52 to this subchapter"},
            {"htsno": "9903.05.26", "general": "The duty provided in the "
             "applicable subheading + 10%",
             "description": "articles the product of Bangladesh, as provided "
                            "for in U.S. note 52 to this subchapter"},
            # 對中 301 的舊條號（不該混進來）——這條是讓第一版算錯的元凶
            {"htsno": "9903.88.15", "general": "The duty provided in the "
             "applicable subheading + 7.5%",
             "description": "articles the product of China, as provided for in "
                            "U.S. note 20(f) to this subchapter"},
        ]
        kept = [r for r in rows if r["htsno"].startswith(tariff.SERIES_PREFIX)]
        self.assertEqual(len(kept), 2, "9903.88 系列必須被排除")
        china = [r for r in kept if "China" in r["description"]]
        self.assertEqual(len(china), 1)
        self.assertEqual(tariff.parse_rate(china[0]["general"]), 12.5,
                         "中國應為 12.5% 而非 9903.88 的 7.5%")


class TestParsing(unittest.TestCase):
    def test_parse_rate(self):
        self.assertEqual(
            tariff.parse_rate("The duty provided in the applicable "
                              "subheading + 12.5%"), 12.5)
        self.assertEqual(tariff.parse_rate("... + 10%"), 10.0)

    def test_parse_rate_returns_none_when_absent(self):
        """沒有額外關稅與解析失敗是兩件事，後者混成 0 會靜默算錯。"""
        self.assertIsNone(tariff.parse_rate("The duty provided in the "
                                            "applicable subheading"))
        self.assertIsNone(tariff.parse_rate(""))
        self.assertIsNone(tariff.parse_rate(None))

    def test_parse_country_from_standard_phrasing(self):
        d = ("Except for products described in headings 9903.05.85–9903.05.92, "
             "articles the product of Bangladesh, as provided for in U.S. "
             "note 52 to this subchapter")
        self.assertEqual(tariff.parse_countries(d), ["Bangladesh"])

    def test_parse_multiword_country(self):
        d = ("articles the product of Costa Rica, as provided for in U.S. "
             "note 52 to this subchapter")
        self.assertEqual(tariff.parse_countries(d), ["Costa Rica"])

    def test_parse_strips_leading_the(self):
        d = ("articles the product of the Bahamas, as provided for in U.S. "
             "note 52 to this subchapter")
        self.assertEqual(tariff.parse_countries(d), ["Bahamas"])

    def test_cross_reference_yields_no_country(self):
        """交叉引用抓不到國名是正常的——寬鬆比對會把條號誤判成國名。"""
        d = "Except for products described in headings 9903.05.85–9903.05.92"
        self.assertEqual(tariff.parse_countries(d), [])

    def test_fta_countries(self):
        got = tariff.fta_countries("Free (AU,BH,CL,KR,SG)")
        self.assertEqual(got, {"Australia", "Bahrain", "Chile",
                               "Korea", "Singapore"})
        self.assertEqual(tariff.fta_countries(""), set())


class TestComparison(unittest.TestCase):
    """比較邏輯：總稅率 = 基本稅率（FTA 則為 0）+ 該國 301 稅率。"""

    DUTIES = {
        "China": [{"hts": "9903.05.31", "rate": 12.5, "desc": ""}],
        "Bangladesh": [{"hts": "9903.05.26", "rate": 10.0, "desc": ""}],
        "Australia": [{"hts": "9903.05.23", "rate": 12.5, "desc": ""}],
    }
    PRODUCT = {"general": "16.5%", "special": "Free (AU,BH,CL)"}

    def test_matches_july_2026_reality(self):
        """2026-07 實際：中國 12.5%、孟加拉 10%。棉質 T 恤基本稅率 16.5%。"""
        rows = tariff.compare("6109.10.00", self.PRODUCT, self.DUTIES)
        by = {r["country"]: r for r in rows}
        self.assertAlmostEqual(by["China"]["total"], 29.0)
        self.assertAlmostEqual(by["Bangladesh"]["total"], 26.5)
        self.assertLess(by["Bangladesh"]["total"], by["China"]["total"],
                        "孟加拉必須比中國便宜——第一版算反了")

    def test_fta_zeroes_the_base_rate(self):
        rows = tariff.compare("6109.10.00", self.PRODUCT, self.DUTIES)
        au = next(r for r in rows if r["country"] == "Australia")
        self.assertTrue(au["fta"])
        self.assertEqual(au["base"], 0.0)
        self.assertAlmostEqual(au["total"], 12.5)

    def test_only_lists_countries_with_data(self):
        """不補上「未知」的國家——那會讓人以為該國沒有額外關稅。"""
        rows = tariff.compare("6109.10.00", self.PRODUCT, self.DUTIES)
        self.assertEqual({r["country"] for r in rows}, set(self.DUTIES))

    def test_sorted_by_total(self):
        rows = tariff.compare("6109.10.00", self.PRODUCT, self.DUTIES)
        totals = [r["total"] for r in rows]
        self.assertEqual(totals, sorted(totals))


class TestPage(unittest.TestCase):
    def test_page_renders_with_real_data(self):
        html = tariff_page.render_page()
        self.assertIn("<title>", html)
        self.assertIn("China", html or "")
        # 資料要內嵌，不能在前端打外部 API（USITC 無 CORS 且無 SLA）
        self.assertNotIn("hts.usitc.gov/reststop", html)

    def test_page_states_its_scope(self):
        """範圍限制必須寫在頁面上——只涵蓋一套制度卻不說，等於誤導。"""
        html = tariff_page.render_page()
        self.assertIn("9903.05", html)
        self.assertIn("9903.88", html, "要說明另有並存的制度")
        self.assertIn("報關", html, "要提醒不能直接當報關依據")

    def test_page_links_back_to_news(self):
        html = tariff_page.render_page()
        self.assertIn('href="./"', html)


if __name__ == "__main__":
    unittest.main()
