"""前端离线资源、交互稳定性、XSS 防护与滑窗限制 E2E 测试。"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.e2e


def _wait_vue_ready(page) -> None:
    page.wait_for_function(
        "() => Boolean(document.querySelector('#app') && document.querySelector('#app').__vue__)"
    )


def test_local_vendor_assets_and_no_external_cdn_requests(authed_page, base_url, factories):
    """所有主页面仅加载本地 /static/vendor/ 资源，不发起外部 CDN 请求且无运行时 JS 报错。"""
    factories.room(live_id="e2e_vendor_01", anchor_name="本地资源测试")
    base_host = urlparse(base_url).netloc

    external_requests = []
    vendor_statuses = {}
    page_errors = []

    def on_request(request):
        parsed = urlparse(request.url)
        if parsed.scheme in ("http", "https") and parsed.netloc != base_host:
            external_requests.append(request.url)

    def on_response(response):
        path = urlparse(response.url).path
        if path.startswith("/static/vendor/"):
            vendor_statuses[path] = response.status

    authed_page.on("request", on_request)
    authed_page.on("response", on_response)
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))

    for path in ("/", "/stats", "/history", "/room/e2e_vendor_01"):
        authed_page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
        _wait_vue_ready(authed_page)
        authed_page.wait_for_timeout(150)

    assert external_requests == [], f"检测到外部请求: {external_requests}"
    assert page_errors == [], f"页面产生运行时异常: {page_errors}"
    assert vendor_statuses.get("/static/vendor/vue.min.js") in (200, 304)
    assert vendor_statuses.get("/static/vendor/tailwind.min.css") in (200, 304)
    assert vendor_statuses.get("/static/vendor/echarts.min.js") in (200, 304)
    assert vendor_statuses.get("/static/vendor/socket.io.min.js") in (200, 304)


@pytest.mark.parametrize("route_path", ["/stats", "/room/e2e_blur_01"])
def test_user_search_blur_closes_dropdown_without_errors(authed_page, base_url, factories, route_path):
    """统计页与房间页用户名搜索框失焦时正确收起下拉面板且零 console/pageerror。"""
    if route_path.startswith("/room/"):
        factories.room(live_id="e2e_blur_01", anchor_name="失焦测试主播")

    page_errors = []
    console_errors = []
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))
    authed_page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )

    def handle_user_search(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "users": [
                        {"user_id": "u_blur_1", "nickname": "测试观众甲", "message_count": 3}
                    ]
                }
            ),
        )

    authed_page.route("**/user-search*", handle_user_search)
    authed_page.goto(f"{base_url}{route_path}", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    search_input = authed_page.locator("input[data-user-search-input='1']")
    search_input.click()
    search_input.fill("测试观众")
    authed_page.wait_for_timeout(450)
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.userSearchFocused") is True

    # 触发 blur 后延迟 180ms 应无报错地将 userSearchFocused 置为 false
    search_input.evaluate("el => el.blur()")
    authed_page.wait_for_timeout(300)

    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.userSearchFocused") is False
    assert page_errors == [], f"pageerror: {page_errors}"
    assert console_errors == [], f"console errors: {console_errors}"


def test_room_xss_escaped_and_message_sliding_window(authed_page, base_url, factories):
    """房间页实时消息对 HTML/XSS 载荷安全转义，并将消息列表裁剪至最多 500 条。"""
    factories.room(live_id="e2e_xss_01", anchor_name="安全与滑窗测试")

    page_errors = []
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))
    authed_page.goto(f"{base_url}/room/e2e_xss_01", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    authed_page.evaluate("() => { window.__xssExecuted = false; }")

    xss_chat = "<script>window.__xssExecuted=true</script><b onmouseover='window.__xssExecuted=true'>hello</b>"
    xss_user = "<img src=x onerror='window.__xssExecuted=true'>坏蛋"
    xss_gift = "<img src=x onerror='window.__xssExecuted=true'>火箭"

    authed_page.evaluate(
        """([xssChat, xssUser, xssGift]) => {
            const vm = document.querySelector('#app').__vue__;
            vm.handleMessage({
                type: 'chat',
                user_id: 'u_xss_1',
                user: xssUser,
                text: xssChat
            });
            vm.handleMessage({
                type: 'gift',
                user_id: 'u_xss_2',
                user: '礼物测试<svg onload="window.__xssExecuted=true">',
                gift_name: xssGift,
                gift_count: 2,
                total_value: 20
            });
            vm.handleMessage({
                type: 'chat',
                user_id: 'u_xss_3',
                user: '旧格式用户',
                content: '<span class="username">旧格式用户</span>: <img src=x onerror="window.__xssExecuted=true">旧消息'
            });
        }""",
        [xss_chat, xss_user, xss_gift],
    )
    authed_page.wait_for_timeout(150)

    assert authed_page.evaluate("() => Boolean(window.__xssExecuted)") is False
    assert authed_page.locator(".messages-container [onerror], .messages-container script, .messages-container svg[onload]").count() == 0
    container_text = authed_page.locator(".messages-container").inner_text()
    assert "<script>window.__xssExecuted=true</script>" in container_text
    assert xss_user in container_text
    assert xss_gift in container_text

    # 验证超过 500 条实时消息时的滑窗裁剪
    authed_page.evaluate(
        """() => {
            const vm = document.querySelector('#app').__vue__;
            vm.messages = [];
            for (let i = 0; i < 550; i++) {
                vm.handleMessage({
                    type: 'chat',
                    user_id: 'u_' + i,
                    user: '观众' + i,
                    text: 'msg-' + i
                });
            }
        }"""
    )
    authed_page.wait_for_timeout(150)

    state = authed_page.evaluate(
        """() => {
            const vm = document.querySelector('#app').__vue__;
            return {
                count: vm.messages.length,
                first: vm.messages[0] ? vm.messages[0].text : null,
                last: vm.messages[vm.messages.length - 1] ? vm.messages[vm.messages.length - 1].text : null
            };
        }"""
    )
    assert state["count"] == 500
    assert state["first"] == "msg-50"
    assert state["last"] == "msg-549"
    assert page_errors == []


def test_stats_like_tab_preserved_when_switching_back_to_all_rooms(authed_page, base_url, factories):
    """统计页在点赞榜切回“全部房间”时保留看板可见并刷新全部房间点赞榜。"""
    factories.room(live_id="e2e_stats_01", anchor_name="统计主播甲")

    authed_page.route(
        "**/api/rooms/sessions/stats*",
        lambda r: r.fulfill(status=200, content_type="application/json", body='{"stats":{"total_income":100,"total_sessions":1,"live_sessions":0,"ended_sessions":1,"total_gift_count":1,"total_chat_count":2,"total_like_count":50,"trend":[]}}'),
    )
    authed_page.route(
        "**/api/rooms/*/sessions/stats*",
        lambda r: r.fulfill(status=200, content_type="application/json", body='{"stats":{"total_income":100,"total_sessions":1,"live_sessions":0,"ended_sessions":1,"total_gift_count":1,"total_chat_count":2,"total_like_count":50,"trend":[]}}'),
    )
    authed_page.route(
        "**/api/rooms/*/sessions?*",
        lambda r: r.fulfill(status=200, content_type="application/json", body='{"sessions":[]}'),
    )
    authed_page.route(
        "**/api/rooms/contributors-by-date*",
        lambda r: r.fulfill(status=200, content_type="application/json", body='{"contributors":[],"page":1,"page_size":20,"total":0,"total_pages":1}'),
    )
    authed_page.route(
        "**/api/rooms/top-likers*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "likers": [
                        {
                            "user_id": "liker_1",
                            "user_name": "全站榜一",
                            "live_id": "e2e_stats_01",
                            "anchor_name": "统计主播甲",
                            "gift_count": 0,
                            "like_count": 999,
                            "fans_club_level": 0,
                        }
                    ]
                }
            ),
        ),
    )

    authed_page.goto(f"{base_url}/stats", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.hasSearched === true && vm.loading === false; }"
    )

    room_select = authed_page.locator("select").first
    room_select.select_option("e2e_stats_01")
    authed_page.wait_for_function("() => document.querySelector('#app').__vue__.hasSearched === true")

    authed_page.get_by_role("button", name="点赞榜").click()
    authed_page.wait_for_timeout(150)
    assert "全站榜一" in authed_page.locator("body").inner_text()

    # 切回“全部房间”("")：点赞榜和主看板应继续显示而不是突然消失
    room_select.select_option("")
    authed_page.wait_for_timeout(200)
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.hasSearched") is True
    assert "全站榜一" in authed_page.locator("body").inner_text()


def test_index_add_room_modal_extracts_live_id_from_douyin_share_url(authed_page, base_url):
    """首页添加直播间弹窗从分享链接提取直播间号 261378947940 并作为 live_id 提交。"""
    captured = []

    def handle_rooms(route):
        if route.request.method == "POST":
            captured.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"message": "ok", "room": {"live_id": "261378947940"}}),
            )
        else:
            route.continue_()

    authed_page.route("**/api/rooms", handle_rooms)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    authed_page.get_by_role("button", name="+ 添加直播间").click()
    room_input = authed_page.locator("input[placeholder*='live.douyin.com']")
    room_input.fill("来看看直播 https://live.douyin.com/261378947940?enter_from_merge=link_share 复制打开抖音")

    authed_page.get_by_text("已从链接自动提取房间标识：261378947940").wait_for()
    authed_page.get_by_role("button", name="确认添加").click()
    authed_page.wait_for_function("() => document.querySelector('#app').__vue__.showAddModal === false")

    assert len(captured) == 1
    assert captured[0].get("live_id") == "261378947940"


def test_room_active_offline_button_prefill_and_stream_user_click(authed_page, base_url, factories):
    """房间页在 24h 等待中显示停止监控、冷启动回填历史消息、点击用户打开详情弹窗。"""
    factories.room(live_id="e2e_room_ux_01", anchor_name="交互验证主播")

    authed_page.route(
        "**/api/rooms/e2e_room_ux_01",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "room": {
                        "live_id": "e2e_room_ux_01",
                        "anchor_name": "交互验证主播",
                        "status": "offline",
                        "is_active": True,
                        "is_archived": False,
                        "status_text": "主播未开播，等待开播中...",
                    }
                }
            ),
        ),
    )
    authed_page.route(
        "**/api/rooms/e2e_room_ux_01/current-session",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"session": {"id": 777, "status": "live", "viewer_count": 42, "total_income": 88}}),
        ),
    )
    authed_page.route(
        "**/api/rooms/e2e_room_ux_01/messages*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "messages": [
                        {"type": "chat", "user": "弹幕老观众", "user_id": "u_hist_1", "text": "后发的第二条"},
                        {"type": "chat", "user": "弹幕老观众", "user_id": "u_hist_1", "text": "先发的第一条"},
                    ]
                }
            ),
        ),
    )
    authed_page.route(
        "**/api/rooms/e2e_room_ux_01/user-messages*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "user": {"user_id": "u_hist_1", "nickname": "弹幕老观众"},
                    "stats": {
                        "total_messages": 2,
                        "chat_count": 2,
                        "gift_count": 0,
                        "like_count": 0,
                        "total_value": 0,
                    },
                    "messages": [
                        {"type": "chat", "content": "先发的第一条", "created_at": "2026-11-21T16:00:00"}
                    ],
                    "pagination": {"page": 1, "page_size": 50, "total": 1, "total_pages": 1},
                }
            ),
        ),
    )

    authed_page.goto(f"{base_url}/room/e2e_room_ux_01", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    authed_page.get_by_role("button", name="停止监控").wait_for()
    assert authed_page.get_by_role("button", name="启动监控").count() == 0
    assert authed_page.get_by_role("button", name="历史场次").is_visible()
    assert authed_page.get_by_role("link", name="房间统计").is_visible()

    authed_page.wait_for_function(
        "() => { const el = document.querySelector('.messages-container'); return el && el.innerText.includes('先发的第一条') && el.innerText.includes('后发的第二条'); }"
    )
    container_text = authed_page.locator(".messages-container").inner_text()
    assert container_text.index("先发的第一条") < container_text.index("后发的第二条")

    authed_page.locator(".messages-container .user-highlight").first.click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.showUserMessagesModal === true && vm.userMessagesLoading === false; }"
    )
    assert "弹幕老观众" in authed_page.locator("body").inner_text()


def test_stats_deeplink_query_params_and_auto_load(authed_page, base_url, factories):
    """统计页识别 ?live_id=...&range=total 深链并在首屏自动完成查询。"""
    factories.room(live_id="e2e_deep_01", anchor_name="深链测试主播")

    authed_page.goto(f"{base_url}/stats?live_id=e2e_deep_01&range=total", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.hasSearched === true && vm.loading === false; }"
    )

    state = authed_page.evaluate(
        "() => ({ room: document.querySelector('#app').__vue__.selectedRoomId, range: document.querySelector('#app').__vue__.timeRange })"
    )
    assert state == {"room": "e2e_deep_01", "range": "total"}
    assert authed_page.locator("a[href='/room/e2e_deep_01']").is_visible()


def test_history_custom_hard_delete_modal_and_stats_link(authed_page, base_url):
    """归档页展示查看统计直链并通过自定义确认弹窗与点击填入 ID 完成永久删除。"""
    delete_hits = []

    authed_page.route(
        "**/api/rooms/archived*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "rooms": [
                        {
                            "live_id": "e2e_arch_01",
                            "anchor_name": "归档测试主播",
                            "total_sessions": 3,
                            "total_income": 520,
                            "total_chat_count": 10,
                            "total_gift_count": 2,
                            "archived_at": "2026-11-21T12:00:00",
                        }
                    ]
                }
            ),
        ),
    )

    def handle_hard_delete(route):
        if route.request.method == "DELETE":
            payload = json.loads(route.request.post_data or "{}")
            delete_hits.append(payload)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"message": "deleted"}),
            )
        else:
            route.continue_()

    authed_page.route("**/api/rooms/e2e_arch_01/hard-delete*", handle_hard_delete)
    authed_page.goto(f"{base_url}/history", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    authed_page.get_by_text("归档测试主播").first.wait_for()
    assert authed_page.locator("a[href='/stats?live_id=e2e_arch_01&range=total']:visible").count() >= 1

    authed_page.locator("button:visible", has_text="永久删除").first.click()
    dialog = authed_page.locator(".hub-dialog-overlay")
    dialog.wait_for()
    confirm_btn = dialog.get_by_role("button", name="确认永久删除")
    assert confirm_btn.is_disabled()

    dialog.get_by_role("button", name="点击填入 ID").click()
    assert confirm_btn.is_enabled()
    confirm_btn.click()

    dialog.wait_for(state="detached")
    assert delete_hits == [{"confirm_live_id": "e2e_arch_01"}]


def test_room_offline_prefills_last_session_and_clears_on_live(authed_page, base_url, factories):
    """未开播房间自动回填上一场消息与榜单，收到开播实时消息后自动清空预览并切入实时流。"""
    factories.room(live_id="e2e_offline_prev_01", anchor_name="未开播预览主播")

    authed_page.route(
        "**/api/rooms/e2e_offline_prev_01",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "room": {
                        "live_id": "e2e_offline_prev_01",
                        "anchor_name": "未开播预览主播",
                        "status": "offline",
                        "is_active": True,
                        "is_archived": False,
                    }
                }
            ),
        ),
    )
    authed_page.route(
        "**/api/rooms/e2e_offline_prev_01/current-session",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "session": {
                        "id": 555,
                        "status": "ended",
                        "start_time": "2026-11-20T20:00:00",
                        "total_income": 300,
                        "total_gift_count": 3,
                    }
                }
            ),
        ),
    )
    authed_page.route(
        "**/api/rooms/e2e_offline_prev_01/sessions?*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "sessions": [
                        {
                            "id": 555,
                            "status": "ended",
                            "start_time": "2026-11-20T20:00:00",
                            "total_income": 300,
                            "total_gift_count": 3,
                        }
                    ]
                }
            ),
        ),
    )
    authed_page.route(
        "**/api/rooms/e2e_offline_prev_01/messages*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "messages": [
                        {"type": "chat", "user": "昨晚老粉", "user_id": "u_prev_1", "text": "主播明天见"}
                    ]
                }
            ),
        ),
    )
    authed_page.route(
        "**/api/rooms/sessions/555*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "session": {"id": 555, "live_id": "e2e_offline_prev_01"},
                    "counts": {"chat_count": 1, "gift_count": 0},
                    "contributors": [
                        {"user_id": "u_boss_1", "nickname": "昨晚榜一", "contribution_value": 300}
                    ],
                }
            ),
        ),
    )
    authed_page.route(
        "**/api/rooms/top-likers*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "likers": [
                        {"user_id": "u_like_1", "user_name": "历史点赞王", "like_count": 666}
                    ]
                }
            ),
        ),
    )

    authed_page.goto(f"{base_url}/room/e2e_offline_prev_01", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    authed_page.wait_for_function(
        "() => { const el = document.querySelector('.messages-container'); return el && el.innerText.includes('主播明天见'); }"
    )
    body_text = authed_page.locator("body").inner_text()
    assert "当前未开播" in body_text
    assert "昨晚榜一" in body_text
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.showLiveMetricCards") is False

    authed_page.locator("button", has_text="点赞榜").click()
    authed_page.wait_for_timeout(100)
    assert "历史点赞王" in authed_page.locator(".contributor-container").inner_text()

    authed_page.evaluate(
        """() => {
            const vm = document.querySelector('#app').__vue__;
            vm.currentSession = { id: 556, status: 'live' };
            vm.handleMessage({ type: 'chat', user_id: 'u_live_1', user: '新场观众', text: '开播啦第一条' });
        }"""
    )
    authed_page.wait_for_timeout(120)

    msg_text = authed_page.locator(".messages-container").inner_text()
    assert "开播啦第一条" in msg_text
    assert "主播明天见" not in msg_text
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.isOfflinePreview") is False


def test_blank_nickname_search_douyin_id_display_and_studio_icon(authed_page, base_url, factories):
    """空格可触发空白昵称检索，页面与弹窗统一显示 [空白昵称] 与抖音号/ID，并启用新版导播台图标。"""
    factories.room(live_id="e2e_blank_search_01", anchor_name="空白搜索测试主播")
    captured_queries = []

    def handle_user_search(route):
        parsed = urlparse(route.request.url)
        captured_queries.append(parsed.query)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "users": [
                        {
                            "live_id": "e2e_blank_search_01",
                            "anchor_name": "空白搜索测试主播",
                            "user_id": "100000099",
                            "display_id": "mock_dy_blank",
                            "nickname": "\u3164\u3164\u3164",
                            "total_messages": 4,
                            "chat_count": 3,
                            "gift_count": 1,
                            "total_value": 120,
                        }
                    ],
                    "total": 1,
                }
            ),
        )

    def handle_user_messages(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "user": {
                        "user_id": "100000099",
                        "display_id": "mock_dy_blank",
                        "nickname": "\u3164\u3164\u3164",
                        "level": 18,
                        "fans_club_level": 0,
                    },
                    "stats": {
                        "total_messages": 2,
                        "chat_count": 1,
                        "gift_count": 1,
                        "like_count": 5,
                        "total_value": 120,
                    },
                    "messages": [
                        {
                            "id": 901,
                            "type": "chat",
                            "user_id": "100000099",
                            "nickname": "\u3164\u3164\u3164",
                            "content": "空白昵称观众发言",
                            "created_at": "2026-11-21T12:00:00",
                        }
                    ],
                    "pagination": {"page": 1, "page_size": 50, "total": 1, "total_pages": 1},
                }
            ),
        )

    authed_page.route("**/user-search*", handle_user_search)
    authed_page.route("**/user-messages*", handle_user_messages)

    authed_page.goto(f"{base_url}/room/e2e_blank_search_01", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    # 验证新版 favicon 链接与 3 柱 + Live 红点导播台品牌图标
    favicon_href = authed_page.locator("link[rel='icon']").get_attribute("href")
    assert favicon_href == "/static/favicon.svg?v=2"
    assert authed_page.locator(".hub-brand-mark svg rect").count() == 3
    assert authed_page.locator(".hub-brand-mark svg circle").count() == 1

    # 弹幕流收到隐形填充符昵称时应渲染为可点击的 [空白昵称]
    authed_page.evaluate(
        """() => {
            const vm = document.querySelector('#app').__vue__;
            vm.handleMessage({
                type: 'chat',
                user_id: '100000099',
                user: '\\u3164\\u3164\\u3164',
                text: '隐形字符测试消息'
            });
        }"""
    )
    authed_page.wait_for_function(
        "() => document.querySelector('.messages-container').innerText.includes('[空白昵称]')"
    )

    # 输入 3 个空格并搜索，不应提示“请输入用户名”
    search_input = authed_page.locator("input[data-user-search-input='1']")
    search_input.click()
    search_input.fill("   ")
    authed_page.locator("button[type='submit']", has_text="搜索").click()

    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.userSearchResults.length === 1; }"
    )
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.userSearchError") == ""
    assert any("user_name=+++" in q or "user_name=%20%20%20" in q for q in captured_queries)

    dropdown_text = authed_page.locator("form .absolute").last.inner_text()
    assert "[空白昵称]" in dropdown_text
    assert "抖音号: mock_dy_blank" in dropdown_text
    assert "ID: 100000099" in dropdown_text

    # 点击搜索结果打开用户消息弹窗，验证标题和抖音号/ID副标题
    authed_page.locator("form button", has_text="[空白昵称]").first.dispatch_event("mousedown")
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.showUserMessagesModal && !vm.userMessagesLoading; }"
    )
    modal_text = authed_page.locator(".user-modal-content").inner_text()
    assert "[空白昵称]" in modal_text
    assert "抖音号: mock_dy_blank" in modal_text
    assert "ID: 100000099" in modal_text
    assert "空白昵称观众发言" in modal_text

    # 同时验证统计页输入纯空格也能触发检索且正常显示抖音号
    authed_page.goto(f"{base_url}/stats", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)
    stats_input = authed_page.locator("input[data-user-search-input='1']")
    stats_input.click()
    stats_input.fill("   ")
    authed_page.locator("button[type='submit']", has_text="搜索").click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.userSearchResults.length === 1; }"
    )
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.userSearchError") == ""
    assert "抖音号: mock_dy_blank" in authed_page.locator("form .absolute").last.inner_text()


def test_emoji_rendering_and_fallback_across_pages(authed_page, base_url, factories):
    """官方表情、富文本/贴纸表情正常渲染，非法 URL 被拦截，图片加载失败时优雅降级为文本。"""
    from pathlib import Path

    factories.room(live_id="e2e_emoji_01", anchor_name="表情E2E主播")

    valid_webp = (Path(__file__).resolve().parents[2] / "data" / "emoji_img" / "weixiao.webp").read_bytes()
    evil_requests = []

    def handle_douyinpic(route):
        url = route.request.url
        if "broken.png" in url:
            route.fulfill(status=404, body=b"not found")
        else:
            route.fulfill(status=200, content_type="image/webp", body=valid_webp)

    def handle_evil(route):
        evil_requests.append(route.request.url)
        route.abort()

    authed_page.route("https://*.douyinpic.com/**", handle_douyinpic)
    authed_page.route("http://evil.example/**", handle_evil)
    authed_page.route("https://douyinpic.com.evil.com/**", handle_evil)

    authed_page.route(
        "**/api/rooms/e2e_emoji_01/user-messages*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "user": {"user_id": "u_em_1", "nickname": "表情观众"},
                    "stats": {
                        "total_messages": 1,
                        "chat_count": 1,
                        "gift_count": 0,
                        "like_count": 0,
                        "total_value": 0,
                    },
                    "messages": [
                        {
                            "id": 1001,
                            "type": "chat",
                            "user_id": "u_em_1",
                            "nickname": "表情观众",
                            "content": "弹窗[微笑][专属]",
                            "emoji_list": [
                                {"alt": "[专属]", "url": "https://p3-webcast.douyinpic.com/img/modal.png"}
                            ],
                            "created_at": "2026-11-22T20:00:00",
                        }
                    ],
                    "pagination": {"page": 1, "page_size": 50, "total": 1, "total_pages": 1},
                }
            ),
        ),
    )

    page_errors = []
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))

    authed_page.goto(f"{base_url}/room/e2e_emoji_01", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    emoji_resp = authed_page.request.get(f"{base_url}/emoji_img/weixiao.webp")
    assert emoji_resp.status == 200

    authed_page.evaluate(
        """() => {
            const vm = document.querySelector('#app').__vue__;
            vm.handleMessage({
                type: 'chat',
                user_id: 'u_em_1',
                user: '表情观众',
                text: '太棒了[微笑][比心][粉丝牌][坏链接][失灵]',
                emoji_list: [
                    { alt: '[粉丝牌]', url: 'https://p3-webcast.douyinpic.com/img/badge.png' },
                    { alt: '[坏链接]', url: 'http://evil.example/x.png' },
                    { alt: '[坏链接]', url: 'javascript:alert(1)' },
                    { alt: '[失灵]', url: 'https://p3-webcast.douyinpic.com/img/broken.png' }
                ]
            });
            vm.handleMessage({
                type: 'chat',
                user_id: 'u_em_2',
                user: '贴纸观众',
                text: '[大贴纸]',
                emoji_list: [
                    { alt: '[大贴纸]', url: 'https://p3-webcast.douyinpic.com/img/sticker.png', sticker: true }
                ]
            });
        }"""
    )

    authed_page.wait_for_function(
        "() => document.querySelectorAll('.messages-container img.danmu-emoji, .messages-container img.danmu-emoji-sticker').length === 4"
    )
    # 等待 broken.png 触发 @error 降级为文本节点 [失灵] 且其余 4 张图片加载成功
    authed_page.wait_for_function(
        """() => {
            const box = document.querySelector('.messages-container');
            if (!box || !box.innerText.includes('[失灵]') || box.querySelector('img[src*="broken.png"]')) {
                return false;
            }
            const imgs = Array.from(box.querySelectorAll('img.danmu-emoji, img.danmu-emoji-sticker'));
            return imgs.length === 4 && imgs.every(el => el.complete && el.naturalWidth > 0);
        }"""
    )

    srcs = authed_page.evaluate(
        """() => Array.from(document.querySelectorAll('.messages-container img.danmu-emoji, .messages-container img.danmu-emoji-sticker'))
            .map(el => ({
                src: el.getAttribute('src'),
                alt: el.getAttribute('alt'),
                cls: el.getAttribute('class'),
                ref: el.getAttribute('referrerpolicy')
            }))"""
    )
    assert any(item["src"].startswith("/emoji_img/") and item["alt"] == "[微笑]" and item["ref"] is None for item in srcs)
    assert any(item["src"].startswith("/emoji_img/") and item["alt"] == "[比心]" for item in srcs)
    assert any(item["src"] == "https://p3-webcast.douyinpic.com/img/badge.png" and item["ref"] == "no-referrer" for item in srcs)
    assert any(item["src"] == "https://p3-webcast.douyinpic.com/img/sticker.png" and item["cls"] == "danmu-emoji-sticker" and item["ref"] == "no-referrer" for item in srcs)
    assert "http://evil.example/x.png" not in [item["src"] for item in srcs]
    assert "javascript:alert(1)" not in [item["src"] for item in srcs]
    assert evil_requests == []
    assert "[坏链接]" in authed_page.locator(".messages-container").inner_text()

    # 点击用户打开弹窗验证弹窗内表情渲染
    authed_page.locator(".messages-container .user-highlight").first.click()
    authed_page.wait_for_function(
        "() => document.querySelector('.user-modal-content img.danmu-emoji') !== null"
    )
    modal_imgs = authed_page.evaluate(
        "() => Array.from(document.querySelectorAll('.user-modal-content img.danmu-emoji')).map(el => el.getAttribute('src'))"
    )
    assert any(s.startswith("/emoji_img/") for s in modal_imgs)
    assert "https://p3-webcast.douyinpic.com/img/modal.png" in modal_imgs

    # 统计页场次明细与弹窗表情渲染
    authed_page.route(
        "**/api/rooms/sessions/888*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "session": {"id": 888, "live_id": "e2e_emoji_01", "start_time": "2026-11-22T19:00:00", "status": "ended", "total_income": 0, "total_gift_count": 0, "total_chat_count": 1},
                    "counts": {"chat_count": 1, "gift_count": 0},
                    "chats": [
                        {
                            "id": 2001,
                            "user_id": "u_em_1",
                            "nickname": "表情观众",
                            "content": "统计页[微笑][专属]",
                            "emoji_list": [{"alt": "[专属]", "url": "https://p3-webcast.douyinpic.com/img/stats.png"}],
                            "created_at": "2026-11-22T19:05:00",
                        }
                    ],
                }
            ),
        ),
    )
    authed_page.goto(f"{base_url}/stats?live_id=e2e_emoji_01&range=total", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)
    authed_page.evaluate("() => document.querySelector('#app').__vue__.viewSessionDetail({ id: 888 })")
    authed_page.wait_for_function(
        "() => document.querySelectorAll('img.danmu-emoji').length >= 2"
    )
    stats_srcs = authed_page.evaluate(
        "() => Array.from(document.querySelectorAll('img.danmu-emoji')).map(el => el.getAttribute('src'))"
    )
    assert any(s.startswith("/emoji_img/") for s in stats_srcs)
    assert "https://p3-webcast.douyinpic.com/img/stats.png" in stats_srcs
    assert page_errors == []


def test_modern_native_dialogs_ime_guard_and_stacked_top_layer(authed_page, base_url, factories):
    """原生顶层 <dialog> 弹窗、IME 组合态回车保护、顶层 popover Toast 与多层叠加 Esc 逐层关闭均正常。"""
    factories.room(live_id="e2e_modern_01", anchor_name="现代Web标准测试")

    page_errors = []
    console_errors = []
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))
    authed_page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )

    # 1. 首页：验证原生 <dialog> 打开、IME Enter 保护、顶层 Toast 及 Esc 关闭
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)

    authed_page.get_by_role("button", name="+ 添加直播间").click()
    authed_page.wait_for_function("() => document.querySelectorAll('dialog[open]').length === 1")

    # 模拟中文输入法组合态下的 Enter（isComposing / keyCode=229）不应触发提交或提示
    authed_page.evaluate(
        """() => {
            const input = document.getElementById('add-room-input');
            const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
            Object.defineProperty(ev, 'isComposing', { value: true });
            input.dispatchEvent(ev);
        }"""
    )
    authed_page.wait_for_timeout(120)
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.showAddModal") is True
    assert authed_page.locator(".hub-toast").count() == 0

    # 空输入点击“确认添加”应弹出位于顶层 Popover 中的 Toast，而不会关闭弹窗
    authed_page.get_by_role("button", name="确认添加").click()
    authed_page.locator(".hub-toast").wait_for()
    assert authed_page.evaluate(
        """() => {
            const c = document.querySelector('.hub-toast-container');
            return Boolean(c && (typeof c.showPopover !== 'function' || c.matches(':popover-open')));
        }"""
    ) is True

    # 按 Esc 关闭原生 <dialog>，Vue 状态同步回 false
    authed_page.keyboard.press("Escape")
    authed_page.wait_for_function(
        "() => document.querySelector('#app').__vue__.showAddModal === false && document.querySelectorAll('dialog[open]').length === 0"
    )

    # 2. 统计页：验证 场次详情 <dialog> 内再打开 用户消息 <dialog> 的原生顶层叠加与逐层 Esc 关闭
    authed_page.route(
        "**/api/rooms/sessions/909*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "session": {
                        "id": 909,
                        "live_id": "e2e_modern_01",
                        "start_time": "2026-11-23T01:00:00",
                        "status": "ended",
                        "total_income": 100,
                        "total_gift_count": 1,
                        "total_chat_count": 1,
                    },
                    "counts": {"chat_count": 1, "gift_count": 0},
                    "chats": [
                        {
                            "id": 3001,
                            "user_id": "u_mod_1",
                            "nickname": "叠加弹窗观众",
                            "content": "测试原生顶层叠加[微笑]",
                            "created_at": "2026-11-23T01:05:00",
                        }
                    ],
                }
            ),
        ),
    )
    authed_page.route(
        "**/user-messages*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "user": {"user_id": "u_mod_1", "nickname": "叠加弹窗观众"},
                    "stats": {"total_messages": 1, "chat_count": 1, "gift_count": 0, "like_count": 0, "total_value": 0},
                    "messages": [
                        {
                            "id": 3001,
                            "type": "chat",
                            "user_id": "u_mod_1",
                            "nickname": "叠加弹窗观众",
                            "content": "测试原生顶层叠加[微笑]",
                            "created_at": "2026-11-23T01:05:00",
                        }
                    ],
                    "pagination": {"page": 1, "page_size": 50, "total": 1, "total_pages": 1},
                }
            ),
        ),
    )

    authed_page.goto(f"{base_url}/stats?live_id=e2e_modern_01&range=total", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)
    authed_page.evaluate("() => document.querySelector('#app').__vue__.viewSessionDetail({ id: 909 })")
    authed_page.wait_for_function("() => document.querySelectorAll('dialog[open]').length === 1")

    authed_page.locator("dialog[open] .clickable-user", has_text="叠加弹窗观众").first.click()
    authed_page.wait_for_function(
        "() => document.querySelectorAll('dialog[open]').length === 2 && document.querySelector('#app').__vue__.showUserMessagesModal === true"
    )

    # 第一次 Esc 仅关闭最上层的用户消息弹窗，场次详情弹窗保持打开
    authed_page.keyboard.press("Escape")
    authed_page.wait_for_function(
        "() => document.querySelector('#app').__vue__.showUserMessagesModal === false && document.querySelector('#app').__vue__.showSessionModal === true && document.querySelectorAll('dialog[open]').length === 1"
    )

    # 第二次 Esc 关闭底层的场次详情弹窗
    authed_page.keyboard.press("Escape")
    authed_page.wait_for_function(
        "() => document.querySelector('#app').__vue__.showSessionModal === false && document.querySelectorAll('dialog[open]').length === 0"
    )

    assert page_errors == [], f"pageerror: {page_errors}"
    assert console_errors == [], f"console errors: {console_errors}"


def test_user_card_live_vs_history_scope_toggle_and_dividers(authed_page, base_url, factories):
    from datetime import datetime
    from models.database import CHINA_TZ

    room = factories.room(live_id="e2e_scope_live_01", anchor_name="双场测试主播", status="monitoring")
    s_old = factories.session_(
        live_id=room.live_id,
        status="ended",
        start_time=datetime(2026, 5, 1, 20, 0, tzinfo=CHINA_TZ),
        end_time=datetime(2026, 5, 1, 22, 0, tzinfo=CHINA_TZ),
        total_income=100,
        total_chat_count=1,
        total_gift_count=1,
    )
    s_live = factories.session_(
        live_id=room.live_id,
        status="live",
        start_time=datetime(2026, 5, 2, 20, 0, tzinfo=CHINA_TZ),
        total_income=10.5,
        total_chat_count=1,
        total_gift_count=3,
    )
    factories.chat(
        live_id=room.live_id,
        live_session_id=s_old.id,
        user_id="u_dual_1",
        user_name="双场老粉",
        user_level=22,
        content="昨晚的历史弹幕",
        created_at=datetime(2026, 5, 1, 20, 30, tzinfo=CHINA_TZ),
    )
    factories.gift(
        live_id=room.live_id,
        live_session_id=s_old.id,
        user_id="u_dual_1",
        user_name="双场老粉",
        user_level=22,
        gift_name="火箭",
        gift_count=1,
        gift_price=100,
        created_at=datetime(2026, 5, 1, 20, 31, tzinfo=CHINA_TZ),
    )
    factories.chat(
        live_id=room.live_id,
        live_session_id=s_live.id,
        user_id="u_dual_1",
        user_name="双场老粉",
        user_level=25,
        content="本场实时弹幕",
        created_at=datetime(2026, 5, 2, 20, 5, tzinfo=CHINA_TZ),
    )
    factories.gift(
        live_id=room.live_id,
        live_session_id=s_live.id,
        user_id="u_dual_1",
        user_name="双场老粉",
        user_level=25,
        gift_name="棒棒糖",
        gift_count=3,
        gift_price=3.5,
        created_at=datetime(2026, 5, 2, 20, 6, tzinfo=CHINA_TZ),
    )
    factories.contribution(
        live_id=room.live_id,
        user_id="u_dual_1",
        user_name="双场老粉",
        total_score=110.5,
        gift_count=4,
        chat_count=2,
        like_count=88,
        user_level=25,
    )

    page_errors = []
    console_errors = []
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))
    authed_page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )

    authed_page.goto(f"{base_url}/room/{room.live_id}", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)
    authed_page.wait_for_function(
        "() => document.querySelector('.messages-container')?.innerText.includes('本场实时弹幕')"
    )

    authed_page.locator(".messages-container .user-highlight", has_text="双场老粉").first.click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.showUserMessagesModal && !vm.userMessagesLoading; }"
    )

    focus_btn = authed_page.locator(".user-modal-content [data-scope-btn='focus']")
    all_btn = authed_page.locator(".user-modal-content [data-scope-btn='all']")
    assert focus_btn.get_attribute("aria-pressed") == "true"
    assert all_btn.get_attribute("aria-pressed") == "false"
    assert "本场直播" in focus_btn.inner_text()

    diamond_chip = authed_page.locator(".user-modal-content [data-chip='diamond']").inner_text()
    like_chip = authed_page.locator(".user-modal-content [data-chip='like']").inner_text()
    assert "10.5" in diamond_chip
    assert "累计 110.5" in diamond_chip
    assert "88" in like_chip
    assert "房间累计" in like_chip

    assert authed_page.locator(".user-modal-content .user-session-divider").count() == 0
    list_text_focus = authed_page.locator(".user-modal-content .user-messages-list").inner_text()
    assert "本场实时弹幕" in list_text_focus
    assert "昨晚的历史弹幕" not in list_text_focus

    authed_page.evaluate("() => { document.querySelector('#app').__vue__.userMessagesData.pagination.page = 2; }")
    all_btn.click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.userCardScopeMode === 'all' && !vm.userMessagesLoading; }"
    )
    assert authed_page.evaluate("() => document.querySelector('#app').__vue__.userMessagesData.pagination.page") == 1
    assert all_btn.get_attribute("aria-pressed") == "true"

    dividers = authed_page.locator(".user-modal-content .user-session-divider")
    assert dividers.count() == 2
    assert "is-live" in (dividers.nth(0).get_attribute("class") or "")
    assert "本场直播" in dividers.nth(0).inner_text()
    assert "is-live" not in (dividers.nth(1).get_attribute("class") or "")
    assert f"历史场次 #{s_old.id}" in dividers.nth(1).inner_text()

    list_text_all = authed_page.locator(".user-modal-content .user-messages-list").inner_text()
    assert "本场实时弹幕" in list_text_all
    assert "昨晚的历史弹幕" in list_text_all
    assert page_errors == [], f"pageerror: {page_errors}"
    assert console_errors == [], f"console errors: {console_errors}"


def test_room_offline_preview_search_opens_all_scope_for_historical_user(authed_page, base_url, factories):
    from datetime import datetime
    from models.database import CHINA_TZ

    room = factories.room(live_id="e2e_scope_off_01", anchor_name="离线预览主播", status="offline")
    s_older = factories.session_(
        live_id=room.live_id,
        status="ended",
        start_time=datetime(2026, 5, 1, 18, 0, tzinfo=CHINA_TZ),
        end_time=datetime(2026, 5, 1, 19, 0, tzinfo=CHINA_TZ),
        total_chat_count=1,
    )
    s_prev = factories.session_(
        live_id=room.live_id,
        status="ended",
        start_time=datetime(2026, 5, 2, 18, 0, tzinfo=CHINA_TZ),
        end_time=datetime(2026, 5, 2, 19, 0, tzinfo=CHINA_TZ),
        total_chat_count=1,
    )
    factories.chat(
        live_id=room.live_id,
        live_session_id=s_older.id,
        user_id="u_old_only",
        user_name="更早历史观众",
        content="前天的留言记录",
        created_at=datetime(2026, 5, 1, 18, 20, tzinfo=CHINA_TZ),
    )
    factories.chat(
        live_id=room.live_id,
        live_session_id=s_prev.id,
        user_id="u_prev_only",
        user_name="上一场观众",
        content="昨晚收官消息",
        created_at=datetime(2026, 5, 2, 18, 20, tzinfo=CHINA_TZ),
    )

    page_errors = []
    console_errors = []
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))
    authed_page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )

    authed_page.goto(f"{base_url}/room/{room.live_id}", wait_until="domcontentloaded")
    _wait_vue_ready(authed_page)
    authed_page.wait_for_function(
        "() => document.querySelector('#app').__vue__.isOfflinePreview === true"
    )

    search_input = authed_page.locator("input[data-user-search-input='1']")
    search_input.click()
    search_input.fill("更早历史观众")
    authed_page.locator("button[type='submit']", has_text="搜索").click()
    authed_page.wait_for_function(
        "() => document.querySelector('#app').__vue__.userSearchResults.length === 1"
    )

    authed_page.locator("form button", has_text="更早历史观众").first.dispatch_event("mousedown")
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.showUserMessagesModal && !vm.userMessagesLoading; }"
    )

    focus_btn = authed_page.locator(".user-modal-content [data-scope-btn='focus']")
    all_btn = authed_page.locator(".user-modal-content [data-scope-btn='all']")
    assert "上一场" in focus_btn.inner_text()
    assert focus_btn.get_attribute("aria-pressed") == "false"
    assert all_btn.get_attribute("aria-pressed") == "true"
    assert "前天的留言记录" in authed_page.locator(".user-modal-content .user-messages-list").inner_text()
    assert page_errors == [], f"pageerror: {page_errors}"
    assert console_errors == [], f"console errors: {console_errors}"


def test_stats_session_deeplink_room_scoping_and_date_range_toggle(authed_page, base_url, factories):
    from datetime import timedelta
    from models.database import get_china_now

    now = get_china_now()
    recent_dt = (now - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    old_dt = (now - timedelta(days=20)).replace(hour=20, minute=0, second=0, microsecond=0)

    room_a = factories.room(live_id="e2e_stats_scope_a", anchor_name="主播甲")
    room_b = factories.room(live_id="e2e_stats_scope_b", anchor_name="主播乙")

    s_old_a = factories.session_(
        live_id=room_a.live_id,
        status="ended",
        start_time=old_dt,
        end_time=old_dt + timedelta(hours=1),
        total_income=50,
        total_chat_count=1,
        total_gift_count=1,
    )
    s_recent_a = factories.session_(
        live_id=room_a.live_id,
        status="ended",
        start_time=recent_dt,
        end_time=recent_dt + timedelta(hours=1),
        total_income=80,
        total_chat_count=1,
        total_gift_count=1,
    )
    s_other_b = factories.session_(
        live_id=room_b.live_id,
        status="ended",
        start_time=recent_dt,
        end_time=recent_dt + timedelta(hours=1),
        total_income=30,
        total_chat_count=1,
    )

    factories.chat(
        live_id=room_a.live_id,
        live_session_id=s_old_a.id,
        user_id="u_shared_99",
        user_name="跨期常客",
        content="甲房20天前弹幕",
        created_at=old_dt + timedelta(minutes=10),
    )
    factories.gift(
        live_id=room_a.live_id,
        live_session_id=s_old_a.id,
        user_id="u_shared_99",
        user_name="跨期常客",
        gift_name="旧玫瑰",
        gift_count=1,
        gift_price=50,
        created_at=old_dt + timedelta(minutes=15),
    )
    factories.chat(
        live_id=room_a.live_id,
        live_session_id=s_recent_a.id,
        user_id="u_shared_99",
        user_name="跨期常客",
        content="甲房近期弹幕",
        created_at=recent_dt + timedelta(minutes=10),
    )
    factories.gift(
        live_id=room_a.live_id,
        live_session_id=s_recent_a.id,
        user_id="u_shared_99",
        user_name="跨期常客",
        gift_name="新玫瑰",
        gift_count=1,
        gift_price=80,
        created_at=recent_dt + timedelta(minutes=15),
    )
    factories.chat(
        live_id=room_b.live_id,
        live_session_id=s_other_b.id,
        user_id="u_shared_99",
        user_name="跨期常客",
        content="乙房绝不该泄露的弹幕",
        created_at=recent_dt + timedelta(minutes=20),
    )

    page_errors = []
    console_errors = []
    authed_page.on("pageerror", lambda err: page_errors.append(str(err)))
    authed_page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )

    authed_page.goto(
        f"{base_url}/stats?session_id={s_recent_a.id}",
        wait_until="domcontentloaded",
    )
    _wait_vue_ready(authed_page)
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.showSessionModal && !vm.sessionDetailLoading; }"
    )
    authed_page.locator("dialog[open] .clickable-user", has_text="跨期常客").first.click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.showUserMessagesModal && !vm.userMessagesLoading; }"
    )

    assert "主播: 主播甲" in authed_page.locator(".user-modal-content [data-user-room-pill='1']").inner_text()
    assert authed_page.locator(".user-modal-content [data-scope-btn='focus']").get_attribute("aria-pressed") == "true"

    authed_page.locator(".user-modal-content [data-scope-btn='all']").click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.userCardScopeMode === 'all' && !vm.userMessagesLoading; }"
    )
    all_text = authed_page.locator(".user-modal-content .user-messages-list").inner_text()
    assert "甲房近期弹幕" in all_text
    assert "甲房20天前弹幕" in all_text
    assert "乙房绝不该泄露的弹幕" not in all_text

    authed_page.keyboard.press("Escape")
    authed_page.wait_for_function("() => document.querySelector('#app').__vue__.showUserMessagesModal === false")
    authed_page.keyboard.press("Escape")
    authed_page.wait_for_function("() => document.querySelector('#app').__vue__.showSessionModal === false")

    authed_page.locator("[data-dashboard-section='detail-grid'] .clickable-user", has_text="跨期常客").first.click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.showUserMessagesModal && !vm.userMessagesLoading; }"
    )
    focus_btn = authed_page.locator(".user-modal-content [data-scope-btn='focus']")
    assert focus_btn.get_attribute("aria-pressed") == "true"
    assert "近7天" in focus_btn.inner_text()
    focused_list_text = authed_page.locator(".user-modal-content .user-messages-list").inner_text()
    assert "甲房近期弹幕" in focused_list_text
    assert "甲房20天前弹幕" not in focused_list_text

    authed_page.locator(".user-modal-content [data-scope-btn='all']").click()
    authed_page.wait_for_function(
        "() => { const vm = document.querySelector('#app').__vue__; return vm.userCardScopeMode === 'all' && !vm.userMessagesLoading; }"
    )
    full_list_text = authed_page.locator(".user-modal-content .user-messages-list").inner_text()
    assert "甲房20天前弹幕" in full_list_text
    assert authed_page.locator(".user-modal-content .user-session-divider").count() == 2
    assert page_errors == [], f"pageerror: {page_errors}"
    assert console_errors == [], f"console errors: {console_errors}"




