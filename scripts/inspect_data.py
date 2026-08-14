"""inspect_data.py

快速打印 H&M 电商数据库的关键指标(用于核对看板数字、写 README/简历)。

用法:
    python scripts/inspect_data.py
"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "ecommerce.db",
)


def show(title: str, rows: list[tuple], width: int = 64) -> None:
    print("=" * width)
    print(title)
    print("=" * width)
    for row in rows:
        print("  ", " | ".join(str(x) for x in row))
    print()


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    q = cur.execute

    show("总量(注意: 微软镜像价格已归一化, 只能看相对量)", q("""
        SELECT 'GMV 合计(相对值)', printf('%.4f', SUM(price))
        FROM fact_transaction
        UNION ALL
        SELECT '售出件数', SUM(items)
        FROM monthly_metrics
        UNION ALL
        SELECT '下单客户数', COUNT(DISTINCT customer_id)
        FROM fact_transaction
    """))

    show("渠道 GMV 占比", q("""
        SELECT CASE channel WHEN 1 THEN '1=线上(Online)' WHEN 2 THEN '2=门店(Store)' END,
               printf('%.2f%%', 100.0 * SUM(gmv) / SUM(SUM(gmv)) OVER ())
        FROM daily_sales GROUP BY channel
    """))

    show("月度范围", q("""
        SELECT MIN(month_date), MAX(month_date),
               COUNT(DISTINCT month_date)
        FROM monthly_metrics
    """))

    show("最早3个月 vs 最近3个月(月合计)", q("""
        SELECT month_date, printf('%.4f', SUM(gmv)) AS gmv,
               SUM(items) AS items, SUM(customers) AS customers
        FROM monthly_metrics
        WHERE month_date IN (
            SELECT month_date FROM monthly_metrics
            GROUP BY month_date ORDER BY month_date LIMIT 3
        ) OR month_date IN (
            SELECT month_date FROM monthly_metrics
            GROUP BY month_date ORDER BY month_date DESC LIMIT 3
        )
        GROUP BY month_date ORDER BY month_date
    """))

    show("RFM 分层", q("""
        SELECT segment, COUNT(*) AS customers,
               printf('%.4f', SUM(monetary)) AS monetary
        FROM rfm GROUP BY segment ORDER BY monetary DESC
    """))

    show("用户分层占比", q("""
        SELECT segment, COUNT(*) AS customers,
               printf('%.2f%%', 100.0 * COUNT(*) / SUM(COUNT(*)) OVER ())
        FROM rfm GROUP BY segment ORDER BY customers DESC
    """))

    show("留存概览(25个 cohort)", q("""
        SELECT printf('%.2f%%', MIN(retention_rate)),
               printf('%.2f%%', MAX(retention_rate)),
               printf('%.2f%%', AVG(retention_rate))
        FROM cohort_retention
    """))

    show("月度复购率概览", q("""
        SELECT printf('%.2f%%', MIN(r)),
               printf('%.2f%%', MAX(r)),
               printf('%.2f%%', AVG(r))
        FROM (SELECT month_date, AVG(repurchase_rate) AS r
              FROM monthly_metrics GROUP BY month_date)
    """))

    show("品类结构(Top 8)", q("""
        SELECT product_group_name, COUNT(*) AS articles
        FROM dim_article GROUP BY product_group_name
        ORDER BY COUNT(*) DESC LIMIT 8
    """))

    conn.close()


if __name__ == "__main__":
    main()
