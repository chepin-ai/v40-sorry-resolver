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
