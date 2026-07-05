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

## 常用指令

```bash
python3 news.py init            # 建立資料庫（首次）
python3 news.py add <file|->    # 寫入一筆評分
python3 news.py list [--grade S]  # 快速列表
python3 news.py serve [--port 8765]  # 網頁介面 http://127.0.0.1:8765
python3 news.py fetch           # 抓取 feeds.txt 的 RSS，新連結存入 pending 表
python3 news.py pending [--all] [--json] [--limit N]  # 列出待評分清單
python3 news.py skip <id...>    # 把待評分項目標為略過
```

## 批次評分

使用者要求「批次評分」、「處理待評分清單」時，用 `/news-importance-score` 的批次模式：`pending --json` 取清單 → 依標題粗篩（不值得的用 `skip` 標掉）→ 逐則抓內文完整評分寫入 → 輸出總表。詳細流程見 skill 內的「批次模式」章節。

## RSS 自動抓取

- `feeds.txt` 每行「來源名稱 網址」，`#` 開頭為註解。
- `fetch` 會跳過 `news` 表已有的連結；`pending` 表以 url 去重，重跑安全。
- `add` 寫入評分後，會把 `pending` 中相同 url 的項目標成 `scored`。
- 定時排程用 launchd：
  ```bash
  cp com.chinsheng.news-fetch.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.chinsheng.news-fetch.plist
  ```
  預設每天 08:00 執行，log 寫到 `fetch.log`。

## 架構

- `news.py` — CLI（init / add / list / serve / fetch / pending），schema 定義在此
- `server.py` — 網頁介面，Python 標準庫實作，無外部依賴
- `feeds.txt` — RSS 來源清單
- `com.chinsheng.news-fetch.plist` — launchd 排程範本（每日抓取）
- `news.db` — SQLite 資料庫（不進版控）
