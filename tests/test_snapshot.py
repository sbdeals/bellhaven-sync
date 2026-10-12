"""Snapshot / replay mode: SnapshotCRM over captured JSON with a persisted write state,
fixture_fetcher over captured HTML, and the CLI `--snapshot` wiring."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from datetime import date
from pathlib import Path

import pytest

from bellhaven_sync.config import DEFAULT_PARENT_NAME, Settings
from bellhaven_sync.crm import CRMError
from bellhaven_sync.models import NEW_ACCOUNT_REF, Proposal
from bellhaven_sync.pipeline import run
from bellhaven_sync.snapshot import SnapshotCRM, default_state_path, fixture_fetcher
from bellhaven_sync.store import Store
from tests.conftest import BELLHAVEN_PARENT_ID, FIXTURES, HARBORVIEW_PARENT_ID

CRM_DIR = FIXTURES / "crm"
SITE_DIR = FIXTURES / "site"
TODAY = date(2026, 10, 11)


def _digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.json"))}


@pytest.fixture(autouse=True)
def fixtures_untouched():
    before = _digest(CRM_DIR)
    yield
    assert _digest(CRM_DIR) == before, "snapshot mode must never write into the snapshot directory"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "snapshot_state.json"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://example.invalid", token="", db_path=tmp_path / "snapshot.db",
        parent_account_name=DEFAULT_PARENT_NAME, parent_account_id="",
    )


def _apply(crm, p: Proposal) -> None:
    new_id = None
    for a in p.actions:
        fields = {k: (new_id if v == NEW_ACCOUNT_REF else v) for k, v in a.fields.items()}
        if a.op == "create":
            new_id = crm.create_account(fields).account_id
        else:
            crm.update_account(a.account_id, fields)


# ------------------------------------------------------------------ SnapshotCRM


def test_loads_snapshot(state) -> None:
    crm = SnapshotCRM(CRM_DIR, state_path=state)
    accounts = crm.list_accounts()
    assert len(accounts) == 121 and len(crm.list_contacts()) == 67
    lima = crm.get_account("001LGFPBJY4N9MB6KL")
    assert lima.parent_name == "Harborview Care Group (Parent Account)"
    assert [c.name for c in crm.list_contacts("001EGU7BMJ942ZTRE6")] == ["Gloria Lambert"]
    assert not state.exists()  # reads never create the state file


def test_writes_persist_across_instances(state) -> None:
    crm = SnapshotCRM(CRM_DIR, state_path=state)
    resp = crm.update_account("00116ETS45BL7DTQP7", {"status": "Needs Review", "note": "x"})
    assert resp["account_id"] == "00116ETS45BL7DTQP7" and resp["fields"] == ["note", "status"]
    new = crm.create_account({"name": "Bellhaven of Batavia", "parent_id": BELLHAVEN_PARENT_ID, "billing_zip": "45103"})
    assert new.account_id == "001SNAP000001" and new.created_by_candidate
    assert state.exists()

    again = SnapshotCRM(CRM_DIR, state_path=state)
    assert again.get_account("00116ETS45BL7DTQP7").status == "Needs Review"
    assert again.get_account("00116ETS45BL7DTQP7").note == "x"
    b = again.get_account("001SNAP000001")
    assert (b.name, b.parent_id, b.parent_name) == ("Bellhaven of Batavia", BELLHAVEN_PARENT_ID, DEFAULT_PARENT_NAME)
    assert len(again.list_accounts()) == 122
    assert again.create_account({"name": "Another"}).account_id == "001SNAP000002"
    assert [w["op"] for w in json.loads(state.read_text(encoding="utf-8"))["writes"]] == ["patch", "create", "create"]


def test_without_state_path_writes_stay_in_memory(tmp_path) -> None:
    crm = SnapshotCRM(CRM_DIR)
    crm.update_account("00116ETS45BL7DTQP7", {"status": "Inactive"})
    assert crm.get_account("00116ETS45BL7DTQP7").status == "Inactive"
    assert SnapshotCRM(CRM_DIR).get_account("00116ETS45BL7DTQP7").status == "Active"


@pytest.mark.parametrize(
    ("call", "status_code"),
    [
        (lambda c: c.update_account("00116ETS45BL7DTQP7", {"status": "Closed"}), 422),
        (lambda c: c.update_account("00116ETS45BL7DTQP7", {"parent_id": "001NOPE"}), 422),
        (lambda c: c.update_account("00116ETS45BL7DTQP7", {"lifetime_revenue": 5}), 422),  # no mutable field
        (lambda c: c.update_account("001NOPE", {"status": "Inactive"}), 404),
        (lambda c: c.get_account("001NOPE"), 404),
        (lambda c: c.create_account({"parent_id": BELLHAVEN_PARENT_ID}), 422),  # name required
        (lambda c: c.create_account({"name": "X", "status": "Open"}), 422),
        (lambda c: c.create_account({"name": "X", "parent_id": "001NOPE"}), 422),
    ],
)
def test_validation_matches_sandbox(state, call, status_code) -> None:
    crm = SnapshotCRM(CRM_DIR, state_path=state)
    with pytest.raises(CRMError) as exc:
        call(crm)
    assert exc.value.status_code == status_code
    assert not state.exists()  # rejected writes are not persisted


def test_non_mutable_fields_are_ignored(state) -> None:
    crm = SnapshotCRM(CRM_DIR, state_path=state)
    resp = crm.update_account("00116ETS45BL7DTQP7", {"status": "Inactive", "lifetime_revenue": 999, "account_id": "zzz"})
    assert resp["fields"] == ["status"]
    a = SnapshotCRM(CRM_DIR, state_path=state).get_account("00116ETS45BL7DTQP7")
    assert a.lifetime_revenue == 0 and a.status == "Inactive"


def test_missing_snapshot_files_raise(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        SnapshotCRM(tmp_path)


def test_default_state_path() -> None:
    assert default_state_path(Path("data") / "snapshot.db") == Path("data") / "snapshot_state.json"


# --------------------------------------------------------------- fixture_fetcher


def test_fixture_fetcher_paths() -> None:
    fetch = fixture_fetcher(SITE_DIR)
    p3 = (SITE_DIR / "communities_p3.html").read_text(encoding="utf-8")
    assert fetch("/") == (SITE_DIR / "index.html").read_text(encoding="utf-8")
    assert fetch("/about") == (SITE_DIR / "about.html").read_text(encoding="utf-8")
    assert fetch("/communities") == (SITE_DIR / "communities_p1.html").read_text(encoding="utf-8")
    assert fetch("/communities?page=2") == (SITE_DIR / "communities_p2.html").read_text(encoding="utf-8")
    assert fetch("/communities?page=3") == p3 and fetch("/communities?page=9") == p3  # out of range -> last
    assert "Tiffin" in fetch("/communities/bellhaven-of-tiffin")
    with pytest.raises(LookupError):
        fetch("/communities/no-such-place")
    with pytest.raises(LookupError):
        fetch("/careers")


# ----------------------------------------------------------------------- pipeline


def test_pipeline_over_snapshot_matches_fake_crm(settings, state, fake_crm) -> None:
    pytest.importorskip("bellhaven_sync.scraper")
    fetch = fixture_fetcher(SITE_DIR)
    snap = run(settings, crm=SnapshotCRM(CRM_DIR, state_path=state), store=Store(), fetch=fetch, today=TODAY)
    fake = run(settings, crm=fake_crm, store=Store(), fetch=fetch, today=TODAY)
    assert len(snap.proposals) == 46
    assert {p.fingerprint for p in snap.proposals} == {p.fingerprint for p in fake.proposals}
    assert not state.exists()


def test_approvals_survive_restart_and_converge(settings, state) -> None:
    pytest.importorskip("bellhaven_sync.scraper")
    fetch = fixture_fetcher(SITE_DIR)
    store = Store()
    first = run(settings, crm=SnapshotCRM(CRM_DIR, state_path=state), store=store, fetch=fetch, today=TODAY)
    crm = SnapshotCRM(CRM_DIR, state_path=state)
    for p in first.proposals:
        _apply(crm, p)
    restarted = SnapshotCRM(CRM_DIR, state_path=state)  # e.g. the review app restarted
    assert restarted.get_account(HARBORVIEW_PARENT_ID).status == "Inactive"
    assert restarted.get_account("001SXSF4ELF0Z2LGDM").chow_current_account == "0017JP8Z1UQ763BVK3"
    second = run(settings, crm=restarted, store=store, fetch=fetch, today=TODAY)
    assert second.by_kind == {"confirmed": 35}


# ---------------------------------------------------------------------------- CLI


def test_cli_run_snapshot(tmp_path, capsys) -> None:
    pytest.importorskip("bellhaven_sync.scraper")
    from bellhaven_sync import cli

    db = tmp_path / "x.db"
    assert cli.main(["run", "--snapshot", str(CRM_DIR), "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "SNAPSHOT MODE" in out and "46 proposals" in out and "new=46" in out
    assert Store(db).counts() == {"pending": 46}
    assert not (tmp_path / "snapshot_state.json").exists()  # run never writes to the (snapshot) CRM


def test_cli_snapshot_defaults_db(monkeypatch, tmp_path) -> None:
    from bellhaven_sync import cli

    base = Settings(base_url="https://example.invalid", token="", db_path=tmp_path / "data" / "bellhaven_sync.db")
    monkeypatch.setattr(cli, "get_settings", lambda: base)
    args = cli.build_parser().parse_args(["run", "--snapshot", str(CRM_DIR)])
    assert cli._settings(args).db_path == tmp_path / "data" / "snapshot.db"
    args = cli.build_parser().parse_args(["--db", str(tmp_path / "a.db"), "run", "--snapshot", str(CRM_DIR)])
    assert cli._settings(args).db_path == tmp_path / "a.db"
    args = cli.build_parser().parse_args(["run"])
    assert cli._settings(args).db_path == tmp_path / "data" / "bellhaven_sync.db"


def test_cli_review_snapshot_passes_snapshot_crm(monkeypatch, tmp_path, capsys) -> None:
    from bellhaven_sync import cli

    seen = {}
    fake_app_module = types.ModuleType("bellhaven_sync.app.main")

    def create_app(settings, store, crm):
        seen.update(settings=settings, store=store, crm=crm)
        return "APP"

    fake_app_module.create_app = create_app
    monkeypatch.setitem(sys.modules, "bellhaven_sync.app.main", fake_app_module)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(app=app, host=host, port=port))
    db = tmp_path / "s.db"
    assert cli.main(["review", "--snapshot", str(CRM_DIR), "--db", str(db)]) == 0
    assert "SNAPSHOT MODE" in capsys.readouterr().out
    assert seen["app"] == "APP" and isinstance(seen["crm"], SnapshotCRM)
    assert seen["crm"].state_path == tmp_path / "snapshot_state.json"
    assert seen["settings"].db_path == db


def test_real_apply_against_snapshot_crm(settings, state) -> None:
    """The review app's apply path (apply.apply_proposal) works unchanged on SnapshotCRM."""
    apply = pytest.importorskip("bellhaven_sync.apply")
    pytest.importorskip("bellhaven_sync.scraper")
    report = run(settings, crm=SnapshotCRM(CRM_DIR, state_path=state), store=Store(),
                 fetch=fixture_fetcher(SITE_DIR), today=TODAY)
    tiffin = next(p for p in report.proposals if p.subject_account_id == "001U6RW32TY0WSXZZB")
    result = apply.apply_proposal(tiffin, SnapshotCRM(CRM_DIR, state_path=state))
    assert result.ok and result.created_account_id == "001SNAP000001"
    after = SnapshotCRM(CRM_DIR, state_path=state)  # restart
    old = after.get_account("001U6RW32TY0WSXZZB")
    assert old.chow_current_account == "001SNAP000001" and old.parent_id != BELLHAVEN_PARENT_ID
    assert after.get_account("001SNAP000001").parent_id == BELLHAVEN_PARENT_ID
    bad = apply.apply_proposal(tiffin.__class__.from_dict({**tiffin.to_dict(), "actions": [
        {"op": "patch", "account_id": "001U6RW32TY0WSXZZB", "fields": {"status": "Closed"}}]}), after)
    assert not bad.ok and "422" in (bad.error or "")
