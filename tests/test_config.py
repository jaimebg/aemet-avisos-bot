"""Tests for config.py: environment parsing and the preference-rank helper."""

from __future__ import annotations

import pytest

import config


def test_a_malformed_numeric_env_var_is_reported_by_validate(monkeypatch):
    """A typo in .env must reach validate(), not blow up at import time."""
    monkeypatch.setattr(config, "_ENV_ERRORS", [])
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "abc")

    assert config._env_int("POLL_INTERVAL_SECONDS", 300) == 300
    assert config._ENV_ERRORS == [
        "POLL_INTERVAL_SECONDS must be a whole number (got 'abc')"
    ]

    with pytest.raises(config.ConfigError) as excinfo:
        config.validate()

    assert "POLL_INTERVAL_SECONDS" in str(excinfo.value)


def test_env_helpers_read_good_values_and_treat_an_empty_one_as_unset(monkeypatch):
    monkeypatch.setattr(config, "_ENV_ERRORS", [])
    monkeypatch.setenv("HTTP_MAX_CONCURRENCY", "12")
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("SEEN_RETENTION_DAYS", "")
    monkeypatch.delenv("CLEANUP_INTERVAL_SECONDS", raising=False)

    assert config._env_int("HTTP_MAX_CONCURRENCY", 8) == 12
    assert config._env_float("HTTP_TIMEOUT_SECONDS", 20.0) == 2.5
    # A bare "SEEN_RETENTION_DAYS=" line means "unset", not "malformed".
    assert config._env_int("SEEN_RETENTION_DAYS", 7) == 7
    assert config._env_int("CLEANUP_INTERVAL_SECONDS", 3600) == 3600
    assert config._ENV_ERRORS == []


def test_rank_for_defaults_on_a_missing_or_unrecognised_preference():
    """The stored-preference rule: unknown degrades to the default rank."""
    assert config.rank_for("amarillo") == 1
    assert config.rank_for("naranja") == 2
    assert config.rank_for("rojo") == 3

    default_rank = config.LEVEL_RANK[config.DEFAULT_MIN_LEVEL]
    assert config.rank_for(None) == default_rank
    assert config.rank_for("verde") == default_rank
    # Not the same question as an unknown *incoming* alert level, which ranks
    # UNKNOWN_LEVEL_RANK so the severity filter never discards it.
    assert config.rank_for("verde") != config.UNKNOWN_LEVEL_RANK
