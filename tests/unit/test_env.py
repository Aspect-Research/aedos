"""Tests for `aedos.utils.env` (F3 §6 / F-013)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aedos.utils import env as env_module


@pytest.fixture(autouse=True)
def reset_idempotency_flag():
    """Each test starts from a fresh _loaded state."""
    env_module._reset_for_tests()
    yield
    env_module._reset_for_tests()


class TestLoadDotenvIfPresent:
    def test_loads_when_file_present(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("F3_TEST_KEY=loaded\n", encoding="utf-8")
        monkeypatch.delenv("F3_TEST_KEY", raising=False)
        result = env_module.load_dotenv_if_present(env_file)
        assert result is True
        assert os.environ.get("F3_TEST_KEY") == "loaded"

    def test_returns_false_when_no_file(self, tmp_path):
        nonexistent = tmp_path / "missing.env"
        assert env_module.load_dotenv_if_present(nonexistent) is False

    def test_idempotent_no_explicit_path(self, tmp_path, monkeypatch):
        """Default-path search caches the loaded state; second call is a
        no-op at the file-read level. The result is still True (the env
        was already loaded once)."""
        env_file = tmp_path / ".env"
        env_file.write_text("F3_TEST_IDEMP=first\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("F3_TEST_IDEMP", raising=False)

        # First call: loads.
        assert env_module.load_dotenv_if_present() is True
        assert os.environ["F3_TEST_IDEMP"] == "first"

        # Mutate the file; second call should NOT reload because the
        # idempotency flag is set. The value stays the first one.
        env_file.write_text("F3_TEST_IDEMP=second\n", encoding="utf-8")
        # The env value the second .env would set must not appear in
        # process env after a no-op call.
        assert env_module.load_dotenv_if_present() is True
        assert os.environ["F3_TEST_IDEMP"] == "first"

    def test_explicit_path_bypasses_idempotency(self, tmp_path, monkeypatch):
        """Idempotency applies only to the default-path search. An
        explicit-path call always re-attempts the load — useful when
        a caller knows it wants this specific file."""
        env_file = tmp_path / ".env"
        env_file.write_text("F3_TEST_EXPL=v1\n", encoding="utf-8")
        monkeypatch.delenv("F3_TEST_EXPL", raising=False)

        env_module.load_dotenv_if_present(env_file)
        assert os.environ["F3_TEST_EXPL"] == "v1"

        env_file.write_text("F3_TEST_EXPL=v2\n", encoding="utf-8")
        # Default override=False — the existing process value wins.
        env_module.load_dotenv_if_present(env_file)
        assert os.environ["F3_TEST_EXPL"] == "v1"

        # override=True lets the new file's value win.
        env_module.load_dotenv_if_present(env_file, override=True)
        assert os.environ["F3_TEST_EXPL"] == "v2"

    def test_does_not_override_existing_env_var_by_default(self, tmp_path, monkeypatch):
        """An explicit `export VAR=...` should win over the .env file —
        matches the python-dotenv default and the Phase 10.5 runbook's
        documented behavior."""
        env_file = tmp_path / ".env"
        env_file.write_text("F3_TEST_PRECEDENCE=from_file\n", encoding="utf-8")
        monkeypatch.setenv("F3_TEST_PRECEDENCE", "from_shell")
        env_module.load_dotenv_if_present(env_file)
        assert os.environ["F3_TEST_PRECEDENCE"] == "from_shell"

    def test_finds_env_in_parent_directory(self, tmp_path, monkeypatch):
        """When called without a path, walks up from CWD to find `.env`."""
        env_file = tmp_path / ".env"
        env_file.write_text("F3_TEST_FOUND=yes\n", encoding="utf-8")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        monkeypatch.delenv("F3_TEST_FOUND", raising=False)

        assert env_module.load_dotenv_if_present() is True
        assert os.environ["F3_TEST_FOUND"] == "yes"
