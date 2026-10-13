# ChurnGuard：AI 流失預測 + 自動化喚醒推播

## 開始使用（網頁介面）
```bash
pip install -r requirements.txt
python main.py serve            # 開啟 http://127.0.0.1:8000
```
在網頁上 **上傳 CSV／Excel、貼上資料，或輸入連結**（含 Google 試算表，需設為「知道連結的人都能檢視」），
系統會自動辨識欄位並分析，產出：
- **AI 結論與行動建議**（優先挽回誰、預算夠不夠、哪個品類風險高、活躍顧客是否下滑…）
- 顧客分群（健康／預警／沉睡／已流失）、流失機率分布、每月活躍趨勢、**流失驅動因素**
- 依「預期保住營收」排序、受預算／冷卻期限制的**推播名單與個人化訊息**（可下載 CSV）
- 設定 `ANTHROPIC_API_KEY` 後，可對分析結果**直接問 Claude**（只傳彙總統計，不含個資）

## 資料格式
每列一筆訂單。必填：**顧客編號、訂單日期**；選填：訂單金額、商品類別、姓名、聯絡管道（line/email/app）、LINE User ID。
欄位名稱中英文皆可自動對應（例：會員編號、下單日期、實付金額），認錯可手動改。日期支援 2026-01-05、2026/1/5、2026年1月5日、Excel 日期；金額可含 NT$、逗號。
資料太少（< 約 6 個月或 < 100 位活躍顧客）時自動改用規則式評分並提醒。

## 命令列
```bash
python main.py demo                                         # 模擬資料
python main.py run --orders orders.csv|URL [--customers c.csv] [--config config.json]   # dry-run
LINE_CHANNEL_TOKEN=xxx python main.py run --orders ... --send                             # 真的發送
```
輸出在 `out/`。`config.json`（`python main.py init-config` 產生）可調門檻、預算、冷卻天數、勿擾時段、優惠文案、`channel`（console/line/webhook）、`use_llm`。

## 注意
- 網頁版只產生名單，不會發送；實際發送走命令列 `--send`。
- 連結匯入會擋內網／本機位址（防 SSRF）；本機測試可設 `CHURNGUARD_ALLOW_PRIVATE_URLS=1`。
- 要公開給他人使用請看 `DEPLOY.md`：設定 `CHURNGUARD_PUBLIC=1` 會啟用會話隔離、自動刪除、限流、同意聲明等防護；內建 Dockerfile 與 gunicorn 設定。
- 模擬資料的 AUC 僅代表流程可行；請用真實資料驗證。推播前確認顧客已同意接收並提供退訂機制。
