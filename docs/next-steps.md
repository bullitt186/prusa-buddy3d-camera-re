# Next Steps Plan — Prusa Buddy3D Camera Impersonator

**Created:** 2026-07-07  
**Context:** Camera impersonator works (snapshots, Socket.IO auth, `/c/info`, RTSP) but the
mobile app never sends WebRTC offers and shows "Kamera-Kommunikation Fehlgeschlagen".
Two leads are open: (a) a backend **registration/registry gate** on the token
(Steps 1–2), and (b) the **hardware-identity hypothesis** below — that eligibility depends
solely on data the camera itself supplies. Do these in order — each tier's results inform
whether the next is worth attempting.

---

## Priority — Hardware-identity hypothesis (does the camera's own data gate WebRTC?)

**Why this is a lead:** there is no user setting to enable/disable WebRTC, cameras are not tied
to a user account (they can be resold), and pairing is QR-based — so WebRTC eligibility likely
depends only on what the camera *reports about itself* (serial / MAC / HW identity, or a specific
field combination). Today we send a **Pi-OUI MAC**, a **static fingerprint** (not `MD5(MAC)` as
the firmware computes), an **invented HW string** (`NB.1.1.0` / `Pi Zero 2 W`), and **~90
`CameraInfoMessage` fields are un-mapped**. Provenance breakdown: see the "Current deployment
state" table in [`status.md`](status.md). Field-5 sub-field mechanics overlap with Step 5 below.

### P.1 — Map the remaining `CameraInfoMessage` fields (is a serial / HW-id transmitted?)

Cheapest and most decisive — answers "does the camera put its serial on the wire at all?".

- [ ] **P.1.1** Decompile the identity getters and find their callers:
  ```
  mcp__ghidrassist__get_code(0x72534, format=decompiler)   # ReadHwVersionFromCamera
  ```
  Also locate the **serial/OTP getter** (reads `/sys/class/spi_master/spi2/spi2.0/version`,
  see findings §7.2) and any function reading the factory serial.
- [ ] **P.1.2** On the reconstructed struct (`auto_structs/CameraInfoMessage`, encoder at
  `0xa01dc`), run `struct field_xrefs` on each un-named `field_0xNN` writer — flag any that
  call the serial/HW getter. Focus on the field-5 **hardware sub-block** (the "2 ints + string"
  HW block near `field5.2`), the most likely home for a serial.
- [ ] **P.1.3** Record the verdict in [`status.md`](status.md):
  - **Serial IS written to a status field** → a valid factory serial is required; we cannot
    supply one → this *confirms* a hardware-identity gate.
  - **No serial anywhere in the struct** → hardware-ID-in-`status` is ruled out; weight shifts
    to MAC/fingerprint (P.2/P.3) or the token-registry gate (Steps 1–2).

### P.2 — MAC / OUI test (does the backend check the MAC or its vendor prefix?)

- [ ] **P.2.1** Find a genuine Niceboy/Prusa camera **OUI** (IEEE OUI lookup, or the community
  project `tlchandler/Improved-Buddy3D-...`).
- [ ] **P.2.2** Temporarily spoof the Pi's `wlan0` MAC to that OUI:
  ```bash
  sudo ip link set wlan0 down
  sudo ip link set wlan0 address <NICEBOY_OUI>:XX:XX:XX
  sudo ip link set wlan0 up
  ```
- [ ] **P.2.3** Recompute the fingerprint (P.3), update `config.ini`, restart `prusa-cam`, then
  re-run `/c/info` + the viewer-flow test. Note whether viewer ACK `5` or the
  `/v1/cameras/<token>` 404 changes.
  - **Caveat:** the registry gate is keyed on the *token* (origin fixed at creation), so MAC/OUI
    changes may not move it. A negative result here mainly rules an OUI check *out* as an
    *additional* gate; it does not by itself re-register the camera.

### P.3 — Make the fingerprint firmware-faithful (`MD5(MAC)`)

Today `fingerprint` is read verbatim from `config.ini` and is not tied to the MAC we send — a
mismatch could itself fail a check. The firmware computes `MD5(MAC)`
(`lp_fingerprint_generation_tool.cpp`: MAC via `iw dev` → MD5, random fallback).

- [ ] **P.3.1** Compute it instead of reading a static value (in `main.py`):
  ```python
  import hashlib
  mac = open('/sys/class/net/wlan0/address').read().strip()
  fingerprint = hashlib.md5(mac.encode()).hexdigest()
  ```
  Keep the `config.ini` value as an optional override.
- [ ] **P.3.2** **Careful:** if the current token was registered against the *old* fingerprint,
  changing it may re-key/disassociate the camera. Test with a spare token, and verify
  `camera_authentication` still ACKs `1` afterward.

**Order:** P.1 first (decisive, no risk), then P.2 + P.3 as quick empirical tests.

---

## Step 0 — Fix Pi SSH access (prerequisite for everything else)

- [ ] **0.1** Attempt password auth explicitly:
  ```bash
  ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no pi@<PI_IP>
  ```
  _Note: BUILD\_PLAN.md says `pi@<PI_IP>` with no password; try both users._

- [ ] **0.2** If password fails, attempt with explicit key:
  ```bash
  ssh -i ~/.ssh/id_rsa pi@<PI_IP>
  ssh -i ~/.ssh/id_ed25519 pi@<PI_IP>
  ```

- [ ] **0.3** If both fail, connect a keyboard/monitor and reset `authorized_keys` or
  re-enable password auth in `/etc/ssh/sshd_config` (`PasswordAuthentication yes`), then
  `sudo systemctl restart ssh`.

- [ ] **0.4** Verify SSH works end-to-end:
  ```bash
  ssh pi@<PI_IP> "systemctl status prusa-cam.service | head -20"
  ```
  **Pass:** service is `active (running)` and last log line is recent.

---

## Step 1 — PrusaLink printer API (get `origin: LINK`)

The Prusa CORE One runs PrusaLink which exposes a local HTTP API. If it has a camera
registration endpoint, posting our token through it may cause the backend to assign
`origin: LINK` — the only remaining origin path not yet tried.

- [ ] **1.1** Find the printer's local IP address.
  - Check your router's DHCP table, or
  - Check Prusa Connect web UI → printer detail page (usually shows local IP), or
  - `arp -a | grep -i prusa` / `nmap -sn 192.168.0.0/24 | grep -A1 -i prusa`

- [ ] **1.2** Confirm PrusaLink is accessible:
  ```bash
  curl -s http://<PRINTER_IP>/api/version | python3 -m json.tool
  ```
  **Pass:** JSON response with `api`, `server`, `text` keys.

- [ ] **1.3** Check the API root for a cameras or devices section:
  ```bash
  # PrusaLink Swagger UI (if enabled):
  open http://<PRINTER_IP>/api/swagger-ui/
  # Or enumerate common paths:
  for path in cameras camera devices link; do
    echo -n "$path: "
    curl -s -o /dev/null -w "%{http_code}" http://<PRINTER_IP>/api/v1/$path
    echo
  done
  ```

- [ ] **1.4** If a cameras endpoint exists, read the PrusaLink API key from the printer
  (visible in its web UI under Settings → API or via `curl http://<IP>/api/v1/auth`), then
  try registering the camera token:
  ```bash
  # PrusaLink uses X-Api-Key header
  PRINTER_IP=192.168.0.XXX
  API_KEY=<from printer web UI>
  CAM_TOKEN=<from ~/prusa-cam/config.ini on Pi>

  # Attempt 1: register camera via printer
  curl -s -X POST http://$PRINTER_IP/api/v1/cameras \
    -H "X-Api-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"token\": \"$CAM_TOKEN\"}" | python3 -m json.tool

  # Attempt 2: if the endpoint is different
  curl -s -X PUT http://$PRINTER_IP/api/v1/camera \
    -H "X-Api-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"token\": \"$CAM_TOKEN\", \"connected\": true}" | python3 -m json.tool
  ```

- [ ] **1.5** After any successful POST/PUT, check whether `origin` changed:
  ```bash
  # From the Pi (once SSH is fixed):
  curl -s https://webcam.connect.prusa3d.com/c/info \
    -H "Token: $CAM_TOKEN" \
    -H "Fingerprint: $(python3 -c "import hashlib,json; t='$CAM_TOKEN'; print(hashlib.md5(t.encode()).hexdigest()[:16])")" \
    | python3 -m json.tool | grep origin
  ```
  **Pass:** `"origin": "LINK"` (or `"WEB"`). Any change from `"OTHER"` is worth testing.

- [ ] **1.6** If origin changed, restart `prusa-cam.service` on the Pi and open the mobile
  app camera page. Watch Pi logs for incoming Socket.IO events:
  ```bash
  ssh pi@<PI_IP> "journalctl -f -u prusa-cam.service"
  ```
  **Pass:** Logs show an inbound `webrtc` event within 10–15 seconds of opening the app.

---

## Step 2 — mitmproxy on phone (cloud-side interception)

Goal: see what `camera-service-api.prusa3d.com` returns when the app tries to initiate
WebRTC for our camera. This will either confirm origin gating definitively or reveal a
different reason.

- [ ] **2.1** Install mitmproxy on Mac:
  ```bash
  brew install mitmproxy
  ```

- [ ] **2.2** Start mitmproxy in transparent/web mode on the Mac:
  ```bash
  mitmweb --listen-host 0.0.0.0 --listen-port 8080
  # Opens browser at http://localhost:8081 (the web UI)
  ```

- [ ] **2.3** Configure iPhone to proxy through Mac:
  - Settings → Wi-Fi → tap your network → Configure Proxy → Manual
  - Server: `<Mac's LAN IP>` (e.g. `192.168.0.X`), Port: `8080`
  - Leave Authentication off

- [ ] **2.4** Install mitmproxy's root CA on the iPhone:
  - On the phone, navigate to `http://mitm.it` (while proxy is running)
  - Download the iOS certificate and install it
  - Settings → General → VPN & Device Management → install the cert
  - Settings → General → About → Certificate Trust Settings → enable full trust for the cert

- [ ] **2.5** On the phone, open the Prusa Connect app and navigate to the camera page.
  In the mitmproxy web UI (`localhost:8081`), filter for `camera-service-api`:
  - Look for requests to `camera-service-api.prusa3d.com`
  - Note the exact endpoint, HTTP method, request body, and **response body**

- [ ] **2.6** Document the exact error/rejection:
  - If the response is `403` / `"origin not allowed"` or similar → confirms origin gating
  - If the response is a different error → copy the full response JSON here for analysis
  - If there is **no** request to `camera-service-api` at all → the gate is earlier
    (perhaps in the app itself reading a field from the camera record)

- [ ] **2.7** Also capture the camera record the app fetches:
  - Look for requests to `connect.prusa3d.com/api/v1/cameras/<uuid>` or
    `connect-api.prusa3d.com/graphql` with the camera query
  - Copy the full response — particularly any `status`, `origin`, `flags`, or
    `capabilities` fields that differ from what we send

- [ ] **2.8** When done, remove the proxy from iPhone Settings and uninstall the CA cert
  (Settings → General → VPN & Device Management → delete).

---

## Step 3 — Add `@sio.on('webrtc')` handler to `signaling.py`

The impersonator silently drops any inbound `webrtc` Socket.IO event. This step adds
the handler, wires it to `webrtc.py`'s existing peer connection logic, and adds the
outbound SDP-answer emit. Required before any end-to-end WebRTC test can succeed.

### 3a — Log-first stub (deploy immediately, before full implementation)

- [ ] **3a.1** SSH to Pi and edit `~/prusa-cam/signaling.py`. Add this handler alongside
  the other `@sio.on` handlers:
  ```python
  @sio.on('webrtc')
  async def on_webrtc(data):
      log.info(f"INBOUND webrtc event, raw bytes: {data.hex() if isinstance(data, (bytes,bytearray)) else repr(data)}")
      # TODO: decode protobuf and start WebRTC session
  ```
  This ensures we at least see and log any offer that arrives, even before full handling.

- [ ] **3a.2** Restart service and verify no errors:
  ```bash
  sudo systemctl restart prusa-cam.service
  journalctl -f -u prusa-cam.service | grep -E "ERROR|webrtc|INBOUND"
  ```

### 3b — Protobuf decode

- [ ] **3b.1** From the RE work, the inbound `webrtc` payload is a protobuf message
  containing at minimum: request_id, message type, SDP string, and ICE/TURN config.
  Add a decoder to `proto.py`:
  ```python
  def decode_webrtc_message(data: bytes) -> dict:
      """Decode inbound 'webrtc' Socket.IO event payload."""
      fields = {}
      i = 0
      while i < len(data):
          tag, i = read_varint(data, i)
          field_num = tag >> 3
          wire_type = tag & 0x07
          value, i = read_field(data, i, wire_type)
          fields[field_num] = value
      return fields
      # Expected fields (to be confirmed empirically once a real offer arrives):
      # 1: request_id (string)
      # 2: message_type (varint: 1=offer, 2=answer, 3=ice_candidate)
      # 3: sdp (string)
      # 4+: ice/turn config (submessage)
  ```

- [ ] **3b.2** Until we receive a real offer, test the decoder against a synthetic payload:
  ```python
  # In proto.py or a test script:
  test = encode_string(1, "test-req-id") + encode_varint(2, 1) + encode_string(3, "v=0\r\no=...")
  assert decode_webrtc_message(test)[1] == b"test-req-id"
  ```

### 3c — Full handler wiring

- [ ] **3c.1** Import and call the existing WebRTC session logic from `webrtc.py`:
  ```python
  @sio.on('webrtc')
  async def on_webrtc(data):
      payload = decode_webrtc_message(data if isinstance(data, bytes) else bytes(data))
      msg_type = payload.get(2, 0)
      request_id = payload.get(1, b'').decode()
      sdp = payload.get(3, b'').decode()

      log.info(f"webrtc event: type={msg_type} request_id={request_id[:8]}... sdp_len={len(sdp)}")

      if msg_type == 1:  # offer
          answer_sdp = await webrtc_session.handle_offer(sdp, payload)
          # Emit answer back on the same 'webrtc' event
          answer_payload = encode_string(1, request_id) + encode_varint(2, 2) + encode_string(3, answer_sdp)
          await sio.emit('webrtc', answer_payload)
          log.info(f"webrtc answer sent for request_id={request_id[:8]}...")
      elif msg_type == 3:  # ICE candidate
          await webrtc_session.add_ice_candidate(sdp, payload)
      else:
          log.warning(f"webrtc: unhandled message type {msg_type}")
  ```

- [ ] **3c.2** Deploy and restart. With the log-first stub already in place, the first
  real offer will be logged in full before the handler runs — no offer is "consumed"
  silently.

- [ ] **3c.3** End-to-end test (once an offer actually arrives):
  - Open the app camera page
  - Watch for `INBOUND webrtc event` in logs
  - Confirm `webrtc answer sent` follows within 2–3 seconds
  - Confirm `webrtc_connection_info` ICE events appear in logs
  - **Pass:** App shows live video feed

---

## Step 4 — Detailed logging

Add structured, timestamped logging across all three Pi services so any future session
can observe what's happening without blind spots.

### 4a — `signaling.py` logging improvements

- [ ] **4a.1** Replace bare `print()` calls with a proper logger:
  ```python
  import logging, sys
  logging.basicConfig(
      level=logging.DEBUG,
      format='%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s | %(message)s',
      datefmt='%H:%M:%S',
      handlers=[
          logging.StreamHandler(sys.stdout),
          logging.FileHandler('/var/log/prusa-cam/signaling.log', mode='a'),
      ]
  )
  log = logging.getLogger('signaling')
  ```

- [ ] **4a.2** Log every inbound Socket.IO event (not just `webrtc`):
  ```python
  @sio.on('*')
  async def on_any(event, data):
      raw = data.hex() if isinstance(data, (bytes, bytearray)) else repr(data)[:200]
      log.debug(f"SIO IN  [{event}] {len(data) if hasattr(data,'__len__') else '?'}B: {raw}")
  ```
  _Note: `'*'` catch-all works in python-socketio's asyncio client._

- [ ] **4a.3** Log every outbound Socket.IO emit:
  ```python
  async def sio_emit(event, data, **kwargs):
      raw = data.hex() if isinstance(data, (bytes, bytearray)) else repr(data)[:200]
      log.debug(f"SIO OUT [{event}] {len(data) if hasattr(data,'__len__') else '?'}B: {raw}")
      await sio.emit(event, data, **kwargs)
  ```
  Replace all `await sio.emit(...)` calls with `await sio_emit(...)`.

- [ ] **4a.4** Log connection lifecycle events with timestamps:
  ```python
  @sio.event
  async def connect():
      log.info(f"Socket.IO connected to {SIGNALING_URL}")

  @sio.event
  async def disconnect():
      log.warning("Socket.IO disconnected — will reconnect")

  @sio.event
  async def connect_error(data):
      log.error(f"Socket.IO connect error: {data}")
  ```

- [ ] **4a.5** Add shallow protobuf decode to inbound event logs:
  ```python
  def pb_summary(data: bytes) -> str:
      """One-line field summary: {1: 'abc...', 2: <varint 42>, 3: <bytes 18B>}"""
      try:
          fields = {}
          i = 0
          while i < len(data) and len(fields) < 8:
              tag, i = read_varint(data, i)
              fn, wt = tag >> 3, tag & 7
              if wt == 0:
                  v, i = read_varint(data, i); fields[fn] = v
              elif wt == 2:
                  n, i = read_varint(data, i)
                  chunk = data[i:i+n]; i += n
                  try: fields[fn] = f'"{chunk.decode()}"'
                  except: fields[fn] = f'<bytes {n}B>'
              else:
                  break
          return str(fields)
      except Exception as e:
          return f'<parse error: {e}>'
  ```
  Use in the catch-all handler: `log.debug(f"SIO IN [{event}] {pb_summary(data)}")`

### 4b — `main.py` logging improvements

- [ ] **4b.1** Add a `log = logging.getLogger('main')` and log all `/c/snapshot` responses:
  ```python
  log.info(f"snapshot → {resp.status_code} ({len(img_bytes)}B, {elapsed_ms:.0f}ms)")
  ```

- [ ] **4b.2** Log when the snapshot loop is paused (WebRTC or RTSP streaming active):
  ```python
  if streaming or rtsp_streaming():
      log.debug("snapshot loop paused (streaming active)")
      continue
  ```

- [ ] **4b.3** Log `/c/info` upload result:
  ```python
  log.info(f"/c/info → {resp.status_code}, origin={resp.json().get('origin','?')}, registered={resp.json().get('registered','?')}")
  ```

### 4c — Log rotation and access

- [ ] **4c.1** Create log directory on Pi:
  ```bash
  sudo mkdir -p /var/log/prusa-cam
  sudo chown pi:pi /var/log/prusa-cam
  ```

- [ ] **4c.2** Add logrotate config:
  ```bash
  cat << 'EOF' | sudo tee /etc/logrotate.d/prusa-cam
  /var/log/prusa-cam/*.log {
      daily
      rotate 7
      compress
      missingok
      notifempty
      copytruncate
  }
  EOF
  ```

- [ ] **4c.3** Verify logs are being written after restart:
  ```bash
  sudo systemctl restart prusa-cam.service
  sleep 5
  tail -20 /var/log/prusa-cam/signaling.log
  ```
  **Pass:** Timestamped lines including `Socket.IO connected`, `SIO OUT [status]`, etc.

- [ ] **4c.4** Quick log monitor alias (add to `~/.bashrc` on Pi):
  ```bash
  alias camlog='tail -f /var/log/prusa-cam/signaling.log | grep -v "snapshot loop paused"'
  alias camlog-all='tail -f /var/log/prusa-cam/signaling.log'
  alias camevents='tail -f /var/log/prusa-cam/signaling.log | grep -E "SIO IN|webrtc|ERROR|WARN"'
  ```

---

## Step 5 — Deeper `status` message tracing (field 5 sub-fields)

Field 5.11 (`GetWebRtcMode` / `GetWebRtcStatus`) at CameraInfoMessage offset `0x144`
is the most interesting untraced field — if the backend reads it to determine WebRTC
eligibility, we need to send a non-zero "enabled/active" value.

- [ ] **5.1** In Ghidra, decompile `GetWebRtcMode` (string at `0x3fcbeb`) and
  `GetWebRtcStatus` (string at `0x3fcc08`) to understand the return values:
  ```
  mcp__ghidrassist__search_strings("GetWebRtcMode")
  → find address, then get_code(address, format=decompiler)
  ```

- [ ] **5.2** Trace what `SendCameraInfoMessage` writes to offsets `0x144`–`0x154`
  (field 5.11 block). Use `struct field_xrefs` on the CameraInfoMessage struct fields
  at those offsets to find every write site.

- [ ] **5.3** Similarly trace field 5.6 (RTSP mode/status/url, offset `0x0f0`):
  - `GetRtspServerMode`, `GetRtspServerStatus`, `GetRtspServerUrl` — find their return
    values and the protobuf field numbers within the 5.6 sub-message.
  - The impersonator currently sends `{}` for 5.6; it should send at least
    `{1: rtsp_mode, 2: rtsp_status}` with the real enum values.

- [ ] **5.4** Update `signaling.py`'s `send_status()` with confirmed field 5 sub-field
  values. Specifically:
  ```python
  # Field 5.11: WebRTC mode + status
  # mode: 1=enabled, 0=disabled (TBD from GetWebRtcMode decompile)
  # status: 1=running, 0=stopped (TBD)
  field_5_11 = encode_varint(1, 1) + encode_varint(2, 1)  # enabled + running

  # Field 5.6: RTSP mode + status + url
  field_5_6 = (encode_varint(1, 1) +           # mode: enabled
               encode_varint(2, 1) +            # status: running
               encode_string(3, RTSP_URL))      # url: rtsp://<PI_IP>:8554/live
  ```

- [ ] **5.5** Deploy, restart, and retest with mobile app:
  ```bash
  sudo systemctl restart prusa-cam.service
  journalctl -f -u prusa-cam.service | grep -E "status|webrtc|INBOUND"
  ```
  Note status message byte count before/after (it should increase).

---

## Step 6 — MQTT topic investigation

The Prusa Connect app subscribes to `wss://mqtt.prusa3d.com:8084/mqtt` for real-time
camera status. Understanding the topic/message structure may reveal status fields the
backend publishes that gate WebRTC in the app.

- [ ] **6.1** Find the MQTT topics by capturing the app's MQTT traffic via mitmproxy
  (same setup as Step 2, but filter for `mqtt.prusa3d.com`).
  WebSocket MQTT traffic: look for `CONNECT` and `SUBSCRIBE` frames in the WS stream.

- [ ] **6.2** Alternatively, try connecting to the broker directly with the user's JWT
  token as the MQTT password (MQTT over WebSocket typically uses username/password from
  the app's auth):
  ```bash
  pip3 install paho-mqtt
  python3 - << 'EOF'
  import paho.mqtt.client as mqtt, ssl

  def on_connect(c, ud, flags, rc):
      print(f"Connected: {rc}")
      c.subscribe("#")  # wildcard — will likely be rejected, but shows error

  def on_message(c, ud, msg):
      print(f"TOPIC: {msg.topic}")
      print(f"PAYLOAD: {msg.payload[:200]}")

  c = mqtt.Client(transport="websockets")
  c.tls_set()
  c.username_pw_set(username="<YOUR_EMAIL>", password="<JWT_TOKEN>")
  c.on_connect = on_connect
  c.on_message = on_message
  c.connect("mqtt.prusa3d.com", 8084)
  c.loop_forever()
  EOF
  ```
  _The JWT token is obtainable from the browser session (check localStorage or the
  Authorization header in any authenticated API call captured by mitmproxy in Step 2)._

- [ ] **6.3** Once connected, subscribe to camera-specific topics:
  ```python
  # Common MQTT topic patterns for IoT camera platforms:
  c.subscribe(f"camera/{CAMERA_UUID}/#")
  c.subscribe(f"device/{CAMERA_UUID}/#")
  c.subscribe(f"v1/devices/{CAMERA_UUID}/#")
  ```

- [ ] **6.4** With the real camera app open, trigger the "camera failed" state and note:
  - What topics receive messages when the state changes
  - What the message payload contains (likely protobuf or JSON)
  - Whether there is a `webrtc_ready`, `status`, or `connection` topic that changes

- [ ] **6.5** If a camera status topic is found, decode its content and compare against
  what the impersonator currently sends in the Socket.IO `status` event. Any discrepancy
  is a candidate for the next fix.

---

## Summary / dependency graph

```
Step 0 (SSH fix) ──────────────────────────────────┐
                                                    │
Step 1 (PrusaLink / origin: LINK) ─────────────────┤
        │                                           │
        └─ If origin changes → test immediately    │
                                                    ▼
Step 2 (mitmproxy) ─────────────────────── reveals exact gate
        │
        └─ If origin confirmed as gate → done researching,
           deploy Step 3 and wait for real hardware access
        │
        └─ If different error → use response to guide Step 5

Step 4 (logging) ─── deploy in parallel with any of the above,
                      prerequisite for interpreting all test results

Step 3 (webrtc handler) ─── needed before any end-to-end WebRTC test
Step 5 (status fields) ──── low priority unless Step 2 reveals a status field as the gate
Step 6 (MQTT) ──────────── low priority unless Step 2 shows app reads MQTT before offering
```

**Minimum useful state:** Steps 0 + 4 deployed, Step 2 done. From there you know whether
any more software work can help or whether you need real hardware.
