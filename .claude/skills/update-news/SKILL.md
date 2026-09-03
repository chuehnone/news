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

**六步一次跑完，收尾自動 commit + push。**

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
python3 news.py anchors           # ← 先讀，寫任何分數之前
python3 news.py pending --json    # 取清單
```

→ 依標題粗篩，不值得的用 `skip` 標掉
→ 逐則抓內文完整評分寫入
→ 輸出總表

**`anchors` 要在寫第一則分數之前讀，不是評完再對照。**
2026-07-28 實測：未校準的兩批 A 級佔 32-43%、決策相關性平均 11.1；
讀完前期範例後那批降到 8.7% 與 9.91。差別只在有沒有先看。
關鍵數字：**A 級的決策相關性中位數是 12**，`≥15` 前期只出現在颱風登陸台灣。

**抓內文一律用 `pending --json` 給的 url，不要憑標題拼湊網址。**
2026-08-01 與 08-02 連續兩次踩到：依標題猜 `technews.tw/2026/07/31/qualcomm-...`
與中央社 `202608020042`，全部 404，每次都要多一輪查詢才拿到真網址。

**這條規則寫了兩處仍在 2026-09-01 與 09-03 各踩一次，failure mode 已經清楚**：
不是漏看規則，而是**批次組多個 WebFetch 呼叫的當下**才憑印象補 url——
清單在前一輪的輸出裡、隔了幾個訊息，於是「看起來記得」就直接填了。
09-01 四則全錯（抓到國樂團團長卸任、TikTok 詐騙、404、能源獎項），
09-03 兩則全 404。錯的網址**不一定報錯**：中央社的流水號猜錯會回到另一篇
真實報導，內容完整、看起來就像抓對了，只有比對標題才會發現。

**防範動作：組任何一批 WebFetch 之前，先跑一次只印 id 與 url 的指令**，
從那份輸出複製貼上，不要從記憶裡打：

```bash
python3 news.py pending --json | python3 -c "
import json,sys
for x in json.load(sys.stdin): print(x['id'], x['url'])
"
```

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

### 步驟 3：校準驗算

```bash
python3 news.py calibrate
```

**評完必跑，不論 `add` 有沒有印紅線。**
完整程序（紅線的意義、亮紅線時跑 `drift` 的處理順序、回報寫法）讀
**`docs/calibrate-protocol.md`** ——那是唯一出處，`/news-importance-score`
也指向同一份。**不要在這裡重抄**，兩份手抄版曾各自漂移。

### 步驟 4：watch_next 順手判定

```bash
python3 news.py watch
```

列出當天新聞可能命中的舊 watch_next 指標，判讀後用 `watch-verify` 記錄。

**這一步刻意綁在每日批次裡**，不另外安排回溯工程——久遠的條目只能靠標籤
撈到的內容判讀，品質不會比當下讀新聞時好。沒有候選就跳過，不用勉強找。

### 步驟 5：投資預測到期判定

```bash
python3 news.py position-due
```

列出到期該判定的投資預測，逐條用 `position-verify` 記錄。
沒有到期項目就跳過。

### 步驟 6：收尾（commit + push）

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

- **投資觀察資料不得進版控、不得上靜態站**（`CLAUDE.md` 的「投資觀察」，`TestPositionsStayLocal`
  守著）。步驟 5 的產出只留在本機 `news.db`，`export-json` 只匯出 news 表。
  commit 前若發現 diff 含 position 相關資料，**停下來問使用者**，不要自行處理。
- **本機是唯一資料寫入來源**（`CLAUDE.md` 的「線上部署（GitHub Pages）」）。抓取與評分都在本機，
  CI 只負責把 JSON 轉靜態站。不要試圖在 CI 端補資料。

---

## 收尾回報

跑完回報五個數字，一行帶過即可：

```
fetch N 則新增｜評分 N 則（skip N）｜calibrate S/A N%（N 倍錨點）｜watch 判定 N 條｜position 到期 N 筆
commit <sha> 已 push
```

若某一步是 0 或跳過，照實寫 0，不要省略該欄——省略會讓人分不清
「沒有」和「忘了跑」。

**calibrate 那欄不能省，亮紅線時要加註 `drift` 的結論**——
寫法與理由見 `docs/calibrate-protocol.md` 的「回報時怎麼寫」。

**skip 掉的邊界案例要主動點名**（2026-08-17 實測）：那天 skip 39 則全憑標題，
其中 InP 基板那則事後補評是 B58，關鍵訊號（磊晶廠須現金交割）只在內文看得到。
把自己覺得可能誤判的兩三則列出來，使用者才有機會推翻你的粗篩。

---

## 已知 trap

- **BBC fetch 失敗是最高頻的踩雷**（65 次／10 個 session）。見步驟 2。
- **`pending --json` 的 url 直接用**，不要憑標題拼網址。**已知會重複犯**
  （08-01、08-02、09-01、09-03 各一次，規則寫兩處也擋不住）——錯在批次組
  WebFetch 的當下憑記憶填，且中央社猜錯流水號會回到另一篇真實報導、不報錯。
  組批次前先印一次 id+url 對照表，從輸出複製。見步驟 2。
- **同日多批要在 commit message 標批次序號**，否則 log 上分不出來。
- **步驟 4、5 沒有候選是正常的**，不要為了「有產出」硬湊判定——
  自動判定會產出大量似是而非的 hit，而這張表的全部價值在判讀品質
  （`CLAUDE.md` 的「watch_next 逐條驗證」的設計取捨）。
  判定前先問：**這條證據能不能直接回答指標問的問題？** 標籤交集只是候選線索，
  不是命中。2026-08-17 的 15 條候選全部落在「同標籤但問的不是同一件事」
  （貝瑞問 Oracle 租賃揭露 vs 今日 Baker 談輝達融資），照實記 0 條。
- **`calibrate` 評完必跑**（步驟 3），且基準是固定錨點而非昨天。
  單靠記得跑已經證明會漏——2026-07-25~28 連續四天漂移都回報「與前期一致」。
