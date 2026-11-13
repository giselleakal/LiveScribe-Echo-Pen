"""Filesystem locations shared by every pipeline module.

``REPO`` is the checkout holding the code. ``ROOT`` holds the data the
pipeline reads and writes (``data/``, ``output/``, state files, logs); it
is the checkout too unless ``ECHO_ROOT`` points elsewhere, which keeps
tests and experiments away from the real library.
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.abspath(os.environ.get("ECHO_ROOT") or REPO)

SRC = os.path.join(REPO, "src")
VENDOR = os.path.join(REPO, "vendor")

DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
LOGS = os.path.join(ROOT, "logs")
