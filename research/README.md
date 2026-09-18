# Research helpers

Small standalone artifacts from the reverse-engineering work.

| File | What it is |
|---|---|
| `camera_info_struct.c` | Byte-exact C struct for the firmware's `CameraInfoMessage` (field offsets from Ghidra). Reference for building the protobuf payload. |
| `compute_camerainfo_offsets.py` | Generator that computes the struct offsets above from the descriptor tables. |
| `RK_OTA_update.sh` | Rockchip OTA update helper found on the device. Kept for reference only — do **not** run it against a camera you care about. |
| `ghidra/ExportAllDecomp.java` | Recreates the complete per-function decompiler export cited by the implementation gap tracker. |
| `ghidra/DecompileFunctions.java` | Prints selected functions by VMA for focused inspection. |
| `ghidra/ExportFidHashes.java` | Produces relocation-insensitive function hashes for firmware comparisons. |
| `ghidra/ListXrefs.java` / `ShowData.java` | Resolves reference paths and literal-pool/string targets. |

See [`../docs/camerainfo-verification.md`](../docs/camerainfo-verification.md) for how these were
produced and validated. Full 3.1.6 export commands and evidence-handling rules are in
[`../docs/reverse-engineering.md`](../docs/reverse-engineering.md).
