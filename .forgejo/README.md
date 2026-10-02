# Upstream Sync Pipeline

This directory holds the machinery that keeps this fork rebased on top of the
upstream `Tasshack/dreame-vacuum` project. The Forgejo Actions workflow runs
**daily** (and can be triggered manually) to pull the latest upstream, re-apply
this fork's deactivation patches, verify the result, and push to `master`.

## Files

| File | Purpose |
|------|---------|
| `workflows/sync.yml` | The scheduled Forgejo Actions workflow |
| `sync-patch.py` | Pure-Python patch script that applies the fork's changes on top of upstream |

## How it works

The workflow performs, in order:

1. **Checkout** the fork's `master` (full history).
2. **Fetch** upstream `master` and this `fork-sync-pipeline` branch.
3. **Reset** `master` onto `upstream/master` (`git reset --hard upstream/master`),
   discarding stale fork history.
4. **Cherry-pick** the `fork-sync-pipeline` commit onto upstream. This is what
   brings the `.forgejo` files (this workflow + patch script) back into the tree.
5. **Run** `python3 .forgejo/sync-patch.py` to apply the deactivation patches.
6. **Verify** the patched files have no syntax errors (`compileall`), then clean
   up the `__pycache__` it creates.
7. **Commit + push** `master` to origin with `--force-with-lease`, unless the
   resulting tree is already identical to `origin/master`.

Because the sync rebuilds `master` from scratch each run, the fork's history is
always a clean linear stack of *upstream + our pipeline + our patches* — no
rebase conflicts to resolve by hand.

## The patches

`sync-patch.py` applies these changes on top of a fresh upstream checkout:

1. **Mandatory:** delete the obfuscated proprietary license frontend
   (`custom_components/dreame_vacuum/frontend.py`).
2. Strip the obfuscated JS bundle + banner image constants (`FRONTEND`, `DVC`)
   from `const.py`.
3. Drop the now-undefined `DVC` import from `config_flow.py` / `coordinator.py`.
4. Switch `hacs.json` to "install from repo" (no `zip_release`).
5. Strip the DVC paywall marketing / notification banners.

Only step 1 (frontend deletion) is mandatory. Every other step is **best-effort**
and "allowed to fail": if a pattern isn't found because upstream changed the code,
the step logs a warning and continues rather than failing the whole sync.

Each strip also has a **sanity guard** (`MAX_STRIP_LINES = 25`): if a matched
region would span more than 25 lines, that individual patch is aborted and left
unapplied, rather than risk deleting a large chunk of unrelated code.

## The single-commit requirement

The `fork-sync-pipeline` branch must remain a **single commit** on top of
`b6455fd`. This is critical because the workflow does `git cherry-pick
fork-sync-pipeline`, and a multi-commit branch does **not** cherry-pick cleanly —
applying a range of `.forgejo` commits leaves the files in a deleted/untracked
conflict state.

So whenever the pipeline changes (editing `sync.yml` or `sync-patch.py`), the
branch is **flattened** back to one commit before pushing:

```bash
git checkout fork-sync-pipeline
git reset --soft b6455fd     # b6455fd is the branch base
git add .forgejo
git commit -m "ci: apply patches after upstream sync"
git push origin fork-sync-pipeline --force-with-lease
```

The commit message stays `ci: apply patches after upstream sync` (the workflow
uses this branch purely as a cherry-pick target, so the message is cosmetic).

## How to update the pipeline

1. Make your edits to `workflows/sync.yml` and/or `sync-patch.py`.
2. Test the patch script locally against upstream before committing:
   ```bash
   git show upstream/master:custom_components/dreame_vacuum/frontend.py > /tmp/frontend.py
   # ... set up a scratch tree with upstream's const/config_flow/coordinator/hacs ...
   python3 .forgejo/sync-patch.py   # verify output is correct
   ```
3. Flatten to a single commit and push (see above).
4. Also mirror the changed files onto `master` so the **live** workflow (which
   Forgejo actually executes from `master`) has the latest version:
   ```bash
   git checkout master
   git checkout fork-sync-pipeline -- .forgejo
   git add .forgejo
   git commit -m "ci: ..."
   git push origin master
   ```

> Note: `master` and `fork-sync-pipeline` must keep identical `.forgejo`
> contents. The pipeline branch is the canonical source; master is what the
> runner executes, and the next sync re-cherry-picks the pipeline branch over
> whatever master had.

## Requirements / assumptions

- The Forgejo runner must have a **runner enabled for scheduled workflows**.
- Push to origin uses the built-in `secrets.GITHUB_TOKEN` (needs `contents:
  write` permission, which the workflow declares).
- Upstream is fetched over public HTTPS — no auth needed.
- `python3` is required on the runner (for the patch script and the syntax check).
