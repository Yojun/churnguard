"""把分析結果變成「結論 + 建議」，並可選擇讓 Claude 回答自訂問題（只傳彙總數字，不含個資）。"""
import json
import os

import numpy as np
import pandas as pd

SEG_LABEL = {"healthy": "健康", "at_risk": "預警", "dormant": "沉睡", "lost": "已流失"}


def build(scored, plan, n_candidates, orders, metrics, cfg, now):
    n = len(scored)
    seg = {k: 0 for k in SEG_LABEL}
    seg.update({k: int(v) for k, v in scored.segment.value_counts().items()})
    total_rev = float(orders.amount.sum()) if orders is not None else float(scored.monetary.sum())
    seg_rev = {k: float(v) for k, v in scored.groupby("segment").monetary.sum().items()}

    ar = scored[scored.segment == "at_risk"]
    monthly_risk = float((ar.churn_prob * ar.avg_amount * 30 / ar.avg_gap.clip(lower=3)).sum()) if len(ar) else 0.0
    vip_ar = ar[ar.monetary_rank >= cfg["vip_percentile"]]
    risky = scored[scored.segment.isin(["at_risk", "dormant"])]
    risky_share = float(risky.monetary.sum() / total_rev) if total_rev else 0.0

    act = scored[scored.segment.isin(["healthy", "at_risk"])]
    counts, _ = np.histogram(act.churn_prob, bins=10, range=(0, 1)) if len(act) else (np.zeros(10, int), None)
    hist = [{"label": f"{i * 10}–{i * 10 + 10}%", "count": int(c)} for i, c in enumerate(counts)]

    monthly, trend_change = _monthly(orders, now) if orders is not None else ([], None)

    cat = (scored.assign(risky=scored.segment.isin(["at_risk", "dormant"]))
           .groupby("fav_category").agg(n=("customer_id", "size"), risky=("risky", "sum")))
    cat = cat[cat.n >= 5].assign(share=lambda d: d.risky / d.n).sort_values("share", ascending=False).head(6)
    categories = [{"name": str(i), "n": int(r.n), "risky": int(r.risky), "share": float(r.share)} for i, r in cat.iterrows()]

    maxv = max([v for _, v in metrics.get("importance", [])] or [1])
    drivers = [{"label": k, "pct": max(4, int(v / maxv * 100))} for k, v in metrics.get("importance", [])]

    cost = float(plan.unit_cost.sum()) if len(plan) else 0.0
    coverage = {"planned": int(len(plan)), "candidates": int(n_candidates), "cost": cost,
                "budget": cfg["daily_budget"], "uncovered": int(n_candidates - len(plan))}

    recs = []
    if seg["at_risk"]:
        o = cfg["offers"]["at_risk"]
        recs.append({"level": "high", "title": f"優先挽回 {seg['at_risk']:,} 位預警顧客",
                     "text": f"他們還沒真正流失、挽回成本最低；預估每月約 {monthly_risk:,.0f} 的營收有風險。"
                             f"建議盡快以「{o['text']}」推播，{o['valid_days']} 天內有效。"})
    if len(vip_ar):
        recs.append({"level": "high", "title": f"{len(vip_ar):,} 位高價值顧客有流失風險",
                     "text": "他們屬於累積消費前段班，建議在自動推播之外加上專人關懷（電話或客服私訊）。"})
    if seg["dormant"]:
        recs.append({"level": "mid", "title": f"{seg['dormant']:,} 位沉睡顧客",
                     "text": f"已超過 {cfg['dormant_days']} 天未購買，回流率通常較低；建議用較強的誘因（{cfg['offers']['dormant']['text']}），"
                             "並限制發送次數，避免打擾。"})
    if coverage["uncovered"] > 0:
        recs.append({"level": "mid", "title": f"預算只涵蓋 {coverage['planned']:,} / {coverage['candidates']:,} 位",
                     "text": f"另有 {coverage['uncovered']:,} 位因超過預算或單次上限未列入。系統已依「預期保住營收」由高到低挑選；"
                             "若要全部觸及，可在下方設定調高預算或單次上限。"})
    if categories and categories[0]["share"] > 0 and len(categories) > 1 and categories[0]["share"] >= 1.3 * (len(risky) / n if n else 1):
        c = categories[0]
        recs.append({"level": "info", "title": f"「{c['name']}」客群風險偏高",
                     "text": f"偏好此品類的顧客有 {c['share']:.0%} 屬預警或沉睡，高於整體 {len(risky) / n:.0%}；可檢查該品類的缺貨、價格或新品。"})
    if trend_change is not None and trend_change <= -0.1:
        recs.append({"level": "high", "title": "活躍顧客數正在下滑",
                     "text": f"最近 3 個月的月均活躍顧客數比前 3 個月少 {abs(trend_change):.0%}，除了喚醒舊客，也要檢視拉新與回購機制。"})
    if metrics["method"] == "rule":
        pass        # 規則式的說明已在結果頁頂端以提示列顯示，不在建議裡重複
    elif metrics["auc"] is not None and metrics["auc"] < 0.65:
        recs.append({"level": "info", "title": "模型預測力偏低",
                     "text": f"AUC 只有 {metrics['auc']}，名單僅供排序參考；補充更多欄位（如商品、折扣、客服紀錄）通常能提升。"})
    if not recs:
        recs.append({"level": "info", "title": "目前沒有明顯風險", "text": "沒有顧客達到預警或沉睡條件，可維持現有經營並持續追蹤。"})

    scope = (f"{n:,} 位顧客、{len(orders):,} 筆訂單（{orders.order_date.min().date()} ～ {orders.order_date.max().date()}）"
             if orders is not None else f"{n:,} 位顧客（會員摘要資料，基準日 {now.date()}）")
    headline = (f"共分析 {scope}。"
                f"其中預警 {seg['at_risk']:,} 位、沉睡 {seg['dormant']:,} 位、已流失 {seg['lost']:,} 位；"
                f"風險顧客（預警＋沉睡）累積貢獻了 {risky_share:.0%} 的歷史營收。")

    facts = {
        "customers": n, "orders": int(len(orders)) if orders is not None else int(scored.frequency.sum()), "segments": seg,
        "revenue_by_segment": {SEG_LABEL[k]: round(v) for k, v in seg_rev.items()},
        "total_revenue": round(total_rev), "risky_revenue_share": round(risky_share, 3),
        "estimated_monthly_revenue_at_risk": round(monthly_risk),
        "vip_at_risk": int(len(vip_ar)),
        "model": {"method": metrics["method"], "auc": metrics["auc"], "base_churn_rate": metrics["base_churn_rate"]},
        "top_drivers": [d["label"] for d in drivers[:5]],
        "category_risk": [{"category": c["name"], "customers": c["n"], "risky_share": round(c["share"], 2)} for c in categories],
        "monthly_active_customers_last12": [{"month": x["label"], "customers": x["customers"], "revenue": round(x["revenue"])} for x in monthly],
        "campaign": {"planned": coverage["planned"], "candidates": coverage["candidates"], "cost": round(cost, 1), "budget": cfg["daily_budget"]},
        "thresholds": {"risk_threshold": cfg["risk_threshold"], "dormant_days": cfg["dormant_days"], "lost_days": cfg["lost_days"]},
    }
    return {"headline": headline, "seg": seg, "seg_rev": seg_rev, "total_rev": total_rev, "monthly_risk": monthly_risk,
            "risky_share": risky_share, "hist": hist, "monthly": monthly, "categories": categories, "drivers": drivers,
            "recs": recs, "coverage": coverage, "vip_at_risk": int(len(vip_ar)), "facts": facts}


def _monthly(orders, now):
    m = (orders.assign(month=orders.order_date.dt.to_period("M"))
         .groupby("month").agg(customers=("customer_id", "nunique"), revenue=("amount", "sum"), orders=("amount", "size")))
    monthly = [{"label": str(i), "customers": int(r.customers), "revenue": float(r.revenue), "orders": int(r.orders)}
               for i, r in m.tail(12).iterrows()]
    partial = now.day < now.days_in_month
    full = m.iloc[:-1] if partial and len(m) > 1 else m
    trend_change = None
    if len(full) >= 6:
        recent, prev = full.customers.tail(3).mean(), full.customers.iloc[-6:-3].mean()
        trend_change = float((recent - prev) / prev) if prev else None
    return monthly, trend_change


SYSTEM = ("你是資深的顧客留存分析師。只能依據提供的彙總資料回答，不要編造數字；若資料不足以回答，直接說明缺什麼。"
          "請用繁體中文，簡潔、可執行，必要時給出具體的下一步。")


def ask_claude(question, facts, cfg):
    """回傳 (答案, 錯誤訊息)。只傳送彙總統計，不含任何顧客個資。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "尚未設定 ANTHROPIC_API_KEY，無法使用 Claude 問答。請設定環境變數後重新啟動。"
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=cfg.get("llm_model", "claude-sonnet-5-5"), max_tokens=900, system=SYSTEM,
            messages=[{"role": "user", "content":
                       f"以下是這份顧客資料的彙總分析結果（JSON）：\n{json.dumps(facts, ensure_ascii=False)}\n\n問題：{question}"}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip(), None
    except Exception as e:  # noqa
        return None, f"呼叫 Claude 失敗：{type(e).__name__}"
