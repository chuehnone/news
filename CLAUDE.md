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
python3 news.py prune [--days 30]  # 清除 pending 中過期的已處理項目
python3 news.py schema          # 輸出 add 的 JSON 格式與驗證規則
python3 news.py export-json     # 匯出 news 表到 data/news.json（進版控）
python3 news.py import-json [--replace]  # 從 JSON 重建 news 表（CI 用）
python3 news.py export [--out dist] [--retention]  # 輸出靜態網站（--retention 為 CI 用，見下）
python3 -m unittest test_news    # 跑回歸測試（CI 也會跑）
```

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

- `news.py` — CLI（init / add / list / serve / fetch / pending / tags / tag / alias /
  export-json / import-json / export）。
  schema 常數（`DIMENSIONS` / `SECTIONS` / `GRADE_THRESHOLDS` / `GRADES` / `GRADE_LABELS`）定義在此，是唯一出處
- `test_news.py` — 回歸測試（標準庫 unittest，52 個）。涵蓋 news_date 格式驗證、
  保留期分層、匯出／匯入 round-trip 無損、動態站與靜態站的篩選一致性、
  標籤正規化與整值比對、schema 常數與函式不得重複定義；
  改這幾處的邏輯後務必跑過（CI 也會在建站前跑）。
- `server.py` — 網頁介面，Python 標準庫實作，無外部依賴。常數一律 import 自 `news.py`
- `fetch_article.py` — 內文抓取 fallback（BBC 等 WebFetch 被擋的站）
- `feeds.txt` — RSS 來源清單
- `data/news.json` — 進版控的資料來源（由 export-json 產生）
- `.github/workflows/deploy.yml` — 部署 GitHub Pages
- `news.db` — SQLite 資料庫（不進版控，可由 import-json 重建）
