import requests
from bs4 import BeautifulSoup
from pathlib import Path

base = ''
entries = [
    ('stack1', '330000010000000000800070CF010002000301,330026000000000000800070CF020003000401,3300260100000000E6400070CF030011010401'),
    ('stack2', '060000010000000000800070CF010011000301'),
    ('stack3', '0C0000010000000000800070CF010002000301,0C000C220000000000000070CF020003000401'),
]

out = Path('_inspect')
out.mkdir(exist_ok=True)

s = requests.Session()
first = s.get(base, timeout=20)
first.raise_for_status()
login = s.post(base + '/login.html', data={'user': 'admin', 'pass': ''}, timeout=20, allow_redirects=True)
login.raise_for_status()

for name, stack in entries:
    url = f'{base}/settings_export.html?stack={stack}'
    r = s.get(url, timeout=20)
    r.raise_for_status()
    (out / f'{name}.html').write_text(r.text, encoding='utf-8')
    soup = BeautifulSoup(r.text, 'html.parser')
    summary = []
    summary.append(f'=== {name} ===')
    summary.append(f'status={r.status_code}')
    summary.append(f'title={soup.title.get_text(" ", strip=True) if soup.title else None}')
    summary.append(f'forms={[(f.get("action"), f.get("method")) for f in soup.find_all("form")]})')
    summary.append(f'inputs={[(i.get("type"), i.get("name"), i.get("value")) for i in soup.find_all("input")[:20]]}')
    summary.append(f'selects={[(sel.get("name"), [o.get_text(" ", strip=True) for o in sel.find_all("option")]) for sel in soup.find_all("select")]})')
    summary.append(f'tables={len(soup.find_all("table"))}')
    text = soup.get_text(' ', strip=True)
    hits = [token for token in ['Heizkreis', 'Systembetriebsart', 'Wärmepumpe', 'Betrieb', 'Sole', 'Raumsolltemperator', 'Komfort', 'Automatik', 'Heizen', 'Kühlen', 'Sommer', 'Standby', '2. WEZ'] if token in text]
    summary.append(f'hits={hits}')
    (out / f'{name}.txt').write_text('\n'.join(summary), encoding='utf-8')

print('done')
