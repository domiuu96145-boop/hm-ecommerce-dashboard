"""verify_dashboard.py

自动验证 H&M 电商看板中的每一张图表都能通过 Superset API 正常出数。

用法:
    python scripts/verify_dashboard.py

可选环境变量:
    SUPERSET_URL      默认 http://localhost:8088
    SUPERSET_USER     默认 admin
    SUPERSET_PASSWORD 默认 admin
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = os.environ.get("SUPERSET_URL", "http://localhost:8088").rstrip("/")
USERNAME = os.environ.get("SUPERSET_USER", "admin")
PASSWORD = os.environ.get("SUPERSET_PASSWORD", "admin")

# H&M 看板下的 13 张图表 id(由 build_superset_dashboard.py 创建)
CHART_IDS = [104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116]


def api(path: str, token: str | None = None, payload: dict | None = None) -> dict:
    """调用 Superset REST API,payload 不为 None 时使用 POST。"""
    req = urllib.request.Request(
        BASE_URL + path, method="GET" if payload is None else "POST"
    )
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:800]}") from exc


def adhoc_filters_to_filters(adhoc_filters: list[dict]) -> list[dict]:
    """把 params 里的 adhoc_filters 转成 query 用的 filters。"""
    filters = []
    for f in adhoc_filters or []:
        if f.get("expressionType") == "SIMPLE" and f.get("clause") == "WHERE":
            filters.append(
                {
                    "col": f["subject"],
                    "op": f.get("operator", "=="),
                    "val": f.get("comparator"),
                }
            )
    return filters


def build_query_payload(chart: dict) -> dict:
    """根据图表存储的 params 构造 /api/v1/chart/data 的请求体。"""
    params = json.loads(chart["params"] or "{}")
    ds_id = chart["datasource_id"]
    viz = chart["viz_type"]

    columns = list(params.get("groupby") or [])
    if viz == "echarts_heatmap":
        columns = [c for c in (params.get("x_axis"), params.get("y_axis")) if c]

    metrics = list(params.get("metrics") or [])
    if not metrics and params.get("metric"):
        metrics = [params["metric"]]

    query: dict = {
        "columns": columns,
        "metrics": metrics,
        "filters": adhoc_filters_to_filters(params.get("adhoc_filters") or []),
        "time_range": params.get("time_range") or "No filter",
        "row_limit": params.get("row_limit") or 1000,
    }

    form_data = {
        **params,
        "datasource": f"{ds_id}__table",
        "viz_type": viz,
    }
    return {
        "datasource": {"id": ds_id, "type": "table"},
        "queries": [query],
        "form_data": form_data,
        "result_format": "json",
        "force": False,
    }


def main() -> int:
    token = api(
        "/api/v1/security/login",
        payload={
            "username": USERNAME,
            "password": PASSWORD,
            "provider": "db",
            "refresh": True,
        },
    )["access_token"]

    failures = []
    print(f"待验证图表: {len(CHART_IDS)} 张\n")
    for chart_id in CHART_IDS:
        chart = api(f"/api/v1/chart/{chart_id}", token=token)["result"]
        name = chart["slice_name"]
        try:
            payload = build_query_payload(chart)
            resp = api("/api/v1/chart/data", token=token, payload=payload)
            rows = len(resp.get("result", [{}])[0].get("data", [])) if resp.get("result") else 0
            status = "OK "
            detail = f"返回 {rows} 行"
        except Exception as exc:  # noqa: BLE001
            status = "FAIL"
            detail = str(exc)[:300]
            failures.append((chart_id, name, detail))
        print(f"[{status}] #{chart_id} {name} -> {detail}")

    print(f"\n结果: {len(CHART_IDS) - len(failures)}/{len(CHART_IDS)} 通过")
    if failures:
        print("\n失败的图表:")
        for chart_id, name, detail in failures:
            print(f"  #{chart_id} {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
