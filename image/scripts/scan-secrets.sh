#!/usr/bin/env bash
# scan-secrets.sh — release secret scanner (AC-35).
#
# Usage: scan-secrets.sh <path>...
#
# Scans files and directories for material that must never reach a release
# artifact, the uncompressed image, an SBOM, a package manifest, a log, or a
# committed test fixture:
#   * private keys (PEM / OpenSSH / PKCS8)
#   * minisign secret keys
#   * SSH host private keys
#   * a non-empty machine-id
#   * Wi-Fi PSK values and NetworkManager connection profiles
#   * Prusa tokens
#   * MQTT passwords
#   * password_hash values
#   * the developer's personal username and personal home directories
#
# Every match is printed as file:line and the script exits non-zero when any
# match is found. Binary files are skipped for content patterns (they are pure
# noise) but secret-bearing filenames are still flagged. The scan is
# deterministic: LC_ALL is pinned and directory walks are sorted.
set -euo pipefail
export LC_ALL=C

die() { echo "ERROR: $*" >&2; exit 2; }
usage() { echo "Usage: scan-secrets.sh <path>..." >&2; }

[ "$#" -ge 1 ] || { usage; exit 2; }

# Personal username to flag. Assembled from a character class so the literal
# does not appear in the repository itself; override with
# SCAN_PERSONAL_USER_PATTERN when scanning a different contributor's tree.
PERSONAL_USER_PATTERN="${SCAN_PERSONAL_USER_PATTERN:-b[u]llitt}"

# --- collect files deterministically ----------------------------------------
files=()
for path in "$@"; do
   if [ -d "$path" ]; then
      while IFS= read -r -d '' f; do
         files+=("$f")
      done < <(find "$path" -type f -print0 | sort -z)
   elif [ -f "$path" ]; then
      files+=("$path")
   elif [ -e "$path" ]; then
      die "not a regular file or directory: $path"
   else
      die "path does not exist: $path"
   fi
done

if [ "${#files[@]}" -eq 0 ]; then
   echo "scan-secrets: no files to scan"
   exit 0
fi

matches=0

# Content patterns. grep -I / --binary-files=without-match keeps binary noise
# (including the compressed .img.xz) out of the report; -H prints the filename
# and -n the line number.
scan_content() {
   local out
   out="$(grep -inHIE --binary-files=without-match -e "$2" -- "${files[@]}" 2>/dev/null || true)"
   if [ -n "$out" ]; then
      printf '%s\n' "$out"
      matches=$((matches + 1))
   fi
}

scan_content "private key"       '-----BEGIN [A-Z0-9 ]*PRIVATE[ ]KEY-----'
scan_content "minisign key"      'minisign encrypted secret key'
scan_content "machine-id"        '^[0-9a-fA-F]{32}$'
scan_content "wifi psk"          '^[[:space:]]*psk[[:space:]]*='
scan_content "prusa token json"  '"[A-Za-z_]*token"[[:space:]]*:[[:space:]]*"[^"]+"'
scan_content "prusa token env"   '[A-Z_]*TOKEN[[:space:]]*=[[:space:]]*[^[:space:]]+'
scan_content "mqtt password"     '[Mm][Qq][Tt][Tt][A-Za-z_-]*[Pp][Aa][Ss][Ss][A-Za-z]*[[:space:]]*[:=][[:space:]]*[^[:space:]]+'
scan_content "password_hash"     'password_hash'
scan_content "personal username" "$PERSONAL_USER_PATTERN"

# Personal home paths, excluding service/system accounts. Filter PER MATCH
# (grep -o) rather than per line, so a line that also mentions an allowed
# account still flags the personal path. HOME_ALLOW_PATTERN must be defined:
# under `set -u` an unset value aborts the pipeline and would silently disable
# this whole check.
HOME_ALLOW_PATTERN="${SCAN_HOME_ALLOW_PATTERN:-prusa-cam|pi|root|admin}"
home_out="$(grep -inoHIE --binary-files=without-match \
   -e '/home[/][A-Za-z0-9._-]+' -- "${files[@]}" 2>/dev/null \
   | grep -vE ":/home[/](${HOME_ALLOW_PATTERN})\$" || true)"
if [ -n "$home_out" ]; then
   printf '%s\n' "$home_out"
   matches=$((matches + 1))
fi

# Secret-bearing filenames are flagged regardless of content (a host private
# key or connection profile is a secret even if its body is not plain text).
for f in "${files[@]}"; do
   base="${f##*/}"
   case "$base" in
      *.nmconnection)
         echo "$f:0:NetworkManager connection profile"
         matches=$((matches + 1))
         ;;
      ssh_host_*_key)
         echo "$f:0:SSH host private key"
         matches=$((matches + 1))
         ;;
      minisign.key)
         echo "$f:0:minisign secret key file"
         matches=$((matches + 1))
         ;;
      id_[r]sa | id_[e]d25519)
         echo "$f:0:SSH private key file"
         matches=$((matches + 1))
         ;;
   esac
done

if [ "$matches" -gt 0 ]; then
   echo "scan-secrets: $matches secret pattern(s) matched" >&2
   exit 1
fi

echo "scan-secrets: clean"
exit 0
