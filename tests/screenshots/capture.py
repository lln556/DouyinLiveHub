"""一次性 mock 截图录制脚本，生成 docs/screenshots/*.png。

约束（与 commit 4c44699 的 mock 截图录制方式一致）：
- 全部使用虚构主播 / 用户 / 礼物，不引用任何真实账号
- mock 房间用 monitor_type=manual + auto_reconnect=False，避免被 scheduler
  真去连抖音 WebSocket 并写 error_message 进库
- 写入的目标库由 DATABASE_URL 决定 —— 调用方 (scripts/capture_screenshots.sh)
  负责把它锁到测试库 douyin_live_test，并把临时 app 进程指到同一个库；
  脚本自身只接受测试库，否则拒绝执行

运行方式：scripts/capture_screenshots.sh
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "tests" / ".env.test")
load_dotenv(ROOT / ".env")

from playwright.sync_api import sync_playwright  # noqa: E402
from sqlalchemy import text  # noqa: E402

from models.database import (  # noqa: E402
    ChatMessage,
    GiftMessage,
    LiveRoom,
    LiveSession,
    UserContribution,
    get_china_now,
)
from services.data_service import DataService  # noqa: E402

OUT_DIR = ROOT / "docs" / "screenshots"
TARGET_LIVE_ID = "live_201"
ANCHOR_NAME = "茶语小筑"

ROOMS = [
    dict(live_id="live_201", anchor_name="茶语小筑", status="monitoring"),
    dict(live_id="live_202", anchor_name="山海图志", status="monitoring"),
    dict(live_id="live_203", anchor_name="蘑菇研究所", status="offline"),
    dict(live_id="live_204", anchor_name="晚风手账", status="stopped"),
    dict(live_id="live_205", anchor_name="夜车电台", status="archived"),
]

USERS = [
    dict(user_id="u_1001", user_name="云间野客",   user_level=42, gender=1, follower=1280, following=88,  age=3),
    dict(user_id="u_1002", user_name="雾里看花",   user_level=35, gender=2, follower=520,  following=128, age=4),
    dict(user_id="u_1003", user_name="星月相照",   user_level=58, gender=1, follower=3200, following=15,  age=3),
    dict(user_id="u_1004", user_name="溪畔渡舟",   user_level=21, gender=2, follower=180,  following=88,  age=2),
    dict(user_id="u_1005", user_name="古道西风",   user_level=12, gender=1, follower=42,   following=320, age=5),
    dict(user_id="u_1006", user_name="江南烟雨客", user_level=66, gender=2, follower=8800, following=22,  age=4),
    dict(user_id="u_1007", user_name="北国清秋",   user_level=8,  gender=1, follower=12,   following=520, age=2),
]

CHATS = [
    ("听了一下午都没腻",          "u_1002"),
    ("主播声音真治愈",            "u_1004"),
    ("这首歌循环单曲",            "u_1003"),
    ("正在做晚饭，背景音正合适",  "u_1005"),
    ("今天的瓷器特写好美",        "u_1001"),
    ("茶汤颜色很漂亮",            "u_1006"),
    ("评论区好友善",              "u_1007"),
    ("终于赶上直播",              "u_1002"),
    ("收藏起来下次再听",          "u_1004"),
    ("画面拍得很有质感",          "u_1001"),
    ("背景插画也很喜欢",          "u_1003"),
    ("第一次来主播间，点关注了",  "u_1007"),
]

GIFTS_PLAN = [
    ("u_1006", "嘉年华",       30000.0, 1),
    ("u_1003", "至尊超跑",     12000.0, 1),
    ("u_1001", "守护团徽章",   100.0,   5),
    ("u_1006", "玫瑰",         1.0,     99),
    ("u_1003", "小心心",       1.0,     200),
    ("u_1001", "小心心",       1.0,     88),
    ("u_1002", "玫瑰",         1.0,     36),
    ("u_1004", "小心心",       1.0,     12),
]

CONTRIBS = [
    ("u_1006", 30099.0, 100, 2, 320),
    ("u_1003", 12200.0, 201, 1, 5800),
    ("u_1001", 588.0,   93,  1, 1200),
    ("u_1002", 36.0,    36,  1, 420),
    ("u_1004", 12.0,    12,  1, 88),
    ("u_1005", 0.0,     0,   1, 60),
    ("u_1007", 0.0,     0,   1, 12),
]


def _user(uid: str) -> dict:
    return next(u for u in USERS if u["user_id"] == uid)


def truncate_test_db(ds: DataService) -> None:
    sess = ds.get_session()
    try:
        sess.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for tbl in [
            "chat_messages",
            "gift_messages",
            "user_contributions",
            "room_stats",
            "system_events",
            "live_sessions",
            "live_rooms",
        ]:
            sess.execute(text(f"TRUNCATE TABLE {tbl}"))
        sess.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        sess.commit()
    finally:
        sess.close()


def seed(ds: DataService) -> None:
    now = get_china_now()
    sess = ds.get_session()
    try:
        # 1) rooms（全部 manual + auto_reconnect=False，避免 scheduler 真去抓抖音）
        for r in ROOMS:
            kw = dict(
                live_id=r["live_id"],
                anchor_name=r["anchor_name"],
                status=r["status"],
                monitor_type="manual",
                auto_reconnect=False,
            )
            if r["status"] == "offline":
                kw["error_message"] = "主播未开播，等待开播中..."
            elif r["status"] == "archived":
                kw["archived_at"] = now - timedelta(days=2)
            sess.add(LiveRoom(**kw))
        sess.commit()

        # 2) 目标房间：进行中的 session
        live = LiveSession(
            live_id=TARGET_LIVE_ID,
            anchor_name=ANCHOR_NAME,
            status="live",
            start_time=now - timedelta(hours=2, minutes=18),
            total_income=42935.0,
            total_gift_count=442,
            total_chat_count=860,
            total_like_count=18420,
            peak_viewer_count=215,
        )
        sess.add(live)
        sess.commit()

        # 3) 弹幕
        for i, (content, uid) in enumerate(CHATS):
            u = _user(uid)
            sess.add(ChatMessage(
                live_id=TARGET_LIVE_ID,
                anchor_name=ANCHOR_NAME,
                live_session_id=live.id,
                user_id=uid,
                user_name=u["user_name"],
                user_level=u["user_level"],
                content=content,
                created_at=now - timedelta(minutes=len(CHATS) - i),
            ))
        sess.commit()

        # 4) 礼物
        for i, (uid, name, price, count) in enumerate(GIFTS_PLAN):
            u = _user(uid)
            sess.add(GiftMessage(
                live_id=TARGET_LIVE_ID,
                anchor_name=ANCHOR_NAME,
                live_session_id=live.id,
                user_id=uid,
                user_name=u["user_name"],
                user_level=u["user_level"],
                gift_name=name,
                gift_count=count,
                gift_price=price,
                total_value=price * count,
                send_type="normal",
                trace_id=f"trace_mock_{i:03d}",
                created_at=now - timedelta(minutes=len(GIFTS_PLAN) - i),
            ))
        sess.commit()

        # 5) 贡献榜（聚合）
        for uid, score, gc, cc, lc in CONTRIBS:
            u = _user(uid)
            sess.add(UserContribution(
                live_id=TARGET_LIVE_ID,
                anchor_name=ANCHOR_NAME,
                user_id=uid,
                user_name=u["user_name"],
                total_score=score,
                gift_count=gc,
                chat_count=cc,
                like_count=lc,
                user_level=u["user_level"],
                gender=u["gender"],
                follower_count=u["follower"],
                following_count=u["following"],
                age_range=u["age"],
            ))
        sess.commit()

        # 6) 补充近 6 天虚构历史场次（让 /stats 趋势图丰富真实）
        history_sessions = [
            ("live_202", "山海图志", "live",   0, 3, 18600.0, 145, 510, 9600, 168),
            ("live_201", "茶语小筑", "ended",  1, 3, 21400.0, 198, 620, 12800, 192),
            ("live_204", "晚风手账", "ended",  2, 3,  9240.0,  76, 340,  5200, 108),
            ("live_201", "茶语小筑", "ended",  3, 2, 15800.0, 130, 480,  8900, 174),
            ("live_202", "山海图志", "ended",  4, 3, 27600.0, 210, 790, 14100, 224),
            ("live_201", "茶语小筑", "ended",  5, 2, 12300.0, 112, 410,  7600, 155),
        ]
        for lid, aname, st, days_ago, hours_len, inc, gc, cc, lc, pv in history_sessions:
            start_dt = (now - timedelta(days=days_ago)).replace(hour=19, minute=30, second=0, microsecond=0)
            sess.add(LiveSession(
                live_id=lid,
                anchor_name=aname,
                status=st,
                start_time=start_dt,
                end_time=None if st == "live" else start_dt + timedelta(hours=hours_len),
                total_income=inc,
                total_gift_count=gc,
                total_chat_count=cc,
                total_like_count=lc,
                peak_viewer_count=pv,
            ))
        sess.commit()
    finally:
        sess.close()


def capture(base_url: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
            color_scheme="light",
        )
        page = ctx.new_page()

        # 登录
        page.goto(f"{base_url}/login")
        page.fill("input[name=username]", os.environ["AUTH_USERNAME"])
        page.fill("input[name=password]", os.environ["AUTH_PASSWORD"])
        page.click("button[type=submit]")
        page.wait_for_url(f"{base_url}/")

        # 01 主页
        page.goto(f"{base_url}/")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT_DIR / "01-home.png"), full_page=True)
        print("[ok] 01-home.png")

        # 02 房间详情
        page.goto(f"{base_url}/room/{TARGET_LIVE_ID}")
        page.wait_for_function(
            "() => { const el = document.querySelector('.messages-container'); return el && el.innerText.includes('听了一下午都没腻'); }"
        )
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT_DIR / "02-room.png"), full_page=True)
        print("[ok] 02-room.png")

        # 03 数据统计：新版页面首屏即自动查询近7天
        page.goto(f"{base_url}/stats")
        page.wait_for_function(
            "() => { const vm = document.querySelector('#app') && document.querySelector('#app').__vue__; return Boolean(vm && vm.hasSearched === true && vm.loading === false); }"
        )
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT_DIR / "03-stats.png"), full_page=True)
        print("[ok] 03-stats.png")

        # 04 数据统计页内搜索（与 README「在统计页输入用户名」一致）
        # 复用 03 已加载的数据状态，只在搜索框输入触发下拉
        search = page.locator('input[data-user-search-input="1"]')
        search.click()
        search.fill("云")
        page.wait_for_timeout(1000)  # 350 debounce + API
        page.screenshot(path=str(OUT_DIR / "04-search.png"), full_page=True)
        print("[ok] 04-search.png")

        # 05 用户消息模态框（点击 04 搜索结果触发）
        page.locator('text=云间野客').first.click()
        page.wait_for_function(
            "() => { const vm = document.querySelector('#app') && document.querySelector('#app').__vue__; return Boolean(vm && vm.showUserMessagesModal === true && vm.userMessagesLoading === false); }"
        )
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT_DIR / "05-user-modal.png"), full_page=False)
        print("[ok] 05-user-modal.png")

        browser.close()


def _start_app_subprocess(port: int) -> subprocess.Popen:
    """启 ephemeral app 进程指向测试库；环境继承自当前进程（已 setdefault 好测试库 + 端口）。"""
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "capture-app.log"
    log_file = log_path.open("ab")
    return subprocess.Popen(
        ["uv", "run", "python", "app.py"],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def _wait_ready(base_url: str, timeout_s: int = 40) -> bool:
    deadline = time.monotonic() + timeout_s
    healthz = f"{base_url}/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(healthz, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1.0)
    return False


def _stop_app_subprocess(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _resolve_test_db_url() -> str:
    """从 TEST_DATABASE_URL 或 tests/.env.test 解析测试库连接串；若 3307 不可达则回退本地 3306。"""
    import socket

    explicit = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if explicit:
        assert "douyin_live_test" in explicit, f"Refusing non-test DB: {explicit}"
        return explicit

    url = (os.environ.get("DATABASE_URL") or "").strip()
    assert "douyin_live_test" in url, f"Refusing non-test DB: {url}"
    if "localhost:3307" in url or "127.0.0.1:3307" in url:
        try:
            with socket.create_connection(("127.0.0.1", 3307), timeout=0.3):
                return url
        except OSError:
            return url.replace("localhost:3307", "127.0.0.1:3306").replace("127.0.0.1:3307", "127.0.0.1:3306")
    return url


def main() -> int:
    # 锁定测试库：不允许写 dev/生产库
    test_db_url = _resolve_test_db_url()
    assert "douyin_live_test" in test_db_url, f"Refusing non-test DB: {test_db_url}"
    os.environ["DATABASE_URL"] = test_db_url

    # 临时端口，避开 dev app 占用的 7654
    port = int(os.environ.get("CAPTURE_PORT", "17654"))
    os.environ["APP_PORT"] = str(port)

    # 截图阶段不连抖音、不走代理、关闭后台测活/备份干扰
    os.environ["DOUYIN_COOKIE"] = ""
    os.environ["PROXY_ENABLED"] = "False"
    os.environ["DEBUG"] = "False"
    os.environ["COOKIE_HEALTH_CHECK_INTERVAL"] = "0"
    os.environ["AUTO_DB_BACKUP_ENABLED"] = "false"
    os.environ["STATUS_DISPLAY_ENABLED"] = "False"

    for required in ("AUTH_USERNAME", "AUTH_PASSWORD", "SECRET_KEY"):
        if not os.environ.get(required):
            sys.stderr.write(f"[capture] 缺少环境变量 {required}（应在项目根 .env 中配置）。\n")
            return 2

    base_url = f"http://localhost:{port}"

    ds = DataService(test_db_url)
    ds.create_tables()
    truncate_test_db(ds)

    # 先启 app 让 RoomManager 跑一遍 _cleanup_stale_statuses（此时表是空的，无副作用）
    # 然后再 seed 数据；如果颠倒顺序，cleanup 会把 mock 的 monitoring 房间重置成 stopped。
    app_proc = _start_app_subprocess(port)
    try:
        if not _wait_ready(base_url, timeout_s=40):
            sys.stderr.write(
                f"[capture] 临时 app 在 40s 内未在 {base_url}/healthz 就绪，详见 logs/capture-app.log\n"
            )
            return 1
        seed(ds)
        capture(base_url)
    finally:
        _stop_app_subprocess(app_proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
