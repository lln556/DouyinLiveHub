"""官方礼物目录与 AI 盒子计价回归；仅使用虚构身份和隔离 SQLite。"""
import importlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import config
from models.database import GiftMessage as StoredGift, LiveSession, UserContribution
from protobuf import douyin as protocol
from services.data_service import DataService
from services.room_manager import MonitoredRoom
from utils.gift_packet_sampler import GiftPacketSampler
from ws_handlers import handlers


BOX_ITEMS = '[{"values":{"gift_id":4353}},{"values":{"gift_id":781}}]'


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    """覆盖全局 MySQL 清库，并隔离实时环境价格和采样设置。"""
    for key in ('GIFT_PRICE_OVERRIDES', 'GIFT_PACKET_SAMPLE_FILE',
                'GIFT_PACKET_SAMPLING_ENABLED', 'GIFT_PACKET_SAMPLE_MAX_BYTES',
                'GIFT_PACKET_SAMPLE_PER_KEY_LIMIT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, 'GIFT_PRICE_OVERRIDES',
                        config.SPECIAL_GIFT_DIAMOND_OVERRIDES.copy())


@pytest.fixture
def registry():
    assert importlib.util.find_spec('utils.gift_registry'), '官方礼物目录模块尚未实现'
    return importlib.import_module('utils.gift_registry')


def test_special_overrides_are_exactly_the_verified_nineteen():
    expected = {
        '点点星光': 9, '星光闪耀': 9, '闪烁星河': 99, '闪耀星辰': 99,
        '钻石兔兔': 360, '钻石热气球': 620, '钻石跑车': 1500,
        '烈焰跑车': 6000, '落日追光': 6000, '钻石邮轮': 7200,
        '御风飞机': 9000, '至尊超跑': 12000, '钻石火箭': 12001,
        '凌霄战机': 18000, '钻石飞艇': 23333, '520嘉年华': 33000,
        '钻石嘉年华': 36000, '无界超跑': 36000, '星际战舰': 36000,
    }
    assert config.SPECIAL_GIFT_DIAMOND_OVERRIDES == expected
    assert config.Config.SPECIAL_GIFT_DIAMOND_OVERRIDES == expected
    for name in ('幻翼超跑', '青绿典藏版嘉年华', 'AI礼物盒子', 'AI分身礼物邮箱'):
        assert name not in config.GIFT_PRICE_OVERRIDES


def test_catalog_contains_core_official_ids(registry):
    catalog = registry.DOUYIN_GIFT_ID_CATALOG
    assert len(catalog) >= 1800
    assert all(isinstance(key, int) and not isinstance(key, bool)
               and 0 < key < 90000 for key in catalog)
    expected = {
        165: ('棒棒糖', 9), 463: ('小心心', 1), 781: ('比心', 199),
        789: ('嘉年华', 30000), 3729: ('私人飞机', 3000),
        4353: ('跑车', 1200), 12776: ('AI分身礼物邮箱', 99),
        15738: ('豪华跑车', 1200), 21296: ('无界超跑', 36000),
    }
    for gift_id, entry in expected.items():
        assert catalog[gift_id] == entry
        assert registry.lookup_gift_by_id(gift_id) == (entry[0], float(entry[1]))


@pytest.mark.parametrize('gift_id', [None, True, False, 4353.5, float('nan'),
                                   float('inf'), 0, -4353, '', '跑车', '4353x',
                                   '4353.0', '-4353', ' 4353 ', {}, []])
def test_lookup_rejects_invalid_ids(registry, gift_id):
    assert registry.lookup_gift_by_id(gift_id) is None


@pytest.mark.parametrize('gift_id', [4353, '4353', 4353.0])
def test_lookup_accepts_integer_ids(registry, gift_id):
    assert registry.lookup_gift_by_id(gift_id) == ('跑车', 1200.0)
    assert registry.lookup_gift_by_id(999999) is None


@pytest.mark.parametrize('gift_id,name,price', [
    (6957, '钻石嘉年华', 36000.0), (6775, '闪烁星河', 99.0),
    (6776, '点点星光', 9.0),
])
def test_lookup_prefers_builtin_overrides(registry, gift_id, name, price):
    assert registry.lookup_gift_by_id(gift_id, config.GIFT_PRICE_OVERRIDES) == (name, price)


def test_lookup_prefers_custom_overrides(registry):
    overrides = config.parse_gift_price_overrides('{"跑车":1500,"钻石嘉年华":37000}')
    assert registry.lookup_gift_by_id(4353, overrides) == ('跑车', 1500.0)
    assert registry.lookup_gift_by_id(6957, overrides) == ('钻石嘉年华', 37000.0)
    assert registry.lookup_gift_by_id(4353, overrides=[]) == ('跑车', 1200.0)
    for bad_override in ({'跑车': True}, {'跑车': 'bad'}, {'跑车': -5},
                         {'跑车': float('nan')}, {'跑车': float('inf')}):
        assert registry.lookup_gift_by_id(4353, bad_override) == ('跑车', 1200.0)


@pytest.mark.parametrize('raw,price,summary', [
    (BOX_ITEMS, 1399.0, '跑车+比心'),
    ([{'gift_id': 4353}, {'gift_id': 4353}, {'gift_id': 781}], 2599.0, '跑车x2+比心'),
    ([{'values': {'gift_id': 4353, 'count': 3}}], 3600.0, '跑车x3'),
    ([{'values': {'gift_id': '4353', 'count': '3'}}], 3600.0, '跑车x3'),
    ({'items': [{'values': {'gift_id': 4353}}]}, 1200.0, '跑车'),
    ({'data': {'items': [{'values': {'gift_id': 4353}}]}}, 1200.0, '跑车'),
    ({'diy_item_info': [{'gift_id': 4353}]}, 1200.0, '跑车'),
    ({'list': [{'gift_id': 4353}]}, 1200.0, '跑车'),
    ([{'gift_id': 4353, 'values': {'num': '2'}}], 2400.0, '跑车x2'),
    ([{'values': {'gift_id': 4353, 'gift_count': 2}}], 2400.0, '跑车x2'),
    ([{'gift_id': 4353, 'count': 2, 'values': {}}], 2400.0, '跑车x2'),
    ([{'gift_id': 4353, 'num': 2, 'values': {}}], 2400.0, '跑车x2'),
])
def test_composite_formats_and_quantities(registry, raw, price, summary):
    assert registry.resolve_diy_composite_gift(raw, outer_name='AI礼物盒子') == (
        price, f'AI礼物盒子({summary})')


def test_composite_uses_first_list_and_first_non_none_count(registry):
    raw = {'items': [], 'list': [{'gift_id': 4353}]}
    assert registry.resolve_diy_composite_gift(raw) is None
    raw = [{'gift_id': 781, 'count': 4,
            'values': {'gift_id': 4353, 'count': None, 'num': 2, 'gift_count': 3}}]
    assert registry.resolve_diy_composite_gift(raw) == (2400.0, '跑车x2')


def test_composite_truncates_long_summaries_and_outer_names(registry):
    items = [{'gift_id': gid} for gid in list(registry.DOUYIN_GIFT_ID_CATALOG)[:40]]
    assert len({registry.DOUYIN_GIFT_ID_CATALOG[item['gift_id']][0] for item in items}) > 15
    price, name = registry.resolve_diy_composite_gift(items, outer_name='AI礼物盒子')
    assert price == sum(registry.DOUYIN_GIFT_ID_CATALOG[item['gift_id']][1] for item in items)
    assert len(name) <= 96 and name.startswith('AI礼物盒子(') and name.endswith('…)')
    assert len(registry.resolve_diy_composite_gift(items, outer_name='盒' * 120)[1]) <= 96
    assert len(registry.resolve_diy_composite_gift(items, max_name_len=24)[1]) <= 24


@pytest.mark.parametrize('raw', [
    None, '', ' ', '{bad', 'null', '99', '[]', [], {}, {'items': 'bad'},
    [None, 'gift', 4353], [{'gift_id': True}], [{'gift_id': 4353, 'count': -1}],
    [{'gift_id': 4353, 'count': 0}], [{'gift_id': 4353, 'count': True}],
    [{'gift_id': 4353, 'count': 1.5}], [{'gift_id': 4353, 'count': '1.5'}],
    [{'gift_id': 999999}], [{'gift_id': 999999, 'diamond_count': True}],
    [{'gift_id': 999999, 'diamond_count': '520'}],
    [{'gift_id': 999999, 'diamond_count': float('inf')}],
    [{'gift_id': 999999, 'diamond_count': float('nan')}],
    [{'gift_id': 999999, 'diamond_count': -1}],
])
def test_invalid_composites_return_none_and_handler_uses_outer_price(registry, raw):
    assert registry.resolve_diy_composite_gift(raw, outer_name='AI礼物盒子') is None
    blob = raw if isinstance(raw, str) else json.dumps(raw)
    assert handlers.WebDouyinLiveFetcher._resolve_gift_unit_price_with_source(
        protocol.GiftStruct(name='AI礼物盒子', diamond_count=99),
        extra_json_blobs=(blob,),
    ) == (99.0, 'base_diamond')


@pytest.mark.parametrize('key', ['real_diamond_count', 'buff_diamond_count', 'diamond_count'])
@pytest.mark.parametrize('nested', [False, True])
def test_future_gift_uses_explicit_price(registry, key, nested):
    values = {'gift_id': 999999, key: 520, 'gift_name': '新限定礼物'}
    raw = [{'values': values}] if nested else [values]
    assert registry.resolve_diy_composite_gift(raw, outer_name='AI礼物盒子') == (
        520.0, 'AI礼物盒子(新限定礼物)')


def test_price_and_name_fallback_order(registry):
    raw = [{'gift_id': 999999, 'diamond_count': 999, 'gift_name': '备用名字',
            'values': {'real_diamond_count': 520, 'buff_diamond_count': 600,
                       'name': ' 新限定礼物 ', 'count': 2}}]
    assert registry.resolve_diy_composite_gift(raw) == (1040.0, '新限定礼物x2')
    assert registry.resolve_diy_composite_gift([{'gift_id': 999999, 'diamond_count': 520}]) == (
        520.0, '礼物#999999')
    assert registry.resolve_diy_composite_gift([{'diamond_count': 520}], outer_name='盒子') == (520.0, '盒子')
    assert registry.resolve_diy_composite_gift([{'gift_id': 4353}], outer_name=' 跑车 ') == (1200.0, '跑车')
    assert registry.resolve_diy_composite_gift([{'gift_id': 4353, 'diamond_count': 9999}]) == (1200.0, '跑车')


@pytest.mark.parametrize('bad_name', [123, True, {'zh': '新礼物'}])
def test_composite_handles_non_string_names_and_max_len_one(registry, bad_name):
    assert registry.resolve_diy_composite_gift(
        [{'gift_id': 999999, 'diamond_count': 520, 'gift_name': bad_name}],
        outer_name='AI礼物盒子',
    ) == (520.0, 'AI礼物盒子(礼物#999999)')
    assert registry.resolve_diy_composite_gift(
        [{'gift_id': {'id': 1}, 'diamond_count': 520, 'name': bad_name}],
        outer_name='AI礼物盒子',
    ) == (520.0, 'AI礼物盒子')
    assert len(registry.resolve_diy_composite_gift([{'gift_id': 4353}], max_name_len=1)[1]) <= 1


def test_handler_registry_uses_live_environment_overrides(monkeypatch):
    struct = protocol.GiftStruct(name='AI分身礼物邮箱', diamond_count=99)
    assert handlers.WebDouyinLiveFetcher._resolve_gift_unit_price_with_source(
        struct, extra_json_blobs=(BOX_ITEMS,)) == (1399.0, 'diy_gift_registry')
    monkeypatch.setenv('GIFT_PRICE_OVERRIDES', '{"跑车":1500}')
    assert handlers.WebDouyinLiveFetcher._resolve_gift_unit_price_with_source(
        struct, extra_json_blobs=(BOX_ITEMS,)) == (1699.0, 'diy_gift_registry')


def test_handler_preserves_price_source_priority(monkeypatch):
    resolver = handlers.WebDouyinLiveFetcher._resolve_gift_unit_price_with_source
    struct = protocol.GiftStruct(name='钻石嘉年华', diamond_count=99,
                                 gift_buff_infos=[protocol.GiftBuffInfo(buff_level=2, buff_diamond_count=28888)])
    tray = protocol.GiftTrayInfo(buff_level=2)
    assert resolver(struct, tray, (BOX_ITEMS,)) == (36000.0, 'builtin_override')
    monkeypatch.setenv('GIFT_PRICE_OVERRIDES', '{"钻石嘉年华":36000}')
    assert resolver(struct, tray, (BOX_ITEMS,)) == (36000.0, 'env_override')
    struct.name = 'AI礼物盒子'
    assert resolver(struct, tray, (BOX_ITEMS,)) == (28888.0, 'buff_level')
    raw = json.dumps({'real_diamond_count': 5200, 'items': json.loads(BOX_ITEMS)})
    assert resolver(struct, None, (raw,)) == (5200.0, 'extra_json')
    raw = json.dumps({'real_diamond_count': 1, 'items': json.loads(BOX_ITEMS)})
    assert resolver(struct, None, (raw,)) == (1399.0, 'diy_gift_registry')
    monkeypatch.setenv('GIFT_PRICE_OVERRIDES', '{bad')
    assert resolver(protocol.GiftStruct(name='钻石嘉年华')) == (36000.0, 'builtin_override')
    monkeypatch.delenv('GIFT_PRICE_OVERRIDES')
    monkeypatch.setattr(config, 'GIFT_PRICE_OVERRIDES', {'跑车': 1600})
    assert resolver(struct, None, (BOX_ITEMS,)) == (1799.0, 'diy_gift_registry')
    assert resolver(protocol.GiftStruct(name='跑车')) == (1600.0, 'env_override')


@pytest.fixture
def accounting_context(tmp_path):
    service = DataService(f'sqlite:///{tmp_path / "accounting.sqlite"}')
    service.create_tables()
    service.create_live_room('test-room', anchor_name='测试主播', status='monitoring')
    session = service.create_live_session('test-room', anchor_name='测试主播', status='live')
    room = MonitoredRoom('test-room', SimpleNamespace(data_service=service))
    socket = MagicMock()
    with patch('crawler.DouyinLiveWebFetcher') as core:
        core.return_value.anchor_info = {}
        fetcher = handlers.WebDouyinLiveFetcher('test-room', service, socket, room)
        fetcher.log = MagicMock()
        yield service, session.id, room, socket, fetcher
    service.close_session()


def box_message(trace='test-box', combo_count=0, outer_name='AI礼物盒子', **kwargs):
    fields = dict(
        user=protocol.User(id_str='test-viewer', nick_name='测试观众'),
        gift=protocol.GiftStruct(id=12776, name=outer_name, diamond_count=99),
        gift_id=12776, group_count=1, trace_id=trace,
        combo_count=combo_count, group_id=42 if combo_count else 0,
        diy_item_info=BOX_ITEMS,
    )
    fields.update(kwargs)
    return protocol.GiftMessage(**fields)


def gift_payloads(socket):
    return [call.args[1] for call in socket.emit.call_args_list
            if call.args[0] == 'room_test-room']


@pytest.mark.parametrize('outer_name', ['AI礼物盒子', 'AI分身礼物邮箱'])
@pytest.mark.parametrize('field', ['diy_item_info', 'interact_gift_info', 'gift_play_param'])
def test_normal_box_accounts_inner_gifts_and_pushes_display_name(accounting_context, outer_name, field):
    service, session_id, room, socket, fetcher = accounting_context
    message = box_message(outer_name=outer_name, diy_item_info='')
    setattr(message, field, BOX_ITEMS)
    fetcher._handle_gift_message(message, raw_payload=bytes(message))
    with service.get_session() as db:
        stored = db.query(StoredGift).one()
        session = db.get(LiveSession, session_id)
        contribution = db.query(UserContribution).one()
        assert stored.gift_price == stored.total_value == session.total_income == 1399.0
        assert stored.gift_name == f'{outer_name}(跑车+比心)'
        assert stored.gift_count == session.total_gift_count == contribution.gift_count == 1
        assert contribution.total_score == 1399.0
    assert room.stats['total_income'] == fetcher.total_income == 1399.0
    assert room.user_contributions['test-viewer']['score'] == 1399.0
    payload = gift_payloads(socket)[0]
    assert payload['gift_price'] == payload['total_value'] == 1399.0
    assert payload['gift_name'] == f'{outer_name}(跑车+比心)'
    assert payload['gift_count'] == 1 and payload['is_combo'] is False
    assert message.gift.name == outer_name


def test_box_combo_accounts_increment_and_trace_replay_is_idempotent(accounting_context):
    service, session_id, room, socket, fetcher = accounting_context
    first = box_message(trace='test-combo-1', combo_count=1)
    third = box_message(trace='test-combo-3', combo_count=3, repeat_end=1)
    for message in (first, first, third, third, first):
        fetcher._handle_gift_message(message, raw_payload=bytes(message))
    room.trace_dedupe.clear()
    room.trace_dedupe_set.clear()
    room.combo_gifts.clear()
    fetcher._handle_gift_message(third, raw_payload=bytes(third))
    with service.get_session() as db:
        rows = db.query(StoredGift).order_by(StoredGift.id).all()
        assert [row.gift_count for row in rows] == [1, 2]
        assert [row.total_value for row in rows] == [1399.0, 2798.0]
        assert all(row.gift_price == 1399.0 for row in rows)
        session = db.get(LiveSession, session_id)
        assert session.total_income == 4197.0 and session.total_gift_count == 3
    contribution = service.get_user_contribution('test-room', 'test-viewer')
    assert contribution.total_score == room.stats['total_income'] == 4197.0
    assert contribution.gift_count == 3
    payloads = gift_payloads(socket)
    assert len(payloads) == 2
    assert [payload['partial_count'] for payload in payloads] == [1, 2]
    assert payloads[-1]['total_gift_count'] == 3
    assert payloads[-1]['total_combo_value'] == 4197.0


def test_box_sampler_keeps_original_outer_name(accounting_context, tmp_path, monkeypatch):
    service, _, _, _, fetcher = accounting_context
    sample_file = tmp_path / 'gift_packet_samples.jsonl'
    sampler = GiftPacketSampler(sample_file=sample_file, enabled=True)
    monkeypatch.setattr(handlers, 'gift_packet_sampler', sampler)
    message = box_message()
    original_payload = bytes(message)
    fetcher._handle_gift_message(message, raw_payload=original_payload)
    fetcher._handle_gift_message(message, raw_payload=original_payload)
    records = [json.loads(line) for line in sample_file.read_text(encoding='utf-8').splitlines()]
    assert len(records) == 1
    assert records[0]['price_source'] == 'diy_gift_registry'
    assert records[0]['resolved_price'] == 1399.0
    assert records[0]['gift_name'] == 'AI礼物盒子'
    assert bytes(message) == original_payload
    with service.get_session() as db:
        assert db.query(StoredGift).one().gift_name == 'AI礼物盒子(跑车+比心)'


@pytest.mark.parametrize('long_name', [False, True])
def test_series_child_composite_is_priced_named_and_deduped(accounting_context, long_name):
    service, session_id, _, socket, fetcher = accounting_context
    message = box_message(outer_name='盒' * 120 if long_name else 'AI礼物盒子')
    message.series_gift_data = [protocol.SeriesPlayGift(
        gift_struct=protocol.GiftStruct(id=12776, name='AI分身礼物邮箱', diamond_count=99),
        diy_item_info=BOX_ITEMS,
    )]
    fetcher._handle_gift_message(message, raw_payload=bytes(message))
    fetcher._handle_gift_message(message, raw_payload=bytes(message))
    with service.get_session() as db:
        stored = db.query(StoredGift).one()
        assert stored.gift_price == stored.total_value == 1399.0
        assert db.get(LiveSession, session_id).total_income == 1399.0
        assert len(stored.gift_name) <= 96
        if not long_name:
            assert stored.gift_name == 'AI礼物盒子(跑车+比心)'
    assert len(gift_payloads(socket)) == 1
