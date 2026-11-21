"""Offline sampler contracts; all sample files and accounting use isolated storage."""
import base64
import importlib
import json
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import config
from protobuf import douyin as protocol
from ws_handlers import handlers


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    """Override the global MySQL cleanup and isolate runtime sampler settings."""
    for key in ('GIFT_PACKET_SAMPLE_FILE', 'GIFT_PACKET_SAMPLING_ENABLED',
                'GIFT_PACKET_SAMPLE_MAX_BYTES', 'GIFT_PACKET_SAMPLE_PER_KEY_LIMIT',
                'GIFT_PRICE_OVERRIDES'):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def sampler_module():
    assert importlib.util.find_spec('utils.gift_packet_sampler'), 'sampler is missing'
    return importlib.import_module('utils.gift_packet_sampler')


@pytest.fixture
def sampler(sampler_module, tmp_path):
    return sampler_module.GiftPacketSampler(sample_file=tmp_path / 'gift_packet_samples.jsonl')


def gift(name='幻翼超跑', describe='送出甜心跑车', **kwargs):
    fields = dict(gift_id=16179, group_count=1,
                  gift=protocol.GiftStruct(id=16179, name=name, describe=describe, diamond_count=1200))
    fields.update(kwargs)
    return protocol.GiftMessage(**fields)


def sample(sampler, message=None, raw=None, method='WebcastGiftMessage', source='base_diamond'):
    message = message if message is not None else gift()
    sampler.sample_gift_message(method, bytes(message) if raw is None else raw, message, 1200, source)


def records(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(tag, value, wire=2):
    if isinstance(value, str):
        value = value.encode()
    body = varint(value) if wire == 0 else (varint(len(value)) + value if wire == 2 else value)
    return varint((tag << 3) | wire) + body


def tags(payload):
    """Independent flat tag reader for checking re-encoded packet contents."""
    result = {}
    offset = 0

    def read():
        nonlocal offset
        value = shift = 0
        while True:
            byte = payload[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7

    while offset < len(payload):
        key = read()
        tag, wire = key >> 3, key & 7
        if wire == 0:
            value = read()
        else:
            size = read() if wire == 2 else {1: 8, 5: 4}[wire]
            value = payload[offset:offset + size]
            offset += size
        result.setdefault(tag, []).append(value)
    return result


@pytest.mark.parametrize('name', ['小心心', '粉丝团灯牌'])
def test_ordinary_gifts_do_not_create_sample_file(sampler, tmp_path, name):
    sample(sampler, gift(name=name, describe=f'送出{name}'))
    assert not list(tmp_path.iterdir())


def test_disguised_shared_id_keeps_verified_base_price(sampler, tmp_path):
    sample(sampler)
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    assert 'disguised_name' in record['triggers']
    assert record['gift_id'] == '16179'
    assert record['gift_name'] == '幻翼超跑'
    assert record['gift_describe'] == '送出甜心跑车'
    assert record['base_diamond'] == record['resolved_price'] == 1200
    assert record['price_source'] == 'base_diamond'


@pytest.mark.parametrize('nickname', ['a', 'id', 're', '超', '幻翼', 'sampled_at'])
def test_short_identities_preserve_schema_gift_metadata_and_price(sampler, tmp_path, nickname):
    message = gift(user=protocol.User(nick_name=nickname, id=1, id_str='12'),
                   common=protocol.Common(msg_id=16),
                   gift_play_param='{"data":{"real_diamond_count":36000}}')
    sample(sampler, message)
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    assert datetime.fromisoformat(record['sampled_at']).tzinfo is not None
    assert record['method'] == 'WebcastGiftMessage'
    assert 'disguised_name' in record['triggers']
    assert record['gift_id'] == '16179'
    assert record['gift_name'] == '幻翼超跑'
    assert record['gift_describe'] == '送出甜心跑车'
    assert record['gift_type'] == 0
    assert record['base_diamond'] == record['resolved_price'] == 1200
    assert record['price_source'] == 'base_diamond'
    assert record['buff_level'] == 0 and record['buff_infos'] == []
    assert json.loads(record['extra_json']['gift_play_param'])['data']['real_diamond_count'] == 36000
    assert not ({'1', '7'} & set(record['wire_fields']))
    assert record['wire_fields']['15']['2'] == '送出甜心跑车'
    assert record['wire_fields']['15']['16'] == '幻翼超跑'
    scrubbed_tags = tags(base64.b64decode(record['scrubbed_payload_b64']))
    assert not ({1, 7} & set(scrubbed_tags))
    assert tags(scrubbed_tags[15][0])[16] == ['幻翼超跑'.encode()]


def test_message_secret_thresholds_and_gift_text_exclusions(sampler_module):
    child = protocol.GiftStruct(id=123456, name='进阶超跑', describe='送出进阶超跑')
    message = gift(user=protocol.User(nick_name='幻翼超跑', id=1, id_str='12', sec_uid='  abcd  '),
                   to_user=protocol.User(nick_name='私密', id_str='123456'),
                   common=protocol.Common(msg_id=16, room_id=654321, describe='私密观众送出甜心跑车'),
                   trace_id=' re ', log_id=' log-secret ',
                   series_gift_data=[protocol.SeriesPlayGift(gift_struct=child)])
    secrets = sampler_module._message_secrets(message)
    assert {'abcd', '私密', '654321', '私密观众', 'log-secret'} <= set(secrets)
    assert not ({'1', '12', '16', 're', '幻翼超跑', '送出甜心跑车', '16179',
                 '123456', '进阶超跑', '送出进阶超跑'} & set(secrets))


@pytest.mark.parametrize('prefix,expected', [('a', False), ('id', False), ('超', False), ('私密', True)])
def test_common_description_residual_must_meet_secret_thresholds(sampler_module, prefix, expected):
    message = gift(common=protocol.Common(describe=f'{prefix}送出甜心跑车'))
    secrets = sampler_module._message_secrets(message)
    assert (prefix in secrets) is expected
    assert '送出甜心跑车' not in secrets


def test_short_unicode_secrets_do_not_scrub_dictionary_keys(sampler_module):
    result = sampler_module._safe({'超跑档位': 2, 'note': '超跑观众'}, ('超跑',))
    assert result == {'超跑档位': 2, 'note': '<redacted_identity>'}
    assert sampler_module._safe({'private_note': 'ok'}, ('private',)) == {'<redacted_identity>': 'ok'}


def test_series_summary_and_wire_preserve_child_gift_names(sampler, tmp_path):
    child = protocol.GiftStruct(id=123456, name='进阶超跑', describe='送出进阶超跑', diamond_count=36000)
    message = gift(user=protocol.User(nick_name='超跑', id_str='123456'),
                   common=protocol.Common(describe='送出进阶超跑'),
                   series_gift_data=[protocol.SeriesPlayGift(
                       gift_struct=child, diy_item_info='{"data":{"real_diamond_count":36000}}')])
    sample(sampler, message, source='series_children')
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    summary = record['series_items'][0]
    assert summary['gift_id'] == '123456' and summary['gift_name'] == '进阶超跑'
    assert summary['gift_describe'] == '送出进阶超跑' and summary['base_diamond'] == 36000
    assert json.loads(summary['diy_item_info'])['data']['real_diamond_count'] == 36000
    assert record['wire_fields']['42']['1']['16'] == '进阶超跑'
    top = tags(base64.b64decode(record['scrubbed_payload_b64']))
    assert tags(tags(top[42][0])[1][0])[16] == ['进阶超跑'.encode()]


def test_pii_removed_unknown_wire_tags_and_gift_metadata_retained(sampler, tmp_path):
    image = protocol.Image(url_list_list=['https://douyinpic/private-avatar'])
    message = gift(
        user=protocol.User(nick_name='私密观众', id_str='999888', sec_uid='MS4wLjABsecret'),
        to_user=protocol.User(nick_name='私密主播'),
        common=protocol.Common(describe='私密观众送出', msg_id=887766, room_id=556677),
        trace_id='secret-trace', log_id='secret-log', to_user_ids_list=[999888],
    )
    message.gift.image = message.gift.icon = message.gift.gift_label_icon = image
    buff = protocol.GiftBuffInfo(buff_level=2, buff_diamond_count=28888)
    message.gift.gift_buff_infos = [buff]
    inner = bytes(message.gift) + field(46, 36000, 0) + field(47, '{"skin_price":36000}')
    raw = (bytes(message) + field(15, inner) + field(48, 28888, 0)
           + field(49, '{"mystery_price":36000}')
           + field(50, (123).to_bytes(8, 'little'), 1)
           + field(51, (456).to_bytes(4, 'little'), 5))
    sample(sampler, message, raw)
    text = (tmp_path / 'gift_packet_samples.jsonl').read_text(encoding='utf-8')
    record = json.loads(text)
    scrubbed = base64.b64decode(record['scrubbed_payload_b64'])
    for secret in ('私密观众', '私密主播', '999888', 'MS4wLjABsecret', 'secret-trace',
                   'secret-log', 'douyinpic', 'private-avatar'):
        assert secret not in text
        assert secret.encode() not in scrubbed
    for key in ('live_id', 'room_id', 'anchor_name', 'anchor_id', 'user_id', 'user_name',
                'sec_uid', 'log_id', 'trace_id', 'msg_id'):
        assert key not in text
    wire = record['wire_fields']
    assert wire['48'] == 28888
    assert wire['49'] == '{"mystery_price":36000}'
    assert wire['50'] == 123 and wire['51'] == 456
    assert wire['15'][-1]['46'] == 36000
    assert wire['15'][-1]['47'] == '{"skin_price":36000}'
    assert wire['15'][-1]['45']['9'] == 28888
    top = tags(scrubbed)
    assert not (set(top) & {1, 7, 8, 10, 16, 18, 19, 32, 35})
    assert top[48] == [28888] and top[49] == [b'{"mystery_price":36000}']
    for struct in top[15]:
        assert not (set(tags(struct)) & {1, 15, 21})


def test_binding_and_series_scrub_outer_common_and_child_images(sampler, tmp_path):
    child = protocol.GiftStruct(id=8, name='子礼物', diamond_count=99,
                                image=protocol.Image(uri='private-child-image'))
    message = gift(series_gift_data=[protocol.SeriesPlayGift(gift_struct=child)])
    binding = protocol.BindingGiftMessage(msg=message, common=protocol.Common(describe='私密外层'))
    sample(sampler, message, bytes(binding), method='WebcastBindingGiftMessage', source='series_children')
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    assert {'binding_gift', 'series_gift_data'} <= set(record['triggers'])
    assert record['series_items'][0]['gift_name'] == '子礼物'
    scrubbed = base64.b64decode(record['scrubbed_payload_b64'])
    assert b'private-child-image' not in scrubbed and '私密外层'.encode() not in scrubbed
    assert 2 in tags(scrubbed)  # Inner GiftMessage, not the BindingGiftMessage envelope.
    child_tags = tags(tags(tags(scrubbed)[42][0])[1][0])
    assert not (set(child_tags) & {1, 15, 21})
    assert record['wire_fields']['42']['1']['12'] == 99


@pytest.mark.parametrize('trigger,changes,source', [
    ('binding_gift', {}, 'base_diamond'),
    ('series_gift_data', {'series_gift_data': [protocol.SeriesPlayGift()]}, 'base_diamond'),
    ('buff_or_tray', {'tray_info': protocol.GiftTrayInfo(buff_level=2)}, 'base_diamond'),
    ('buff_or_tray', {'tray_info': protocol.GiftTrayInfo(origin_gift_id=2)}, 'base_diamond'),
    ('buff_or_tray', {'tray_info': protocol.GiftTrayInfo(toolbar_card_name='进阶卡')}, 'base_diamond'),
    ('extra_json', {'interact_gift_info': '{}'}, 'base_diamond'),
    ('extra_json', {'diy_item_info': '{}'}, 'base_diamond'),
    ('extra_json', {'gift_play_param': '{}'}, 'base_diamond'),
    ('price_override', {}, 'env_override'),
    ('price_override', {}, 'builtin_override'),
    ('price_override', {}, 'override'),
])
def test_individual_triggers(sampler, tmp_path, trigger, changes, source):
    method = 'WebcastBindingGiftMessage' if trigger == 'binding_gift' else 'WebcastGiftMessage'
    message = gift(name='普通礼物', describe='送出普通礼物', **changes)
    raw = bytes(protocol.BindingGiftMessage(msg=message)) if trigger == 'binding_gift' else bytes(message)
    sample(sampler, message, raw, method=method, source=source)
    assert trigger in records(tmp_path / 'gift_packet_samples.jsonl')[0]['triggers']


@pytest.mark.parametrize('name', ['盲盒', '宝箱', '邮箱', '福袋', '锦盒', '宝盒', '魔盒', '超跑', '追光'])
def test_box_or_special_names(sampler, tmp_path, name):
    sample(sampler, gift(name=name, describe=f'送出{name}'))
    assert 'box_or_special_type' in records(tmp_path / 'gift_packet_samples.jsonl')[0]['triggers']


def test_special_type_and_buff_infos(sampler, tmp_path):
    message = gift(name='普通礼物', describe='送出普通礼物')
    message.gift.type = 13
    message.gift.gift_buff_infos = [protocol.GiftBuffInfo(buff_level=2)]
    sample(sampler, message)
    assert {'box_or_special_type', 'buff_or_tray'} <= set(records(tmp_path / 'gift_packet_samples.jsonl')[0]['triggers'])


def test_combo_does_not_exhaust_five_sample_quota(sampler, tmp_path):
    sample(sampler, gift(combo_count=1))
    for count in range(2, 20):
        sample(sampler, gift(combo_count=count))
    assert len(records(tmp_path / 'gift_packet_samples.jsonl')) == 1
    for _ in range(10):
        sample(sampler, gift(combo_count=1))
    assert len(records(tmp_path / 'gift_packet_samples.jsonl')) == 5


def test_quota_is_thread_safe(sampler, tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: sample(sampler), range(40)))
    assert len(records(tmp_path / 'gift_packet_samples.jsonl')) == 5


def test_signature_cache_is_bounded(sampler_module, tmp_path):
    path = tmp_path / 'samples.jsonl'
    sampler = sampler_module.GiftPacketSampler(sample_file=path, per_key_limit=1)
    for index in range(513):
        sample(sampler, gift(name=f'盲盒{index}'))
    sample(sampler, gift(name='盲盒0'))
    assert len(records(path)) == 514


def test_rotation_uses_atomic_replace_and_utf8_byte_size(sampler_module, tmp_path, monkeypatch):
    path = tmp_path / 'nested' / 'samples.jsonl'
    sampler = sampler_module.GiftPacketSampler(sample_file=path)
    sample(sampler)
    first = path.read_bytes()
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_MAX_BYTES', str(len(first) + 8))
    with patch.object(sampler_module.os, 'replace', wraps=sampler_module.os.replace) as replace:
        sample(sampler)
    assert replace.call_count == 1
    assert path.with_name('samples.jsonl.1').read_bytes() == first
    assert len(records(path)) == 1


def test_default_pytest_sampler_never_writes_even_after_environment_changes(sampler_module, tmp_path, monkeypatch):
    default = sampler_module.GiftPacketSampler()
    path = tmp_path / 'accidental.jsonl'
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_FILE', str(path))
    sample(default)
    default.sample_unhandled_method('WebcastGiftAIGCEffectMessage', field(2, 9, 0))
    assert not path.exists()


def test_dynamic_settings_and_empty_environment_fallback(sampler_module, tmp_path, monkeypatch):
    path = tmp_path / 'environment.jsonl'
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_FILE', str(path))
    sampler = sampler_module.GiftPacketSampler()
    monkeypatch.setenv('GIFT_PACKET_SAMPLING_ENABLED', 'False')
    sample(sampler)
    assert not path.exists()
    monkeypatch.setenv('GIFT_PACKET_SAMPLING_ENABLED', 'on')
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_PER_KEY_LIMIT', '1')
    sample(sampler)
    sample(sampler)
    assert len(records(path)) == 1
    fallback = tmp_path / 'fallback.jsonl'
    monkeypatch.setattr(config, 'GIFT_PACKET_SAMPLE_FILE', str(fallback))
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_FILE', '')
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_PER_KEY_LIMIT', '')
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_MAX_BYTES', '')
    monkeypatch.setenv('GIFT_PACKET_SAMPLING_ENABLED', '')
    sample(sampler, gift(name='盲盒新键'))
    assert fallback.exists()
    for name in ('GIFT_PACKET_SAMPLING_ENABLED', 'GIFT_PACKET_SAMPLE_FILE',
                 'GIFT_PACKET_SAMPLE_MAX_BYTES', 'GIFT_PACKET_SAMPLE_PER_KEY_LIMIT'):
        assert hasattr(config.Config, name)


def test_runtime_environment_path_takes_precedence_over_constructor(sampler, tmp_path, monkeypatch):
    runtime_path = tmp_path / 'runtime' / 'samples.jsonl'
    monkeypatch.setenv('GIFT_PACKET_SAMPLE_FILE', str(runtime_path))
    sample(sampler)
    assert runtime_path.exists()
    assert not (tmp_path / 'gift_packet_samples.jsonl').exists()


@pytest.mark.parametrize('raw', [b'\x80', b'\x00', b'\x0b', b'\x12\xff', b'\x09\x00', b'\x15\x00'])
def test_malformed_wire_is_safe(sampler, tmp_path, raw):
    sample(sampler, raw=raw)
    sampler.sample_unhandled_method('WebcastMagicGiftMessage', raw)
    for record in records(tmp_path / 'gift_packet_samples.jsonl'):
        assert isinstance(record['wire_fields'], dict)


def test_urls_tokens_and_identity_json_are_scrubbed_in_both_outputs(sampler, tmp_path):
    message = gift(gift_play_param='{"user_id":"999888","data":{"real_diamond_count":36000}}')
    raw = bytes(message) + field(49, 'https://example/private') + field(50, 'MS4wLjABprivate')
    sample(sampler, message, raw)
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    assert record['wire_fields']['49'] == '<redacted_url_or_token>'
    assert record['wire_fields']['50'] == '<redacted_url_or_token>'
    for content in (json.dumps(record), base64.b64decode(record['scrubbed_payload_b64']).decode(errors='ignore')):
        assert 'https://' not in content and 'MS4wLjAB' not in content
        assert 'user_id' not in content and '999888' not in content


def test_json_identity_keys_and_sensitive_url_keys_cannot_leak(sampler, tmp_path):
    extra = json.dumps({
        'diagnostic.trace_id': 'private-trace', 'UserId': '999888',
        'https://example/private-key': 'MS4wLjABsecret',
        'data': {'real_diamond_count': 36000},
    })
    message = gift(gift_play_param=extra)
    sample(sampler, message)
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    for content in (json.dumps(record), base64.b64decode(record['scrubbed_payload_b64']).decode(errors='ignore')):
        for secret in ('trace_id', 'UserId', '999888', 'private-trace', 'https://', 'MS4wLjAB'):
            assert secret not in content
    assert json.loads(record['wire_fields']['40'])['data']['real_diamond_count'] == 36000


def test_scrubbing_survives_copies_of_known_identity_in_extra_metadata(sampler, tmp_path):
    message = gift(user=protocol.User(nick_name='私密观众', id_str='999888'),
                   gift_play_param='{"data":{"note":"私密观众 999888","real_diamond_count":36000}}')
    sample(sampler, message)
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    for content in (json.dumps(record, ensure_ascii=False), base64.b64decode(record['scrubbed_payload_b64']).decode(errors='ignore')):
        assert '私密观众' not in content and '999888' not in content
    assert json.loads(record['wire_fields']['40'])['data']['real_diamond_count'] == 36000


def test_large_payload_omits_base64(sampler, tmp_path):
    sample(sampler, gift(gift_play_param='x' * 9000))
    assert 'scrubbed_payload_b64' not in records(tmp_path / 'gift_packet_samples.jsonl')[0]


@pytest.fixture
def accounting_context():
    from models.database import GiftMessage as StoredGift, LiveSession
    from services.data_service import DataService
    from services.room_manager import MonitoredRoom

    service = DataService('sqlite:///:memory:')
    service.create_tables()
    service.create_live_room('room', status='monitoring')
    session = service.create_live_session('room', status='live')
    room = MonitoredRoom('room', SimpleNamespace(data_service=service))
    with patch('crawler.DouyinLiveWebFetcher'):
        fetcher = handlers.WebDouyinLiveFetcher('room', service, MagicMock(), room)
        fetcher.log = MagicMock()
        yield fetcher, service, session.id, StoredGift, LiveSession
    service.close_session()


@pytest.mark.parametrize('method', ['WebcastGiftAIGCEffectMessage', 'WebcastInteractOpenRewardMessage'])
def test_unhandled_websocket_methods_scrub_common_and_media(accounting_context, sampler, tmp_path, monkeypatch, method):
    fetcher, *_ = accounting_context
    monkeypatch.setattr(handlers, 'gift_packet_sampler', sampler)
    raw = (field(1, bytes(protocol.Common(describe='私密观众送出', room_id=999888)))
           + field(2, 'douyinpic/private') + field(3, 'MS4wLjABsecret')
           + field(4, 'https://example/private') + field(48, 28888, 0))
    response = protocol.Response(messages_list=[protocol.Message(method=method, payload=raw)])
    fetcher._wsOnMessage(MagicMock(), bytes(protocol.PushFrame(payload=bytes(response))))
    record = records(tmp_path / 'gift_packet_samples.jsonl')[0]
    assert record['triggers'] == ['unhandled_gift_method']
    assert record['method'] == method and '1' not in record['wire_fields']
    assert record['wire_fields']['2'] == record['wire_fields']['3'] == '<redacted_user_or_media>'
    assert record['wire_fields']['4'] == '<redacted_url_or_token>'
    assert record['wire_fields']['48'] == 28888
    assert 'scrubbed_payload_b64' not in record
    assert '私密观众' not in json.dumps(record, ensure_ascii=False)


@pytest.mark.parametrize('method', ['', 'WebcastMemberMessage', 'WebcastChatMessage', 'WebcastGiftMessage',
                                  'WebcastBindingGiftMessage', 'WebcastLikeMessage',
                                  'WebcastRoomUserSeqMessage', 'WebcastControlMessage'])
def test_unhandled_sampler_ignores_non_gift_and_known_methods(sampler, tmp_path, method):
    sampler.sample_unhandled_method(method, field(2, 9, 0))
    assert not list(tmp_path.iterdir())


def test_websocket_member_is_ignored(accounting_context, sampler, tmp_path, monkeypatch):
    fetcher, *_ = accounting_context
    monkeypatch.setattr(handlers, 'gift_packet_sampler', sampler)
    response = protocol.Response(messages_list=[protocol.Message(method='WebcastMemberMessage', payload=field(2, 9, 0))])
    fetcher._wsOnMessage(MagicMock(), bytes(protocol.PushFrame(payload=bytes(response))))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('method', ['WebcastGiftMessage', 'WebcastBindingGiftMessage'])
def test_websocket_passes_original_payload_and_skips_remembered_replay(accounting_context, sampler, tmp_path, monkeypatch, method):
    fetcher, service, session_id, StoredGift, _ = accounting_context
    monkeypatch.setattr(handlers, 'gift_packet_sampler', sampler)
    message = gift(user=protocol.User(id_str='viewer-id', nick_name='viewer'), trace_id='dedupe')
    payload = bytes(protocol.BindingGiftMessage(msg=message)) if 'Binding' in method else bytes(message)
    payload += field(48, 28888, 0)
    response = protocol.Response(messages_list=[protocol.Message(method=method, payload=payload)])
    packet = bytes(protocol.PushFrame(payload=bytes(response)))
    fetcher._wsOnMessage(MagicMock(), packet)
    fetcher._wsOnMessage(MagicMock(), packet)
    output = records(tmp_path / 'gift_packet_samples.jsonl')
    assert len(output) == 1 and output[0]['method'] == method
    if method == 'WebcastGiftMessage':
        assert output[0]['wire_fields']['48'] == 28888
    with service.get_session() as db:
        assert db.query(StoredGift).count() == 1


@pytest.mark.parametrize('failure_location', ['writer', 'public_method'])
def test_sampling_failure_cannot_interrupt_persistence_or_accounting(accounting_context, sampler, monkeypatch, failure_location):
    fetcher, service, session_id, StoredGift, LiveSession = accounting_context
    monkeypatch.setattr(handlers, 'gift_packet_sampler', sampler)
    message = gift(user=protocol.User(id_str='viewer-id', nick_name='viewer'), trace_id='failure-safe', group_count=2)
    if failure_location == 'writer':
        monkeypatch.setattr('pathlib.Path.open', MagicMock(side_effect=OSError('sample disk failure')))
    else:
        monkeypatch.setattr(sampler, 'sample_gift_message', MagicMock(side_effect=RuntimeError('sampler failure')))
    fetcher._handle_gift_message(message, raw_payload=bytes(message))
    with service.get_session() as db:
        stored = db.query(StoredGift).one()
        session = db.get(LiveSession, session_id)
        assert stored.total_value == session.total_income == 2400
        assert stored.gift_count == session.total_gift_count == 2
    assert fetcher.total_income == fetcher.monitored_room.stats['total_income'] == 2400
    assert service.get_user_contribution('room', 'viewer-id').total_score == 2400


def test_gift_sampling_runs_without_holding_room_data_lock(accounting_context, sampler, monkeypatch):
    class ObservableRLock:
        """Keep the production lock's reentrancy with a portable locked() probe."""
        def __init__(self):
            self._lock = threading.RLock()
            self._depth = 0

        def __enter__(self):
            self._lock.acquire()
            self._depth += 1
            return self

        def __exit__(self, *args):
            self._depth -= 1
            self._lock.release()

        def locked(self):
            return self._depth > 0

    fetcher, service, session_id, StoredGift, LiveSession = accounting_context
    monkeypatch.setattr(fetcher.monitored_room, 'data_lock', ObservableRLock())
    monkeypatch.setattr(handlers, 'gift_packet_sampler', sampler)
    observed = []
    original = sampler.sample_gift_message

    def sampling(*args, **kwargs):
        observed.append(fetcher.monitored_room.data_lock.locked())
        return original(*args, **kwargs)

    monkeypatch.setattr(sampler, 'sample_gift_message', sampling)
    message = gift(user=protocol.User(id_str='viewer-id', nick_name='viewer'), trace_id='lock-test')
    fetcher._handle_gift_message(message, raw_payload=bytes(message))
    assert observed == [False]
    with service.get_session() as db:
        assert db.query(StoredGift).one().total_value == db.get(LiveSession, session_id).total_income == 1200


@pytest.mark.parametrize('name,tray,extra,expected', [
    ('幻翼超跑', None, (), (1200.0, 'base_diamond')),
    ('至尊超跑', None, (), (12000.0, 'builtin_override')),
    ('幻翼超跑', protocol.GiftTrayInfo(buff_level=2), (), (28888.0, 'buff_level')),
    ('幻翼超跑', None, ('{"real_diamond_count":36000}',), (36000.0, 'extra_json')),
])
def test_price_source_preserves_existing_resolution(name, tray, extra, expected):
    resolver = getattr(handlers.WebDouyinLiveFetcher, '_resolve_gift_unit_price_with_source', None)
    assert callable(resolver), 'price source resolver is missing'
    struct = protocol.GiftStruct(name=name, diamond_count=1200,
                                 gift_buff_infos=[protocol.GiftBuffInfo(buff_level=2, buff_diamond_count=28888)])
    assert resolver(struct, tray, extra) == expected
    assert handlers.WebDouyinLiveFetcher._resolve_gift_unit_price(struct, tray, extra) == expected[0]


def test_environment_override_source_for_equal_builtin_and_configured_prices(monkeypatch):
    resolver = getattr(handlers.WebDouyinLiveFetcher, '_resolve_gift_unit_price_with_source', None)
    assert callable(resolver), 'price source resolver is missing'
    monkeypatch.setenv('GIFT_PRICE_OVERRIDES', '{"至尊超跑":12000,"幻翼超跑":1500}')
    assert resolver(protocol.GiftStruct(name='至尊超跑')) == (12000.0, 'env_override')
    assert resolver(protocol.GiftStruct(name='幻翼超跑')) == (1500.0, 'env_override')
    assert resolver(protocol.GiftStruct(name='无界超跑')) == (36000.0, 'builtin_override')
