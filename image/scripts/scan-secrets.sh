#!/usr/bin/env bash
# scan-secrets.sh — release secret scanner (AC-35).
#
# Usage: scan-secrets.sh <path>...
#        scan-secrets.sh --source-tree <path>...
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
# The default (artifact) mode scans every pattern above. The optional
# ``--source-tree`` mode scans only the high-signal, low-false-positive classes
# (private keys, minisign secret keys, machine-id, secret-bearing filenames and
# personal username/home paths) so an application source tree can be gated
# without flagging ordinary code that merely assigns a runtime token or an empty
# Wi-Fi PSK variable. The full token/PSK/password patterns are only meaningful on
# built artifacts, where those code shapes do not appear.
#
# Every match is printed as file:line and the script exits non-zero when any
# match is found. Binary files are skipped for content patterns (they are pure
# noise) but secret-bearing filenames are still flagged. The scan is
# deterministic: LC_ALL is pinned and directory walks are sorted.
set -euo pipefail
export LC_ALL=C

die() { echo "ERROR: $*" >&2; exit 2; }
usage() {
   echo "Usage: scan-secrets.sh [--source-tree] <path>..." >&2
}

MODE="artifacts"
paths=()
while [ "$#" -gt 0 ]; do
   case "$1" in
      --source-tree) MODE="source"; shift ;;
      -h | --help) usage; exit 0 ;;
      --) shift; while [ "$#" -gt 0 ]; do paths+=("$1"); shift; done ;;
      *) paths+=("$1"); shift ;;
   esac
done

[ "${#paths[@]}" -ge 1 ] || { usage; exit 2; }

# Personal username to flag. Assembled from a character class so the literal
# does not appear in the repository itself; override with
# SCAN_PERSONAL_USER_PATTERN when scanning a different contributor's tree.
PERSONAL_USER_PATTERN="${SCAN_PERSONAL_USER_PATTERN:-b[u]llitt}"

# --- collect files deterministically ----------------------------------------
# Fail closed: an unreadable path is a match, and a failed directory walk is a
# hard error rather than a silent "clean" result.
matches=0
files=()
ENUM_TMP="$(mktemp "${TMPDIR:-/tmp}/scan-secrets-enum.XXXXXX")"
trap 'rm -f "$ENUM_TMP"' EXIT

for path in "${paths[@]}"; do
   if [ -d "$path" ]; then
      if [ ! -r "$path" ]; then
         echo "$path:0:unreadable path"
         matches=$((matches + 1))
         continue
      fi
      if ! find "$path" -type f -print0 | sort -z > "$ENUM_TMP"; then
         die "failed to enumerate files under: $path"
      fi
      while IFS= read -r -d '' f; do
         files+=("$f")
      done < "$ENUM_TMP"
   elif [ -f "$path" ]; then
      if [ ! -r "$path" ]; then
         echo "$path:0:unreadable path"
         matches=$((matches + 1))
         continue
      fi
      files+=("$path")
   elif [ -e "$path" ]; then
      die "not a regular file or directory: $path"
   else
      die "path does not exist: $path"
   fi
done

if [ "${#files[@]}" -eq 0 ]; then
   if [ "$matches" -gt 0 ]; then
      echo "scan-secrets: $matches secret pattern(s) matched" >&2
      exit 1
   fi
   echo "scan-secrets: no files to scan"
   exit 0
fi

# Content patterns. grep -I / --binary-files=without-match keeps binary noise
# (including the compressed .img.xz) out of the report; -H prints the filename
# and -n the line number. A grep error (exit >= 2, e.g. an unreadable file) is
# treated as a match so the scan can never fail open.
scan_content() {
   local out rc=0
   out="$(grep -inHIE --binary-files=without-match -e "$2" -- "${files[@]}" 2>/dev/null)" || rc=$?
   if [ "$rc" -ge 2 ]; then
      echo "scan-secrets: content scan '$1' failed (grep exit $rc)" >&2
      matches=$((matches + 1))
      return
   fi
   if [ -n "$out" ]; then
      printf '%s\n' "$out"
      matches=$((matches + 1))
   fi
}

# Personal home paths, excluding service/system accounts. Filter PER MATCH
# (grep -o) rather than per line, so a line that also mentions an allowed
# account still flags the personal path. HOME_ALLOW_PATTERN must be defined:
# under `set -u` an unset value aborts the pipeline and would silently disable
# this whole check. A grep error in the pipeline is treated as a match.
scan_home_paths() {
   local home_out rc=0
   HOME_ALLOW_PATTERN="${SCAN_HOME_ALLOW_PATTERN:-prusa-cam|pi|root|admin}"
   home_out="$(grep -inoHIE --binary-files=without-match \
      -e '/home[/][A-Za-z0-9._-]+' -- "${files[@]}" 2>/dev/null \
      | grep -vE ":/home[/](${HOME_ALLOW_PATTERN})\$")" || rc=$?
   if [ "$rc" -ge 2 ]; then
      echo "scan-secrets: home-path scan failed (grep exit $rc)" >&2
      matches=$((matches + 1))
      return
   fi
   if [ -n "$home_out" ]; then
      printf '%s\n' "$home_out"
      matches=$((matches + 1))
   fi
}

# Secret-bearing filenames are flagged regardless of content (a host private
# key or connection profile is a secret even if its body is not plain text).
scan_filenames() {
   local f base
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
}

# High-signal content patterns shared by both modes.
scan_content "private key"       '-----BEGIN [A-Z0-9 ]*PRIVATE[ ]KEY-----'
scan_content "minisign key"      'minisign encrypted secret key'
scan_content "machine-id"        '^[0-9a-fA-F]{32}$'

if [ "$MODE" != "source" ]; then
   # Broad patterns that only make sense on built artifacts.
   scan_content "wifi psk"          '^[[:space:]]*psk[[:space:]]*='
   scan_content "prusa token json"  '"[A-Za-z_]*token"[[:space:]]*:[[:space:]]*"[^"]+"'
   scan_content "prusa token env"   '[A-Z_]*TOKEN[[:space:]]*=[[:space:]]*[^[:space:]]+'
   scan_content "mqtt password"     '[Mm][Qq][Tt][Tt][A-Za-z_-]*[Pp][Aa][Ss][Ss][A-Za-z]*[[:space:]]*[:=][[:space:]]*[^[:space:]]+'
   scan_content "password_hash"     'password_hash'
fi
scan_content "personal username" "$PERSONAL_USER_PATTERN"

scan_home_paths
scan_filenames

if [ "$matches" -gt 0 ]; then
   echo "scan-secrets: $matches secret pattern(s) matched" >&2
   exit 1
fi

echo "scan-secrets: clean"
exit 0
