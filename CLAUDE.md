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

```json
{
  "title": "新聞標題（必填）",
  "url": "原始新聞連結",
  "summary": "新聞摘要（2-3 句）",
  "news_date": "YYYY-MM-DD（新聞日期，非今天日期）",
  "section": "今日最重要 / 影響未來的趨勢 / 跟生活決策有關 / 被忽略但重要 / 熱但未必重要 / 不建議放入每日摘要",
  "one_line": "一句話判斷",
  "why_important": "為什麼重要",
  "affected": "可能影響誰",
  "watch_next": ["觀察指標 1", "觀察指標 2", "觀察指標 3"],
  "dimensions": {
    "scope":       {"score": 0, "reason": "影響範圍（0-25）理由"},
    "duration":    {"score": 0, "reason": "影響時間（0-20）理由"},
    "decision":    {"score": 0, "reason": "決策相關性（0-20）理由"},
    "structural":  {"score": 0, "reason": "結構性意義（0-20）理由"},
    "credibility": {"score": 0, "reason": "事實可信度（0-15）理由"}
  }
}
```

`total_score` 與 `grade` 不用填，CLI 會自動由 dimensions 加總並判定等級（85+ S / 70+ A / 55+ B / 40+ C / 其餘 D）。相同 `url` 預設會拒絕重複寫入（`--force` 可覆寫）。

`news_date` **會被強制驗證**，不合規會拒絕寫入：

- 必須是補零的 `YYYY-MM-DD`（`2026-7-5` 會被擋，要寫 `2026-07-05`）——
  保留期是拿它做字串字面比較，未補零會被歸到錯誤的層級。
- 不接受不存在的日期（`2026-02-30`）與未來日期。
- 可以留空或省略（代表日期不明），這類資料一律保留、不受保留期影響。

## 常用指令

```bash
python3 news.py init            # 建立資料庫（首次）
python3 news.py add <file|->    # 寫入一筆評分（順手更新 data/news.json）
python3 news.py list [--grade S]  # 快速列表
python3 news.py serve [--port 8765]  # 網頁介面 http://127.0.0.1:8765
python3 news.py fetch           # 抓取 feeds.txt 的 RSS，新連結存入 pending 表
python3 news.py pending [--all] [--json] [--limit N]  # 列出待評分清單
python3 news.py skip <id...>    # 把待評分項目標為略過
python3 news.py digest [--date YYYY-MM-DD]  # 輸出當日每日摘要（markdown）
python3 news.py prune [--days 30]  # 清除 pending 中過期的已處理項目
python3 news.py export-json     # 匯出 news 表到 data/news.json（進版控）
python3 news.py import-json [--replace]  # 從 JSON 重建 news 表（CI 用）
python3 news.py export [--out dist] [--retention]  # 輸出靜態網站（--retention 為 CI 用，見下）
python3 -m unittest test_news    # 跑回歸測試（CI 也會跑）
```

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
- **`news_date` 是字串字面比較**：保留期直接比對字串大小，所以格式必須正規化。
  驗證寫在 `add` 入口，`strptime` 本身擋不掉 `2026-7-5`（它會照樣 parse 成功），
  需另外比對正規化後的字串是否相同。

## 架構

- `news.py` — CLI（init / add / list / serve / fetch / pending / export-json / import-json / export），schema 定義在此
- `test_news.py` — 回歸測試（標準庫 unittest，22 個）。涵蓋 news_date 格式驗證、
  保留期分層、匯出／匯入 round-trip 無損、動態站與靜態站的篩選一致性；
  改這幾處的邏輯後務必跑過（CI 也會在建站前跑）。
- `server.py` — 網頁介面，Python 標準庫實作，無外部依賴
- `fetch_article.py` — 內文抓取 fallback（BBC 等 WebFetch 被擋的站）
- `feeds.txt` — RSS 來源清單
- `data/news.json` — 進版控的資料來源（由 export-json 產生）
- `.github/workflows/deploy.yml` — 部署 GitHub Pages
- `news.db` — SQLite 資料庫（不進版控，可由 import-json 重建）
