"""Smoke E2E：验证 harness 跑通 —— 登录态访问首页、退出回到登录页。"""
import pytest

pytestmark = pytest.mark.e2e


def test_home_renders_with_navigation(authed_page, base_url):
    """登录态首页能渲染、导航按钮在。"""
    authed_page.goto(base_url)
    assert authed_page.locator("text=抖音直播记录站").is_visible()
    assert authed_page.locator("a[href='/stats']").is_visible()
    assert authed_page.locator("a[href='/history']").is_visible()


def test_logout_redirects_to_login(authed_page, base_url):
    """点击退出登录会跳到 /login 并展示用户名输入框。"""
    authed_page.goto(base_url)
    authed_page.locator("a[href='/logout']").click()
    authed_page.wait_for_url(f"{base_url}/login")
    assert authed_page.locator("input[name=username]").is_visible()


def test_home_shows_cookie_health_pill(authed_page, base_url):
    """首页 Cookie 状态徽章渲染健康状态文案（未配置/正常/确认中/已失活/未知之一）。"""
    authed_page.goto(base_url)
    pill = authed_page.locator(".status-pill", has_text="Cookie")
    assert pill.is_visible()
    text = pill.inner_text()
    assert any(label in text for label in ("未配置", "正常", "确认中", "已失活", "未知"))
