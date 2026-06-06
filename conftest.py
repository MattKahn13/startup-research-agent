import sys
from pathlib import Path

# Make project root importable so tests can `import schema` etc.
sys.path.insert(0, str(Path(__file__).parent))
