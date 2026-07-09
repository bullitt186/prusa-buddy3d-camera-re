# Research helpers

Small standalone artifacts from the reverse-engineering work.

| File | What it is |
|---|---|
| `camera_info_struct.c` | Byte-exact C struct for the firmware's `CameraInfoMessage` (field offsets from Ghidra). Reference for building the protobuf payload. |
| `compute_camerainfo_offsets.py` | Generator that computes the struct offsets above from the descriptor tables. |
| `RK_OTA_update.sh` | Rockchip OTA update helper found on the device. Kept for reference only — do **not** run it against a camera you care about. |

See [`../docs/camerainfo-verification.md`](../docs/camerainfo-verification.md) for how these were
produced and validated.
