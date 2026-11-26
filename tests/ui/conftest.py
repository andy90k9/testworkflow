# -*- coding: utf-8 -*-
"""UI 测试 page fixture：local / cdp 双模式；ego_skill 模式不需要 Playwright。"""
import os

import pytest

UI_MODE = os.environ.get("DSH_UI_MODE", "local").lower()

if UI_MODE != "ego_skill":
    playwright = pytest.importorskip(
        "playwright", reason="未安装 playwright 库（UI 测试跳过）: pip install playwright"
    )
    from playwright.sync_api import sync_playwright  # noqa: E402

    UI_MODE = os.environ.get("DSH_UI_MODE", "local").lower()
    CDP_ENDPOINT = os.environ.get("DSH_UI_CDP_ENDPOINT", "http://127.0.0.1:9222")
    ATTACH_CONTEXT = os.environ.get("DSH_UI_ATTACH_CONTEXT", "first")
    CLOSE_PAGE_AFTER = os.environ.get("DSH_UI_CLOSE_PAGE_AFTER", "true").lower() == "true"
    HEADLESS = os.environ.get("DSH_UI_HEADLESS", "true").lower() == "true"

    @pytest.fixture(scope="session")
    def _pw():
        manager = sync_playwright().start()
        yield manager
        manager.stop()

    @pytest.fixture(scope="session")
    def _browser(_pw):
        if UI_MODE == "cdp":
            try:
                browser = _pw.chromium.connect_over_cdp(CDP_ENDPOINT, timeout=8000)
            except Exception as e:
                pytest.exit(
                    f"无法连接已打开的浏览器（CDP {CDP_ENDPOINT}）: {e}\n"
                    f"请确认 Ego 浏览器已带调试端口启动，或改用 ui_mode: ego_skill",
                    returncode=3,
                )
            yield browser
            try:
                browser.close()
            except Exception:
                pass
        else:
            browser = _pw.chromium.launch(headless=HEADLESS)
            yield browser
            browser.close()

    @pytest.fixture
    def page(_browser):
        if UI_MODE == "cdp":
            ctxs = list(_browser.contexts)
            if ctxs and ATTACH_CONTEXT == "first":
                ctx = ctxs[0]
            else:
                ctx = _browser.new_context()
                ctxs.append(ctx)
            pg = ctx.new_page()
            yield pg
            if CLOSE_PAGE_AFTER:
                try:
                    pg.close()
                except Exception:
                    pass
        else:
            ctx = _browser.new_context()
            pg = ctx.new_page()
            yield pg
            try:
                ctx.close()
            except Exception:
                pass
