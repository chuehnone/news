#!/usr/bin/env python3
"""美國 301 關稅的產地比較工具。

回答一個問題：**同一件產品換個國家生產，關稅差多少**。

2026 年 7 月的實測背景：301 條款在兩週內取代 122 條款成為主要關稅法源
（7/14 最高法院判 122 條款違法、退還企業 2.6 兆元，7/24 換 301 條款上路），
稅率一次擴及約 60 國且逐國不同——越南被拉到 12.5% 與中國同列、孟加拉與
柬埔寨 10%、巴西 25%。中小出口商沒有貿易法務團隊，只能人工比對公告。

資料源是 USITC 的 HTS REST API（免費、無金鑰）。**刻意在本機抓取後存成
JSON 進版控，CI 只讀 JSON**：外部 API 沒有 SLA，讓部署依賴它等於把別人的
故障變成自己的故障。這與 data/news.json 的既有模式一致。

用法：
    python3 tariff.py fetch          # 從 USITC 抓取並更新 data/tariff.json
    python3 tariff.py compare 6109.10.00   # 比較各產地的總稅率
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "tariff.json"

HTS_API = "https://hts.usitc.gov/reststop/exportList"

# 第 99 章是「臨時性調整」專章，額外關稅掛在這裡而非商品本身的章節。
# 抓取範圍涵蓋整個 9903，但**只有 9903.05 系列進入比較**（見下）。
ADDITIONAL_DUTY_RANGE = ("9903.01", "9903.99")

# 只採 9903.05 系列——2026-07-24 上路的「強迫勞動 301」。
#
# 這是整個工具最關鍵的一條規則。第 99 章裡至少有四套並存的關稅制度：
#   9903.88.* / 9903.91.*  2018 年起的對中 301，依產品清單分級（7.5%~100%）
#   9903.01.* / 9903.02.*  對等關稅（34%、46%…）
#   9903.05.*              強迫勞動 301，**逐國單一稅率**
# 若混在一起取值，中國會因為 9903.88.15 的 7.5%（某個特定商品類別的舊條號）
# 被算成「比孟加拉便宜」——實測第一版就這樣算出中國 24.0% < 孟加拉 26.5%，
# 與 2026-07 的實際情況（中國 12.5%、孟加拉 10%）完全相反。
#
# **一個會讓人賠錢的工具比沒有工具更糟**，所以寧可只涵蓋一套制度並明說範圍，
# 也不要把不可比的數字混在一起。要擴充到其他制度時，必須逐套分開呈現而非合併。
SERIES_PREFIX = "9903.05"

# 9903.05 系列的描述格式高度一致：
#   "Except for products described in headings 9903.05.85–9903.05.92,
#    articles the product of {國家}, as provided for in U.S. note 52..."
# 用這個格式抓國名而非維護白名單——第一版用白名單漏掉 40 個國家（阿爾及利亞、
# 安哥拉、阿根廷…），而漏掉的國家會顯示成「未知」，讓使用者以為沒有資料。
COUNTRY_PATTERN = re.compile(
    r"articles\s+the\s+product\s+of\s+(?:the\s+)?"
    r"([A-Z][A-Za-z]*(?:[\s'-][A-Z][A-Za-z]*)*)"
    r"\s*,\s*as\s+provided", re.IGNORECASE)

# 有自由貿易協定的國家在 special 欄位以代碼列出，Free (AU,BH,CL,...) 這種。
# 代碼對照表只放實際會用到的，完整清單見 HTS 的 General Note 3(c)。
FTA_CODES = {
    "AU": "Australia", "BH": "Bahrain", "CL": "Chile", "CO": "Colombia",
    "IL": "Israel", "JO": "Jordan", "KR": "Korea", "MA": "Morocco",
    "OM": "Oman", "P": "Panama", "PA": "Panama", "PE": "Peru",
    "S": "Singapore", "SG": "Singapore", "CA": "Canada", "MX": "Mexico",
}


def api_get(from_code, to_code, timeout=30):
    """向 USITC 取一段 HTS 區間。失敗時回 None 而非拋出——抓取是批次作業，
    單一區間失敗不該中斷整批。"""
    url = (f"{HTS_API}?from={from_code}&to={to_code}"
           f"&format=JSON&styles=false")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  ⚠️  取得 {from_code}~{to_code} 失敗：{e}", file=sys.stderr)
        return None


def parse_rate(text):
    """從 "The duty provided in the applicable subheading + 12.5%" 取出 12.5。

    取不到時回 None 而非 0——「沒有額外關稅」與「解析失敗」是兩件事，
    後者混成 0 會讓比較結果靜默錯誤。
    """
    if not text:
        return None
    m = re.search(r"\+\s*([\d.]+)\s*%", text)
    return float(m.group(1)) if m else None


def parse_countries(description):
    """從描述文字抓出適用國家。

    只認 "articles the product of X, as provided" 這個固定句式——描述裡
    另有大量 "Except for products described in headings 9903.05.85–…" 的
    交叉引用，用寬鬆的比對會把條號或其他專有名詞誤判成國名。
    """
    if not description:
        return []
    m = COUNTRY_PATTERN.search(description)
    return [m.group(1).strip()] if m else []


def fetch_additional_duties():
    """抓第 99 章的額外關稅，回傳 {國家: [{"hts":…, "rate":…, "desc":…}]}。"""
    rows = api_get(*ADDITIONAL_DUTY_RANGE)
    if not rows:
        return {}
    by_country = {}
    unmatched = 0
    for r in rows:
        hts = r.get("htsno") or ""
        rate = parse_rate(r.get("general"))
        # 只收 9903.05 系列：混入其他制度會讓比較結果失真（見 SERIES_PREFIX）
        if not hts.startswith(SERIES_PREFIX) or rate is None:
            continue
        desc = r.get("description") or ""
        countries = parse_countries(desc)
        if not countries:
            unmatched += 1
            continue
        for c in countries:
            by_country.setdefault(c, []).append(
                {"hts": hts, "rate": rate, "desc": desc[:200]})
    if unmatched:
        # 交叉引用的條目抓不到國名是預期內的，但數量要可見——若哪天暴增，
        # 代表 HTS 改了描述格式而 parse_countries 需要跟上。
        print(f"  （{unmatched} 筆額外關稅條目無法對應到國家，多為交叉引用）")
    return by_country


def fetch_product(hts_code):
    """抓單一 HS code 的基本稅率。取區間是因為 API 以區間查詢，
    精確碼要從回傳結果裡挑。"""
    prefix = hts_code.split(".")[0]
    rows = api_get(prefix, str(int(prefix) + 1))
    if not rows:
        return None
    for r in rows:
        if (r.get("htsno") or "") == hts_code:
            return r
    # 找不到完全相符時退回前綴相符的第一筆（有稅率的）
    for r in rows:
        if (r.get("htsno") or "").startswith(hts_code) and r.get("general"):
            return r
    return None


def fta_countries(special_text):
    """從 "Free (AU,BH,CL,...)" 取出享免稅的國家名。"""
    if not special_text:
        return set()
    m = re.search(r"Free\s*\(([^)]+)\)", special_text)
    if not m:
        return set()
    return {FTA_CODES[c.strip()] for c in m.group(1).split(",")
            if c.strip() in FTA_CODES}


def cmd_fetch(args):
    """抓取並更新 data/tariff.json。

    刻意只抓額外關稅（第 99 章）而非整份 HTS：後者有數萬筆、多數用不到，
    而基本稅率在 compare 時才按需查詢。
    """
    print("從 USITC HTS API 抓取額外關稅（第 99 章）…")
    duties = fetch_additional_duties()
    if not duties:
        sys.exit("抓取失敗，未更新既有資料")

    data = {
        "source": HTS_API,
        "note": "由 tariff.py fetch 產生。CI 只讀這份 JSON，不打外部 API。",
        "additional_duties": duties,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    total = sum(len(v) for v in duties.values())
    print(f"已寫入 {DATA_PATH}：{len(duties)} 個國家、{total} 筆額外關稅條目")
    for c in sorted(duties, key=lambda k: -len(duties[k]))[:8]:
        rates = sorted({d["rate"] for d in duties[c]})
        print(f"  {c:12} {len(duties[c]):3d} 筆  稅率 {rates}")


def load_data():
    if not DATA_PATH.exists():
        sys.exit(f"找不到 {DATA_PATH}，先跑 python3 tariff.py fetch")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def compare(hts_code, product_row, duties):
    """算出各產地的總稅率，回傳依總稅率排序的 list。

    總稅率 = 基本稅率（或 FTA 免稅）+ 該國的強迫勞動 301 稅率。

    只採 9903.05 系列後，同一國通常只有一個稅率（該制度是逐國統一的）。
    仍取最低值是為了防禦：若哪天同一國出現多條，取最低是保守下界——
    高估省下的錢會誤導決策，而這個工具的使用情境是要花錢移產線。
    """
    general = product_row.get("general") or ""
    base = parse_rate("+" + general) if "%" in general else None
    if base is None:
        m = re.match(r"\s*([\d.]+)\s*%", general)
        base = float(m.group(1)) if m else 0.0
    fta = fta_countries(product_row.get("special"))

    # 只列出資料裡實際有 301 稅率的國家。刻意不補上「未知」的國家——
    # 那會讓使用者以為該國沒有額外關稅，而實際上只是這個工具沒涵蓋。
    out = []
    for country, entries in duties.items():
        extra = min(d["rate"] for d in entries)
        base_rate = 0.0 if country in fta else base
        out.append({
            "country": country,
            "base": base_rate,
            "fta": country in fta,
            "extra": extra,
            "total": base_rate + extra,
        })
    return sorted(out, key=lambda x: x["total"])


def cmd_compare(args):
    data = load_data()
    duties = data["additional_duties"]
    print(f"查詢 {args.hts_code} 的基本稅率…")
    row = fetch_product(args.hts_code)
    if not row:
        sys.exit(f"找不到 HS code {args.hts_code}")

    desc = (row.get("description") or "").strip()
    print(f"\n{args.hts_code}  {desc[:70]}")
    print(f"基本稅率：{row.get('general') or '（無）'}")
    if row.get("special"):
        print(f"優惠協定：{row['special'][:70]}")
    print()

    rows = compare(args.hts_code, row, duties)
    origin = args.origin
    print(f"{'產地':<12} {'基本':>7} {'301':>7} {'合計':>8}")
    print("-" * 40)
    base_total = None
    for r in rows:
        if r["country"] == origin:
            base_total = r["total"]
    shown = rows if args.all else [
        r for r in rows
        if r["country"] == origin or base_total is None or r["total"] < base_total
    ][:args.limit]
    for r in shown:
        note = ""
        if r["country"] == origin:
            note = "  ← 目前"
        elif base_total is not None and r["total"] < base_total:
            note = f"  省 {base_total - r['total']:.1f} 個百分點"
        fta = "（FTA 免稅）" if r["fta"] else ""
        print(f"{r['country']:<22} {r['base']:>6.1f}% {r['extra']:>+6.1f}% "
              f"{r['total']:>7.1f}%{note}{fta}")

    if not args.all and len(rows) > len(shown):
        print(f"\n（共 {len(rows)} 國有此關稅，上方只列比目前產地便宜的；"
              f"完整清單用 --all）")
    print()
    print("※ 只涵蓋 2026-07-24 上路的強迫勞動 301（HTS 9903.05 系列）。")
    print("  第 99 章另有對中 301（9903.88/9903.91）與對等關稅（9903.01/02）")
    print("  等並存制度，實際應繳稅額需合併計算，請以 HTS 條文與報關行為準。")


def main():
    p = argparse.ArgumentParser(description="美國 301 關稅的產地比較")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="從 USITC 抓取額外關稅並更新 data/tariff.json")

    pc = sub.add_parser("compare", help="比較各產地的總稅率")
    pc.add_argument("hts_code", help="HS code，例如 6109.10.00")
    pc.add_argument("--origin", default="China", help="目前的生產地（預設 China）")
    pc.add_argument("--all", action="store_true", help="列出所有國家而非只列更便宜的")
    pc.add_argument("--limit", type=int, default=12, help="最多列幾國（預設 12）")

    args = p.parse_args()
    {"fetch": cmd_fetch, "compare": cmd_compare}[args.command](args)


if __name__ == "__main__":
    main()
