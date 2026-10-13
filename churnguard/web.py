"""網頁介面：上傳／貼上／連結 → 確認欄位 → 分析結果。

公開部署請設 CHURNGUARD_PUBLIC=1（見 DEPLOY.md）。公開模式會啟用：
會話隔離、閒置自動刪除、使用頻次限制、上傳／筆數上限、連結網域白名單、同意聲明、安全標頭。
"""
import functools
import hmac
import io
import os
import secrets
import threading
import time
from collections import defaultdict, deque

import pandas as pd
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import analysis, campaign, data, ingest, insights


def _env_int(key, default):
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default


PUBLIC = os.environ.get("CHURNGUARD_PUBLIC") == "1"
PASSWORD = os.environ.get("CHURNGUARD_PASSWORD", "")
CONTACT = os.environ.get("CHURNGUARD_CONTACT", "")
HTTPS = os.environ.get("CHURNGUARD_HTTPS", "0") == "1"
ENABLE_URL = os.environ.get("CHURNGUARD_ENABLE_URL", "1") != "0"
ENABLE_AI = os.environ.get("CHURNGUARD_AI", "1") != "0"
TTL = _env_int("CHURNGUARD_TTL_MIN", 30 if PUBLIC else 24 * 60) * 60          # 閒置多久後刪除
MAX_JOBS = _env_int("CHURNGUARD_MAX_JOBS", 30 if PUBLIC else 20)
JOBS_PER_SESSION = _env_int("CHURNGUARD_JOBS_PER_SESSION", 2 if PUBLIC else 20)
MAX_UPLOAD_MB = _env_int("CHURNGUARD_MAX_UPLOAD_MB", 10 if PUBLIC else 30)
MAX_ROWS = _env_int("CHURNGUARD_MAX_ROWS", 200_000 if PUBLIC else 2_000_000)
RATE_ANALYZE = _env_int("CHURNGUARD_RATE_ANALYZE", 12 if PUBLIC else 0)         # 每 IP 每小時；0 = 不限
RATE_ASK = _env_int("CHURNGUARD_RATE_ASK", 30 if PUBLIC else 0)
ASK_PER_JOB = _env_int("CHURNGUARD_ASK_PER_JOB", 10 if PUBLIC else 100)
MAX_CONCURRENT = _env_int("CHURNGUARD_MAX_CONCURRENT", 2)

if PUBLIC:
    os.environ.setdefault("CHURNGUARD_URL_ALLOWLIST",
                          "docs.google.com,googleusercontent.com,raw.githubusercontent.com,github.com")

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024, SECRET_KEY=os.environ.get("CHURNGUARD_SECRET") or secrets.token_hex(32),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=HTTPS)
if os.environ.get("CHURNGUARD_TRUST_PROXY") == "1":     # 在反向代理／雲端平台後面時必開，否則限流會把所有人當同一個 IP
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

JOBS: dict = {}
_jobs_lock = threading.Lock()
_hits: dict = defaultdict(deque)
_hits_lock = threading.Lock()
_SEM = threading.BoundedSemaphore(MAX_CONCURRENT)

SEG = {"healthy": ("健康", "ok"), "at_risk": ("預警", "warn"), "dormant": ("沉睡", "bad"), "lost": ("已流失", "mute")}
NUM_PARAMS = {  # 欄位 → (型別, 最小, 最大)
    "risk_threshold": (float, 0.05, 0.99), "dormant_days": (int, 14, 720), "lost_days": (int, 30, 3650),
    "horizon_days": (int, 14, 180), "daily_budget": (float, 0, 1e9), "cooldown_days": (int, 0, 365),
    "max_messages_per_run": (int, 1, 100000),
}


# ───────────────────────── 工具 ─────────────────────────
@app.template_filter("num")
def _num(v, nd=0):
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return v


@app.template_filter("pct")
def _pct(v, nd=0):
    try:
        return f"{float(v) * 100:.{nd}f}%"
    except (TypeError, ValueError):
        return v


def _ai_ok():
    return ENABLE_AI and bool(os.environ.get("ANTHROPIC_API_KEY"))


@app.context_processor
def _ctx():
    return dict(PUBLIC=PUBLIC, CONTACT=CONTACT, TTL_MIN=TTL // 60, URL_ENABLED=ENABLE_URL,
                PASSWORD_ON=bool(PASSWORD), MAX_UPLOAD_MB=MAX_UPLOAD_MB, MAX_ROWS=MAX_ROWS, RATE_ANALYZE=RATE_ANALYZE)


def _limited(bucket, limit, window=3600):
    if not limit:
        return False
    key, now = (bucket, request.remote_addr), time.time()
    with _hits_lock:
        q = _hits[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


def limited(bucket, limit, api=False):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            if _limited(bucket, limit):
                msg = "操作過於頻繁，請稍後再試。"
                return (jsonify(error=msg), 429) if api else (render_template("index.html", error=msg), 429)
            return fn(*a, **k)
        return wrapper
    return deco


def _purge():
    now = time.time()
    with _jobs_lock:
        for k in [k for k, j in JOBS.items() if now - j["last"] > TTL]:
            JOBS.pop(k, None)
    if len(_hits) > 5000:
        with _hits_lock:
            for k in [k for k, q in _hits.items() if not q or q[-1] < now - 3600]:
                _hits.pop(k, None)


def _web_cfg():
    cfg = campaign.load_config()
    cfg["quiet_hours"] = [0, 0]          # 網頁分析只產生名單，不依時段延後
    cfg["daily_budget"], cfg["max_messages_per_run"] = 500.0, 1000
    return cfg


def cfg_from_form(form):
    cfg = _web_cfg()
    for key, (cast, lo, hi) in NUM_PARAMS.items():
        if form.get(key, "").strip():
            try:
                cfg[key] = min(hi, max(lo, cast(float(form[key]))))
            except ValueError:
                pass
    cfg["lost_days"] = max(cfg["lost_days"], cfg["dormant_days"] + 30)
    if form.get("brand", "").strip():
        cfg["brand"] = form["brand"].strip()[:30]
    for seg in ("at_risk", "dormant"):
        t, c = form.get(f"{seg}_text", "").strip(), form.get(f"{seg}_code", "").strip()
        if t:
            cfg["offers"][seg]["text"] = t[:60]
        if c:
            cfg["offers"][seg]["code"] = c[:20]
    return cfg


def _new_job(df, source, df_c=None):
    sid = session["sid"]
    with _jobs_lock:
        mine = sorted((k for k, j in JOBS.items() if j["sid"] == sid), key=lambda k: JOBS[k]["last"])
        while len(mine) >= JOBS_PER_SESSION:            # 每位訪客只保留最近幾份
            JOBS.pop(mine.pop(0), None)
        while len(JOBS) >= MAX_JOBS:
            if PUBLIC:
                raise ingest.DataError("目前使用人數較多，請幾分鐘後再試。")
            JOBS.pop(min(JOBS, key=lambda k: JOBS[k]["last"]))
        jid = secrets.token_urlsafe(12)
        JOBS[jid] = {"df": df, "df_c": df_c, "source": source, "mapping": ingest.detect_mapping(df), "sid": sid,
                     "last": time.time(), "result": None, "report": None, "cfg": _web_cfg(), "asks": 0}
    return jid


def _job(jid):
    j = JOBS.get(jid)
    if not j or j["sid"] != session.get("sid") or time.time() - j["last"] > TTL:
        abort(404, "找不到這份分析（可能已過期或已刪除），請重新上傳資料。")
    j["last"] = time.time()
    return j


def _execute(job, form):
    cfg = cfg_from_form(form)
    if any(k.startswith("map_") for k in form):
        job["mapping"] = {f: (form.get("map_" + f) or None) for f in ingest.FIELDS}
    if not _SEM.acquire(timeout=20):
        raise ingest.DataError("目前使用人數較多，請稍後再試。")
    try:
        orders, customers, rep = ingest.prepare(job["df"], job["mapping"], cfg["default_channel"])
        if job["df_c"] is not None:
            customers = ingest.merge_customers(customers, ingest.prepare_customers(job["df_c"], cfg["default_channel"]))
        job.update(result=analysis.run(orders, customers, cfg), report=rep, cfg=cfg)
    except ingest.DataError:
        raise
    except Exception:  # noqa
        app.logger.exception("analysis failed")
        raise ingest.DataError("資料已讀取，但分析時發生錯誤。請確認日期與金額欄位的內容是否正確，或在欄位對應頁調整後重試。")
    finally:
        _SEM.release()


def _render_map(jid, job, error=None):
    df = job["df"]
    return render_template("map.html", jid=jid, job=job, columns=list(df.columns), fields=ingest.FIELDS,
                           labels=ingest.FIELD_LABELS, required=ingest.REQUIRED, error=error,
                           preview=df.head(8).fillna("").astype(str).values.tolist(), n_rows=len(df))


def data_sample():
    cust, ords = data.generate(900, seed=7)
    m = ords.merge(cust[["customer_id", "name", "channel"]], on="customer_id")
    m["order_date"] = m.order_date.dt.strftime("%Y-%m-%d")
    m = m.rename(columns={"customer_id": "會員編號", "name": "姓名", "order_date": "訂單日期",
                          "amount": "訂單金額", "category": "商品類別", "channel": "聯絡管道"})
    return m[["會員編號", "姓名", "訂單日期", "訂單金額", "商品類別", "聯絡管道"]].astype(object)


# ───────────────────────── 安全：登入、標頭 ─────────────────────────
@app.before_request
def _gate():
    _purge()
    session.setdefault("sid", secrets.token_hex(12))
    if request.endpoint in ("healthz", "login", "static") or not PASSWORD:
        return
    if not session.get("auth"):
        return redirect(url_for("login"))


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                               "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    if request.path.startswith(("/job", "/start", "/login")):
        resp.headers["Cache-Control"] = "no-store"
    if HTTPS:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.get("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if _limited("login", 10, 600):
            return render_template("login.html", error="嘗試次數過多，請 10 分鐘後再試。"), 429
        if hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
            session.clear()
            session["auth"], session["sid"] = True, secrets.token_hex(12)
            return redirect(url_for("index"))
        error = "密碼不正確。"
    return render_template("login.html", error=error), (401 if error else 200)


@app.post("/logout")
def logout():
    sid = session.get("sid")
    with _jobs_lock:
        for k in [k for k, j in JOBS.items() if j["sid"] == sid]:
            JOBS.pop(k, None)
    session.clear()
    return redirect(url_for("index"))


# ───────────────────────── 頁面 ─────────────────────────
@app.get("/")
def index():
    return render_template("index.html", error=None, notice=request.args.get("notice"))


@app.get("/privacy")
def privacy():
    return render_template("privacy.html")


@app.post("/start")
@limited("analyze", RATE_ANALYZE)
def start():
    try:
        sample = bool(request.form.get("sample"))
        if PUBLIC and not sample and not request.form.get("agree"):
            raise ingest.DataError("請先勾選確認：你有權處理這份資料，且已取得顧客同意或資料已去識別化。")
        df_c = None
        if sample:
            df, source = data_sample(), "範例資料（模擬）"
        else:
            df, source = ingest.load_source(request.files.get("file"), request.form.get("text", ""),
                                            request.form.get("url", "") if ENABLE_URL else "")
            cf = request.files.get("customers_file")
            df_c = ingest.read_table_bytes(cf.read(), cf.filename) if cf and cf.filename else None
        if len(df) > MAX_ROWS:
            raise ingest.DataError(f"資料超過 {MAX_ROWS:,} 列的上限，請先篩選（例如只留近 12–18 個月）再上傳。")
        jid = _new_job(df, source, df_c)
        job = JOBS[jid]
        if ingest.missing_required(job["mapping"], df):
            return redirect(url_for("mapping", jid=jid))
        try:
            _execute(job, {})
        except ingest.DataError:
            return redirect(url_for("mapping", jid=jid))
        return redirect(url_for("result", jid=jid))
    except ingest.DataError as e:
        return render_template("index.html", error=str(e)), 400


@app.get("/sample.csv")
def sample_csv():
    buf = io.StringIO()
    data_sample().head(300).to_csv(buf, index=False)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sample_orders.csv"})


@app.get("/job/<jid>/map")
def mapping(jid):
    return _render_map(jid, _job(jid))


@app.post("/job/<jid>/run")
@limited("analyze", RATE_ANALYZE)
def run(jid):
    job = _job(jid)
    try:
        _execute(job, request.form)
    except ingest.DataError as e:
        return _render_map(jid, job, str(e)), 400
    return redirect(url_for("result", jid=jid))


@app.get("/job/<jid>")
def result(jid):
    job = _job(jid)
    if not job["result"]:
        return redirect(url_for("mapping", jid=jid))
    res = job["result"]
    cols = ["customer_id", "name", "segment", "churn_prob", "recency", "reason", "channel", "message", "unit_cost"]
    return render_template("result.html", jid=jid, job=job, res=res, ins=res["insights"], m=res["metrics"],
                           cfg=job["cfg"], rep=job["report"], plan_rows=res["plan"][cols].head(300).to_dict("records"),
                           SEG=SEG, columns=list(job["df"].columns), fields=ingest.FIELDS, labels=ingest.FIELD_LABELS,
                           has_key=_ai_ok(), asks_left=max(0, ASK_PER_JOB - job["asks"]))


@app.post("/job/<jid>/delete")
def delete(jid):
    _job(jid)
    with _jobs_lock:
        JOBS.pop(jid, None)
    return redirect(url_for("index", notice="資料已從伺服器刪除。"))


def _safe_csv(df):
    """避免 Excel 把以 = + - @ 開頭的文字當成公式執行。"""
    df = df.copy()
    for c in df.select_dtypes(include="object").columns:
        df[c] = df[c].map(lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v)
    return df


@app.get("/job/<jid>/<which>.csv")
def download(jid, which):
    res = _job(jid)["result"]
    if not res or which not in ("plan", "scored"):
        abort(404)
    df = res["plan"] if which == "plan" else res["scored"]
    buf = io.StringIO()
    _safe_csv(df).to_csv(buf, index=False)
    name = "campaign_plan" if which == "plan" else "scored_customers"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})


@app.post("/job/<jid>/ask")
@limited("ask", RATE_ASK, api=True)
def ask(jid):
    job = _job(jid)
    q = (request.get_json(silent=True) or {}).get("question", "").strip()[:500]
    if not q or not job["result"]:
        return jsonify(error="請輸入問題。"), 400
    if not ENABLE_AI:
        return jsonify(error="此網站未開放 Claude 問答。"), 503
    if job["asks"] >= ASK_PER_JOB:
        return jsonify(error=f"這份分析的提問次數已達上限（{ASK_PER_JOB} 次）。"), 429
    job["asks"] += 1
    answer, err = insights.ask_claude(q, job["result"]["insights"]["facts"], job["cfg"])
    return (jsonify(error=err), 503) if err else jsonify(answer=answer)


# ───────────────────────── 錯誤頁 ─────────────────────────
@app.errorhandler(404)
def _404(e):
    return render_template("index.html", error=getattr(e, "description", "找不到頁面。")), 404


@app.errorhandler(413)
def _413(e):
    return render_template("index.html", error=f"檔案太大（上限 {MAX_UPLOAD_MB}MB），請先篩選或拆分後再上傳。"), 413


@app.errorhandler(Exception)
def _500(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("unhandled error")
    return render_template("index.html", error="發生非預期的錯誤，請稍後再試。"), 500
