"""API 集成测试：官方表情静态资源与弹幕 emoji_list 序列化。"""
import pytest

pytestmark = pytest.mark.integration


def test_official_emoji_static_assets_served(api_client):
    map_resp = api_client.get('/static/js/emoji-map.js')
    assert map_resp.status == 200
    assert 'window.DOUYIN_EMOJI_MAP' in map_resp.text()
    assert '[微笑]' in map_resp.text()

    img_resp = api_client.get('/emoji_img/weixiao.webp')
    assert img_resp.status == 200


def test_chat_messages_apis_expose_emoji_list(api_client, factories):
    live_id = 'emoji-api-room'
    factories.room(live_id=live_id, anchor_name='表情接口主播')
    session = factories.session_(live_id=live_id, status='ended')
    custom_emojis = [{'alt': '[粉丝勋章]', 'url': 'https://p3-webcast.douyinpic.com/img/fan_badge.webp'}]

    factories.chat(
        live_id=live_id,
        live_session_id=session.id,
        user_id='u_emoji_1',
        user_name='测试观众',
        content='真棒[微笑][粉丝勋章]',
        emoji_list=custom_emojis,
    )
    factories.chat(
        live_id=live_id,
        live_session_id=session.id,
        user_id='u_emoji_1',
        user_name='测试观众',
        content='普通弹幕[比心]',
    )

    for url in (
        f'/api/rooms/{live_id}/messages?type=chat',
        f'/api/rooms/{live_id}/messages?type=all&session_id={session.id}',
    ):
        resp = api_client.get(url)
        assert resp.status == 200
        msgs = {m['text'] if 'text' in m and m['text'] else m['content']: m for m in resp.json()['messages']}
        assert msgs['真棒[微笑][粉丝勋章]']['emoji_list'] == custom_emojis
        assert msgs['普通弹幕[比心]']['emoji_list'] is None

    sess_resp = api_client.get(f'/api/rooms/sessions/{session.id}?type=chat')
    assert sess_resp.status == 200
    chats = {c['content']: c for c in sess_resp.json()['chats']}
    assert chats['真棒[微笑][粉丝勋章]']['emoji_list'] == custom_emojis
    assert chats['普通弹幕[比心]']['emoji_list'] is None

    user_resp = api_client.get(f'/api/rooms/{live_id}/user-messages?user_id=u_emoji_1&type=chat')
    assert user_resp.status == 200
    user_chats = {m['content']: m for m in user_resp.json()['messages']}
    assert user_chats['真棒[微笑][粉丝勋章]']['emoji_list'] == custom_emojis
    assert user_chats['普通弹幕[比心]']['emoji_list'] is None
