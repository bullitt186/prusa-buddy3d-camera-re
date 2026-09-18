# Firmware 3.1.6 delta analysis

Compared directly against the public 3.1.5 OTA package on 2026-09-17. Firmware artifacts,
Ghidra databases, and decompilation remain outside this repository under
`~/firmware-analysis/`.

Package SHA-256: 3.1.5 `57a1bd8bbd869e1bfe652e680fdce9620156dc2f49fdfdc9bb5859a7f5862dda`;
3.1.6 `094d420db6faa172cad8bffdeef792cbefebbb3c6a782669076eaca790917612`.

Evidence markers follow the rest of this project: **[confirmed]** is directly observed in the
packages/binaries; **[inferred]** is the narrowest explanation consistent with those facts.

## Executive result

There is no observed cloud-protocol change to carry into the impersonator. **[confirmed]**
Firmware 3.1.6 keeps protobuf schema `4.4`, the same Socket.IO events/features, the same HTTP
keys and endpoints, and the same protocol-critical code in the `/c/info`, nanopb string encoder,
and WebRTC dispatch paths (address-normalized Ghidra decompilation is identical).

The impersonator should carry over only the advertised firmware identity (`3.1.6`). Its existing
`NB.1.1.0` / `Buddy3D-C1` identity remains valid. Native kernel, rootfs, and Realtek driver
updates belong to the original Rockchip camera and must not be copied to Raspberry Pi OS.

## Package-level diff

| Artifact | 3.1.5 | 3.1.6 | Finding |
|---|---:|---:|---|
| `boot.img` | 2,914,816 B | 2,914,304 B | Kernel/resource payload rebuilt |
| `oem.img` | 19,660,800 B | 19,660,800 B | Same-size UBI image |
| `rootfs.img` | absent | 8,650,752 B | Full rootfs included in the OTA |
| `lp_app` | 4,873,460 B | 4,881,652 B | Main application changed |

The extracted OEM partitions each contain 260 files. **[confirmed]** 258 are byte-identical;
only `usr/sbin/lp_app` and `usr/ko/8188fu.ko` differ. The Wi-Fi module still reports the same
Realtek version (`v5.11.5.2-2-g57aeb1afd.20220114_beta`), source version, kernel vermagic,
parameters, and ELF section layout. This is consistent with a rebuild for the bundled platform
update, not a new interface for the impersonator.

## `lp_app` changes

ELF dependencies, `.rodata` size, dynamic symbols, human-readable strings (apart from the
firmware version), protocol schema version, feature list, event names, endpoints, and JSON keys
are unchanged. `.text` grows by 4,096 bytes and `.bss` by exactly 3,072 bytes.

The growth comes primarily from an expanded hardware-version classification table that is
compiled into 16 translation units. Each copy adds three 64-byte range/model pairs, explaining
the complete BSS increase: `16 × 3 × 64 = 3,072` bytes. **[confirmed]** The added classifications
are:

| Numeric hardware range | Hardware string | Model |
|---:|---|---|
| 77,400–79,399 | `NB.1.0.1` | `Buddy3D` |
| 79,400–85,399 | `NB.1.1.0` | `Buddy3D-C1` |
| 85,400–94,399 | `NB.1.1.0` | `Buddy3D-C1` |

The existing `NB.1.1.1` / `Buddy3D-POE` range is also widened at its lower boundary from
1,000,030 to 1,000,000. No new model or hardware-version strings were introduced.

This changes how genuine devices classify additional SPI hardware-version values. It does not
add unique device identity, authentication material, a new camera class on the wire, or a new
backend enrollment mechanism.

## Protocol regression checks

Complete headless exports covered every Ghidra-defined function: 10,548 functions in 3.1.5 and
10,552 in 3.1.6, with zero decompiler failures. The exported corpora, Function-ID tables, and
comparison report are under `~/firmware-analysis/`. Ghidra's relocation-insensitive Function-ID
hashes—including the call-target hash—match exactly for all protocol-critical routines below:

| Role | 3.1.5 | 3.1.6 |
|---|---:|---:|
| `/c/info` JSON builder | `0x00061bbc` | `0x00062d74` |
| nanopb string encoder | `0x0009c294` | `0x0009d44c` |
| WebRTC message dispatch/gate | `0x000b87b4` | `0x000b996c` |
| WebRTC answer/candidate encoder | `0x000a2cd8` | `0x000a3e90` |
| WebRTC numeric-type translator | `0x000b5be4` | `0x000b6d9c` |
| local ICE candidate emission | `0x000b6428` | `0x000b75e0` |
| QR token parser/validator | `0x00067898` | `0x00068a50` |
| QR payload dispatcher | `0x0006e9dc` | `0x0006fb94` |
| token persistence (`setHttpToken`) | `0x00081d34` | `0x00082eec` |
| fingerprint seed generation | `0x00095b20` | `0x00096cd8` |
| fingerprint MD5/hex encoding | `0x00096894` | `0x00097a4c` |

The three WebRTC encode/translate/emission rows also confirm the camera-side wire enum remains `1=request`, `2=answer`,
`3=offer`, `4=candidate`. No 3.1.6 enrollment request, new endpoint, authentication field,
Socket.IO event, TURN configuration path, or WebRTC enable mechanism was found. **[confirmed]**

The token trace is unchanged as well. Connect—not the firmware—creates the random 20-character
alphanumeric token. Firmware accepts it as the QR JSON's top-level `token` value and persists it;
there is no device-side token derivation. The separate fingerprint is generated from the uppercase,
colon-separated `wlan0` MAC (random 10-character fallback) and MD5-encoded as lowercase hex before
transmission. Full provenance and function flow are documented in [`protocol.md`](protocol.md#firmware-token-provenance-316).

**Conclusion:** keep protocol schema `4.4`, feature/capability payloads, signaling behavior, and
`Buddy3D-C1` model identity unchanged. Advertise firmware `3.1.6` in `/c/info`, status/features
protobuf messages, the OTA header, and the signaling User-Agent.
