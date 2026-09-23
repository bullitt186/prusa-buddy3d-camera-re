"""Signed application-update install orchestration (WP-R4b; AC-29/AC-30/AC-31).

This module owns distribution plan §7.2 (the install algorithm) and the
report-only availability check. It builds on the reviewed verification core in
:mod:`updater` (manifest parsing, minisign verification, safe extraction) and
changes none of its accepted behavior.

Two public entry points
-----------------------

``check_for_update(...)``
    §7.2 step 1-2: the 24 h + jittered, report-only availability check. It
    never installs; it fetches a signed manifest through an injected callable,
    verifies it, classifies it against the installed versions, and returns a
    bounded :class:`CheckResult`. The check interval is enforced through an
    injectable ``clock`` and ``rng``; the last-check timestamp is persisted to
    ``last_check_path`` (best effort).

``install_update(...)``
    §7.2 steps 3-11: unique staging directory on DATA, verify manifest
    signature / bundle signature / SHA-256 / size / version / compatibility /
    free space *before* extracting, extract, build the release venv from
    bundled hash-pinned wheels, preflight, atomic rename + ``current`` swap,
    restart, health-check for up to 90 s, roll back to ``previous`` and mark the
    attempted version bad on failure, prune only after a successful health
    check. Prusa cloud reachability is deliberately **not** a health
    requirement (an Internet outage must never trigger a rollback).

Injectable I/O (no network/filesystem/command call on import)
-------------------------------------------------------------

Every external effect is a callable passed by the caller, so the orchestration
is host-testable with fakes and the module never touches the running system at
import time. The contracts are:

``fetch_manifest() -> (ok, payload_or_reason)``
    payload is a manifest ``dict``/JSON ``str``/JSON ``bytes``.

``verify(payload) -> (ok, reason)``
    verify the signed manifest; the CLI binds this to :func:`updater.verify_file`.

``download(manifest, staging_dir) -> (ok, bundle_path_or_reason)``
    download the bundle into ``staging_dir`` and return its path.

``verify_manifest_signature(manifest, staging_dir) -> (ok, reason)``
``verify_bundle_signature(bundle_path) -> (ok, reason)``
``free_space(path) -> int``
    free bytes available on the filesystem holding ``path``.

``extract(bundle_path, dest_dir, *, expected_sha256) -> (ok, reason)``
    compatible with :func:`updater.extract_bundle`; SHA-256 is checked before
    any extraction.

``build_venv(staging_dir, manifest) -> (ok, reason)``
``preflight(staging_dir, manifest) -> (ok, reason)``
``switch(paths, staging_dir, version) -> (ok, reason)``
    atomic rename into ``<version>`` and ``previous``/``current`` swap.

``health_check(version) -> truthy | (ok, reason)``
``restart_services(version) -> (ok, reason)``
``record_bad(version, reason)``
``prune(paths, protected_versions)``
    remove old releases; must never remove ``current``/``previous``/factory.

``clock() -> float`` (epoch seconds) and ``sleeper(seconds)``.

A release that failed health checks is recorded under
``<releases>/.bad/<version>.json``; :func:`check_for_update` never offers it and
:func:`install_update` refuses it unless the caller passes ``force_reinstall``.

Stdlib only, import-safe: importing this module runs no command, reads no file
and performs no network I/O. The CLI (``recover`` / ``check`` / ``install``) is
the only place that assembles the real callables, and it is guarded by
``__main__``.
"""
import argparse
import dataclasses
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import app_version
import updater

log = logging.getLogger('prusa-cam.updater_install')

# --------------------------------------------------------------------------- #
# Paths and bounds
# --------------------------------------------------------------------------- #

#: Durable DATA root. Releases live on the PERSIST partition, never in ROOT.
DATA_ROOT = '/data/prusa-cam'
DEFAULT_RELEASES_DIR = DATA_ROOT + '/releases'
DEFAULT_CURRENT_LINK = DEFAULT_RELEASES_DIR + '/current'
DEFAULT_PREVIOUS_LINK = DEFAULT_RELEASES_DIR + '/previous'
#: The immutable factory application is the fallback when DATA has no valid
#: active release (and the initial ``previous`` target before the first update).
DEFAULT_FACTORY_APP = '/opt/prusa-cam'
DEFAULT_STATE_DIR = DATA_ROOT
DEFAULT_LAST_CHECK_PATH = DEFAULT_STATE_DIR + '/last-update-check.json'
DEFAULT_PUBLIC_KEY_PATH = updater.DEFAULT_PUBLIC_KEY_PATH

#: HA update-state document read by the MQTT service and written by the root
#: updater after every ``check``/``install`` run. It lives directly under the
#: durable ``/data/prusa-cam`` state area (root-owned releases aside, this file is
#: written only by the root updater with mode 0644, so the unprivileged
#: ``prusa-cam`` app can read it but never needs to write it).
DEFAULT_UPDATE_STATE_PATH = DATA_ROOT + '/update-state.json'
#: Hard bound on the state document (both read and write); a larger file is
#: treated as corrupt so a runaway writer cannot feed the MQTT publisher.
MAX_UPDATE_STATE_BYTES = 8 * 1024

#: Downloaded bundle filename inside the unique staging directory.
BUNDLE_FILENAME = 'bundle.tar.zst'
#: Detached minisign signature for the bundle (master §7.1 publishes it next to
#: the archive; :func:`updater.verify_file` defaults to ``<path> + suffix``).
BUNDLE_SIGNATURE_SUFFIX = updater.DEFAULT_SIGNATURE_SUFFIX
#: Directory under the releases tree holding per-version bad-release markers.
BAD_RELEASES_DIRNAME = '.bad'

#: 24 h check interval with up to +/- 1 h of randomized jitter (AC-30).
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
CHECK_JITTER_SECONDS = 60 * 60

#: Local health checks must pass within 90 s (AC-30).
HEALTH_TIMEOUT_SECONDS = 90.0
HEALTH_POLL_SECONDS = 5.0
#: Hard cap so a fake clock/sleeper that never advances cannot loop forever.
MAX_HEALTH_ATTEMPTS = 200

#: Free-space headroom required on top of the declared bundle size. The
#: downloaded bundle is *compressed*; the extracted release tree plus the venv
#: built from the bundled wheels need room too. The reservation is therefore
#: ``bundle_size * EXTRACTED_EXPANSION_FACTOR + VENV_ALLOWANCE_BYTES +
#: FREE_SPACE_HEADROOM_BYTES`` (see :func:`required_free_space`).
FREE_SPACE_HEADROOM_BYTES = 64 * 1024 * 1024
#: Worst-case ratio between the extracted release tree and the compressed
#: ``.tar.zst`` bundle (zstd on Python/static assets regularly exceeds 3x).
EXTRACTED_EXPANSION_FACTOR = 4
#: Fixed allowance for the per-release venv (interpreter symlinks + installed
#: pure-Python wheels) that is not part of the compressed bundle.
VENV_ALLOWANCE_BYTES = 256 * 1024 * 1024

#: Bound every reason returned to a caller/log (and the raw manifest read).
MAX_REASON_LENGTH = 200
MAX_MANIFEST_DOWNLOAD_BYTES = 64 * 1024
#: Detached minisign signatures are tiny; cap them tightly.
MAX_SIGNATURE_DOWNLOAD_BYTES = 64 * 1024

#: Bounded outcomes of :func:`check_for_update`.
CHECK_AVAILABLE = 'available'
CHECK_UP_TO_DATE = 'up_to_date'
CHECK_INVALID = 'invalid'
CHECK_ERROR = 'error'
#: The signed manifest offers a version the caller has already marked bad; it is
#: not offered again unless a reinstall is explicitly forced (AC-30).
CHECK_SUPPRESSED = 'suppressed'

#: Bounded outcomes of :func:`install_update`.
INSTALL_INSTALLED = 'installed'
INSTALL_ROLLED_BACK = 'rolled_back'
INSTALL_FAILED = 'failed'

#: Bounded wall-clock timeouts for the real (CLI) callables.
DOWNLOAD_TIMEOUT_SECONDS = 300.0
COMMAND_TIMEOUT_SECONDS = 300.0
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0

#: Absolute binaries for the root service. The systemd unit also pins a fixed
#: PATH so ``minisign``/``zstd`` (invoked by :mod:`updater`) resolve
#: deterministically; these two are resolved by absolute path first.
SYSTEMCTL_BINARY = '/usr/bin/systemctl'
PYTHON_BINARY = '/usr/bin/python3'

#: Environment overrides used by the real CLI callables. ``MANIFEST_URL_ENV`` is
#: supplied by the root-owned ``/etc/prusa-updater.conf`` (``EnvironmentFile=``),
#: never by a service-writable path. The signing public key is deliberately NOT
#: configurable: the updater always trusts :data:`DEFAULT_PUBLIC_KEY_PATH`
#: (AC-32), so a compromised service account cannot redirect verification.
MANIFEST_URL_ENV = 'PRUSA_UPDATE_MANIFEST_URL'
HEALTH_ENDPOINTS_ENV = 'PRUSA_UPDATE_HEALTH_ENDPOINTS'
#: Local-only health endpoints. The default is the release app's own local
#: HTTP/ONVIF facade on port 80 (started by the release ``main.py``), so a pass
#: proves the newly activated release is actually serving -- not an unrelated
#: process that happens to hold a port. Prusa cloud reachability is deliberately
#: excluded (an Internet outage must never trigger a rollback).
DEFAULT_HEALTH_ENDPOINTS = ('127.0.0.1:80',)
#: RTSP ports are deliberately NOT in :data:`DEFAULT_HEALTH_ENDPOINTS`: a device
#: with RTSP intentionally disabled must still pass a post-update health check.
#: Operators who want RTSP/ONVIF coverage list them through
#: :data:`HEALTH_ENDPOINTS_ENV`; every listed endpoint must then pass.
RTSP_HEALTH_PORTS = (8554, 8555)

#: Core release modules that must import cleanly under the release interpreter.
#: Chosen because they are stdlib-only at import time (no display/GStreamer and
#: no eager ``paho``/``aiohttp`` import), so the check proves the staged release
#: tree is self-consistent without requiring a display or network.
PREFLIGHT_IMPORT_MODULES = ('config_schema', 'mqtt_service', 'updater', 'settings_store')
#: Durable device document the migration dry-run loads and migrates (AC-30,
#: §7.2 step 7). Mirrors :data:`config_schema.DEVICE_TOML_PATH`; kept as a plain
#: string so this module does not need to import :mod:`config_schema` on the host
#: (the dry-run imports it *inside* the release interpreter).
DEFAULT_DEVICE_CONFIG_PATH = '/data/prusa-cam/config/device.toml'
#: Bounded wall-clock timeout for each preflight subprocess (compile, import,
#: migration dry-run).
PREFLIGHT_TIMEOUT_SECONDS = 60.0

_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]+')
#: A bare Python exception class name (the only dry-run stderr we echo back).
_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_REDACTED = '<redacted>'


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class InstallPaths:
    """Filesystem layout for the updater.

    Defaults match the appliance: releases under ``/data/prusa-cam/releases``
    (DATA), the factory application under ``/opt/prusa-cam`` (ROOT, immutable).
    """

    releases_dir: str = DEFAULT_RELEASES_DIR
    current_link: str = DEFAULT_CURRENT_LINK
    previous_link: str = DEFAULT_PREVIOUS_LINK
    factory_app: str = DEFAULT_FACTORY_APP
    state_dir: str = DEFAULT_STATE_DIR

    def version_dir(self, version):
        """Absolute path of the final release directory for ``version``."""
        return os.path.join(self.releases_dir, version)

    def bad_dir(self):
        """Absolute path of the per-version bad-release marker directory."""
        return os.path.join(self.releases_dir, BAD_RELEASES_DIRNAME)


@dataclasses.dataclass(frozen=True)
class RecoveryReport:
    """Bounded result of :func:`recover_interrupted` (all paths are basenames)."""

    staging_removed: tuple = ()
    orphans_removed: tuple = ()
    current_repaired: bool = False
    current_target: str = ''

    @property
    def changed(self):
        return bool(
            self.staging_removed or self.orphans_removed or self.current_repaired)



@dataclasses.dataclass(frozen=True)
class CheckResult:
    """Bounded result of :func:`check_for_update` (never partially populated)."""

    checked: bool
    outcome: str
    manifest: object = None
    reason: str = ''
    next_check_epoch: float = 0.0

    @property
    def available(self):
        return self.checked and self.outcome == CHECK_AVAILABLE


@dataclasses.dataclass(frozen=True)
class InstallResult:
    """Bounded result of :func:`install_update`."""

    ok: bool
    outcome: str
    installed_version: str = ''
    attempted_version: str = ''
    reboot_required: bool = False
    reason: str = ''
    staging_dir: str = ''


# --------------------------------------------------------------------------- #
# Reason hygiene
# --------------------------------------------------------------------------- #

def _sanitize(reason):
    """Bound and strip control characters from a reason string."""
    if not isinstance(reason, str):
        return ''
    return _CONTROL_RE.sub(' ', reason).strip()[:MAX_REASON_LENGTH]


def _redact(reason, secrets):
    """Replace every secret substring, then bound the result."""
    text = _sanitize(reason)
    for secret in secrets or ():
        if isinstance(secret, str) and secret:
            text = text.replace(secret, _REDACTED)
    return text[:MAX_REASON_LENGTH]


def _bounded_text(value, limit):
    """Return a control-character-free, bounded string (``''`` when not a str)."""
    if not isinstance(value, str):
        return ''
    return _CONTROL_RE.sub(' ', value).strip()[:limit]


# --------------------------------------------------------------------------- #
# Small I/O helpers (used by the real callables and by switch/rollback)
# --------------------------------------------------------------------------- #

def _is_sha256(value):
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def _valid_bundle_size(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 < value <= updater.MAX_BUNDLE_SIZE
    )


def required_free_space(bundle_size):
    """Free bytes required on DATA before downloading ``bundle_size`` bytes.

    The compressed bundle alone is not enough: extraction expands it, and the
    per-release venv sits beside the extracted tree. The reservation is
    ``bundle_size * EXTRACTED_EXPANSION_FACTOR + VENV_ALLOWANCE_BYTES +
    FREE_SPACE_HEADROOM_BYTES`` (AC-30). Pure; returns ``0`` for a non-integer.
    """
    if isinstance(bundle_size, bool) or not isinstance(bundle_size, int):
        return 0
    return (
        bundle_size * EXTRACTED_EXPANSION_FACTOR
        + VENV_ALLOWANCE_BYTES
        + FREE_SPACE_HEADROOM_BYTES
    )


def _remove_file(path):
    # A failed os.makedirs/tempfile leaves ``path`` as None; never let a
    # best-effort cleanup turn into a TypeError (WP-R4c-2a).
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    try:
        os.remove(path)
    except (OSError, TypeError):
        pass


def _atomic_symlink(target, link):
    """Atomically point ``link`` at ``target`` (create-then-rename)."""
    directory = os.path.dirname(link) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = f'{link}.tmp.{os.getpid()}'
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link)
    except OSError:
        _remove_file(tmp)
        raise


def _current_target(paths):
    """Real path of the active release, or the factory app when none is valid."""
    try:
        target = os.path.realpath(paths.current_link)
    except OSError:
        return paths.factory_app
    if os.path.isdir(target):
        return target
    return paths.factory_app


def _active_version(paths):
    """Active release version (SemVer basename), or ``''`` for factory/none."""
    try:
        target = os.path.realpath(paths.current_link)
    except OSError:
        return ''
    name = os.path.basename(target.rstrip('/'))
    try:
        updater.parse_semver(name)
    except (ValueError, TypeError):
        return ''
    return name


def _symlink_version(link):
    """SemVer basename a symlink points at, only when that directory exists."""
    try:
        target = os.path.realpath(link)
    except OSError:
        return ''
    if not os.path.isdir(target):
        return ''
    name = os.path.basename(target.rstrip('/'))
    try:
        updater.parse_semver(name)
    except (ValueError, TypeError):
        return ''
    return name


def _version_dirs(paths):
    """SemVer release directory names present under the releases tree."""
    try:
        names = os.listdir(paths.releases_dir)
    except OSError:
        return []
    versions = []
    for name in names:
        if name in ('current', 'previous') or name.startswith('.'):
            continue
        path = os.path.join(paths.releases_dir, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            updater.parse_semver(name)
        except (ValueError, TypeError):
            continue
        versions.append(name)
    return versions


def _newest_version(versions):
    """Strictly newest SemVer from an iterable; ``''`` when none parse."""
    best = ''
    for version in versions:
        if not best or updater.is_newer(version, best):
            best = version
    return best


def read_bad_versions(paths):
    """Versions with a ``.bad/<version>.json`` marker (a bounded frozenset).

    Only well-formed SemVer markers are returned; an unreadable or missing
    marker directory yields the empty set. Performs read-only I/O.
    """
    try:
        names = os.listdir(paths.bad_dir())
    except OSError:
        return frozenset()
    versions = set()
    for name in names:
        if not name.endswith('.json'):
            continue
        version = name[:-len('.json')]
        try:
            updater.parse_semver(version)
        except (ValueError, TypeError):
            continue
        if os.path.isfile(os.path.join(paths.bad_dir(), name)):
            versions.add(version)
    return frozenset(versions)


def _resolve_binary(absolute, name):
    """Prefer the absolute binary; fall back to a PATH lookup."""
    if isinstance(absolute, str) and os.path.isfile(absolute) and os.access(
            absolute, os.X_OK):
        return absolute
    return shutil.which(name)


# --------------------------------------------------------------------------- #
# Atomic switch + rollback (direct filesystem, never injected)
# --------------------------------------------------------------------------- #

def switch_release(paths, staging_dir, version):
    """Atomically rename ``staging_dir`` to ``<version>`` and swap the links.

    The version directory is renamed into place *first*; ``previous`` and
    ``current`` are re-pointed only afterwards. A rename failure therefore
    leaves both symlinks exactly as they were (the staging directory stays
    intact for the caller to clean up). On a later symlink failure the rename is
    undone so no inactive release directory is left behind. Returns
    ``(ok, reason)`` and never raises.
    """
    try:
        try:
            updater.parse_semver(version)
        except (ValueError, TypeError):
            return False, 'release version is invalid'
        target = paths.version_dir(version)
        if os.path.exists(target):
            return False, 'release directory already exists'
        previous = _current_target(paths)
        try:
            os.rename(staging_dir, target)
        except OSError as e:
            return False, f'release activation failed ({type(e).__name__})'
        try:
            _atomic_symlink(previous, paths.previous_link)
        except OSError as e:
            # Undo the rename so no inactive release directory is left behind.
            shutil.rmtree(target, ignore_errors=True)
            return False, f'could not update the previous symlink ({type(e).__name__})'
        try:
            _atomic_symlink(target, paths.current_link)
        except OSError as e:
            shutil.rmtree(target, ignore_errors=True)
            return False, f'could not update the current symlink ({type(e).__name__})'
        return True, ''
    except OSError as e:
        return False, f'release activation failed ({type(e).__name__})'
    except Exception as e:  # noqa: BLE001 - activation must never raise
        log.debug('updater_install: switch_release failed: %s', type(e).__name__)
        return False, 'release activation failed'


def _rollback(paths):
    """Restore ``current`` to ``previous`` (or remove it for factory fallback)."""
    previous = None
    try:
        previous = os.readlink(paths.previous_link)
    except OSError:
        previous = None
    if previous and os.path.isdir(previous):
        try:
            _atomic_symlink(previous, paths.current_link)
            return True
        except OSError:
            return False
    # No usable previous release: fall back to the immutable factory app.
    _remove_file(paths.current_link)
    return False


# --------------------------------------------------------------------------- #
# Crash recovery (AC-30: interrupted staging/activation)
# --------------------------------------------------------------------------- #

def recover_interrupted(paths):
    """Repair state left behind by an interrupted update; never raises.

    * stale ``.<version>.staging.*`` directories are removed;
    * release directories not referenced by ``current``/``previous`` are removed
      (the immutable factory app lives outside the releases tree);
    * a dangling ``current`` symlink is re-pointed at the newest valid release,
      or at the factory app when no release survives.

    Idempotent: a second run on a healthy tree changes nothing. Returns a
    :class:`RecoveryReport` with basenames only. Performs filesystem I/O and is
    intended to run as root (the updater service ``ExecStartPre``).
    """
    try:
        return _recover_interrupted(paths)
    except Exception as e:  # noqa: BLE001 - recovery must never raise
        log.debug('updater_install: recover_interrupted failed: %s', type(e).__name__)
        return RecoveryReport()


def _recover_interrupted(paths):
    if not os.path.isdir(paths.releases_dir):
        return RecoveryReport()

    try:
        entries = os.listdir(paths.releases_dir)
    except OSError:
        return RecoveryReport()

    # Referenced versions come from real directories only: a dangling symlink
    # must not keep a deleted release "protected". A dangling ``current`` is
    # repaired to the newest valid release, which is therefore kept.
    active = _symlink_version(paths.current_link)
    previous = _symlink_version(paths.previous_link)
    referenced = {v for v in (active, previous) if v}

    dangling = (
        os.path.islink(paths.current_link)
        and not os.path.isdir(os.path.realpath(paths.current_link))
    )
    repair_target = ''
    if dangling:
        newest = _newest_version(_version_dirs(paths))
        if newest:
            referenced.add(newest)
            repair_target = paths.version_dir(newest)

    staging_removed = []
    for name in entries:
        if not name.startswith('.') or '.staging.' not in name:
            continue
        path = os.path.join(paths.releases_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            staging_removed.append(name)

    orphans_removed = []
    for name in _version_dirs(paths):
        if name in referenced:
            continue
        shutil.rmtree(os.path.join(paths.releases_dir, name), ignore_errors=True)
        orphans_removed.append(name)

    repaired = False
    target = ''
    if dangling:
        if not repair_target and os.path.isdir(paths.factory_app):
            repair_target = paths.factory_app
        if repair_target:
            try:
                _atomic_symlink(repair_target, paths.current_link)
                repaired = True
                target = repair_target
            except OSError:
                repaired = False
        else:
            # No valid release and no factory app: drop the dangling link so the
            # launcher's own factory fallback applies.
            _remove_file(paths.current_link)
            repaired = True

    return RecoveryReport(
        staging_removed=tuple(sorted(staging_removed)),
        orphans_removed=tuple(sorted(orphans_removed)),
        current_repaired=repaired,
        current_target=target,
    )


# --------------------------------------------------------------------------- #
# Injectable-call plumbing
# --------------------------------------------------------------------------- #

def _effect(fn, *args, **kwargs):
    """Call an injected effect; return ``(ok, value_or_reason)``; never raises.

    A callable may return ``(ok, value)``, a truthy/falsy scalar, or ``None``
    (treated as success). Any exception is a bounded failure.
    """
    try:
        result = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - a bad callable must not crash the run
        log.debug('updater_install: effect failed: %s', type(e).__name__)
        return False, f'{type(e).__name__}'
    if isinstance(result, tuple):
        if len(result) == 2:
            return bool(result[0]), result[1]
        return bool(result), ''
    if result is None:
        return True, ''
    return bool(result), ''


def _free_bytes(free_space, path):
    """Return an integer free-space value, or ``None`` on any failure."""
    try:
        value = free_space(path)
    except Exception:  # noqa: BLE001 - free-space probing must never raise
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _health_ok(result):
    """Normalize a health-check result (``bool``, truthy scalar or ``(ok, ...)``)."""
    if isinstance(result, tuple):
        return bool(result[0]) if result else False
    return bool(result)


# --------------------------------------------------------------------------- #
# check_for_update (§7.2 steps 1-2)
# --------------------------------------------------------------------------- #

def _now(clock):
    if clock is None:
        return time.time()
    try:
        value = clock()
    except Exception:  # noqa: BLE001 - a broken clock must not raise
        return time.time()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return time.time()
    return float(value)


def _jittered_interval(rng):
    """Return the base interval plus/minus up to :data:`CHECK_JITTER_SECONDS`."""
    base = float(CHECK_INTERVAL_SECONDS)
    jitter = float(CHECK_JITTER_SECONDS)
    if rng is None:
        return base
    try:
        if hasattr(rng, 'uniform'):
            offset = float(rng.uniform(-jitter, jitter))
        else:
            offset = (float(rng.random()) * 2.0 - 1.0) * jitter
    except Exception:  # noqa: BLE001 - a broken rng falls back to the base
        return base
    interval = base + offset
    if interval < base * 0.5:
        interval = base * 0.5
    if interval > base * 1.5:
        interval = base * 1.5
    return interval


def _read_last_check(path):
    """Read a persisted last-check epoch; ``None`` when absent/corrupt."""
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, encoding='utf-8') as handle:
            text = handle.read(4096)
    except OSError:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        try:
            return float(text.strip())
        except ValueError:
            return None
    value = doc.get('last_check') if isinstance(doc, dict) else doc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _write_last_check(path, now, outcome):
    """Persist the last-check timestamp atomically (best effort)."""
    if not isinstance(path, str) or not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f'{path}.tmp.{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump({'last_check': float(now), 'outcome': outcome}, handle)
        os.replace(tmp, path)
    except OSError:
        log.debug('updater_install: could not persist the last-check timestamp')


def check_for_update(*, current_version, current_image_version,
                     fetch_manifest, verify=None, clock=None, rng=None,
                     last_check_path=DEFAULT_LAST_CHECK_PATH, force=False,
                     bad_versions=(), force_reinstall=False):
    """Report-only availability check (§7.2 steps 1-2).

    Enforces the 24 h interval with randomized jitter through the injected
    ``clock``/``rng`` (``force=True`` bypasses it for the authenticated manual
    request). Never installs anything. A version in ``bad_versions`` (the
    ``.bad`` markers) is suppressed (:data:`CHECK_SUPPRESSED`) unless
    ``force_reinstall`` is set. Returns a :class:`CheckResult` whose ``outcome``
    is one of :data:`CHECK_AVAILABLE`, :data:`CHECK_UP_TO_DATE`,
    :data:`CHECK_INVALID`, :data:`CHECK_SUPPRESSED` or :data:`CHECK_ERROR`;
    ``manifest`` is populated when a manifest parsed successfully. Never raises.
    """
    try:
        return _check_for_update(
            current_version=current_version,
            current_image_version=current_image_version,
            fetch_manifest=fetch_manifest,
            verify=verify,
            clock=clock,
            rng=rng,
            last_check_path=last_check_path,
            force=force,
            bad_versions=bad_versions,
            force_reinstall=force_reinstall,
        )
    except Exception as e:  # noqa: BLE001 - the check must never raise
        log.debug('updater_install: check_for_update failed: %s', type(e).__name__)
        return CheckResult(True, CHECK_ERROR, None, 'update check failed', 0.0)


def _check_for_update(*, current_version, current_image_version,
                      fetch_manifest, verify, clock, rng, last_check_path,
                      force, bad_versions, force_reinstall):
    now = _now(clock)
    interval = _jittered_interval(rng)
    last = _read_last_check(last_check_path)
    if not force and last is not None and now - last < interval:
        return CheckResult(
            False, CHECK_UP_TO_DATE, None,
            'update check is not due yet', last + interval)

    ok, payload = _effect(fetch_manifest)
    if not ok:
        return CheckResult(
            True, CHECK_ERROR, None,
            _redact(payload or 'could not fetch the update manifest', ()), now)

    if verify is not None:
        ok, reason = _effect(verify, payload)
        if not ok:
            _write_last_check(last_check_path, now, CHECK_INVALID)
            return CheckResult(
                True, CHECK_INVALID, None,
                _redact(reason or 'manifest signature verification failed', ()),
                now + interval)

    try:
        # Parse for structure/SemVer only; the installed-version relationship
        # (available / up-to-date / downgrade / incompatible) is decided by
        # classify_manifest below. Passing current_version here would turn an
        # equal or older release into a parse error instead of a classification.
        manifest = updater.parse_manifest(payload)
    except updater.ManifestError as e:
        _write_last_check(last_check_path, now, CHECK_INVALID)
        return CheckResult(
            True, CHECK_INVALID, None,
            _redact(str(e) or 'manifest is invalid', ()), now + interval)
    except Exception as e:  # noqa: BLE001 - any parse failure is bounded
        log.debug('updater_install: manifest parse failed: %s', type(e).__name__)
        _write_last_check(last_check_path, now, CHECK_INVALID)
        return CheckResult(True, CHECK_INVALID, None, 'manifest is invalid',
                           now + interval)

    if not force_reinstall and _is_bad_version(manifest, bad_versions):
        _write_last_check(last_check_path, now, CHECK_SUPPRESSED)
        return CheckResult(
            True, CHECK_SUPPRESSED, manifest,
            'release is marked bad; reinstall requires an explicit force',
            now + interval)

    classification = updater.classify_manifest(
        manifest,
        current_version=current_version,
        current_image_version=current_image_version,
    )
    if classification.ok:
        _write_last_check(last_check_path, now, CHECK_AVAILABLE)
        return CheckResult(
            True, CHECK_AVAILABLE, manifest, '', now + interval)

    up_to_date = classification.outcome == updater.OUTCOME_UP_TO_DATE
    outcome = CHECK_UP_TO_DATE if up_to_date else CHECK_INVALID
    _write_last_check(last_check_path, now, outcome)
    return CheckResult(
        True, outcome, manifest,
        _redact(classification.reason or 'manifest is not installable', ()),
        now + interval)


def _is_bad_version(manifest, bad_versions):
    """True when ``manifest.version`` is in the ``bad_versions`` collection."""
    if not bad_versions:
        return False
    version = getattr(manifest, 'version', None)
    try:
        return version in frozenset(bad_versions)
    except TypeError:
        return False


# --------------------------------------------------------------------------- #
# install_update (§7.2 steps 3-11)
# --------------------------------------------------------------------------- #

def _manifest_version(manifest):
    value = getattr(manifest, 'version', None)
    try:
        updater.parse_semver(value)
    except (ValueError, TypeError):
        return None
    return value


def _failed(paths, version, secrets, reason):
    return InstallResult(
        False, INSTALL_FAILED, _active_version(paths), version, False,
        _redact(reason, secrets))


def _wait_for_health(health_check, clock, sleeper, version):
    """Poll ``health_check`` until healthy or :data:`HEALTH_TIMEOUT_SECONDS`."""
    deadline = _now(clock) + HEALTH_TIMEOUT_SECONDS
    for _attempt in range(MAX_HEALTH_ATTEMPTS):
        try:
            if _health_ok(health_check(version)):
                return True
        except Exception as e:  # noqa: BLE001 - an unhealthy probe is a failure
            log.debug('updater_install: health probe failed: %s', type(e).__name__)
        now = _now(clock)
        if now >= deadline:
            return False
        delay = min(HEALTH_POLL_SECONDS, deadline - now)
        if delay <= 0:
            return False
        try:
            sleeper(delay)
        except Exception:  # noqa: BLE001 - a broken sleeper ends the wait
            return False
    return False


def install_update(manifest, *, paths, download, verify_manifest_signature,
                   verify_bundle_signature, free_space, extract, build_venv,
                   preflight, switch, health_check, restart_services, record_bad,
                   prune, clock, sleeper, secrets=(), current_version='',
                   current_image_version='', force_reinstall=False):
    """Install a validated, signed update (§7.2 steps 3-11).

    Returns an :class:`InstallResult` and never raises. ``secrets`` is an
    iterable of strings that must never appear in a returned reason or log line.
    ``current_version``/``current_image_version`` default to the active release
    and to "unknown image" respectively; when supplied they are re-checked
    (downgrade/equal/incompatible-image) before any staging work. A version with
    a ``.bad`` marker is refused unless ``force_reinstall`` is set.
    """
    secrets = tuple(s for s in secrets if isinstance(s, str) and s)
    try:
        return _install_update(
            manifest,
            paths=paths,
            download=download,
            verify_manifest_signature=verify_manifest_signature,
            verify_bundle_signature=verify_bundle_signature,
            free_space=free_space,
            extract=extract,
            build_venv=build_venv,
            preflight=preflight,
            switch=switch,
            health_check=health_check,
            restart_services=restart_services,
            record_bad=record_bad,
            prune=prune,
            clock=clock,
            sleeper=sleeper,
            secrets=secrets,
            current_version=current_version,
            current_image_version=current_image_version,
            force_reinstall=force_reinstall,
        )
    except Exception as e:  # noqa: BLE001 - the install must never raise
        log.debug('updater_install: install_update failed: %s', type(e).__name__)
        return InstallResult(
            False, INSTALL_FAILED, '', '', False,
            _redact(f'install failed ({type(e).__name__})', secrets))


def _install_update(manifest, *, paths, download, verify_manifest_signature,
                    verify_bundle_signature, free_space, extract, build_venv,
                    preflight, switch, health_check, restart_services, record_bad,
                    prune, clock, sleeper, secrets, current_version,
                    current_image_version, force_reinstall):
    version = _manifest_version(manifest)
    if version is None:
        return _failed(paths, '', secrets, 'manifest is invalid')
    sha = getattr(manifest, 'bundle_sha256', None)
    size = getattr(manifest, 'bundle_size', None)
    if not _is_sha256(sha) or not _valid_bundle_size(size):
        return _failed(paths, version, secrets, 'manifest is invalid')

    if not force_reinstall and version in read_bad_versions(paths):
        return _failed(
            paths, version, secrets,
            'release is marked bad; reinstall requires an explicit force')

    installed = current_version or _active_version(paths)
    classification = updater.classify_manifest(
        manifest,
        current_version=installed,
        current_image_version=current_image_version,
    )
    if not classification.ok:
        return _failed(
            paths, version, secrets,
            classification.reason or 'manifest is not installable')

    staging = None
    try:
        try:
            os.makedirs(paths.releases_dir, exist_ok=True)
            staging = tempfile.mkdtemp(
                prefix=f'.{version}.staging.', dir=paths.releases_dir)
        except OSError as e:
            return _failed(
                paths, version, secrets,
                f'could not create the staging directory ({type(e).__name__})')

        free = _free_bytes(free_space, paths.releases_dir)
        if free is None:
            return _failed(paths, version, secrets, 'could not determine free space')
        if free < required_free_space(size):
            return _failed(paths, version, secrets, 'insufficient free space')

        # --- download + verify BEFORE extracting (steps 3-4) ---------------
        ok, value = _effect(download, manifest, staging)
        if not ok:
            return _failed(paths, version, secrets, value or 'bundle download failed')
        bundle_path = (
            value if isinstance(value, str) and value
            else os.path.join(staging, BUNDLE_FILENAME)
        )
        if not os.path.isfile(bundle_path):
            return _failed(paths, version, secrets, 'downloaded bundle is missing')

        ok, reason = _effect(verify_manifest_signature, manifest, staging)
        if not ok:
            return _failed(
                paths, version, secrets,
                reason or 'manifest signature verification failed')

        ok, reason = _effect(verify_bundle_signature, bundle_path)
        if not ok:
            return _failed(
                paths, version, secrets,
                reason or 'bundle signature verification failed')

        try:
            actual_size = os.path.getsize(bundle_path)
        except OSError:
            return _failed(paths, version, secrets, 'bundle could not be read')
        if actual_size != size:
            return _failed(paths, version, secrets, 'bundle size mismatch')

        # SHA-256 is verified inside extract_bundle before decompression.
        ok, reason = _effect(extract, bundle_path, staging, expected_sha256=sha)
        if not ok:
            return _failed(
                paths, version, secrets, reason or 'bundle extraction failed')
        _remove_file(bundle_path)

        # --- venv + preflight (steps 6-7) ---------------------------------
        ok, reason = _effect(build_venv, staging, manifest)
        if not ok:
            return _failed(
                paths, version, secrets, reason or 'release venv build failed')

        ok, reason = _effect(preflight, staging, manifest)
        if not ok:
            return _failed(
                paths, version, secrets, reason or 'release preflight failed')

        # --- atomic switch (step 8) ---------------------------------------
        previous_version = _active_version(paths)
        ok, reason = _effect(switch, paths, staging, version)
        if not ok:
            return _failed(
                paths, version, secrets, reason or 'release activation failed')

        # --- restart + health (steps 9-10) --------------------------------
        ok, reason = _effect(restart_services, version)
        if not ok:
            return _roll_back(
                paths, version, secrets,
                reason or 'service restart failed; restored the previous release',
                restart_services, record_bad)

        if not _wait_for_health(health_check, clock, sleeper, version):
            return _roll_back(
                paths, version, secrets,
                'health checks failed; restored the previous release',
                restart_services, record_bad)

        # --- prune only after a successful health check (step 11) ---------
        protected = tuple(v for v in (version, previous_version) if v)
        _effect(prune, paths, protected)
        return InstallResult(
            True, INSTALL_INSTALLED, version, version,
            bool(getattr(manifest, 'reboot_required', False)), '')
    finally:
        # Any failure path removes the staging directory; after a successful
        # switch the staging path no longer exists (it was renamed).
        if staging and os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _roll_back(paths, version, secrets, reason, restart_services, record_bad):
    """Restore ``previous``, restart it, and mark ``version`` bad."""
    _rollback(paths)
    restored = _active_version(paths)
    _effect(restart_services, restored or version)
    _effect(record_bad, version, reason)
    return InstallResult(
        False, INSTALL_ROLLED_BACK, restored, version, False,
        _redact(reason, secrets))


# --------------------------------------------------------------------------- #
# Home Assistant JSON update state (AC-31; wiring is WP-R4c)
# --------------------------------------------------------------------------- #

def _percentage(value, in_progress):
    """Normalize ``update_percentage`` to ``None`` or a bounded 0-100 number."""
    if not in_progress:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number:  # NaN
        return None
    if number < 0:
        return 0
    if number > 100:
        return 100
    return int(number) if number.is_integer() else round(number, 2)


def update_state_document(*, installed_version, latest_version, release_summary,
                          release_url, in_progress, update_percentage):
    """Return the Home Assistant JSON update schema (secret-free).

    Keys: ``installed_version``, ``latest_version``, ``release_summary``,
    ``release_url``, ``in_progress`` and ``update_percentage``. Text fields are
    bounded and control-character-free; no secret is ever added.
    """
    return {
        'installed_version': _bounded_text(
            installed_version, updater.MAX_VERSION_LENGTH),
        'latest_version': _bounded_text(
            latest_version, updater.MAX_VERSION_LENGTH),
        'release_summary': _bounded_text(
            release_summary, updater.MAX_SUMMARY_LENGTH),
        'release_url': _bounded_text(release_url, updater.MAX_URL_LENGTH),
        'in_progress': bool(in_progress),
        'update_percentage': _percentage(update_percentage, bool(in_progress)),
    }


#: The exact HA update-schema keys the persisted state file may carry. Reads are
#: projected onto this allowlist so an unexpected key (for example a secret a
#: future writer might add) can never reach the MQTT publisher.
UPDATE_STATE_KEYS = (
    'installed_version', 'latest_version', 'release_summary', 'release_url',
    'in_progress', 'update_percentage',
)


def read_update_state(path=DEFAULT_UPDATE_STATE_PATH):
    """Read and normalize the persisted HA update-state document.

    Returns a dict with exactly :data:`UPDATE_STATE_KEYS`, or ``None`` when the
    file is absent, unreadable, oversized, malformed JSON, or not a JSON object.
    Bounded and never raises. The value is re-normalized through
    :func:`update_state_document` so unknown keys and control characters cannot
    reach the MQTT publisher.
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, 'rb') as handle:
            raw = handle.read(MAX_UPDATE_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_UPDATE_STATE_BYTES:
        return None
    try:
        document = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return update_state_document(
        installed_version=document.get('installed_version'),
        latest_version=document.get('latest_version'),
        release_summary=document.get('release_summary'),
        release_url=document.get('release_url'),
        in_progress=document.get('in_progress', False),
        update_percentage=document.get('update_percentage'),
    )


def write_update_state(document, path=DEFAULT_UPDATE_STATE_PATH):
    """Atomically write the HA update-state document; never raises.

    The state area is root-owned (only the root updater writes this file) and the
    document is written as mode 0644 so the unprivileged ``prusa-cam`` app can
    read it. The write goes through a temp file + ``os.replace`` so a concurrent
    reader never observes a partially written document. Returns ``True`` on
    success; a non-dict document, a non-string/empty path, an oversized
    encoding, or any filesystem error returns ``False``.
    """
    if not isinstance(document, dict) or not isinstance(path, str) or not path:
        return False
    try:
        payload = json.dumps(document, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError):
        return False
    if len(payload) > MAX_UPDATE_STATE_BYTES:
        return False
    tmp = None
    try:
        directory = os.path.dirname(path) or '.'
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=os.path.basename(path) + '.', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError:
            _remove_file(tmp)
            return False
        return True
    except OSError:
        _remove_file(tmp)
        return False



# --------------------------------------------------------------------------- #
# Real callables for the CLI (never used by the tests)
# --------------------------------------------------------------------------- #

def default_free_space(path):
    """Free bytes on the filesystem holding ``path``."""
    return shutil.disk_usage(path).free


def default_download(manifest, staging_dir):
    """Download the bundle *and* its detached signature into ``staging_dir``.

    Fetches both ``manifest.bundle_url`` (to ``bundle.tar.zst``) and
    ``manifest.bundle_url + '.minisig'`` (to ``bundle.tar.zst.minisig``), which
    is the default signature path :func:`updater.verify_file` looks for. Bounded
    and https-only; on any failure both partial files are removed.
    """
    url = getattr(manifest, 'bundle_url', None)
    if not isinstance(url, str) or not url:
        return False, 'bundle url is missing'
    size_limit = getattr(manifest, 'bundle_size', 0)
    if not _valid_bundle_size(size_limit):
        return False, 'bundle size is invalid'
    target = os.path.join(staging_dir, BUNDLE_FILENAME)
    signature = target + BUNDLE_SIGNATURE_SUFFIX
    try:
        _download_to(url, target, size_limit)
        _download_to(url + BUNDLE_SIGNATURE_SUFFIX, signature,
                     MAX_SIGNATURE_DOWNLOAD_BYTES)
    except Exception as e:  # noqa: BLE001 - download must never raise
        _remove_file(target)
        _remove_file(signature)
        log.debug('updater_install: download failed: %s', type(e).__name__)
        return False, f'bundle download failed ({type(e).__name__})'
    return True, target


def default_build_venv(staging_dir, manifest):
    """Build the release venv from bundled, hash-pinned wheels."""
    venv_dir = os.path.join(staging_dir, 'venv')
    lock = os.path.join(staging_dir, 'requirements.lock')
    wheels = os.path.join(staging_dir, 'wheels')
    try:
        result = subprocess.run(
            [PYTHON_BINARY, '-m', 'venv', '--system-site-packages', venv_dir],
            capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except Exception as e:  # noqa: BLE001 - venv build must never raise
        return False, f'release venv creation failed ({type(e).__name__})'
    if result.returncode != 0:
        return False, f'release venv creation failed (exit {result.returncode})'
    if not os.path.isfile(lock):
        return True, ''
    pip = os.path.join(venv_dir, 'bin', 'pip')
    args = [
        pip, 'install', '--require-hashes', '--no-cache-dir', '--no-index',
        '--find-links', wheels, '-r', lock,
    ]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except Exception as e:  # noqa: BLE001 - dependency install must never raise
        return False, f'release dependency install failed ({type(e).__name__})'
    if result.returncode != 0:
        return False, f'release dependency install failed (exit {result.returncode})'
    return True, ''


def _default_preflight_runner(args, timeout):
    """Run a preflight command with a bounded timeout (text output)."""
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout, check=False)


def _preflight_python(staging_dir, python):
    """Release interpreter: explicit override, release venv, then system Python."""
    if isinstance(python, str) and python:
        return python
    venv_python = os.path.join(staging_dir, 'venv', 'bin', 'python')
    if os.path.isfile(venv_python):
        return venv_python
    return PYTHON_BINARY


def _preflight_returncode(result):
    """Best-effort return code of a runner result (missing means failure)."""
    code = getattr(result, 'returncode', 1)
    return code if isinstance(code, int) and not isinstance(code, bool) else 1


def _run_preflight_command(runner, args, timeout):
    """Call ``runner``; return ``(result_or_None, exception_name)``; never raises."""
    try:
        return runner(list(args), timeout), ''
    except Exception as e:  # noqa: BLE001 - a broken runner is a bounded failure
        return None, type(e).__name__


def _preflight_reason(prefix, exception_name):
    """Append a bounded exception class name when it is a plain identifier."""
    if isinstance(exception_name, str) and _IDENTIFIER_RE.match(exception_name):
        return _sanitize(f'{prefix} ({exception_name})')
    return prefix


def _import_check_script(staging_dir, modules):
    """Script that imports the release's core modules from the staged tree.

    The staging directory is prepended to ``sys.path`` explicitly so the check
    resolves the release modules regardless of the service working directory
    (which stays ``/opt/prusa-cam``).
    """
    imports = '; '.join(f'import {name}' for name in modules)
    return f'import sys; sys.path.insert(0, {str(staging_dir)!r}); {imports}'


def _migration_dry_run_script(staging_dir, config_path):
    """Script that parses + migrates the durable device document, secret-free.

    Loads ``config_path`` with the release's :mod:`config_schema`, migrates it up
    to the release's ``SCHEMA_VERSION``, and exits non-zero on any failure. Only
    the exception *class name* is ever written to stderr, never a document value,
    so the dry-run cannot leak configuration contents or secrets.
    """
    return (
        'import sys\n'
        f'sys.path.insert(0, {str(staging_dir)!r})\n'
        'import config_schema as _c\n'
        'try:\n'
        f'    with open({str(config_path)!r}, encoding="utf-8") as _h:\n'
        '        _text = _h.read()\n'
        '    _cfg = _c.parse_device(_text)\n'
        '    _c.migrate_device(_cfg, _cfg.get("schema_version", 1))\n'
        'except Exception as _e:\n'
        '    sys.stderr.write(type(_e).__name__)\n'
        '    sys.exit(1)\n'
    )


def default_preflight(staging_dir, manifest, *, runner=None, python=None,
                      config_path=DEFAULT_DEVICE_CONFIG_PATH):
    """Compile, import-check, and migration-dry-run the staged release.

    §7.2 step 7. In order:

    1. ``main.py`` must be present (the launcher entry point).
    2. ``compileall`` over the staged tree.
    3. An **import check** that imports the release's stdlib-only core modules
       (:data:`PREFLIGHT_IMPORT_MODULES`) with the release interpreter.
    4. A **migration dry-run** that parses the durable device document at
       ``config_path`` and runs :func:`config_schema.migrate_device` up to the
       release's ``SCHEMA_VERSION``. A missing document (first install /
       unclaimed device) skips the dry-run and still passes. The dry-run never
       prints configuration contents or secrets; only the exception class name
       reaches the bounded reason.

    Returns ``(ok, reason)`` and never raises. ``runner(args, timeout)`` is
    injectable so tests never invoke a real subprocess; ``python`` overrides the
    release interpreter and ``config_path`` overrides the device document.
    """
    try:
        return _default_preflight(
            staging_dir, manifest, runner, python, config_path)
    except Exception as e:  # noqa: BLE001 - preflight must never raise
        log.debug('updater_install: preflight failed: %s', type(e).__name__)
        return False, 'release preflight failed'


def _default_preflight(staging_dir, manifest, runner, python, config_path):
    if not os.path.isfile(os.path.join(staging_dir, 'main.py')):
        return False, 'release is missing main.py'
    interpreter = _preflight_python(staging_dir, python)
    runner = runner or _default_preflight_runner

    result, exception_name = _run_preflight_command(
        runner, [interpreter, '-m', 'compileall', '-q', staging_dir],
        PREFLIGHT_TIMEOUT_SECONDS)
    if result is None:
        return False, _preflight_reason('release preflight failed', exception_name)
    if _preflight_returncode(result) != 0:
        return False, 'release preflight compilation failed'

    script = _import_check_script(staging_dir, PREFLIGHT_IMPORT_MODULES)
    result, exception_name = _run_preflight_command(
        runner, [interpreter, '-c', script], PREFLIGHT_TIMEOUT_SECONDS)
    if result is None:
        return False, _preflight_reason('release import check failed', exception_name)
    if _preflight_returncode(result) != 0:
        return False, 'release core modules failed to import'

    if isinstance(config_path, str) and config_path and os.path.isfile(config_path):
        script = _migration_dry_run_script(staging_dir, config_path)
        result, exception_name = _run_preflight_command(
            runner, [interpreter, '-c', script], PREFLIGHT_TIMEOUT_SECONDS)
        if result is None:
            return False, _preflight_reason(
                'release configuration migration dry-run failed', exception_name)
        if _preflight_returncode(result) != 0:
            return False, _migration_dry_run_reason(result)
    return True, ''


def _migration_dry_run_reason(result):
    """Bounded, non-secret reason from the dry-run's stderr (class name only)."""
    base = 'release configuration migration dry-run failed'
    detail = _bounded_text(getattr(result, 'stderr', ''), MAX_REASON_LENGTH)
    if detail and _IDENTIFIER_RE.match(detail):
        return _sanitize(f'{base} ({detail})')
    return base


def _parse_endpoints(raw):
    """Parse ``host:port,...`` tolerantly, skipping malformed entries.

    Empty entries, entries without a port, an empty host, a non-numeric port and
    an out-of-range port are all dropped rather than raising.
    """
    endpoints = []
    for item in str(raw).split(','):
        item = item.strip()
        if not item or ':' not in item:
            continue
        host, _, port = item.rpartition(':')
        host = host.strip()
        if not host:
            continue
        try:
            number = int(port)
        except ValueError:
            continue
        if not 1 <= number <= 65535:
            continue
        endpoints.append((host, number))
    return endpoints


def default_health_check(version):
    """Local-only health probe (source/local HTTP/ONVIF/RTSP/app).

    The default endpoint is the release app's own local HTTP/ONVIF facade on
    port 80 (started by the release ``main.py``), so a pass proves the newly
    activated release is serving rather than an unrelated process. Prusa cloud
    reachability is deliberately excluded (an Internet outage must never trigger
    a rollback).

    RTSP ports (:data:`RTSP_HEALTH_PORTS`, 8554/8555) are intentionally *not* in
    the default set, so a device with RTSP disabled still passes. Operators who
    want RTSP/ONVIF coverage list them through :data:`HEALTH_ENDPOINTS_ENV`
    (``host:port,...``); every listed endpoint must then pass. Returns ``True``
    only when every endpoint accepts a TCP connection.
    """
    raw = os.environ.get(HEALTH_ENDPOINTS_ENV)
    if raw:
        endpoints = _parse_endpoints(raw)
    else:
        # The defaults are documented in the same ``host:port`` form as the
        # environment override, so they go through the same tolerant parser.
        endpoints = _parse_endpoints(','.join(DEFAULT_HEALTH_ENDPOINTS))
    if not endpoints:
        return False
    for host, port in endpoints:
        try:
            with socket.create_connection(
                    (host, port), timeout=HEALTH_PROBE_TIMEOUT_SECONDS):
                pass
        except OSError:
            return False
    return True


def default_restart_services(version):
    """Restart the camera runtime so it picks up the active release."""
    binary = _resolve_binary(SYSTEMCTL_BINARY, 'systemctl')
    if binary is None:
        return False, 'systemctl is unavailable'
    try:
        result = subprocess.run(
            [binary, 'restart', 'prusa-camera.target'],
            capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except Exception as e:  # noqa: BLE001 - restart must never raise
        return False, f'service restart failed ({type(e).__name__})'
    if result.returncode != 0:
        return False, f'service restart failed (exit {result.returncode})'
    return True, ''


def record_bad_release(paths, version, reason):
    """Write a bounded marker for a release that failed health checks."""
    try:
        directory = os.path.join(paths.releases_dir, '.bad')
        os.makedirs(directory, exist_ok=True)
        marker = os.path.join(directory, f'{version}.json')
        tmp = f'{marker}.tmp.{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump({'version': version, 'reason': _sanitize(reason)}, handle)
        os.replace(tmp, marker)
        return True
    except OSError:
        return False


def default_prune(paths, protected_versions):
    """Remove old SemVer release directories, never current/previous/factory."""
    protected = set(protected_versions or ())
    active = _active_version(paths)
    if active:
        protected.add(active)
    try:
        previous = os.path.realpath(paths.previous_link)
        if os.path.isdir(previous):
            protected.add(os.path.basename(previous.rstrip('/')))
    except OSError:
        pass

    try:
        entries = os.listdir(paths.releases_dir)
    except OSError:
        return []
    removed = []
    for name in entries:
        if name in protected or name in ('current', 'previous') or name.startswith('.'):
            continue
        path = os.path.join(paths.releases_dir, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            updater.parse_semver(name)
        except (ValueError, TypeError):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)
    return removed


# --------------------------------------------------------------------------- #
# CLI (check / install)
# --------------------------------------------------------------------------- #

def _download_to(url, target, limit):
    """Download ``url`` to ``target``, capped at ``limit`` bytes."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        if not str(response.geturl()).startswith('https://'):
            raise ValueError('url must be https')
        written = 0
        with open(target, 'wb') as handle:
            while True:
                block = response.read(65536)
                if not block:
                    break
                written += len(block)
                if written > limit:
                    raise ValueError('download exceeds the limit')
                handle.write(block)


def _paths_from_data_root(data_root):
    releases = os.path.join(data_root, 'releases')
    return InstallPaths(
        releases_dir=releases,
        current_link=os.path.join(releases, 'current'),
        previous_link=os.path.join(releases, 'previous'),
        factory_app=DEFAULT_FACTORY_APP,
        state_dir=data_root,
    )


def _state_path(args):
    """State-file path: explicit ``--state-path`` or ``<data-root>/update-state.json``."""
    explicit = getattr(args, 'state_path', '')
    if isinstance(explicit, str) and explicit:
        return explicit
    return os.path.join(args.data_root, 'update-state.json')


def _install_state_document(installed_version, manifest=None, reason=''):
    """Build the HA update-state document written after a CLI run.

    ``manifest`` supplies the offered version/summary/URL when an installable or
    up-to-date release was seen; otherwise ``reason`` (already bounded by the
    caller) is surfaced as the summary so HA reflects an error/rejection.
    """
    latest = getattr(manifest, 'version', '') if manifest is not None else ''
    summary = getattr(manifest, 'release_summary', '') if manifest is not None else ''
    url = getattr(manifest, 'release_url', '') if manifest is not None else ''
    if not latest and reason:
        summary = reason
    return update_state_document(
        installed_version=installed_version,
        latest_version=latest,
        release_summary=summary or '',
        release_url=url or '',
        in_progress=False,
        update_percentage=None,
    )


def _write_state(args, installed_version, manifest=None, reason=''):
    """Best-effort persist of the HA update-state document; never raises."""
    write_update_state(
        _install_state_document(installed_version, manifest, reason),
        path=_state_path(args))


def _cli_check(args):
    # Repair any interrupted previous update before deciding availability; the
    # service also runs this via ExecStartPre. Idempotent and best-effort.
    paths = _paths_from_data_root(args.data_root)
    recover_interrupted(paths)
    current_version = args.current_version or app_version.application_version()
    if not args.manifest_url:
        # No manifest URL configured (the root-owned /etc/prusa-updater.conf is
        # unset): a scheduled check is a no-op, never a usage error. The state
        # file is still refreshed so HA reflects the installed version.
        print('updater: no update manifest URL is configured; nothing to check')
        _write_state(args, current_version)
        return 0
    tmp = tempfile.mkdtemp(prefix='buddy3d-update-check-')
    try:
        manifest_path = os.path.join(tmp, 'update-manifest.json')
        sig_path = manifest_path + updater.DEFAULT_SIGNATURE_SUFFIX
        try:
            _download_to(args.manifest_url, manifest_path,
                         MAX_MANIFEST_DOWNLOAD_BYTES)
            _download_to(args.manifest_url + updater.DEFAULT_SIGNATURE_SUFFIX,
                         sig_path, MAX_MANIFEST_DOWNLOAD_BYTES)
            with open(manifest_path, 'rb') as handle:
                payload = handle.read(MAX_MANIFEST_DOWNLOAD_BYTES + 1)
        except Exception as e:  # noqa: BLE001 - fetch failures are reported
            print(f'updater: could not fetch the update manifest '
                  f'({type(e).__name__})', file=sys.stderr)
            _write_state(args, current_version,
                         reason=f'manifest fetch failed ({type(e).__name__})')
            return 1

        def fetch_manifest():
            return True, payload

        def verify(_payload):
            return updater.verify_file(
                manifest_path, signature_path=sig_path,
                public_key_path=DEFAULT_PUBLIC_KEY_PATH)

        result = check_for_update(
            current_version=current_version,
            current_image_version=args.current_image_version,
            fetch_manifest=fetch_manifest,
            verify=verify,
            clock=time.time,
            rng=random.Random(),
            last_check_path=args.last_check_path,
            force=args.force,
            bad_versions=read_bad_versions(paths),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if result.outcome == CHECK_AVAILABLE:
        _write_state(args, current_version, manifest=result.manifest)
        print(f'updater: update {result.manifest.version} is available')
        return 0
    if result.outcome == CHECK_UP_TO_DATE:
        _write_state(args, current_version, manifest=result.manifest,
                     reason=result.reason)
        print(f'updater: {result.reason or "no update available"}')
        return 0
    if result.outcome == CHECK_SUPPRESSED:
        _write_state(args, current_version, reason=result.reason)
        print(f'updater: {result.reason or "no update available"}')
        return 0
    _write_state(args, current_version, reason=result.reason or result.outcome)
    print(f'updater: update check failed: {result.reason}', file=sys.stderr)
    return 1


def _cli_install(args):
    current_version = args.current_version or app_version.application_version()
    tmp = None
    try:
        manifest_path = args.manifest
        if manifest_path:
            if not os.path.isfile(manifest_path):
                print(f'updater: manifest not found: {manifest_path}',
                      file=sys.stderr)
                _write_state(args, current_version, reason='manifest not found')
                return 2
        elif args.manifest_url:
            # The root service starts ``install`` with no positional manifest; it
            # fetches the signed manifest from the configured (root-owned)
            # PRUSA_UPDATE_MANIFEST_URL. The signature is read from the default
            # ``<manifest>.minisig`` path that ``updater.verify_file`` expects.
            tmp = tempfile.mkdtemp(prefix='buddy3d-update-install-')
            manifest_path = os.path.join(tmp, 'update-manifest.json')
            sig_path = manifest_path + updater.DEFAULT_SIGNATURE_SUFFIX
            try:
                _download_to(args.manifest_url, manifest_path,
                             MAX_MANIFEST_DOWNLOAD_BYTES)
                _download_to(args.manifest_url + updater.DEFAULT_SIGNATURE_SUFFIX,
                             sig_path, MAX_MANIFEST_DOWNLOAD_BYTES)
            except Exception as e:  # noqa: BLE001 - fetch failures are reported
                print(f'updater: could not fetch the update manifest '
                      f'({type(e).__name__})', file=sys.stderr)
                _write_state(args, current_version,
                             reason='manifest fetch failed')
                return 1
        else:
            print('updater: no manifest or update manifest URL is configured',
                  file=sys.stderr)
            _write_state(args, current_version, reason='no manifest configured')
            return 2

        # The signing key is fixed in the image (AC-32); it is never taken from
        # the environment or a CLI flag, so a service-writable config cannot
        # redirect it.
        ok, reason = updater.verify_file(
            manifest_path, public_key_path=DEFAULT_PUBLIC_KEY_PATH)
        if not ok:
            print(f'updater: manifest signature verification failed: {reason}',
                  file=sys.stderr)
            _write_state(args, current_version, reason=reason)
            return 1
        try:
            with open(manifest_path, encoding='utf-8') as handle:
                payload = handle.read(updater.MAX_MANIFEST_BYTES + 1)
            manifest = updater.parse_manifest(payload)
        except (OSError, updater.ManifestError) as e:
            print(f'updater: invalid manifest: {e}', file=sys.stderr)
            _write_state(args, current_version, reason='invalid manifest')
            return 1

        paths = _paths_from_data_root(args.data_root)
        recover_interrupted(paths)
        result = install_update(
            manifest,
            paths=paths,
            download=default_download,
            verify_manifest_signature=lambda _m, _s: updater.verify_file(
                manifest_path, public_key_path=DEFAULT_PUBLIC_KEY_PATH),
            verify_bundle_signature=lambda path: updater.verify_file(
                path, public_key_path=DEFAULT_PUBLIC_KEY_PATH),
            free_space=default_free_space,
            extract=updater.extract_bundle,
            build_venv=default_build_venv,
            preflight=default_preflight,
            switch=switch_release,
            health_check=default_health_check,
            restart_services=default_restart_services,
            record_bad=lambda version, bad_reason: record_bad_release(
                paths, version, bad_reason),
            prune=default_prune,
            clock=time.time,
            sleeper=time.sleep,
            current_version=current_version,
            current_image_version=args.current_image_version,
            force_reinstall=args.force_reinstall,
        )
        _write_state(args, result.installed_version or current_version,
                     manifest=manifest)
        if result.ok:
            print(f'updater: installed {result.installed_version}')
            return 0
        print(f'updater: install failed ({result.outcome}): {result.reason}',
              file=sys.stderr)
        return 1
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _cli_recover(args):
    paths = _paths_from_data_root(args.data_root)
    report = recover_interrupted(paths)
    if not report.changed:
        print('updater: no interrupted update state to recover')
        return 0
    if report.staging_removed:
        print('updater: removed stale staging: '
              + ', '.join(report.staging_removed))
    if report.orphans_removed:
        print('updater: removed orphan releases: '
              + ', '.join(report.orphans_removed))
    if report.current_repaired:
        print('updater: repaired current symlink -> '
              + (report.current_target or 'factory fallback'))
    return 0


def main(argv=None):
    """CLI entry point used by ``prusa-updater.service``."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        description='Buddy3D signed application updater')
    sub = parser.add_subparsers(dest='command', required=True)

    recover = sub.add_parser(
        'recover', help='repair interrupted update state (idempotent)')
    recover.add_argument('--data-root', default=DATA_ROOT)

    check = sub.add_parser('check', help='check for an update (report-only)')
    check.add_argument(
        '--manifest-url',
        default=os.environ.get(MANIFEST_URL_ENV, ''),
        help=f'signed update-manifest.json URL (default: ${MANIFEST_URL_ENV})')
    check.add_argument('--current-version', default='')
    check.add_argument(
        '--current-image-version',
        default=os.environ.get('PRUSA_IMAGE_VERSION', ''))
    check.add_argument('--last-check-path', default=DEFAULT_LAST_CHECK_PATH)
    check.add_argument('--data-root', default=DATA_ROOT)
    check.add_argument(
        '--state-path', default='',
        help='HA update-state file (default: <data-root>/update-state.json)')
    check.add_argument('--force', action='store_true',
                       help='bypass the 24 h interval (manual request)')

    install = sub.add_parser(
        'install',
        help='install a signed update (from a manifest path or manifest URL)')
    install.add_argument(
        'manifest', nargs='?', default='',
        help='path to update-manifest.json (default: fetch --manifest-url)')
    install.add_argument(
        '--manifest-url',
        default=os.environ.get(MANIFEST_URL_ENV, ''),
        help=f'signed update-manifest.json URL (default: ${MANIFEST_URL_ENV})')
    install.add_argument('--data-root', default=DATA_ROOT)
    install.add_argument(
        '--state-path', default='',
        help='HA update-state file (default: <data-root>/update-state.json)')
    install.add_argument('--current-version', default='')
    install.add_argument(
        '--current-image-version',
        default=os.environ.get('PRUSA_IMAGE_VERSION', ''))
    install.add_argument(
        '--force-reinstall', action='store_true',
        help='reinstall a version previously marked bad')

    args = parser.parse_args(argv)
    if args.command == 'recover':
        return _cli_recover(args)
    if args.command == 'check':
        return _cli_check(args)
    return _cli_install(args)


__all__ = [
    'BAD_RELEASES_DIRNAME',
    'BUNDLE_FILENAME',
    'BUNDLE_SIGNATURE_SUFFIX',
    'CHECK_AVAILABLE',
    'CHECK_ERROR',
    'CHECK_INVALID',
    'CHECK_INTERVAL_SECONDS',
    'CHECK_JITTER_SECONDS',
    'CHECK_SUPPRESSED',
    'CHECK_UP_TO_DATE',
    'CheckResult',
    'DATA_ROOT',
    'DEFAULT_CURRENT_LINK',
    'DEFAULT_DEVICE_CONFIG_PATH',
    'DEFAULT_FACTORY_APP',
    'DEFAULT_LAST_CHECK_PATH',
    'DEFAULT_PREVIOUS_LINK',
    'DEFAULT_PUBLIC_KEY_PATH',
    'DEFAULT_RELEASES_DIR',
    'DEFAULT_STATE_DIR',
    'DEFAULT_UPDATE_STATE_PATH',
    'EXTRACTED_EXPANSION_FACTOR',
    'FREE_SPACE_HEADROOM_BYTES',
    'HEALTH_TIMEOUT_SECONDS',
    'PREFLIGHT_IMPORT_MODULES',
    'PREFLIGHT_TIMEOUT_SECONDS',
    'RTSP_HEALTH_PORTS',
    'INSTALL_FAILED',
    'INSTALL_INSTALLED',
    'INSTALL_ROLLED_BACK',
    'InstallPaths',
    'InstallResult',
    'MAX_SIGNATURE_DOWNLOAD_BYTES',
    'MAX_UPDATE_STATE_BYTES',
    'RecoveryReport',
    'UPDATE_STATE_KEYS',
    'VENV_ALLOWANCE_BYTES',
    'check_for_update',
    'default_build_venv',
    'default_download',
    'default_free_space',
    'default_health_check',
    'default_preflight',
    'default_prune',
    'default_restart_services',
    'install_update',
    'main',
    'read_bad_versions',
    'read_update_state',
    'record_bad_release',
    'recover_interrupted',
    'required_free_space',
    'switch_release',
    'update_state_document',
    'write_update_state',
]


if __name__ == '__main__':
    raise SystemExit(main())
