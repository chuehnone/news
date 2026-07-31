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
python3 news.py calibrate [--date YYYY-MM-DD]  # 比對某日評分與錨點期（每批評完必跑，見下）
python3 news.py drift [--split YYYY-MM-DD]  # 偵測評分標準漂移（review 的前提，見下）
python3 news.py review [--window 30] [--since YYYY-MM-DD]  # 評分回顧校準（見下）
python3 news.py watch [--date YYYY-MM-DD] [--json]  # 列出當天新聞可能命中的舊 watch_next
python3 news.py watch-verify <url> <idx> <hit|miss|moot> [--note ...] [--evidence ...]
python3 news.py watch-stats    # watch_next 命中率統計
python3 news.py prune [--days 30]  # 清除 pending 中過期的已處理項目
python3 news.py schema          # 輸出 add 的 JSON 格式與驗證規則
python3 news.py add-position <file|->   # 新增一次投資觀點（格式見 position-schema）
python3 news.py positions [標的] [--pending] [-v]  # 列出投資觀點與預測狀態
python3 news.py position-due [標的]     # 列出到期該判定的預測
python3 news.py position-verify <預測id> <hit|miss|moot> [--note ...]
python3 news.py position-stats  # 投資預測命中率（依類型分組）
python3 news.py position-schema # 輸出 add-position 的 JSON 格式
python3 news.py export-json     # 匯出 news 表到 data/news.json（進版控）
python3 news.py import-json [--replace]  # 從 JSON 重建 news 表（CI 用）
python3 news.py export [--out dist] [--retention]  # 輸出靜態網站（--retention 為 CI 用，見下）
python3 news.py og              # 重產分享預覽圖 assets/og.png（需 ImageMagick，產完要 commit）
python3 -m unittest test_news    # 跑回歸測試（CI 也會跑）
```

## 每批評完的校準粗篩（`news.py calibrate`）

**2026-07-31 發現的循環論證**：每日批次評完後回報「與前期水準一致，無漂移」，
但那個檢查是拿**今天跟昨天**比，而兩天都落在已經墊高的區間裡——用逐日比較
去驗證有沒有逐日墊高，方法上不成立。實測 A 級佔比從錨點期的 5.7% 一路升到
7/25-7/28 的 **28-48%**，而每天都回報「一致」。

`drift` 當時也沒報警：它的切分點取中位日（7/25），前後期都在墊高後的區間，
且 8% 門檻對 +4.8% 放行。

- **基準一律是固定錨點，不與昨天比**。`anchor_sa_rate` 即時從 db 的
  `ANCHOR_START`~`ANCHOR_END` 算出（實測 5.7%）。刻意不寫死常數——寫死的
  數字與實際資料脫鉤後沒有任何機制會發現；改由 `TestCalibrateBaseline`
  斷言它等於 0.057，錨點期資料若被改動測試會失敗。
  **那個測試失敗時要先查錨點期的評分是不是被動過，不要直接改期望值**。
- **主判準是 S/A 佔比，不是單一面向**。實測決策相關性完全沒漂（錨點中位 8，
  近期 8/8/8.5），漂的是總分層面——五個面向各自的小幅上升加總後把等級推高。
  五個面向的中位數留作**輔助診斷**，用來看是哪個面向在推。
- **門檻 `CALIBRATE_SA_MULTIPLE` = 2.5x**。用全部 22 個批次實測觸發率：
  1.5x → 50%、2x → 41%、2.5x → 32%、3x → 27%。取 2.5x 是因為它精準命中
  7/25-7/28 的異常區間，同時放行 7/20、7/30 這類 10-12% 的正常波動。
  **1.5x 與 2x 會讓半數批次都示警，那等於沒有警告**（由
  `test_does_not_warn_on_normal_variation` 守著）。
- **少於 `CALIBRATE_MIN_BATCH`（10）則不檢查**：10 則裡有 2 則 A 就是 20%，
  佔比本身不穩定。實測 7/13、7/14 各只有 10 則卻都達 20%，正是這種誤觸發。
- **這個指標分不出「標準鬆了」與「當期新聞真的更重要」**，報表必須明說這點。
  每批只有 10-20 則，湊不出主題控制需要的樣本（`drift` 要求前後期各 5 則），
  所以 calibrate 只做「要不要停下來看一眼」的粗篩，**區分兩者一律看 `drift`**。
- **`add` 寫入後會對當日累計做一行紅線提醒**（`_warn_if_batch_drifts`）。
  只印一行、只在超過門檻時印：評分當下看到長篇分析也來不及改，作用只是讓人
  停下來。這一層存在的理由是「分開做就不會做」——單靠記得跑 calibrate 已經
  證明會漏掉整整四天。檢查範圍是**當日累計**而非單一批次，因為 `add` 沒有
  批次概念（7/30 分三批評分別是 12%/15%/0%，只看得到合計的 10%）。
- 錨點為 0% 時倍數是無限大，輸出改印「遠高於」而非 `inf`——後者讀起來像壞掉
  而不像訊息。由 `test_zero_baseline_does_not_print_inf` 守著。

**輔助診斷的實測價值**：7/25 那批五個面向有四個同步上升（決策相關性
8 → 12.5 最劇烈），只有**事實可信度不動**。這正是「事實可信度是天然對照組」
的再次驗證——推力來自主觀判斷鬆動，不是來源品質改變。

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
- **固定錨點是絕對門檻的基準**（`news.py anchors` 重新產生）。取
  `ANCHOR_START`～`ANCHOR_END`（2026-06-22～07-10）的實際評分算出各分數段的
  面向中位數，寫進 `/news-importance-score` skill 供評分時對照。
  期間**刻意寫死日期而非「最近 N 則」**：錨點若隨資料滾動，標準會跟著近期
  評分一起漂，等於沒有錨點。由 `TestAnchorsAreFixed` 守著。
  關鍵數字：**A 級的決策相關性中位數是 12**，`≥15` 前期只出現在颱風登陸台灣
  （全民當天要改變行動）。**不要因為近期新聞看起來更重要而更新錨點**——那正是漂移。
- **漂移的典型長相是「同主題逐日墊高」**：前期「荷莫茲船隻遇襲＋美撤銷制裁豁免」
  評 A70／決策 12，後期「伊朗攔截 4 艘船（未遂）」卻評 A76／決策 15——後者量級
  更小分數卻更高。中東、關稅、AI 這類連續報導的主題最容易發生，看多了會把
  「又一則」當成「更嚴重」。

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

## 投資觀察（`add-position` / `positions` / `position-due` / `position-verify` / `position-stats`）

2026-07-29 新增的第二條線。**動機是 watch_next 的實測結論**：那套機制證明了
「寫下可驗證預測 → 到期逐條判定 → 統計命中率」是唯一能回答「判斷準不準」的做法，
但新聞題材的驗證品質受限於來源（見「來源不涵蓋型僅 15%」那條）。投資標的的驗證
乾淨得多——營收年增就是一個數字，沒有「這算不算命中」的判讀空間。

**換的是題材，不是機制**。判定值域（`WATCH_VERDICTS`）與 moot 語意兩邊共用，
`hit_rate` / `accuracy_by` 是抽出來給兩邊用的，各存一份遲早會漂。

- **資料粒度是「一次觀點」而非「一個標的」**：`positions` 存某時點對某標的的判斷
  （thesis + rationale），底下掛多條 `predictions`。同標的多次觀點形成時間序列。
  刻意不做成「標的持續追蹤」——那樣所有預測混在同一個標的底下，
  三個月後看到「8 月營收年增 >30%」卻不知道當時為什麼那樣想，
  **而那正是事後檢討時唯一有價值的部分**。`thesis` 因此是必填。
- **預測分兩類（`PREDICTION_KINDS`）且命中率必須分開看**：基本面有客觀數字可查、
  結構要人判讀事件是否發生。混算會得到一個無法行動的總命中率——
  那正是 `review` 的失敗模式（測得出數字但無法轉成改進）。
  由 `test_hit_rate_is_reported_per_kind` 守著。
- **「市場類」（股價、相對報酬）於 2026-07-31 廢除，不要補回來**
  （由 `test_no_market_prediction_kind` 守著）。兩個理由：
  1. **沒有資料源**。`feeds.txt` 只有中央社／BBC／科技新報，而 `LOWPRIO_KEYWORDS`
     還主動過濾「收盤／盤中／盤前」——價格資訊本來就不在這個系統裡。實測 7 條
     市場類預測全部無法判定，**連 miss 都算不上**（進不了分母，只會永遠掛著）。
  2. **更根本的是它測不出判斷力**。「Meta 落後標普 8 個百分點」的結果混雜利率、
     地緣、大盤情緒，推論正確與否只佔很小部分——與 `review` 用「標籤後續數」
     當代理訊號是同一種病。
  原本想測的「資訊是否已被 price in」，用基本面預測搭配新聞觀察即可
  （例：台積電毛利率創高的同時股價表現如何，新聞會報導）。
- **每條預測必填 `source_hint`（到期時去哪裡查）**，模糊詞（`VAGUE_SOURCE_HINTS`：
  財報、新聞、股價…）會被擋下。這是把檢查放進「寫的當下」而非事後——
  那 7 條市場類預測寫得很工整（都指明了比較基準與時間窗），問題正是
  **沒人在寫的當下問過「這個數字從哪來」**。現行 schema 早就有寫預測的原則，
  照樣寫錯，證明「寫指引期待下次記得」無效（同「分開做就不會做」）。
  填不出具體來源 = 這條預測現在就該重寫。
  補填時立刻抓到兩條同類問題（輝達應收帳款細項、美光產品線營收拆分），
  兩者都需要新聞不會報導的財報顆粒度——**含財報數字的新聞僅佔全站 9%**。
- **`void`（作廢）與 `moot`（前提消失）刻意分開，差別在歸因**：
  `moot` 是世界變了（談判取消），`void` 是我當初設計了無法驗證的指標。
  混用會讓 moot 失去診斷價值——同 CLAUDE.md 堅持 moot 與 miss 分開的理由。
  統計處理也不同：**`void` 連 counts 都不進**（它不是一種判定結果），
  `moot` 仍計入 counts 但不進分母（「前提消失」本身是關於世界的資訊）。
  但 void 的**數量必須顯示**，藏起來就是選擇性揭露。
  `void` 只在 `POSITION_VERDICTS`，不進 `WATCH_VERDICTS`——新聞線的 watch_next
  不依賴外部資料源，沒有這個問題，多餘的選項會被誤用。
  作廢不等於刪除：記錄「我曾經寫了無法驗證的預測」是校準的一部分。
- **未判定不等於 miss**：新增的預測 `verdict` 為 NULL，不進命中率分母。
  若預設成任何一種判定，命中率會從一開始就被系統性扭曲。
  由 `test_verdict_defaults_to_unjudged` 守著。
  但未判定也不能無限累積——`position-due` 會把放滿 `POSITION_MIN_AGE_DAYS`（14 天）
  的列出來，否則不利的預測會默默停在未判定，等於排除在統計外。
- **改判定要 `--force`**：事後改判定會讓命中率失去意義。
- **`POSITION_MIN_AGE_DAYS` 比新聞的 7 天長**：基本面預測的驗證點（月營收、財報）
  本來就以月為單位，太早看必然是「還沒發生」。
- **資料只存 `news.db`，不進版控、不上靜態站**（`TestPositionsStayLocal` 守著）。
  理由不是技術性的：repo 是 public，而**公開投資判斷會改變書寫方式**——
  會不自覺寫得保守、寫得容易命中，而這條線的全部價值就在於記錄真實的判斷。
  呈現只走本機 `news.py serve` 的 `/positions`（頁面帶 `noindex`）。
  代價是 db 沒有副本，備份要自己處理。
- **`validate_date_string` 是兩條線共用的日期驗證**：news_date 與 obs_date／due_date
  都做字串字面比較，補零規則必須一致。`allow_future` 是唯一的差別——
  預測的到期日本來就在未來。

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

三件必須順手做的事（分開做就不會做）：

1. **評分前先讀前期錨定範例**（`news.py anchors`）。2026-07-28 實測：
   未校準的兩批 A 級佔 32-43%、決策相關性平均 11.1；讀完前期範例後的那批
   降到 8.7% 與 9.91，回到前期水準。差別只在有沒有先看。
2. **評分後跑 `news.py calibrate`** 驗算這批與錨點的差距。
   ⚠️ **不要用「跟昨天比」代替這一步**——那是循環論證，已經漏掉整整四天的
   漂移（見「每批評完的校準粗篩」）。`add` 會在超標時印一行提醒，但那只是
   紅線，完整診斷要跑 `calibrate`。
3. **評分後跑 `news.py watch`**，判讀當天新聞命中了哪些舊指標。剛讀完當日
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

- `news.py` — CLI（init / add / list / serve / fetch / pending / calibrate / drift /
  review / watch / watch-verify / watch-stats / anchors / tags / tag / alias /
  export-json / import-json / export；投資線見 add-position / positions /
  position-due / position-verify / position-stats / position-schema）。
  schema 常數（`DIMENSIONS` / `SECTIONS` / `GRADE_THRESHOLDS` / `GRADES` / `GRADE_LABELS`）定義在此，是唯一出處
- `test_news.py` — 回歸測試（標準庫 unittest，134 個）。涵蓋 news_date 格式驗證、
  保留期分層、匯出／匯入 round-trip 無損、動態站與靜態站的篩選一致性、
  標籤正規化與整值比對、schema 常數與函式不得重複定義、
  回顧校準的三項偏誤修正、watch_next 驗證的候選收窄與 moot 語意、
  卡片只標命中且必附分母、錨點期間不得隨資料滾動、
  calibrate 的基準不得滾動與門檻不得誤報、
  投資預測必填 source_hint 且市場類不得復活、void 與 moot 的統計處理不同、
  投資觀察不得外洩到靜態站或版控；
  改這幾處的邏輯後務必跑過（CI 也會在建站前跑）。
- `server.py` — 網頁介面，Python 標準庫實作，無外部依賴。常數一律 import 自 `news.py`
- `fetch_article.py` — 內文抓取 fallback（BBC 等 WebFetch 被擋的站）
- `feeds.txt` — RSS 來源清單
- `data/news.json` — 進版控的資料來源（由 export-json 產生）
- `data/watch_verify.json` — watch_next 判定結果，進版控（非網站資料，但 db 不進版控）
- `assets/og.png` — 分享預覽圖，進版控的成品（由 `news.py og` 產生，export 複製上線）
- `.github/workflows/deploy.yml` — 部署 GitHub Pages
- `news.db` — SQLite 資料庫（不進版控，可由 import-json 重建）
