import sys
from pathlib import Path

# Make the application package importable when pytest is run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
