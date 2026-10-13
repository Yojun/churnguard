"""精準推播：名單篩選、成本控制、個人化訊息、頻率上限、發送（預設 dry-run）。"""
import json
import os
import urllib.request
from datetime import datetime

import numpy as np
import pandas as pd

DEFAULT_CONFIG = {
    "brand": "好咖啡",
    "horizon_days": 60,
    "dormant_days": 120,          # 超過幾天沒買視為沉睡
    "lost_days": 365,             # 超過幾天沒買視為已流失（不再主動推播）
    "default_channel": "line",    # 資料沒有聯絡管道欄位時使用
    "risk_threshold": 0.6,        # 流失機率達多少進入「預警」名單
    "cooldown_days": 14,          # 同一位顧客兩次推播的最短間隔
    "daily_budget": 300.0,        # 單次執行預算上限
    "cost_per_message": {"line": 0.5, "email": 0.05, "app": 0.0, "sms": 0.8},
    "max_messages_per_run": 500,
    "quiet_hours": [22, 9],       # 22:00–09:00 不發送
    "vip_percentile": 0.8,        # 累積消費前 20% 視為 VIP
    "offers": {
        "at_risk": {"text": "專屬 9 折券", "code": "STAY10", "valid_days": 7},
        "dormant": {"text": "回娘家 85 折 + 免運", "code": "BACK15", "valid_days": 14},
        "vip_bonus": {"text": "再加贈指定商品一份"},
    },
    "templates": {
        "at_risk": "{name}，好一陣子沒見到您了！我們替您準備了{offer}（代碼 {code}，{valid_days} 天內有效），想念您喜歡的{category}～",
        "dormant": "{name}，{brand}很想念您！回來看看新品吧，限時{offer}（代碼 {code}，{valid_days} 天內有效），{category}也補貨囉！",
    },
    "channel": "console",         # console | line | webhook
    "webhook_url": "",
    "use_llm": False,             # 設 True 且有 ANTHROPIC_API_KEY 時，用 Claude 改寫成更自然的訊息
    "llm_model": "claude-sonnet-5-5",
}


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))
    return cfg


def _merge(base, new):
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _in_quiet_hours(now_dt, quiet):
    s, e = quiet
    h = now_dt.hour
    return (h >= s or h < e) if s > e else (s <= h < e)


def _render(row, cfg):
    offer = dict(cfg["offers"][row.segment])
    text = offer["text"]
    if row.monetary_rank >= cfg["vip_percentile"]:
        text += "，" + cfg["offers"]["vip_bonus"]["text"]
    return cfg["templates"][row.segment].format(
        name=row["name"], brand=cfg["brand"], offer=text, code=offer["code"],
        valid_days=offer["valid_days"], category=row.fav_category)


def _llm_rewrite(text, row, cfg):
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=cfg["llm_model"], max_tokens=300,
            messages=[{"role": "user", "content":
                       "請把下列行銷訊息改寫成親切、自然、60 字內的繁體中文推播，保留優惠內容與代碼，"
                       f"不要加引號或多餘說明。\n顧客偏好：{row.fav_category}\n原文：{text}"}])
        return msg.content[0].text.strip()
    except Exception:
        return text


def plan(scored, cfg, send_log=None, now=None):
    """依機率 × 客單價排序，在預算與頻率上限內選出推播名單。"""
    now_dt = now or datetime.now()
    df = scored[scored.segment.isin(["at_risk", "dormant"])].copy()

    # 冷卻期：近期已推播過的不再打擾
    if send_log is not None and len(send_log):
        recent = send_log[pd.to_datetime(send_log.sent_at) > pd.Timestamp(now_dt) - pd.Timedelta(days=cfg["cooldown_days"])]
        df = df[~df.customer_id.astype(str).isin(recent.customer_id.astype(str))]

    # 優先順序 = 預期保住的營收（預警用流失機率；沉睡客依沉睡時間遞減）
    win = np.where(df.segment == "at_risk", df.churn_prob, np.maximum(0.1, 1 - df.recency / 365) * 0.5)
    df["priority"] = win * df.avg_amount
    df["unit_cost"] = df.channel.map(cfg["cost_per_message"]).fillna(0.5)
    df = df.sort_values("priority", ascending=False)

    df["cum_cost"] = df.unit_cost.cumsum()
    df = df[df.cum_cost <= cfg["daily_budget"]].head(cfg["max_messages_per_run"]).copy()

    df["message"] = [_render(r, cfg) for _, r in df.iterrows()]
    if cfg["use_llm"] and os.environ.get("ANTHROPIC_API_KEY"):
        df["message"] = [_llm_rewrite(m, r, cfg) for m, (_, r) in zip(df.message, df.iterrows())]

    df["status"] = "planned"
    if _in_quiet_hours(now_dt, cfg["quiet_hours"]):
        df["status"] = "deferred_quiet_hours"
    return df


def _post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def dispatch(plan_df, cfg, send=False, log_path="out/send_log.csv", now=None):
    """send=False 為 dry-run（只寫紀錄不真的發）。"""
    now_dt = now or datetime.now()
    rows = []
    for _, r in plan_df.iterrows():
        status = r.status
        if status == "planned":
            if not send:
                status = "dry_run"
            else:
                try:
                    if cfg["channel"] == "line":
                        _post("https://api.line.me/v2/bot/message/push",
                              {"to": r.line_user_id, "messages": [{"type": "text", "text": r.message}]},
                              {"Content-Type": "application/json",
                               "Authorization": "Bearer " + os.environ["LINE_CHANNEL_TOKEN"]})
                    elif cfg["channel"] == "webhook":
                        _post(cfg["webhook_url"], {"customer_id": str(r.customer_id), "message": r.message},
                              {"Content-Type": "application/json"})
                    else:
                        print(f"[push → {r['name']}] {r.message}")
                    status = "sent"
                except Exception as e:  # noqa
                    status = f"failed:{type(e).__name__}"
        rows.append((str(r.customer_id), now_dt.isoformat(timespec="seconds"), cfg["channel"], r.segment, status))

    log = pd.DataFrame(rows, columns=["customer_id", "sent_at", "channel", "segment", "status"])
    if len(log):
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        # 只有真的發出（或 dry-run 以外）才算冷卻；dry-run 不寫入冷卻紀錄
        to_save = log[log.status == "sent"]
        if len(to_save):
            to_save.to_csv(log_path, mode="a", header=not os.path.exists(log_path), index=False)
    return log
