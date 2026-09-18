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

## Key VMAs

| Address | Function |
|---------|----------|
| `0x0009c294` | `pb_encode_string_cus` (nanopb callback encoder) |
| `0x0009a938` | `pb_encode_tag` (tag encoder with type jump table) |
| `0x0009a9c4` | `pb_encode_string` (writes varint len + bytes) |
| `0x0009b0cc` | `pb_encode` (main encode entry, takes stream + descriptor + struct) |
| `0x0009a648` | `pb_ostream_from_buffer` (init output stream) |
| `0x000a1ea0` | `SendAuthMessage` |
| `0x000a7d18` | `SendCameraSupportedFeatures` |
| `0x000a23b8` | `SendProtobufSchemaVersion` |
| `0x000a322c` | WebRTC incoming message parser |
| `0x000b87b4` | `ProcessingMessage` (WebRTC message dispatch) |
| `0x00061bbc` | `do_update_camera_attr` (JSON builder for /c/info) |
| `0x0009ea50` | Feature list string builder |
| `0x000a6510` | `SetVideoQualityFromProtobuf` |
| `0x000a003c` | `TranslateVideoProtobufToString` |
| `0x0005e274` | HTTP snapshot upload function |

## Descriptor Table VMAs

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

## Ghidra Scripts Used

All in `/tmp/`, referenced by filename:

| Script | Purpose |
|--------|---------|
| `DumpDescriptors.java` | Dumps nanopb field descriptor tables from known rodata addresses |
| `DecompileSenders.java` | Finds Send* functions via literal pool xrefs |
| `ForceDecompile.java` | Creates functions at discovered addresses and decompiles |
| `ExtractRemaining.java` | Decompiles ProcessingMessage, features builder, emit helpers |
| `ExtractFinal.java` | Incoming events, HTTP upload, video quality enum, timers |
| `ExtractLast.java` | `do_update_camera_attr` (JSON) and feature list assembly |

## Community Reference

https://github.com/tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One

Runs custom scripts on the camera via SD card override (`/mnt/sdcard/lp_app.sh`). Confirmed: config file paths, token source, upload interval units, RTSP mode values, video capture via Rockchip MPI.

## What Remains Unknown

- Exact content of `"options"` and `"capabilities"` JSON fields (likely empty arrays work)
- Whether `"features"` goes into protobuf field 1 or another field in the 6-field message
- Exact sub-message field assignments for CameraInfoMessage fields 1-6

All resolvable by trial against the live server.
