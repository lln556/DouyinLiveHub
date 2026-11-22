"""官方与富文本/贴纸表情解析、下载与落库单元测试。"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from protobuf import douyin as protocol
from services import room_manager as room_manager_module
from services.data_service import DataService
from utils import download_emoji, emoji_helper
from ws_handlers.handlers import WebDouyinLiveFetcher


@pytest.fixture(autouse=True)
def clean_db():
    """Unit tests in this module use SQLite in-memory databases or mocks only."""


@pytest.fixture
def sqlite_fetcher_context():
    service = DataService('sqlite:///:memory:')
    service.create_tables()
    service.create_live_room('emoji_room', anchor_name='表情测试主播')
    live_session = service.create_live_session('emoji_room', anchor_name='表情测试主播', status='live')
    room = room_manager_module.MonitoredRoom(
        'emoji_room', SimpleNamespace(data_service=service))
    room.anchor_name = '表情测试主播'
    socket = MagicMock()
    with patch('crawler.DouyinLiveWebFetcher'):
        fetcher = WebDouyinLiveFetcher('emoji_room', service, socket, room)
        fetcher.current_session_id = live_session.id
        fetcher.log = Mock()
        yield service, live_session, room, socket, fetcher
    service.close_session()


@pytest.mark.parametrize('payload,expected', [
    (b'RIFF\x00\x00\x00\x00WEBPVP8 ', 'webp'),
    (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR', 'png'),
    (b'GIF89a\x01\x00', 'gif'),
    (b'GIF87a\x01\x00', 'gif'),
    (b'\xff\xd8\xff\xe0\x00\x10JFIF', 'jpg'),
    (b'unknown-bytes', 'png'),
])
def test_detect_image_ext(payload, expected):
    assert download_emoji.detect_image_ext(payload) == expected


def test_sanitize_stem_handles_non_ascii_and_empty():
    assert download_emoji.sanitize_stem('weixiao.png', '[微笑]') == 'weixiao'
    assert download_emoji.sanitize_stem('foo/bar-1_2.webp', '[x]') == 'bar-1_2'
    fallback = download_emoji.sanitize_stem('中文名.png', '[中文]')
    assert len(fallback) == 12 and fallback.isalnum()


def test_sync_official_emojis_writes_manifest_and_js_map(tmp_path, monkeypatch):
    emoji_dir = tmp_path / 'emoji_img'
    map_js = tmp_path / 'emoji-map.js'
    fake_fetcher = SimpleNamespace(
        proxies=None,
        user_agent='TestUA',
        ttwid='t',
        request_timeout=(5, 10),
        close=lambda: None,
    )
    fake_list = [
        {
            'origin_uri': 'weixiao.png',
            'display_name': '[微笑]',
            'emoji_url': {'url_list': ['https://p3-pc-sign.douyinpic.com/obj/1']},
        },
        {
            'origin_uri': 'weixiao.png',
            'display_name': '[大笑]',
            'emoji_url': {'url_list': ['https://p3-pc-sign.douyinpic.com/obj/2']},
        },
    ]
    monkeypatch.setattr(download_emoji, 'fetch_official_emoji_list', lambda f: (42, fake_list))
    monkeypatch.setattr(
        download_emoji,
        '_download_emoji_bytes',
        lambda urls, proxies, ua: b'RIFF\x00\x00\x00\x00WEBPVP8 ' if urls[0].endswith('/1') else b'\x89PNG\r\n\x1a\nraw',
    )

    mapping = download_emoji.sync_official_emojis(
        emoji_dir=emoji_dir,
        map_js_path=map_js,
        fetcher=fake_fetcher,
    )

    assert mapping['[微笑]'] == 'weixiao.webp'
    assert mapping['[大笑]'].startswith('weixiao_') and mapping['[大笑]'].endswith('.png')
    assert (emoji_dir / mapping['[微笑]']).read_bytes().startswith(b'RIFF')
    manifest = json.loads((emoji_dir / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest == {'version': 42, 'count': 2, 'emojis': dict(sorted(mapping.items()))}
    js_text = map_js.read_text(encoding='utf-8')
    assert 'window.DOUYIN_EMOJI_MAP = Object.freeze(' in js_text
    assert '"[微笑]": "weixiao.webp"' in js_text


@pytest.mark.parametrize('url,expected', [
    ('https://p3-webcast.douyinpic.com/img/a.webp', True),
    ('https://p9-pc-sign.byteimg.com/tos-cn-i-123/abc', True),
    ('https://sf1-cdn-tos.bytescm.com/obj/a.png', True),
    ('https://douyinpic.com/a.png', True),
    ('http://p3-webcast.douyinpic.com/img/a.webp', False),
    ('javascript:alert(1)', False),
    ('data:image/png;base64,AAAA', False),
    ('https://douyinpic.com.evil.com/img/a.png', False),
    ('https://user:pass@p3-webcast.douyinpic.com/img/a.png', False),
    ('https://p3-webcast.douyinpic.com/img/a.png" onerror="alert(1)', False),
    ('', False),
])
def test_is_safe_douyin_cdn_url(url, expected):
    assert emoji_helper.is_safe_douyin_cdn_url(url) is expected


def test_extract_chat_skips_local_official_and_keeps_custom_rtf_emojis():
    local_names = {'[微笑]', '[比心]'}
    user = protocol.User(id=101, nick_name='测试观众')
    chat = protocol.ChatMessage(
        user=user,
        content='太强了[比心][粉丝牌]',
        rtf_content_v2=protocol.Text(
            pieces_list=[
                protocol.TextPiece(type=2, user_value=protocol.TextPieceUser(user=user, with_colon=True)),
                protocol.TextPiece(type=1, string_value='太强了'),
                protocol.TextPiece(
                    type=15,
                    image_value=protocol.TextPieceImage(
                        image=protocol.Image(
                            url_list_list=['https://p3-webcast.douyinpic.com/img/bixin.webp'],
                            content=protocol.ImageContent(alternative_text='[比心]'),
                        )
                    ),
                ),
                protocol.TextPiece(
                    type=15,
                    image_value=protocol.TextPieceImage(
                        image=protocol.Image(
                            url_list_list=['https://p3-webcast.douyinpic.com/img/badge.png'],
                            content=protocol.ImageContent(alternative_text='粉丝牌'),
                        )
                    ),
                ),
                protocol.TextPiece(
                    type=15,
                    image_value=protocol.TextPieceImage(
                        image=protocol.Image(
                            url_list_list=['https://evil.example.com/bad.png'],
                            content=protocol.ImageContent(alternative_text='[恶意]'),
                        )
                    ),
                ),
            ]
        ),
    )

    content, emoji_list = emoji_helper.extract_chat_content_and_emojis(chat, local_names=local_names)
    assert content == '太强了[比心][粉丝牌]'
    assert emoji_list == [{'alt': '[粉丝牌]', 'url': 'https://p3-webcast.douyinpic.com/img/badge.png'}]


def test_extract_chat_plain_and_reconstruct_empty_content():
    plain = protocol.ChatMessage(user=protocol.User(id=1, nick_name='u'), content='纯文本[微笑]')
    assert emoji_helper.extract_chat_content_and_emojis(plain, local_names={'[微笑]'}) == ('纯文本[微笑]', None)

    empty_content = protocol.ChatMessage(
        user=protocol.User(id=2, nick_name='u2'),
        content='',
        rtf_content=protocol.Text(
            pieces_list=[
                protocol.TextPiece(type=1, string_value='好听'),
                protocol.TextPiece(
                    type=15,
                    image_value=protocol.TextPieceImage(
                        image=protocol.Image(
                            url_list_list=['https://p3-webcast.douyinpic.com/img/vip.webp'],
                            content=protocol.ImageContent(alternative_text='[VIP打call]'),
                        )
                    ),
                ),
            ]
        ),
    )
    content, emoji_list = emoji_helper.extract_chat_content_and_emojis(empty_content, local_names=set())
    assert content == '好听[VIP打call]'
    assert emoji_list == [{'alt': '[VIP打call]', 'url': 'https://p3-webcast.douyinpic.com/img/vip.webp'}]


def test_extract_emoji_chat_message_sticker():
    emoji_msg = protocol.EmojiChatMessage(
        user=protocol.User(id=9, nick_name='表情达人'),
        emoji_id=888,
        default_content='[冲鸭]',
        emoji_content=protocol.Text(
            pieces_list=[
                protocol.TextPiece(
                    type=15,
                    image_value=protocol.TextPieceImage(
                        image=protocol.Image(
                            url_list_list=['https://p11-webcast.douyinpic.com/img/sticker.webp']
                        )
                    ),
                )
            ]
        ),
    )
    content, emoji_list = emoji_helper.extract_emoji_chat_message(emoji_msg)
    assert content == '[冲鸭]'
    assert emoji_list == [{
        'alt': '[冲鸭]',
        'url': 'https://p11-webcast.douyinpic.com/img/sticker.webp',
        'sticker': True,
    }]


def test_fetcher_handles_rtf_and_emoji_chat_end_to_end(sqlite_fetcher_context):
    service, live_session, _, socket, fetcher = sqlite_fetcher_context
    user = protocol.User(id=2001, id_str='2001', nick_name='贴纸观众')

    chat_msg = protocol.ChatMessage(
        user=user,
        content='打call[专属表情]',
        rtf_content_v2=protocol.Text(
            pieces_list=[
                protocol.TextPiece(
                    type=15,
                    image_value=protocol.TextPieceImage(
                        image=protocol.Image(
                            url_list_list=['https://p3-webcast.douyinpic.com/img/exclusive.png'],
                            content=protocol.ImageContent(alternative_text='[专属表情]'),
                        )
                    ),
                )
            ]
        ),
    )
    emoji_msg = protocol.EmojiChatMessage(
        user=user,
        emoji_id=321,
        default_content='[开心大笑]',
        emoji_content=protocol.Text(
            pieces_list=[
                protocol.TextPiece(
                    type=15,
                    image_value=protocol.TextPieceImage(
                        image=protocol.Image(
                            url_list_list=['https://p3-webcast.douyinpic.com/img/big_laugh.webp']
                        )
                    ),
                )
            ]
        ),
    )

    response = protocol.Response(
        messages_list=[
            protocol.Message(method='WebcastChatMessage', payload=bytes(chat_msg)),
            protocol.Message(method='WebcastEmojiChatMessage', payload=bytes(emoji_msg)),
        ]
    )
    fetcher._wsOnMessage(MagicMock(), bytes(protocol.PushFrame(payload=bytes(response))))

    emitted_chats = [
        call.args[1] for call in socket.emit.call_args_list
        if call.args[0] == 'room_emoji_room'
    ]
    assert len(emitted_chats) == 2
    assert emitted_chats[0]['text'] == '打call[专属表情]'
    assert emitted_chats[0]['emoji_list'] == [{
        'alt': '[专属表情]',
        'url': 'https://p3-webcast.douyinpic.com/img/exclusive.png',
    }]
    assert emitted_chats[1]['text'] == '[开心大笑]'
    assert emitted_chats[1]['emoji_list'] == [{
        'alt': '[开心大笑]',
        'url': 'https://p3-webcast.douyinpic.com/img/big_laugh.webp',
        'sticker': True,
    }]

    all_msgs = service.get_all_messages('emoji_room', limit=10)
    session_msgs = service.get_session_messages(live_session.id, 'chat', limit=10)
    user_msgs = service.get_user_messages(live_id='emoji_room', user_id='2001', message_type='chat')
    assert len(all_msgs) == 2
    assert len(session_msgs) == 2
    assert len(user_msgs['messages']) == 2
    assert any(m['emoji_list'] and m['emoji_list'][0].get('sticker') for m in session_msgs)
    assert any(m['emoji_list'] and m['emoji_list'][0]['alt'] == '[专属表情]' for m in user_msgs['messages'])
