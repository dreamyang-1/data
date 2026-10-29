from pathlib import Path
import sys

# Integration fixtures remain owned by the host application's existing tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
