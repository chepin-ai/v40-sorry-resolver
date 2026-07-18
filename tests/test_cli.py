"""CLI integration tests (N-1 regression, health gate N-4, mock e2e N-5).

The P0 bug was ``scanner.scan()`` called without the required ``paths``
argument, crashing every CLI entry point at startup; there was no CLI test
to catch it. These tests run the real CLI against a tiny tmp Lean project
with ``--mock-llm`` (deterministic fakes) — no network, no Lean toolchain.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeLLMClient, FakeRouter
from v40_sorry_resolver import cli
from v40_sorry_resolver.config import V40Config
from v40_sorry_resolver.llm.router import Role


@pytest.fixture()
def fake_lean_project(tmp_path):
    """A minimal scannable Lean project with one sorry."""
    proj = tmp_path / "fake_lean_proj"
    proj.mkdir()
    (proj / "lakefile.toml").write_text('name = "clitest"\n')
    (proj / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
    (proj / "A.lean").write_text("theorem cli_trivial : 1 + 1 = 2 := by\n  sorry\n")
    return str(proj)


@pytest.fixture()
def no_dotenv(monkeypatch):
    """Isolate from the repo-root .env: real keys must never enter tests."""
    real = V40Config.from_env.__func__
    monkeypatch.setattr(
        V40Config,
        "from_env",
        classmethod(lambda cls, env_file=".env": real(cls, None)),
    )
    return monkeypatch


def _argv(project, work_dir, *extra):
    return [
        "--project-paths",
        project,
        "--output-dir",
        str(work_dir),
        "--log-level",
        "WARNING",
        *extra,
    ]


# --------------------------------------------------------------------- help


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    assert "--mock-llm" in capsys.readouterr().out


# ------------------------------------------------------------------ dry-run


def test_dry_run_mock_llm(fake_lean_project, tmp_path, capsys, no_dotenv):
    """N-1 regression: --dry-run must scan (with paths!) and exit 0."""
    rc = cli.main(_argv(fake_lean_project, tmp_path / "work", "--dry-run", "--mock-llm"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] tasks=1" in out
    assert "cli_trivial" in out
    assert "[dry-run] llm health:" in out


# --------------------------------------------------------------- full run


def test_full_run_mock_llm_mock_verifier(
    fake_lean_project, tmp_path, capsys, no_dotenv
):
    """SPEC 5.2: a complete small run through the real CLI exits 0 and
    produces run json + checkpoint; mock results are marked [UNVERIFIED]."""
    work = tmp_path / "work"
    rc = cli.main(
        _argv(fake_lean_project, work, "--mock-llm", "--verifier", "mock", "--no-resume")
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "[UNVERIFIED]" in out

    # Run report artifact.
    runs = sorted((work / "results").glob("run_*.json"))
    assert runs, "run json artifact missing"
    report = json.loads(runs[-1].read_text(encoding="utf-8"))
    assert report["processed"] == 1
    assert len(report["results"]) == 1
    result = report["results"][0]
    # mock LLM proposes 'rfl', mock verifier requires the VALID marker ->
    # the task fails honestly (no fake solving anywhere).
    assert result["status"] == "FAILED_ALL"
    assert result["success"] is False
    assert result["unverified"] is True  # N-5 labeling in the artifact

    # Checkpoint artifact with a JSON metrics snapshot (N-7).
    checkpoint = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
    assert isinstance(checkpoint["metrics"], dict)
    assert checkpoint["metrics"]["tasks"]["processed"] == 1


def test_full_run_mock_llm_solves_with_valid_marker(
    fake_lean_project, tmp_path, capsys, no_dotenv, monkeypatch
):
    """A mock-LLM that emits the VALID marker solves the task through the
    full CLI path — still labeled unverified because the verifier is mock."""
    work = tmp_path / "work"
    original_generate = cli._MockLLMClient.generate

    async def valid_generate(self, prompt, **kwargs):
        resp = await original_generate(self, prompt, **kwargs)
        if self.role == Role.PROVER:
            resp.text = "```lean\nVALID rfl\n```"
        return resp

    monkeypatch.setattr(cli._MockLLMClient, "generate", valid_generate)
    rc = cli.main(
        _argv(fake_lean_project, work, "--mock-llm", "--verifier", "mock", "--no-resume")
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "[UNVERIFIED]" in out
    runs = sorted((work / "results").glob("run_*.json"))
    report = json.loads(runs[-1].read_text(encoding="utf-8"))
    assert report["solved"] == 1
    assert report["results"][0]["unverified"] is True


# -------------------------------------------------------------- health gate


@pytest.mark.asyncio
async def test_health_gate_aborts_when_all_providers_fail(
    fake_lean_project, tmp_path, no_dotenv, monkeypatch
):
    """N-4: real (non-mock) path runs the startup health check; when every
    provider fails, the CLI reports and exits non-zero BEFORE solving."""

    class _DeadRouter:
        def client(self, role):
            raise RuntimeError("no provider")

        async def health_check_all(self):
            return {"deepseek_a": False, "kimi": False}

        async def close(self):
            return None

    monkeypatch.setattr(
        cli.MultiLLMRouter,
        "from_config",
        classmethod(lambda cls, cfg, cache=None, metrics=None: _DeadRouter()),
    )
    args = cli.build_parser().parse_args(
        _argv(fake_lean_project, tmp_path / "work", "--verifier", "mock")
    )
    rc = await cli.async_main(args)
    assert rc == 2
    # Nothing ran: no results dir was produced.
    assert not (tmp_path / "work" / "results").exists()


@pytest.mark.asyncio
async def test_health_gate_passes_then_pipeline_runs(
    fake_lean_project, tmp_path, no_dotenv, monkeypatch
):
    """N-4 counterpart: a healthy (fake) router passes the gate and the real
    pipeline runs to completion through the non-mock CLI path."""
    router = FakeRouter(
        {
            Role.PROVER: FakeLLMClient(Role.PROVER, script="VALID proof"),
            Role.CRITIC: FakeLLMClient(Role.CRITIC),
        }
    )
    monkeypatch.setattr(
        cli.MultiLLMRouter,
        "from_config",
        classmethod(lambda cls, cfg, cache=None, metrics=None: router),
    )
    args = cli.build_parser().parse_args(
        _argv(fake_lean_project, tmp_path / "work", "--verifier", "mock", "--no-resume")
    )
    rc = await cli.async_main(args)
    assert rc == 0
    runs = sorted((tmp_path / "work" / "results").glob("run_*.json"))
    assert runs
    report = json.loads(runs[-1].read_text(encoding="utf-8"))
    assert report["solved"] == 1
    assert report["results"][0]["unverified"] is True  # --verifier mock
