# Licensing, IP & Legal Notice

## Licensing decision: intentionally **no license** (all rights reserved)

This repository ships **without an open-source license file**. Under default copyright that
means "all rights reserved" — the safest posture while the repo is **private**.

Why not a permissive license yet:

- The documentation describes Prusa's **proprietary** cloud protocol. Reverse engineering for
  interoperability is broadly permissible (e.g. EU Software Directive 2009/24/EC Art. 6, and
  US fair-use precedent), but *granting others redistribution rights* over a derived protocol
  spec is a separate, murkier question.
- The repo is private and there is **no commercial interest**, so no license is needed for the
  author's own use. A license can always be added later; it cannot easily be walked back.

### If you later make it public

The author's **own original code** (`pi-impersonator/`, `proxy/`, `research/`) and the
prose docs can be released — recommended: **MIT** (or Apache-2.0 for its patent grant) — by
adding a `LICENSE` file and a header note that the protocol documentation is the result of
independent reverse engineering for interoperability and contains no Prusa source code.
Keep the firmware-exclusion below in force regardless.

## Prusa intellectual property

- No Prusa source code is included. The protocol was reconstructed from observed behaviour and
  from decompiling the shipped firmware binary — a clean-room-style interoperability effort.
- Prusa®, Prusa Connect, and Buddy3D are trademarks of Prusa Research a.s. This project is
  **not affiliated with or endorsed by Prusa Research**.

## Firmware is not redistributed

The following are **excluded from the repo** (enforced by `.gitignore`) because redistributing
Prusa's firmware or its decompilation would infringe Prusa's copyright:

| Excluded | What it was |
|---|---|
| `oem.img`, `boot.img` | raw camera firmware images (UBI / boot) |
| `lp_app`, `lp_app.c`, `lp_app.h`, `lp_app.gzf` | the camera's main binary + its Ghidra decompilation/export |
| `oem_extracted/` | unpacked firmware filesystem |
| `ghidrassist_*.db` | Ghidra analysis databases |

How to obtain and analyse the firmware yourself is documented in
[`docs/sources.md`](docs/sources.md) and [`docs/reverse-engineering.md`](docs/reverse-engineering.md).

## No warranty

Provided as-is. Modifying camera/cloud behaviour can break your device or violate Prusa's
terms of service. You are responsible for your own use.
