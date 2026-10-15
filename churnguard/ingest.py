"""資料匯入：檔案 / 貼上文字 / 連結 → 自動辨識欄位 → 清理成分析用格式。"""
import io
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import warnings

import numpy as np
import pandas as pd

MAX_BYTES = 20 * 1024 * 1024


class DataError(Exception):
    """給使用者看的資料問題（訊息為繁體中文）。"""


FIELD_LABELS = {
    "customer_id": "顧客編號（必填）",
    "last_order_date": "最近消費日（會員摘要）",
    "order_count": "消費次數（會員摘要）",
    "total_spend": "累計消費金額（會員摘要）",
    "first_order_date": "首次消費／加入日（會員摘要）",
    "order_date": "訂單日期（訂單明細）",
    "amount": "訂單金額（訂單明細）",
    "name": "顧客姓名",
    "channel": "聯絡管道",
    "category": "商品／偏好類別",
    "line_user_id": "LINE User ID",
}
REQUIRED = ["customer_id", "order_date"]   # 會員摘要模式下，order_date 可由 last_order_date 取代
FIELDS = list(FIELD_LABELS)
FIELD_GROUPS = [
    ("共同欄位", ["customer_id", "name", "channel", "category", "line_user_id"]),
    ("訂單明細（每列一筆訂單）", ["order_date", "amount"]),
    ("會員摘要（每列一位顧客）", ["last_order_date", "order_count", "total_spend", "first_order_date"]),
]

SYNONYMS = {
    "customer_id": ["customerid", "custid", "memberid", "userid", "clientid", "customer", "member",
                    "會員編號", "會員id", "會員", "顧客編號", "顧客id", "顧客", "客戶編號", "客戶id", "客戶",
                    "帳號", "手機", "電話", "email", "信箱", "id"],
    "last_order_date": ["lastorderdate", "lastpurchasedate", "lastpurchase", "lastvisit", "lastorder",
                        "最近消費日", "最後消費日", "最近購買日", "最後購買日", "最近消費日期", "最後消費日期",
                        "最近購買日期", "最後購買日期", "最近一次消費", "最近一次購買", "最近來店", "最後來店",
                        "上次消費", "上次購買", "上次來店"],
    "order_count": ["ordercount", "orders", "numorders", "frequency", "purchasecount", "visits",
                    "消費次數", "購買次數", "訂單數", "訂單筆數", "來店次數", "購買頻次", "交易次數"],
    "total_spend": ["totalspend", "totalspent", "ltv", "lifetimevalue", "累計消費", "累積消費", "消費總額",
                    "累計金額", "累積金額", "累計消費金額", "累積消費金額", "歷史消費", "總消費"],
    "first_order_date": ["firstorderdate", "firstpurchase", "joindate", "signupdate", "registerdate",
                         "首次消費日", "首次購買日", "首次消費日期", "首購日", "首購日期", "加入日期", "加入日",
                         "註冊日期", "註冊日", "入會日期", "入會日", "開卡日"],
    "order_date": ["orderdate", "date", "datetime", "createdat", "purchasedate", "transactiondate",
                   "日期", "訂單日期", "交易日期", "購買日期", "下單日期", "下單時間", "消費日期", "時間"],
    "amount": ["amount", "total", "price", "revenue", "sales", "金額", "訂單金額", "消費金額",
               "實付", "實付金額", "總額", "總金額", "小計", "營收"],
    "name": ["name", "customername", "姓名", "顧客姓名", "會員姓名", "會員名稱", "客戶名稱", "暱稱"],
    "channel": ["channel", "contact", "推播管道", "聯絡管道", "管道", "偏好管道"],
    "category": ["category", "product", "item", "品類", "商品類別", "類別", "商品", "品項", "產品", "偏好品類", "常購品類"],
    "line_user_id": ["lineuserid", "lineid", "lineuid", "line"],
}
_CJK = re.compile(r"[一-鿿]")


# ───────────────────────── 讀取來源 ─────────────────────────
def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _sniff(text: str) -> str:
    first = text.splitlines()[0] if text else ""
    counts = {s: first.count(s) for s in (",", "\t", ";", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.loc[:, [not str(c).startswith("Unnamed") or df[c].notna().any() for c in df.columns]]
    df = df.reset_index(drop=True)
    if len(df) == 0 or df.shape[1] < 2:
        raise DataError("讀到的表格是空的，或只有一個欄位。請確認第一列是欄位名稱，且至少包含顧客編號與日期。")
    return df


def _read_text(text: str) -> pd.DataFrame:
    text = text.strip("﻿\r\n ")
    if not text:
        raise DataError("沒有收到資料。")
    try:
        df = pd.read_csv(io.StringIO(text), sep=_sniff(text), dtype=object, skipinitialspace=True)
    except Exception as e:  # noqa
        raise DataError(f"無法解析成表格（支援 CSV／TSV／Excel）：{e}")
    return _clean_frame(_infer_types(df))


def _infer_types(df: pd.DataFrame) -> pd.DataFrame:
    """read_csv 以 object 讀入以保留 ID 的前導 0；其餘欄位交給 prepare 再轉型。"""
    return df


def read_table_bytes(raw: bytes, filename: str = "") -> pd.DataFrame:
    if len(raw) > MAX_BYTES:
        raise DataError("檔案超過 20MB，請先篩選或拆分後再上傳。")
    if filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        try:
            sheets = pd.read_excel(io.BytesIO(raw), dtype=object, sheet_name=None)
        except ImportError:
            raise DataError("伺服器缺少 openpyxl，無法讀取 Excel，請改用 CSV。")
        except Exception as e:  # noqa
            raise DataError(f"Excel 讀取失敗：{e}")
        cands = [(len(d.dropna(how="all")), n, d) for n, d in sheets.items() if str(n).strip() != "說明" and d.shape[1] >= 2]
        if not cands:
            raise DataError("Excel 裡沒有可用的資料表。")
        n_rows, _, best = max(cands, key=lambda t: t[0])           # 多個工作表時取資料最多的一張
        if n_rows == 0:
            raise DataError("Excel 裡還沒有填寫任何資料（只有標題列）。請把資料填在「會員摘要」或「訂單明細」工作表後再上傳。")
        return _clean_frame(best)
    return _read_text(_decode(raw))


# ───────────────────────── 連結 ─────────────────────────
def _check_host(host: str):
    if os.environ.get("CHURNGUARD_ALLOW_PRIVATE_URLS") == "1":
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise DataError("無法解析連結的主機名稱，請確認網址是否正確。")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise DataError("基於安全考量，不支援內部網路或本機位址的連結。")


def _allowed_host(host: str) -> bool:
    """公開版可用 CHURNGUARD_URL_ALLOWLIST（逗號分隔網域）限制只能從特定網站匯入。"""
    allow = [h.strip().lower() for h in os.environ.get("CHURNGUARD_URL_ALLOWLIST", "").split(",") if h.strip()]
    host = host.lower()
    return not allow or any(host == a or host.endswith("." + a) for a in allow)


def _check_url(url: str):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise DataError("連結必須以 http:// 或 https:// 開頭。")
    if not _allowed_host(p.hostname):
        raise DataError("此網站僅支援 Google 試算表與 GitHub 的連結；其他來源請改用上傳檔案。")
    _check_host(p.hostname)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalize_url(url: str) -> str:
    """Google 試算表／GitHub 檔案連結 → 可直接下載的 CSV 連結。"""
    url = url.strip()
    m = re.match(r"https?://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m:
        gid = re.search(r"[?&#]gid=(\d+)", url)
        return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
                + (f"&gid={gid.group(1)}" if gid else ""))
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/blob/(.+)", url)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
    return url


def fetch_url(url: str) -> pd.DataFrame:
    url = normalize_url(url)
    _check_url(url)
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "ChurnGuard/1.0"})
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read(MAX_BYTES + 1)
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise DataError("連結需要登入或沒有權限。Google 試算表請設為「知道連結的人都能檢視」，或改用上傳檔案。")
        raise DataError(f"下載失敗（HTTP {e.code}）。")
    except DataError:
        raise
    except Exception as e:  # noqa
        raise DataError(f"無法連線到該連結：{type(e).__name__}。若網站需要登入，請改用上傳檔案。")
    if len(raw) > MAX_BYTES:
        raise DataError("連結內容超過 20MB。")

    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith((".xlsx", ".xlsm", ".xls")) or "spreadsheetml" in ctype:
        return read_table_bytes(raw, "x.xlsx")
    text = _decode(raw)
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        try:
            tables = pd.read_html(io.StringIO(text))
            best = max(tables, key=lambda t: t.shape[0] * t.shape[1])
            return _clean_frame(best.astype(object))
        except Exception:  # noqa
            raise DataError("該連結是網頁而不是資料表。若是 Google 試算表，請設為「知道連結的人都能檢視」；"
                            "否則請改用上傳檔案。")
    return _read_text(text)


def load_source(file=None, text: str = "", url: str = ""):
    """優先順序：檔案 > 貼上 > 連結。回傳 (DataFrame, 來源說明)。"""
    if file is not None and getattr(file, "filename", ""):
        return read_table_bytes(file.read(), file.filename), f"檔案：{file.filename}"
    if text and text.strip():
        return _read_text(text), "貼上的資料"
    if url and url.strip():
        return fetch_url(url), f"連結：{url.strip()[:80]}"
    raise DataError("請上傳檔案、貼上資料，或輸入連結。")


# ───────────────────────── 欄位辨識 ─────────────────────────
def _norm(s) -> str:
    return re.sub(r"[\s_\-./（）()]", "", str(s).lower())


def _score(field: str, col: str) -> int:
    c = _norm(col)
    if field == "customer_id" and any(k in c for k in ("order", "訂單編號", "單號", "name", "姓名", "名稱")):
        return 0
    best = 0
    for syn in SYNONYMS[field]:
        if c == syn:
            best = max(best, 100)
        elif (len(syn) >= 3 or (_CJK.search(syn) and len(syn) >= 2)) and syn in c:
            best = max(best, 50 + len(syn))
        elif len(c) >= 3 and c in syn:
            best = max(best, 30)
    return best


def _to_number(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce")
    cleaned = s.astype(str).str.replace(r"[^\d.\-]", "", regex=True).replace("", np.nan)
    return pd.to_numeric(cleaned, errors="coerce")


def _to_date(s: pd.Series) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if pd.api.types.is_datetime64_any_dtype(s):
            d = s
        else:
            num = pd.to_numeric(s, errors="coerce")
            if num.notna().mean() > 0.9 and num.between(20000, 80000).all():       # Excel 序列日期
                d = pd.to_datetime(num, unit="D", origin="1899-12-30", errors="coerce")
            else:
                d = pd.to_datetime(s, errors="coerce")
                if d.notna().mean() < 0.5:                                         # 中文格式如「2024年3月5日 下午 2:30」
                    ymd = s.astype(str).str.extract(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})").astype(float)
                    ymd.columns = ["year", "month", "day"]
                    d2 = pd.to_datetime(ymd, errors="coerce")
                    if d2.notna().sum() > d.notna().sum():
                        d = d2
        if getattr(d.dt, "tz", None) is not None:
            d = d.dt.tz_convert("Asia/Taipei").dt.tz_localize(None)
    return d.dt.normalize()


def _looks_like_date(s: pd.Series) -> bool:
    sample = s.dropna().head(200)
    if len(sample) == 0 or pd.api.types.is_bool_dtype(sample):
        return False
    return _to_date(sample).notna().mean() >= 0.8 and sample.astype(str).str.contains(r"\d{1,4}\D\d{1,2}").mean() > 0.8


def detect_mapping(df: pd.DataFrame) -> dict:
    cols = list(df.columns)
    cands = sorted(((_score(f, c), f, c) for f in FIELDS for c in cols), key=lambda t: -t[0])
    mapping, used = {f: None for f in FIELDS}, set()
    for s, f, c in cands:
        if s <= 0:
            break
        if mapping[f] is None and c not in used:
            mapping[f] = c
            used.add(c)
    if mapping["order_date"] is None:
        for c in cols:
            if c not in used and _looks_like_date(df[c]):
                mapping["order_date"] = c
                used.add(c)
                break
    if mapping["amount"] is None:
        for c in cols:
            if c not in used and _to_number(df[c].head(500)).notna().mean() >= 0.9 and not _looks_like_date(df[c]):
                mapping["amount"] = c
                used.add(c)
                break
    return mapping


def _has(df, mapping, f):
    return bool(mapping.get(f)) and mapping[f] in df.columns


def missing_required(mapping: dict, df: pd.DataFrame) -> list:
    miss = [] if _has(df, mapping, "customer_id") else ["customer_id"]
    if not (_has(df, mapping, "order_date") or _has(df, mapping, "last_order_date")):
        miss.append("order_date")
    return miss


# ───────────────────────── 清理成分析格式 ─────────────────────────
def _norm_channel(v, default):
    if pd.isna(v):
        return default
    t = str(v).lower()
    if "line" in t:
        return "line"
    if "mail" in t or "信箱" in t or "郵件" in t:
        return "email"
    if "app" in t or "推播" in t:
        return "app"
    if "sms" in t or "簡訊" in t:
        return "sms"
    return default


def _col(df, mapping, field):
    c = mapping.get(field)
    return df[c] if c and c in df.columns else None


def _ids(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    return s.mask(s.isin(["", "nan", "NaN", "None", "NaT", "<NA>"]))


def _customers_from(df, mapping, cid, default_channel):
    t = pd.DataFrame({"customer_id": cid})
    for f in ("name", "channel", "line_user_id"):
        c = _col(df, mapping, f)
        t[f] = c.astype(str).str.strip().replace({"": np.nan, "nan": np.nan, "None": np.nan}).values if c is not None else np.nan
    cust = t.groupby("customer_id", sort=False).last().reset_index()
    cust["channel"] = cust["channel"].map(lambda v: _norm_channel(v, default_channel))
    return cust


def prepare(df: pd.DataFrame, mapping: dict, default_channel: str = "line"):
    """回傳 (orders, customers, report)。"""
    miss = missing_required(mapping, df)
    if miss:
        raise DataError("請指定必填欄位：" + "、".join({"customer_id": "顧客編號", "order_date": "訂單日期（或最近消費日）"}[f] for f in miss))
    rep = {"rows_in": int(len(df)), "warnings": [], "no_amount": False, "mode": "orders"}

    cid = _ids(df[mapping["customer_id"]])
    date = _to_date(df[mapping["order_date"]])
    amt_col = _col(df, mapping, "amount")
    if amt_col is None:
        amt = pd.Series(1.0, index=df.index)
        rep["no_amount"] = True
        rep["warnings"].append("沒有金額欄位：每筆訂單以 1 計，分析會以購買次數而非金額評估價值。")
    else:
        amt = _to_number(amt_col)
        bad = int(amt.isna().sum())
        if bad:
            rep["warnings"].append(f"{bad:,} 筆金額無法辨識，已以 0 計。")
        amt = amt.fillna(0).clip(lower=0)
    cat_col = _col(df, mapping, "category")
    cat = (cat_col.astype(str).str.strip().replace({"": np.nan, "nan": np.nan, "None": np.nan}).fillna("精選商品")
           if cat_col is not None else pd.Series("精選商品", index=df.index))

    bad_id, bad_date = cid.isna(), date.isna()
    if bad_id.sum():
        rep["warnings"].append(f"{int(bad_id.sum()):,} 筆沒有顧客編號，已略過。")
    if (bad_date & ~bad_id).sum():
        rep["warnings"].append(f"{int((bad_date & ~bad_id).sum()):,} 筆日期無法辨識，已略過。")
    keep = ~(bad_id | bad_date)
    if keep.sum() == 0:
        raise DataError("沒有任何可用的訂單列：日期欄位無法辨識或顧客編號為空。請在下一步確認欄位對應。")

    orders = pd.DataFrame({"customer_id": cid[keep], "order_date": date[keep],
                           "amount": amt[keep], "category": cat[keep]}).reset_index(drop=True)
    cust = _customers_from(df[keep].reset_index(drop=True), mapping, orders["customer_id"], default_channel)

    n_c = orders.customer_id.nunique()
    if n_c == len(orders):
        rep["warnings"].append("每位顧客只有一筆訂單，無法判斷購買頻率；分析會以「距離上次購買天數」為主，準確度有限。")
    if n_c < 30:
        rep["warnings"].append(f"只有 {n_c} 位顧客，統計結果參考性低。")
    span = (orders.order_date.max() - orders.order_date.min()).days
    rep.update(rows_used=int(len(orders)), n_customers=int(n_c), span_days=int(span),
               date_min=orders.order_date.min().date().isoformat(), date_max=orders.order_date.max().date().isoformat())
    return orders, cust, rep


def merge_customers(base: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    """以另外上傳的顧客名單補充姓名／管道／LINE ID（優先採用名單內容）。"""
    b = base.set_index("customer_id")
    e = extra.drop_duplicates("customer_id", keep="last").set_index("customer_id").reindex(b.index)
    return e.combine_first(b).reset_index()


def prepare_customers(df: pd.DataFrame, default_channel: str = "line") -> pd.DataFrame:
    mapping = detect_mapping(df)
    if not mapping.get("customer_id"):
        raise DataError("顧客名單找不到「顧客編號」欄位。")
    cid = _ids(df[mapping["customer_id"]])
    return _customers_from(df[cid.notna()].reset_index(drop=True), mapping, cid[cid.notna()].reset_index(drop=True), default_channel)


# ───────────────────────── 會員摘要模式（每位顧客一列） ─────────────────────────
def is_summary(df, mapping) -> bool:
    """欄位有「最近消費日」，或每位顧客只有一列且有消費次數／累計金額 → 會員摘要。"""
    if _has(df, mapping, "last_order_date"):
        return True
    if _has(df, mapping, "customer_id") and _has(df, mapping, "order_date") and (
            _has(df, mapping, "order_count") or _has(df, mapping, "total_spend")):
        return bool(df[mapping["customer_id"]].dropna().is_unique)
    return False


def parse_asof(text):
    text = (text or "").strip()
    if not text:
        return None
    d = pd.to_datetime(text, errors="coerce")
    if pd.isna(d):
        raise DataError("基準日格式不正確，請輸入像 2026-10-14 的日期。")
    return d.normalize()


def prepare_summary(df, mapping, default_channel="line", typical_cycle=45, asof=None):
    """回傳 (features, customers, report)。沒有逐筆訂單，所以直接由摘要欄位推算購買節奏。"""
    if not _has(df, mapping, "customer_id"):
        raise DataError("請指定必填欄位：顧客編號")
    last_col = mapping["last_order_date"] if _has(df, mapping, "last_order_date") else mapping.get("order_date")
    if not last_col or last_col not in df.columns:
        raise DataError("請指定必填欄位：最近消費日")
    rep = {"rows_in": int(len(df)), "warnings": [], "no_amount": False, "mode": "summary"}

    cid, last = _ids(df[mapping["customer_id"]]), _to_date(df[last_col])
    if cid.isna().sum():
        rep["warnings"].append(f"{int(cid.isna().sum()):,} 列沒有顧客編號，已略過。")
    if (cid.notna() & last.isna()).sum():
        rep["warnings"].append(f"{int((cid.notna() & last.isna()).sum()):,} 列的最近消費日無法辨識，已略過。")
    keep = (cid.notna() & last.notna()).to_numpy()
    if keep.sum() == 0:
        raise DataError("沒有任何可用的列：最近消費日無法辨識或顧客編號為空。請確認欄位對應。")
    d, cid, last = df[keep].reset_index(drop=True), cid[keep].reset_index(drop=True), last[keep].reset_index(drop=True)
    dup = cid.duplicated(keep="last").to_numpy()
    if dup.any():
        rep["warnings"].append(f"有 {int(dup.sum()):,} 列顧客編號重複，已保留最後一列。")
        d, cid, last = d[~dup].reset_index(drop=True), cid[~dup].reset_index(drop=True), last[~dup].reset_index(drop=True)

    asof = pd.Timestamp(asof) if asof is not None else last.max()
    cnt_col = _col(d, mapping, "order_count")
    if cnt_col is not None:
        freq = _to_number(cnt_col).fillna(1).clip(lower=1)
    else:
        freq = pd.Series(1.0, index=d.index)
        rep["warnings"].append("沒有「消費次數」欄位：每位以 1 次計。")
    spend_col = _col(d, mapping, "total_spend")
    if spend_col is None:
        spend_col = _col(d, mapping, "amount")
    if spend_col is None:
        monetary = freq.astype(float)
        rep["no_amount"] = True
        rep["warnings"].append("沒有「累計消費金額」欄位：以消費次數代替金額評估價值。")
    else:
        monetary = _to_number(spend_col).fillna(0).clip(lower=0)

    recency = (asof - last).dt.days.clip(lower=0)
    first_col = _col(d, mapping, "first_order_date")
    if first_col is not None:
        first = _to_date(first_col)
        first = first.where(first.notna() & (first <= last), last)        # 缺漏或晚於最近消費日 → 視為與最近消費同日
        span = (last - first).dt.days
        gap = pd.Series(np.where((freq > 1) & (span > 0), span / (freq - 1).clip(lower=1), float(typical_cycle)), index=d.index)
        tenure = recency + span
    else:
        gap = pd.Series(float(typical_cycle), index=d.index)
        tenure = recency + (freq - 1) * typical_cycle
        if cnt_col is None:
            rep["warnings"].append(f"沒有首次消費日或消費次數，無法得知每位顧客自己的購買節奏，改用行業典型週期（約 {typical_cycle} 天）判斷。")
        else:
            rep["warnings"].append(f"沒有「首次消費／加入日」：無法算出每位顧客自己的購買間隔，改用行業典型週期（約 {typical_cycle} 天）判斷。")
    gap = gap.clip(lower=1)

    cat_col = _col(d, mapping, "category")
    cat = (cat_col.astype(str).str.strip().replace({"": np.nan, "nan": np.nan, "None": np.nan}).fillna("精選商品")
           if cat_col is not None else pd.Series("精選商品", index=d.index))
    f = pd.DataFrame({
        "recency": recency.astype(float), "frequency": freq.astype(float), "monetary": monetary.astype(float),
        "avg_amount": (monetary / freq).astype(float), "avg_gap": gap.astype(float), "std_gap": 0.0,
        "last_gap": gap.astype(float), "overdue_ratio": (recency / gap).astype(float),
        "freq_30": 0.0, "freq_90": 0.0, "freq_prev90": 0.0, "trend": 0.0, "tenure": tenure.astype(float),
        "fav_category": cat.values, "last_order": last.values,
    })
    f.index = pd.Index(cid.values, name="customer_id")   # 先以共同索引組表，再換成顧客編號（直接指定 index 會依標籤對齊而變成 NaN）
    cust = _customers_from(d, mapping, cid, default_channel)

    n = len(f)
    if n < 30:
        rep["warnings"].append(f"只有 {n} 位顧客，統計結果參考性低。")
    rep.update(rows_used=n, n_customers=n, span_days=int((last.max() - last.min()).days),
               date_min=last.min().date().isoformat(), date_max=last.max().date().isoformat(), asof=asof.date().isoformat())
    return f, cust, rep
