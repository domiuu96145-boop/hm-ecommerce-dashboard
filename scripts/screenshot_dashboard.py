"""screenshot_dashboard.py

用 Selenium 自动登录 Superset,打开 H&M 看板,等待全部图表渲染后
截取整页长图(用于 README/作品集展示)。

用法:
    python scripts/screenshot_dashboard.py

可选环境变量:
    SUPERSET_URL      默认 http://localhost:8088
    SUPERSET_USER     默认 admin
    SUPERSET_PASSWORD 默认 admin
    DASHBOARD_ID      默认 10
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options

BASE_URL = os.environ.get("SUPERSET_URL", "http://localhost:8088").rstrip("/")
USERNAME = os.environ.get("SUPERSET_USER", "admin")
PASSWORD = os.environ.get("SUPERSET_PASSWORD", "admin")
DASHBOARD_ID = os.environ.get("DASHBOARD_ID", "10")
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"


def wait_spinner_gone(driver: webdriver.Edge, timeout: int = 240) -> None:
    """等待页面上的加载动画全部消失(图表查询完成)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        spinners = driver.find_elements(By.CSS_SELECTOR, ".ant-spin-spinning, .loading")
        if not spinners:
            return
        time.sleep(2)
    raise TimeoutError("图表加载超时")


def main() -> None:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,3000")
    options.add_argument("--force-device-scale-factor=1")

    driver = webdriver.Edge(options=options)
    try:
        # 1. 登录
        driver.get(f"{BASE_URL}/login/")
        time.sleep(3)
        driver.find_element(By.CSS_SELECTOR, "#username").send_keys(USERNAME)
        driver.find_element(By.CSS_SELECTOR, "#password").send_keys(PASSWORD)
        submit = driver.find_element(
            By.CSS_SELECTOR, "button[type=submit], input[type=submit]"
        )
        submit.click()
        time.sleep(8)
        assert "welcome" in driver.current_url or "dashboard" in driver.current_url, (
            f"登录后未跳转: {driver.current_url}"
        )

        # 2. 打开 H&M 看板
        driver.get(f"{BASE_URL}/superset/dashboard/{DASHBOARD_ID}/")
        time.sleep(10)
        wait_spinner_gone(driver)

        # 3. 看板使用内部滚动容器,先滚到底再回顶,触发懒加载的图表渲染
        driver.execute_script(
            "const el = document.querySelector('[data-test=\"dashboard-content-wrapper\"]')"
            " || document.querySelector('.grid-container');"
            " if (el) { el.scrollTop = el.scrollHeight; }"
        )
        time.sleep(8)
        driver.execute_script(
            "const el = document.querySelector('[data-test=\"dashboard-content-wrapper\"]')"
            " || document.querySelector('.grid-container');"
            " if (el) { el.scrollTop = 0; }"
        )
        time.sleep(8)
        wait_spinner_gone(driver)

        # 4. 整页截图(CDP 支持超出视口)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_file = OUT_DIR / "dashboard_full.png"
        shot = driver.execute_cdp_cmd(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": True},
        )
        out_file.write_bytes(base64.b64decode(shot["data"]))
        print(f"截图已保存: {out_file} ({out_file.stat().st_size / 1024:.0f} KB)")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
