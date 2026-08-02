#!/usr/bin/env python3
"""關稅比較頁的靜態產生器。

輸出一個單頁工具：選 HS code 與現在的產地，看各國的總稅率比較。

**刻意獨立於 server.py**：新聞評分與關稅查詢是兩件事，共用同一個模組只會
讓那邊更肥。共用的只有樣式常數（避免兩套視覺）。

**資料在建置時內嵌進頁面**，不在瀏覽器端打 API：USITC 沒有 CORS 標頭，
前端直接呼叫會被擋；而且外部 API 沒有 SLA，讓頁面依賴它等於把別人的故障
變成自己的。資料由 `tariff.py fetch` 在本機更新並進版控。
"""

import json
from html import escape
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "tariff.json"

# 常見出口品項的 HS code。刻意只放少數幾個而非整份 HTS（數萬筆）：
# 這個頁面回答的是「移產地划不划算」，不是「查所有商品的稅率」。
# 選這幾類是因為它們正是 2026-07 關稅新聞裡被點名的產業。
PRESET_PRODUCTS = [
    ("6109.10.00", "棉質 T 恤", 16.5,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
    ("6203.42.40", "男用棉質長褲", 16.6,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
    ("6403.99.60", "皮革鞋類", 8.5,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
    ("8471.30.01", "筆記型電腦", 0.0,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
    ("8517.13.00", "智慧型手機", 0.0,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
    ("9401.61.40", "布面木框座椅", 0.0,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
    ("8708.29.50", "汽車車身零件", 2.5,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
    ("7318.15.20", "鋼鐵螺栓", 0.0,
     "AU,BH,CL,CO,IL,JO,KR,MA,OM,P,PA,PE,S,SG"),
]

FTA_CODES = {
    "AU": "Australia", "BH": "Bahrain", "CL": "Chile", "CO": "Colombia",
    "IL": "Israel", "JO": "Jordan", "KR": "Korea", "MA": "Morocco",
    "OM": "Oman", "P": "Panama", "PA": "Panama", "PE": "Peru",
    "S": "Singapore", "SG": "Singapore", "CA": "Canada", "MX": "Mexico",
}

# 國名中譯。只放實際會出現的，查不到就顯示原文——寧可顯示英文也不要猜錯。
ZH = {
    "Algeria": "阿爾及利亞", "Angola": "安哥拉", "Argentina": "阿根廷",
    "Australia": "澳洲", "Bahamas": "巴哈馬", "Bahrain": "巴林",
    "Bangladesh": "孟加拉", "Bolivia": "玻利維亞", "Brazil": "巴西",
    "Cambodia": "柬埔寨", "Cameroon": "喀麥隆", "Canada": "加拿大",
    "Chile": "智利", "China": "中國", "Colombia": "哥倫比亞",
    "Costa Rica": "哥斯大黎加", "Dominican Republic": "多明尼加",
    "Ecuador": "厄瓜多", "Egypt": "埃及", "El Salvador": "薩爾瓦多",
    "Ghana": "迦納", "Guatemala": "瓜地馬拉", "Honduras": "宏都拉斯",
    "Hong Kong": "香港", "India": "印度", "Indonesia": "印尼",
    "Israel": "以色列", "Japan": "日本", "Jordan": "約旦",
    "Kazakhstan": "哈薩克", "Korea": "南韓", "Malaysia": "馬來西亞",
    "Mexico": "墨西哥", "Morocco": "摩洛哥", "Nigeria": "奈及利亞",
    "Oman": "阿曼", "Pakistan": "巴基斯坦", "Panama": "巴拿馬",
    "Peru": "秘魯", "Philippines": "菲律賓", "Singapore": "新加坡",
    "South Africa": "南非", "Sri Lanka": "斯里蘭卡", "Taiwan": "台灣",
    "Thailand": "泰國", "Tunisia": "突尼西亞", "Turkey": "土耳其",
    "Ukraine": "烏克蘭", "Vietnam": "越南", "Zimbabwe": "辛巴威",
}

STYLE = """
:root {
  --bg:#f6f7f9; --card:#fff; --text:#1a1d21; --muted:#6b7280;
  --border:#e5e7eb; --link:#2563eb; --good:#16a34a; --bad:#dc2626;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171a; --card:#1c2024; --text:#e8eaed; --muted:#9aa3ad;
          --border:#2c3238; --link:#7aa7ff; --good:#4ade80; --bad:#f87171; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",
  "PingFang TC","Microsoft JhengHei",sans-serif; }
.wrap { max-width:900px; margin:0 auto; padding:28px 18px 60px; }
h1 { font-size:1.5rem; margin:0 0 6px; }
.sub { color:var(--muted); font-size:.9rem; margin-bottom:22px; }
.nav { display:flex; gap:14px; margin-bottom:20px; font-size:.92rem; }
.nav a { color:var(--muted); text-decoration:none; padding:4px 0;
  border-bottom:2px solid transparent; }
.nav a.active { color:var(--text); font-weight:600; border-bottom-color:var(--link); }
.panel { background:var(--card); border:1px solid var(--border);
  border-radius:12px; padding:18px; margin-bottom:18px; }
.controls { display:flex; gap:14px; flex-wrap:wrap; }
.field { flex:1 1 220px; min-width:0; }
label { display:block; font-size:.82rem; color:var(--muted); margin-bottom:5px; }
select { width:100%; padding:9px 10px; border-radius:8px;
  border:1px solid var(--border); background:var(--card); color:var(--text);
  font-size:.95rem; }
table { width:100%; border-collapse:collapse; margin-top:4px; }
th,td { padding:9px 10px; text-align:right; border-bottom:1px solid var(--border); }
th { font-size:.78rem; color:var(--muted); font-weight:500; }
td.name,th.name { text-align:left; }
tr.current { background:color-mix(in srgb,var(--link) 10%,transparent); }
tr.current td { font-weight:600; }
.save { color:var(--good); font-size:.85rem; }
.total { font-weight:600; }
.tag { font-size:.72rem; padding:1px 6px; border-radius:999px;
  background:var(--border); color:var(--muted); margin-left:6px; }
.caveat { color:var(--muted); font-size:.82rem; line-height:1.7;
  border-top:1px solid var(--border); padding-top:14px; margin-top:6px; }
.summary { font-size:1.02rem; margin:0 0 12px; }
.scroll { overflow-x:auto; }
@media (max-width:600px) { .wrap { padding:20px 13px 44px; } th,td { padding:8px 6px; } }
"""

JS = """
const DATA = __DATA__;
const PRODUCTS = __PRODUCTS__;
const ZH = __ZH__;
const FTA = __FTA__;

function ftaSet(codes) {
  const s = new Set();
  (codes || '').split(',').forEach(c => { const n = FTA[c.trim()]; if (n) s.add(n); });
  return s;
}

function render() {
  const hts = document.getElementById('product').value;
  const origin = document.getElementById('origin').value;
  const p = PRODUCTS.find(x => x[0] === hts);
  if (!p) return;
  const [, name, base, ftaCodes] = p;
  const fta = ftaSet(ftaCodes);

  const rows = Object.entries(DATA).map(([country, entries]) => {
    const extra = Math.min(...entries.map(e => e.rate));
    const isFta = fta.has(country);
    const b = isFta ? 0 : base;
    return { country, base: b, fta: isFta, extra, total: b + extra };
  }).sort((a, b) => a.total - b.total);

  const cur = rows.find(r => r.country === origin);
  const curTotal = cur ? cur.total : null;
  const cheaper = curTotal === null ? [] : rows.filter(r => r.total < curTotal);

  let html = '';
  if (cur) {
    html += `<p class="summary">目前從<b>${ZH[origin] || origin}</b>出口，合計稅率 ` +
      `<b>${cur.total.toFixed(1)}%</b>。` +
      (cheaper.length
        ? `有 <b>${cheaper.length}</b> 個產地更便宜，最多可省 ` +
          `<b class="save">${(curTotal - cheaper[0].total).toFixed(1)} 個百分點</b>。`
        : '目前沒有更便宜的產地。') + '</p>';
  }

  html += '<div class="scroll"><table><thead><tr>' +
    '<th class="name">產地</th><th>基本稅率</th><th>301 加徵</th><th>合計</th>' +
    '<th class="name">與目前相比</th></tr></thead><tbody>';
  const show = document.getElementById('all').checked ? rows : rows.slice(0, 15);
  show.forEach(r => {
    const isCur = r.country === origin;
    let diff = '';
    if (!isCur && curTotal !== null) {
      const d = curTotal - r.total;
      diff = d > 0 ? `<span class="save">省 ${d.toFixed(1)}</span>`
                   : (d < 0 ? `多 ${(-d).toFixed(1)}` : '相同');
    }
    html += `<tr class="${isCur ? 'current' : ''}">` +
      `<td class="name">${ZH[r.country] || r.country}` +
      (r.fta ? '<span class="tag">FTA 免稅</span>' : '') +
      (isCur ? '<span class="tag">目前</span>' : '') + '</td>' +
      `<td>${r.base.toFixed(1)}%</td><td>+${r.extra.toFixed(1)}%</td>` +
      `<td class="total">${r.total.toFixed(1)}%</td>` +
      `<td class="name">${diff}</td></tr>`;
  });
  html += '</tbody></table></div>';
  if (!document.getElementById('all').checked && rows.length > 15) {
    html += `<p class="caveat">共 ${rows.length} 國，上方顯示最便宜的 15 國。</p>`;
  }
  document.getElementById('result').innerHTML = html;
}

document.addEventListener('DOMContentLoaded', () => {
  ['product', 'origin', 'all'].forEach(id =>
    document.getElementById(id).addEventListener('change', render));
  render();
});
"""


def load_duties():
    if not DATA_PATH.exists():
        raise SystemExit(f"找不到 {DATA_PATH}，先跑 python3 tariff.py fetch")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))["additional_duties"]


def render_page(site_title="美國 301 關稅產地比較"):
    duties = load_duties()
    countries = sorted(duties, key=lambda c: ZH.get(c, c))
    opts = "".join(
        f'<option value="{escape(c)}"{" selected" if c == "China" else ""}>'
        f'{escape(ZH.get(c, c))}</option>' for c in countries)
    prod_opts = "".join(
        f'<option value="{escape(h)}">{escape(n)}（{escape(h)}）</option>'
        for h, n, _b, _f in PRESET_PRODUCTS)

    js = (JS.replace("__DATA__", json.dumps(duties, ensure_ascii=False))
            .replace("__PRODUCTS__", json.dumps(PRESET_PRODUCTS, ensure_ascii=False))
            .replace("__ZH__", json.dumps(ZH, ensure_ascii=False))
            .replace("__FTA__", json.dumps(FTA_CODES, ensure_ascii=False)))

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(site_title)}</title>
<meta name="description" content="比較同一件產品在各國生產時的美國進口關稅，含 2026-07-24 上路的強迫勞動 301 加徵。">
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<h1>🧾 {escape(site_title)}</h1>
<div class="sub">同一件產品換個國家生產，關稅差多少｜資料來自 USITC HTS</div>
<div class="nav">
<a href="./">📰 新聞評分</a>
<a href="tariff.html" class="active">🧾 關稅比較</a>
</div>

<div class="panel">
<div class="controls">
<div class="field"><label for="product">產品</label>
<select id="product">{prod_opts}</select></div>
<div class="field"><label for="origin">目前的生產地</label>
<select id="origin">{opts}</select></div>
<div class="field" style="flex:0 0 auto;align-self:flex-end">
<label style="display:inline"><input type="checkbox" id="all"> 顯示全部國家</label></div>
</div>
</div>

<div class="panel" id="result"></div>

<div class="panel">
<div class="caveat">
<b>這個工具的範圍</b><br>
只涵蓋 <b>2026-07-24 上路的強迫勞動 301 關稅</b>（HTS 9903.05 系列），
那是當時一次擴及約 60 國、逐國單一稅率的制度。<br><br>
美國第 99 章另有並存的關稅制度——對中 301（9903.88／9903.91，依產品清單
7.5%~100%）、對等關稅（9903.01／9903.02）等。<b>實際應繳稅額需合併計算</b>，
本頁的數字不能直接當成報關依據，請以 HTS 條文與報關行的核算為準。<br><br>
基本稅率取自 USITC HTS 的 general 欄位；FTA 免稅依 special 欄位的協定國清單。
資料由本機定期抓取後進版控，非即時同步。
</div>
</div>
</div>
<script>{js}</script>
</body>
</html>"""


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist/tariff.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    html = render_page()
    out.write_text(html, encoding="utf-8")
    print(f"已輸出 {out}（{len(html.encode('utf-8'))/1024:.0f} KB）")
