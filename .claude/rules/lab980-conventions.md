# lab980 conventions

Applies to every site served from the lab980 droplet. Platform-wide detail —
droplet layout, nginx/TLS/DNS, the `bin/` fixers — lives in the
`ivjames/lab980.com` repo's `CLAUDE.md`; this file is the part that has to be
true in *this* repo, so it is here rather than one clone away.

## How changes land

1. Work on a branch, never directly on the branch that deploys.
2. **Open a pull request.** The PR is the review record, and skipping it to
   save a round trip loses it. If your harness defaults to "don't open a PR
   unless asked", this file is the standing ask: open one.
3. **Codex reviews it, not a person.** Nobody is waiting to look at your PR, so
   a PR left open "pending review" is a PR that will sit there forever. The
   loop is: open it, let the bot review, address what it finds, merge it
   yourself. `boxoffice` has said this in its own `CLAUDE.md` for a while —
   it's true everywhere here.

   **When it doesn't review, you review it.** Three ways it is out. It
   *declines*: the connector posts a plain comment where a review should be,
   naming something to go and set up — "create a Codex account and connect to
   github" in one repo, "create an environment for this repo" in another. The
   wording tracks the cause, so recognise the shape rather than the string. It
   *doesn't fire*: no review, no comment, nothing to tell that PR apart from
   one still in flight. Or it was never enabled on the repo at all, which
   surfaces as the first of these if it surfaces. In all three the PR is not
   reviewed and is not going to become reviewed, so waiting is the one
   response that cannot work — and a PR left sitting is the failure this rule
   opens by warning about.

   Decide it on a clock. A refusal is fast and does not need to be asked for:
   seconds after the PR opens, or seconds after an ask. Silence needs a bound
   — two poll cycles from opening (per 7, about ten minutes, against a review
   that arrives in about four when it arrives at all), then treat the review
   as not coming. One `@codex review` at that point is allowed and is not the
   re-request 5 warns against: there is no round to restart, and it is what
   turns silence into an answer. Silence in reply to it is the same answer,
   slower.

   Then **review the PR yourself, adversarially, against the diff**, and hold
   what you find to exactly the standard 4 sets for the bot's findings: verify
   each against the code, fix what is real, say why the rest isn't. 5's
   one-round limit does not bind your own pass — it exists because a metered
   review re-reads the whole diff and restarts the loop, and neither is true
   of yours — so re-read after your own fixes as often as it is useful. Then
   merge, and **say on the PR that the bot did not review it, that you did,
   and what you checked**, so a PR that merged without its intended review
   carries that in its own record instead of leaving the next reader to infer
   it from an absence.

   Two things this does not add work to: a PR the bot did review is reviewed,
   and a sync of this file to canonical was reviewed in the lab980 PR that
   authored it (per 5), so it needs neither a pass nor a note.

   Evidence, cited as what two repos did rather than as a rule. In `slime` on
   2026-09-12: PR 73 was reviewed four minutes after it opened and PR 72 —
   same author, same day, nothing to tell them apart — never was; PRs 74, 76,
   77 and 78 got no review at all, 74's explicit ask was refused six seconds
   later, and 76 and 77 sat open for a day with a poll on them learning
   nothing each time it fired. In `lab980.com` on 2026-09-14, the PR that
   added this rule was refused **ten seconds after it opened**, unasked, and
   for a different stated cause than slime's — which is why nothing here
   generalises about which PRs get refused, only about what to do once one
   is. That a self-review is not a formality standing in for a real one is
   evidenced from the same work: it found a harness flag that validated the
   *shape* of its argument and not its *range*, so an out-of-range value would
   have exercised none of the code the flag existed to cover while every
   digest matched and the run exited 0 — and, on a second pass, a colour set
   as `--dim` under an opacity, compositing to 3.44:1 where that sheet's own
   `:root` comment says `--dim` was raised to clear the 4.5:1 AA floor.
4. **Verify each finding before you fix it.** The bot is usually right and
   occasionally not, and a fix pushed on its say-so that changes correct code
   is worse than the bug it imagined. Read the actual script or file it names
   first. When it *is* right, push the fix and resolve the thread; when it
   isn't, say why on the thread rather than silently ignoring it.
5. **One review round is the default — don't ask for another after fixing.**
   Codex reviews when a PR opens, when a draft is marked ready, and when it's
   asked (`@codex review`), *not* on every push. But it re-reads the whole diff
   rather than what changed since, so asking again after pushing the fixes it
   requested restarts the loop on code it has already passed: fresh findings on
   old lines, another round of fixes, a PR that stops converging, and a metered
   review spent each time round. The fix you pushed is the end of the round —
   verify it yourself, resolve the thread, merge.
   Ask for a second review only when the PR has picked up work the first one
   never saw: commits that add or change behaviour on their own account, not
   the ones answering its findings. A PR whose reviewed commit is ten commits
   of new work behind its head has effectively not been reviewed; a PR whose
   only unreviewed commits are its own review fixes has. If you can't name what
   is unreviewed, that is the answer — don't ask. Nor does a sync of this file
   to canonical need asking: its content is reviewed in the lab980 PR that
   authors it, and asking again in every site repo reviews the same bytes N
   times to learn nothing.
6. **Which branch it targets is this repo's business** — its own `CLAUDE.md`
   says, and that answer wins over this file. Where it says nothing: target
   the branch this repo actually deploys from, which is its **default branch**
   — read it, don't assume it. `main` is the common case but not a safe
   default: of the repos this file is fanned out to, two are on `master` and
   one has no `main` at all, its only branch being a `claude/…` one. A PR
   opened against a branch that doesn't exist is a PR nobody merges. Sites
   here also differ on purpose; `boxoffice` ships beta-first, so feature PRs
   go to `staging` and reach `main` by promotion.
7. **Watch the PR on a five-minute poll**, not the hourly one a harness will
   default to. GitHub's events — CI, review comments, conflict notices — do the
   real work and nearly all arrive within about four minutes of a push; the
   poll exists only to catch the case events can't express, which is that
   nothing happened at all. An hourly poll on a four-minute loop leaves a
   stalled PR untouched for fifty-six minutes out of sixty. Stop polling when
   the PR merges — which, per 3, is something you do, not something you wait
   for.
8. **Merging does not put anything live.** Deploying is a separate command run
   on the droplet (or, where a site auto-deploys a branch, a push that has
   actually completed), and a session working from a laptop or the cloud
   usually cannot reach the droplet at all. Say plainly that a change is merged
   but not yet deployed rather than implying it shipped.

## Deploying

Each site ships its own operate CLI at `bin/<stub>`, symlinked to
`/usr/local/bin/<stub>` on the droplet. `<stub> deploy` and `<stub> --help` are
the only two every site has — the rest of the command set differs, and
`boxoffice` for instance has no `status` at all. Ask `--help` rather than
assuming; this repo's `DEPLOY.md` is the full runbook.

- **Never edit a tracked file on the droplet.** Deploying re-syncs the
  checkout from git, and what that does to a hand-edit depends on the site:
  most hard-reset, so the edit is destroyed silently; at least one
  fast-forwards, so a non-conflicting edit survives into production and a
  conflicting one aborts the deploy. Both are bad, differently, and neither is
  something to rely on — for anything in git, the repo is the only place to fix
  it. Which one this site does is in its own `DEPLOY.md`, or failing that in
  its `bin/<stub>`.
- **On an app site, the untracked state is a different matter and is meant to
  be edited on the box.** `.env` and `data/` are gitignored precisely so they
  survive a deploy: the keys go into `.env` on the droplet by hand, and that is
  the supported path, not a workaround. Never move a secret into the repo to
  avoid editing there.
- **A static site has no such state, and must not acquire any.** Where the
  checkout *is* the web root, anything left in it is something the internet
  can fetch, and a `data/` directory created on the box would be served. Do
  not assume that shape, and do not assume what the vhost denies — both vary.
  `bonita` roots its vhost at `site/` inside the checkout and `bw` at a build
  release under `current/`, so most of those two checkouts is not reachable at
  all. The `new-site` scaffold's static vhost denies dotfiles and `*.md`, but
  `highlander` denies `/deploy/` as well, and as of 2026-09-07 five of the
  sites this file fans out to deny no `*.md` at all: `forest`, `haunted`,
  `resume`, `ffc` and `mbw` each serve their own README over HTTP right now.
  What a site denies is a fact about its *installed* vhost, which is
  frequently not the copy in its repo — most of these sites' dotfile rule was
  inserted on the droplet by `fix-dotgit` and never written back. Check the
  live host rather than the repo. Static sites hold no secrets, in git or
  beside it.
- **Check what is actually live before saying a deploy happened.** A 200 only
  proves the endpoint answered, not which build it served — ask for the
  deployed commit, not just the status code.
- `health-check --site <stub>` (from the lab980 repo's `bin/`) probes DNS, the
  upstream port where there is one, the public URL and cert expiry.

## About this file

It is generated by the `new-site` skill in `ivjames/lab980.com` and is
**byte-identical in every site repo**, so the next sweep overwrites whatever is
here. Two consequences worth knowing: an edit made in this repo will be lost,
and anything true of only this site belongs in this repo's own `CLAUDE.md`
instead. To change a convention, change it in lab980 and let it fan out.

The test for whether a line belongs here: you have to be able to state it as
true of **every repo this file is fanned out to**, having checked rather than
assumed. That set is exactly what `sites.py --attach` lists — registry entries
with a recorded repo, minus the conventions repo itself, which holds the
canonical copy rather than a copy of it. It is deliberately not "every entry in
the registry": `lab980` deploys with `update.sh` rather than a `bin/<stub>`
CLI, and four entries have no repo recorded yet, so a test written that way
would be unverifiable and would quietly license checking the same handful while
believing otherwise. Five claims have already failed that test — a hard reset
that one site doesn't do, a `status` command one site doesn't have, a deploy
ref that isn't always `main`, a *default branch* that isn't always `main`
either, and a vhost deny rule that five sites did not have while this file
said flatly that they did — and each was wrong in the direction that stops the
reader looking. The last one cost the most: because the file asserted the deny
as settled fact, `lab980.com` served its own 51KB `CLAUDE.md` and a 77KB
droplet security audit to the internet for as long as anyone had been reading
this paragraph and believing it. A file this widely copied earns its keep only
by being narrower than it is tempting to make it.
