"""Tests for the self-contained standalone bundle generator + bootstrap.

The generator (tools/make_standalone_bundle.py) is loaded by path (tools/
is not a package). The *generated* bundle is built into a tmp dir and
exec'd so its bootstrap helpers can be unit-tested without network.
"""

from __future__ import annotations

import importlib.util
import os
import py_compile
import sys
import zipfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR = os.path.join(REPO_ROOT, "tools", "make_standalone_bundle.py")


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_module(GENERATOR, "make_standalone_bundle")


@pytest.fixture(scope="module")
def mini_project(tmp_path_factory):
    """A fake lean_mini_project with noise dirs that must be excluded."""
    root = tmp_path_factory.mktemp("mini") / "lean_mini_project"
    (root / "LeanMiniProject").mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
    (root / "lakefile.toml").write_text('name = "LeanMiniProject"\n')
    (root / "LeanMiniProject.lean").write_text("import LeanMiniProject.Trivial\n")
    (root / "LeanMiniProject" / "Trivial.lean").write_text("theorem t : 1 = 1 := by\n  sorry\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".lake").mkdir()
    (root / ".lake" / "build.olean").write_text("bin")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_text("pyc")
    return str(root)


@pytest.fixture(scope="module")
def bundle_path(generator, mini_project, tmp_path_factory):
    out = tmp_path_factory.mktemp("dist") / "v40_standalone.py"
    return generator.build_bundle(mini_project, str(out))


@pytest.fixture(scope="module")
def bundle(bundle_path):
    return _load_module(bundle_path, "v40_standalone_under_test")


# ------------------------------------------------------------- generator


def test_build_creates_file(bundle_path):
    assert os.path.isfile(bundle_path)
    assert os.path.getsize(bundle_path) > 10_000


def test_generated_file_pycompiles(bundle_path):
    py_compile.compile(bundle_path, doraise=True)


def test_pkg_zip_contains_package(bundle):
    import base64
    import io

    blob = base64.b64decode("".join(bundle._PKG_B64))
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert "v40_sorry_resolver/cli.py" in names
    assert "v40_sorry_resolver/engine/orchestrator.py" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)


def test_mini_zip_contents_and_exclusions(bundle):
    import base64
    import io

    blob = base64.b64decode("".join(bundle._MINI_PROJECT_B64))
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert "lakefile.toml" in names
    assert "lean-toolchain" in names
    assert "LeanMiniProject/Trivial.lean" in names
    assert not any(n.startswith(".git") or "/.git" in n for n in names)
    assert not any(n.startswith(".lake") or "/.lake" in n for n in names)
    assert not any("__pycache__" in n for n in names)


def test_unpack_mini_project_roundtrip(bundle, tmp_path):
    dest = bundle.unpack_mini_project(str(tmp_path))
    assert os.path.isfile(os.path.join(dest, "lakefile.toml"))
    assert os.path.isfile(os.path.join(dest, "LeanMiniProject", "Trivial.lean"))
    assert not os.path.exists(os.path.join(dest, ".git"))


def test_unpack_bundle_adds_sys_path(bundle, tmp_path):
    dest = bundle.unpack_bundle(str(tmp_path / "src"))
    assert dest in sys.path
    assert os.path.isfile(os.path.join(dest, "v40_sorry_resolver", "cli.py"))


# --------------------------------------------------------- flag handling


def test_strip_bootstrap_flags(bundle):
    cleaned, flags = bundle.strip_bootstrap_flags(
        ["--self-test", "--project", "x", "--real-llm", "--skip-bootstrap"]
    )
    assert cleaned == ["--project", "x"]
    assert flags == {"self_test", "real_llm", "skip_bootstrap"}


def test_argv_value_forms(bundle):
    assert bundle._argv_value(["--verifier", "dojo"], "--verifier") == "dojo"
    assert bundle._argv_value(["--verifier=repl"], "--verifier") == "repl"
    assert bundle._argv_value(["--workers", "4"], "--verifier") is None


def test_needs_optional_verifier_deps(bundle):
    assert bundle.needs_optional_verifier_deps(["--verifier", "dojo"])
    assert bundle.needs_optional_verifier_deps(["--verifier=repl"])
    assert bundle.needs_optional_verifier_deps(["--verifier", "hybrid"])
    assert not bundle.needs_optional_verifier_deps(["--verifier", "subprocess"])
    assert not bundle.needs_optional_verifier_deps([])


# ------------------------------------------------------- github: parsing


@pytest.mark.parametrize(
    "spec,expected",
    [
        (
            "github:owner/repo",
            {"owner": "owner", "repo": "repo", "subdir": "", "ref": None},
        ),
        (
            "github:owner/repo/sub/dir",
            {"owner": "owner", "repo": "repo", "subdir": "sub/dir", "ref": None},
        ),
        (
            "github:owner/repo@v1.2",
            {"owner": "owner", "repo": "repo", "subdir": "", "ref": "v1.2"},
        ),
        (
            "github:owner/repo/sub@feature/x",
            {"owner": "owner", "repo": "repo", "subdir": "sub", "ref": "feature/x"},
        ),
    ],
)
def test_parse_github_spec_valid(bundle, spec, expected):
    assert bundle.parse_github_spec(spec) == expected


@pytest.mark.parametrize("spec", ["github:bad", "github:", "github:o/r/", "http://x"])
def test_parse_github_spec_invalid(bundle, spec):
    assert bundle.parse_github_spec(spec) is None


def test_clone_urls_without_token(bundle, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    urls = bundle._clone_urls("o", "r")
    assert urls[0] == "https://github.com/o/r.git"
    assert urls[1] == bundle.GHFAST_PREFIX + "https://github.com/o/r.git"


def test_clone_urls_with_token(bundle, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekret")
    urls = bundle._clone_urls("o", "r")
    assert urls[0] == "https://x-access-token:sekret@github.com/o/r.git"
    assert urls[1].startswith(bundle.GHFAST_PREFIX)


def test_codeload_urls(bundle):
    urls = bundle._codeload_urls("o", "r", None)
    assert urls[0] == "https://codeload.github.com/o/r/zip/HEAD"
    assert urls[1] == bundle.GHFAST_PREFIX + urls[0]
    assert bundle._codeload_urls("o", "r", "dev")[0].endswith("/zip/dev")


# --------------------------------------------------------- key bootstrap


def test_bootstrap_keys_env_wins(bundle, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=from-file\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    monkeypatch.setattr(bundle, "_kaggle_secret", lambda name: None)
    assert bundle.bootstrap_keys([], env_file=str(env)) is True
    assert os.environ["DEEPSEEK_API_KEY"] == "from-env"


def test_bootstrap_keys_from_env_file(bundle, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nKIMI_API_KEY=file-key\n\n")
    for name in bundle.LLM_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bundle, "_kaggle_secret", lambda name: None)
    assert bundle.bootstrap_keys([], env_file=str(env)) is True
    assert os.environ["KIMI_API_KEY"] == "file-key"


def test_bootstrap_keys_kaggle_beats_file(bundle, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("LONGCAT_API_KEY=file-key\n")
    for name in bundle.LLM_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        bundle,
        "_kaggle_secret",
        lambda name: "kaggle-key" if name == "LONGCAT_API_KEY" else None,
    )
    assert bundle.bootstrap_keys([], env_file=str(env)) is True
    assert os.environ["LONGCAT_API_KEY"] == "kaggle-key"


def test_bootstrap_keys_none_found(bundle, monkeypatch, tmp_path):
    for name in bundle.LLM_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bundle, "_kaggle_secret", lambda name: None)
    assert bundle.bootstrap_keys([], env_file=str(tmp_path / "nope.env")) is False


def test_parse_env_file_quotes(bundle, tmp_path):
    env = tmp_path / ".env"
    env.write_text('A="quoted"\nB=\'single\'\nC = spaced \n')
    values = bundle._parse_env_file(str(env))
    assert values == {"A": "quoted", "B": "single", "C": "spaced"}


# ------------------------------------------------------------- self-test


def _report(solved, vpr, hard_rejects):
    results = [
        {"task_id": f"Hard.lean:h{i}", "success": False} for i in range(hard_rejects)
    ]
    return {"solved": solved, "verify_pass_rate": vpr, "results": results}


def test_self_test_eval_pass(bundle):
    ok, reasons = bundle.evaluate_self_test(_report(9, 1.0, 2))
    assert ok and reasons == []


def test_self_test_eval_boundary_solved(bundle):
    ok, _ = bundle.evaluate_self_test(_report(bundle.SELF_TEST_MIN_SOLVED, 1.0, 2))
    assert ok


def test_self_test_eval_fail_low_solved(bundle):
    ok, reasons = bundle.evaluate_self_test(_report(6, 1.0, 2))
    assert not ok and any("solved" in r for r in reasons)


def test_self_test_eval_fail_vpr(bundle):
    ok, reasons = bundle.evaluate_self_test(_report(9, 0.9, 2))
    assert not ok and any("verify_pass_rate" in r for r in reasons)


def test_self_test_eval_fail_hard(bundle):
    ok, reasons = bundle.evaluate_self_test(_report(9, 1.0, 1))
    assert not ok and any("Hard" in r for r in reasons)


def test_self_test_eval_with_scanned_hard_ids(bundle):
    report = {
        "solved": 8,
        "verify_pass_rate": 1.0,
        "results": [
            {"task_id": "aaa", "success": False},
            {"task_id": "bbb", "success": False},
            {"task_id": "ccc", "success": True},
        ],
    }
    ok, reasons = bundle.evaluate_self_test(report, hard_ids={"aaa", "bbb"})
    assert ok and reasons == []
    ok, reasons = bundle.evaluate_self_test(report, hard_ids={"aaa", "ccc"})
    assert not ok and any("Hard" in r for r in reasons)


# ------------------------------------------------------------- lean install


def test_lean_asset_name_linux(bundle, monkeypatch):
    monkeypatch.setattr(bundle.sys, "platform", "linux")
    assert bundle._lean_asset_name("4.20.0") == "lean-4.20.0-linux.tar.zst"


def test_prepend_path_idempotent(bundle, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    bundle._prepend_path("/opt/x")
    bundle._prepend_path("/opt/x")
    assert os.environ["PATH"].split(os.pathsep).count("/opt/x") == 1
    assert os.environ["PATH"].startswith("/opt/x")


def test_ensure_lean_reuses_warm_toolchain_dir(bundle, monkeypatch, tmp_path):
    monkeypatch.setattr(bundle, "_lake_on_path", lambda: None)
    fakebin = tmp_path / ".v40" / "toolchains" / "lean" / "bin"
    fakebin.mkdir(parents=True)
    (fakebin / "lake").write_text("#!fake\n")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(bundle.os.path, "expanduser", lambda p: str(tmp_path))
    monkeypatch.setattr(bundle, "_elan_home", lambda: str(tmp_path / "no-elan"))
    monkeypatch.setattr(
        bundle, "_try_elan_install", lambda: pytest.fail("must not reinstall")
    )
    assert bundle.ensure_lean() is True
    assert str(fakebin) in os.environ["PATH"].split(os.pathsep)


def test_ensure_lean_skips_when_lake_present(bundle, monkeypatch):
    monkeypatch.setattr(bundle, "_lake_on_path", lambda: "/usr/bin/lake")
    monkeypatch.setattr(
        bundle,
        "_try_elan_install",
        lambda: pytest.fail("must not attempt install when lake exists"),
    )
    assert bundle.ensure_lean() is True


def test_with_ghfast_fallback_order(bundle):
    urls = bundle._with_ghfast("https://github.com/x/y")
    assert urls == ["https://github.com/x/y", bundle.GHFAST_PREFIX + "https://github.com/x/y"]
