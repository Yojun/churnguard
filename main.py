"""ChurnGuard：AI 流失預測 + 自動化喚醒推播。

用法：
  python main.py serve [--port 8000]        # 開啟網頁介面：上傳檔案／貼上資料／輸入連結後分析
  python main.py demo                       # 用模擬資料跑完整流程（不會真的發送）
  python main.py run --orders o.csv|URL [--customers c.csv] [--config config.json] [--send]
  python main.py init-config                # 產生可編輯的 config.json
"""
import argparse
import json
import os

import pandas as pd

from churnguard import analysis, campaign, data, ingest, report


def pipeline(orders, customers, cfg, send=False):
    res = analysis.run(orders, customers, cfg)
    scored, plan_df, metrics = res["scored"], res["plan"], res["metrics"]
    log_path = "out/send_log.csv"
    if os.path.exists(log_path):  # 有發送紀錄時，重新依冷卻期規劃
        plan_df = campaign.plan(scored, cfg, pd.read_csv(log_path))
    result = campaign.dispatch(plan_df, cfg, send=send, log_path=log_path)

    os.makedirs("out", exist_ok=True)
    scored.to_csv("out/scored_customers.csv", index=False, encoding="utf-8-sig")
    plan_df.to_csv("out/campaign_plan.csv", index=False, encoding="utf-8-sig")
    rpt = report.write_report(scored, plan_df, metrics, cfg)

    print("模型：", {k: metrics[k] for k in ("method", "auc", "pr_auc", "base_churn_rate")})
    print("分群：", scored.segment.value_counts().to_dict())
    print(res["insights"]["headline"])
    print(f"推播名單：{len(plan_df)} 筆，狀態：{result.status.value_counts().to_dict()}")
    print("報表：", rpt)
    return scored, plan_df


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    sub.add_parser("init-config")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    r = sub.add_parser("run")
    r.add_argument("--orders", required=True, help="CSV／Excel 路徑，或連結（含 Google 試算表）")
    r.add_argument("--customers", help="顧客名單（選填）：姓名、聯絡管道、LINE ID")
    r.add_argument("--config")
    r.add_argument("--send", action="store_true", help="真的發送（預設 dry-run）")
    args = ap.parse_args()

    if args.cmd == "init-config":
        with open("config.json", "w", encoding="utf-8") as fh:
            json.dump(campaign.DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)
        print("已建立 config.json")
    elif args.cmd == "serve":
        from churnguard.web import app
        print(f"開啟 http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=False)
    elif args.cmd == "demo":
        cust, ords = data.generate()
        os.makedirs("data", exist_ok=True)
        cust.to_csv("data/customers.csv", index=False, encoding="utf-8-sig")
        ords.to_csv("data/orders.csv", index=False)
        cust["customer_id"], ords["customer_id"] = cust.customer_id.astype(str), ords.customer_id.astype(str)
        pipeline(ords, cust, campaign.load_config("config.json"))
    else:
        cfg = campaign.load_config(args.config)
        if os.path.exists(args.orders):
            with open(args.orders, "rb") as fh:
                df = ingest.read_table_bytes(fh.read(), args.orders)
        else:
            df = ingest.fetch_url(args.orders)
        mapping = ingest.detect_mapping(df)
        print("欄位對應：", {k: v for k, v in mapping.items() if v})
        orders, cust, rep = ingest.prepare(df, mapping, cfg["default_channel"])
        for w in rep["warnings"]:
            print("注意：", w)
        if args.customers:
            with open(args.customers, "rb") as fh:
                cust = ingest.merge_customers(cust, ingest.prepare_customers(ingest.read_table_bytes(fh.read(), args.customers)))
        pipeline(orders, cust, cfg, send=args.send)


if __name__ == "__main__":
    main()
