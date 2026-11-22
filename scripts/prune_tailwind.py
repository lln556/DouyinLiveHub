#!/usr/bin/env python3
"""Prune unused utility selectors from the vendored Tailwind v2 stylesheet."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAILWIND_PATH = ROOT / 'static' / 'vendor' / 'tailwind.min.css'
SOURCE_GLOBS = (
    'templates/*.html',
    'static/js/*.js',
    'static/css/*.css',
    'tests/screenshots/*.py',
)
_TOKEN_RE = re.compile(r'[A-Za-z0-9_:/\-\.%]+')
_CLASS_SELECTOR_RE = re.compile(r'\.((?:\\.|[A-Za-z0-9_-])+)')


def collect_used_tokens(root: Path = ROOT) -> set[str]:
    """Collect candidate class names across templates, scripts, styles, and screenshot helpers."""
    tokens: set[str] = set()
    for pattern in SOURCE_GLOBS:
        for path in sorted(root.glob(pattern)):
            content = path.read_text(encoding='utf-8')
            for match in _TOKEN_RE.findall(content):
                tokens.add(match)
    return tokens


def unescape_css_class(raw: str) -> str:
    """Decode CSS identifier escapes such as \\32xl\\:container or w-1\\/2."""
    decoded = re.sub(r'\\([0-9a-fA-F]{1,6})\s?', lambda m: chr(int(m.group(1), 16)), raw)
    return re.sub(r'\\(.)', r'\1', decoded)


def _split_top_level_blocks(src: str) -> list[tuple]:
    blocks: list[tuple] = []
    i, n = 0, len(src)
    while i < n:
        while i < n and src[i].isspace():
            i += 1
        if i >= n:
            break
        if src.startswith('/*', i):
            end = src.find('*/', i + 2)
            end = n if end == -1 else end + 2
            blocks.append(('comment', src[i:end]))
            i = end
            continue
        j = i
        while j < n and src[j] not in '{;':
            j += 1
        if j >= n:
            break
        if src[j] == ';':
            blocks.append(('stmt', src[i:j + 1]))
            i = j + 1
            continue
        prelude = src[i:j]
        depth = 1
        k = j + 1
        in_str = None
        while k < n and depth > 0:
            ch = src[k]
            if in_str:
                if ch == '\\':
                    k += 2
                    continue
                if ch == in_str:
                    in_str = None
            else:
                if ch in ('"', "'"):
                    in_str = ch
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
            k += 1
        blocks.append(('block', prelude, src[j + 1:k - 1]))
        i = k
    return blocks


def _filter_selector_group(selector_group: str, used_tokens: set[str]) -> str:
    parts: list[str] = []
    current: list[str] = []
    paren_depth = 0
    for ch in selector_group:
        if ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth = max(0, paren_depth - 1)
        if ch == ',' and paren_depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())

    kept = []
    for part in parts:
        classes = [unescape_css_class(m.group(1)) for m in _CLASS_SELECTOR_RE.finditer(part)]
        if not classes or all(cls in used_tokens for cls in classes):
            kept.append(part)
    return ','.join(kept)


def prune_css(css_text: str, used_tokens: set[str]) -> str:
    """Keep normalize/preflight, keyframes, and referenced utility rules."""
    out: list[str] = []
    for item in _split_top_level_blocks(css_text):
        kind = item[0]
        if kind in ('comment', 'stmt'):
            out.append(item[1])
            continue
        prelude, body = item[1].strip(), item[2]
        if prelude.startswith(('@keyframes', '@-webkit-keyframes', '@font-face', '@page')):
            out.append(f'{prelude}{{{body}}}')
        elif prelude.startswith(('@media', '@supports')):
            inner = prune_css(body, used_tokens)
            if inner.strip():
                out.append(f'{prelude}{{{inner}}}')
        else:
            kept_selector = _filter_selector_group(prelude, used_tokens)
            if kept_selector:
                out.append(f'{kept_selector}{{{body}}}')
    return ''.join(out)


def collect_selector_classes(css_text: str) -> set[str]:
    """Collect unescaped CSS class names exclusively from selector preludes (not declaration bodies)."""
    classes: set[str] = set()
    for item in _split_top_level_blocks(css_text):
        if item[0] != 'block':
            continue
        prelude, body = item[1].strip(), item[2]
        if prelude.startswith(('@media', '@supports')):
            classes.update(collect_selector_classes(body))
        elif not prelude.startswith(('@keyframes', '@-webkit-keyframes', '@font-face', '@page')):
            for m in _CLASS_SELECTOR_RE.finditer(prelude):
                classes.add(unescape_css_class(m.group(1)))
    return classes


def verify_retained_classes(original_css: str, pruned_css: str, used_tokens: set[str]) -> set[str]:
    """Verify every used class present in original_css selectors remains present in pruned_css."""
    orig_classes = collect_selector_classes(original_css)
    pruned_classes = collect_selector_classes(pruned_css)
    missing = (used_tokens & orig_classes) - pruned_classes
    if missing:
        raise RuntimeError(f'Missing retained Tailwind classes after pruning: {sorted(missing)}')
    return pruned_classes


def main():
    original_css = TAILWIND_PATH.read_text(encoding='utf-8')
    used_tokens = collect_used_tokens(ROOT)
    pruned_css = prune_css(original_css, used_tokens)
    retained = verify_retained_classes(original_css, pruned_css, used_tokens)
    TAILWIND_PATH.write_text(pruned_css, encoding='utf-8')
    print(
        f'Pruned {TAILWIND_PATH.relative_to(ROOT)}: '
        f'{len(original_css)} -> {len(pruned_css)} bytes '
        f'({len(retained)} utility classes retained)'
    )


if __name__ == '__main__':
    main()
