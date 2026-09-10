# lwcc — working conventions

## Git workflow (required)

**Never commit directly to `main`.** All work happens on a feature branch:

1. Develop and commit on your designated `claude/...` branch (create it from
   the latest `origin/main` if it doesn't exist).
2. Run both test suites before any push: `python3 test/run.py` and
   `python3 test/test_app.py` — green is a precondition, not a goal.
3. Push the branch, then merge it into `main` with a real merge commit
   (`git merge --no-ff`). `main` only ever receives merge commits — never
   direct commits, never fast-forwards — so branch provenance stays visible
   in its history. Rebase your branch onto `origin/main` first when it has
   moved.
4. Multiple sessions work this repo concurrently. Always `git fetch` and
   check `origin/main` before pushing; if it moved, rebase on top of it and
   re-run the tests — never force-push `main`, and use `--force-with-lease`
   only on your own feature branch after a rebase.

## Orientation

- `app.py` — the lwcc.lab980.com site + admin + upload/convert app (stdlib
  only). Deployed on the droplet at `/var/www/lwcc` under pm2; `lwcc deploy`
  there fast-forwards to `origin/main` and restarts (`bin/lwcc`; `redeploy`
  is an alias). Merging is not deploying.
- `lwccauth.py` — accounts, one-time invite/reset links and server-side
  sessions, all in `users.json` beside the app (0600, gitignored, never
  committed). Accounts exist only by redeeming an invite link, so no password
  passes through whoever issued it. Two roles: staff upload/review/edit,
  admins also get the maintenance tools and `/admin/users` — enforced in the
  rendered HTML *and* server-side (`ADMIN_ONLY_ACTIONS`).
- The AI article scanner is dormant: no routes, no UI, no worker threads.
  `wgconvert/aiscan.py` and the `aiscan_*` helpers in `app.py` are kept
  intact and still tested in-process, so re-enabling it means restoring the
  routes and the worker startup, not rewriting it.
- `wgconvert/` — the PDF → guide.json → HTML converter package. Parser
  changes must keep the checked-in samples converting clean; when a new
  format variant is taught, pin it in `test/run.py`.
- Published Sundays live on the droplet in `public/<YYYY-MM-DD>/` (untracked);
  the repo only tracks the app, converter, and the `2026-08-02` reference.
- The printed PDF is the source of truth: the converter preserves its text
  faithfully (typos included) and never silently drops content — losses are
  warnings, loose classifications are notes.
