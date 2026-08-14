"""在本地 Superset 中自动搭建 H&M 电商经营分析看板。

前置条件:
1. 本地 Superset 已启动(http://localhost:8088,admin/admin)
2. 已执行 build_database.py 生成 data/ecommerce.db
3. Superset 中已注册 "H&M E-commerce" 数据库(SQLite)

用法:
    python scripts/build_superset_dashboard.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
import zipfile
from pathlib import Path

import requests

BASE = "http://localhost:8088"
USER = "admin"
PASSWORD = "admin"
DB_NAME = "H&M E-commerce"
REPO = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO / "dashboard"


def metric(col: str, agg: str, label: str) -> dict:
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": col},
        "aggregate": agg,
        "label": label,
    }


def login(session: requests.Session) -> str:
    r = session.post(
        f"{BASE}/api/v1/security/login",
        json={"username": USER, "password": PASSWORD, "provider": "db", "refresh": True},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def csrf(session: requests.Session, token: str) -> str:
    r = session.get(
        f"{BASE}/api/v1/security/csrf_token/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["result"]


def api_get(session: requests.Session, token: str, path: str) -> dict:
    r = session.get(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def api_post(session: requests.Session, token: str, csrf_token: str, path: str, payload: dict) -> dict:
    r = session.post(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "X-CSRFToken": csrf_token},
        json=payload,
        timeout=180,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def api_put(session: requests.Session, token: str, csrf_token: str, path: str, payload: dict) -> dict:
    r = session.put(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "X-CSRFToken": csrf_token},
        json=payload,
        timeout=180,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"PUT {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def link_dashboard_slices(dash_id: int, chart_ids: list[int]) -> None:
    """在 Superset 元数据库中把图表关联到看板(REST API 不暴露此关系)。

    与 UI 中"把图表拖进看板"的效果一致, 幂等可重复执行。
    """
    db_path = os.environ.get("SUPERSET_DB_PATH") or (
        Path(sys.executable).resolve().parents[2] / "data" / "superset.db"
    )
    if not Path(db_path).exists():
        raise RuntimeError(f"找不到 Superset 元数据库: {db_path}")
    con = sqlite3.connect(db_path)
    try:
        con.executemany(
            "INSERT OR IGNORE INTO dashboard_slices (dashboard_id, slice_id) VALUES (?, ?)",
            [(dash_id, cid) for cid in chart_ids],
        )
        con.commit()
        linked = con.execute(
            "SELECT COUNT(*) FROM dashboard_slices WHERE dashboard_id = ?", (dash_id,)
        ).fetchone()[0]
    finally:
        con.close()
    print(f"图表已关联到看板: {linked} 张")


def find_database(session: requests.Session, token: str) -> int:
    data = api_get(session, token, "/api/v1/database/?q=(page_size:100)")
    for row in data["result"]:
        if row["database_name"] == DB_NAME:
            return row["id"]
    raise RuntimeError(f"数据库 {DB_NAME} 未找到,请先在 Superset 中注册")


def ensure_dataset(session: requests.Session, token: str, csrf_token: str, db_id: int, table: str) -> int:
    data = api_get(session, token, "/api/v1/dataset/?q=(page_size:500)")
    for row in data["result"]:
        if row["database"]["id"] == db_id and row["table_name"] == table:
            print(f"  数据集已存在: {table} (id={row['id']})")
            return row["id"]
    try:
        payload = {"database": db_id, "table_name": table}
        created = api_post(session, token, csrf_token, "/api/v1/dataset/", payload)
        print(f"  创建数据集: {table} (id={created['id']})")
        return created["id"]
    except RuntimeError as e:
        if "schema" in str(e).lower() or "table" in str(e).lower():
            payload = {"database": db_id, "schema": "main", "table_name": table}
            created = api_post(session, token, csrf_token, "/api/v1/dataset/", payload)
            print(f"  创建数据集(main): {table} (id={created['id']})")
            return created["id"]
        raise


def ensure_temporal_column(
    session: requests.Session, token: str, csrf_token: str,
    ds_id: int, column_name: str,
) -> None:
    """把数据集中的某一列标记为时间列(时间序列图表必需)。"""
    ds = api_get(session, token, f"/api/v1/dataset/{ds_id}")["result"]
    cols = ds.get("columns") or []
    target = next((c for c in cols if c["column_name"] == column_name), None)
    if target is None:
        raise RuntimeError(f"数据集 {ds_id} 中找不到列 {column_name}")
    if target.get("is_dttm"):
        return
    payload_cols = []
    for c in cols:
        item = {
            "id": c["id"],
            "column_name": c["column_name"],
            "type": c.get("type"),
            "is_dttm": c["column_name"] == column_name,
            "groupby": c.get("groupby", True),
            "filterable": c.get("filterable", True),
            "expression": c.get("expression"),
            "verbose_name": c.get("verbose_name"),
            "description": c.get("description"),
            "advanced_data_type": c.get("advanced_data_type"),
            "python_date_format": c.get("python_date_format"),
        }
        if c["column_name"] == column_name:
            item["type"] = "TIMESTAMP"
        payload_cols.append(item)
    api_put(session, token, csrf_token, f"/api/v1/dataset/{ds_id}", {"columns": payload_cols})
    print(f"  已将 {column_name} 标记为时间列 (dataset id={ds_id})")


def create_chart(
    session: requests.Session, token: str, csrf_token: str,
    ds_id: int, name: str, viz_type: str, params: dict,
) -> int:
    existing = api_get(session, token, "/api/v1/chart/?q=(page_size:1000)")
    for row in existing["result"]:
        if row["slice_name"] == name:
            cid = row["id"]
            cur = api_get(session, token, f"/api/v1/chart/{cid}")["result"]
            cur_params = json.loads(cur["params"] or "{}")
            if cur["viz_type"] != viz_type or cur_params != params:
                api_put(session, token, csrf_token, f"/api/v1/chart/{cid}", {
                    "viz_type": viz_type,
                    "params": json.dumps(params, ensure_ascii=False),
                })
                print(f"  更新图表: {name} (id={cid})")
            else:
                print(f"  图表已存在: {name} (id={cid})")
            return cid
    payload = {
        "datasource_id": ds_id,
        "datasource_type": "table",
        "viz_type": viz_type,
        "slice_name": name,
        "params": json.dumps(params, ensure_ascii=False),
    }
    created = api_post(session, token, csrf_token, "/api/v1/chart/", payload)
    print(f"  图表: {name} (id={created['id']})")
    return created["id"]


def main() -> int:
    session = requests.Session()
    token = login(session)
    csrf_token = csrf(session, token)
    db_id = find_database(session, token)
    print(f"数据库: {DB_NAME} (id={db_id})")

    tables = [
        "daily_sales",
        "monthly_metrics",
        "rfm",
        "cohort_retention",
        "category_monthly",
        "dim_article",
        "dim_customer",
    ]
    ds_ids: dict[str, int] = {}
    for t in tables:
        ds_ids[t] = ensure_dataset(session, token, csrf_token, db_id, t)
    for t in ("monthly_metrics", "category_monthly"):
        ensure_temporal_column(session, token, csrf_token, ds_ids[t], "month_date")

    charts: list[tuple[str, int, str]] = []  # (name, chart_id, chart_uuid)

    def add(ds_key: str, name: str, viz: str, params: dict) -> None:
        cid = create_chart(session, token, csrf_token, ds_ids[ds_key], name, viz, params)
        cuuid = api_get(session, token, f"/api/v1/chart/{cid}")["result"]["uuid"]
        charts.append((name, cid, cuuid))

    # KPI 卡片
    add("monthly_metrics", "累计 GMV(万)", "big_number_total",
        {"metric": metric("gmv", "SUM", "GMV"), "time_range": "No filter", "subheader": "GMV 合计"})
    add("monthly_metrics", "累计售出件数(万)", "big_number_total",
        {"metric": metric("items", "SUM", "件数"), "time_range": "No filter", "subheader": "售出商品件数"})
    add("monthly_metrics", "累计活跃客户(万)", "big_number_total",
        {"metric": metric("customers", "SUM", "客户数"), "time_range": "No filter", "subheader": "下单客户数"})
    add("monthly_metrics", "平均月度复购率(%)", "big_number_total",
        {"metric": metric("repurchase_rate", "AVG", "复购率"), "time_range": "No filter", "subheader": "月度复购率均值"})

    # 趋势(时间序列柱状图)
    add("monthly_metrics", "月度 GMV 趋势", "echarts_timeseries_bar",
        {"metrics": [metric("gmv", "SUM", "GMV")],
         "time_range": "No filter", "granularity_sqla": "month_date",
         "time_grain_sqla": "P1M", "adhoc_filters": []})
    add("monthly_metrics", "月度件数与客户数", "echarts_timeseries_bar",
        {"metrics": [metric("items", "SUM", "件数"), metric("customers", "SUM", "客户数")],
         "time_range": "No filter", "granularity_sqla": "month_date",
         "time_grain_sqla": "P1M", "adhoc_filters": []})

    # 结构
    add("monthly_metrics", "渠道销售对比", "pie",
        {"metric": metric("gmv", "SUM", "GMV"), "groupby": ["channel"],
         "time_range": "No filter", "row_limit": 10, "label_type": "value_percent",
         "show_labels": True, "show_legend": True})
    add("rfm", "用户分层占比", "pie",
        {"metric": metric("customer_id", "COUNT_DISTINCT", "客户数"), "groupby": ["segment"],
         "time_range": "No filter", "row_limit": 10, "label_type": "value_percent",
         "show_labels": True, "show_legend": True})
    add("rfm", "各分层消费贡献", "pie",
        {"metric": metric("monetary", "SUM", "消费额"), "groupby": ["segment"],
         "time_range": "No filter", "row_limit": 10, "label_type": "value_percent",
         "show_labels": True, "show_legend": True, "donut": True})

    # 留存与品类
    add("cohort_retention", "月度留存热力图", "heatmap_v2",
        {"x_axis": "month_index", "y_axis": "cohort_ym",
         "metric": metric("retention_rate", "AVG", "留存率"),
         "time_range": "No filter", "linear_color_scheme": "fire", "show_value": True})
    add("category_monthly", "品类销售 TOP10", "treemap_v2",
        {"metrics": [metric("gmv", "SUM", "GMV")], "groupby": ["product_group"],
         "time_range": "No filter", "row_limit": 10})
    add("category_monthly", "品类月度销售明细", "table",
        {"metrics": [metric("gmv", "SUM", "GMV"), metric("items", "SUM", "件数")],
         "groupby": ["month", "product_group"], "time_range": "No filter",
         "row_limit": 20})
    add("rfm", "RFM 分层汇总表", "table",
        {"metrics": [metric("customer_id", "COUNT_DISTINCT", "客户数"),
                     metric("monetary", "SUM", "消费额"),
                     metric("frequency", "AVG", "平均购买次数")],
         "groupby": ["segment"], "time_range": "No filter", "row_limit": 10})

    # 创建/复用看板
    dash_title = "H&M 电商经营分析看板"
    dash_id = None
    existing_dash = api_get(session, token, "/api/v1/dashboard/?q=(page_size:100)")
    for row in existing_dash["result"]:
        if row["dashboard_title"] == dash_title:
            dash_id = row["id"]
            break
    if dash_id is None:
        dash = api_post(session, token, csrf_token, "/api/v1/dashboard/",
                        {"dashboard_title": dash_title, "slug": "hm-ecommerce-dashboard"})
        dash_id = dash["id"]
    print(f"看板: {dash_title} (id={dash_id})")

    # 布局: 与 Superset 自身生成的 v2 结构一致
    # ROOT_ID -> GRID_ID -> ROW-xxx -> CHART-xxx
    pos = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": []},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID",
                      "meta": {"text": "H&M 电商经营分析看板"}, "children": []},
    }
    rows = []
    for i in range(0, len(charts), 2):
        row_id = f"ROW-{uuid.uuid4().hex[:8]}"
        row_children = []
        for name, cid, cuuid in charts[i:i + 2]:
            chart_key = f"CHART-{cid}"
            pos[chart_key] = {
                "type": "CHART", "id": chart_key, "children": [],
                "meta": {"chartId": cid, "width": 6, "height": 40,
                         "sliceName": name, "uuid": cuuid},
            }
            row_children.append(chart_key)
        pos[row_id] = {
            "type": "ROW", "id": row_id, "children": row_children,
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        rows.append(row_id)
    pos["GRID_ID"]["children"] = rows
    api_put(session, token, csrf_token, f"/api/v1/dashboard/{dash_id}", {
        "position_json": json.dumps(pos, ensure_ascii=False),
        "json_metadata": json.dumps({
            "chart_configuration": {},
            "color_scheme": "",
            "color_scheme_domain": [],
            "cross_filters_enabled": False,
            "default_filters": "{}",
            "expanded_slices": {},
            "global_chart_configuration": {},
            "label_colors": {},
            "map_label_colors": {},
            "native_filter_configuration": [],
            "refresh_frequency": 0,
            "shared_label_colors": [],
            "timed_refresh_immune_slices": [],
        }),
    })
    print("看板布局已更新")
    link_dashboard_slices(dash_id, [cid for _name, cid, _cuuid in charts])

    # 导出看板配置(供 GitHub 仓库使用)
    DASHBOARD_DIR.mkdir(exist_ok=True)
    export = session.get(
        f"{BASE}/api/v1/dashboard/export/?q=!({dash_id})",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    export.raise_for_status()
    zip_path = DASHBOARD_DIR / "hm_ecommerce_dashboard.zip"
    zip_path.write_bytes(export.content)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(DASHBOARD_DIR)
    print(f"看板已导出: {zip_path}")

    print(f"\n完成!打开 http://localhost:8088/superset/dashboard/{dash_id}/ 查看看板")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
