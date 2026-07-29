from pathlib import Path

import config

# Check Item_15 first 500 chars
files = list(config.SECTIONS_DIR.rglob('*24-000048*Item_15*'))
if files:
    text = files[0].read_text(encoding='utf-8')
    print(f"Item_15 total chars: {len(text)}")
    print()
    print("First 500 chars:")
    print(text[:500])
    print()
    print("Searching for Item 1A content...")
    if 'risk factor' in text.lower():
        pos = text.lower().find('risk factor')
        print(f"Found 'risk factor' at position {pos}")
        print(text[pos:pos+200])