from pathlib import Path

# Read the parsed text
files = list(Path('data/02_interim').rglob('*24-000048*'))
if files:
    text = files[0].read_text(encoding='utf-8')
    lines = text.split('\n')
    
    # Find all lines containing "item 1a"
    print("All lines containing 'item 1a':")
    for i, line in enumerate(lines):
        if 'item 1a' in line.lower():
            print(f"Line {i}: '{line.strip()}'")