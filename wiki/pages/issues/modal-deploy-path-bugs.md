---
title: Modal deploy failed twice on path/mounting bugs (first real deploy of the streaming rebuild)
type: issue
status: resolved
sources: [decisions-log, subsystem-notes]
updated: 2026-07-03
---

The 2026-07-02 streaming rebuild ([[audio-pipeline-latency-budget]], `.agents/decisions/log.md`'s
2026-07-02 entry) was merged to `main` but never actually deployed to Modal until 2026-07-03 —
the first real `modal deploy modal_deploy/worker.py` attempt failed twice in a row, on two
unrelated bugs, before the worker came up.

## Bug 1: hardcoded old folder name

`modal_deploy/worker.py`, `modal_deploy/app.py`, `modal_deploy/local_server.py`, and
`.gitignore` all still referenced the vendored RVC WebUI folder by its old name,
`Retrieval-based-Voice-Conversion-WebUI/` — but the folder had since been renamed to `RVC/`
(matching what `CLAUDE.md`'s own Tech Stack section already said). `modal deploy` failed
immediately with `local dir ... does not exist`, on **any** machine, not just Render (Render's
checkout never had the folder at all, since it's gitignored and too large for GitHub; a local
machine with the actual folder present would have hit the identical error, since the code was
looking for the wrong name).

**Fix**: updated all four references to `RVC`. See [[decisions-log]] commit `d82f22c`.

## Bug 2: sibling module not bundled into the container

Deploy succeeded after the rename fix, but the container **crash-looped**:
`ModuleNotFoundError: No module named 'streaming'`. `worker.py` imports the sibling
`modal_deploy/streaming.py` module at the top (`from modal_deploy import streaming as st`,
falling back to `import streaming as st`) — and that local import succeeding during
`modal deploy` itself said nothing about whether Modal actually bundled the file into the
remote container. It hadn't; the image definition only had `.add_local_dir("RVC", ...)`,
with no explicit instruction to also ship `streaming.py`.

**Fix**: added `.add_local_python_source("streaming")` to the `Image` chain in `worker.py`.
Confirmed by the resulting deploy's mount list showing `Created mount PythonPackage:streaming`,
and by `/health` returning `{"status":"ready","cuda_available":true,"cuda_device":"Tesla T4",...}`
afterward. See [[decisions-log]] commit `904757f`.

## Lesson

Both bugs are variants of the same trap: **a local import/path succeeding while running
`modal deploy` proves nothing about what ships in the remote container.** Modal does not
auto-bundle local source files just because your entrypoint script can `import` them — every
local file/module a Modal function needs (beyond the entrypoint file itself) must be explicitly
declared via `add_local_dir` / `add_local_file` / `add_local_python_source`. This had been
flagged as an *unverified* risk in the rebuild's own progress ledger before the first real
deploy ("Modal auto-mounting of `modal_deploy/streaming.py` unverified locally... may need
`add_local_python_source`") — the risk was real. If `worker.py` ever grows another sibling
module, mount it explicitly the same way rather than assuming it'll "just work" because the
local `modal deploy` invocation didn't error.

Also incidentally surfaced: `.gitignore` never matched the *old* folder name correctly either
in a way that would've caught this — and separately, because it also never matched the *new*
name until this fix, ~195 files under `RVC/` had been accidentally `git add`-ed and committed
at some point before the gitignore was corrected. Changing `.gitignore` doesn't retroactively
untrack already-committed files — see [[active-backlog]] for the cleanup this still needs.
