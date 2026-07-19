# Licensing, IP & Legal Notice

## License: MIT

This repository is released under the **MIT License** — see [`LICENSE`](LICENSE).

The license covers all original work: the impersonator code (`pi-impersonator/`), the proxy
(`proxy/`), RE helper scripts (`research/`), and the protocol documentation (`docs/`). The
protocol documentation is the result of independent reverse engineering for interoperability
and contains no Prusa source code (EU Software Directive 2009/24/EC Art. 6 / US fair use).

**Not covered:** `docs/openapi.yaml` is Prusa Research's published Camera API spec — it has
been replaced with a pointer to `https://connect.prusa3d.com/docs/cameras/openapi/`.

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
