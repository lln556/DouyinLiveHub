"""Bounded, best-effort gift diagnostics without room or viewer identities.

Wire inspection deliberately does not round-trip through the generated schema:
new platform fields must survive even when the protobuf model does not know them.
"""
import base64
import json
import logging
import os
import sys
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import config


logger = logging.getLogger(__name__)
_GIFT_PII_TAGS = {1, 7, 8, 10, 16, 18, 19, 32, 35}
_GIFT_IMAGE_TAGS = {1, 15, 21}
_HANDLED_METHODS = {
    'WebcastChatMessage', 'WebcastEmojiChatMessage', 'WebcastGiftMessage', 'WebcastBindingGiftMessage',
    'WebcastLikeMessage', 'WebcastRoomUserSeqMessage', 'WebcastControlMessage',
}
_METHOD_WORDS = ('gift', 'box', 'reward', 'aigc', 'magic', 'treasure', 'lottery', 'blind', 'chest')
_GIFT_WORDS = ('盒', '箱', '邮箱', '盲盒', '福袋', '锦盒', '宝盒', '魔盒', '超跑', '追光')
_MEDIA_MARKERS = (b'MS4wLjAB', b'douyinpic', b'byteimg')
_URL_MARKERS = ('MS4wLjAB', 'douyinpic', 'byteimg', 'http://', 'https://')
_PII_NAMES = (
    'live_id', 'room_id', 'anchor_name', 'anchor_id', 'user_id', 'user_name',
    'sec_uid', 'log_id', 'trace_id', 'msg_id', 'nick_name', 'id_str',
    'display_id', 'special_id',
)
_PII_KEYS = {name.replace('_', '') for name in _PII_NAMES} | {
    'user', 'touser', 'common', 'anchor', 'room', 'viewer', 'nickname', 'shortid', 'openid', 'unionid',
}


def _varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _read_varint(payload, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(payload):
            raise ValueError('truncated varint')
        byte = payload[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError('varint overflow')
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('invalid varint')


def _wire_tags(payload):
    """Return the valid prefix plus completeness; never expose malformed tails."""
    fields = []
    offset = 0
    try:
        while offset < len(payload):
            key, offset = _read_varint(payload, offset)
            tag, wire = key >> 3, key & 7
            if not 0 < tag < (1 << 29) or wire not in (0, 1, 2, 5):
                raise ValueError('invalid wire tag')
            if wire == 0:
                value, offset = _read_varint(payload, offset)
            else:
                if wire == 2:
                    size, offset = _read_varint(payload, offset)
                else:
                    size = 8 if wire == 1 else 4
                end = offset + size
                if end > len(payload):
                    raise ValueError('truncated field')
                value = payload[offset:end]
                offset = end
            fields.append((tag, wire, value))
    except ValueError:
        return fields, False
    return fields, True


def _encode_field(tag, wire, value):
    body = _varint(value) if wire == 0 else (_varint(len(value)) + value if wire == 2 else value)
    return _varint((tag << 3) | wire) + body


def _safe(value, secrets=()):
    """Scrub only allowlisted summaries and anonymous wire values, including JSON."""
    if isinstance(value, dict):
        result = {}
        key_secrets = tuple(secret for secret in secrets if len(secret) >= 4)
        for key, item in value.items():
            text_key = str(key)
            if (text_key.replace('_', '').lower() in _PII_KEYS
                    or any(name in text_key.lower() for name in _PII_NAMES)):
                continue
            result[_safe(text_key, key_secrets)] = _safe(item, secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            pass
        else:
            safe = _safe(parsed, secrets)
            return value if safe == parsed else json.dumps(safe, ensure_ascii=False, separators=(',', ':'))
    if any(marker in value for marker in _URL_MARKERS):
        return '<redacted_url_or_token>'
    if (any(name.replace('_', '') in value.replace('_', '').lower() for name in _PII_NAMES)
            or any(secret in value for secret in secrets)):
        return '<redacted_identity>'
    return value


def _url_safe(value):
    """Keep fixed schema keys and gift metadata intact, removing only URL/tokens."""
    if isinstance(value, dict):
        return {key: _url_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_url_safe(item) for item in value]
    if isinstance(value, str) and any(marker in value for marker in _URL_MARKERS):
        return '<redacted_url_or_token>'
    return value


def _message_secrets(message):
    """Use known identities only in memory to scrub accidental copies in metadata."""
    protected = {str(getattr(message, 'gift_id', 0) or '').strip()}
    structs = [getattr(message, 'gift', None)] + [
        getattr(item, 'gift_struct', None) for item in getattr(message, 'series_gift_data', None) or []
    ]
    for struct in structs:
        for name in ('name', 'describe', 'id'):
            protected.add(str(getattr(struct, name, '') or '').strip())
    protected.discard('')
    protected_texts = sorted(protected, key=len, reverse=True)

    def eligible(text, numeric=False):
        if numeric:
            return len(text) >= 6
        return len(text) >= 2 and (not text.isascii() or len(text) >= 4)

    secrets = set()
    for obj, names in (
        (getattr(message, 'user', None), ('nick_name', 'id_str', 'id', 'sec_uid', 'display_id', 'special_id')),
        (getattr(message, 'to_user', None), ('nick_name', 'id_str', 'id', 'sec_uid', 'display_id', 'special_id')),
        (getattr(message, 'common', None), ('describe', 'msg_id', 'room_id', 'log_id')),
        (message, ('trace_id', 'log_id')),
    ):
        for name in names:
            value = getattr(obj, name, None)
            text = str(value).strip() if value is not None else ''
            numeric = name in ('id', 'id_str', 'msg_id', 'room_id')
            if text in protected or not eligible(text, numeric):
                continue
            if name == 'describe':
                for gift_text in protected_texts:
                    text = text.replace(gift_text, '')
                text = text.strip()
            if text not in protected and eligible(text, numeric):
                secrets.add(text)
    return tuple(sorted(secrets))


def _inspect_wire(payload, context='', unhandled=False, secrets=(), depth=0):
    """Scrub known structural tags; decode anonymous fields to at most three levels."""
    if depth > 16:
        return b'', {}  # Bound adversarial nesting without retaining an opaque PII tail.
    fields, _ = _wire_tags(payload)
    output = bytearray()
    decoded = {}
    repeated = set()
    for tag, wire, value in fields:
        if ((context == 'gift_message' and tag in _GIFT_PII_TAGS)
                or (context == 'gift_struct' and tag in _GIFT_IMAGE_TAGS)
                or (unhandled and depth == 0 and tag == 1)):
            continue
        if wire == 0:
            display = value
        elif wire in (1, 5):
            display = int.from_bytes(value, 'little')
        else:
            child_context = ''
            if context == 'gift_message':
                child_context = {15: 'gift_struct', 42: 'series'}.get(tag, '')
            elif context == 'series' and tag == 1:
                child_context = 'gift_struct'
            if unhandled and any(marker in value for marker in _MEDIA_MARKERS):
                display = '<redacted_user_or_media>'
                value = display.encode()
            elif child_context:
                value, display = _inspect_wire(value, child_context, unhandled, secrets, depth + 1)
            else:
                try:
                    text = value.decode('utf-8')
                except UnicodeDecodeError:
                    text = None
                if text is not None and all(char.isprintable() or char in '\r\n\t' for char in text):
                    field_secrets = () if context == 'gift_struct' and tag in (2, 16) else secrets
                    display = _safe(text, field_secrets)
                    value = display.encode('utf-8')
                else:
                    nested, complete = _wire_tags(value)
                    if nested and complete:
                        value, display = _inspect_wire(value, '', unhandled, secrets, depth + 1)
                    elif (any(marker in value for marker in _MEDIA_MARKERS)
                          or b'http://' in value or b'https://' in value):
                        display = '<redacted_url_or_token>'
                        value = display.encode()
                    elif any(secret.encode() in value for secret in secrets):
                        display = '<redacted_identity>'
                        value = display.encode()
                    else:
                        display = '<opaque_bytes>'
            if isinstance(display, dict) and depth >= 3:
                display = '<nested_message>'
        output.extend(_encode_field(tag, wire, value))
        key = str(tag)
        if key not in decoded:
            decoded[key] = display
        elif key in repeated:
            decoded[key].append(display)
        else:
            decoded[key] = [decoded[key], display]
            repeated.add(key)
    return bytes(output), decoded


def _buff_summary(struct):
    return [{key: getattr(buff, key, 0 if key != 'text' else '') for key in (
        'text', 'buff_gift_id', 'buff_level', 'buff_can_send', 'buff_diamond_count',
    )} for buff in getattr(struct, 'gift_buff_infos', None) or []]


def _gift_summary(struct):
    return {
        'gift_id': str(getattr(struct, 'id', 0) or ''),
        'gift_name': getattr(struct, 'name', '') or '',
        'gift_describe': getattr(struct, 'describe', '') or '',
        'gift_type': int(getattr(struct, 'type', 0) or 0),
        'base_diamond': getattr(struct, 'diamond_count', 0) or 0,
        'buff_infos': _buff_summary(struct),
    }


class GiftPacketSampler:
    def __init__(self, sample_file=None, enabled=None, max_bytes=None, per_key_limit=None):
        self._isolated = ('pytest' in sys.modules and sample_file is None
                          and 'GIFT_PACKET_SAMPLE_FILE' not in os.environ)
        self._defaults = {
            'GIFT_PACKET_SAMPLE_FILE': sample_file,
            'GIFT_PACKET_SAMPLING_ENABLED': enabled,
            'GIFT_PACKET_SAMPLE_MAX_BYTES': max_bytes,
            'GIFT_PACKET_SAMPLE_PER_KEY_LIMIT': per_key_limit,
        }
        self._counts = OrderedDict()
        self._lock = threading.Lock()

    def _setting(self, name):
        raw = os.getenv(name)
        if raw is not None and raw.strip():
            return raw
        default = self._defaults.get(name)
        return default if default is not None else getattr(config, name)

    def _enabled(self):
        return (not self._isolated and str(self._setting('GIFT_PACKET_SAMPLING_ENABLED'))
                .strip().lower() in ('1', 'true', 'yes', 'on'))

    def _write(self, signature, record, combo_count=0):
        with self._lock:
            limit = int(self._setting('GIFT_PACKET_SAMPLE_PER_KEY_LIMIT'))
            count = self._counts.get(signature, 0)
            if signature in self._counts:
                self._counts.move_to_end(signature)
            if count >= limit or (combo_count > 1 and count >= 1):
                return
            max_bytes = int(self._setting('GIFT_PACKET_SAMPLE_MAX_BYTES'))
            line = (json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
            if max_bytes <= 0 or len(line) > max_bytes:
                return
            path = Path(self._setting('GIFT_PACKET_SAMPLE_FILE'))
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(line) > max_bytes:
                os.replace(path, f'{path}.1')
            with path.open('ab') as sample_file:
                sample_file.write(line)
            self._counts[signature] = count + 1
            while len(self._counts) > 512:
                self._counts.popitem(last=False)

    def sample_gift_message(self, method, raw_payload, gift_msg, resolved_price, price_source):
        try:
            if not self._enabled():
                return
            struct = getattr(gift_msg, 'gift', None)
            tray = getattr(gift_msg, 'tray_info', None)
            name = getattr(struct, 'name', '') or ''
            describe = getattr(struct, 'describe', '') or ''
            buff_level = int(getattr(tray, 'buff_level', 0) or 0)
            series = getattr(gift_msg, 'series_gift_data', None) or []
            extra = {key: getattr(gift_msg, key, '') or '' for key in (
                'interact_gift_info', 'diy_item_info', 'gift_play_param',
            )}
            triggers = []
            if method == 'WebcastBindingGiftMessage':
                triggers.append('binding_gift')
            if series:
                triggers.append('series_gift_data')
            if (buff_level > 0 or int(getattr(tray, 'origin_gift_id', 0) or 0) > 0
                    or (getattr(tray, 'toolbar_card_name', '') or '').strip()
                    or getattr(struct, 'gift_buff_infos', None)):
                triggers.append('buff_or_tray')
            if any(isinstance(value, str) and value for value in extra.values()):
                triggers.append('extra_json')
            if price_source in ('env_override', 'builtin_override', 'override'):
                triggers.append('price_override')
            if describe and name and name not in describe:
                triggers.append('disguised_name')
            if int(getattr(struct, 'type', 0) or 0) == 13 or any(word in name for word in _GIFT_WORDS):
                triggers.append('box_or_special_type')
            if not triggers:
                return
            gift_id = str(getattr(gift_msg, 'gift_id', 0) or getattr(struct, 'id', 0) or '')
            signature = (method, gift_id, name, buff_level, bool(series), tuple(sorted(triggers)))
            secrets = _message_secrets(gift_msg)
            payload = bytes(raw_payload)
            if method == 'WebcastBindingGiftMessage':
                envelope, _ = _wire_tags(payload)
                payload = b''.join(value for tag, wire, value in envelope if tag == 1 and wire == 2)
            scrubbed, wire_fields = _inspect_wire(payload, 'gift_message', secrets=secrets)
            record = {
                'sampled_at': datetime.now(timezone.utc).isoformat(),
                'method': method, 'triggers': triggers, **_gift_summary(struct), 'gift_id': gift_id,
                'resolved_price': resolved_price, 'price_source': price_source, 'buff_level': buff_level,
                'tray_info': _safe({key: getattr(tray, key, '') or '' for key in (
                    'origin_gift_id', 'buff_level', 'toolbar_card_name', 'tray_extra_text',
                )}, secrets),
                'extra_json': _safe(extra, secrets),
                'series_items': [{**_gift_summary(getattr(item, 'gift_struct', None)),
                                  'diy_item_info': _safe(getattr(item, 'diy_item_info', '') or '', secrets)}
                                 for item in series],
                # Inspection already scrubs wire values with secrets, except GiftStruct name/describe.
                'wire_fields': wire_fields,
            }
            record = _url_safe(record)
            if len(scrubbed) <= 8192:
                record['scrubbed_payload_b64'] = base64.b64encode(scrubbed).decode('ascii')
            self._write(signature, record, int(getattr(gift_msg, 'combo_count', 0) or 0))
        except Exception:
            logger.debug('Gift packet sampling skipped after a diagnostic error')

    def sample_unhandled_method(self, method, raw_payload):
        try:
            if (not self._enabled() or not method or method in _HANDLED_METHODS
                    or not any(word in method.lower() for word in _METHOD_WORDS)):
                return
            _, wire_fields = _inspect_wire(bytes(raw_payload), unhandled=True)
            triggers = ['unhandled_gift_method']
            self._write((method, tuple(triggers)), {
                'sampled_at': datetime.now(timezone.utc).isoformat(),
                'method': _safe(method), 'triggers': triggers, 'wire_fields': wire_fields,
            })
        except Exception:
            logger.debug('Unhandled gift packet sampling skipped after a diagnostic error')


gift_packet_sampler = GiftPacketSampler()
