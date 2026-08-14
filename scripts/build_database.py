"""构建 H&M 电商分析数据库(SQLite)。

输入: data/raw 下的三张 parquet 原始表
输出: data/ecommerce.db, 包含:
  - 维度表: dim_customer, dim_article
  - 事实表: fact_transaction(带索引)
  - 派生分析表: daily_sales, monthly_metrics, rfm, cohort_retention

用法:
    python scripts/build_database.py              # 全量构建
    python scripts/build_database.py --sample 3000000  # 仅取前 N 条交易(轻量演示)
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"

# H&M 数据最后交易日期(用于计算 Recency)
LAST_DATE = "2020-09-22"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_dim_tables(con: sqlite3.Connection) -> None:
    log("写入 dim_article ...")
    articles = pd.read_parquet(RAW_DIR / "articles.parquet")
    # 只保留对分析有用的列,减少体积
    keep = [
        "article_id", "product_code", "prod_name", "product_type_name",
        "product_group_name", "graphical_appearance_name", "colour_group_name",
        "perceived_colour_value_name", "perceived_colour_master_name",
        "department_name", "index_name", "index_group_name", "section_name",
        "garment_group_name", "detail_desc",
    ]
    articles = articles[keep].fillna({"detail_desc": ""})
    articles.to_sql("dim_article", con, if_exists="replace", index=False)

    log("写入 dim_customer ...")
    customers = pd.read_parquet(RAW_DIR / "customers.parquet")
    customers = customers.fillna({"FN": 0, "Active": 0})
    customers["age"] = customers["age"].fillna(-1).astype(int)
    customers.loc[customers["age"] < 0, "age"] = None
    customers.to_sql("dim_customer", con, if_exists="replace", index=False)


def build_fact_table(con: sqlite3.Connection, sample: int) -> None:
    log("写入 fact_transaction(流式读取,避免内存峰值)...")
    src = pq.ParquetFile(RAW_DIR / "transactions_train.parquet")
    total_rows = src.metadata.num_rows
    if sample and sample < total_rows:
        log(f"使用采样模式: 前 {sample:,} 行 / 共 {total_rows:,} 行")
    else:
        sample = total_rows

    con.execute("DROP TABLE IF EXISTS fact_transaction")
    con.execute(
        """
        CREATE TABLE fact_transaction (
            t_dat TEXT,
            customer_id TEXT,
            article_id TEXT,
            price REAL,
            sales_channel_id INTEGER
        )
        """
    )
    con.commit()

    inserted = 0
    first = True
    for batch in src.iter_batches(batch_size=1_000_000):
        if inserted >= sample:
            break
        df = batch.to_pandas()
        df = df.head(sample - inserted)
        df["t_dat"] = pd.to_datetime(df["t_dat"]).dt.strftime("%Y-%m-%d")
        df.to_sql(
            "fact_transaction",
            con,
            if_exists="append",
            index=False,
            chunksize=100_000,
        )
        inserted += len(df)
        log(f"  已写入 {inserted:,} 行")

    log("创建索引 ...")
    con.executescript(
        """
        CREATE INDEX idx_fact_date ON fact_transaction(t_dat);
        CREATE INDEX idx_fact_customer ON fact_transaction(customer_id);
        CREATE INDEX idx_fact_article ON fact_transaction(article_id);
        """
    )
    con.commit()


def build_derived_tables(con: sqlite3.Connection) -> None:
    log("构建 daily_sales(日维度销售)...")
    con.execute("DROP TABLE IF EXISTS daily_sales")
    con.execute(
        """
        CREATE TABLE daily_sales AS
        SELECT t_dat AS date,
               sales_channel_id AS channel,
               ROUND(SUM(price), 2) AS gmv,
               COUNT(*) AS items,
               COUNT(DISTINCT customer_id) AS customers,
               COUNT(DISTINCT article_id) AS articles,
               ROUND(AVG(price), 2) AS avg_price
        FROM fact_transaction
        GROUP BY t_dat, sales_channel_id
        """
    )

    log("构建 monthly_metrics(月度指标,含复购率)...")
    con.execute("DROP TABLE IF EXISTS monthly_metrics")
    con.execute(
        """
        CREATE TABLE monthly_metrics AS
        WITH per_customer AS (
            SELECT strftime('%Y-%m', t_dat) AS ym,
                   sales_channel_id AS channel,
                   customer_id,
                   COUNT(*) AS n,
                   SUM(price) AS customer_gmv
            FROM fact_transaction
            GROUP BY ym, channel, customer_id
        )
        SELECT ym AS month,
               channel,
               ROUND(SUM(customer_gmv), 2) AS gmv,
               SUM(n) AS items,
               COUNT(*) AS customers,
               SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END) AS repeat_customers,
               ROUND(
                   100.0 * SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END) / COUNT(*),
                   2
               ) AS repurchase_rate
        FROM per_customer
        GROUP BY ym, channel
        """
    )
    con.commit()

    log("构建 rfm(用户价值分层)...")
    con.execute(
        """
        CREATE TABLE rfm AS
        SELECT customer_id,
               ROUND(julianday(?) - julianday(MAX(t_dat)), 1) AS recency_days,
               COUNT(*) AS frequency,
               ROUND(SUM(price), 2) AS monetary
        FROM fact_transaction
        GROUP BY customer_id
        """,
        (LAST_DATE,),
    )
    rfm = pd.read_sql("SELECT customer_id, recency_days, frequency, monetary FROM rfm", con)
    rfm = rfm[rfm["frequency"] > 0]

    def score_series(s: pd.Series, reverse: bool = False) -> pd.Series:
        q1, q2, q3 = s.quantile([0.25, 0.5, 0.75])

        def to_score(x: float) -> int:
            if x <= q1:
                return 1
            if x <= q2:
                return 2
            if x <= q3:
                return 3
            return 4

        scores = s.apply(to_score).astype(int)
        return 5 - scores if reverse else scores

    rfm["r_score"] = score_series(rfm["recency_days"], reverse=True)
    rfm["f_score"] = score_series(rfm["frequency"])
    rfm["m_score"] = score_series(rfm["monetary"])
    rfm["rfm_score"] = (
        rfm["r_score"].astype(str) + rfm["f_score"].astype(str) + rfm["m_score"].astype(str)
    )
    total = rfm["r_score"] + rfm["f_score"] + rfm["m_score"]
    rfm["segment"] = pd.cut(
        total,
        bins=[0, 4, 7, 10, 12],
        labels=["流失风险", "一般用户", "潜力用户", "高价值"],
        include_lowest=True,
    ).astype(str)
    con.execute("DROP TABLE IF EXISTS rfm")
    rfm.to_sql("rfm", con, if_exists="replace", index=False)
    con.commit()

    log("构建 cohort_retention(月度留存)...")
    con.execute("DROP TABLE IF EXISTS _activity")
    con.execute("DROP TABLE IF EXISTS cohort_retention")
    con.execute(
        """
        CREATE TABLE _activity AS
        SELECT DISTINCT customer_id, strftime('%Y-%m', t_dat) AS ym
        FROM fact_transaction
        """
    )
    con.execute(
        """
        CREATE TABLE cohort_retention AS
        WITH first_purchase AS (
            SELECT customer_id, MIN(ym) AS cohort_ym
            FROM _activity GROUP BY customer_id
        ),
        monthly AS (
            SELECT fp.cohort_ym, a.ym,
                   (CAST(substr(a.ym, 1, 4) AS INTEGER) * 12 + CAST(substr(a.ym, 6, 2) AS INTEGER))
                   - (CAST(substr(fp.cohort_ym, 1, 4) AS INTEGER) * 12 + CAST(substr(fp.cohort_ym, 6, 2) AS INTEGER))
                       AS month_index,
                   COUNT(*) AS active_customers
            FROM first_purchase fp
            JOIN _activity a ON a.customer_id = fp.customer_id
            GROUP BY fp.cohort_ym, a.ym
        )
        SELECT m.cohort_ym,
               m.month_index,
               m.active_customers,
               c.cohort_size,
               ROUND(100.0 * m.active_customers / c.cohort_size, 2) AS retention_rate
        FROM monthly m
        JOIN (SELECT cohort_ym, COUNT(*) AS cohort_size FROM first_purchase GROUP BY cohort_ym) c
          ON c.cohort_ym = m.cohort_ym
        ORDER BY m.cohort_ym, m.month_index
        """
    )
    con.execute("DROP TABLE IF EXISTS _activity")
    con.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="仅取前 N 条交易(0 = 全量)")
    ap.add_argument(
        "--derived-only",
        action="store_true",
        help="跳过维度表/事实表构建(用于事实表已存在时只重算派生指标)",
    )
    args = ap.parse_args()

    out_path = BASE_DIR / "data" / "ecommerce.db"
    if out_path.exists() and not args.derived_only:
        out_path.unlink()

    con = sqlite3.connect(out_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")

    t0 = time.time()
    fact_count = con.execute("SELECT COUNT(*) FROM fact_transaction").fetchone()[0] if con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='fact_transaction'"
    ).fetchone()[0] else 0

    if args.derived_only and fact_count:
        log(f"检测到 fact_transaction({fact_count:,} 行),跳过维度/事实表构建")
    else:
        build_dim_tables(con)
        build_fact_table(con, args.sample)
    build_derived_tables(con)

    con.commit()
    for table in [
        "dim_customer", "dim_article", "fact_transaction",
        "daily_sales", "monthly_metrics", "rfm", "cohort_retention",
    ]:
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        log(f"{table}: {n:,} 行")
    size_mb = out_path.stat().st_size / 1024 / 1024
    log(f"完成,耗时 {time.time() - t0:.0f} 秒,数据库 {size_mb:.0f} MB")
    con.close()


if __name__ == "__main__":
    main()
