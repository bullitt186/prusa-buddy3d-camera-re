# Reverse Engineering Analysis — Reproduction Guide

## Firmware Extraction

```bash
pip install ubi_reader
ubireader_extract_files oem.img -o oem_extracted
```

Produces: `oem_extracted/897730261/oem/` — the camera's `/oem` partition.
Main binary: `oem_extracted/897730261/oem/usr/sbin/lp_app` (ARM ELF 32-bit, stripped, uClibc).

## String Analysis

```bash
strings -t d oem_extracted/897730261/oem/usr/sbin/lp_app | grep -i "pattern"
```

This revealed: server URLs, event names, log format strings, HTTP headers, config keys, AT commands, feature names, and C++ mangled symbol names.

## Ghidra Setup

**Installed at (current workstation):**
- JDK 21: system OpenJDK (`/usr/lib/jvm/java-21-openjdk-amd64/`)
- Ghidra 12.1.3: `~/tools/ghidra_12.1.3_PUBLIC/`
- Projects: `~/firmware-analysis/ghidra-projects/buddy3d-3.1.5` and
  `~/firmware-analysis/ghidra-projects/buddy3d-3.1.6` (both imported as `ARM:LE:32:v7`)

**Running headless scripts:**
```bash
~/tools/ghidra_12.1.3_PUBLIC/support/analyzeHeadless \
  ~/firmware-analysis/ghidra-projects buddy3d-3.1.6 \
  -process lp_app \
  -noanalysis \
  -postScript YourScript.java \
  -scriptPath "$PWD/research/ghidra"
```

Use `-noanalysis` since the binary is already analyzed. Scripts must be referenced by filename
only.

**Opening GUI:**
```bash
~/tools/ghidra_12.1.3_PUBLIC/ghidraRun
```
Open either project under `~/firmware-analysis/ghidra-projects/`, file `lp_app`.

For a repeatable one-off decompile, use the checked-in helper:

```bash
~/tools/ghidra_12.1.3_PUBLIC/support/analyzeHeadless \
  ~/firmware-analysis/ghidra-projects buddy3d-3.1.6 \
  -process lp_app -noanalysis -readOnly \
  -scriptPath "$PWD/research/ghidra" \
  -postScript DecompileFunctions.java 0x62d74 0xb996c
```

See [`firmware-3.1.6.md`](firmware-3.1.6.md) for the version-to-version findings.

### Recreate the complete 3.1.6 export used by the gap tracker

The implementation tracker cites line numbers in a complete per-function export. Recreate it with
the checked-in `ExportAllDecomp.java` script; do not put the resulting copyrighted decompilation in
Git:

```bash
mkdir -p ~/firmware-analysis/decompiled-3.1.6-full
~/tools/ghidra_12.1.3_PUBLIC/support/analyzeHeadless \
  ~/firmware-analysis/ghidra-projects buddy3d-3.1.6 \
  -process lp_app -noanalysis -readOnly \
  -scriptPath "$PWD/research/ghidra" \
  -postScript ExportAllDecomp.java \
  "$HOME/firmware-analysis/decompiled-3.1.6-full"
```

The output must contain `functions.tsv`, `summary.txt`, and `functions/*.c`. Before relying on the
tracker's line anchors, verify that `summary.txt` identifies `lp_app`, reports `failed=0`, and that
the expected file exists, for example:

```bash
grep -E '^(program|total|success|failed)=' \
  ~/firmware-analysis/decompiled-3.1.6-full/summary.txt
test -f ~/firmware-analysis/decompiled-3.1.6-full/functions/000a1394__FUN_000a1394.c
```

Generate the strings file cited by the tracker from the same 3.1.6 binary:

```bash
strings ~/firmware-analysis/cam-3.1.6/oem-extracted/*/oem/usr/sbin/lp_app \
  > ~/firmware-analysis/cam-3.1.6/lp_app.strings
```

If the project was reanalysed and line numbers moved, locate evidence by the function VMA in the
filename and then by the constants/branches described in the tracker. A missing local export is a
recovery prerequisite, not permission to substitute an older prose claim.

## Key Analysis Techniques

### Finding functions in a stripped binary

- String literals in `.rodata` are loaded via ARM literal pools (PC-relative `ldr`)
- Search for a known string VMA as a 32-bit LE value in `.text` → finds the literal pool
- The function containing that literal pool is the function of interest
- Scan backwards from literal pool for `push {r4,...,lr}` (ARM: `0xe92d____`) to find function entry

### Finding protobuf descriptors

- `pb_encode_string_cus` at VMA `0x0009c294` is the callback for all string fields
- Search for `0x0009c294` as a literal pool value → finds all encode call sites
- Each encode function's literal pool also references its descriptor table (in `0x3f5xxx-0x3f6xxx` range)
- Descriptor tables: `[field_info_ptr, submsg_ptr, 0, callback, field_count, largest_tag]`

### Nanopb descriptor format (this firmware)

- Field info: paired 32-bit words per field
- Type byte `0x57` = callback string (`PB_HTYPE_CALLBACK | PB_LTYPE_STRING`)
- Type byte `0x18` = optional submessage
- Type byte `0x15` = uvarint/fixed32
- `largest_tag = 0` means sequential field numbering from 1
- Callback ptr = `pb_encode_string_cus` for strings; NULL = field not sent

### Finding Socket.IO event names

Each Send* function's literal pool contains the event name string near `"Checking sio_client and locking mutex"` and `"Binary message with X sent"`.

## Key 3.1.6 VMAs

| Address | Function |
|---------|----------|
| `0x00062d74` | `/c/info` JSON and HTTP request builder |
| `0x00063bfc` | `/c/info` dirty/retry service loop |
| `0x0005f42c` | Snapshot capture and HTTP upload |
| `0x0006cf34` | QR/configuration semantic dispatcher |
| `0x00072f08` | Raw quality live-change and optional-persistence handler |
| `0x0007d7c4` | Raw quality value to dimensions |
| `0x000a76c8` | Protobuf quality enum to raw value |
| `0x000a11f4` | Protobuf quality enum to string |
| `0x00097e78` | Interface MAC retrieval and uppercase formatting |
| `0x00096cd8` | Fingerprint seed selection and random fallback |
| `0x00097a4c` | Fingerprint MD5/lowercase-hex encoding |
| `0x000a3058` | Camera authentication sender |
| `0x000a3570` | Protobuf-schema-version sender |
| `0x000a1394` | Camera status construction/sender |
| `0x000a8ed0` | Supported-feature construction/hash/sender |
| `0x000a3e90` | WebRTC answer/candidate encoder and sender |
| `0x000b6d9c` | WebRTC numeric-type translator |
| `0x000b75e0` | Local ICE candidate emission |
| `0x000b94ac` | WebRTC enable/disable mode application |
| `0x000b996c` | WebRTC offer gate and peer-work enqueue |

## Legacy descriptor-table leads

The addresses below were recovered from the 3.1.5 baseline. They are useful for matching structure
and locating shifted tables, but **must not be copied as 3.1.6 tag evidence**. Re-resolve the table
and sender assignment path in the 3.1.6 project. The implementation tracker explicitly names the
messages whose 3.1.6 nested annotations are still prerequisites.

| Address | Message | Fields |
|---------|---------|--------|
| `0x3f5c94` | CameraAuthentication | 2 |
| `0x3f61f8` | ProtobufSchemaVersion | 4 |
| `0x3f5d84` | CameraSupportedFeatures | 6 |
| `0x3f5f58` | ClientTrigger | 6 |
| `0x3f5e98` | CameraInfoMessage | 11 |
| `0x3f601c` | TimelapseFileList | 4 |
| `0x3f65c8` | WebRtcConnectionType | 6 |
| `0x3f6680` | WebRTCMessage | 9 |

## Checked-in Ghidra scripts

All maintained scripts are in `research/ghidra/` and are referenced by filename through
`-scriptPath "$PWD/research/ghidra"`:

| Script | Purpose |
|--------|---------|
| `ExportAllDecomp.java` | Exports every function to one C file plus TSV/summary metadata |
| `DecompileFunctions.java` | Decompiles selected VMAs without writing a corpus |
| `ExportFidHashes.java` | Exports relocation-insensitive Function-ID hashes |
| `ListXrefs.java` | Lists references to addresses and their containing functions |
| `ShowData.java` | Resolves raw words and pointer/string targets at specified addresses |

## Community Reference

https://github.com/tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One

Runs custom scripts on the camera via SD card override (`/mnt/sdcard/lp_app.sh`). Confirmed: config file paths, token source, upload interval units, RTSP mode values, video capture via Rockchip MPI.

## Remaining recovery work

Do not use this older list as a guess queue. The authoritative, current prerequisite list is
[`firmware-implementation-gap-tracker.md` § Areas where decompilation still does not remove all
ambiguity](firmware-implementation-gap-tracker.md#areas-where-decompilation-still-does-not-remove-all-ambiguity).
It currently includes the trigger/configuration descriptors, remaining nested status annotations,
ICE subfields, timelapse-list fields, and `client_trigger` subtype/result enums. Recover those from
the 3.1.6 descriptor and assignment paths before implementing their dependent gaps.
