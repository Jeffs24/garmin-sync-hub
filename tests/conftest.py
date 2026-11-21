"""Keep every test away from the real data directory."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="garmin-sync-hub-tests-")
