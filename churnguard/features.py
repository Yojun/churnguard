"""行為與消費頻次特徵（RFM + 購買間隔 + 趨勢）。"""
import numpy as np
import pandas as pd

FEATURES = [
    "recency", "frequency", "monetary", "avg_amount", "avg_gap", "std_gap",
    "last_gap", "overdue_ratio", "freq_30", "freq_90", "freq_prev90", "trend", "tenure",
]

FEATURE_LABELS = {
    "recency": "距上次購買天數", "frequency": "購買次數", "monetary": "累積消費金額",
    "avg_amount": "平均客單價", "avg_gap": "平均購買間隔", "std_gap": "購買間隔波動",
    "last_gap": "最近一次購買間隔", "overdue_ratio": "逾期倍數（未購天數÷平常間隔）",
    "freq_30": "近 30 天購買次數", "freq_90": "近 90 天購買次數", "freq_prev90": "前 90 天購買次數",
    "trend": "購買頻率變化（近 90 天 − 前 90 天）", "tenure": "顧客年資",
}


def build_features(orders: pd.DataFrame, asof) -> pd.DataFrame:
    asof = pd.Timestamp(asof)
    o = orders[orders.order_date <= asof].sort_values(["customer_id", "order_date"]).copy()
    if "category" not in o.columns:
        o["category"] = "精選商品"
    o["gap"] = o.groupby("customer_id").order_date.diff().dt.days

    g = o.groupby("customer_id")
    f = g.agg(last_order=("order_date", "max"), first_order=("order_date", "min"),
              frequency=("order_date", "size"), monetary=("amount", "sum"),
              avg_amount=("amount", "mean"), avg_gap=("gap", "mean"), std_gap=("gap", "std"))
    f["last_gap"] = g["gap"].last()
    f["fav_category"] = g["category"].agg(lambda s: s.mode().iat[0])

    def count_between(lo, hi):
        m = o[(o.order_date > asof - pd.Timedelta(days=hi)) & (o.order_date <= asof - pd.Timedelta(days=lo))]
        return m.groupby("customer_id").size().reindex(f.index).fillna(0)

    f["freq_30"] = count_between(0, 30)
    f["freq_90"] = count_between(0, 90)
    f["freq_prev90"] = count_between(90, 180)
    f["trend"] = f["freq_90"] - f["freq_prev90"]

    f["recency"] = (asof - f["last_order"]).dt.days
    f["tenure"] = (asof - f["first_order"]).dt.days
    median_gap = f["avg_gap"].median()
    f["avg_gap"] = f["avg_gap"].fillna(median_gap if not np.isnan(median_gap) else 30.0)
    f["std_gap"] = f["std_gap"].fillna(0.0)
    f["last_gap"] = f["last_gap"].fillna(f["avg_gap"])
    f["overdue_ratio"] = f["recency"] / f["avg_gap"].clip(lower=1)
    return f
