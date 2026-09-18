# Agent instructions — Prusa Buddy3D Camera RE

Reverse engineering of the Prusa Buddy3D Camera cloud protocol (firmware through `3.1.6`)
plus two working reimplementations. Full orientation is in
[`README.md`](README.md); this file is the fast path for coding agents.

## Where things are

| Need | Go to |
|---|---|
| What's confirmed vs. still failing | `docs/status.md` ⭐ read first |
| The wire protocol (source of truth) | `docs/protocol.md` |
| Errors / red herrings / corrected assumptions | `docs/dead-ends.md` — check before re-deriving anything |
| Reproduce the RE (Ghidra, VMAs, techniques) | `docs/reverse-engineering.md` |
| Pi camera impersonator (Python, primary impl) | `pi-impersonator/` |
| Rust cloud-stream proxy + control tool | `proxy/` |
| Tools / sources | `docs/tools.md`, `docs/sources.md` |

## Working rules

- **Never commit personal data or firmware.** IPs, MACs, SSIDs, tokens, real usernames/home
  paths, `*.img`, `lp_app*`, `.har`, screenshots, `config.ini`, `.env`, `tokens.json` are all
  git-ignored — keep it that way. Redact captured values to `<PLACEHOLDER>` form.
- The firmware binary is **not** in the repo (IP). See `docs/sources.md` for how to obtain it.
- Protocol facts must stay honest: mark **confirmed** (verified live/against firmware) vs.
  **assumption**. Don't promote a guess to a fact without evidence.
- Ghidra work: prefer **GhidrAssistMCP** (`mcp__ghidrassist__*`) over headless — it attaches to
  the open GUI session on `lp_app`. MCP config is in `.codex/config.toml`.

## Working with the physical Pi / camera

Live device access (SSH host, deploy, flash, service management, OTA) is **personal and
git-ignored**. If [`.agent/pi-ops.md`](.agent/pi-ops.md) exists in your checkout, read it for
the real connection details and runbook. It is not on GitHub by design — see
`.agent/pi-ops.example.md` for the (redacted) template if the local file is missing.

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
