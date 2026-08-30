from __future__ import annotations

import os

# Importing `config` reads os.environ["TELEGRAM_TOKEN"] at module load time.
# Set a default before any project import happens (including the ones pulled
# in transitively by collecting this very file) so collection cannot raise
# KeyError. Task 2 makes the token optional at import; this line stays anyway.
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

from collections.abc import Callable
from pathlib import Path

import pytest

import config
import database

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def read_fixture() -> Callable[[str], bytes]:
    def _read(name: str) -> bytes:
        return (FIXTURES_DIR / name).read_bytes()

    return _read


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    database.reset_connections()
    database.init_db()
    yield db_path
    database.reset_connections()


@pytest.fixture
def env_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
