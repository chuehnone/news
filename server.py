#!/usr/bin/env python3
"""新聞重要性評分的網頁介面（標準庫實作，無外部依賴）。

啟動：python3 news.py serve [--port 8765]
"""

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
# 只需要 timedelta：所有「現在時刻／今天」一律走 news.py 的 now_local()／
# today_local()（台北時間），不要在這裡改用 datetime.now() 或 date.today()
from datetime import timedelta
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
# 常數一律取自 news.py，勿在此另存一份（見 TestNoDuplicateConstants）。
# news.py 反向 import server 只發生在 cmd_serve / cmd_export 的函式內，故不成環。
from news import (
    ARCHIVE_GRADES, DB_PATH, DIMENSIONS, GRADE_LABELS, GRADES,
    PREDICTION_KINDS, hit_rate, kind_label_of, load_aliases,
    load_hits_from_json, load_positions, normalize_tag, now_local,
    position_accuracy, tag_counts, tags_of, today_local, verdict_mark,
    verified_counts_from_json,
)

STYLE = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #1a1d21; --muted: #6b7280;
  --border: #e5e7eb; --link: #2563eb;
  --s: #dc2626; --a: #ea580c; --b: #ca8a04; --c: #6b7280; --d: #9ca3af;
  /* badge 底色：低飽和色底 + 深色字，避免實心飽和色搶過標題 */
  --s-bg: #fee2e2; --a-bg: #ffedd5; --b-bg: #fef3c7; --c-bg: #f3f4f6; --d-bg: #f3f4f6;
  --s-fg: #991b1b; --a-fg: #9a3412; --b-fg: #854d0e; --c-fg: #4b5563; --d-fg: #6b7280;
  /* one_line 細線色：淺色模式下與等級色同階即可 */
  --s-line: #dc2626; --a-line: #ea580c; --b-line: #ca8a04; --c-line: #9ca3af; --d-line: #b6bcc4;
  --sticky-bg: rgba(246, 247, 249, .92);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1b1f24; --text: #e6e8ea; --muted: #9aa3ad;
    --border: #2c3238; --link: #7ab0ff;
    /* B 級佔六成資料量，深色模式下把亮黃壓暗，避免整頁最常見元素最刺眼 */
    --s: #f87171; --a: #fb923c; --b: #d4a017; --c: #9ca3af; --d: #6b7280;
    --s-bg: #3f1d1d; --a-bg: #3d2411; --b-bg: #3a2f10; --c-bg: #262b31; --d-bg: #262b31;
    --s-fg: #fca5a5; --a-fg: #fdba74; --b-fg: #e3c26b; --c-fg: #c2c8cf; --d-fg: #9aa3ad;
    /* 暗背景上 2px 細線容易糊掉，故比等級色再亮一階 */
    --s-line: #fca5a5; --a-line: #fdba74; --b-line: #e8c877; --c-line: #b6bdc5; --d-line: #8a939c;
    --sticky-bg: rgba(17, 20, 24, .92);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, "PingFang TC", "Noto Sans TC", sans-serif;
  line-height: 1.6;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: .85rem; margin-bottom: 20px; }
/* 等級 tab 列 sticky：321 則的長列表滑到一半想換等級，不該逼使用者滑回頂端。
   date-head 的 top 要讓開這一列，否則日期會鑽到 tab 底下（見 .date-head）。

   --tabbar-h 必須等於這一列的實際高度，兩者一旦不同步，日期標題就會被蓋掉
   （曾經寫死 46px，實測是 50px，每個斷點都疊到 4px）。故不寫死數字，
   改由組成高度的兩個值算出來：上下 padding（--tabbar-pad）加內容行高（--tabbar-row）。
   --tabbar-row 是 .tabs a 的高度：padding 5px×2 + border 1px×2 + .85rem 文字行高。

   負 margin 讓 sticky 底色滿版；min-width: 0 是必要的——手機版 .tabs 改成
   nowrap 水平捲動後，flex 子項的預設 min-width:auto 會把整列撐到內容寬度，
   連帶把整頁撐寬，卡片右側（分數、標題）就被裁到畫面外。 */
:root {
  --tabbar-pad: 8px; --tabbar-row: 34px;
  --tabbar-h: calc(var(--tabbar-row) + var(--tabbar-pad) * 2);
}
.filters {
  display: flex; align-items: center; gap: 8px 16px;
  /* nowrap：這一列一旦折成兩排，實際高度就超過 --tabbar-h（單排高度的算式），
     date-head 的讓位量不足、日期會被蓋住。密度切換移進這一列後，
     wrap 的風險從「tab 太多」變成「tab + 切換太寬」，故明確鎖成一排，
     由 .tabs 自己水平捲動吸收寬度。 */
  flex-wrap: nowrap;
  position: sticky; top: 0; z-index: 10;
  background: var(--sticky-bg); backdrop-filter: blur(6px);
  margin: 0 -16px 10px; padding: var(--tabbar-pad) 16px;
  min-width: 0;
  /* 高度鎖定成與 --tabbar-h 相同的算式，排版與 date-head 的讓位量不會各走各的 */
  min-height: var(--tabbar-h);
}
/* 桌機寬度足夠時 tab 不需捲動，但仍要能在空間不足時讓出寬度給密度切換 */
.tabs {
  display: flex; flex-wrap: nowrap; gap: 8px;
  overflow-x: auto; scrollbar-width: none; min-width: 0; flex: 1 1 auto;
}
/* 右緣淡出，暗示還有 tab 可以往右捲。捲軸被隱藏（見上），少了這個提示
   使用者會以為 tab 只有看得到的那幾顆——實測手機 390px 寬只放得下約
   3.7 顆，被切一半的那顆看起來像壞掉而非可捲。
   只在真的溢出時才套用（.scrollable 由 JS 量測後加上），否則桌機
   放得下 6 顆時最後一顆會平白被淡化。 */
.tabs.scrollable {
  -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent);
  mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent);
}
.tabs::-webkit-scrollbar { display: none; }
/* 密度切換壓縮成單顆鈕：等級 tab 有 6 顆且會隨等級增加，兩者並排時
   tab 必然被擠掉——實測手機版 B 級被切一半、C/D 完全看不到，
   而捲軸是隱藏的，使用者不會知道右邊還有東西可捲。
   單顆鈕把佔用寬度從約 130px 降到 44px，tab 拿回整列。 */
.filters .density { flex: 0 0 auto; }
.density {
  border: 1px solid var(--border); border-radius: 999px; overflow: hidden;
}
.density button {
  width: 44px; height: 30px; padding: 0;
  border: 0; background: var(--card); color: var(--muted);
  font-size: .95rem; font-family: inherit; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.density button:hover { color: var(--link); }
/* 精簡模式時鈕顯示為 active，讓「目前是精簡」有明確的視覺狀態 */
.density button.active { background: var(--text); color: var(--bg); }
.date-filter select, .section-filter select, .tag-filter select, .search input {
  padding: 5px 10px; border-radius: 999px; border: 1px solid var(--border);
  background: var(--card); color: var(--text); font-size: .85rem;
  font-family: inherit; cursor: pointer;
}
.search { flex: 1 1 180px; min-width: 160px; }
.search input { width: 100%; cursor: text; }
.tabs a {
  padding: 5px 14px; border-radius: 999px; border: 1px solid var(--border);
  text-decoration: none; color: var(--text); font-size: .85rem; background: var(--card);
}
.tabs a.active { background: var(--text); color: var(--bg); border-color: var(--text); }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 24px; }
.density { display: flex; gap: 0; border: 1px solid var(--border); border-radius: 999px; overflow: hidden; }
.density button {
  padding: 4px 12px; border: 0; background: var(--card); color: var(--muted);
  font-size: .8rem; font-family: inherit; cursor: pointer;
}
.density button.active { background: var(--text); color: var(--bg); }
/* top 要讓開上方 sticky 的 .filters（tab 列），否則日期會鑽到 tab 底下被蓋住。
   --tabbar-h 由 .filters 的 padding 與行高算出，不是寫死的數字——見上方說明 */
.date-head {
  font-size: 1rem; color: var(--text); margin: 28px 0 10px; font-weight: 700;
  position: sticky; top: var(--tabbar-h); z-index: 5;
  background: var(--sticky-bg); backdrop-filter: blur(6px);
  padding: 6px 0; border-bottom: 1px solid var(--border);
}
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px 18px; margin-bottom: 12px;
  border-left: 4px solid var(--grade-color, var(--border));
}
/* 等級決定卡片視覺重量：S/A 加重，C/D 降權。
   --grade-color 給左側色條，--accent 是亮一階的版本，供暗背景上的細線用 */
.card.S { --grade-color: var(--s); --accent: var(--s-line); box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.card.A { --grade-color: var(--a); --accent: var(--a-line); }
.card.B { --grade-color: var(--b); --accent: var(--b-line); }
.card.C { --grade-color: var(--c); --accent: var(--c-line); opacity: .78; padding: 12px 16px; }
.card.D { --grade-color: var(--d); --accent: var(--d-line); opacity: .62; padding: 12px 16px; }
.card.C .title, .card.D .title { font-size: 1.1rem; }
.card.C .summary, .card.D .summary { display: none; }
.card.C .score, .card.D .score { font-size: .95rem; color: var(--muted); }
.card-top { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.badge {
  font-weight: 700; font-size: .78rem; padding: 2px 9px; border-radius: 6px;
  flex-shrink: 0; letter-spacing: .02em;
}
.badge.S { background: var(--s-bg); color: var(--s-fg); }
.badge.A { background: var(--a-bg); color: var(--a-fg); }
.badge.B { background: var(--b-bg); color: var(--b-fg); }
.badge.C { background: var(--c-bg); color: var(--c-fg); }
.badge.D { background: var(--d-bg); color: var(--d-fg); }
.grade-label { color: var(--muted); font-size: .8rem; }
/* 分數是 100 檔的細粒度資訊（等級只有 5 檔），權重要高於 badge 與 label */
.score {
  font-weight: 700; font-variant-numeric: tabular-nums;
  color: var(--text); font-size: 1.05rem; margin-left: auto;
}
.title { font-size: 1.32rem; font-weight: 680; line-height: 1.4; margin: 8px 0 6px; }
/* one_line 是判斷而非複述，升格為卡片主角 */
.one-line {
  font-size: .95rem; color: var(--text); margin: 8px 0;
  /* padding 要夠寬，折行的第二行才不會看起來懸空在細線旁 */
  padding-left: 14px; border-left: 3px solid var(--accent, var(--border));
}
.summary { color: var(--muted); font-size: .88rem; margin: 6px 0; }
.meta { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 10px; font-size: .84rem; }
.meta a { color: var(--link); text-decoration: none; }
.meta a:hover { text-decoration: underline; }
.meta .section { color: var(--muted); }
/* 靜態站是 button（JS 篩選）、動態站是 a（query string），兩者外觀要一致 */
.meta button.section, .meta a.section {
  border: 0; background: none; padding: 0; font: inherit; font-size: .84rem;
  color: var(--muted); cursor: pointer; text-decoration: none;
}
.meta button.section:hover, .meta a.section:hover { text-decoration: underline; }
/* 標籤：把講同一件事的新聞串起來，點一下就篩出同標籤的其他則。
   視覺上刻意比 badge 輕（無底色飽和度），避免跟等級搶讀者的第一眼 */
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
/* 靜態站是 button、動態站是 a（見 render_card 的 static 參數），樣式共用 */
.tag {
  display: inline-block;
  font-size: .78rem; padding: 2px 10px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg); color: var(--muted);
  font-family: inherit; cursor: pointer; line-height: 1.5;
  text-decoration: none;
}
.tag:hover { border-color: var(--link); color: var(--link); text-decoration: none; }
.tag.active { background: var(--text); color: var(--bg); border-color: var(--text); }
/* 同標籤的其他新聞：關聯的實際載體，收在評分細節裡不佔卡片版面 */
.related { margin-top: 12px; }
.related h4 { margin: 12px 0 4px; font-size: .84rem; color: var(--muted); }
.related ul { margin: 4px 0; padding-left: 20px; }
.related li { margin: 3px 0; }
.related .rel-grade {
  font-variant-numeric: tabular-nums; color: var(--muted); font-size: .8rem;
  margin-right: 6px;
}
details { margin-top: 10px; }
summary { cursor: pointer; font-size: .84rem; color: var(--muted); user-select: none; }
.detail { font-size: .88rem; margin-top: 10px; }
.detail h4 { margin: 12px 0 4px; font-size: .84rem; color: var(--muted); }
.detail p { margin: 2px 0; }
.detail ul { margin: 4px 0; padding-left: 20px; }
/* 已回頭驗證且成真的觀察指標。只標命中，未判定與未發生一律維持原樣
   （見 render_card 的說明），所以 ✓ 是加分標記而非狀態欄。 */
.wn-hit { color: #16a34a; font-weight: 700; margin-left: -14px; margin-right: 4px; }
.wn-done { list-style: none; }
.wn-ev { font-size: .8rem; margin-left: 4px; white-space: nowrap; }
.wn-summary { margin-top: 6px !important; font-size: .8rem; color: var(--muted); }
@media (prefers-color-scheme: dark) { .wn-hit { color: #4ade80; } }
.dims { width: 100%; border-collapse: collapse; margin-top: 6px; }
.dims th, .dims td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
.dims th { font-size: .8rem; color: var(--muted); font-weight: 600; }
.dims td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
/* 更新提示：固定在底部中央，不擋住卡片內容也不需要使用者捲到特定位置。
   只有 JS 偵測到 ETag 變動時才會被插入 DOM，動態站不會出現 */
.update-bar {
  position: fixed; left: 50%; transform: translateX(-50%);
  bottom: 20px; z-index: 20;
  padding: 9px 18px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--text); color: var(--bg);
  font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer;
  box-shadow: 0 2px 12px rgba(0,0,0,.25);
  /* 不換行：折成兩行的膠囊在手機上很擠，寧可縮字級也要維持單行 */
  white-space: nowrap;
  max-width: calc(100vw - 24px);
}
.empty { text-align: center; color: var(--muted); padding: 60px 0; }
.footer { text-align: center; color: var(--muted); font-size: .8rem; margin-top: 32px; }
/* 精簡模式：一則一行，只留等級與標題 */
body.compact .summary, body.compact .one-line,
body.compact .meta, body.compact .tags, body.compact details { display: none; }
body.compact .card { padding: 8px 14px; margin-bottom: 6px; }
body.compact .title { font-size: .98rem; margin: 2px 0; }
body.compact .card.C, body.compact .card.D { opacity: .7; }
/* 精簡模式隱藏等級文字標籤：badge 已經說了 A/B/C，「重要新聞」是同一件事
   講第二次，卻佔掉標題旁最顯眼的位置。完整模式版面寬鬆才保留它。 */
body.compact .grade-label { display: none; }
/* 分數靠右貼齊展開鈕，不再置中懸空——左 badge、右（分數＋鈕）兩個群組，
   比原本三個各自為政的區塊好掃視 */
body.compact .score { margin-left: auto; }

/* 逐張展開：精簡模式下，標題本身是外部連結（點了就離站），
   沒有展開鈕的話就無法在站內先看評分再決定要不要讀原文。
   .expanded 只是把該張卡片的隱藏區塊還原，故用同一組選擇器加高權重覆蓋。 */
/* 明確寫回各自的原始 display（.meta/.tags 是 flex，其餘是 block），
   不用 revert——它會還原成瀏覽器預設值而非本表的設定，flex 版面會壞掉 */
body.compact .card.expanded .summary, body.compact .card.expanded .one-line,
body.compact .card.expanded details { display: block; }
body.compact .card.expanded .meta, body.compact .card.expanded .tags { display: flex; }
body.compact .card.expanded { padding: 14px 16px; margin-bottom: 12px; }
body.compact .card.expanded .title { font-size: 1.2rem; }
body.compact .card.expanded.C, body.compact .card.expanded.D { opacity: 1; }
/* 展開鈕只在精簡模式出現：完整模式下所有內容本來就都看得到 */
.expand { display: none; }
body.compact .expand {
  display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  /* 44×44 是觸控目標的下限（原本 26px 太小不好按）。用負 margin 讓
     視覺留白不因為加大熱區而變胖——按得到的範圍比看起來的大。 */
  width: 44px; height: 44px; margin: -9px -9px -9px 4px; padding: 0;
  border: 0; background: none; color: var(--muted);
  font-size: 1.1rem; font-family: inherit; line-height: 1; cursor: pointer;
}
body.compact .expand:hover { border-color: var(--link); color: var(--link); }
body.compact .card.expanded .expand { transform: rotate(180deg); }

/* 手機：篩選區原本吃掉近半個首屏（6 顆 tab 換行成兩排 + 3 個 select 換行 + 搜尋框），
   要滑一下才看得到第一則新聞。改為 tab 單行水平捲動、select 併排、字級與留白下修。 */
@media (max-width: 600px) {
  /* 只改 padding，--tabbar-h 會跟著 calc 自動縮小，不需要（也不該）另外寫死高度 */
  :root { --tabbar-pad: 6px; }
  .wrap { padding: 16px 12px 48px; }
  h1 { font-size: 1.2rem; }
  .sub { font-size: .8rem; margin-bottom: 14px; }
  /* nowrap：這一列必須恆為一排，否則實際高度會超過 --tabbar-h 的算式，
     日期列讓位不足而被蓋住（--tabbar-h 是單排高度，見上方 :root 的說明） */
  .filters { margin: 0 -12px 8px; padding: var(--tabbar-pad) 12px; flex-wrap: nowrap; }
  /* 密度切換不參與壓縮，寬度固定；被壓縮時「完整／精簡」會被截字 */
  .filters .density { flex: 0 0 auto; }
  /* 不 wrap，改水平捲動：6 顆 tab 恆為一排，省下第二排的高度。
     -webkit-overflow-scrolling 讓 iOS Safari 有慣性捲動 */
  /* min-width: 0 讓這個 flex 子項可以縮到比內容窄，捲動才被關在這一列裡；
     少了它，nowrap 的 tab 會把 .filters 連同整頁一起撐寬，卡片右緣被裁掉。
     flex: 1 1 0 取代 width: 100%：密度切換與 tab 同列後，tab 若堅持佔滿
     整排會把切換擠到第二排，.filters 變兩排高而 --tabbar-h 仍以一排計算，
     日期列的讓位量就不夠、被蓋住。改成可伸縮後 tab 讓出切換所需的寬度，
     自己在剩餘空間內水平捲動。 */
  .tabs {
    flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
    scrollbar-width: none; flex: 1 1 0; min-width: 0;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tabs a { flex-shrink: 0; }
  .toolbar { gap: 6px; margin-bottom: 16px; }
  /* 三個 select 各佔約 1/3 排成一排，搜尋框與密度切換強制換到下一排。
     flex-basis 用百分比而非 0——用 0 時剩餘空間會被同排的搜尋框搶走，
     select 縮到只剩下拉箭頭、看不到「全部日期」等標籤。 */
  .date-filter, .section-filter, .tag-filter { flex: 1 1 28%; min-width: 0; }
  .date-filter select, .section-filter select, .tag-filter select {
    width: 100%; padding: 5px 8px; font-size: .78rem;
    /* 標籤名可能很長，寧可截斷也不要把 select 撐爆版面 */
    text-overflow: ellipsis;
  }
  /* 佔滿整排，把搜尋框擠到 select 的下一行 */
  .search { flex: 1 1 100%; min-width: 0; }
  .density { flex: 0 0 auto; }
  .card { padding: 14px 14px; }
  .card.C, .card.D { padding: 11px 13px; }
  .title { font-size: 1.15rem; }
  .card.C .title, .card.D .title { font-size: 1.02rem; }
  .one-line { font-size: .9rem; padding-left: 11px; }
  .summary { font-size: .84rem; }
  /* 評分細節的五面向表格在窄螢幕會被壓到換行難讀，縮字級並收窄內距 */
  .dims th, .dims td { padding: 4px 5px; font-size: .82rem; }
  .update-bar { font-size: .8rem; padding: 8px 14px; bottom: 16px; }
}

/* ── positions 頁專用（本機 serve，不上靜態站）───────────────────── */
.nav { display: flex; gap: 14px; margin: 0 0 16px; font-size: .92rem; }
.nav a { color: var(--muted); text-decoration: none; padding: 4px 0;
         border-bottom: 2px solid transparent; }
.nav a.active { color: var(--text); font-weight: 600; border-bottom-color: var(--link); }
.pos { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
       padding: 16px 18px; margin-bottom: 14px; }
.pos-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.ticker { font-weight: 700; font-size: 1.12rem; letter-spacing: .02em; }
.pos-date { color: var(--muted); font-size: .84rem; }
.thesis { font-size: 1.02rem; margin: 8px 0 4px; line-height: 1.5; }
.rationale { color: var(--muted); font-size: .88rem; line-height: 1.6;
             padding-left: 11px; border-left: 2px solid var(--border); margin: 8px 0 12px; }
.preds { list-style: none; padding: 0; margin: 10px 0 0; }
.preds li { display: flex; gap: 8px; align-items: flex-start; padding: 6px 0;
            border-top: 1px dashed var(--border); font-size: .92rem; line-height: 1.5; }
.verdict { flex: 0 0 auto; width: 1.4em; text-align: center; font-weight: 700; }
.v-hit { color: #16a34a; }
.v-miss { color: #dc2626; }
.v-moot { color: var(--muted); }
/* void 比 moot 更淡：它不是一種判定結果，是「這條當初就不該這樣寫」 */
.v-void { color: var(--muted); opacity: .4; }
.v-open { color: var(--muted); opacity: .55; }
.pred-hint { color: var(--muted); font-size: .78rem; margin-top: 2px; opacity: .85; }
.kind { flex: 0 0 auto; font-size: .74rem; padding: 1px 7px; border-radius: 999px;
        background: var(--c-bg); color: var(--c-fg); white-space: nowrap; margin-top: 1px; }
.pred-body { flex: 1 1 auto; min-width: 0; }
.pred-note { color: var(--muted); font-size: .82rem; margin-top: 2px; }
.due { color: var(--muted); font-size: .8rem; white-space: nowrap; }
.due.over { color: #dc2626; }
.stats { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
         padding: 14px 18px; margin-bottom: 18px; }
.stats-row { display: flex; gap: 22px; flex-wrap: wrap; margin-top: 8px; }
.stat b { font-size: 1.3rem; }
.stat span { color: var(--muted); font-size: .82rem; display: block; }
.caveat { color: var(--muted); font-size: .82rem; margin-top: 10px; line-height: 1.6; }
"""


# 保留期分層：只在輸出靜態站時套用（`news.py export --retention`，由 CI 使用）。
# 新聞評分的價值隨時間衰減，舊的低分新聞不會有人回頭看，卻會讓靜態站無上限成長。
#
# 刻意不套用在 export-json／add：data/news.json 必須維持 db 的完整鏡像，
# 否則 import-json --replace 會毀掉資料、且匯出結果會隨執行日期而變動。
# 過濾只發生在「產生網站」這一步，不影響任何資料寫入路徑。
RECENT_DAYS = 30      # 近 30 天：全部等級
ARCHIVE_DAYS = 90     # 30-90 天：只留 S/A；超過 90 天不輸出

# 由 export_static() 設定；None 代表不過濾（本機 serve/export 的預設）
_RETENTION_TODAY = None


def retention_clause():
    """回傳 (sql_fragment, params)；未啟用保留期時為空條件。"""
    if _RETENTION_TODAY is None:
        return "", []
    recent_from = (_RETENTION_TODAY - timedelta(days=RECENT_DAYS)).isoformat()
    archive_from = (_RETENTION_TODAY - timedelta(days=ARCHIVE_DAYS)).isoformat()
    # news_date 為空的資料無從判斷時效，一律保留，避免靜默遺失
    placeholders = ",".join("?" * len(ARCHIVE_GRADES))
    return (
        "(news_date IS NULL OR news_date = '' OR news_date >= ?"
        f" OR (news_date >= ? AND grade IN ({placeholders})))",
        [recent_from, archive_from, *ARCHIVE_GRADES],
    )


def query_news_all():
    """不套用保留期的完整筆數，供 export 比對用。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT id FROM news").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def filter_by_tag(rows, tag):
    """留下掛了指定標籤的資料（整個標籤相等，不是子字串）。

    刻意不在 SQL 用 LIKE '%NVIDIA%'：tags 存的是 JSON 陣列字串，
    子字串比對會讓「NVIDIA」命中「NVIDIA供應鏈」，篩選結果就不精準了。
    資料量是數百筆的規模，取回後在 Python 端精確比對足夠快。

    這裡是純粹的整值比對，不碰別名——db 存的本來就是正規名（正規化只發生
    在寫入路徑），而靜態站的前端沒有別名表可查。若這裡偷偷多做一層收斂，
    動態站與靜態站對同一個 ?tag= 就會給出不同結果，而 TestFilterParity
    只比對兩邊、不會發現其中一邊「多做了事」。
    別名要收斂的是使用者手打的網址，那屬於輸入處理層（見 Handler.do_GET）。
    """
    return [r for r in rows if tag in tags_of(r)]


def query_news(grade=None, date=None, section=None, q=None, tag=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:  # 若尚未 init（table 不存在），回傳空列表而非噴錯
        sql = "SELECT * FROM news"
        conds, params = [], []
        rc, rp = retention_clause()
        if rc:
            conds.append(rc)
            params += rp
        if grade:
            conds.append("grade = ?")
            params.append(grade)
        if date:
            conds.append("news_date = ?")
            params.append(date)
        if section:
            conds.append("section = ?")
            params.append(section)
        if q:
            conds.append(
                "(LOWER(title) LIKE ? OR LOWER(COALESCE(one_line,'')) LIKE ?"
                " OR LOWER(COALESCE(summary,'')) LIKE ?"
                " OR LOWER(COALESCE(tags,'')) LIKE ?)"
            )
            params += [f"%{q.lower()}%"] * 4
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY news_date DESC, total_score DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
        if tag:
            rows = filter_by_tag(rows, tag)
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def grade_counts(rows):
    """各級筆數（與前端 apply() 的 perGrade 行為一致）。

    吃已篩好的 rows 而非自己再查一次：篩選條件曾經是兩份各自維護的 WHERE
    （加 tag 時就得改兩處，漏掉一處分頁計數就會與實際卡片數不符）。
    傳入「等級以外的條件都套用過」的那份 rows，條件就永遠只有一份。
    """
    counts = {}
    for r in rows:
        counts[r["grade"]] = counts.get(r["grade"], 0) + 1
    return counts


def date_counts():
    """回傳 [(news_date, count), ...]，日期新到舊。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rc, rp = retention_clause()
        rows = conn.execute(
            "SELECT news_date, COUNT(*) FROM news"
            " WHERE news_date IS NOT NULL AND news_date != ''"
            + (f" AND {rc}" if rc else "")
            + " GROUP BY news_date ORDER BY news_date DESC",
            rp,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def section_counts():
    """回傳 [(section, count), ...]，筆數多的在前。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rc, rp = retention_clause()
        rows = conn.execute(
            "SELECT section, COUNT(*) c FROM news"
            " WHERE section IS NOT NULL AND section != ''"
            + (f" AND {rc}" if rc else "")
            + " GROUP BY section ORDER BY c DESC",
            rp,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


# 靜態站的公開網址（結尾帶斜線）。og:image / og:url 必須是絕對網址，
# 相對路徑在 Slack、Threads 等平台一律抓不到圖。
# 換網域或改用他人的 fork 時用環境變數覆蓋，不必改程式碼。
#
# 必須是 https：這個網址會變成 og:image 與 canonical。該網域的 http 不會
# 自動轉址（直接回 200），所以填 http 的話，分享出去的預覽圖網址就是 http，
# 有些平台會因混合內容而不抓圖，搜尋引擎也會把兩種 scheme 視為不同頁面。
SITE_URL = os.environ.get("NEWS_SITE_URL", "https://chuehnone.viovie.co/news/")

# 分享預覽圖。刻意是「進版控的 PNG」而不是 export 當下生成的 SVG：
#
# SVG 的文字要靠**對方伺服器**有中文字型才畫得出來（Slack、Threads 等平台
# 是在自己的機器上算縮圖），而它們沒有 PingFang／Noto Sans TC，結果是整張
# 圖的中文全變成顯示 Unicode 碼位的豆腐方塊。本機用 qlmanage 預覽看不出來，
# 因為那是拿自己的字型畫的。
#
# 所以改成本機把文字燒進 PNG（`news.py og`）、圖片進版控，export 只負責複製。
# CI 的 Ubuntu runner 既沒有中文字型也不保證有繪圖工具，讓它生圖只會再壞一次。
OG_IMAGE_NAME = "og.png"

# 建置識別碼檔名。頁面把自己的 build id 內嵌在 JS 裡，前端拿這支檔案比對，
# 不同就提示有新資料（見 FILTER_JS 的 watchForUpdates）。
BUILD_ID_NAME = "build.txt"


def build_id_of(rows):
    """由資料內容算出建置識別碼。

    刻意取自資料而非時間戳：CI 可能因為不相干的改動而重跑，若用時間戳，
    每次重建都會讓所有開著的分頁跳出「有新資料」，但實際上一則都沒變。

    **不能用 r["id"]**：`data/news.json` 刻意不含 id，CI 的
    `import-json --replace` 會重新編號。目前的 id 剛好照插入順序連號而顯得
    穩定，但只要刪掉中間任何一則，其後每一筆的 id 都會位移，於是明明內容
    沒變的頁面也會被判定成新版。改用真正會 round-trip 的欄位。
    """
    payload = ";".join(
        f"{r['news_date']}|{r['title']}|{r['total_score']}|{r['grade']}"
        for r in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
OG_IMAGE_SRC = Path(__file__).parent / "assets" / OG_IMAGE_NAME

# 一則新聞的「相關新聞」最多列幾則。列太多會把評分細節擠掉，
# 且同標籤新聞本來就能用標籤篩選看全部。
RELATED_LIMIT = 5

# data-tags 屬性裡夾住每個標籤的分隔字元，讓前端能做整值比對
# （'|AI|' 不會命中 '|AI晶片|'）。用可列印字元而非 \x1f 之類的控制字元：
# 控制字元寫進 HTML 屬性是不合法的，瀏覽器對它的處理沒有保證。
# 標籤含 '|' 時會破壞編碼，故 render 前一律先剝掉（見 tag_attr_value）。
TAG_SEP = "|"

# 完整／精簡切換。放在 sticky 的 .filters 列裡（與等級 tab 同列）而非
# .toolbar：捲到列表中段想切換檢視密度時，不該逼使用者滑回頂端——
# 這與等級 tab 當初 sticky 的理由相同。日期列的 top 由 --tabbar-h 算出，
# 會自動跟著這一列的高度走，不需要另外調整。
#
# **只用於靜態站**：density 是純前端狀態（body.compact 由 FILTER_JS 切換），
# 而動態 serve 完全沒有載入 JS。動態站沿用這串 markup 會得到一組按了
# 沒反應的死按鈕——標籤與分類都曾經如此（見 render_card 的 static 參數）。
DENSITY_TOGGLE = (
    '<div class="density">'
    '<button type="button" id="density-toggle" aria-pressed="false"'
    ' title="切換精簡／完整檢視" aria-label="切換精簡／完整檢視">☰</button>'
    "</div>"
)


def tag_attr_value(tags):
    """把標籤列表編成 data-tags 屬性值：|標籤1|標籤2|。

    標籤本身若含分隔字元會讓整值比對錯亂，先剝掉再編。
    """
    safe = [t.replace(TAG_SEP, "") for t in tags]
    return TAG_SEP + TAG_SEP.join(safe) + TAG_SEP if safe else ""


def build_tag_index(rows):
    """{標籤: [row, ...]}，供 related_of() 查表。

    先建索引再逐張卡片查，而不是每張卡片重掃一次全部資料——
    後者在數百筆的規模是 O(n²)，靜態站輸出會明顯變慢。
    """
    index = {}
    for r in rows:
        for t in tags_of(r):
            index.setdefault(t, []).append(r)
    return index


def related_of(row, index, limit=RELATED_LIMIT):
    """同標籤的其他新聞（依 rows 原本的排序，即日期新到舊）。

    這是「關聯新聞」的實際載體：標籤只是把關聯建立起來的鍵，
    真正有用的是在一則新聞旁邊直接看到同一主題的來龍去脈。
    """
    out, seen = [], {row["id"]}
    for t in tags_of(row):
        for r in index.get(t, []):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(r)
            if len(out) >= limit:
                return out
    return out


def render_card(r, related=None, static=True, hits=None, verified=None):
    """一張卡片。

    static=True 時分類與標籤是 <button>，由 FILTER_JS 在前端切換篩選；
    static=False（動態 serve）時改成帶 query string 的 <a>，因為動態頁面
    沒有載入 JS。兩者都要能點——曾經動態站直接沿用 button，結果是一顆
    按了沒反應的死按鈕。

    hits: {idx: 判定row}，只含 verdict == "hit" 的 watch_next 條目，
    用來在該條前標 ✓ 並附佐證連結。**刻意只標命中、不標 miss**：
    已判定的則僅佔全站約 5%，若把 miss 也標出來，未判定與「已驗證未發生」
    在視覺上難以區分，訪客會把前者誤讀成後者。整體命中率一律看 CLI 的
    `news.py watch-stats`（那裡 miss 與 moot 都在），網頁不做選擇性統計。

    verified: (hit數, 已判定數)，用來在清單下方標「已回頭驗證 N 條、
    上方標示成真的 M 條」。有這行才不會讓只標 ✓ 讀起來像「全部命中」。
    """
    title = escape(r["title"])
    if r["url"]:
        title_html = f'<a href="{escape(r["url"])}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">{title}</a>'
    else:
        title_html = title

    grade = escape(r["grade"])
    section = r["section"] or ""
    tags = tags_of(r)
    # 搜尋比對用的純文字（標題 + 一句話判斷 + 摘要 + 分類 + 標籤），小寫化交給前端
    haystack = " ".join(filter(None, [r["title"], r["one_line"], r["summary"], section, *tags]))
    # 標籤以 TAG_SEP 夾住每一個值，讓前端能用 indexOf('|標籤|') 做整值比對
    # （直接串接的話，篩「AI」會命中「AI晶片」）。
    tag_attr = tag_attr_value(tags)

    parts = [
        # data-* 供靜態站的前端篩選使用（動態 server 端不需要，但無害）
        f'<div class="card {grade}" data-grade="{grade}"'
        f' data-date="{escape(r["news_date"] or "")}"'
        f' data-section="{escape(section)}"'
        f' data-tags="{escape(tag_attr)}"'
        f' data-text="{escape(haystack.lower())}">',
        '<div class="card-top">',
        f'<span class="badge {grade}">{grade}</span>',
        f'<span class="grade-label">{GRADE_LABELS.get(r["grade"], "")}</span>',
        f'<span class="score">{r["total_score"]}</span>',
        # 精簡模式下逐張展開。只在靜態站輸出：動態站沒有 JS，
        # 也沒有精簡模式（density 是純前端狀態），放了會是死按鈕。
        # 平時（完整模式）以 CSS 隱藏，不佔版面也不干擾閱讀。
        ('<button type="button" class="expand" aria-expanded="false"'
         ' aria-label="展開評分細節">⌄</button>' if static else ""),
        "</div>",
        f'<div class="title">{title_html}</div>',
    ]
    # one_line 是判斷、summary 是複述，故一句話判斷排在摘要之前
    if r["one_line"]:
        parts.append(f'<div class="one-line">{escape(r["one_line"])}</div>')
    if r["summary"]:
        parts.append(f'<div class="summary">{escape(r["summary"])}</div>')

    meta = []
    if r["url"]:
        meta.append(f'<a href="{escape(r["url"])}" target="_blank" rel="noopener">原始新聞 ↗</a>')
    if section:
        if static:
            meta.append(
                f'<button type="button" class="section" data-section-pick="{escape(section)}">'
                f"📂 {escape(section)}</button>"
            )
        else:
            meta.append(
                f'<a class="section" href="/?section={quote(section)}">'
                f"📂 {escape(section)}</a>"
            )
    if meta:
        parts.append(f'<div class="meta">{"".join(f"<span>{m}</span>" for m in meta)}</div>')

    # 標籤：點一下即篩出同標籤的新聞
    if tags:
        if static:
            chips = "".join(
                f'<button type="button" class="tag" data-tag-pick="{escape(t)}">'
                f"#{escape(t)}</button>"
                for t in tags
            )
        else:
            chips = "".join(
                f'<a class="tag" href="/?tag={quote(t)}">#{escape(t)}</a>' for t in tags
            )
        parts.append(f'<div class="tags">{chips}</div>')

    # 詳細評分
    detail = ['<details><summary>評分細節</summary><div class="detail">']
    detail.append('<table class="dims"><tr><th>面向</th><th>分數</th><th>理由</th></tr>')
    for key, label, max_score in DIMENSIONS:
        score = r[f"{key}_score"]
        reason = r[f"{key}_reason"] or ""
        score_txt = f"{score} / {max_score}" if score is not None else "—"
        detail.append(
            f"<tr><td>{label}</td><td class='num'>{score_txt}</td><td>{escape(reason)}</td></tr>"
        )
    detail.append("</table>")

    if r["why_important"]:
        detail.append(f"<h4>為什麼重要</h4><p>{escape(r['why_important'])}</p>")
    if r["affected"]:
        detail.append(f"<h4>可能影響誰</h4><p>{escape(r['affected'])}</p>")
    if r["watch_next"]:
        detail.append("<h4>接下來觀察</h4>")
        try:
            items = json.loads(r["watch_next"])
            if isinstance(items, list):
                lis = []
                for i, text in enumerate(items):
                    hit = hits.get(i) if hits else None
                    if hit:
                        # 只標命中：miss 與未判定在視覺上一律維持原樣，避免
                        # 把「還沒回頭看」誤讀成「沒發生」——已判定的僅佔全站約 5%。
                        note = hit.get("note") or ""
                        ev = hit.get("evidence_url") or ""
                        mark = '<span class="wn-hit" title="'
                        mark += escape(note) + '">✓</span>'
                        body_txt = escape(str(text))
                        if ev:
                            body_txt += (
                                f' <a class="wn-ev" href="{escape(ev)}" '
                                f'target="_blank" rel="noopener">佐證 ↗</a>'
                            )
                        lis.append(f'<li class="wn-done">{mark}{body_txt}</li>')
                    else:
                        lis.append(f"<li>{escape(str(text))}</li>")
                detail.append("<ul>" + "".join(lis) + "</ul>")
                if verified:
                    h, t = verified
                    detail.append(
                        f'<p class="wn-summary">已回頭驗證 {t} 條觀察指標，'
                        f'上方標示成真的 {h} 條。</p>'
                    )
            else:
                detail.append(f"<p>{escape(str(items))}</p>")
        except (json.JSONDecodeError, TypeError):
            detail.append(f"<p>{escape(r['watch_next'])}</p>")

    # 同標籤的其他新聞：讓一則新聞旁邊就能看到同主題的來龍去脈
    if related:
        detail.append('<div class="related"><h4>相關新聞（同標籤）</h4><ul>')
        for rel in related:
            rel_title = escape(rel["title"])
            link = (
                f'<a href="{escape(rel["url"])}" target="_blank" rel="noopener">{rel_title}</a>'
                if rel["url"] else rel_title
            )
            detail.append(
                f'<li><span class="rel-grade">{escape(rel["grade"])} {rel["total_score"]}</span>'
                f'{link}<span class="rel-grade"> · {escape(rel["news_date"] or "未標日期")}</span></li>'
            )
        detail.append("</ul></div>")

    detail.append("</div></details>")
    parts.extend(detail)
    parts.append("</div>")
    return "".join(parts)


def render_page(grade=None, date=None, section=None, q=None, tag=None):
    # 全部資料查一次就好，其餘都從這份導出：每加一種計數就多打一次 db 的話，
    # 一次 render 會變成四次全表掃描（分頁計數、標籤選單、關聯索引各一次）
    all_rows = query_news()
    # 等級以外的條件先套用，分頁計數才能反映「其他條件下各級有幾筆」
    # （與前端 apply() 的 perGrade 同一套邏輯）
    base_rows = query_news(date=date, section=section, q=q, tag=tag)
    rows = [r for r in base_rows if not grade or r["grade"] == grade]
    counts = grade_counts(base_rows)
    total = len(base_rows)
    # 相關新聞取自全部資料而非篩選結果：篩了標籤還只在結果內找關聯，
    # 會看不到被目前條件排除、但確實同主題的新聞
    index = build_tag_index(all_rows)

    keep = ""
    if date:
        keep += f"&date={quote(date)}"
    if section:
        keep += f"&section={quote(section)}"
    if q:
        keep += f"&q={quote(q)}"
    if tag:
        keep += f"&tag={quote(tag)}"
    tabs = [f'<a href="/?{keep.lstrip("&")}" class="{"active" if not grade else ""}">全部 {total}</a>']
    for g in GRADES:
        n = counts.get(g, 0)
        active = "active" if grade == g else ""
        tabs.append(f'<a href="/?grade={g}{keep}" class="{active}">{g} 級 {n}</a>')

    # 篩選表單：任一欄位變動就送出，並以 hidden 欄位保留 grade
    options = ['<option value="">全部日期</option>']
    for d, n in date_counts():
        selected = " selected" if d == date else ""
        options.append(f'<option value="{escape(d)}"{selected}>{escape(d)}（{n}）</option>')
    sec_options = ['<option value="">全部分類</option>']
    for s, n in section_counts():
        selected = " selected" if s == section else ""
        sec_options.append(f'<option value="{escape(s)}"{selected}>{escape(s)}（{n}）</option>')
    tag_options = ['<option value="">全部標籤</option>']
    for t, n in tag_counts(all_rows):
        selected = " selected" if t == tag else ""
        tag_options.append(f'<option value="{escape(t)}"{selected}>{escape(t)}（{n}）</option>')
    controls = [
        '<form class="toolbar" method="get" action="/">',
        f'<input type="hidden" name="grade" value="{escape(grade)}">' if grade else "",
        f'<div class="date-filter"><select name="date" onchange="this.form.submit()">{"".join(options)}</select></div>',
        f'<div class="section-filter"><select name="section" onchange="this.form.submit()">{"".join(sec_options)}</select></div>',
        f'<div class="tag-filter"><select name="tag" onchange="this.form.submit()">{"".join(tag_options)}</select></div>',
        '<div class="search">'
        f'<input name="q" type="search" value="{escape(q or "")}"'
        ' placeholder="搜尋標題／判斷／摘要…" autocomplete="off"></div>',
        '<button type="submit" style="display:none">篩選</button>',
        "</form>",
    ]

    body = []
    all_hits = load_hits_from_json()
    all_verified = verified_counts_from_json()
    if not rows:
        body.append('<div class="empty">目前沒有新聞。用 <code>/news-importance-score</code> 評分後，執行 <code>python3 news.py add</code> 寫入。</div>')
    else:
        current_date = object()
        for r in rows:
            if r["news_date"] != current_date:
                current_date = r["news_date"]
                body.append(f'<div class="date-head">📅 {escape(current_date or "未標日期")}</div>')
            body.append(render_card(
                r, related_of(r, index), static=False,
                hits=all_hits.get(r["url"]), verified=all_verified.get(r["url"]),
            ))

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日新聞重要性評分</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<h1>📰 每日新聞重要性評分</h1>
<div class="sub">依 /news-importance-score 五面向評分（100 分制）整理的新聞資料庫</div>
{render_nav("news")}
<div class="filters"><div class="tabs">{"".join(tabs)}</div></div>
{"".join(controls)}
{"".join(body)}
</div>
</body>
</html>"""


# 靜態站的前端篩選：伺服器端的 grade/date 篩選靠 query string，
# 靜態主機沒有 server 可以處理，故改為一次輸出全部卡片、用 JS 切換顯示。
FILTER_JS = """
(function () {
  var SEP = '__TAG_SEP__';  // 由 render_static_page 換成 TAG_SEP，兩邊不會各寫一份
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var heads = Array.prototype.slice.call(document.querySelectorAll('.date-head'));
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs a'));
  var dateSel = document.getElementById('date-select');
  var sectionSel = document.getElementById('section-select');
  var tagSel = document.getElementById('tag-select');
  var searchBox = document.getElementById('search-box');
  var empty = document.getElementById('empty');
  var densityBtn = document.getElementById('density-toggle');
  var state = { grade: '', date: '', section: '', tag: '', q: '', density: '' };

  // 篩選狀態存進 hash，讓靜態站的篩選結果可分享、可用上一頁還原
  function readHash() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h) return;
    h.split('&').forEach(function (kv) {
      var i = kv.indexOf('=');
      if (i < 0) return;
      var k = kv.slice(0, i), v = decodeURIComponent(kv.slice(i + 1));
      if (k in state) state[k] = v;
    });
  }

  function writeHash() {
    var parts = [];
    Object.keys(state).forEach(function (k) {
      if (state[k]) parts.push(k + '=' + encodeURIComponent(state[k]));
    });
    var h = parts.join('&');
    // replaceState 避免每次打字都塞一筆歷史紀錄
    history.replaceState(null, '', h ? '#' + h : location.pathname);
  }

  function syncControls() {
    if (dateSel) dateSel.value = state.date;
    if (sectionSel) sectionSel.value = state.section;
    if (tagSel) tagSel.value = state.tag;
    if (searchBox) searchBox.value = state.q;
    document.body.classList.toggle('compact', state.density === 'compact');
    if (densityBtn) {
      var isCompact = state.density === 'compact';
      densityBtn.classList.toggle('active', isCompact);
      densityBtn.setAttribute('aria-pressed', isCompact ? 'true' : 'false');
    }
    // .expanded 刻意不在切換密度時清掉：它只在精簡模式看得見，
    // 保留著代表「切到完整再切回精簡」時，原本展開的那幾張還是開的。
    // 卡片上的標籤反映目前選中的標籤，讓「這是我正在看的主題」有視覺回饋
    Array.prototype.forEach.call(document.querySelectorAll('[data-tag-pick]'), function (b) {
      b.classList.toggle('active', !!state.tag && b.dataset.tagPick === state.tag);
    });
  }

  function apply() {
    var q = state.q.trim().toLowerCase();
    var shown = 0;
    var perGrade = {};
    cards.forEach(function (c) {
      var g = c.dataset.grade;
      // 等級以外的條件先算，才能讓分頁計數反映「其他條件下各級有幾筆」
      // 標籤用 SEP 包住每個值做整值比對，避免篩「AI」時命中「AI晶片」
      // （對應 server 端 filter_by_tag() 的精確比對）
      var base = (!state.date || c.dataset.date === state.date)
        && (!state.section || c.dataset.section === state.section)
        && (!state.tag
            || (c.dataset.tags || '').indexOf(SEP + state.tag + SEP) !== -1)
        && (!q || (c.dataset.text || '').indexOf(q) !== -1);
      if (base) perGrade[g] = (perGrade[g] || 0) + 1;
      var ok = base && (!state.grade || g === state.grade);
      c.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    // 日期標題只在其底下還有可見卡片時顯示
    heads.forEach(function (h) {
      var any = cards.some(function (c) {
        return c.dataset.date === h.dataset.date && c.style.display !== 'none';
      });
      h.style.display = any ? '' : 'none';
    });
    // 分頁計數隨其他篩選連動（與 server 端的 grade_counts(base_rows) 行為一致）
    var total = 0;
    Object.keys(perGrade).forEach(function (k) { total += perGrade[k]; });
    tabs.forEach(function (t) {
      var g = t.dataset.grade;
      var n = g ? (perGrade[g] || 0) : total;
      t.textContent = (g ? g + ' 級 ' : '全部 ') + n;
      t.classList.toggle('active', g === state.grade);
    });
    empty.style.display = shown ? 'none' : '';
  }

  function update() { syncControls(); apply(); writeHash(); }

  tabs.forEach(function (t) {
    t.addEventListener('click', function (e) {
      e.preventDefault();
      state.grade = t.dataset.grade || '';
      update();
    });
  });
  if (dateSel) dateSel.addEventListener('change', function () {
    state.date = dateSel.value; update();
  });
  if (sectionSel) sectionSel.addEventListener('change', function () {
    state.section = sectionSel.value; update();
  });
  if (tagSel) tagSel.addEventListener('change', function () {
    state.tag = tagSel.value; update();
  });
  if (searchBox) searchBox.addEventListener('input', function () {
    state.q = searchBox.value; update();
  });
  if (densityBtn) {
    densityBtn.addEventListener('click', function () {
      state.density = state.density === 'compact' ? '' : 'compact';
      update();
    });
  }
  // 精簡模式下逐張展開。用事件委派而非逐張綁定：卡片有 500+ 張，
  // 且篩選時不會重建 DOM（只切 display），綁定一次就夠。
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var btn = e.target.closest('.expand');
    if (!btn) return;
    var card = btn.closest('.card');
    if (!card) return;
    var open = card.classList.toggle('expanded');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.setAttribute('aria-label', open ? '收合評分細節' : '展開評分細節');
  });
  // 卡片上的分類與標籤都可直接點成篩選條件（再點一次取消）
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var secBtn = e.target.closest('[data-section-pick]');
    var tagBtn = e.target.closest('[data-tag-pick]');
    if (!secBtn && !tagBtn) return;
    if (secBtn) {
      var v = secBtn.dataset.sectionPick;
      state.section = (state.section === v) ? '' : v;
    } else {
      var t = tagBtn.dataset.tagPick;
      state.tag = (state.tag === t) ? '' : t;
    }
    update();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  window.addEventListener('hashchange', function () {
    readHash(); syncControls(); apply();
  });

  // 手機預設精簡模式：小螢幕上 300+ 則的列表，先掃標題才是合理的閱讀起點。
  //
  // 條件是「網址沒有明講 density」而非「網址沒有 hash」：分享出去的連結多半帶的是
  // grade／tag 等篩選條件，若以整個 hash 為準，手機開任何一條分享連結都會退回完整模式。
  // hash 裡出現 density= 才代表那是使用者自己的選擇，這時才不覆寫。
  var hadDensity = /(^|&)density=/.test((location.hash || '').replace(/^#/, ''));
  readHash();
  var mobileDefault = !hadDensity
    && window.matchMedia('(max-width: 600px)').matches;
  if (mobileDefault) state.density = 'compact';
  // 這個預設刻意不寫進 hash：它是「這台裝置的呈現偏好」而非使用者選的篩選條件，
  // 寫進去會讓手機上複製的網址帶著 density=compact，在桌機打開也變精簡模式。
  // 使用者一旦自己點過密度切換，就會走 update() 正常寫入 hash。
  if (mobileDefault) { syncControls(); apply(); }
  else { update(); }

  // 更新提示：GitHub Pages 給 HTML 的是 max-age=600，在這段期間瀏覽器完全
  // 不會問伺服器——連重開分頁都直接讀本機快取（實測 0 個網路請求），
  // 所以新資料上線後，使用者可能持續看到舊頁面而毫無跡象。
  //
  // 基準值取自頁面內嵌的 BUILD_ID 而非「第一次 HEAD 問到的 ETag」：後者在
  // 快取命中時會問到伺服器的**新**值，於是舊頁面把新版當成自己的基準，
  // 從此永遠比對不出差異——正是這個提示最該作用的場景。
  //
  // 用 HEAD 比對而非直接重新整理：頁面有 1.8 MB，且使用者可能正在讀某一則
  // 或已設好篩選條件，不該擅自把它捲掉——由使用者決定何時重載。
  // tab 列真的溢出時才加淡出提示（見 .tabs.scrollable 的說明）。
  // 用 ResizeObserver 而非只算一次：轉向、視窗縮放都會改變是否溢出。
  var tabsEl = document.querySelector('.tabs');
  if (tabsEl) {
    var syncScrollHint = function () {
      tabsEl.classList.toggle(
        'scrollable', tabsEl.scrollWidth > tabsEl.clientWidth + 1);
    };
    syncScrollHint();
    if (window.ResizeObserver) new ResizeObserver(syncScrollHint).observe(tabsEl);
    window.addEventListener('resize', syncScrollHint);
  }

  (function watchForUpdates() {
    // 這份 HTML 產出時的識別碼，隨每次 export 改變
    var seen = '__BUILD_ID__';
    var checking = false; // 避免 visibilitychange 連續觸發時重複發出請求
    var banner = null;

    function show() {
      if (banner) return;  // 已經提示過就不再重複插入
      banner = document.createElement('button');
      banner.className = 'update-bar';
      banner.type = 'button';
      banner.textContent = '有新的評分資料　點此更新';
      banner.addEventListener('click', function () { location.reload(); });
      document.body.appendChild(banner);
    }

    // 抓 build.txt 而非比對主頁的 ETag：那份檔案只有幾十個位元組，
    // 且 GitHub Pages 對壓縮與否會回不同形式的 ETag（W/"abc" 與 "abc"），
    // 拿來比字串本身就不可靠。
    function check() {
      if (checking || document.hidden) return;
      checking = true;
      // cache: 'no-store' 讓這個請求本身不被快取，否則讀到的是快取裡的舊值
      fetch('build.txt', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (id) {
          if (!id) return;               // 拿不到就放棄，不做無謂的猜測
          if (id.trim() !== seen) show();
        })
        .catch(function () { /* 離線或請求失敗：靜默略過，下次可見時再試 */ })
        .then(function () { checking = false; });
    }

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) check();
    });
    check();  // 開頁時先記下目前的 ETag 當基準
  })();
})();
"""


SITE_TITLE = "每日新聞重要性評分"


def meta_description(rows, counts):
    """分享預覽與搜尋結果顯示的描述。

    刻意用實際數字而非固定文案：貼到 Slack／Threads 時，
    「323 則、S 級 3 則必讀」比「一個新聞評分網站」更能讓人判斷要不要點。

    長度控制在 80 個字元以內——中文佔的視覺寬度是拉丁字母的兩倍，
    各平台約在 70-90 字元截斷，寫太長等於把後半段浪費掉。
    """
    dates = [r["news_date"] for r in rows if r["news_date"]]
    latest = max(dates) if dates else None
    top = "／".join(
        f"{g} 級 {counts[g]}" for g in ("S", "A") if counts.get(g)
    )
    desc = (
        f"用五個面向為新聞評分（100 分制），篩掉噪音、留下值得追蹤的事。"
        f"已收錄 {len(rows)} 則"
    )
    if top:
        desc += f"，其中 {top} 則值得優先讀"
    if latest:
        desc += f"，更新至 {latest}"
    return desc + "。"


def og_image_lines(rows, counts):
    """預覽圖上的文字內容。抽出來讓 build 與測試共用同一份。

    回傳 [(文字, 字級, 顏色, y 座標), ...]。
    """
    dates = [r["news_date"] for r in rows if r["news_date"]]
    latest = max(dates) if dates else "—"
    # 標籤取前 5 個並限制總長：文字不會自動換行，超出畫布就被裁掉
    tags, width = [], 0
    for t, _ in tag_counts(rows):
        if len(tags) >= 5 or width + len(t) + 2 > 34:
            break
        tags.append(t)
        width += len(t) + 2
    grade_line = "   ".join(f"{g} {counts[g]}" for g in GRADES if counts.get(g))
    return [
        (SITE_TITLE, 60, "#e6e8ea", 150),
        ("五面向評分 · 100 分制 · 篩掉噪音", 30, "#9aa3ad", 225),
        (f"{len(rows)} 則已評分", 52, "#e6e8ea", 350),
        (grade_line, 30, "#fdba74", 420),
        ("   ".join(f"#{t}" for t in tags), 26, "#7ab0ff", 505),
        (f"最新至 {latest}", 24, "#6b7280", 565),
    ]


# 產圖用的中文字型（macOS 內建）。找不到就中止並說明，不要默默畫出豆腐字。
OG_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]


def build_og_image(out_path):
    """把文字燒進 PNG（需要 ImageMagick）。由 `news.py og` 呼叫。

    產出的圖進版控，所以只有本機需要 ImageMagick 與中文字型，
    CI 與其他機器直接用 repo 裡的成品。
    """
    import shutil
    import subprocess

    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        raise SystemExit(
            "找不到 ImageMagick（brew install imagemagick）。"
            "預覽圖已進版控，沒有要改內容的話不需要重產。"
        )
    font = next((f for f in OG_FONT_CANDIDATES if Path(f).exists()), None)
    if not font:
        raise SystemExit(
            "找不到中文字型，產出的圖會是豆腐字。"
            f"預期路徑：{'、'.join(OG_FONT_CANDIDATES)}"
        )

    rows = query_news()
    cmd = [
        magick, "-size", "1200x630", "xc:#111418",
        "-fill", "#dc2626", "-draw", "rectangle 0,0 1200,10",
        "-font", font,
    ]
    for text, size, colour, y in og_image_lines(rows, grade_counts(rows)):
        if not text:
            continue
        cmd += ["-fill", colour, "-pointsize", str(size),
                "-annotate", f"+80+{y}", text]
    cmd.append(str(out_path))
    subprocess.run(cmd, check=True)
    return out_path


def render_static_page():
    """輸出含全部卡片的單一頁面，篩選交給前端 JS。"""
    rows = query_news()
    counts = grade_counts(rows)
    total = len(rows)

    tabs = ['<a href="#" data-grade="" class="active">全部 %d</a>' % total]
    for g in GRADES:
        tabs.append(f'<a href="#" data-grade="{g}">{g} 級 {counts.get(g, 0)}</a>')

    options = ['<option value="">全部日期</option>']
    for d, n in date_counts():
        options.append(f'<option value="{escape(d)}">{escape(d)}（{n}）</option>')
    sec_options = ['<option value="">全部分類</option>']
    for s, n in section_counts():
        sec_options.append(f'<option value="{escape(s)}">{escape(s)}（{n}）</option>')
    tag_options = ['<option value="">全部標籤</option>']
    for t, n in tag_counts(rows):
        tag_options.append(f'<option value="{escape(t)}">{escape(t)}（{n}）</option>')
    controls = (
        '<div class="toolbar">'
        f'<div class="date-filter"><select id="date-select">{"".join(options)}</select></div>'
        f'<div class="section-filter"><select id="section-select">{"".join(sec_options)}</select></div>'
        f'<div class="tag-filter"><select id="tag-select">{"".join(tag_options)}</select></div>'
        '<div class="search">'
        '<input id="search-box" type="search" placeholder="搜尋標題／判斷／摘要…" autocomplete="off">'
        "</div>"
        "</div>"
    )

    body = []
    current_date = object()
    index = build_tag_index(rows)
    # 判定資料讀版控裡的 JSON：CI 建站時沒有 news.db
    all_hits = load_hits_from_json()
    all_verified = verified_counts_from_json()
    for r in rows:
        if r["news_date"] != current_date:
            current_date = r["news_date"]
            body.append(
                f'<div class="date-head" data-date="{escape(current_date or "")}">'
                f'📅 {escape(current_date or "未標日期")}</div>'
            )
        body.append(render_card(
            r, related_of(r, index),
            hits=all_hits.get(r["url"]), verified=all_verified.get(r["url"]),
        ))

    empty_msg = "目前沒有符合條件的新聞。" if rows else "目前沒有新聞。"
    generated = now_local().strftime("%Y-%m-%d %H:%M")
    desc = escape(meta_description(rows, counts))
    og_image = SITE_URL.rstrip("/") + "/" + OG_IMAGE_NAME
    build_id = build_id_of(rows)

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(SITE_TITLE)}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{escape(SITE_URL)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{escape(SITE_TITLE)}">
<meta property="og:title" content="{escape(SITE_TITLE)}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{escape(SITE_URL)}">
<meta property="og:image" content="{escape(og_image)}">
<meta property="og:locale" content="zh_TW">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{escape(SITE_TITLE)}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{escape(og_image)}">
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<h1>📰 {escape(SITE_TITLE)}</h1>
<div class="sub">依 /news-importance-score 五面向評分（100 分制）整理的新聞資料庫｜更新於 {generated}</div>
<div class="filters"><div class="tabs">{"".join(tabs)}</div>{DENSITY_TOGGLE}</div>
{controls}
{"".join(body)}
<div class="empty" id="empty" style="display:none">{empty_msg}</div>
<div class="footer">共 {total} 則評分紀錄</div>
</div>
<script>{FILTER_JS.replace("__TAG_SEP__", TAG_SEP).replace("__BUILD_ID__", build_id)}</script>
</body>
</html>"""


# 投資觀察頁刻意只在本機 serve 提供，不進 export_static：
# repo 是 public，投資判斷公開會改變書寫方式（會不自覺寫得容易命中），
# 而這條線的全部價值就在於記錄真實的判斷。資料同理只在 news.db，不匯出 JSON。
NAV = """<div class="nav">
<a href="/"{news_active}>📰 新聞評分</a>
<a href="/positions"{pos_active}>📈 投資觀察</a>
</div>"""


def render_nav(current):
    return NAV.format(
        news_active=' class="active"' if current == "news" else "",
        pos_active=' class="active"' if current == "positions" else "",
    )


def render_prediction(p, today):
    """一條預測。未判定且已過期的到期日標紅——那是「該去判定了」的提示。

    未判定不畫成 miss：兩者在統計上意義完全不同（未判定不進分母），
    視覺上混淆會讓人以為預測已經失敗。與新聞卡片只標 hit 是同一個考量。
    """
    verdict = p["verdict"]
    # 標記與標籤一律取自 news.py：兩邊各存一份會在改了其中一邊時靜默漂移
    # （tag_counts 曾經如此，見 CLAUDE.md 的「函式比常數更容易悄悄漂移」）。
    mark = verdict_mark(verdict)
    cls = {"hit": "v-hit", "miss": "v-miss",
           "moot": "v-moot", "void": "v-void"}.get(verdict, "v-open")
    due = ""
    if p["due_date"]:
        over = " over" if verdict is None and p["due_date"] <= today else ""
        due = f'<span class="due{over}">{escape(p["due_date"])} 前</span>'
    note = f'<div class="pred-note">{escape(p["note"])}</div>' if p["note"] else ""
    # source_hint 是「到期時去哪查」，顯示在預測底下讓判定時不必回頭找
    hint = (f'<div class="pred-hint">查：{escape(p["source_hint"])}</div>'
            if p["source_hint"] else "")
    return (
        f'<li><span class="verdict {cls}">{mark}</span>'
        f'<span class="kind">{escape(kind_label_of(p["kind"]))}</span>'
        f'<span class="pred-body">{escape(p["text"])}{hint}{note}</span>{due}</li>'
    )


def render_position(pos, preds, today):
    name = f'<span class="pos-date">{escape(pos["name"])}</span>' if pos["name"] else ""
    src = (f' · <a href="{escape(pos["source_url"])}" target="_blank" rel="noopener">來源</a>'
           if pos["source_url"] else "")
    rationale = (f'<div class="rationale">{escape(pos["rationale"])}</div>'
                 if pos["rationale"] else "")
    tags = tags_of(pos)
    tag_html = ""
    if tags:
        picks = "".join(
            f'<a class="tag" href="/positions?tag={quote(t)}">{escape(t)}</a>' for t in tags)
        tag_html = f'<div class="tags">{picks}</div>'
    items = "".join(render_prediction(p, today) for p in preds)
    return (
        f'<div class="pos">'
        f'<div class="pos-head"><span class="ticker">{escape(pos["ticker"])}</span>{name}'
        f'<span class="pos-date">{escape(pos["obs_date"])}{src}</span></div>'
        f'<div class="thesis">{escape(pos["thesis"])}</div>'
        f'{rationale}<ul class="preds">{items}</ul>{tag_html}</div>'
    )


def render_positions_page(ticker=None, tag=None, pending=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    items = load_positions(conn, ticker, pending_only=pending)
    acc = position_accuracy(conn)
    open_count = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE verdict IS NULL").fetchone()[0]
    conn.close()

    if tag:
        items = [(p, ps) for p, ps in items if tag in tags_of(p)]

    today = today_local().isoformat()
    c = acc["overall"]["counts"]
    judged = c["hit"] + c["miss"]
    rate = acc["overall"]["rate"]

    # 依類型的命中率是這頁真正要看的東西：三類的可驗證程度差很多，
    # 只看總命中率會得到一個無法行動的數字（見 PREDICTION_KINDS）。
    by_kind = []
    for k, label, _desc in PREDICTION_KINDS:
        kc = acc["by_kind"].get(k)
        if not kc:
            continue
        kj = kc["counts"]["hit"] + kc["counts"]["miss"]
        if not kj:
            continue
        by_kind.append(
            f'<div class="stat"><b>{kc["rate"]:.0%}</b>'
            f'<span>{escape(label)}（{kc["counts"]["hit"]}/{kj}）</span></div>')

    overall_html = (
        f'<div class="stat"><b>{rate:.0%}</b>'
        f'<span>整體（{c["hit"]}/{judged}）</span></div>' if rate is not None else "")
    warn = (f'<div class="caveat">⚠️ 樣本僅 {judged} 條，結論參考價值有限（建議 20 條以上）。</div>'
            if 0 < judged < 20 else "")
    # void 單獨列出而非藏起來：它記錄「我曾經寫了無法驗證的預測」，
    # 那是校準的一部分，藏起來就變成選擇性揭露（同新聞卡片必附分母的理由）。
    void_html = (f'<div class="stat"><b>{acc["void"]}</b><span>已作廢</span></div>'
                 if acc.get("void") else "")
    stats = (
        f'<div class="stats"><b>命中率</b>'
        f'<div class="stats-row">{overall_html}{"".join(by_kind)}'
        f'<div class="stat"><b>{open_count}</b><span>尚未判定</span></div>'
        f'{void_html}</div>'
        f'{warn}'
        f'<div class="caveat">moot（前提消失）不列入分母，void（當初寫成無法'
        f'驗證的形式）完全排除在統計外。兩類要分開看：基本面有客觀數字可查、'
        f'結構要人判讀事件是否發生——基本面命中但結構落空，代表數字對了'
        f'但推論的機制沒發生。</div></div>'
    ) if (judged or open_count or acc.get("void")) else ""

    tickers = sorted({p["ticker"] for p, _ in load_positions_all()})
    opts = ['<option value="">全部標的</option>']
    for t in tickers:
        sel = " selected" if t == ticker else ""
        opts.append(f'<option value="{escape(t)}"{sel}>{escape(t)}</option>')
    controls = (
        '<form class="toolbar" method="get" action="/positions">'
        f'<div class="tag-filter"><select name="ticker" onchange="this.form.submit()">'
        f'{"".join(opts)}</select></div>'
        f'<label style="font-size:.88rem;color:var(--muted)">'
        f'<input type="checkbox" name="pending" value="1" onchange="this.form.submit()"'
        f'{" checked" if pending else ""}> 只看未判定完的</label>'
        '</form>'
    )

    if items:
        body = "".join(render_position(p, ps, today) for p, ps in items)
    else:
        body = ('<div class="empty">還沒有投資觀察。用 '
                '<code>python3 news.py add-position</code> 新增'
                '（格式見 <code>news.py position-schema</code>）。</div>')

    filtered = f"（篩選：{escape(ticker or tag)}）" if (ticker or tag) else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>投資觀察</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<h1>📈 投資觀察</h1>
<div class="sub">每次觀點含推論與可驗證預測，到期後逐條判定{escape(filtered)}｜僅本機可見</div>
{render_nav("positions")}
{stats}
{controls}
{body}
</div>
</body>
</html>"""


def load_positions_all():
    """取全部觀點（供標的下拉選單用）。分開一支避免篩選後選單只剩當前標的。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    items = load_positions(conn)
    conn.close()
    return items


def verify_html(html, expected):
    """檢查產出的頁面確實含有 expected 張卡片。

    export 最清楚自己生了什麼，故由這裡把關而非讓 CI 比對 HTML 字串——
    CI 的比對條件會跟 markup 脫鉤（曾因卡片改成 class="card S" 而誤判成 0）。
    回傳實際卡片數，不符則 raise。
    """
    found = len(re.findall(r'<div class="card[^"]*" data-grade=', html))
    if found != expected:
        raise SystemExit(
            f"產出異常：頁面有 {found} 張卡片，但資料庫有 {expected} 筆。"
            "（import 失敗或 render_card 壞掉時會出現）"
        )
    return found


def export_static(out_dir, retention=False, today=None):
    """輸出靜態站。retention=True 時套用保留期分層（CI 用）。

    today 可帶入固定日期讓輸出可重現（測試用），預設為今天。
    """
    global _RETENTION_TODAY
    # 基準日用台北時間：CI 的 runner 是 UTC，台灣上午 8 點前跑會拿到「昨天」，
    # 30 天的保留界線因此整個往前挪一天（見 news.py 的 TZ_TAIPEI 說明）
    _RETENTION_TODAY = (today or today_local()) if retention else None
    try:
        total = len(query_news_all())
        rows = query_news()
        if retention:
            # 全部資料都過期時輸出空站沒有意義，且多半代表 RECENT_DAYS 設錯
            # 或久未評分，中止讓人工確認，而不是靜默上線一個空網站。
            if total and not rows:
                raise SystemExit(
                    f"中止輸出：db 有 {total} 筆，但沒有任何一筆落在保留期內"
                    f"（近 {RECENT_DAYS} 天全部、{RECENT_DAYS}-{ARCHIVE_DAYS} 天限 S/A）。"
                )
            print(f"套用保留期：{len(rows)} / {total} 筆進入靜態站")

        out_dir.mkdir(parents=True, exist_ok=True)
        index = out_dir / "index.html"
        html = render_static_page()
        n = verify_html(html, len(rows))
        index.write_text(html, encoding="utf-8")
        # 前端拿這支檔案跟自己內嵌的 build id 比對，不同就提示有新資料。
        # 必須與 index.html 同層，頁面用相對路徑 'build.txt' 取用。
        (out_dir / BUILD_ID_NAME).write_text(build_id_of(rows), encoding="utf-8")
        # 分享預覽圖與 index 同層，og:image 才對得上 SITE_URL + 檔名。
        # 複製進版控的成品而非當場生成——CI 沒有中文字型，生出來會是豆腐字。
        if OG_IMAGE_SRC.exists():
            shutil.copyfile(OG_IMAGE_SRC, out_dir / OG_IMAGE_NAME)
        else:
            # 少了圖只是沒有大圖預覽，標題與描述仍在，不值得中止整個部署
            print(f"警告：找不到 {OG_IMAGE_SRC}，分享預覽將沒有圖"
                  f"（用 `python3 news.py og` 產生）")
        kb = len(html.encode("utf-8")) / 1024
        print(f"已輸出靜態網站到 {index}（{kb:.0f} KB、{n} 張卡片）")
    finally:
        _RETENTION_TODAY = None  # 不留狀態給後續呼叫（serve 與測試會重複進出）


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/positions":
            qs = parse_qs(parsed.query)
            self.respond(render_positions_page(
                ticker=(qs.get("ticker", [None])[0] or None),
                tag=(qs.get("tag", [None])[0] or None),
                pending=bool(qs.get("pending", [None])[0]),
            ))
            return
        if parsed.path != "/":
            self.send_error(404)
            return
        qs = parse_qs(parsed.query)
        grade = qs.get("grade", [None])[0]
        if grade:
            grade = grade.upper()
            if grade not in GRADES:
                grade = None
        date = qs.get("date", [None])[0]
        if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = None
        section = qs.get("section", [None])[0] or None
        tag = qs.get("tag", [None])[0] or None
        if tag:
            # 手打的網址可能用別名寫法（?tag=輝達），在這層收斂成正規名——
            # 與上面的 grade.upper()、日期格式檢查同屬「清理使用者輸入」。
            # 篩選本身一律用正規名做整值比對，靜態站才能有相同行為。
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            tag = normalize_tag(tag, load_aliases(conn))
            conn.close()
        q = qs.get("q", [None])[0] or None
        self.respond(render_page(grade, date, section, q, tag))

    def respond(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # 安靜模式


def lan_ip():
    """本機在區網的位址，抓不到時回 None。

    用 UDP socket「連」到一個外部位址來問作業系統會從哪張網卡出去——
    UDP 不會真的送出封包，所以離線時也只是拋錯而非卡住。
    比 gethostbyname(gethostname()) 可靠：後者在 macOS 常回 127.0.0.1。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))  # TEST-NET-1，保證不存在的位址
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def run(port=8765, host="127.0.0.1"):
    """啟動網頁介面。

    預設綁 127.0.0.1（只有本機連得到），要用手機看才傳 host="0.0.0.0"。
    **預設值刻意不改**：綁 0.0.0.0 等於把頁面公開給同網段的所有裝置，
    而 /positions 是投資判斷——那正是刻意不上公開站的內容。
    風險應該由使用者決定何時承擔，不是由預設值替他決定。
    """
    server = HTTPServer((host, port), Handler)
    exposed = host not in ("127.0.0.1", "localhost")
    print(f"新聞評分網頁介面：http://127.0.0.1:{port}（Ctrl+C 結束）")
    if exposed:
        ip = lan_ip()
        if ip:
            print(f"  手機／同網段裝置：http://{ip}:{port}")
        print(f"  ⚠️  已對外開放（{host}）——同一個 Wi-Fi 下的任何裝置都看得到，")
        print("     包含 /positions 的投資判斷，且沒有密碼保護。")
        print("     在咖啡廳、公司等共用網路請用完就關掉。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    run()
