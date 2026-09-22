# Release signing keys

- `buddy3d-release.pub` — the minisign **public** key. Committed. It is embedded
  in the image and used by the update verifier (WP-6) and release automation
  (WP-7) to verify signed releases and manifests.
- The matching **secret** key is NOT in this repository. It lives only in the
  release environment. The development key is at
  `~/.config/buddy3d/release-signing.key` (mode 0600). Never commit it, log it,
  copy it into an image, or place it on a device.

Public key ID: `48680BE111FEB8E3`

```sh
# sign (release environment only)
minisign -S -s ~/.config/buddy3d/release-signing.key -m <file>
# verify
minisign -V -p image/keys/buddy3d-release.pub -m <file>
```

Before a public stable release, replace this development key with the
owner-held key and re-sign; only the public half changes in the repository.
