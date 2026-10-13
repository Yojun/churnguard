"""模擬顧客與訂單資料（沒有真實資料時用來示範；正式使用請改讀自己的 CSV）。

訂單 CSV 欄位：customer_id, order_date, amount, category
顧客 CSV 欄位：customer_id, name, channel, line_user_id（其餘欄位可自由新增）
"""
import numpy as np
import pandas as pd

SURNAMES = list("陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴徐")
GIVENS = ["怡君", "冠宇", "雅婷", "承恩", "宜庭", "志明", "佳穎", "宗翰", "欣怡", "建宏", "家豪", "品妤"]
CATEGORIES = ["咖啡豆", "茶飲", "甜點", "周邊商品", "禮盒"]


def generate(n_customers=2000, seed=42, end=None):
    rng = np.random.default_rng(seed)
    end = pd.Timestamp(end or pd.Timestamp.today()).normalize()
    start = end - pd.Timedelta(days=540)
    customers, orders = [], []

    for cid in range(1, n_customers + 1):
        join = start + pd.Timedelta(days=int(rng.integers(0, 500)))
        gap = rng.gamma(4, 6) + 4                    # 平均購買間隔（天）
        avg_amt = float(rng.lognormal(5.8, 0.5))      # 平均客單價
        churn_at = None
        if rng.random() < 0.5:                        # 約一半會流失
            churn_at = join + pd.Timedelta(days=int(rng.integers(30, 400)))
        cat = rng.choice(CATEGORIES)

        t = join
        while t <= end:
            amt = max(50.0, rng.normal(avg_amt, avg_amt * 0.25))
            c = cat if rng.random() < 0.7 else rng.choice(CATEGORIES)
            orders.append((cid, t.normalize(), round(amt), c))
            g = rng.exponential(gap) + 1
            if churn_at is not None:                  # 流失前購買間隔逐漸拉長（可被模型學到的訊號）
                lead = (t - (churn_at - pd.Timedelta(days=75))).days
                if lead > 0:
                    g *= 1 + lead / 25
            t = t + pd.Timedelta(days=float(g))
            if churn_at is not None and t > churn_at:
                break

        customers.append((cid, rng.choice(SURNAMES) + rng.choice(GIVENS),
                          rng.choice(["line", "email", "app"], p=[0.6, 0.25, 0.15]),
                          f"U{cid:08d}", join.normalize()))

    cust = pd.DataFrame(customers, columns=["customer_id", "name", "channel", "line_user_id", "join_date"])
    ords = pd.DataFrame(orders, columns=["customer_id", "order_date", "amount", "category"])
    return cust, ords


def load_csv(customers_path, orders_path):
    cust = pd.read_csv(customers_path)
    ords = pd.read_csv(orders_path, parse_dates=["order_date"])
    return cust, ords
