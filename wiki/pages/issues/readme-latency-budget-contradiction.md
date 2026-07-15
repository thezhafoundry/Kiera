---
title: README states a stale 5000ms fail-safe budget
type: issue
status: resolved
sources: [readme-md, latency-md]
updated: 2026-07-15
---

**Resolved 2026-07-07**: the fail-safe/budget concept itself is gone from the code, not just
the number. The 2026-07-02 streaming rebuild deleted the raw-voice-fallback path structurally
(see CLAUDE.md "Fail-CLOSED, never raw") — there is no timeout budget left to state correctly.
README.md's architecture section was rewritten to describe the current fail-closed
(silence-on-failure + standing playout buffer) design instead of picking a number for a
mechanism that no longer exists. Original write-up preserved below as historical record.

Found during the first wiki lint pass (bootstrap ingest, 2026-07-02): [[readme-md]]
states the RVC fail-safe timeout budget as **"5000ms"**; [[latency-md]] and the
`.agents/` notes all say **2000ms**.

**Checked against the actual code** (not just the two docs):
- [backend/main.py:190-195](../../../backend/main.py) constructs `RVCVoiceConverter`
  with `budget_ms=2000.0` explicitly.
- [backend/converters/rvc.py:51](../../../backend/converters/rvc.py) has a *class
  default* of `budget_ms: float = 5000.0` — but this default is overridden by the
  explicit `2000.0` passed at construction time.

**Conclusion**: the runtime behavior is 2000ms, matching LATENCY.md. README.md's
"5000ms" appears to describe the unused class default rather than the actual configured
value — it's stale documentation, not a second real budget. This directly affects
[[rvc-cold-start]] and [[audio-pipeline-latency-budget]] — anyone reading only the
README would under-estimate how aggressively the pipeline fails over to raw voice.

**Suggested fix**: update README.md §1 to say 2000ms, or better, stop hard-coding the
number in prose and point to `backend/main.py`'s `budget_ms` construction site so it
can't drift again.
