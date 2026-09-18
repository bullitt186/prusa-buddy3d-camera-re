# Tools Used

Everything used to reverse the protocol and build the implementations.

## Firmware extraction & static analysis

| Tool | Version / notes | Used for |
|---|---|---|
| **ubi_reader** (`ubireader_extract_files`) | `pip install ubi_reader` | Unpack `oem.img` (UBI) → camera `/oem` filesystem |
| **binutils `strings`** | `strings -t d` | Pull URLs, event names, config keys, log formats, mangled C++ symbols from `lp_app` |
| **Ghidra** | 12.1.3 (public) | Decompile the stripped ARM `lp_app` binary (`ARM:LE:32:v7`, uClibc) |
| **JDK** | OpenJDK 21 | Runtime for Ghidra |
| **radare2 / ARM binutils** | 5.5.0 / 2.42 | Independent disassembly, ELF section and ARM unwind-table comparison |
| **GhidrAssistMCP** | MCP server bridging Claude ↔ the open Ghidra GUI | Fast decompile / struct / xref queries (`get_code`, `struct`, `variables`, `get_data_at`, `xrefs`) — preferred over headless |
| **Ghidra headless** (`analyzeHeadless`) | `-noanalysis -postScript` | Batch scripts: descriptor dumps, sender discovery, forced decompilation (see `docs/reverse-engineering.md`) |

Key domain knowledge applied during static analysis: **nanopb** protobuf descriptor layout
(the firmware encodes protobuf via nanopb callbacks), and **Socket.IO / Engine.IO** framing.

## Dynamic analysis / capture

| Tool | Used for |
|---|---|
| **tcpdump** (on the Pi `wlan0`) | Packet capture during live app-open tests; mDNS cross-referencing |
| **Chrome DevTools → HAR export** | Capturing Prusa Connect **web** app traffic (control commands: reboot, IR mode, quality). HARs are git-ignored — they contain account tokens. |
| **Prusa Connect** iOS app + web UI | Ground-truth behaviour, camera registration, token/QR retrieval |

## Impersonator / proxy build & runtime

| Tool | Used for |
|---|---|
| **Rust** (edition 2021, toolchain ≥ 1.85) + **cargo** + **just** | `proxy/` — the cloud-stream proxy & control tool |
| Rust crates: `tokio`, `axum`, `reqwest` (rustls), `prost`/protobuf, `webrtc` 0.17 | async runtime, HTTP, WebRTC, protobuf |
| **Python 3** (venv `--system-site-packages`) | `pi-impersonator/` — the on-device camera impersonator |
| **PyGObject / GStreamer** (`gst-rtsp-server`) | Pi RTSP server + capture pipeline |
| **rpicam-apps**, **libcamera** | Pi camera capture (`rpicam-hello`, `rpicam-jpeg`, `libcamerasrc`) |
| **VLC** | Verifying the local RTSP stream (`rtsp://<host>:8554/live`) |
| **protoc** / `cargo xtask gen-proto` | Regenerating Rust protobuf bindings from `proto/buddy3d.proto` |

## Target hardware

- **Raspberry Pi Zero 2 W**, Raspberry Pi OS Lite 64-bit (Debian trixie), camera module.
- Original device: Prusa Buddy3D Camera (Rockchip SoC, ARM 32-bit, firmware through 3.1.6).
