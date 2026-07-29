from pathlib import Path

import config

files = list(config.SECTIONS_DIR.rglob('*.txt'))
print(f'Total section files: {len(files)}')
print()
print('Sections for AAP 2024:')
aap_files = [f for f in files if '24-000048' in f.name]
for f in sorted(aap_files):
    parts = f.stem.split('__')
    section = parts[-1]
    chars = len(f.read_text(encoding='utf-8'))
    print(f'  {section} — {chars} chars')