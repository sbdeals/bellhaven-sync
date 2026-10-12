# bellhaven-sync — conventions for anyone (human or agent) working here

- Python 3.12, managed by **uv**. Run everything with `uv run ...` (e.g. `uv run pytest -q`).
  Never `pip install`; add deps with `uv add`.
- Source lives in `src/bellhaven_sync/`; tests in `tests/`. Fixtures in `tests/fixtures/` are
  REAL captures of the website and CRM taken 2026-10-11. Do not edit them.
- Read `docs/superpowers/specs/2026-10-11-bellhaven-sync-design.md` before touching matching
  or proposal logic. The table of known data situations is the acceptance checklist.
- Shared contract files (`models.py`, `store.py`, `crm.py`, `config.py`, `tests/fakes.py`) are
  owned by the orchestrator. If you need a change there, make the smallest one and say so
  in your report.
- The CRM is a live sandbox that is graded on end state. **Only `apply.py` may write to it,
  and only the review app may call `apply.py`.** Tests must never hit the live API unless
  marked `@pytest.mark.live`.
- Keep functions pure where possible: scraper and matcher take data in, return data out.
- Style: type hints, dataclasses, no global state, stdlib `logging`. No LLM calls in the pipeline.
