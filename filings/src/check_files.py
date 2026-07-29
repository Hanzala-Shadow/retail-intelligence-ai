from pathlib import Path
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

root = config.RAW_10K_DIR
folders = [f for f in root.iterdir() if f.is_dir()]
print(f'Company folders found: {len(folders)}')

total_files = 0
for folder in folders:
    files = list(folder.glob('*.htm'))
    total_files += len(files)

print(f'Total .htm files: {total_files}')