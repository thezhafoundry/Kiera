# Active Roadmap & Technical Debt

## Backlog
| Task | Priority | Status |
|---|---|---|
| Resolve Modal/Render region mismatch (Modal pinned `ap-southeast`, Render deployed in Oregon/us-west) — either re-pin Modal to a US region or move Render closer to Singapore | High | Open, identified 2026-07-02 (see [[log]], [[subsystem-notes]]) |
| Avoid Render `autoDeploy: commit` killing in-flight calls mid-test — consider deploy hooks/manual deploy during active test sessions, or a drain/graceful-shutdown path for `VoiceConversionWorker` | Medium | Open, identified 2026-07-02 |
| Compile RNNoise / get `webrtc-noise-gain` MSVC build working on Windows dev machines so local dev doesn't silently run in passthrough mode | Low | Open — degrades gracefully so not urgent, but masks real noise-suppression behavior during local testing (see [[subsystem-notes]]) |

## Known Tech Debt
- Buffering/pre-buffer logic has been reverted and re-implemented multiple times
  (`523e6d9` → `0a76fe1` → `2a20b3a` → revert → `fe678d6`) — the current adaptive
  standing-buffer design ([[subsystem-notes]]) is the latest iteration but the area has a
  history of regressions; treat playout timing changes as high-risk and re-run the
  spectral latency test (LATENCY.md §3) after any edit.
- No automated latency regression test — LATENCY.md's spectral tone test is manual
  (two browser tabs). A change could regress mouth-to-ear latency without any CI signal.
- `RVC/` vendored third-party WebUI checked into the repo tree (used for offline model
  training, not runtime) — large surface area unrelated to Keira's own code; worth
  confirming it's excluded from anything that scans/lints the whole repo.
