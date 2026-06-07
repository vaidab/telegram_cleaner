"""CLI safety-gate tests via Typer's CliRunner. The two invariants that prevent
data loss — "nothing fires without --yes" and "nothing fires after final no" —
live here. Network seams (gather_candidates, run_actions) are monkeypatched."""

from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

import cleaner
from cleaner import BatchResult, Candidate, Category, DialogInfo, Kind, app

runner = CliRunner()
NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)


def make_candidate(category: Category, id: int = 1, kind: Kind = Kind.USER) -> Candidate:
    return Candidate(
        DialogInfo(
            id=id,
            title=f"chat-{id}",
            kind=kind,
            last_message_date=NOW - timedelta(days=900),
            dialog_date=NOW - timedelta(days=1000),
        ),
        category,
    )


@pytest.fixture
def actions_recorder(monkeypatch):
    """Patch the two network seams; record every batch handed to run_actions."""
    executed: list[list[Candidate]] = []

    def fake_run_actions(candidates):
        executed.append(list(candidates))
        return BatchResult(done=list(candidates), failed=[])

    monkeypatch.setattr(cleaner, "run_actions", fake_run_actions)
    return executed


def patch_candidates(monkeypatch, candidates):
    monkeypatch.setattr(cleaner, "gather_candidates", lambda thresholds: list(candidates))


# --- purge-ghosts: the --yes gate -------------------------------------------------


def test_purge_without_yes_fires_nothing(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.GHOST)])
    result = runner.invoke(app, ["purge-ghosts"])
    assert result.exit_code == 0
    assert actions_recorder == []  # THE invariant: dry run by default
    assert "DRY RUN" in result.output


def test_purge_with_yes_fires_only_ghosts(monkeypatch, actions_recorder):
    ghost = make_candidate(Category.GHOST, id=1)
    stale = make_candidate(Category.STALE_DM, id=2)
    tomb = make_candidate(Category.FORBIDDEN, id=3, kind=Kind.FORBIDDEN)
    patch_candidates(monkeypatch, [ghost, stale, tomb])
    result = runner.invoke(app, ["purge-ghosts", "--yes"])
    assert result.exit_code == 0
    assert len(actions_recorder) == 1
    assert [c.info.id for c in actions_recorder[0]] == [1]  # stale and tomb untouched


def test_purge_include_forbidden_widens_candidates(monkeypatch, actions_recorder):
    ghost = make_candidate(Category.GHOST, id=1)
    tomb = make_candidate(Category.FORBIDDEN, id=3, kind=Kind.FORBIDDEN)
    patch_candidates(monkeypatch, [ghost, tomb])
    result = runner.invoke(app, ["purge-ghosts", "--yes", "--include-forbidden"])
    assert result.exit_code == 0
    assert [c.info.id for c in actions_recorder[0]] == [1, 3]


def test_purge_with_no_ghosts_exits_cleanly(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM)])
    result = runner.invoke(app, ["purge-ghosts", "--yes"])
    assert result.exit_code == 0
    assert actions_recorder == []
    assert "No ghosts" in result.output


# --- review: per-item marks and the final gate -------------------------------------


def test_review_final_no_fires_nothing(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM, id=i) for i in (1, 2)])
    # approve both, then refuse the final gate
    result = runner.invoke(app, ["review"], input="y\ny\nn\n")
    assert result.exit_code == 0
    assert actions_recorder == []  # THE second invariant
    assert "Cancelled" in result.output


def test_review_executes_only_approved_after_final_yes(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM, id=i) for i in (1, 2, 3)])
    # approve 1, skip 2, approve 3, confirm batch
    result = runner.invoke(app, ["review"], input="y\nn\ny\ny\n")
    assert result.exit_code == 0
    assert len(actions_recorder) == 1
    assert [c.info.id for c in actions_recorder[0]] == [1, 3]


def test_review_all_skipped_executes_nothing(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM)])
    result = runner.invoke(app, ["review"], input="n\n")
    assert result.exit_code == 0
    assert actions_recorder == []
    assert "Nothing approved" in result.output


def test_review_k_appends_keeplist_and_fires_nothing(monkeypatch, tmp_path, actions_recorder):
    monkeypatch.setattr(cleaner, "KEEPLIST_PATH", tmp_path / "keeplist.json")
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM, id=77)])
    result = runner.invoke(app, ["review"], input="k\n")
    assert result.exit_code == 0
    assert actions_recorder == []
    assert cleaner.load_keeplist() == {77}


def test_review_rejects_unreviewable_types(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.GHOST)])
    result = runner.invoke(app, ["review", "--types", "ghost"])
    assert result.exit_code == 1
    assert "purge-only" in result.output
    assert actions_recorder == []


def test_review_has_no_yes_flag():
    result = runner.invoke(app, ["review", "--yes"])
    assert result.exit_code != 0  # --yes is purge-ghosts only, by design


# --- env validation ------------------------------------------------------------------


@pytest.fixture
def no_credentials(monkeypatch):
    """Simulate a machine with no .env and no exported credentials.

    load_dotenv() walks up from cleaner.py's directory, so a developer's real
    .env would leak into these tests without this patch."""
    monkeypatch.setattr(cleaner, "load_dotenv", lambda: None)
    for var in ("TG_API_ID", "TG_API_HASH", "TG_PHONE"):
        monkeypatch.delenv(var, raising=False)


def test_missing_env_vars_abort_scan_with_runbook_pointer(no_credentials):
    # gather_candidates NOT patched: scan must die at validation, before any network
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 1
    assert "runbook/auth.md" in result.output
    assert "TG_API_ID" in result.output


def test_validate_env_raises_typer_exit_directly(no_credentials):
    import typer as typer_mod

    with pytest.raises(typer_mod.Exit):
        cleaner.validate_env()
