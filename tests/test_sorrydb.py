"""SorryScanner / SorryDBClient tests (SPEC 3.9).

Key contract points covered:
  * mini project scan returns exactly the 11 real sorries, all theorem names
    correct, 1-based line/col, per-file priority heuristic, extracted goals;
  * theorem name = *nearest* theorem/lemma declaration *above* the sorry line
    (v39 P2-19 fix) — verified on a synthetic two-theorem file;
  * empty / sorry-free / missing scans -> WARNING + [] and **never** fake tasks
    (v39 P1-9 fix);
  * SorryDBClient disabled when endpoint is None; failures -> WARNING + [].
"""
from __future__ import annotations

import pytest

from v40_sorry_resolver.sorrydb import SorryDBClient, SorryScanner
from v40_sorry_resolver.models import PriorityLevel

from conftest import MINI_PROJECT

EXPECTED_NAMES = {
    "nat_refl", "one_plus_one", "and_comm_simple", "or_intro_simple",
    "list_length_append_simple",
    "add_zero_custom", "add_comm_custom", "mul_two", "list_map_id",
    "impossible_zero_eq_one", "unprovable_all_even",
}


@pytest.fixture(scope="module")
def tasks():
    return SorryScanner().scan([MINI_PROJECT])


def test_scan_returns_11_tasks_with_correct_names(tasks):
    assert len(tasks) == 11
    assert {t.theorem_name for t in tasks} == EXPECTED_NAMES


def test_line_col_are_1_based(tasks):
    by = {t.theorem_name: t for t in tasks}
    nat = by["nat_refl"]
    assert (nat.line_number, nat.column_number) == (8, 3)
    imp = by["impossible_zero_eq_one"]
    assert (imp.line_number, imp.column_number) == (10, 3)


def test_file_paths_and_project_root(tasks):
    for t in tasks:
        assert t.project_path == MINI_PROJECT
        assert t.file_path.startswith("LeanMiniProject/")
        assert t.file_path.endswith(".lean")
    by = {t.theorem_name: t for t in tasks}
    assert by["nat_refl"].file_path == "LeanMiniProject/Trivial.lean"
    assert by["impossible_zero_eq_one"].file_path == "LeanMiniProject/Hard.lean"


def test_priority_heuristic(tasks):
    by = {t.theorem_name: t for t in tasks}
    assert by["nat_refl"].priority is PriorityLevel.P2_MEDIUM          # trivial
    assert by["one_plus_one"].priority is PriorityLevel.P2_MEDIUM      # trivial
    assert by["add_comm_custom"].priority is PriorityLevel.P1_IMPORTANT  # medium
    assert by["impossible_zero_eq_one"].priority is PriorityLevel.P0_CRITICAL  # hard
    assert by["unprovable_all_even"].priority is PriorityLevel.P0_CRITICAL


def test_goal_extraction(tasks):
    by = {t.theorem_name: t for t in tasks}
    assert by["nat_refl"].goal_state == "n = n"
    assert by["one_plus_one"].goal_state == "1 + 1 = 2"
    assert by["and_comm_simple"].goal_state == "b ∧ a"
    assert "xs ++ ys" in by["list_length_append_simple"].goal_state


def test_task_ids_and_cache_key_stable(tasks):
    ids = [t.id for t in tasks]
    assert len(set(ids)) == len(ids)  # unique
    for t in tasks:
        assert len(t.id) == 12
        assert len(t.cache_key()) == 16
        # serialization round-trip
        assert type(t).from_dict(t.to_dict()).cache_key() == t.cache_key()


# ---------------------------------------------------------- nearest-decl fix
def test_theorem_name_is_nearest_decl_above(tmp_path):
    """A sorry must bind to the closest theorem above it, not the file's first."""
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    lean = tmp_path / "Two.lean"
    lean.write_text(
        "theorem first_theorem : True := by\n"
        "  trivial\n"
        "\n"
        "theorem second_theorem (n : Nat) : n = n := by\n"
        "  sorry\n"
        "\n"
        "lemma third_lemma : 1 + 1 = 2 := by\n"
        "  sorry\n"
    )
    found = SorryScanner().scan([str(tmp_path)])
    names = sorted(t.theorem_name for t in found)
    assert names == ["second_theorem", "third_lemma"]
    for t in found:
        assert t.project_path == str(tmp_path.resolve())


def test_sorry_in_second_of_two_sorries_same_file(tmp_path):
    """Every sorry is found; each binds to its own enclosing theorem."""
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    lean = tmp_path / "Multi.lean"
    lean.write_text(
        "theorem a : True := by\n"
        "  sorry\n"
        "theorem b : True := by\n"
        "  sorry\n"
    )
    found = SorryScanner().scan([str(tmp_path)])
    assert sorted(t.theorem_name for t in found) == ["a", "b"]


# ------------------------------------------------------------- no fake tasks
def test_scan_empty_dir_returns_empty(tmp_path):
    assert SorryScanner().scan([str(tmp_path)]) == []


def test_scan_missing_path_returns_empty():
    assert SorryScanner().scan(["/nonexistent/definitely/not/here"]) == []


def test_scan_sorry_free_project_returns_empty(tmp_path):
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "Ok.lean").write_text("theorem done : True := by\n  trivial\n")
    assert SorryScanner().scan([str(tmp_path)]) == []


def test_scan_ignores_dot_lake_and_hidden(tmp_path):
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    hidden = tmp_path / ".lake" / "build"
    hidden.mkdir(parents=True)
    (hidden / "Dep.lean").write_text("theorem dep : True := by\n  sorry\n")
    assert SorryScanner().scan([str(tmp_path)]) == []


def test_comment_sorry_not_counted(tmp_path):
    """`sorry` inside comments/docstrings must not be scanned as a real sorry."""
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "C.lean").write_text(
        "/- this mentions sorry but is a block comment -/\n"
        "-- and a sorry in a line comment\n"
        "theorem real_one (n : Nat) : n = n := by\n"
        "  sorry  -- the only real one\n"
    )
    found = SorryScanner().scan([str(tmp_path)])
    assert [t.theorem_name for t in found] == ["real_one"]
    assert len(found) == 1


# --------------------------------------------------------------- SorryDBClient
def test_sorrydb_client_disabled_without_endpoint():
    client = SorryDBClient(endpoint=None)
    assert client.enabled is False


@pytest.mark.asyncio
async def test_sorrydb_client_returns_empty_when_disabled():
    client = SorryDBClient(endpoint=None)
    assert await client.fetch_tasks() == []


@pytest.mark.asyncio
async def test_sorrydb_client_failure_returns_empty_not_fake():
    # Unreachable endpoint -> WARNING + [] (never injects fake tasks, v39 P1-9).
    client = SorryDBClient(endpoint="http://127.0.0.1:1/unreachable", timeout_s=1.0)
    assert client.enabled is True
    assert await client.fetch_tasks() == []


# ======================================================================
# Frontier integration: real SorryDB snapshots (frontier_resources sec. 1)
# ======================================================================

SORRYDB_ENTRY = {
    "id": "sorrydb-entry-001",
    "repo": {
        "remote": "https://github.com/example/lean-repo",
        "branch": "master",
        "commit": "abc123def456",
        "lean_version": "leanprover/lean4:v4.20.0",
    },
    "location": {
        "path": "LeanRepo/Basic.lean",
        "start_line": 42,
        "start_column": 5,
        "end_line": 42,
        "end_column": 10,
    },
    "debug_info": {
        "goal": "n m : Nat ⊢ n + m = m + n",
        "url": "https://sorrydb.org/sorries/sorrydb-entry-001",
    },
    "metadata": {"blame_date": "2026-01-01", "inclusion_date": "2026-01-11"},
}


def _snapshot_doc(*entries) -> str:
    import json

    return json.dumps({"repos": [{"remote": "https://github.com/example/lean-repo"}],
                       "sorries": list(entries)})


@pytest.mark.asyncio
async def test_sorrydb_client_local_json_snapshot(tmp_path):
    """Real SorryDB layout ({"repos": [...], "sorries": [...]}) from a local
    JSON file is parsed with the pydantic-model field mapping."""
    snap = tmp_path / "deduplicated_sorries.json"
    snap.write_text(_snapshot_doc(SORRYDB_ENTRY), encoding="utf-8")
    client = SorryDBClient(endpoint=str(snap))
    assert client.enabled is True

    tasks = await client.fetch_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "sorrydb-entry-001"
    assert t.file_path == "LeanRepo/Basic.lean"
    assert t.line_number == 42
    assert t.column_number == 5
    assert t.goal_state == "n m : Nat ⊢ n + m = m + n"
    # Repo remote becomes the project path when no local override is given.
    assert t.project_path == "https://github.com/example/lean-repo"
    assert "abc123def456" in t.surrounding_context
    # SorryDB schema has no theorem name; missing field tolerated as "".
    assert t.theorem_name == ""


@pytest.mark.asyncio
async def test_sorrydb_client_local_jsonl_snapshot(tmp_path):
    """JSONL (one sorry entry per line) is accepted as well."""
    import json

    snap = tmp_path / "sorries.jsonl"
    lines = [json.dumps(SORRYDB_ENTRY), json.dumps({**SORRYDB_ENTRY, "id": "e2"})]
    snap.write_text("\n".join(lines) + "\n", encoding="utf-8")
    client = SorryDBClient(endpoint=str(snap))

    tasks = await client.fetch_tasks(project_path="/local/clone")
    assert [t.id for t in tasks] == ["sorrydb-entry-001", "e2"]
    assert all(t.project_path == "/local/clone" for t in tasks)


@pytest.mark.asyncio
async def test_sorrydb_client_remote_url_mocked(monkeypatch):
    """Remote URL: httpx is fully mocked; no real network access."""
    import json

    class FakeResp:
        text = _snapshot_doc(SORRYDB_ENTRY)

        def raise_for_status(self):
            return None

        def json(self):
            return json.loads(self.text)

    class FakeClient:
        def __init__(self, *a, **k):
            self.requests = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, **kwargs):
            self.requests.append(url)
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = SorryDBClient(endpoint="https://sorrydb.example/snapshot.json")
    tasks = await client.fetch_tasks()
    assert len(tasks) == 1
    assert tasks[0].goal_state == "n m : Nat ⊢ n + m = m + n"


@pytest.mark.asyncio
async def test_sorrydb_client_malformed_entries_tolerated(tmp_path):
    """Entries missing location.path/start_line are skipped; valid ones kept."""
    bad = {"id": "bad-1", "repo": {"remote": "x"}, "location": {"path": ""}}
    snap = tmp_path / "snap.json"
    snap.write_text(_snapshot_doc(bad, SORRYDB_ENTRY), encoding="utf-8")
    client = SorryDBClient(endpoint=str(snap))

    tasks = await client.fetch_tasks()
    assert [t.id for t in tasks] == ["sorrydb-entry-001"]


@pytest.mark.asyncio
async def test_sorrydb_client_empty_and_garbage_payloads(tmp_path, caplog):
    """Empty file / garbage JSON -> WARNING + [], never fake tasks."""
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert await SorryDBClient(endpoint=str(empty)).fetch_tasks() == []

    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json at all", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert await SorryDBClient(endpoint=str(garbage)).fetch_tasks() == []


@pytest.mark.asyncio
async def test_sorrydb_client_missing_local_file(tmp_path, caplog):
    """Nonexistent local path -> WARNING + [] (no fake tasks, v39 P1-9)."""
    client = SorryDBClient(endpoint=str(tmp_path / "nope.json"))
    with caplog.at_level("WARNING"):
        assert await client.fetch_tasks() == []


# ======================================================================
# Frontier integration: SorryDB anti-cheat verification protocol (5.1)
# (pure-text helpers -> no Lean toolchain needed)
# ======================================================================

_ANTI_CHEAT_FILE = (
    "theorem foo (n : Nat) : n + 0 = n := by\n"
    "  sorry\n"
    "\n"
    "theorem bar : True := by\n"
    "  trivial\n"
)


def _make_verifier(tmp_path, sorrydb_mode=True, check_axioms=False):
    from v40_sorry_resolver.config import V40Config
    from v40_sorry_resolver.verify.subprocess_lean import SubprocessLeanVerifier

    cfg = V40Config(
        work_dir=str(tmp_path / "v40_work"),
        sorrydb_mode=sorrydb_mode,
        check_axioms=check_axioms,
    )
    return SubprocessLeanVerifier(cfg)


def _write_project(tmp_path, content=_ANTI_CHEAT_FILE):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "Main.lean").write_text(content, encoding="utf-8")
    return str(proj)


def _anti_cheat_task(project_path):
    from v40_sorry_resolver.models import SorryTask

    return SorryTask(
        id="ac-1",
        project_path=project_path,
        file_path="Main.lean",
        line_number=2,
        column_number=3,
        theorem_name="foo",
    )


@pytest.mark.asyncio
async def test_sorrydb_mode_valid_proof_passes_integrity(tmp_path, monkeypatch):
    """sorrydb_mode + honest proof: integrity checks pass, compile proceeds
    (subprocess layer mocked out; no Lean toolchain needed)."""
    from v40_sorry_resolver.verify.subprocess_lean import SubprocessLeanVerifier

    verifier = _make_verifier(tmp_path)
    project = _write_project(tmp_path)

    async def fake_run_lean(self, run_dir, rel_file):
        return 0, "", "", False

    monkeypatch.setattr(SubprocessLeanVerifier, "_run_lean", fake_run_lean)
    vr = await verifier.verify_proof(_anti_cheat_task(project), "simp")
    assert vr.ok is True, vr.error


@pytest.mark.asyncio
async def test_sorrydb_mode_statement_change_rejected(tmp_path):
    """Protocol (2): a splice that alters the theorem statement is rejected."""
    from v40_sorry_resolver.verify.subprocess_lean import VerificationError

    verifier = _make_verifier(tmp_path)
    project = _write_project(tmp_path)
    task = _anti_cheat_task(project)
    # Simulate a tampered splice output (statement weakened to `True`).
    tampered = (
        "theorem foo (n : Nat) : True := by\n"
        "  simp\n"
        "\n"
        "theorem bar : True := by\n"
        "  trivial\n"
    )
    with pytest.raises(VerificationError, match="statement modified"):
        verifier._sorrydb_integrity_check(task, tampered)


@pytest.mark.asyncio
async def test_sorrydb_mode_sorry_count_must_drop_exactly_one(tmp_path):
    """Protocol (1): sorry count in the target theorem must drop by exactly 1."""
    from v40_sorry_resolver.verify.subprocess_lean import VerificationError

    verifier = _make_verifier(tmp_path)
    project = _write_project(tmp_path)
    task = _anti_cheat_task(project)
    # New content where the sorry is NOT replaced (count unchanged).
    with pytest.raises(VerificationError, match="exactly 1"):
        verifier._sorrydb_integrity_check(task, _ANTI_CHEAT_FILE)


@pytest.mark.asyncio
async def test_sorrydb_mode_axioms_sorryAx_rejected(tmp_path, monkeypatch):
    """Protocol (3): check_axioms + `#print axioms` output with sorryAx -> reject."""
    from v40_sorry_resolver.verify.subprocess_lean import SubprocessLeanVerifier

    verifier = _make_verifier(tmp_path, check_axioms=True)
    project = _write_project(tmp_path)

    async def fake_run_lean(self, run_dir, rel_file):
        # Compile OK but the axioms report leaks sorryAx.
        return 0, "'foo' depends on axioms: [sorryAx]", "", False

    monkeypatch.setattr(SubprocessLeanVerifier, "_run_lean", fake_run_lean)
    vr = await verifier.verify_proof(_anti_cheat_task(project), "simp")
    assert vr.ok is False
    assert "sorryAx" in (vr.error or "")


@pytest.mark.asyncio
async def test_sorrydb_mode_off_keeps_default_behavior(tmp_path, monkeypatch):
    """Default (sorrydb_mode=False) path is untouched by the protocol."""
    from v40_sorry_resolver.verify.subprocess_lean import SubprocessLeanVerifier

    verifier = _make_verifier(tmp_path, sorrydb_mode=False)
    project = _write_project(tmp_path)

    async def fake_run_lean(self, run_dir, rel_file):
        return 0, "", "", False

    monkeypatch.setattr(SubprocessLeanVerifier, "_run_lean", fake_run_lean)
    vr = await verifier.verify_proof(_anti_cheat_task(project), "simp")
    assert vr.ok is True, vr.error


def test_theorem_facts_counts_sorries_and_statement(tmp_path):
    """_theorem_facts: normalized statement + per-block sorry count."""
    verifier = _make_verifier(tmp_path)
    text = (
        "theorem multi (n : Nat) : n = n := by\n"
        "  have h : n = n := by sorry\n"
        "  sorry  -- trailing comment with sorry word\n"
    )
    stmt, count = verifier._theorem_facts(text, "multi", 3)
    assert count == 2  # comment occurrence is stripped
    assert stmt == "theorem multi (n : Nat) : n = n"


# --------------------------------------- roadmap: comment/string-aware scan


def test_string_literal_sorry_not_counted(tmp_path):
    """`sorry` inside a string literal is not a real sorry (Kaggle 2026-07-21
    mathlib false positive: Basic.lean:149 was inside a string/comment)."""
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "S.lean").write_text(
        'theorem real_one : True := by\n'
        '  trivial\n'
        'def msg : String := "do not leave sorry in proofs"\n'
        'def msg2 := "escaped \\" sorry quote"\n'
        'theorem real_two : 1 = 1 := by\n'
        '  sorry\n'
    )
    found = SorryScanner().scan([str(tmp_path)])
    assert [t.theorem_name for t in found] == ["real_two"]
    assert found[0].line_number == 6  # real file line, not a stripped-text line


def test_nested_block_comment_sorry_not_counted(tmp_path):
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "N.lean").write_text(
        "/- outer sorry\n"
        "   /- inner sorry -/\n"
        "   still outer sorry -/\n"
        "theorem t : True := by\n"
        "  trivial\n"
    )
    assert SorryScanner().scan([str(tmp_path)]) == []


def test_example_decl_is_sorry_container(tmp_path):
    """Nameless `example` declarations are collected as sorry containers."""
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "E.lean").write_text(
        "example : True := by\n"
        "  sorry\n"
        "example (n : Nat) : n + 0 = n := by\n"
        "  sorry\n"
    )
    found = SorryScanner().scan([str(tmp_path)])
    assert len(found) == 2
    assert all(t.theorem_name.startswith("example_") for t in found)
    assert found[0].line_number == 2 and found[1].line_number == 4
    assert found[1].goal_state == "n + 0 = n"


def test_nameless_instance_is_sorry_container(tmp_path):
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "I.lean").write_text(
        "instance : Inhabited Nat := by\n"
        "  sorry\n"
    )
    found = SorryScanner().scan([str(tmp_path)])
    assert len(found) == 1
    assert found[0].theorem_name.startswith("instance_")
    assert found[0].goal_state == "Inhabited Nat"


def test_def_sorry_recorded_with_warning(tmp_path, caplog):
    """def sorries are recorded but flagged (verification splices by theorem)."""
    import logging

    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "D.lean").write_text(
        "def compute : Nat := by\n"
        "  sorry\n"
    )
    with caplog.at_level(logging.WARNING):
        found = SorryScanner().scan([str(tmp_path)])
    assert len(found) == 1
    assert found[0].theorem_name == "compute"
    assert any("inside a `def`" in r.message for r in caplog.records)


def test_mathlib_style_file_yields_zero_tasks_with_stats(tmp_path):
    """mathlib CI enforces zero sorries; the word 'sorry' appears in comments
    and docstrings. The scan must return 0 tasks and still report stats."""
    (tmp_path / "lakefile.toml").write_text('name = "x"\n')
    (tmp_path / "Basic.lean").write_text(
        "/-! # Category theory basics\n"
        "This module used to contain a `sorry`, removed upstream. -/\n"
        "-- See the sorry-free policy in mathlib CI.\n"
        "theorem id_comp : True := by\n"
        "  trivial\n"
        "/- multi\n"
        "   /- nested sorry mention -/\n"
        "   line -/\n"
        "lemma comp_id : True := by\n"
        "  trivial\n"
        'def note := "sorry is forbidden here"\n'
    )
    scanner = SorryScanner()
    assert scanner.scan([str(tmp_path)]) == []
    assert scanner.last_stats["files"] >= 1
    assert scanner.last_stats["declarations"] >= 2
    assert scanner.last_stats["sorries"] == 0
