"""產生 HTML 報表。"""
import html
import os


def write_report(scored, plan_df, metrics, cfg, path="out/report.html"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    seg = scored.segment.value_counts()
    total = len(scored)
    exp_cost = plan_df.unit_cost.sum() if len(plan_df) else 0
    at_value = (plan_df.priority.sum() if len(plan_df) else 0)

    def card(title, val, sub=""):
        return f'<div class="c"><div class="t">{title}</div><div class="v">{val}</div><div class="s">{sub}</div></div>'

    rows = ""
    for _, r in plan_df.head(40).iterrows():
        tag = "預警" if r.segment == "at_risk" else "沉睡"
        rows += (f"<tr><td>{html.escape(str(r.customer_id))}</td><td>{html.escape(str(r['name']))}</td><td>{tag}</td>"
                 f"<td>{r.churn_prob:.0%}</td><td>{int(r.recency)} 天</td><td>{html.escape(r.reason)}</td>"
                 f"<td>{html.escape(r.message)}</td></tr>")

    doc = f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>流失預警與喚醒報表</title>
<style>body{{font-family:system-ui,"Noto Sans TC",sans-serif;margin:24px;color:#222}}
.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.c{{border:1px solid #ddd;border-radius:10px;padding:12px}}.t{{color:#666;font-size:13px}}
.v{{font-size:26px;font-weight:600}}.s{{color:#888;font-size:12px}}
table{{border-collapse:collapse;width:100%;margin-top:16px;font-size:13px}}
td,th{{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}}</style>
<h1>顧客流失預警與喚醒報表</h1>
<div class="g">
{card("顧客總數", total)}
{card("預警（尚未流失）", seg.get("at_risk", 0), f"機率 ≥ {cfg['risk_threshold']:.0%}")}
{card("沉睡客", seg.get("dormant", 0), f"≥ {cfg['dormant_days']} 天未購買")}
{card("本次推播名單", len(plan_df), f"預估成本 {exp_cost:.1f}／預算 {cfg['daily_budget']}")}
{card("預期保住營收", f"{at_value:,.0f}", "機率 × 客單價（估算）")}
{card("模型 AUC", metrics['auc'] if metrics.get('auc') is not None else "規則式", (f"PR-AUC {metrics['pr_auc']}，基準流失率 {metrics['base_churn_rate']:.0%}") if metrics.get('auc') is not None else html.escape(metrics.get('note', '')))}
</div>
<h2>推播名單（前 40 筆，依預期營收排序）</h2>
<table><tr><th>ID</th><th>姓名</th><th>分群</th><th>流失機率</th><th>未購買</th><th>原因</th><th>個人化訊息</th></tr>{rows}</table>
</html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
