vars = [
("auStack_3e0", 0x3e0, 36, "byte"),
("local_3bc", 0x3bc, 1, "byte"),
("local_3b8", 0x3b8, 4, "uint"),
("local_3b4", 0x3b4, 4, "uint"),
("local_3b0", 0x3b0, 4, "uint"),
("local_3ac", 0x3ac, 4, "uint"),
("local_3a8", 0x3a8, 4, "uint"),
("local_3a4", 0x3a4, 4, "uint"),
("local_3a0", 0x3a0, 4, "float"),
("local_398", 0x398, 1, "byte"),
("local_384", 0x384, 4, "uint"),
("local_380", 0x380, 4, "int"),
("local_37c", 0x37c, 4, "uint"),
("local_378", 0x378, 4, "uint"),
("local_374", 0x374, 1, "byte"),
("local_370", 0x370, 1, "byte"),
("local_36c", 0x36c, 4, "uint"),
("local_368", 0x368, 4, "uint"),
("local_364", 0x364, 4, "uint"),
("local_360", 0x360, 4, "uint"),
("local_35c", 0x35c, 4, "uint"),
("local_358", 0x358, 4, "uint"),
("local_34c", 0x34c, 4, "uint"),
("local_32c", 0x32c, 1, "byte"),
("local_328", 0x328, 4, "uint"),
("local_324", 0x324, 4, "uint"),
("local_320", 0x320, 4, "uint"),
("local_31c", 0x31c, 4, "uint"),
("local_318", 0x318, 4, "uint"),
("local_314", 0x314, 4, "uint"),
("local_310", 0x310, 1, "byte"),
("local_30c", 0x30c, 4, "uint"),
("local_308", 0x308, 4, "uint"),
("local_304", 0x304, 4, "uint"),
("local_300", 0x300, 4, "uint"),
("local_2fc", 0x2fc, 4, "uint"),
("local_2f8", 0x2f8, 4, "uint"),
("local_2f0", 0x2f0, 1, "byte"),
("local_2ec", 0x2ec, 4, "uint"),
("local_2e8", 0x2e8, 4, "uint"),
("local_2e4", 0x2e4, 4, "uint"),
("local_2e0", 0x2e0, 4, "uint"),
("local_2dc", 0x2dc, 1, "byte"),
("local_2d8", 0x2d8, 4, "uint"),
("local_2d4", 0x2d4, 4, "uint"),
("local_2c8", 0x2c8, 1, "byte"),
("local_2c4", 0x2c4, 4, "uint"),
("local_2c0", 0x2c0, 4, "uint"),
("local_2bc", 0x2bc, 4, "uint"),
("local_2b8", 0x2b8, 4, "uint"),
("local_2b4", 0x2b4, 4, "uint"),
("local_2b0", 0x2b0, 4, "uint"),
("local_2ac", 0x2ac, 1, "byte"),
("local_2a8", 0x2a8, 4, "uint"),
("local_2a4", 0x2a4, 4, "uint"),
("local_2a0", 0x2a0, 4, "uint"),
("local_29c", 0x29c, 1, "byte"),
("local_298", 0x298, 4, "uint"),
("local_294", 0x294, 4, "uint"),
("local_278", 0x278, 4, "uint"),
("local_274", 0x274, 4, "uint"),
("local_270", 0x270, 1, "byte"),
("local_268", 0x268, 4, "uint"),
("local_260", 0x260, 8, "ulonglong"),
("local_258", 0x258, 4, "uint"),
("local_254", 0x254, 4, "uint"),
("local_250", 0x250, 4, "uint"),
("local_24c", 0x24c, 4, "uint"),
("local_248", 0x248, 4, "uint"),
("local_244", 0x244, 4, "uint"),
("local_240", 0x240, 4, "uint"),
("local_23c", 0x23c, 4, "uint"),
("local_238", 0x238, 4, "uint"),
("local_234", 0x234, 4, "uint"),
("local_230", 0x230, 4, "uint"),
("local_22c", 0x22c, 4, "uint"),
("local_228", 0x228, 4, "uint"),
("local_220", 0x220, 4, "uint"),
("local_21c", 0x21c, 4, "int"),
("local_218", 0x218, 1, "byte"),
("local_214", 0x214, 4, "uint"),
]

base = 0x3e0
prev_end = 0
lines = []
gapnum = 0
for name, stackoff, size, typ in vars:
    off = base - stackoff
    if off != prev_end:
        gap = off - prev_end
        gapnum += 1
        lines.append(f"  byte gap{gapnum}_{prev_end:#x}[{gap}]; // offset {prev_end:#x}-{off:#x}")
    lines.append(f"  {typ} {name}; // offset {off:#x} size {size}")
    prev_end = off + size

total = base  # 0x1d0... wait check
print(f"// total buffer size expected 0x1d0 = {0x1d0}, computed end = {prev_end:#x} ({prev_end})")
print("struct CameraInfoMessage {")
for l in lines:
    print(l)
print("};")

# Second pass: emit with known semantic names + gaps, for actual Ghidra struct create
known_names = {
    0x24: "has_field2_timelapse_status", # top-level field 2 has flag
    0x48: "has_field3_camera_status", # top-level field 3 has flag
    0x5c: "ir_mode",        # setIrMode, confirmed
    0x68: "speaker_volume", # config.volume, confirmed
    0x6c: "has_field4_network",      # top-level field 4 has flag
    0x70: "has_network_current",      # field4.1 has flag
    0xb4: "has_field5_extended_status", # top-level field 5 has flag
    0xd0: "has_field5_video_mode", # field5.4 has flag
    0xf0: "has_field5_rtsp", # field5.6 has flag
    0x104: "has_field5_log_level", # field5.7 has flag
    0x118: "has_field5_services", # field5.9 has flag
    0x134: "has_field5_timezone", # field5.10 has flag
    0x144: "has_field5_webrtc", # field5.11 has flag
    0x168: "token_callback", # field 8 callback, confirmed http.token
    0x16c: "token_string",   # field 8 value, confirmed http.token
    0x170: "has_field9_system_info", # top-level field 9 has flag
    0x1c8: "has_video_quality", # field 11 has flag
    0x1cc: "video_quality", # FUN_0009d13c, confirmed (1=SD 2=HD 3=FHD)
}
print("\n\n===== FINAL C DEFINITION =====")
lines2 = []
prev_end = 0
gapnum = 0
for name, stackoff, size, typ in vars:
    off = base - stackoff
    if off != prev_end:
        gap = off - prev_end
        gapnum += 1
        lines2.append(f"  byte gap{gapnum}_0x{prev_end:x}[{gap}];")
    fname = known_names.get(off, f"field_0x{off:03x}")
    natural_size = {"byte":1,"uint":4,"int":4,"float":4,"ulonglong":8}[typ]
    if size != natural_size:
        lines2.append(f"  {typ} {fname}[{size}]; // 0x{off:03x}")
    else:
        lines2.append(f"  {typ} {fname}; // 0x{off:03x}")
    prev_end = off + size
print("struct CameraInfoMessage {")
for l in lines2:
    print(l)
print("};")
