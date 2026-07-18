"""Tests for v40_sorry_resolver.config (SPEC 3.2)."""

import logging
import re
from pathlib import Path

import pytest

from v40_sorry_resolver import config as config_mod
from v40_sorry_resolver.config import LLMProviderConfig, V40Config

ALL_ENV_VARS = [
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY_2",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_REASONER_MODEL",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_MODEL",
    "LONGCAT_API_KEY",
    "LONGCAT_BASE_URL",
    "LONGCAT_MODEL",
    "V40_VERIFIER",
    "V40_NUM_WORKERS",
]


@pytest.fixture()
def clean_env(monkeypatch):
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestFromEnv:
    def test_missing_keys_disable_providers_and_warn(self, clean_env, caplog):
        with caplog.at_level(logging.WARNING, logger="v40_sorry_resolver.config"):
            cfg = V40Config.from_env(env_file=None)
        assert set(cfg.providers) == {"deepseek_a", "deepseek_b", "kimi", "longcat"}
        for provider in cfg.providers.values():
            assert provider.enabled is False
            assert provider.api_key == ""
        warnings = [r.getMessage() for r in caplog.records]
        assert any("DEEPSEEK_API_KEY" in w for w in warnings)
        assert any("DEEPSEEK_API_KEY_2" in w for w in warnings)
        assert any("KIMI_API_KEY" in w for w in warnings)
        assert any("LONGCAT_API_KEY" in w for w in warnings)

    def test_real_default_models_and_urls(self, clean_env):
        cfg = V40Config.from_env(env_file=None)
        assert cfg.providers["deepseek_a"].model == "deepseek-chat"
        assert cfg.providers["deepseek_a"].base_url == "https://api.deepseek.com/v1"
        assert cfg.providers["deepseek_b"].model == "deepseek-chat"
        assert cfg.providers["kimi"].model == "moonshot-v1-8k"
        assert cfg.providers["kimi"].base_url == "https://api.moonshot.cn/v1"
        assert cfg.providers["longcat"].model == "LongCat-Flash-Chat"
        assert (
            cfg.providers["longcat"].base_url
            == "https://api.longcat.chat/openapi/v1"
        )
        assert cfg.deepseek_reasoner_model == "deepseek-reasoner"

    def test_keys_enable_providers(self, clean_env):
        clean_env.setenv("DEEPSEEK_API_KEY", "test-key-a")
        clean_env.setenv("DEEPSEEK_API_KEY_2", "test-key-b")
        clean_env.setenv("KIMI_API_KEY", "test-key-kimi")
        clean_env.setenv("LONGCAT_API_KEY", "test-key-longcat")
        cfg = V40Config.from_env(env_file=None)
        assert all(p.enabled for p in cfg.providers.values())
        assert cfg.providers["deepseek_a"].api_key == "test-key-a"
        assert cfg.providers["deepseek_b"].api_key == "test-key-b"

    def test_env_overrides_models(self, clean_env):
        clean_env.setenv("DEEPSEEK_API_KEY", "k")
        clean_env.setenv("DEEPSEEK_MODEL", "deepseek-chat-custom")
        clean_env.setenv("DEEPSEEK_REASONER_MODEL", "deepseek-reasoner-x")
        cfg = V40Config.from_env(env_file=None)
        assert cfg.providers["deepseek_a"].model == "deepseek-chat-custom"
        assert cfg.providers["deepseek_b"].model == "deepseek-chat-custom"
        assert cfg.deepseek_reasoner_model == "deepseek-reasoner-x"

    def test_dotenv_file_is_read(self, clean_env, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment\n"
            "KIMI_API_KEY=file-key-kimi\n"
            "export LONGCAT_API_KEY='file-key-longcat'\n"
            'DEEPSEEK_MODEL="deepseek-chat-from-file" # trailing comment\n'
            "\n"
            "BROKEN LINE WITHOUT EQUALS\n",
            encoding="utf-8",
        )
        cfg = V40Config.from_env(env_file=str(env_file))
        assert cfg.providers["kimi"].enabled is True
        assert cfg.providers["kimi"].api_key == "file-key-kimi"
        assert cfg.providers["longcat"].api_key == "file-key-longcat"
        assert cfg.providers["deepseek_a"].model == "deepseek-chat-from-file"

    def test_real_env_beats_dotenv(self, clean_env, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("KIMI_API_KEY=file-key\n", encoding="utf-8")
        clean_env.setenv("KIMI_API_KEY", "env-key")
        cfg = V40Config.from_env(env_file=str(env_file))
        assert cfg.providers["kimi"].api_key == "env-key"

    def test_missing_dotenv_file_is_ok(self, clean_env, tmp_path):
        cfg = V40Config.from_env(env_file=str(tmp_path / "does-not-exist.env"))
        assert isinstance(cfg, V40Config)

    def test_verifier_and_workers_env(self, clean_env):
        clean_env.setenv("V40_VERIFIER", "dojo")
        clean_env.setenv("V40_NUM_WORKERS", "16")
        cfg = V40Config.from_env(env_file=None)
        assert cfg.verifier == "dojo"
        assert cfg.num_workers == 16

    def test_invalid_workers_falls_back(self, clean_env):
        clean_env.setenv("V40_NUM_WORKERS", "not-a-number")
        cfg = V40Config.from_env(env_file=None)
        assert cfg.num_workers == 8

    def test_spec_defaults(self, clean_env):
        cfg = V40Config.from_env(env_file=None)
        assert cfg.verifier == "subprocess"
        assert cfg.lean_timeout_s == 30.0
        assert cfg.max_concurrent_lean == 4
        assert cfg.num_workers == 8
        assert cfg.wall_clock_budget_s == 36000.0
        assert cfg.per_task_time_budget_s == 600.0
        assert cfg.per_task_token_budget == 200_000
        assert cfg.soft_deadline_s == 32400.0
        assert cfg.tactic_search_depth == 4
        assert cfg.tactic_search_width == 2
        assert cfg.agentic_max_iterations == 8
        assert cfg.agentic_stall_patience == 3
        assert cfg.thinking_max_tokens == 2048
        assert cfg.escalation_threshold == 3
        assert cfg.axiom_quota == 45
        assert cfg.llm_temperature == 0.3
        assert cfg.work_dir == "./v40_work"
        assert cfg.checkpoint_interval_tasks == 10
        assert cfg.lean_project_paths == ["/mnt/agents/output/lean_mini_project"]
        assert cfg.sorrydb_endpoint is None


class TestValidate:
    def make_valid_config(self, tmp_path):
        cfg = V40Config()
        cfg.lean_project_paths = [str(tmp_path)]
        cfg.providers = {
            "deepseek_a": LLMProviderConfig(
                name="deepseek_a",
                base_url="https://api.deepseek.com/v1",
                api_key="k",
                model="deepseek-chat",
            )
        }
        return cfg

    def test_valid_config_has_no_problems(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        assert cfg.validate() == []

    def test_returns_human_readable_strings(self, clean_env):
        problems = V40Config.from_env(env_file=None).validate()
        assert isinstance(problems, list)
        assert all(isinstance(p, str) and p for p in problems)
        assert any("deepseek_a" in p for p in problems)
        assert any("no LLM provider is enabled" in p for p in problems)

    def test_invalid_verifier_flagged(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        cfg.verifier = "bogus"
        assert any("verifier" in p for p in cfg.validate())

    def test_mock_verifier_flagged_as_unverified(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        cfg.verifier = "mock"
        assert any("UNVERIFIED" in p for p in cfg.validate())

    def test_thinking_timeout_floor(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        cfg.providers["deepseek_a"].thinking_timeout_s = 120.0
        assert any("thinking_timeout_s" in p for p in cfg.validate())

    def test_normal_timeout_ceiling(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        cfg.providers["deepseek_a"].timeout_s = 90.0
        assert any("timeout_s" in p for p in cfg.validate())

    def test_soft_deadline_after_budget_flagged(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        cfg.soft_deadline_s = cfg.wall_clock_budget_s + 1
        assert any("soft_deadline_s" in p for p in cfg.validate())

    def test_missing_project_path_flagged(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        cfg.lean_project_paths = [str(tmp_path / "nope")]
        assert any("does not exist" in p for p in cfg.validate())

    def test_bad_numeric_fields_flagged(self, tmp_path):
        cfg = self.make_valid_config(tmp_path)
        cfg.num_workers = 0
        cfg.lean_timeout_s = -1
        cfg.thinking_max_tokens = 0
        problems = cfg.validate()
        assert any("num_workers" in p for p in problems)
        assert any("lean_timeout_s" in p for p in problems)
        assert any("thinking_max_tokens" in p for p in problems)


class TestNoHardcodedSecrets:
    def test_default_keys_empty(self, clean_env):
        cfg = V40Config.from_env(env_file=None)
        for provider in cfg.providers.values():
            assert provider.api_key == ""

    def test_source_contains_no_api_key_pattern(self):
        source = Path(config_mod.__file__).read_text(encoding="utf-8")
        assert not re.search(r"sk-[A-Za-z0-9]{8,}", source)
