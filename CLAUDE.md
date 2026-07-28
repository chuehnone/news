# 每日新聞重要性評分

透過 `/news-importance-score` skill 對新聞評分，結果記錄到 SQLite（`news.db`），再由網頁介面瀏覽。

## 工作流程

當使用者提供新聞（連結、標題或內文）要求評分並記錄時：

1. 執行 `/news-importance-score` skill 完成評分（100 分制、5 面向）。
2. 將評分結果整理成 JSON（格式見下），寫入暫存檔或經 stdin 傳給 CLI：
   ```bash
   python3 news.py add - <<'EOF'
   { ...評分 JSON... }
   EOF
   ```
3. 回報寫入結果（id、等級、分數）。

## JSON 格式

`python3 news.py schema` 輸出完整格式與驗證規則。那是唯一出處（由 `news.py` 的
`DIMENSIONS` / `SECTIONS` 生成），不要在別處另抄一份——三處手抄的版本曾經各自漂移。

會擋人的驗證，值得先知道：

- `news_date` 必須是補零的 `YYYY-MM-DD`（`2026-7-5` 會被擋）。保留期拿它做字串
  字面比較，未補零會被歸到錯誤的層級。不接受不存在的日期與未來日期；可留空。
- 各面向分數超出上限、相同 `url` 重複寫入，都會拒絕（後者 `--force` 可覆寫）。

## 常用指令

```bash
python3 news.py init            # 建立資料庫（首次）
python3 news.py add <file|->    # 寫入一筆評分（順手更新 data/news.json）
python3 news.py list [--grade S]  # 快速列表
python3 news.py serve [--port 8765]  # 網頁介面 http://127.0.0.1:8765
python3 news.py fetch           # 抓取 feeds.txt 的 RSS，新連結存入 pending 表
python3 news.py pending [--all] [--json] [--limit N]  # 列出待評分清單
python3 news.py skip <id...>    # 把待評分項目標為略過
python3 news.py tags [標籤]     # 列出所有標籤與筆數／某標籤底下的新聞
python3 news.py tag <id> <標籤...>   # 修改某則的標籤（--add 附加、--clear 清空）
python3 news.py alias [別名 正規名]  # 管理標籤別名（不帶參數列出全部、--remove 刪除）
python3 news.py digest [--date YYYY-MM-DD]  # 輸出當日每日摘要（markdown）
python3 news.py drift [--split YYYY-MM-DD]  # 偵測評分標準漂移（review 的前提，見下）
python3 news.py review [--window 30] [--since YYYY-MM-DD]  # 評分回顧校準（見下）
python3 news.py watch [--date YYYY-MM-DD] [--json]  # 列出當天新聞可能命中的舊 watch_next
python3 news.py watch-verify <url> <idx> <hit|miss|moot> [--note ...] [--evidence ...]
python3 news.py watch-stats    # watch_next 命中率統計
python3 news.py prune [--days 30]  # 清除 pending 中過期的已處理項目
python3 news.py schema          # 輸出 add 的 JSON 格式與驗證規則
python3 news.py export-json     # 匯出 news 表到 data/news.json（進版控）
python3 news.py import-json [--replace]  # 從 JSON 重建 news 表（CI 用）
python3 news.py export [--out dist] [--retention]  # 輸出靜態網站（--retention 為 CI 用，見下）
python3 news.py og              # 重產分享預覽圖 assets/og.png（需 ImageMagick，產完要 commit）
python3 -m unittest test_news    # 跑回歸測試（CI 也會跑）
```

## 評分標準漂移偵測（`news.py drift`）

`review` 檢驗「判斷準不準」，但它**假設評分標準前後一致**。標準若漂了，
review 的結論就不可信——所以這是 review 的前提而非補充。

**2026-07-28 首次實測就發現漂移已在發生**：決策相關性 8.66 → 11.07
（滿分的 +12%），A 級佔比 7% → 30%、C 級 28% → 12%。不是新聞變重要，是標準鬆了。

- **主題控制是這個工具的關鍵**，少了它只要當期主題組成改變就會誤報，
  很快會被當成狼來了而忽略。做法是只比「同一標籤內」的前後期：實測 27 個
  標籤中有 24 個與整體同方向（關稅 +5.58、中國 +3.10），排除了
  「近期剛好都是大事」的解釋。由 `test_topic_mix_change_is_not_reported_as_drift`
  守著——該測試刻意讓整體平均上升但各標籤內不變，若移除主題控制就會誤報。
- **事實可信度是天然的對照組**：它最有客觀依據（有無具名來源、有無數據），
  實測只動 +0.2% 而主觀面向大動，這個對比本身就是「漂移來自判斷鬆動」的證據。
  若哪天連它都漂了，要先懷疑是不是來源品質真的變了。
- 單一標籤在前後期各需至少 `DRIFT_MIN_TAG_SAMPLE`（5）則才納入複驗，
  否則個別極端值會主導結論。
- 切分點預設取中位日，但會避開最後一天——否則資料只有兩個日期時後期會是空的。
- **校準做法是回頭看前期的錨定範例，不是調整門檻遷就現況**。
  門檻一改，前後資料就永久不可比了。

## 評分回顧校準（`news.py review`）

「影響時間」與「結構性意義」這兩個面向本質是**預測**——它們宣稱這則之後還會有
後續、還值得追蹤。`review` 回頭用實際資料檢驗那個宣稱，是校準評分標準的工具，
不是給訪客看的內容（所以只有 CLI，沒有網頁）。

訊號是「後續關聯度」：一則評分後，它的標籤在往後 N 天內又出現幾次。

- **三個偏誤必須修正，否則指標退化成雜訊**（`followup_stats` 的核心）：
  1. **標籤規模**：「中國」62 則、「儲能」1 則，掛大標籤的天生後續多。
     故用標籤的出現率當分母，算「超額倍數」而非絕對次數。
  2. **每日評分量**：6/22 評 2 則、7/27 評 49 則，晚期的天生有更多後續機會。
     故期望值要乘上窗口內的實際評分量。
  3. **主題正在延燒**：基準率取**窗口當期**而非全期平均。廣西水災六月沒出現、
     七月連日洗版，用全期基準會低估它的當期常態，那幾則就衝到 ×3 以上被報成
     「低估」——但後續多是主題在延燒，不是當初判斷精準。少了這項，報表會反覆
     建議調高災害類的分數。由 `test_burst_topic_is_not_reported_as_underestimated`
     守著（用「全期罕見但當期洗版」的 fixture，移除修正會測到 ×3.22）。
  少了任一項，測到的就只是「標籤有多大 × 評得多晚 × 主題在不在延燒」。由
  `TestReviewCalibration` 守著（蓄意移除修正會讓測試失敗）。
- **鑑別力目前只有 +0.23（duration）／+0.29（structural），偏弱**。
  這是加入當期基準後的誠實數字：先前報的 +0.55／+0.54 有相當部分來自
  延燒主題自己貢獻的後續，不是判讀準確。修正只是部分解——在延燒「開始時」
  評的那則，當期基準仍偏低（廣西水災現在仍有 ×2.3-2.7）。要再進一步得處理
  「事件熱度」與「判讀正確」的分離，目前無解法，別把現有數字當成判讀能力的證明。
- **窗口未走完的則不納入報表**：今天評的後續必為 0，混進去就是假警訊。
  成熟與否由 `followup_stats` 以「資料中最新的評分日」判定，`cmd_review`
  直接取用該旗標而不自己再算一次日期——兩份判斷漂移時會靜默納入未成熟的則。
- **只校準 duration 與 structural**：其餘三個面向（影響範圍、決策相關性、
  事實可信度）評的是新聞當下的性質，與後續數沒有邏輯關聯，硬套會產出
  看似有據的假結論。這條由 `test_only_verifiable_dimensions_are_calibrated` 守著。
- **中位數切組要排除同分者**：分數是整數且高度集中（實際有 51 則同為 13 分），
  用 `>=` 切會把同分者全塞進高分組、稀釋對比。改用 `>` 並在報表標示排除幾則。
  （當時實測鑑別力從 +0.41 升到 +0.53，但那組數字是在當期基準修正之前算的，
  絕對值已不可比；這條規則本身仍成立。）
- **成熟判定的基準是「資料中最新的評分日」而非今天**：後續只在有評分時才累積，
  若停評一個月，用今天判斷會把那個月的則全標成成熟，但它們的後續根本沒機會發生。
- `followup_stats` 用日期索引與前綴和，不要改回逐筆比對日期的寫法——那是 O(n²)，
  實測 4300 筆要 12 秒且資料每天在長。等價性由
  `test_optimized_stats_match_naive_computation` 以天真實作當對照組守著
  （fixture 刻意在窗口首末日各放一筆，否則測不到 off-by-one）。
- 樣本少於 20 則會印出警告。資料要累積過一個完整窗口才有參考價值，
  想早點看到訊號可以用較短的窗口（`--window 7`）。

## watch_next 逐條驗證（`news.py watch` / `watch-verify` / `watch-stats`）

`review` 用「標籤後續數」當代理訊號，永遠分不清「事件在延燒」與「判斷正確」。
逐條驗證直接檢驗當初寫的觀察指標有沒有發生，是唯一能回答「判斷準不準」的機制。

**做法是每日批次時順手回顧**，不另外安排回溯工程：`watch` 列出當天新聞可能
命中的舊指標，判讀後用 `watch-verify` 記錄。過去累積的條目補不回來是刻意的
取捨——久遠的條目只能靠標籤撈到的內容判讀，品質不會比當下讀新聞時好。

- **候選配對只用標籤交集，且只採「窄標籤」**（佔比 < `WATCH_TAG_MAX_SHARE`
  或出現數 ≤ `WATCH_TAG_MIN_ABS`）。共用「台灣政策」這種寬標籤完全不構成
  線索——實測會讓「綜所稅退稅」被列為「海纜備援進度」的線索，候選暴增到
  881 條且幾乎全不可用。由 `test_broad_shared_tag_is_not_a_candidate` 守著。
- **刻意不做語意比對或自動判定**。目的只是把候選縮到人能讀完的範圍，
  判定要由讀的人下。自動判定會產出大量似是而非的 hit，而這張表的全部價值
  就在於判定可信。
- **`moot` 必須與 `miss` 分開**：前提消失（談判取消、政策撤回）是「無從判斷」，
  不是「預測錯」。混為一談會系統性低估命中率，且兩者對校準的意涵不同——
  miss 該檢討判斷，moot 只是世界變了。moot 不列入命中率分母，由
  `test_moot_excluded_from_hit_rate` 守著。
- **未滿 `WATCH_MIN_AGE_DAYS`（7 天）的指標不列出**：太早看什麼都還沒發生，
  會把「時候未到」記成 miss 而污染命中率。
- **判定以 `news_url` 關聯而非 `id`**：CI 的 `import-json --replace` 會重建
  news 表、id 由 AUTOINCREMENT 重新配發，存 id 會在重建後全部對錯人。
  由 `test_verify_survives_import_json_replace` 守著。
- **判定另存 `data/watch_verify.json` 並進版控**：`news.db` 不進版控，只存 db
  裡的話重新 clone 就全沒了，而這是要累積數月才有意義的資料。刻意不併進
  `data/news.json`——後者的「完整鏡像」保證只涵蓋 news 表，混進別的表會讓
  `import-json --replace` 的語意變模糊。
  網頁端讀這個 JSON 而非 db（`load_hits_from_json`），因為 CI 建站時沒有
  `news.db`；它也在 `deploy.yml` 的觸發路徑內，只更新判定同樣會重新部署。
- **卡片上只標命中，不標 miss**（`render_card` 的 `hits` 參數）：已判定的則
  僅約佔全站 5%，若把 miss 也標出來，「未判定」與「已驗證未發生」在視覺上
  難以區分，訪客會把前者誤讀成後者。
  但只標 ✓ 會讓人以為全部命中（實際 37 hit / 68 miss），所以清單下方**必須**
  附「已回頭驗證 N 條、上方標示成真的 M 條」——分母含 miss、排除 moot。
  拿掉那一行就變成選擇性揭露，由 `test_hit_summary_states_the_denominator` 守著。
  整體命中率一律看 CLI 的 `watch-stats`，網頁不做統計數字。
- 真正要看的是**依等級的命中率**：高分級若低於低分級，代表高分那些
  「還會有後續」的宣稱撐不住，是評分過鬆的直接證據，比 `drift` 更難辯駁。

**2026-07-28 首輪實測（109 條已判定）**：

- 等級梯度成立且方向正確：**A 42%（23/55）> B 33%（12/36）> C 14%（2/14）**。
  高分則的「還會有後續」宣稱確實比低分則站得住。這與 `drift` 的警訊並不矛盾：
  **鬆動的是絕對門檻，不是相對排序能力**，兩者要分開處理。
- **指標寫法對命中率的影響比等級更大**：機制延續型 48%（16/33）、
  特定事件型 29%（21/72）、來源不涵蓋型僅 15%（2/13）。寫法指引已寫進
  `/news-importance-score` skill 的「怎麼寫可驗證的觀察指標」。
- **有一整類 miss 不是判斷錯而是來源不涵蓋**：戰爭險保費、LNG 現貨價、
  ONI 聖嬰指標、大宗農產品價格——在 76 則美伊後續中出現 0 次。`feeds.txt`
  是中央社／BBC／科技新報，本來就不報這些。這類指標寫了註定 miss，
  會壓低命中率但壓低的是分母品質而非判讀能力。
- 判定時最容易犯的錯是**把鄰近事件當成命中**。實測修正了四次，例如指標問
  「是否**國會立法**重建關稅權」，實際是行政走 301 條款——關稅權確實重建了
  但不是透過立法，這是 miss。判定前先問：這條證據能不能直接回答指標問的問題？
- **`--evidence` 只能填資料庫裡已評分的報導網址**，CLI 會擋下庫外的網址。
  首輪判定時憑印象拼湊網址（`technews.tw/2026/07/27/<自造 slug>`），37 條佐證
  裡 24 條是死連結、其餘 13 條格式對但編號撞到別篇無關報導（「布蘭特油價站穩
  85 美元」的佐證變成「萊茵河水位降至新低」），**沒有一條可信**，最後全部清空。
  找不到合適的已評分報導時就省略，把依據寫進 `--note`——note 會顯示在網頁的
  hover 提示，本來就是主要的說明管道。由 `TestWatchEvidenceMustExist` 守著。

## 關聯新聞（標籤）

同一件事會分很多天、多則報導（301 關稅、NVIDIA、中東局勢），標籤把它們串起來：
網頁上點卡片的標籤即篩出同主題的所有新聞，卡片的評分細節裡也會直接列出同標籤的其他則。

- 評分時在 JSON 填 `tags`（最多 5 個，超過會拒絕）。取「未來還會有後續報導」的主題
  （公司、政策、事件、地區），不要用「重要」這種形容詞或只出現一次的事件名。
- **別名表存在 db 的 `tag_aliases` 表**，不是寫死在原始碼裡——別名是會持續長出來的資料，
  每發現一組新寫法就改一次程式並不合理。`TAG_ALIAS_SEED` 只是 init 時的種子。
- 寫入時就套用別名收斂（`輝達` → `NVIDIA`），所以 **db 內存的一律是正規名**。
  正規化只發生在寫入路徑，CI 從 JSON 重建靜態站時完全不需要這張表。
- 發現同一主題分裂成兩個標籤時：`news.py alias 晶圓代工 台積電`。
  這會**一併收斂既有資料**並更新 `data/news.json`，不是只影響之後寫入的。
- 別名不得指向另一個別名（會讓正規化結果取決於查表順序），CLI 會擋下並提示正確的正規名。
- 刪掉的種子別名不會在下次連線時復活（種子只在表是空的時候灌）。

## 批次評分

使用者要求「批次評分」、「處理待評分清單」時，用 `/news-importance-score` 的批次模式：`pending --json` 取清單 → 依標題粗篩（不值得的用 `skip` 標掉）→ 逐則抓內文完整評分寫入 → 輸出總表。詳細流程見 skill 內的「批次模式」章節。

兩件必須順手做的事（分開做就不會做）：

1. **評分前先讀前期錨定範例**（見「評分標準漂移偵測」）。2026-07-28 實測：
   未校準的兩批 A 級佔 32-43%、決策相關性平均 11.1；讀完前期範例後的那批
   降到 8.7% 與 9.91，回到前期水準。差別只在有沒有先看。
2. **評分後跑 `news.py watch`**，判讀當天新聞命中了哪些舊指標。剛讀完當日
   新聞的當下是判讀品質最高的時刻，錯過就只能靠標籤事後撈。

## RSS 自動抓取

- `feeds.txt` 每行「來源名稱 網址」，`#` 開頭為註解。
- 所有 url 存入與比對前都會經 `normalize_url()` 剝除追蹤參數（utm_*、at_* 等）。
- `fetch` 會跳過 `news` 表已有的連結；`pending` 表以 url 去重，重跑安全。標題命中 `LOWPRIO_KEYWORDS`（盤勢／天氣／體育／彩券）的項目直接標 `low`，不進預設待評分清單（`pending --all` 可見）。
- `add` 寫入評分後，會把 `pending` 中相同 url 或相同標題（含「 - 媒體名」後綴）的項目標成 `scored`。
- WebFetch 被擋的網站（如 BBC）用 `python3 fetch_article.py <url>...` 抓內文。
- 目前沒有定時排程，`fetch` 由手動（或請 Claude Code）執行。

## 線上部署（GitHub Pages）

靜態站部署，**本機是唯一資料寫入來源**：抓取（`fetch`）與評分（skill）都在本機，
CI 只負責把 JSON 轉成靜態站並上線。

- `data/news.json` 是進版控的資料來源；`news.db` 仍不進版控
  （SQLite 是二進位檔，每次 commit 都是整檔快照，repo 會無上限膨脹）。
- 匯出**刻意不含 `created_at`**：repo 是 public，逐筆評分時間會洩漏作業時段等
  行為 metadata，而網站只用 `news_date`。匯入時該欄位套用 schema 預設值。
- `add` 寫入後會自動更新 `data/news.json`，所以評分完直接 commit push 即可：
  ```bash
  git add data/news.json && git commit -m "更新新聞資料" && git push
  ```
  批次評分也一樣，不需要特別處理（匯出實測約 5ms，相對抓內文的數秒是雜訊）。
  `--no-export` 旗標仍保留給大量匯入等想自行控制匯出時機的場景。
- push 後 `.github/workflows/deploy.yml` 自動 import-json → export → 部署 Pages。
- **保留期分層只在 CI 的 `export --retention` 套用**：近 30 天全部等級、
  30-90 天只留 S/A、90 天以上不上站（常數見 `server.py` 的 `RECENT_DAYS` /
  `ARCHIVE_DAYS`），用意是讓靜態站不隨時間無上限成長。
  db 與 JSON 都保有完整資料，過期項目只是不上站；全部過期時 `export` 會中止而非部署空站。
- 首次啟用：repo Settings → Pages → Source 選 **GitHub Actions**。
- 網頁篩選在靜態站是**前端 JS**（`FILTER_JS`），動態 `serve` 仍走 server 端 query string，
  兩者共用 `render_card`。兩邊行為必須一致，這件事由
  `test_news.py` 的 `TestFilterParity` 把關（改任一邊而忘了另一邊會測試失敗），
  不必靠人工記得。
- **卡片上可點的元素在兩種模式要用不同標籤**（`render_card` 的 `static` 參數）：
  靜態站用 `<button data-*-pick>` 交給 `FILTER_JS`，動態站用 `<a href="/?...">`。
  動態頁面**完全沒有載入 JS**，沿用 button 的話會是一顆按了沒反應的死按鈕
  （標籤與分類都曾經如此）。由 `test_tag_and_section_are_clickable_in_both_modes` 守著。
- **分享預覽（OG／description）**：貼連結到 Slack／Threads／LINE 時的呈現。
  描述與預覽圖都由實際資料生成（筆數、S/A 級數、最新日期），不是固定文案。
  - **預覽圖必須是點陣圖（`assets/og.png`），而且要進版控**。曾經用 SVG，
    結果整張圖的中文都變成顯示 Unicode 碼位的豆腐方塊——Slack／Threads 是在
    **它們自己的伺服器**上算縮圖，那裡沒有 PingFang／Noto Sans TC。
    本機用 `qlmanage` 預覽完全看不出來，因為那是拿自己的字型畫的。
  - 改圖用 `python3 news.py og` 重產（需 ImageMagick 與系統中文字型），
    **產完要 commit**；`export` 只負責把成品複製到輸出目錄。
    CI 的 Ubuntu runner 既沒中文字型也不保證有繪圖工具，讓它生圖只會再壞一次。
    因此 `deploy.yml` 的觸發路徑要包含 `assets/**`，只換圖也才會重新部署。
  - `og:image` **必須是絕對網址**，相對路徑各平台一律抓不到圖，所以有
    `SITE_URL` 常數（換網域用環境變數 `NEWS_SITE_URL` 覆蓋，不必改程式碼）。
  - 圖上的文字**不會自動換行**，超出畫布就被裁掉，改文案後要實際開圖看過。
    標題與標籤行都已按字數限長。
  - 預覽圖不放 emoji：各平台 emoji 字型不一，容易變成豆腐字。
  - 目前**只做分享預覽，沒有做 sitemap／robots**——這個站是自用與分享用途，
    不主動求搜尋引擎索引。若哪天要流量，該做的是把每則新聞與每個標籤
    拆成獨立頁面（現在全部擠在單一 1.6 MB 的 index.html，且標籤篩選是
    hash，搜尋引擎不會當成獨立頁面）。
- ⚠️ GitHub Pages 免費版一律 public，資料會公開。

## 已知陷阱

實際踩過而且代價不小的，改動前先看這節：

- **改卡片 markup 前先 `grep` 全 repo**：曾有 `deploy.yml` 用字串比對
  `class="card"` 數卡片，卡片改成 `class="card S"` 後比對不到，部署整個中止。
  現在改由 `export` 自檢（`server.py` 的 `verify_html`），但同類的隱形依賴
  隨時可能再出現——動輸出格式前先確認誰在消費它。
- **`data/news.json` 必須維持 db 的完整鏡像**：這是 `import-json --replace`
  不會掉資料的前提。保留期一旦被挪到匯出階段套用，JSON 就變成子集，
  回灌會永久刪掉只存在於 db 的資料（news.db 不進版控，沒有其他副本）。
  這個保證由 `TestExportJsonIsFullMirror` 守著。
- **新增欄位要同時補 `migrate()`**：`CREATE TABLE IF NOT EXISTS` 對已存在的表
  完全不動，只改 `SCHEMA` 的話新欄位在既有 db 上不會出現（news.db 不進版控，
  每台機器的 db 是各自長出來的，不會因為重新 clone 而重建）。
  新欄位也要加進 `NEWS_COLUMNS`，否則不會進 `data/news.json`，
  CI 從 JSON 重建時整欄資料會消失。
- **`news_date` 是字串字面比較**：保留期直接比對字串大小，所以格式必須正規化。
  驗證寫在 `add` 入口，`strptime` 本身擋不掉 `2026-7-5`（它會照樣 parse 成功），
  需另外比對正規化後的字串是否相同。
- **schema 常數只能有 news.py 一份**：`server.py` 一律 import，不得各存一份。
  兩份同值時完全不會報錯，只改一邊也不會——網頁只是靜默地按舊上限畫分數條。
  同理 `GRADE_THRESHOLDS`：門檻曾經是 `grade_of()` 一份、`cmd_schema()` 手抄一份，
  改門檻會讓對外說明與實際評分不一致。由 `TestNoDuplicateConstants` 守著：
  除了比對身分（複製一份同值的也會失敗），還會掃描 `server.py` 有沒有定義
  任何與 `news.py` 同名的常數，所以新增第五個共用常數時不必回頭補測試清單。
  **同一條規則也適用於函式**：`tag_counts` 一度兩邊各一份逐字相同的實作，
  只差取資料的來源，穿過了只掃大寫常數的守門測試。函式比常數更容易悄悄漂移
  （改了排序規則卻只改一邊，CLI 與網頁就給出不同順序），
  現在由 `test_server_defines_no_shadowing_function` 一併擋下。
  共用邏輯要吃「已取好的 rows」而非自己查 db，取資料的範圍才能留給呼叫端決定。
- **標籤「篩選」必須是整值，「搜尋」則是模糊的**：兩者刻意不同，別把後者「修正」成前者。
  - 篩選（`?tag=AI`）：`AI` 與 `AI晶片` 是兩個標籤，用 `LIKE '%AI%'`（SQL）或
    `indexOf('AI')`（前端）都會讓前者撈到後者。SQL 端在 Python 精確比對
    （`filter_by_tag`），前端把每個標籤用 `TAG_SEP` 夾住（`|AI|`）後比對整值。
    `TestFilterParity` 的 fixture 刻意放了這組前綴包含的標籤，任一邊退化就會失敗。
    分隔字元刻意用可列印的 `|` 而非控制字元——控制字元寫進 HTML 屬性不合法。
  - 搜尋（`?q=AI`）：本來就跨欄位模糊比對，命中 `AI晶片` 是預期行為。
- **別名收斂只在兩個地方發生：寫入時，以及 `do_GET` 處理網址參數時**。
  篩選層（`filter_by_tag`）是純整值比對，刻意不碰別名——db 存的本來就是正規名，
  而靜態站的前端沒有別名表可查。篩選層若多做一層收斂，動態站就會比靜態站多認得
  一種寫法，而 `assert_parity` 兩邊傳同一個字串、發現不了「其中一邊多做了事」。
- **等級集合用 `GRADES`，別寫 `"SABCD"`**：那是字串，`grade not in "SABCD"` 是
  子字串比對，`?grade=AB` 會通過驗證。網頁 tab 順序、query 參數驗證都取 `GRADES`。
  封存層級用 `ARCHIVE_GRADES`，digest 的詳列等級用 `DIGEST_DETAILED_GRADES`——
  兩者目前同值（S/A）但語意不同，刻意分開命名以免日後改一個時誤動另一個。

## 架構

- `news.py` — CLI（init / add / list / serve / fetch / pending / drift / review /
  watch / watch-verify / watch-stats / tags / tag / alias / export-json /
  import-json / export）。
  schema 常數（`DIMENSIONS` / `SECTIONS` / `GRADE_THRESHOLDS` / `GRADES` / `GRADE_LABELS`）定義在此，是唯一出處
- `test_news.py` — 回歸測試（標準庫 unittest，88 個）。涵蓋 news_date 格式驗證、
  保留期分層、匯出／匯入 round-trip 無損、動態站與靜態站的篩選一致性、
  標籤正規化與整值比對、schema 常數與函式不得重複定義、
  回顧校準的三項偏誤修正、watch_next 驗證的候選收窄與 moot 語意、
  卡片只標命中且必附分母；改這幾處的邏輯後務必跑過（CI 也會在建站前跑）。
- `server.py` — 網頁介面，Python 標準庫實作，無外部依賴。常數一律 import 自 `news.py`
- `fetch_article.py` — 內文抓取 fallback（BBC 等 WebFetch 被擋的站）
- `feeds.txt` — RSS 來源清單
- `data/news.json` — 進版控的資料來源（由 export-json 產生）
- `data/watch_verify.json` — watch_next 判定結果，進版控（非網站資料，但 db 不進版控）
- `assets/og.png` — 分享預覽圖，進版控的成品（由 `news.py og` 產生，export 複製上線）
- `.github/workflows/deploy.yml` — 部署 GitHub Pages
- `news.db` — SQLite 資料庫（不進版控，可由 import-json 重建）
