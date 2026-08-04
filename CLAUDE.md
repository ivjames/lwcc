# lwcc — working conventions

## Git workflow (required)

**Never commit directly to `main`.** All work happens on a feature branch:

1. Develop and commit on your designated `claude/...` branch (create it from
   the latest `origin/main` if it doesn't exist).
2. Run both test suites before any push: `python3 test/run.py` and
   `python3 test/test_app.py` — green is a precondition, not a goal.
3. Push the branch, then update `main` from it (fast-forward merge preferred;
   rebase your branch onto `origin/main` first when it has moved).
4. Multiple sessions work this repo concurrently. Always `git fetch` and
   check `origin/main` before pushing; if it moved, rebase on top of it and
   re-run the tests — never force-push `main`, and use `--force-with-lease`
   only on your own feature branch after a rebase.

## Orientation

- `app.py` — the lwcc.lab980.com site + admin + upload/convert app (stdlib
  only). Deployed on the droplet at `/var/www/lwcc` under pm2; `lwcc redeploy`
  there pulls `main` and restarts.
- `wgconvert/` — the PDF → guide.json → HTML converter package. Parser
  changes must keep the checked-in samples converting clean; when a new
  format variant is taught, pin it in `test/run.py`.
- Published Sundays live on the droplet in `public/<YYYY-MM-DD>/` (untracked);
  the repo only tracks the app, converter, and the `2026-08-02` reference.
- The printed PDF is the source of truth: the converter preserves its text
  faithfully (typos included) and never silently drops content — losses are
  warnings, loose classifications are notes.
