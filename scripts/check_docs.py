"""Check local links and anchors in current public Markdown (archives are historical)."""
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent


def anchors(text):
    result, seen = set(), {}
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    for heading in re.findall(r'^#{1,6}\s+(.+)$', text, re.M):
        slug = re.sub(r'<[^>]+>', '', heading).lower().strip()
        slug = re.sub(r'[^\w\- ]', '', slug).replace(' ', '-')
        index = seen.get(slug, 0)
        seen[slug] = index + 1
        result.add(slug + (f'-{index}' if index else ''))
    result.update(re.findall(r'(?:id|name)=["\x27]([^"\x27]+)', text))
    return result


def check():
    files = list(ROOT.glob('*.md')) + list((ROOT / 'docs').glob('*.md')) + list((ROOT / 'extension').rglob('*.md'))
    files = [p for p in files if 'node_modules' not in p.parts]
    errors, checked = [], 0
    for path in files:
        text = re.sub(r'```.*?```', '', path.read_text(), flags=re.S)
        for match in re.finditer(r'\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)', text):
            url = urlsplit(match[1].strip('<>'))
            if url.scheme or url.netloc:
                continue
            dest = path.parent / unquote(url.path) if url.path else path
            if not dest.exists():
                errors.append(f'{path.relative_to(ROOT)}: missing {match[1]}')
            elif url.fragment and dest.suffix == '.md' and unquote(url.fragment) not in anchors(dest.read_text()):
                errors.append(f'{path.relative_to(ROOT)}: missing anchor {match[1]}')
            checked += 1
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Checked {checked} local links across {len(files)} current Markdown files.')


if __name__ == '__main__':
    check()
