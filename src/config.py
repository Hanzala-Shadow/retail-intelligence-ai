from pathlib import Path

# Repo root is one level above src/
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = str(REPO_ROOT / "data")
