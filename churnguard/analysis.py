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


SUMMARY_NOTE = ("這是會員摘要資料（每位顧客一列），沒有逐筆訂單，因此使用規則式評分：依「未購買天數 ÷ 平常購買間隔」判斷風險。"
                "若能提供逐筆訂單，系統會用機器學習模型，通常更準。")


def run_summary(features, customers, cfg, asof):
    asof = pd.Timestamp(asof).normalize()
    metrics = mdl._rule_metrics(SUMMARY_NOTE)
    scored = mdl.score_features(mdl.HeuristicModel(), features, customers, cfg["risk_threshold"],
                                cfg["dormant_days"], cfg.get("lost_days", 365))
    n_candidates = int(scored.segment.isin(["at_risk", "dormant"]).sum())
    plan_df = campaign.plan(scored, cfg, None)
    ins = insights.build(scored, plan_df, n_candidates, None, metrics, cfg, asof)
    return {"now": asof, "scored": scored, "plan": plan_df, "metrics": metrics, "insights": ins,
            "n_orders": int(features.frequency.sum())}
