"""一次完成：訓練 → 評分 → 挑選推播名單 → 洞察。網頁與 CLI 共用。"""
import pandas as pd

from . import campaign, insights, model as mdl


def run(orders, customers, cfg, now=None):
    now = pd.Timestamp(now).normalize() if now is not None else orders.order_date.max().normalize()
    orders = orders[orders.order_date <= now]
    m, metrics = mdl.train(orders, now, cfg["horizon_days"], cfg["dormant_days"])
    scored = mdl.score(m, orders, customers, now, cfg["risk_threshold"], cfg["dormant_days"], cfg.get("lost_days", 365))
    n_candidates = int(scored.segment.isin(["at_risk", "dormant"]).sum())
    plan_df = campaign.plan(scored, cfg, None)
    ins = insights.build(scored, plan_df, n_candidates, orders, metrics, cfg, now)
    return {"now": now, "scored": scored, "plan": plan_df, "metrics": metrics, "insights": ins, "n_orders": int(len(orders))}
