struct CameraInfoMessage {
  byte field_0x000[36]; // 0x000
  byte has_field2_timelapse_status; // 0x024
  byte gap1_0x25[3];
  uint field_0x028; // 0x028
  uint field_0x02c; // 0x02c
  uint field_0x030; // 0x030
  uint field_0x034; // 0x034
  uint field_0x038; // 0x038
  uint field_0x03c; // 0x03c
  float field_0x040; // 0x040
  byte gap2_0x44[4];
  byte has_field3_camera_status; // 0x048
  byte gap3_0x49[19];
  uint ir_mode; // 0x05c
  int field_0x060; // 0x060
  uint field_0x064; // 0x064
  uint speaker_volume; // 0x068
  byte has_field4_network; // 0x06c
  byte gap4_0x6d[3];
  byte has_network_current; // 0x070
  byte gap5_0x71[3];
  uint field_0x074; // 0x074
  uint field_0x078; // 0x078
  uint field_0x07c; // 0x07c
  uint field_0x080; // 0x080
  uint field_0x084; // 0x084
  uint field_0x088; // 0x088
  byte gap6_0x8c[8];
  uint field_0x094; // 0x094
  byte gap7_0x98[28];
  byte has_field5_extended_status; // 0x0b4
  byte gap8_0xb5[3];
  // 0x0b8-0x0cc: three (mode=const, value) pairs, traced 2026-07-09 (see protocol.md
  // "extended_status" section). 0xc4 is a hardware-derived model-name string
  // ("Buddy3D-C1" etc, from the real SPI HW-version chip via a range-table lookup);
  // 0xbc is a compile-time literal string; 0xcc reads a different, untraced singleton.
  uint field5_1_mode; // 0x0b8 -- constant (DAT_000a0ce4)
  uint field5_1_value; // 0x0bc -- FUN_0003726c(): compile-time literal string
  uint field5_2_mode; // 0x0c0 -- constant (DAT_000a0ce4)
  uint field5_2_model_name; // 0x0c4 -- HW-version-derived model string, see above
  uint field5_3_mode; // 0x0c8 -- constant (DAT_000a0ce4)
  uint field5_3_value; // 0x0cc -- untraced second singleton, +0x2c string field
  byte has_field5_video_mode; // 0x0d0
  byte gap9_0xd1[3];
  uint field_0x0d4; // 0x0d4
  uint field_0x0d8; // 0x0d8
  uint field_0x0dc; // 0x0dc
  uint field_0x0e0; // 0x0e0
  uint field_0x0e4; // 0x0e4
  uint field_0x0e8; // 0x0e8
  byte gap10_0xec[4];
  byte has_field5_rtsp; // 0x0f0
  byte gap11_0xf1[3];
  uint field_0x0f4; // 0x0f4
  uint field_0x0f8; // 0x0f8
  uint field_0x0fc; // 0x0fc
  uint field_0x100; // 0x100
  byte has_field5_log_level; // 0x104
  byte gap12_0x105[3];
  uint field_0x108; // 0x108
  uint field_0x10c; // 0x10c
  byte gap13_0x110[8];
  byte has_field5_services; // 0x118
  byte gap14_0x119[3];
  uint field_0x11c; // 0x11c
  uint field_0x120; // 0x120
  uint field_0x124; // 0x124
  uint field_0x128; // 0x128
  uint field_0x12c; // 0x12c
  uint field_0x130; // 0x130
  byte has_field5_timezone; // 0x134
  byte gap15_0x135[3];
  uint field_0x138; // 0x138
  uint field_0x13c; // 0x13c
  uint field_0x140; // 0x140
  byte has_field5_webrtc; // 0x144
  byte gap16_0x145[3];
  uint field_0x148; // 0x148
  uint field_0x14c; // 0x14c
  byte gap17_0x150[24];
  uint token_callback; // 0x168
  uint token_string; // 0x16c
  byte has_field9_system_info; // 0x170
  byte gap18_0x171[7];
  uint field_0x178; // 0x178
  byte gap19_0x17c[4];
  ulonglong field_0x180; // 0x180
  uint field_0x188; // 0x188
  uint field_0x18c; // 0x18c
  uint field_0x190; // 0x190
  uint field_0x194; // 0x194
  uint field_0x198; // 0x198
  uint field_0x19c; // 0x19c
  uint field_0x1a0; // 0x1a0
  uint field_0x1a4; // 0x1a4
  uint field_0x1a8; // 0x1a8
  uint field_0x1ac; // 0x1ac
  uint field_0x1b0; // 0x1b0
  uint field_0x1b4; // 0x1b4
  uint field_0x1b8; // 0x1b8
  byte gap20_0x1bc[4];
  uint field_0x1c0; // 0x1c0
  int field_0x1c4; // 0x1c4
  byte has_video_quality; // 0x1c8
  byte gap21_0x1c9[3];
  uint video_quality; // 0x1cc
};
