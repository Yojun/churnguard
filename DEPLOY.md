# 公開部署指南

## 最快：Railway / Render / Fly.io（用 Dockerfile）
1. 把整個專案資料夾推到 GitHub（或用平台的 CLI 直接部署）。
2. 在平台建立服務，選「從 Dockerfile 部署」。
3. 設定環境變數（見下表），至少要有 `CHURNGUARD_SECRET`。
4. 部署完成後平台會給一個 https 網址；要用自己的網域就在平台綁定。

沒有 Docker 也可以：安裝指令 `pip install -r requirements.txt`，啟動指令 `gunicorn -c gunicorn.conf.py churnguard.web:app`（已附 `Procfile`）。

> **不要把 workers 調高**：分析結果暫存在記憶體，多個 worker 會互相看不到。需要更大流量請升級機器規格，或之後改成共用儲存。

## 環境變數
| 變數 | 說明 | 公開預設 |
|---|---|---|
| `CHURNGUARD_PUBLIC` | `1` = 公開模式（隔離、自動刪除、限流、同意聲明、連結白名單） | Dockerfile 已設 1 |
| `CHURNGUARD_SECRET` | 簽署 session 的隨機長字串（`python -c "import secrets;print(secrets.token_hex(32))"`） | **必設**；沒設會每次重啟登出 |
| `CHURNGUARD_TRUST_PROXY` | `1` = 信任平台代理的 IP 標頭。**在代理後面必開**，否則限流會把所有人當同一個 IP | Dockerfile 已設 1 |
| `CHURNGUARD_HTTPS` | `1` = Cookie 加 Secure、送 HSTS（網站是 https 時開） | Dockerfile 已設 1 |
| `CHURNGUARD_PASSWORD` | 設了就要輸入密碼才能進入（適合只給客戶／內部使用） | 無 |
| `CHURNGUARD_CONTACT` | 顯示在頁尾與隱私頁的聯絡方式 | 無 |
| `ANTHROPIC_API_KEY` | 啟用「問 Claude」。**會產生費用**，建議同時保留限流 | 無（功能停用） |
| `CHURNGUARD_AI` | `0` = 即使有金鑰也關閉 Claude 問答 | 1 |
| `CHURNGUARD_ENABLE_URL` | `0` = 關閉連結匯入分頁 | 1 |
| `CHURNGUARD_URL_ALLOWLIST` | 連結可用的網域（逗號分隔） | Google 試算表、GitHub |
| `CHURNGUARD_TTL_MIN` | 閒置幾分鐘後刪除資料 | 30 |
| `CHURNGUARD_MAX_UPLOAD_MB` / `CHURNGUARD_MAX_ROWS` | 上傳大小／列數上限 | 10 / 200000 |
| `CHURNGUARD_RATE_ANALYZE` / `CHURNGUARD_RATE_ASK` | 每 IP 每小時分析／提問次數（0 = 不限） | 12 / 30 |
| `CHURNGUARD_MAX_JOBS` / `CHURNGUARD_JOBS_PER_SESSION` | 同時保留的分析數（全站／每位訪客） | 30 / 2 |
| `CHURNGUARD_MAX_CONCURRENT` | 同時進行的分析數（CPU 吃重） | 2 |

記憶體規劃：一份 10MB 的資料分析時約佔 100–300MB。`MAX_JOBS` × 單份大小 不要超過主機記憶體，主機小就調低 `MAX_JOBS` 或 `MAX_UPLOAD_MB`。

## 上線前檢查清單
- [ ] 已設 `CHURNGUARD_SECRET`，網站是 HTTPS
- [ ] 確認限流有效（用兩個不同網路各試一次；不確定就檢查 `CHURNGUARD_TRUST_PROXY`）
- [ ] 若設了 `ANTHROPIC_API_KEY`，到 Anthropic Console 為該金鑰設定用量上限
- [ ] 檢視 `/privacy` 頁面內容並補上 `CHURNGUARD_CONTACT`；隱私頁是通用範本，**請依你的實際營運主體與法規自行確認或請法務審閱**
- [ ] 平台日誌保留期限已確認（日誌含 IP 與網址，不含資料內容）
- [ ] 只想給特定客戶用 → 設 `CHURNGUARD_PASSWORD`，或在前面加 Cloudflare Access

## 已內建的防護與限制
- 每位訪客只能看到自己的分析；閒置 30 分鐘自動刪除；結果頁可立即刪除
- 每 IP 限流、單次上傳與列數上限、同時分析數上限
- 連結匯入限 Google 試算表／GitHub，並擋內網／本機位址（含轉址檢查）
- CSP、X-Frame-Options、nosniff、Referrer-Policy: no-referrer、敏感頁面 no-store
- 下載的 CSV 會中和以 `= + - @` 開頭的文字（避免 Excel 公式注入）
- 網站只產生名單，**不會**對顧客發送訊息

## 已知限制
- 服務重啟或平台重新部署時，所有暫存分析會消失（設計如此）。
- 無使用者帳號與歷史紀錄；若要做成會員制（保存多份分析、團隊共用），需要加資料庫與正式登入。
