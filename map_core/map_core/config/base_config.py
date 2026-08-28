# essential configurations that not rely on other modulesfrom pathlib import Path
from pathlib import Path

VERSION = "0.0.1"
PROJECT_ROOT = Path(__file__).parents[2].resolve()  # /home/vscode/map-core
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
