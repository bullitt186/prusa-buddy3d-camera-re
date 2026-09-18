# Agent instructions — Prusa Buddy3D Camera RE

Reverse engineering of the Prusa Buddy3D Camera cloud protocol (firmware through `3.1.6`)
plus two working reimplementations. Full orientation is in
[`README.md`](README.md); this file is the fast path for coding agents.

## Where things are

| Need | Go to |
|---|---|
| Implement firmware-parity gaps | `docs/firmware-implementation-gap-tracker.md` ⭐ read before coding |
| What's confirmed vs. still failing | `docs/status.md` |
| The wire protocol (source of truth) | `docs/protocol.md` |
| Errors / red herrings / corrected assumptions | `docs/dead-ends.md` — check before re-deriving anything |
| Reproduce the RE (Ghidra, VMAs, techniques) | `docs/reverse-engineering.md` |
| Pi camera impersonator (Python, primary impl) | `pi-impersonator/` |
| Rust cloud-stream proxy + control tool | `proxy/` |
| Tools / sources | `docs/tools.md`, `docs/sources.md` |

## Evidence precedence

When documents disagree, use this order:

1. Direct 3.1.6 decompiler control flow, descriptors, or genuine-camera captures cited in
   `docs/firmware-implementation-gap-tracker.md`.
2. Confirmed statements in `docs/protocol.md` and `docs/status.md`.
3. Version-delta and reproduction notes in `docs/firmware-3.1.6.md` and
   `docs/reverse-engineering.md`.
4. Historical journal material, which may contain superseded conclusions.

Treat a conflict as documentation debt: implement from the higher-ranked evidence and update the
lower-ranked document in the same change. Never silently choose the older prose. Fields marked
`descriptor required`, `BLOCKED`, or **assumption** are not implementation specifications.

## Closing an implementation gap

Use the tracker as the work queue. Work on one named `GAP-*` item, or an explicitly stated cohesive
group, and follow this sequence:

1. Read the gap, its evidence row, every cited decompiled line range, and the current target code.
2. Confirm that no prerequisite in the tracker's ambiguity table is unresolved. If one is, recover
   the descriptor/capture first or leave the gap open; do not infer numeric tags or enum values.
3. Implement the smallest shared-state/API change that reproduces the documented behavior. Keep
   hardware-specific Pi policy separate from wire compatibility.
4. Add automated tests matching the gap's acceptance criteria. Prefer decoded/golden wire fixtures
   over assertions against implementation internals.
5. Run the local validation commands below. If the gap requires hardware or Connect verification,
   additionally follow `.agent/pi-ops.md` only after live work has been explicitly requested.
6. Update the tracker checkbox and record the implementing commit, tests, verification date, and
   whether the result is confirmed or still an assumption. Update `protocol.md`/`status.md` when
   externally visible behavior changed.

The owner must choose policy before closing gaps that offer alternatives such as implement versus
stop advertising. This currently includes OTA, timelapse, reboot, WebRTC audio, and unsupported IR,
speaker, fan, or MicroSD behavior. Do not make that product decision implicitly.

## Local development and validation

The Python tests use the standard library and do not require the Pi runtime dependencies:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pi-impersonator tests
```

When changing the Rust proxy, also run:

```bash
cd proxy
cargo test
```

The Pi runtime dependencies (`aiohttp`, `python-socketio`, GStreamer, and PyGObject) are provisioned
by `pi-impersonator/bootstrap.sh`; do not add host-specific virtual environments to the repository.
There is currently no repository-wide formatter or type checker. Do not claim those checks ran.

## Working rules

- **Never commit personal data or firmware.** IPs, MACs, SSIDs, tokens, real usernames/home
  paths, `*.img`, `lp_app*`, `.har`, screenshots, `config.ini`, `.env`, `tokens.json` are all
  git-ignored — keep it that way. Redact captured values to `<PLACEHOLDER>` form.
- The firmware binary is **not** in the repo (IP). See `docs/sources.md` for how to obtain it.
- Protocol facts must stay honest: mark **confirmed** (verified live/against firmware) vs.
  **assumption**. Don't promote a guess to a fact without evidence.
- Ghidra work: prefer **GhidrAssistMCP** (`mcp__ghidrassist__*`) over headless — it attaches to
  the open GUI session on `lp_app`. MCP config is in `.codex/config.toml`.
- For the checked full decompiler export and exact `file:line` anchors, follow
  `docs/reverse-engineering.md`. The export and firmware remain outside Git.

## Working with the physical Pi / camera

Live device access (SSH host, deploy, flash, service management, OTA) is **personal and
git-ignored**. If [`.agent/pi-ops.md`](.agent/pi-ops.md) exists in your checkout, read it for
the real connection details and runbook. It is not on GitHub by design — see
`.agent/pi-ops.example.md` for the (redacted) template if the local file is missing.

**Authorization boundary:** editing or testing repository files does not authorize touching a live
Pi, Connect account, token, camera, or firmware. Do not deploy, mint/rotate tokens, call mutating
backend endpoints, reboot devices, or flash firmware unless the user explicitly requests that live
action. Read-only inspection is still subject to the redaction rules above.

## Making changes to the Pi that survive reboot

The Pi power-cycles with the printer (no clean shutdown), so its **root filesystem is a
read-only overlayfs**: at runtime all writes go to a RAM (tmpfs) upper layer and are
**discarded on every reboot — by design**. This is the durability feature, not a bug.

**Consequence:** any change that must persist — app code, `/etc` configs, `apt` packages,
`config.ini`/token — MUST reach the *lower* (real) filesystem. A bare `ssh … rsync`, `nano`,
or `apt install` on a running prod Pi **is silently lost on the next reboot.**

- **Always deploy via `pi-impersonator/deploy.sh`** (`PI=user@host ./deploy.sh`). It detects the
  overlay state and, in prod mode, automates disable-overlay → reboot → deploy → re-enable →
  reboot so the change lands on disk. In dev mode (overlay off) it's a fast rsync + restart.
- **Check a Pi's mode:** `findmnt -no FSTYPE /` → `overlay` means prod/read-only.
- **Intentionally ephemeral — never try to persist these:** `/etc/prusa-cam/quality.env` (resets
  to FHD on reboot; fine) and journald logs (RAM, `Storage=volatile`). To keep logs across a
  reboot for debugging, flip journald to `persistent` through the maintenance flow, not ad-hoc.
- Real host, maintenance-mode commands, and recovery notes are in `.agent/pi-ops.md`.
