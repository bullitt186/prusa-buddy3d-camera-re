# Sources

Where the analysed material came from. **None of the copyrighted firmware/source below is
redistributed in this repo** (see [`NOTICE.md`](../NOTICE.md)) — this file is the pointer so
the work can be reproduced.

## Primary: the camera firmware (not in repo)

| Item | Description |
|---|---|
| `oem.img` | Raw UBI image of the camera's `/oem` partition, firmware **3.1.5**. Contains the main app binary. |
| `boot.img` | Boot image from the same firmware. |
| `lp_app` | `oem_extracted/…/oem/usr/sbin/lp_app` — the camera's main application. ARM 32-bit ELF, stripped, uClibc. **This binary is the primary RE target.** |
| `../research/RK_OTA_update.sh` | Rockchip OTA update helper found on the device (kept in `research/` — it's a short shell script, not Prusa binary code). |

**How to obtain:** extract from the physical camera's flash / a Prusa firmware OTA package,
then unpack with `ubireader_extract_files` (see [`reverse-engineering.md`](reverse-engineering.md)).
Do not commit the resulting images or decompilation.

## Third-party projects referenced

| Project | License | How it helped |
|---|---|---|
| [tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One](https://github.com/tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One) | MIT | Community SD-card overlay that runs custom scripts on the camera. **Confirmed** config file paths, token source, upload-interval units, RTSP mode values, and Rockchip MPI video capture. Not vendored here — clone it separately if needed. |
| [Prusa-Firmware-Buddy](https://github.com/prusa3d/Prusa-Firmware-Buddy) (6.6.1) | GPL / see repo | Printer-side firmware. Reference for the printer↔camera relationship and UDP print-metrics used for timelapse triggering. |
| Prusa public **Camera API** OpenAPI spec | Prusa docs | Pinned the camera **origin** enum (`WEB`, `OTHER`, …) used at registration. |
| [nanopb](https://github.com/nanopb/nanopb) | zlib | Reference for the protobuf descriptor/callback layout the firmware uses. |

## Live backend endpoints (observed, not secret)

These Prusa hostnames are the protocol's public endpoints (documented here for reference; not
credentials):

- `connect.prusa3d.com` — Prusa Connect API
- `camera-signaling.prusa3d.com` — Socket.IO/WebRTC signaling
- `webcam.connect.prusa3d.com` — snapshot / camera-info HTTP upload
- `camera-service-api.prusa3d.com` — camera registration / info API
- `connect-ota.prusa3d.com` — firmware OTA

## Provenance note

All account-specific values from real captures (tokens, fingerprints, IPs, MACs, SSIDs,
the registration QR) were **redacted** to `<PLACEHOLDER>` form before anything entered this
repo. If you find a real-looking secret, it's a bug — please scrub it.
