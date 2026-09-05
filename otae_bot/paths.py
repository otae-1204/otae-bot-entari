"""Source-relative resources; writable runtime paths retain their CWD semantics."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
