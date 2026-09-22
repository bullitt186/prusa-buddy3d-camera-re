# Prusa pairing-QR schema (WP-4, AC-21)

This document records the **exact** schema of the current Prusa Connect
"Add WiFi Camera" pairing QR. It contains no real credentials: every value is a
`<PLACEHOLDER>`.

## Provenance

The schema was captured from a QR generated through the current Prusa
"Add WiFi Camera" flow and decoded **locally** on a workstation. The decoded raw
payload and the source image are **not** committed to this repository and are
never logged. Only the redacted field names/types below and a fully synthetic,
non-working fixture ([`tests/fixtures/prusa_qr_synthetic.json`](../tests/fixtures/prusa_qr_synthetic.json))
are kept. Fields must never be guessed or extended from this document alone.

## Encoding

- **Wire format:** UTF-8 JSON object (no BOM, no surrounding whitespace).
- **Captured sample size:** 70 bytes.
- **Version markers:** none. There is no version key, no schema tag, and no
  nested container. Exactly three top-level keys are present.

## Fields

| Key | Type | Meaning | Secret |
|---|---|---|---|
| `ssid` | string | Wi-Fi network name | no (may be displayed) |
| `pwd` | string | Wi-Fi password | **yes** |
| `token` | string | Prusa camera registration token | **yes** |

Exact shape, with placeholders:

```json
{"ssid": "<wifi-ssid>", "pwd": "<wifi-password>", "token": "<prusa-token>"}
```

No other keys are present or accepted.

## Validation rules

The parser (`pi-impersonator/qr_pairing.py`, `parse_payload`) accepts a payload
only when **all** of the following hold:

1. The input is text or bytes; bytes must be valid UTF-8.
2. The whole payload is at most **512 bytes**.
3. It decodes to a JSON **object** (not an array, string, number, boolean, or
   null).
4. Its keys are **exactly** `ssid`, `pwd`, and `token` — an unknown or missing
   key is rejected.
5. Every value is a non-empty **string** of at most **256 characters**.
6. No value contains control characters.

Anything else is ignored as malformed/unrelated. The scanner additionally bounds
image size (2 MiB), decode duration (5 s default), and retry frequency
(`ScanLimiter`, default 5 attempts per 60 s window).

## Redaction rule

`redacted(fields)` returns:

```python
{'ssid': <ssid value or '<redacted>'>, 'pwd': '<redacted>', 'token': '<redacted>'}
```

The SSID may be shown; the password and token are **never** exposed. No token or
password may appear in a parse reason, a log line, or a `repr`.

## Claim-time write rule

A valid QR is **staged** in redacted form by the setup wizard. Its `pwd` and
`token` are written to `secrets.toml` **only** when the wizard commits the
claim. A rejected or expired token returns the user to setup **without**
overwriting the last-known-good credentials.

## Fingerprint-binding semantics

The QR carries **no fingerprint**. The optional explicit fingerprint is a
separate wizard step; when left empty the existing MAC-derived fingerprint
behaviour is preserved. No fingerprint binding is inferred from the QR payload.
