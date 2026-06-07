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


def test_review_shows_id_and_last_message(monkeypatch, actions_recorder):
    import dataclasses

    base = make_candidate(Category.STALE_DM, id=987654)
    info = dataclasses.replace(base.info, snippet="see you tomorrow then")
    patch_candidates(monkeypatch, [Candidate(info, Category.STALE_DM)])
    result = runner.invoke(app, ["review"], input="n\n")
    assert result.exit_code == 0
    assert "id: 987654" in result.output
    assert "see you tomorrow then" in result.output
    assert "last message:" in result.output


# --- chat_link: deep links to open the chat locally ---------------------------------


def make_info(**overrides):
    import dataclasses

    return dataclasses.replace(make_candidate(Category.STALE_DM).info, **overrides)


def test_chat_links_public_username():
    info = make_info(id=-1001234, kind=Kind.BROADCAST, username="somechannel")
    assert cleaner.chat_links(info) == ["https://t.me/somechannel"]


def test_chat_links_user_by_id_has_no_link():
    info = make_info(id=987654, kind=Kind.USER, username=None)
    assert cleaner.chat_links(info) == []


def test_chat_links_supergroup_member_link():
    info = make_info(id=-1001234567890, kind=Kind.MEGAGROUP, username=None)
    assert cleaner.chat_links(info) == ["https://t.me/c/1234567890"]


def test_chat_links_basic_group_returns_empty():
    info = make_info(id=-4321, kind=Kind.GROUP, username=None)
    assert cleaner.chat_links(info) == []


def test_review_shows_https_link_for_named_chat(monkeypatch, actions_recorder):
    import dataclasses
    base = make_candidate(Category.UNREAD_CHANNEL, id=987654, kind=Kind.BROADCAST)
    info = dataclasses.replace(base.info, username="mychannel")
    patch_candidates(monkeypatch, [Candidate(info, Category.UNREAD_CHANNEL)])
    result = runner.invoke(app, ["review", "--types", "unread_channel"], input="n\n")
    assert result.exit_code == 0
    assert "https://t.me/mychannel" in result.output


def test_q_approves_current_item_and_stops(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM, id=i) for i in (1, 2, 3)])
    # approve 1, q on 2 (approves 2 + stops, skips 3), confirm batch
    result = runner.invoke(app, ["review"], input="y\nq\ny\n")
    assert result.exit_code == 0
    assert len(actions_recorder) == 1
    assert [c.info.id for c in actions_recorder[0]] == [1, 2]


def test_q_on_first_item_approves_and_executes_it(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM, id=i) for i in (1, 2, 3)])
    # q on item 1: approves it, skips 2+3, final confirmation
    result = runner.invoke(app, ["review"], input="q\ny\n")
    assert result.exit_code == 0
    assert len(actions_recorder) == 1
    assert [c.info.id for c in actions_recorder[0]] == [1]
    assert "Stopping" in result.output


def test_approve_all_skips_per_item_triage_but_final_yes_executes(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM, id=i) for i in (1, 2, 3)])
    result = runner.invoke(app, ["review", "--approve-all"], input="y\n")
    assert result.exit_code == 0
    assert len(actions_recorder) == 1
    assert [c.info.id for c in actions_recorder[0]] == [1, 2, 3]


def test_approve_all_final_no_still_cancels(monkeypatch, actions_recorder):
    patch_candidates(monkeypatch, [make_candidate(Category.STALE_DM, id=1)])
    result = runner.invoke(app, ["review", "--approve-all"], input="n\n")
    assert result.exit_code == 0
    assert actions_recorder == []
    assert "Cancelled" in result.output


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


# --- scan: numbering and last-scan persistence -----------------------------------


def test_scan_saves_last_scan_json(monkeypatch, tmp_path):
    monkeypatch.setattr(cleaner, "LAST_SCAN_PATH", tmp_path / "last-scan.json")
    patch_candidates(monkeypatch, [make_candidate(Category.GHOST, id=42)])
    runner.invoke(app, ["scan"])
    scan_map = cleaner.load_last_scan()
    assert scan_map == {1: 42}


def test_scan_numbers_are_sequential(monkeypatch, tmp_path):
    monkeypatch.setattr(cleaner, "LAST_SCAN_PATH", tmp_path / "last-scan.json")
    patch_candidates(monkeypatch, [make_candidate(Category.GHOST, id=i) for i in (10, 20, 30)])
    runner.invoke(app, ["scan"])
    assert cleaner.load_last_scan() == {1: 10, 2: 20, 3: 30}


def test_scan_output_contains_hash_column(monkeypatch, tmp_path):
    monkeypatch.setattr(cleaner, "LAST_SCAN_PATH", tmp_path / "last-scan.json")
    patch_candidates(monkeypatch, [make_candidate(Category.GHOST, id=1)])
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0
    assert "#" in result.output


def test_load_last_scan_returns_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cleaner, "LAST_SCAN_PATH", tmp_path / "no-such-file.json")
    assert cleaner.load_last_scan() == {}


def test_load_last_scan_returns_empty_on_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "last-scan.json"
    p.write_text("not json")
    monkeypatch.setattr(cleaner, "LAST_SCAN_PATH", p)
    assert cleaner.load_last_scan() == {}


# --- inspect: number and name lookup -----------------------------------------


def test_inspect_by_number_errors_without_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(cleaner, "LAST_SCAN_PATH", tmp_path / "no-scan.json")
    result = runner.invoke(app, ["inspect", "1"])
    assert result.exit_code == 1
    assert "Run 'scan' first" in result.output


def test_inspect_no_targets_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(cleaner, "LAST_SCAN_PATH", tmp_path / "scan.json")
    # Empty list is passed — our guard fires before network
    monkeypatch.setattr(cleaner, "gather_candidates", lambda _: [])
    result = runner.invoke(app, ["inspect", ""])
    # Either our guard (exit 1) or Typer missing-arg (exit 2) — both are non-zero
    assert result.exit_code != 0


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
