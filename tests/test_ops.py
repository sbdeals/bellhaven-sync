"""Sanity checks for the scheduling files (stdlib only; pyyaml is not a dependency)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "daily.yml"
RUN_DAILY = ROOT / "ops" / "run-daily.sh"
CRONTAB = ROOT / "ops" / "crontab"


def _code_lines(text: str) -> list[str]:
    """Non-comment, non-blank lines (comments may mention commands we must not run)."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_workflow_schedule_and_manual_trigger() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "0 11 * * *"' in text
    assert "workflow_dispatch" in text


def test_workflow_runs_the_read_only_pipeline_with_secret_token() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(_code_lines(text))
    assert "uv sync --frozen" in code
    assert "bellhaven-sync run" in code
    assert "BELLHAVEN_CRM_TOKEN: ${{ secrets.BELLHAVEN_CRM_TOKEN }}" in code
    # the scheduled job must never start the review app (the only path that writes)
    assert "bellhaven-sync review" not in code
    assert "set -euo pipefail" in code  # a failing CLI must fail the step despite `tee`


def test_workflow_persists_state_db_between_runs() -> None:
    code = "\n".join(_code_lines(WORKFLOW.read_text(encoding="utf-8")))
    assert "actions/cache/restore@v4" in code
    assert "restore-keys" in code
    assert "actions/cache/save@v4" in code
    assert "actions/upload-artifact@v4" in code
    assert "retention-days: 30" in code
    assert "data/bellhaven_sync.db" in code
    assert "group: bellhaven-sync" in code
    assert "cancel-in-progress: false" in code
    assert "timeout-minutes: 15" in code


def test_run_daily_script() -> None:
    raw = RUN_DAILY.read_bytes()
    assert b"\r\n" not in raw, "CRLF would break the shebang on Linux"
    text = raw.decode("utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert "flock" in text
    assert "/etc/bellhaven-sync.env" in text
    assert "bellhaven-sync run" in text


def test_crontab_entry() -> None:
    text = CRONTAB.read_text(encoding="utf-8")
    active = [ln for ln in _code_lines(text) if ln.startswith("0 11 * * *")]
    assert len(active) == 1, "exactly one active daily entry"
    assert "run-daily.sh" in active[0]
    assert "cd /opt/bellhaven-sync && /usr/local/bin/uv run bellhaven-sync run" in text
