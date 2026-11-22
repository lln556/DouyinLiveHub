"""Helpers for validating and extracting Douyin danmaku emoji metadata."""
import json
from pathlib import Path
import threading
from typing import Optional
import urllib.parse

ALLOWED_EMOJI_CDN_DOMAINS = (
    'douyinpic.com',
    'byteimg.com',
    'bytescm.com',
    'ibytedtos.com',
    'bdurl.net',
    'pstatp.com',
    'snssdk.com',
    'zijieapi.com',
)

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / 'data' / 'emoji_img' / 'manifest.json'
_MANIFEST_LOCK = threading.Lock()
_MANIFEST_CACHE = {'mtime_ns': None, 'names': frozenset()}
_MAX_EMOJI_ITEMS = 20


def is_safe_douyin_cdn_url(url: str) -> bool:
    """Allow only HTTPS ByteDance/Douyin CDN URLs without credentials or unsafe chars."""
    if not isinstance(url, str) or not 1 <= len(url) <= 1000:
        return False
    if any(ch.isspace() or ch in '<>"\'' for ch in url):
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None:
        return False
    hostname = (parsed.hostname or '').lower().rstrip('.')
    if not hostname:
        return False
    return any(hostname == domain or hostname.endswith(f'.{domain}') for domain in ALLOWED_EMOJI_CDN_DOMAINS)


def get_local_emoji_names(manifest_path: Path = _MANIFEST_PATH) -> frozenset[str]:
    """Return official emoji names cached from manifest.json keyed by mtime_ns."""
    try:
        stat = manifest_path.stat()
    except OSError:
        return frozenset()

    with _MANIFEST_LOCK:
        if manifest_path == _MANIFEST_PATH and _MANIFEST_CACHE['mtime_ns'] == stat.st_mtime_ns:
            return _MANIFEST_CACHE['names']

    try:
        data = json.loads(manifest_path.read_text(encoding='utf-8'))
        emojis = data.get('emojis') if isinstance(data, dict) else None
        names = frozenset(str(k) for k in emojis.keys()) if isinstance(emojis, dict) else frozenset()
    except (OSError, ValueError, TypeError):
        names = frozenset()

    if manifest_path == _MANIFEST_PATH:
        with _MANIFEST_LOCK:
            _MANIFEST_CACHE['mtime_ns'] = stat.st_mtime_ns
            _MANIFEST_CACHE['names'] = names
    return names


def pick_safe_image_url(image_obj) -> Optional[str]:
    """Pick the first safe ByteDance CDN URL from an Image proto object."""
    if image_obj is None:
        return None
    for url in getattr(image_obj, 'url_list_list', None) or ():
        if is_safe_douyin_cdn_url(url):
            return url
    return None


def _normalize_alt(raw_alt: str, default: str = '[表情]') -> str:
    text = str(raw_alt or '').replace('\r', '').replace('\n', '').strip()
    if not text:
        return default
    if (
        text.startswith('[')
        and text.endswith(']')
        and 2 < len(text) <= 16
        and '[' not in text[1:-1]
        and ']' not in text[1:-1]
    ):
        return text
    cleaned = text.replace('[', '').replace(']', '').strip()
    if not cleaned:
        return default
    return f'[{cleaned[:14]}]'


def _disambiguate_alt(alt: str, url: str, alt_to_url: dict[str, str]) -> str:
    if alt not in alt_to_url or alt_to_url[alt] == url:
        return alt
    stem = alt[1:-1] if alt.startswith('[') and alt.endswith(']') else alt
    idx = 2
    while True:
        suffix = str(idx)
        candidate = f'[{stem[:14 - len(suffix)]}{suffix}]'
        if candidate not in alt_to_url or alt_to_url[candidate] == url:
            return candidate
        idx += 1


def _is_badge_url(url: str) -> bool:
    lower = url.lower()
    return 'user_grade_level' in lower or 'fansclub' in lower


def _is_sender_user_piece(piece, chat_msg) -> bool:
    user_value = getattr(piece, 'user_value', None)
    if user_value is None:
        return False
    if getattr(user_value, 'with_colon', False):
        return True
    piece_user = getattr(user_value, 'user', None)
    piece_uid = getattr(piece_user, 'id', 0) or 0
    sender_uid = getattr(getattr(chat_msg, 'user', None), 'id', 0) or 0
    return bool(piece_uid and sender_uid and piece_uid == sender_uid)


def extract_chat_content_and_emojis(
    chat_msg,
    local_names: Optional[set[str] | frozenset[str]] = None,
) -> tuple[str, Optional[list[dict]]]:
    """Extract plain chat text and non-official inline emojis from ChatMessage."""
    if local_names is None:
        local_names = get_local_emoji_names()

    raw_content = getattr(chat_msg, 'content', '') or ''
    rtf = None
    for attr in ('rtf_content_v2', 'rtf_content'):
        candidate = getattr(chat_msg, attr, None)
        if candidate is not None and getattr(candidate, 'pieces_list', None):
            rtf = candidate
            break

    if rtf is None or not getattr(rtf, 'pieces_list', None):
        return raw_content, None

    reconstructed_parts: list[str] = []
    emoji_list: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    alt_to_url: dict[str, str] = {}

    for piece in rtf.pieces_list:
        if _is_sender_user_piece(piece, chat_msg):
            continue

        string_value = getattr(piece, 'string_value', '') or ''
        if string_value:
            reconstructed_parts.append(string_value)

        image_value = getattr(piece, 'image_value', None)
        image_obj = getattr(image_value, 'image', None) if image_value is not None else None
        url = pick_safe_image_url(image_obj)
        if not url or _is_badge_url(url):
            continue

        img_content = getattr(image_obj, 'content', None)
        raw_alt = (
            getattr(img_content, 'alternative_text', '')
            or getattr(img_content, 'name', '')
            or ''
        ).strip()
        alt = _normalize_alt(raw_alt, default='[表情]')
        is_official = alt in local_names and (alt in raw_content or not raw_content.strip())
        if is_official:
            reconstructed_parts.append(alt)
            continue

        alt = _disambiguate_alt(alt, url, alt_to_url)
        reconstructed_parts.append(alt)
        pair = (alt, url)
        if pair not in seen_pairs and len(emoji_list) < _MAX_EMOJI_ITEMS:
            seen_pairs.add(pair)
            alt_to_url[alt] = url
            emoji_list.append({'alt': alt, 'url': url})

    if not raw_content.strip() and reconstructed_parts:
        raw_content = ''.join(reconstructed_parts)

    if emoji_list:
        for item in emoji_list:
            if item['alt'] not in raw_content:
                raw_content += item['alt']

    return raw_content, (emoji_list or None)


def extract_emoji_chat_message(emoji_msg) -> tuple[str, Optional[list[dict]]]:
    """Extract sticker content and metadata from WebcastEmojiChatMessage."""
    default_raw = (getattr(emoji_msg, 'default_content', '') or '').strip()
    pieces = getattr(getattr(emoji_msg, 'emoji_content', None), 'pieces_list', None) or []

    urls_with_alts: list[tuple[str, str]] = []
    for piece in pieces:
        image_value = getattr(piece, 'image_value', None)
        image_obj = getattr(image_value, 'image', None) if image_value is not None else None
        url = pick_safe_image_url(image_obj)
        if not url or _is_badge_url(url):
            continue
        img_content = getattr(image_obj, 'content', None)
        img_alt = (
            getattr(img_content, 'alternative_text', '')
            or getattr(img_content, 'name', '')
            or ''
        ).strip()
        urls_with_alts.append((url, img_alt))

    if not urls_with_alts:
        return (default_raw or '[动画表情]'), None

    if len(urls_with_alts) == 1:
        url, img_alt = urls_with_alts[0]
        alt = _normalize_alt(default_raw or img_alt, default='[动画表情]')
        return alt, [{'alt': alt, 'url': url, 'sticker': True}]

    emoji_list: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    alt_to_url: dict[str, str] = {}
    content_parts: list[str] = []

    for idx, (url, img_alt) in enumerate(urls_with_alts, start=1):
        base_alt = _normalize_alt(img_alt, default=f'[动画表情{idx}]')
        alt = _disambiguate_alt(base_alt, url, alt_to_url)
        content_parts.append(alt)
        pair = (alt, url)
        if pair not in seen_pairs and len(emoji_list) < _MAX_EMOJI_ITEMS:
            seen_pairs.add(pair)
            alt_to_url[alt] = url
            emoji_list.append({'alt': alt, 'url': url, 'sticker': True})

    return ''.join(content_parts), (emoji_list or None)
