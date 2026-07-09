# buddy3d-proxy (Rust)

A companion tool to the [Pi impersonator](../pi-impersonator/). It logs into a Prusa account,
connects to Prusa signaling, negotiates WebRTC with an **already-registered** Buddy3D camera,
and **re-serves that cloud stream as a local RTSP URL** — plus a set of camera control
commands. WebRTC stays idle until a viewer connects and is torn down after the last one leaves.

Use it to watch/record a real Prusa camera locally (VLC, Home Assistant, Frigate) without the
app, and to fix cameras that auto-degrade their quality after many reconnects.

## Prerequisites

- Rust toolchain ≥ 1.85 (`rustup`) and optionally [`just`](https://github.com/casey/just).
- A Prusa account with at least one camera.

## Configure

```bash
cp ../config/buddy3d-proxy.env.example .env   # .env is git-ignored
# edit .env: set PRUSA_EMAIL and PRUSA_PASSWORD (everything else has defaults)
```

Config is entirely environment-driven (`src/config.rs`); `.env` is loaded automatically by
`just`. Full variable reference is in the example file.

## Build & run

```bash
just release          # or: cargo build --release
just list-cameras     # verify auth; lists printers + cameras on the account
just serve            # start the RTSP proxy on RTSP_PORT (default 8554)

# then, from anywhere:
vlc rtsp://<host>:8554/<camera-slug>
```

Without `just`, use `cargo run --release --bin buddy3d-proxy -- <subcommand>`.

## Subcommands

| Command | Does |
|---|---|
| `list-cameras` | Log in, list every printer + camera on the account (caches tokens) |
| `serve` | Run the RTSP proxy (idle until a client connects) |
| `watch-stream --duration-seconds N` | Connect + log RTP packet stats for N seconds (diagnostics) |
| `restart-camera [--field 9]` | Reboot the camera (`start_device_reboot`) |
| `set-mode --mode {1\|2\|3}` | IR / day-night: 1 Auto, 2 Day, 3 Night |
| `set-quality --quality {1\|2\|3}` | Resolution: 1 SD, 2 HD, 3 FHD — undo auto-degradation |
| `health --port 8080` | Probe `/healthz` (for container healthchecks) |

## Docker

A `Dockerfile` is included. Provide the same env vars (`--env-file .env`), publish the RTSP and
health ports, and mount a volume for `TOKEN_STORE_PATH` so tokens persist across restarts.

## Notes

- Real-backend smoke tests are `#[ignore]`d; run with `just smoke` (needs `PRUSA_EMAIL`/
  `PRUSA_PASSWORD`).
- Some control-command field numbers were confirmed from captured browser HARs; a few
  (`get_snapshot`, `snapshot_interval`, …) are still unknown — see the `restart-camera`
  help and [`../docs/protocol.md`](../docs/protocol.md).
