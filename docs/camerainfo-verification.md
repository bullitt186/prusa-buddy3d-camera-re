# CameraInfoMessage — Guess Elimination & Implementation Plan

## Context

Earlier sessions reverse-engineered the Prusa Buddy3D camera's cloud protocol well enough
to get snapshots, `/c/info`, and Socket.IO auth/status working (see `status.md`), but
"Watch Live" / WebRTC signaling never activates. The leading unresolved hypothesis is that
our `status` (`CameraInfoMessage`) payload is missing fields the real camera always sends —
we only send 4 of its ~13 always-present optional fields.

This session, GhidrAssistMCP (attached to an already-open Ghidra GUI on `lp_app`) let us
reconstruct the `CameraInfoMessage` C struct byte-exactly (464 bytes, matches the real
`memset(auStack_3e0, 0, 0x1d0)` call precisely) and confirm two of its protobuf field
numbers independently (5 = `network_info`, 11 = `video_quality`) via the nanopb
`submsg_info` pointer array. But most fields are still unidentified, and at least two
things currently in `signaling.py` are explicitly marked as guesses (`network_info`'s
internal field order; the exact field numbers used for `name`/`firmware`).

Goal of this plan: eliminate as many of those guesses as possible via more (cheap, fast,
GUI-attached) Ghidra queries *before* burning another real-hardware test cycle — each
round of "redeploy to the Pi and see if Watch Live appears" costs time and sometimes a
token/re-pairing cycle, so batch the investigation, then make one well-informed
implementation change and one clean empirical test.

**Execution note (2026-07-07):** this plan has now been executed. The key
result is that one of this plan's starting assumptions was wrong: top-level field 5 is
not `network_info`; descriptor + accessor tracing shows top-level field 4 carries
network data. Field 8 is `http.token`, not camera name, field 9 is system telemetry
instead of `available_resolutions`, and field 10 is a conditional request-id-like string,
not firmware. The Pi deployment was updated accordingly; see `status.md` for the exact
evidence and service verification.

**Tooling**: `mcp__ghidrassist__*` MCP tools, attached to the already-open Ghidra project
`lp_app` at `/tmp/ghidra_project/cam_analysis` (binary:
`oem_extracted/897730261/oem/usr/sbin/lp_app`, ARM:LE:32:v7). These are fast (seconds,
live GUI session) — prefer them over headless `analyzeHeadless` runs (which cost 2-3 min
each) for everything in this plan.

**Reference artifacts already produced this session** (do not redo):
- `../research/camera_info_struct.c` — the byte-exact struct definition
- `../research/compute_camerainfo_offsets.py` — generator script for it
- Ghidra structure `/auto_structs/CameraInfoMessage` (464 bytes), already applied to
  `auStack_3e0` in function `FUN_000a01dc` (`SendCameraInfoMessage`)
- Confirmed facts to build on directly (don't re-derive):
  - Encode call: `FUN_0009b0cc(auStack_5bc, DAT_000a0d04, &auStack_3e0)`; `DAT_000a0d04` = `0x3f5e98`
  - nanopb `pb_msgdesc_t` header at `0x3f5e98`: `field_info` ptr = `0x3f5e3c`, `submsg_info`
    ptr = `0x3f5eb0`, `field_count` = 11
  - `submsg_info` array (8 pointers, ascending field-number order — confirmed match against
    known submessage field counts):

    | Address | Protobuf field # | Identity |
    |---|---|---|
    | `0x3f6128` | 1 | unknown (4 subfields/32B) |
    | `0x3f653c` | 2 | HW-version-ish (7 subfields/32B) |
    | `0x3f5cd0` | 3 | capabilities-ish (6/32B) |
    | `0x3f61b0` | **4** | **network_info** (2/68B top block; current network subblock holds SSID/MAC/IP/signal) |
    | `0x3f648c` | **5** | **extended_status** (11/152B; firmware/HW/name/RTSP/services/WebRTC) |
    | `0x3f6308` | 6 | simple pair (2/8B) |
    | `0x3f5e20` | 9 | `system_info` telemetry block |
    | `0x3f6584` | **11** | **video_quality** wrapper |
  - Confirmed semantic struct fields: `ir_mode` @ `0x05c`, `speaker_volume` @ `0x068`,
    `video_quality` @ `0x1cc`
  - 13 "has"-flag byte offsets, all unconditionally set to `1` by the real firmware (i.e.
    always present in the real message): `0x024, 0x048, 0x06c, 0x070, 0x0b4, 0x0d0, 0x0f0,
    0x104, 0x118, 0x134, 0x144, 0x170, 0x1c8`
  - Deployed result after executing this plan: `signaling.py::_send_post_auth` now sends
    fields `2`, `3`, `4`, `5`, `8`, `9`, and `11`, covering the real firmware's
    always-present optional field set.

---

## P0 — Identify the 13 unconfirmed "has"-flag fields (highest value, lowest risk)

For each flag offset below, use `mcp__ghidrassist__get_code` (decompiler format) on
`FUN_000a01dc` (already struct-typed, so it reads as `auStack_3e0.field_0xNNN`) to see
which value field(s) the flag gates, then `mcp__ghidrassist__struct` (`field_xrefs`, then
`rename_field`) to confirm via the actual setter/getter call names — the same method
that already worked for `ir_mode`/`speaker_volume`/`video_quality`.

- [x] `0x024` — gates top-level field 2, now identified as a timelapse-status block (`0x028`-`0x040`,
      7 values) based on code proximity; confirm via `field_xrefs`
- [x] `0x048` — gates top-level field 3 camera-status block: IR mode, upload interval/status, speaker volume
- [x] `0x06c` / `0x070` — gate top-level field 4 and nested field `4.1` current-network block;
      trace the surrounding calls (`FUN_00082330`, `FUN_00096be0`, `FUN_00096cc0`,
      `FUN_000969c0`) for names
- [x] `0x0b4` — gates top-level field 5 extended-status block (`0x0b8`-`0x14f`)
- [x] `0x0d0` — gates field `5.4`, a video/timelapse mode and storage/model-adjacent block using `FUN_000732f4`/`FUN_00073428`
- [x] `0x0f0` — gates field `5.6`, an RTSP enum pair using `FUN_000ae114`/`FUN_000ae06c` plus a string
      field (`local_440`) — possibly RTSP-mode-related (matches `config.rtsp_server_mode`
      noted in `status.md` but never pinned to an offset)
- [x] `0x104` — gates field `5.7`, log-level/status block (`FUN_000917d4`/`FUN_0009184c`,
      feeds into `FUN_0009d194`)
- [x] `0x118` — gates field `5.9`, service endpoint/status string block
- [x] `0x134` — gates field `5.10`, timezone/status block using `FUN_000b0154`/`FUN_000aff34`
- [x] `0x144` — gates field `5.11`, WebRTC mode/status using `FUN_000b3d40`/`FUN_000b3d8c`
- [x] `0x170` — gates top-level field 9 system telemetry (`FUN_00070a60`/`FUN_00070b7c`) plus an 8-byte value
      at `0x180`
- [x] `0x1c8` — gates top-level field 11 video-quality wrapper immediately before `video_quality` is
      written (already partially traced — this is the flag for the `video_quality` field
      itself, likely field 11's "has")
- [x] Write the full flag→field→semantic mapping into `status.md` (see Documentation
      section below) once each is confirmed, not before — don't let a plausible-looking
      code-proximity guess get written down as fact without an actual xref/name check

## P1 — Confirm `network_info` internal layout

- [x] `get_data_at` on `0x3f648c` showed this is field 5 `extended_status`, not network info; `get_data_at` on `0x3f61b0` confirmed field 4 as `network_info`
      the top-level one at `0x3f5e98` (field_info ptr, submsg_info ptr, field_count)
- [x] Parse its `field_info` array the same way — even without fully cracking nanopb's
      per-field bit encoding, the submsg_info pointer order + field_count (11) should let
      us at least enumerate sub-field boundaries
- [x] Cross-check the real field numbers for `ssid`/`mac`/`ip` against what's assumed in
      `signaling.py`: `encode_message({1: self.ssid, 2: self.mac, 3: self.ip})`
- [x] Identify the remaining `network_info` field observed in code: field `4.1.5` is signal quality from the `wlan0`-scoped function

## P2 — Confirm/deny scalar field numbers for `name` (8) and `firmware` (10)

- [x] Note: `name` (field 8 assumption) has *weak empirical support* already — an earlier
      real-hardware test showed the camera name displaying correctly in Prusa Connect
      after sending `8: 'Buddy3D Camera'`. That's evidence, not proof (a wrong field
      number could still coincidentally not break display). Worth confirming properly.
  - [x] `firmware` (field 10) has **no empirical confirmation** — Prusa Connect never
        showed a firmware version in any test this session. Treat as a live suspect.
  - [x] These are plain string fields, not submessages, so they won't appear in
        `submsg_info` — resolving them requires decoding entries in the top-level
        `field_info` array at `0x3f5e3c` (see P4) or finding another decompiled call site
        that references field numbers 8/10 directly (check `analyze_function` /
        `xrefs` on `FUN_000a01dc` callees for anything that looks like a name/version
        setter with a literal tag constant nearby)

## P3 — Map remaining submessages (fields 1, 2, 3, 4, 6) — never traced in detail

- [x] Dump `0x3f6128` (field 1), `0x3f5cd0` (field 3), `0x3f61b0` (field 4), `0x3f6308`
      (field 6) the same way as P1
- [x] For each, identify the C struct offsets already computed in `../research/camera_info_struct.c`
      that correspond to its subfields (cross-reference against the P0 flag/value
      mapping — several of these blocks were already spotted by code proximity in P0)
- [x] Original priority note superseded: fields 2/3/4 are now sent, field 5 is sent,
      and fields 1/6/7 remain descriptor-confirmed but not populated by the firmware's
      `SendCameraInfoMessage` decompile.

## P4 (stretch, higher risk of introducing new wrong guesses) — Decode nanopb's compact `field_info` bit format

- [x] Only attempt after P0-P3. Use the now-plentiful ground truth (fields 4, 5, 8, 9, 10 and 11
      confirmed two independent ways; ir_mode/speaker_volume/video_quality's exact byte
      offsets known) to validate any candidate bit-packing scheme *before* trusting it —
      if a decoding hypothesis doesn't reproduce all known-confirmed (offset, field
      number) pairs simultaneously, discard it rather than cherry-picking matches
- [x] If no bit-packing hypothesis can be confirmed with high confidence, leave the
      remaining scalar field numbers explicitly documented as unknown rather than
      guessing — do not let speculation get written into `protocol.md` as fact

## P5 (minor, background) — `FUN_0009b0cc`'s return-value semantics

- [x] Determine whether `iVar6 == 0` means encode success or failure (the branch
      structure in `FUN_000a01dc` suggests `!= 0` is the "success, dispatch the encoded
      message" path, but this was never confirmed) — matters only for understanding
      whether encode failures are silently swallowed on the real firmware; not blocking
      for the implementation change below

---

## Documenting findings

- **`status.md`** is the primary running log (per `CLAUDE.md`, read first by anyone
  continuing this work). Add a new dated section (`### Update (<date>) — ...`) following
  the existing style already in the file (see the "byte-accurate struct reconstructed"
  section from this session as a template). For each P0-P3 item resolved, state:
  what was checked, the tool call used, the confirmed result, and explicitly flag
  anything that remains a guess vs. is now confirmed — don't blur the two.
- **`protocol.md`** is the definitive spec (per `CLAUDE.md`). Once field numbers/semantics
  are confirmed (not before), update its `CameraInfoMessage`/`status` section with the
  real field table. Keep a clear marker (e.g. a "confidence" column or footnote) for any
  field whose number is still inferred rather than directly confirmed, so this doesn't
  regress into being read as settled fact later.
- **`../research/camera_info_struct.c`** / **`../research/compute_camerainfo_offsets.py`**: as fields get real
  names, apply the rename in Ghidra via `mcp__ghidrassist__struct` (`rename_field`), then
  regenerate/update `../research/camera_info_struct.c` to match so the repo copy stays in sync with
  the live Ghidra structure.
- **`implementation.md`**: update the `status` message code example once `signaling.py` is
  changed (see below), so the build guide doesn't go stale.

## Implementing changes based on findings

- **File**: `~/prusa-cam/signaling.py` on the Pi (`pi@<PI_IP>`), function
  `PrusaSignaling._send_post_auth`.
- Once P0-P2 give enough confidence, expand `status_msg` from its current 4 fields to
  include every field the real camera always sets (the 13 "has"-flag fields identified in
  P0), using the real field numbers and any newly-confirmed `network_info` internal order
  from P1.
- **Do not deploy to the Pi until P0-P2 are as complete as reasonably achievable** —
  batch the investigation into one implementation change, since each real-hardware test
  cycle is expensive (service restart, log correlation, sometimes a token rotation if the
  camera has to be re-paired).
- Keep the existing `asyncio.sleep(0.2-0.3)` timing between emits (confirmed hard
  constraint — removing it causes server disconnect within ~5s, per `status.md`).

## Verification (end-to-end, after implementation)

- [x] SSH to `pi@<PI_IP>`, restart `prusa-cam.service`
      (`sudo systemctl restart prusa-cam.service`), tail logs
      (`journalctl -u prusa-cam -f`) to confirm clean connect/auth/status with no
      disconnects. 2026-07-07 result: `/c/info` 200, auth ACK `1`, snapshots 200,
      `status` 371 bytes, `protobuf_version` 27 bytes, `features` 391 bytes, stable
      connection for 90+ seconds.
- [x] In Prusa Connect, check whether "Watch Live" now appears / whether any `webrtc`
      event is received (this has been zero all session — the key thing to watch for)
      2026-07-07 result: mobile app still showed the communication failure, and no
      `trigger`, `configuration`, or `webrtc` event arrived.
- [x] If inconclusive, repeat the `tcpdump` + Pi-log timestamp correlation method used
      earlier this session (see `status.md`'s hypothesis-testing section) to check for
      any incoming signaling traffic. 2026-07-07 result: phone `<HOST_IP>` emitted
      only mDNS/Bonjour multicast; no direct Pi traffic.
- [x] Record the outcome in `status.md` regardless of result — a clean negative result
      (fuller status message sent, still no `webrtc` event) is itself valuable: it would
      rule out "incomplete status message" and leave the serial-number/provisioning
      hypothesis as the strongest remaining lead by elimination
