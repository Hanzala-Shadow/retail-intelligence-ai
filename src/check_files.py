from pathlib import Path

root = Path('data/01_raw/10k')
folders = [f for f in root.iterdir() if f.is_dir()]
print(f'Company folders found: {len(folders)}')

total_files = 0
for folder in folders:
    files = list(folder.glob('*.htm'))
    total_files += len(files)

print(f'Total .htm files: {total_files}')