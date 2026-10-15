"""流失預測：用「過去某時點的行為」預測「接下來 horizon 天是否沒有再購買」。

資料量或期間不足時，自動改用規則式評分（逾期倍數 + 頻率趨勢），並在結果頁標註。
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.model_selection import train_test_split

from .features import FEATURES, FEATURE_LABELS, build_features


class HeuristicModel:
    """規則式備援：越逾期、購買頻率越下滑，流失機率越高。"""

    def predict_proba(self, X):
        r = X["overdue_ratio"].to_numpy(dtype=float)
        t = X["trend"].to_numpy(dtype=float)
        z = 1.8 * (r - 1.8) - 0.15 * t
        p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
        return np.column_stack([1 - p, p])


def _rule_metrics(note):
    return {"method": "rule", "auc": None, "pr_auc": None, "base_churn_rate": None,
            "n_train": 0, "n_test": 0, "note": note, "importance": []}


def train(orders, now, horizon_days=60, dormant_days=120, seed=0):
    now = pd.Timestamp(now)
    span = (now - orders.order_date.min()).days
    if span < horizon_days + 120:
        return HeuristicModel(), _rule_metrics(
            f"資料期間只有 {span} 天，至少需要約 {horizon_days + 120} 天才能訓練預測模型，已改用規則式評分。")
    asof = now - pd.Timedelta(days=horizon_days)
    X = build_features(orders, asof)
    X = X[X.recency <= dormant_days]                  # 只拿「當時仍算活躍」的客人訓練
    if len(X) < 100:
        return HeuristicModel(), _rule_metrics(f"可供訓練的顧客只有 {len(X)} 位（需 ≥ 100），已改用規則式評分。")
    bought_later = orders[(orders.order_date > asof) & (orders.order_date <= now)].customer_id.unique()
    y = (~X.index.isin(bought_later)).astype(int)
    pos, neg = int(y.sum()), int(len(y) - y.sum())
    if min(pos, neg) < 20:
        return HeuristicModel(), _rule_metrics("流失／回購的樣本數太少（各需 ≥ 20），已改用規則式評分。")

    Xtr, Xte, ytr, yte = train_test_split(X[FEATURES], y, test_size=0.25, random_state=seed, stratify=y)
    model = HistGradientBoostingClassifier(max_depth=4, learning_rate=0.08, max_iter=200, random_state=seed)
    model.fit(Xtr, ytr)
    p = model.predict_proba(Xte)[:, 1]
    imp = permutation_importance(model, Xte, yte, scoring="roc_auc", n_repeats=5, random_state=seed)
    ranked = sorted(zip(FEATURES, imp.importances_mean), key=lambda t: -t[1])
    metrics = {
        "method": "ml",
        "auc": round(float(roc_auc_score(yte, p)), 3),
        "pr_auc": round(float(average_precision_score(yte, p)), 3),
        "base_churn_rate": round(float(np.mean(y)), 3),
        "n_train": int(len(Xtr)), "n_test": int(len(Xte)), "note": "",
        "importance": [(FEATURE_LABELS[k], float(v)) for k, v in ranked[:8] if v > 0],
    }
    return model, metrics


def _reason(r):
    if r.overdue_ratio >= 2:
        return f"已 {int(r.recency)} 天未購買，是平常間隔（約 {r.avg_gap:.0f} 天）的 {r.overdue_ratio:.1f} 倍"
    if r.trend <= -2:
        return "近 90 天購買次數明顯少於前一段期間"
    if r.last_gap > 1.5 * r.avg_gap:
        return "最近一次購買間隔比平常拉長"
    return "整體購買節奏轉慢"


def score(model, orders, customers, now, risk_threshold=0.6, dormant_days=120, lost_days=365):
    """每位顧客的流失機率、分群（healthy 健康 / at_risk 預警 / dormant 沉睡 / lost 已流失）與原因。"""
    return score_features(model, build_features(orders, now), customers, risk_threshold, dormant_days, lost_days)


def score_features(model, f, customers, risk_threshold=0.6, dormant_days=120, lost_days=365):
    """已有特徵表（逐筆訂單算出，或由會員摘要推算）時直接評分。"""
    f = f.copy()
    f["churn_prob"] = model.predict_proba(f[FEATURES])[:, 1]
    f["segment"] = np.select(
        [f.recency >= lost_days, f.recency >= dormant_days, f.churn_prob >= risk_threshold],
        ["lost", "dormant", "at_risk"], default="healthy")
    f["reason"] = f.apply(_reason, axis=1)
    f["monetary_rank"] = f["monetary"].rank(pct=True)
    out = f.reset_index().merge(customers, on="customer_id", how="left")
    for col, default in (("name", "親愛的顧客"), ("channel", "line"), ("line_user_id", "")):
        if col not in out:
            out[col] = default
        out[col] = out[col].fillna(default)
    return out
