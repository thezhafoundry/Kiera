---
title: RTP packet loss on the LiveKit→Twilio SIP leg (Twilio edge not geo-pinned) — RESOLVED 2026-07-08
type: issue
status: resolved
sources: [decisions-log, subsystem-notes, active-backlog]
updated: 2026-07-08
---

On outbound demo calls the lead heard **clipped words** — short chunks of loud speech simply
missing. Distinct from [[part-by-part-audio-investigation]] (converter falling behind →
choppy/"part by part") and from the PSTN's inherent ~3.4kHz muffle: this is discrete audio
*dropouts*, not slur or lag.

## How it was found

The 3-point call capture (see [[subsystem-notes]] "Call-analysis 3-point capture") made it
measurable. On the 2026-07-08 10:16 UTC baseline call (68s):

- The Modal input/output debug WAVs were clean — no clipping, no underruns, the silence gate
  only removed genuine <−47dBFS silence.
- Twilio's own dual-channel recording had **5 dropouts of 0.18–0.40s of loud speech** (~1.4s
  total, ~2%).
- Windowed cross-correlation of the converted-output WAV against the Twilio recording showed
  the delay between them stayed **flat** (1.04→1.14s) across the whole call.

Flat delay is the discriminator: a **playout-buffer underrun inserts silence and steps the
delay up permanently**, so flat-delay-with-missing-audio means the audio was **lost in
transit**, not starved at the source. And because the loss is present in *Twilio's own*
recording, it happened **upstream of Twilio** — i.e. on the LiveKit→Twilio SIP hop, not the
lead's mobile network.

## Root cause

The Twilio trunk's termination domain `thezhafoundrykeira.pstn.twilio.com` does **not
geo-route** — it resolves to Twilio's US/Virginia edge (`54.172.60.x`). With LiveKit and
Render both in Singapore (since the 2026-07-03 migration — see
[[modal-render-region-mismatch]]), every call's RTP took a **transpacific UDP hop**, where
loss is expected. The plain `{trunk}.pstn.twilio.com` host is a US anycast, not a
latency-routed one.

## Fix (2026-07-08)

- LiveKit outbound trunk `ST_BMamqedncjzb` address updated **in place** (ID unchanged) to
  `thezhafoundrykeira.pstn.singapore.twilio.com` (AWS ap-southeast-1), colocated with
  LiveKit/Render.
- Local `.env` and Render env `TWILIO_SIP_URI` updated to match.
- Twilio origination URL (inbound direction) updated to
  `sip:3iv76uh14ps.sip.livekit.cloud;edge=singapore`.
- Render also got `PYTHONUNBUFFERED=1` so log timestamps are usable for this kind of forensics
  (prints had been block-buffered, flushing a whole call's lines under one timestamp).

Post-fix call: **1 dropout** (down from 5). Needs field-verification across more calls before
declaring it fully closed.

## Drift trap (open follow-up)

`POST /api/setup` recreates the Twilio origination URL **without** the `;edge=singapore`
parameter. The outbound edge survives a setup re-run (it reads `TWILIO_SIP_URI`); the
**inbound** pin does not, and would silently revert. `main.py` should be patched to append the
edge param when building the origination URL — tracked in [[active-backlog]]. This is the same
class of recurring infra-drift the project has hit before (see [[modal-render-region-mismatch]]).
