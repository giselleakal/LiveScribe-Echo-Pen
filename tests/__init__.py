"""Test package for the Echo pipeline.

Importing it points every pipeline module at a throwaway ``ECHO_ROOT`` so
no test can read or write the real ``data/``, ``output/``, state files or
logs. The sandbox must exist before any ``src`` module is imported,
because they resolve their paths at import time.
"""
import atexit
import os
import shutil
import sys
import tempfile

SANDBOX = tempfile.mkdtemp(prefix="echo-test-root-")
os.environ["ECHO_ROOT"] = SANDBOX
os.environ.pop("ECHO_SCRIBE_SECRET", None)
os.environ.pop("ECHO_LOCK_HELD", None)
# expected-failure tests would otherwise print warnings; tests read the
# JSONL records instead
os.environ["ECHO_CONSOLE_LOG_LEVEL"] = "CRITICAL"
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
