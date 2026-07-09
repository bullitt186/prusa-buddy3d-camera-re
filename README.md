# Prusa Buddy3D Camera — Protocol Reverse Engineering & Impersonator

Reverse engineering of the **Prusa Buddy3D Camera** cloud protocol (firmware `3.1.5`),
plus two working implementations that let a Linux box (Raspberry Pi) take the camera's
place or pull its stream locally.

The camera normally only works through Prusa Connect's cloud. This project documents its
protocol from the ARM firmware and reimplements it so you can:

- **Impersonate** the camera from a Raspberry Pi + any camera module — it registers to
  Prusa Connect as a genuine Buddy3D camera. → [`pi-impersonator/`](pi-impersonator/)
- **Proxy** an already-registered camera's cloud WebRTC stream to a local RTSP URL and
  send control commands (reboot, resolution, IR mode). → [`proxy/`](proxy/)

> **Status:** partially working against the live Prusa backend. Local RTSP, auth, and the
> corrected `/c/info` upload are confirmed; full end-to-end cloud playback in the Prusa app
> is not yet green. See [`docs/status.md`](docs/status.md) for the exact confirmed-vs-pending
> line.

## Start here

| If you want to… | Read |
|---|---|
| Know what actually works vs. what's still theory | [`docs/status.md`](docs/status.md) ⭐ |
| Understand the wire protocol (the spec) | [`docs/protocol.md`](docs/protocol.md) |
| Run the Pi camera impersonator | [`pi-impersonator/README.md`](pi-impersonator/README.md) |
| Run the local RTSP proxy / control tool | [`proxy/README.md`](proxy/README.md) |
| Reproduce or extend the RE work | [`docs/reverse-engineering.md`](docs/reverse-engineering.md) |
| See what was tried and failed | [`docs/dead-ends.md`](docs/dead-ends.md) |
| Know the tools used | [`docs/tools.md`](docs/tools.md) |
| Know the firmware / project sources | [`docs/sources.md`](docs/sources.md) |
| Check the official REST spec (no WebRTC) | [`docs/openapi.yaml`](docs/openapi.yaml) |
| Understand licensing & IP boundaries | [`NOTICE.md`](NOTICE.md) |

## Repository layout

```
├── README.md                 ← you are here (index)
├── NOTICE.md                 licensing decision + Prusa-IP / firmware boundary
├── docs/
│   ├── status.md             confirmed vs pending, evidence vs assumption ⭐
│   ├── protocol.md           definitive wire-protocol spec (single source of truth)
│   ├── implementation.md     build guide with constants & payload shapes
│   ├── reverse-engineering.md how to reproduce the RE (Ghidra, VMAs, techniques)
│   ├── tools.md              every tool used
│   ├── sources.md            firmware image + third-party projects referenced
│   ├── openapi.yaml           Prusa's official Camera API spec (v0.22.0, REST only — no WebRTC)
│   ├── dead-ends.md          errors, red herrings, corrected assumptions
│   ├── next-steps.md         prioritised open work
│   ├── camerainfo-verification.md  checklist that pinned the CameraInfo struct
│   └── journal/              raw research journal (archive; contains superseded claims)
├── pi-impersonator/          Python impersonator that runs on the Pi (primary impl)
├── proxy/                    Rust cloud-stream proxy + camera control tool
├── config/                   config templates (real secrets stay out of git)
└── research/                 RE helper scripts (struct generator, OTA script)
```

## Evidence conventions

Throughout the docs:

- **Confirmed** / verified live / observed = proven against the real firmware or backend.
- **Assumption** / likely / probably / *guess* = inferred, not yet proven.
- `<PLACEHOLDER>` = a value redacted from real captures (IP, MAC, SSID, token). Supply your own.

## Not included (on purpose)

The Prusa firmware binary, its decompilation, and Ghidra databases are **not** in this repo
for IP reasons — only *how to obtain and analyse them* is documented. See
[`docs/sources.md`](docs/sources.md) and [`NOTICE.md`](NOTICE.md).
