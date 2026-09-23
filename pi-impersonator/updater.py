"""Signed application update core (WP-R4a; master AC-29/AC-30/AC-32).

This module is the trust boundary for the appliance's signed application
updates (distribution plan §7). It implements three independent, host-testable
pieces and nothing that touches the running system:

1. **Manifest validation** (:func:`parse_manifest`, :func:`parse_semver`,
   :func:`is_newer`, :func:`classify_manifest`): the signed
   ``update-manifest.json`` is decoded and strictly validated. Unknown schema
   versions, invalid SemVer, unknown channels, downgrades/equal versions,
   incompatible base images, oversize bundles, non-``https`` URLs, malformed
   SHA-256 values, non-boolean reboot flags, and absent/over-long summary/URL
   fields are all rejected before any download (AC-29).
2. **Minisign verification** (:func:`verify_file`): a thin, injectable wrapper
   around the ``minisign`` CLI. Missing signature, public key or binary is a
   bounded failure, never an exception. The public key is *not* secret; no
   secret ever appears in a returned reason.
3. **Archive safety + extraction** (:func:`validate_archive_members`,
   :func:`extract_bundle`): every ``tar`` member is checked for absolute
   paths, ``..`` traversal, devices/FIFOs, links escaping the destination,
   setuid/setgid bits, foreign owners and per-file/total size caps *before*
   extraction. Extraction uses :mod:`tarfile` with the stdlib ``data`` filter,
   so links can never be followed out of ``dest`` (AC-29, AC-32).

Scope boundary (AC-32): this module replaces only project Python code, static
web assets and pinned pure-Python wheels. It never replaces the kernel, boot
firmware, partition table, systemd base units, Debian/GStreamer/libcamera
packages or the signing public key; those require a new image.

Stdlib only, import-safe: importing this module runs no command, reads no file
and performs no network I/O. Every external command (``minisign``, ``zstd``)
goes through an injectable ``runner(args, timeout)`` callable, mirroring
:mod:`boot_mode`/:mod:`ssh_control`, so the tests never need the real binaries.
``zstd`` is shelled out because Python 3.12/3.13 have no stdlib zstd codec.
"""
import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.parse

log = logging.getLogger('prusa-cam.updater')

# --------------------------------------------------------------------------- #
# Documented limits (the "configured" caps the plan refers to)
# --------------------------------------------------------------------------- #

#: Manifest schema versions this build understands. An unknown value is a hard
#: reject (never best-effort parse).
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: Release channels. v1 ships ``stable``; ``alpha`` is pre-release only.
CHANNELS = ('stable', 'alpha')

#: Default in-image minisign public key (embedded by WP-R4b). Not secret.
DEFAULT_PUBLIC_KEY_PATH = '/usr/share/prusa-buddy3d-camera/buddy3d-release.pub'

#: Default signature suffix when ``signature_path`` is not supplied.
DEFAULT_SIGNATURE_SUFFIX = '.minisig'

#: Binaries invoked by the default runners.
MINISIGN_BINARY = 'minisign'
ZSTD_BINARY = 'zstd'

#: Bounded wall-clock timeouts for every external command.
MINISIGN_TIMEOUT_SECONDS = 30.0
ZSTD_TIMEOUT_SECONDS = 120.0

#: Maximum compressed bundle size accepted from the manifest (256 MiB). The
#: application payload is Python code, web assets and pure-Python wheels; a
#: larger bundle is rejected before download.
MAX_BUNDLE_SIZE = 256 * 1024 * 1024

#: Maximum single extracted member size (64 MiB).
MAX_MEMBER_SIZE = 64 * 1024 * 1024

#: Maximum total extracted size across all members (512 MiB).
MAX_TOTAL_EXTRACTED_SIZE = 512 * 1024 * 1024

#: Maximum size of the decompressed tar (cap on the zstd output; slightly above
#: the extracted total to allow for tar headers/padding).
MAX_DECOMPRESSED_SIZE = 640 * 1024 * 1024

#: Maximum number of members accepted in one bundle.
MAX_ARCHIVE_MEMBERS = 100000

#: Bound on the raw manifest payload read/parsed.
MAX_MANIFEST_BYTES = 64 * 1024

#: Field bounds.
MAX_VERSION_LENGTH = 64
MAX_COMMIT_LENGTH = 128
MAX_SUMMARY_LENGTH = 2000
MAX_URL_LENGTH = 2048

#: Bound a returned reason so it cannot bloat a response/log (and can never
#: carry a secret, because the module only ever emits fixed phrases).
MAX_REASON_LENGTH = 200

_SEMVER_RE = re.compile(r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')
_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]+')


class ManifestError(ValueError):
    """A manifest failed validation with a bounded, non-secret reason."""


# --------------------------------------------------------------------------- #
# Reason hygiene
# --------------------------------------------------------------------------- #

def _sanitize_reason(reason):
    """Bound and strip a reason so it can never carry control characters."""
    if not isinstance(reason, str):
        return ''
    cleaned = _CONTROL_RE.sub(' ', reason).strip()
    return cleaned[:MAX_REASON_LENGTH]


# --------------------------------------------------------------------------- #
# SemVer
# --------------------------------------------------------------------------- #

def parse_semver(text):
    """Parse a strict ``X.Y.Z`` version into ``(major, minor, patch)``.

    Strict SemVer 2.0 numeric identifiers: exactly three dot-separated decimal
    components, no leading zeroes, no ``v`` prefix, no pre-release/build
    suffix, no whitespace. Anything else raises :class:`ValueError` with a
    bounded reason.
    """
    if not isinstance(text, str):
        raise ValueError('version must be a string')
    if not text or len(text) > MAX_VERSION_LENGTH:
        raise ValueError('version must be a bounded non-empty string')
    match = _SEMVER_RE.match(text)
    if match is None:
        raise ValueError('version must be strict X.Y.Z')
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(candidate, current):
    """True when ``candidate`` is a strictly newer SemVer than ``current``.

    Pure: an unparsable argument yields ``False`` (never raises).
    """
    try:
        return parse_semver(candidate) > parse_semver(current)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class Manifest:
    """A fully validated update manifest (never partially validated)."""

    schema_version: int
    version: str
    channel: str
    source_commit: str
    min_image_version: str
    bundle_url: str
    bundle_sha256: str
    bundle_size: int
    release_summary: str
    release_url: str
    reboot_required: bool


#: Validation outcomes returned by :func:`classify_manifest`.
OUTCOME_AVAILABLE = 'update_available'
OUTCOME_UP_TO_DATE = 'up_to_date'
OUTCOME_DOWNGRADE = 'downgrade'
OUTCOME_INCOMPATIBLE_IMAGE = 'incompatible_image'
OUTCOME_INVALID = 'invalid'


class ManifestOutcome(tuple):
    """Immutable ``(ok, outcome, reason)`` validation result.

    Behaves like a 3-tuple (so ``ok, outcome, reason = ...`` works) and also
    exposes the named attributes.
    """

    __slots__ = ()

    def __new__(cls, ok, outcome, reason=''):
        return super().__new__(cls, (bool(ok), outcome, reason))

    @property
    def ok(self):
        return self[0]

    @property
    def outcome(self):
        return self[1]

    @property
    def reason(self):
        return self[2]


def _decode_manifest(payload):
    """Decode a dict/JSON-str/JSON-bytes payload into a dict; else raise."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ManifestError('manifest is too large')
        try:
            text = bytes(payload).decode('utf-8')
        except UnicodeDecodeError:
            raise ManifestError('manifest is not valid UTF-8') from None
    elif isinstance(payload, str):
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ManifestError('manifest is too large')
        text = payload
    else:
        raise ManifestError('manifest must be a JSON object')
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        raise ManifestError('manifest is not valid JSON') from None
    if not isinstance(doc, dict):
        raise ManifestError('manifest must be a JSON object')
    return doc


def _require(doc, key):
    if key not in doc:
        raise ManifestError(f'manifest field {key} is missing')
    return doc[key]


def _require_text(value, key, max_length):
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ManifestError(f'manifest field {key} is invalid')
    if _CONTROL_RE.search(value):
        raise ManifestError(f'manifest field {key} is invalid')
    return value


def _require_semver(value, key):
    _require_text(value, key, MAX_VERSION_LENGTH)
    try:
        parse_semver(value)
    except ValueError:
        raise ManifestError(f'manifest field {key} is not valid SemVer') from None
    return value


def _require_https_url(value, key):
    _require_text(value, key, MAX_URL_LENGTH)
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        raise ManifestError(f'manifest field {key} is invalid') from None
    if parts.scheme != 'https' or not parts.netloc:
        raise ManifestError(f'manifest field {key} must be https')
    return value


def parse_manifest(payload, *, current_version='', current_image_version=''):
    """Parse and strictly validate an update manifest.

    ``payload`` may be a ``dict``, a JSON ``str`` or JSON ``bytes``. Returns a
    fully populated :class:`Manifest` or raises :class:`ManifestError` (a
    :class:`ValueError`) with a bounded, non-secret reason. No partially
    validated manifest is ever returned.

    When ``current_version`` / ``current_image_version`` are supplied and parse
    as SemVer, the candidate must be strictly newer than ``current_version``
    (downgrade *and* equal are rejected) and must not require a newer image
    than ``current_image_version``. Empty values skip those comparisons; the
    caller supplies them once the installed versions are known.
    """
    doc = _decode_manifest(payload)

    schema = doc.get('schema_version')
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ManifestError('manifest schema_version is missing or invalid')
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError('manifest schema_version is not supported')

    version = _require_semver(_require(doc, 'version'), 'version')
    min_image_version = _require_semver(
        _require(doc, 'min_image_version'), 'min_image_version')

    channel = _require(doc, 'channel')
    if channel not in CHANNELS:
        raise ManifestError('manifest channel is not supported')

    source_commit = _require_text(
        _require(doc, 'source_commit'), 'source_commit', MAX_COMMIT_LENGTH)

    bundle_url = _require_https_url(_require(doc, 'bundle_url'), 'bundle_url')
    release_url = _require_https_url(_require(doc, 'release_url'), 'release_url')

    bundle_sha256 = _require(doc, 'bundle_sha256')
    if not isinstance(bundle_sha256, str) or not _SHA256_RE.match(bundle_sha256):
        raise ManifestError('manifest bundle_sha256 is malformed')
    bundle_sha256 = bundle_sha256.lower()

    bundle_size = _require(doc, 'bundle_size')
    if isinstance(bundle_size, bool) or not isinstance(bundle_size, int):
        raise ManifestError('manifest bundle_size is invalid')
    if bundle_size <= 0:
        raise ManifestError('manifest bundle_size is invalid')
    if bundle_size > MAX_BUNDLE_SIZE:
        raise ManifestError('manifest bundle_size exceeds the limit')

    release_summary = _require(doc, 'release_summary')
    if not isinstance(release_summary, str) or not release_summary.strip():
        raise ManifestError('manifest release_summary is missing')
    if len(release_summary) > MAX_SUMMARY_LENGTH:
        raise ManifestError('manifest release_summary is too long')

    reboot_required = _require(doc, 'reboot_required')
    if not isinstance(reboot_required, bool):
        raise ManifestError('manifest reboot_required is not a boolean')

    # Optional upgrade/compatibility comparisons. A supplied-but-unparsable
    # installed version is a hard error: silently skipping the downgrade check
    # would fail open exactly when the durable state is corrupted.
    if current_version:
        try:
            current = parse_semver(current_version)
        except ValueError as e:
            raise ManifestError('installed version is invalid') from e
        candidate = parse_semver(version)
        if candidate < current:
            raise ManifestError('manifest version is a downgrade')
        if candidate == current:
            raise ManifestError(
                'manifest version is not newer than the installed version')
    if current_image_version:
        try:
            image = parse_semver(current_image_version)
        except ValueError as e:
            raise ManifestError('installed image version is invalid') from e
        if parse_semver(min_image_version) > image:
            raise ManifestError('manifest requires a newer image version')

    return Manifest(
        schema_version=schema,
        version=version,
        channel=channel,
        source_commit=source_commit,
        min_image_version=min_image_version,
        bundle_url=bundle_url,
        bundle_sha256=bundle_sha256,
        bundle_size=bundle_size,
        release_summary=release_summary,
        release_url=release_url,
        reboot_required=reboot_required,
    )


def classify_manifest(manifest, *, current_version='', current_image_version=''):
    """Classify a validated :class:`Manifest` against the installed versions.

    Pure: returns a :class:`ManifestOutcome` ``(ok, outcome, reason)``. The
    outcome is one of :data:`OUTCOME_AVAILABLE`, :data:`OUTCOME_UP_TO_DATE`,
    :data:`OUTCOME_DOWNGRADE`, :data:`OUTCOME_INCOMPATIBLE_IMAGE` or
    :data:`OUTCOME_INVALID`. It never raises and performs no I/O.
    """
    try:
        candidate = parse_semver(getattr(manifest, 'version', None))
        min_image = parse_semver(getattr(manifest, 'min_image_version', None))
    except (ValueError, TypeError):
        return ManifestOutcome(False, OUTCOME_INVALID, 'manifest version is invalid')

    image = None
    if current_image_version:
        try:
            image = parse_semver(current_image_version)
        except ValueError:
            return ManifestOutcome(
                False, OUTCOME_INVALID, 'installed image version is invalid')
    if image is not None and min_image > image:
        return ManifestOutcome(
            False, OUTCOME_INCOMPATIBLE_IMAGE,
            'manifest requires a newer image version')

    if current_version:
        try:
            current = parse_semver(current_version)
        except ValueError:
            return ManifestOutcome(
                False, OUTCOME_INVALID, 'installed version is invalid')
        if candidate < current:
            return ManifestOutcome(
                False, OUTCOME_DOWNGRADE, 'manifest version is a downgrade')
        if candidate == current:
            return ManifestOutcome(
                False, OUTCOME_UP_TO_DATE,
                'manifest version is not newer than the installed version')

    return ManifestOutcome(True, OUTCOME_AVAILABLE, '')


# --------------------------------------------------------------------------- #
# Runner plumbing (injectable; no command runs at import)
# --------------------------------------------------------------------------- #

def _default_minisign_runner(args, timeout):
    """Run ``minisign`` with a bounded timeout (text output)."""
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout, check=False)


def _default_zstd_runner(args, timeout):
    """Run ``zstd`` with a bounded timeout (binary-safe output)."""
    return subprocess.run(
        list(args), capture_output=True, timeout=timeout, check=False)


def _runner_returncode(result):
    """Best-effort return code of a runner result (missing means failure)."""
    value = getattr(result, 'returncode', 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _runner_failure(exc, timed_out, missing, unavailable, generic):
    """Map a runner exception to a bounded reason string."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return timed_out
    if isinstance(exc, FileNotFoundError):
        return missing
    if isinstance(exc, OSError):
        return unavailable
    return generic


# --------------------------------------------------------------------------- #
# Minisign verification (AC-29)
# --------------------------------------------------------------------------- #

def verify_file(path, *, signature_path=None,
                public_key_path=DEFAULT_PUBLIC_KEY_PATH, runner=None):
    """Verify a detached minisign signature; return ``(ok, reason)``.

    The default runner invokes exactly::

        minisign -V -m <path> -p <public_key_path> -x <signature_path>

    with a bounded timeout (:data:`MINISIGN_TIMEOUT_SECONDS`). ``runner`` is
    injectable as ``runner(args, timeout)`` so tests never call the real binary.
    ``signature_path`` defaults to ``<path> + '.minisig'``.

    This function never raises: a missing file/signature/public key, a missing
    ``minisign`` binary, a timeout, or a non-zero exit all return ``False`` with
    a short, non-secret reason. The public key is not secret; the reason never
    includes file contents.
    """
    try:
        return _verify_file(
            path,
            signature_path=signature_path,
            public_key_path=public_key_path,
            runner=runner,
        )
    except Exception as e:  # noqa: BLE001 - verification must never raise
        log.debug('updater: verify_file failed: %s', type(e).__name__)
        return False, 'signature verification failed'


def _verify_file(path, *, signature_path, public_key_path, runner):
    if not isinstance(path, str) or not path:
        return False, 'file path is required'
    if not isinstance(public_key_path, str) or not public_key_path:
        return False, 'public key path is required'
    signature = signature_path if signature_path else path + DEFAULT_SIGNATURE_SUFFIX
    if not isinstance(signature, str) or not signature:
        return False, 'signature path is required'

    if not os.path.isfile(path):
        return False, 'file is missing'
    if not os.path.isfile(signature):
        return False, 'signature file is missing'
    if not os.path.isfile(public_key_path):
        return False, 'public key is missing'

    runner = runner or _default_minisign_runner
    args = [
        MINISIGN_BINARY, '-V',
        '-m', path,
        '-p', public_key_path,
        '-x', signature,
    ]
    try:
        result = runner(list(args), MINISIGN_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 - map every runner failure to a reason
        return False, _runner_failure(
            e,
            'minisign verification timed out',
            'minisign is not installed',
            'minisign is unavailable',
            'minisign verification failed',
        )

    returncode = _runner_returncode(result)
    if returncode != 0:
        return False, _sanitize_reason(
            f'signature verification failed (exit {returncode})')
    return True, ''


# --------------------------------------------------------------------------- #
# Archive safety (AC-29, AC-32)
# --------------------------------------------------------------------------- #

def _member_flag(member, name):
    """Best-effort boolean flag from a TarInfo-like member (never raises)."""
    value = getattr(member, name, None)
    if callable(value):
        try:
            return bool(value())
        except Exception:  # noqa: BLE001 - a hostile member must not raise
            return False
    return bool(value)


def _member_is_device(member):
    """True for character/block devices and FIFOs."""
    return any(
        _member_flag(member, name)
        for name in ('isdev', 'ischr', 'isblk', 'isfifo')
    )


def _within(base_real, target_real):
    """True when ``target_real`` is ``base_real`` or nested inside it."""
    try:
        return os.path.commonpath([base_real, target_real]) == base_real
    except ValueError:
        return False


def _escapes(base_real, name):
    """True when an archive path is absolute or escapes ``base_real``."""
    if not isinstance(name, str) or not name:
        return True
    normalized = name.replace('\\', '/')
    if normalized.startswith('/') or os.path.isabs(name):
        return True
    if any(part == '..' for part in normalized.split('/')):
        return True
    target = os.path.realpath(os.path.join(base_real, normalized))
    return not _within(base_real, target)


def validate_archive_members(members, dest):
    """Validate ``tar`` members before extraction; return ``(ok, reason)``.

    ``members`` is any iterable of :class:`tarfile.TarInfo`-like objects.
    Rejects (with a bounded, non-secret reason):

    * absolute paths and any ``..`` path component,
    * paths whose resolved target escapes ``dest``,
    * character/block devices and FIFOs,
    * symlink/hardlink targets that are absolute or escape ``dest``,
    * setuid/setgid mode bits,
    * a non-root owner (``uid``/``gid`` must be ``0``),
    * members larger than :data:`MAX_MEMBER_SIZE` and a total larger than
      :data:`MAX_TOTAL_EXTRACTED_SIZE` (or more than
      :data:`MAX_ARCHIVE_MEMBERS` members).

    Never raises. This is a pure inspection pass; nothing is written.
    """
    try:
        return _validate_members(members, dest)
    except Exception as e:  # noqa: BLE001 - validation must never raise
        log.debug('updater: validate_archive_members failed: %s', type(e).__name__)
        return False, 'archive member validation failed'


def _validate_members(members, dest):
    if not isinstance(dest, str) or not dest:
        return False, 'destination is required'
    if members is None:
        return False, 'archive has no members'
    dest_real = os.path.realpath(dest)

    total = 0
    count = 0
    for member in members:
        count += 1
        if count > MAX_ARCHIVE_MEMBERS:
            return False, 'archive has too many members'
        ok, reason, size = _validate_member(member, dest_real)
        if not ok:
            return False, reason
        total += size
        if total > MAX_TOTAL_EXTRACTED_SIZE:
            return False, 'archive exceeds the total size limit'
    return True, ''


def _validate_member(member, dest_real):
    """Validate one member; return ``(ok, reason, size)``."""
    name = getattr(member, 'name', None)
    if not isinstance(name, str) or not name:
        return False, 'archive member has no name', 0
    if _escapes(dest_real, name):
        return False, 'archive member path escapes the destination', 0

    if _member_is_device(member):
        return False, 'archive member is a device or fifo', 0

    mode = getattr(member, 'mode', 0)
    if isinstance(mode, bool) or not isinstance(mode, int):
        return False, 'archive member mode is invalid', 0
    if mode & (stat.S_ISUID | stat.S_ISGID):
        return False, 'archive member has setuid/setgid bits', 0

    uid = getattr(member, 'uid', 0)
    gid = getattr(member, 'gid', 0)
    if (isinstance(uid, bool) or not isinstance(uid, int)
            or isinstance(gid, bool) or not isinstance(gid, int)):
        return False, 'archive member owner is invalid', 0
    if uid != 0 or gid != 0:
        return False, 'archive member has an unexpected owner', 0

    size = getattr(member, 'size', 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return False, 'archive member size is invalid', 0
    if size > MAX_MEMBER_SIZE:
        return False, 'archive member exceeds the size limit', 0

    if _member_flag(member, 'issym') or _member_flag(member, 'islnk'):
        ok, reason = _validate_link(member, name, dest_real)
        if not ok:
            return False, reason, 0

    return True, '', size


def _validate_link(member, name, dest_real):
    """Validate a symlink/hardlink target stays inside ``dest``."""
    linkname = getattr(member, 'linkname', None)
    if not isinstance(linkname, str) or not linkname:
        return False, 'archive link has no target'
    normalized = linkname.replace('\\', '/')
    if normalized.startswith('/') or os.path.isabs(linkname):
        return False, 'archive link target is absolute'
    if any(part == '..' for part in normalized.split('/')):
        return False, 'archive link target escapes the destination'

    if _member_flag(member, 'islnk'):
        # Hardlink names are relative to the archive root.
        link_target = os.path.realpath(os.path.join(dest_real, normalized))
    else:
        # Symlink names are relative to the member's own directory.
        member_target = os.path.realpath(os.path.join(dest_real, name))
        link_target = os.path.realpath(
            os.path.join(os.path.dirname(member_target), normalized))
    if not _within(dest_real, link_target):
        return False, 'archive link target escapes the destination'
    return True, ''


# --------------------------------------------------------------------------- #
# Bundle extraction (AC-29, AC-30)
# --------------------------------------------------------------------------- #

def _sha256_file(path, chunk_size=1024 * 1024):
    """Return the lowercase hex SHA-256 of ``path``, or ``None`` on error."""
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            while True:
                block = handle.read(chunk_size)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _is_sha256(value):
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def _decompress(archive_path, tar_path, runner):
    """Decompress ``archive_path`` to ``tar_path``; return ``(ok, reason)``.

    The default runner invokes exactly::

        zstd -d -q -f -o <tar_path> <archive_path>

    with a bounded timeout (:data:`ZSTD_TIMEOUT_SECONDS`). ``runner`` is
    injectable as ``runner(args, timeout)``.
    """
    args = [ZSTD_BINARY, '-d', '-q', '-f', '-o', tar_path, archive_path]
    try:
        result = runner(list(args), ZSTD_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 - map every runner failure to a reason
        return False, _runner_failure(
            e,
            'zstd decompression timed out',
            'zstd is not installed',
            'zstd is unavailable',
            'zstd decompression failed',
        )
    returncode = _runner_returncode(result)
    if returncode != 0:
        return False, _sanitize_reason(
            f'zstd decompression failed (exit {returncode})')
    return True, ''


def extract_bundle(archive_path, dest, *, expected_sha256=None, runner=None):
    """Verify, decompress and safely extract a ``.tar.zst`` app bundle.

    Order of operations (all bounded; nothing is extracted before validation):

    1. verify ``expected_sha256`` against the archive when supplied,
    2. reject an archive over :data:`MAX_BUNDLE_SIZE`,
    3. decompress ``.tar.zst`` through the injectable ``zstd`` runner into a
       temporary tar, rejecting output over :data:`MAX_DECOMPRESSED_SIZE`,
    4. validate every member with :func:`validate_archive_members`,
    5. extract into ``dest`` with :mod:`tarfile` and the stdlib ``data`` filter,
       so links are never followed out of ``dest``.

    Returns ``(ok, reason)`` and never raises; the temporary directory is always
    removed. ``runner`` is injectable as ``runner(args, timeout)``.
    """
    try:
        return _extract_bundle(
            archive_path, dest,
            expected_sha256=expected_sha256, runner=runner)
    except Exception as e:  # noqa: BLE001 - extraction must never raise
        log.debug('updater: extract_bundle failed: %s', type(e).__name__)
        return False, 'bundle extraction failed'


def _extract_bundle(archive_path, dest, *, expected_sha256, runner):
    if not isinstance(archive_path, str) or not archive_path:
        return False, 'bundle path is required'
    if not isinstance(dest, str) or not dest:
        return False, 'destination is required'
    if not os.path.isfile(archive_path):
        return False, 'bundle file is missing'

    if expected_sha256 is not None:
        if not _is_sha256(expected_sha256):
            return False, 'bundle sha256 is malformed'
        actual = _sha256_file(archive_path)
        if actual is None:
            return False, 'bundle could not be read'
        if actual != expected_sha256.lower():
            return False, 'bundle sha256 mismatch'

    try:
        archive_size = os.path.getsize(archive_path)
    except OSError:
        return False, 'bundle could not be read'
    if archive_size > MAX_BUNDLE_SIZE:
        return False, 'bundle exceeds the size limit'

    runner = runner or _default_zstd_runner
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix='buddy3d-update-')
        tar_path = os.path.join(tmp_dir, 'bundle.tar')
        ok, reason = _decompress(archive_path, tar_path, runner)
        if not ok:
            return False, reason
        if not os.path.isfile(tar_path):
            return False, 'bundle decompression produced no archive'
        try:
            if os.path.getsize(tar_path) > MAX_DECOMPRESSED_SIZE:
                return False, 'bundle is too large to extract'
        except OSError:
            return False, 'bundle decompression produced no archive'

        try:
            # The destination itself must be a real directory: a pre-planted
            # symlink at the staging path would defeat both the realpath checks
            # and tarfile's data filter (WP-R4a hardening).
            if os.path.islink(dest):
                return False, 'destination must not be a symlink'
            if os.path.exists(dest) and not os.path.isdir(dest):
                return False, 'destination is not a directory'
            created = not os.path.exists(dest)
            try:
                with tarfile.open(tar_path, 'r:') as archive:
                    # Bound the member count lazily so a tar of millions of tiny
                    # entries cannot exhaust memory before the cap applies.
                    members = []
                    for member in archive:
                        members.append(member)
                        if len(members) > MAX_ARCHIVE_MEMBERS:
                            return False, 'bundle has too many members'
                    ok, reason = validate_archive_members(members, dest)
                    if not ok:
                        return False, reason
                    os.makedirs(dest, exist_ok=True)
                    archive.extractall(dest, members=members, filter='data')
            except Exception as e:  # noqa: BLE001 - any tar failure is bounded
                log.debug('updater: tar extraction failed: %s', type(e).__name__)
                # Never leave a half-extracted staging tree behind.
                if created:
                    shutil.rmtree(dest, ignore_errors=True)
                return False, 'bundle archive could not be extracted'
        except Exception as e:  # noqa: BLE001 - any tar failure is bounded
            log.debug('updater: tar extraction failed: %s', type(e).__name__)
            return False, 'bundle archive could not be extracted'
        return True, ''
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


__all__ = [
    'CHANNELS',
    'DEFAULT_PUBLIC_KEY_PATH',
    'DEFAULT_SIGNATURE_SUFFIX',
    'MAX_ARCHIVE_MEMBERS',
    'MAX_BUNDLE_SIZE',
    'MAX_COMMIT_LENGTH',
    'MAX_DECOMPRESSED_SIZE',
    'MAX_MANIFEST_BYTES',
    'MAX_MEMBER_SIZE',
    'MAX_REASON_LENGTH',
    'MAX_SUMMARY_LENGTH',
    'MAX_TOTAL_EXTRACTED_SIZE',
    'MAX_URL_LENGTH',
    'MAX_VERSION_LENGTH',
    'MINISIGN_BINARY',
    'MINISIGN_TIMEOUT_SECONDS',
    'Manifest',
    'ManifestError',
    'ManifestOutcome',
    'OUTCOME_AVAILABLE',
    'OUTCOME_DOWNGRADE',
    'OUTCOME_INCOMPATIBLE_IMAGE',
    'OUTCOME_INVALID',
    'OUTCOME_UP_TO_DATE',
    'SUPPORTED_SCHEMA_VERSIONS',
    'ZSTD_BINARY',
    'ZSTD_TIMEOUT_SECONDS',
    'classify_manifest',
    'extract_bundle',
    'is_newer',
    'parse_manifest',
    'parse_semver',
    'validate_archive_members',
    'verify_file',
]
