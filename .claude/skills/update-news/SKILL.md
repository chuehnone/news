---
name: update-news
description: 跑完整的每日新聞批次流程——抓 RSS、批次評分、順手判定 watch_next、處理到期的投資預測，最後 commit + push 觸發靜態站部署。當使用者說「批次更新新聞」、「批次更新新聞和投資」、「批次更新新聞和財務」、「批次更新新聞還有投資分析」、「update-news」、「跑每日批次」時，主動使用這個 skill。
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch, Skill
---

# update-news — 每日新聞批次流程

## 為什麼有這個 skill

這套流程 2026-07-29 到 08-03 手打了 11 次，措辭飄過四種變體（「批次更新新聞」
「批次更新新聞和投資」「批次更新新聞和財務」「批次更新新聞還有投資分析」），
而且後面幾乎每次都要再補一句 `commit`、`push`。措辭飄代表流程定義只存在腦中，
每次都要重新猜範圍。這個 skill 把它釘死。

**四步一次跑完，收尾自動 commit + push。**

---

## 執行流程

### 步驟 1：抓 RSS

```bash
cd ~/Documents/Projects/news && python3 news.py fetch
```

**新增少於 10 則（`THIN_BATCH_THRESHOLD`）會提示可稍後再跑。**
這個提示是給人看的參考，不是中止條件——已經跑到這裡就繼續走完，
除非使用者明確說要停。實測 fetch→評分轉換率約 16-27%。

### 步驟 2：批次評分

用 `/news-importance-score` 的批次模式：

```bash
python3 news.py pending --json    # 取清單
```

→ 依標題粗篩，不值得的用 `skip` 標掉
→ 逐則抓內文完整評分寫入
→ 輸出總表

**抓內文一律用 `pending --json` 給的 url，不要憑標題拼湊網址。**
2026-08-01 與 08-02 連續兩次踩到：依標題猜 `technews.tw/2026/07/31/qualcomm-...`
與中央社 `202608020042`，全部 404，每次都要多一輪查詢才拿到真網址。

**抓內文的工具依站別二選一，先看網域再決定，不要先試再說**：

| 網域 | 用 | 用錯的下場 |
|------|-----|-----------|
| `www.bbc.com` | `python3 fetch_article.py <url>` | WebFetch 回 `unable to fetch` |
| `technews.tw`（含 `finance.` / `infosecu.` 子網域）、`www.cna.com.tw` | WebFetch | `fetch_article.py` 回**看似成功但無正文**的導覽內容 |

BBC 那條寫在 `CLAUDE.md`，但實測撞牆 25 次只有 1 次真的轉用 fallback
（累計 65 次失敗、跨 10 個 session）。**在這裡把它當預設路徑，不是例外處理**。

反向同樣要小心（2026-08-17 實測）：`fetch_article.py` 是為 BBC 寫的，
對 technews 回會員選單與導覽列、對中央社回整頁側欄連結清單，**都不報錯**。
憑「有輸出」判斷會誤以為抓到內文，實際拿去評分就是憑標題腦補。

### 步驟 3：watch_next 順手判定

```bash
python3 news.py watch
```

列出當天新聞可能命中的舊 watch_next 指標，判讀後用 `watch-verify` 記錄。

**這一步刻意綁在每日批次裡**，不另外安排回溯工程——久遠的條目只能靠標籤
撈到的內容判讀，品質不會比當下讀新聞時好。沒有候選就跳過，不用勉強找。

### 步驟 4：投資預測到期判定

```bash
python3 news.py position-due
```

列出到期該判定的投資預測，逐條用 `position-verify` 記錄。
沒有到期項目就跳過。

### 步驟 5：收尾（commit + push）

```bash
python3 news.py export-json        # 匯出 news 表到 data/news.json（進版控用）
git add -A
git commit -m "更新新聞資料：YYYY-MM-DD 批次評分 N 則"
git push
```

**commit message 沿用既有慣例**：`更新新聞資料：YYYY-MM-DD 批次評分 N 則`。
同一天跑第二次以後加註「（第二批）」「（第三批）」，比照
`839e4fe`、`fec193d`、`3886d18` 的寫法。

push 後 `.github/workflows/deploy.yml` 自動 import-json → export → 部署 Pages。

---

## 紅線

- **投資觀察資料不得進版控、不得上靜態站**（`CLAUDE.md:334`，`TestPositionsStayLocal`
  守著）。步驟 4 的產出只留在本機 `news.db`，`export-json` 只匯出 news 表。
  commit 前若發現 diff 含 position 相關資料，**停下來問使用者**，不要自行處理。
- **本機是唯一資料寫入來源**（`CLAUDE.md:412`）。抓取與評分都在本機，
  CI 只負責把 JSON 轉靜態站。不要試圖在 CI 端補資料。

---

## 收尾回報

跑完回報四個數字，一行帶過即可：

```
fetch N 則新增｜評分 N 則（skip N）｜watch 判定 N 條｜position 到期 N 筆
commit <sha> 已 push
```

若某一步是 0 或跳過，照實寫 0，不要省略該欄——省略會讓人分不清
「沒有」和「忘了跑」。

---

## 已知 trap

- **BBC fetch 失敗是最高頻的踩雷**（65 次／10 個 session）。見步驟 2。
- **`pending --json` 的 url 直接用**，不要憑標題拼網址。見步驟 2。
- **同日多批要在 commit message 標批次序號**，否則 log 上分不出來。
- **步驟 3、4 沒有候選是正常的**，不要為了「有產出」硬湊判定——
  自動判定會產出大量似是而非的 hit，而這張表的全部價值在判讀品質
  （`CLAUDE.md:195` 的設計取捨）。
