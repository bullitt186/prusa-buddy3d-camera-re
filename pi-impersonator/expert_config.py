"""Expert TOML editing: validate before apply (WP-3d1, AC-20).

Source plan §4.6: an expert may edit the durable TOML configuration through SSH,
then run a supplied validator and a single apply command. Invalid files are
rejected without replacing live configuration.

This module is that host-testable validator/apply core. It is stdlib-only and
import-safe and reuses :mod:`config_schema` for the schema, strict allowlist and
atomic writer, so the expert path cannot drift from the wizard/runtime path.

Contract
--------
* :func:`validate_candidate` parses the candidate text with
  :func:`config_schema.parse_device` / :func:`config_schema.parse_secrets`.
  Unknown keys, invalid values and a too-new schema are rejected with a
  non-secret reason; secrets never appear in a reason.
* :func:`apply_candidate` validates **first** and only then atomically writes
  both documents. An invalid candidate leaves the live files byte-identical.
  If the second write fails, the first is rolled back so a partial failure does
  not leave the live configuration inconsistent.
* :func:`current_config` reads the live documents for the editor UI; a missing
  or invalid file yields schema defaults rather than raising.

Secret hygiene
--------------
Reasons name a field or schema error, never a value. As defence in depth the
parsed secret values are scrubbed from any reason via
:func:`admin_auth.redact` before it is returned.

Stdlib only, and no file/network/thread side effects on import.
"""
import dataclasses
import logging
import os

import admin_auth
import config_schema

log = logging.getLogger('prusa-cam.expert_config')

#: Maximum length of a rejection reason.
_REASON_MAX = 200

#: Tables whose string values are secrets and must be scrubbed from reasons.
_SECRET_TABLES = ('prusa', 'mqtt', 'wifi', 'admin')


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ApplyResult:
    """Outcome of :func:`apply_candidate` (never carries a secret in ``reason``)."""

    ok: bool
    reason: str = ''
    device: dict = dataclasses.field(default_factory=dict)
    secrets: dict = dataclasses.field(default_factory=dict)
    wrote: tuple = ()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _secret_values(secrets):
    """Return the literal secret strings in a normalized secrets document."""
    values = []
    if isinstance(secrets, dict):
        for table in _SECRET_TABLES:
            section = secrets.get(table)
            if isinstance(section, dict):
                for value in section.values():
                    if isinstance(value, str) and value:
                        values.append(value)
    return tuple(values)


def _scrub(reason, secrets=None):
    """Bound a reason and scrub any parsed secret value from it."""
    text = reason if isinstance(reason, str) else ''
    text = text.strip()
    values = _secret_values(secrets)
    if values:
        text = admin_auth.redact(text, values)
    if len(text) > _REASON_MAX:
        text = text[:_REASON_MAX]
    return text


def _read_text(path):
    """Return the text at ``path``, or ``None`` when it does not exist/read."""
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return None


def _restore(path, text, mode):
    """Best-effort restore of ``path`` to ``text`` (``None`` removes it)."""
    try:
        if text is None:
            if os.path.isfile(path):
                os.remove(path)
            return True
        config_schema.write_atomic(path, text, mode=mode)
        return True
    except (config_schema.ConfigError, OSError) as e:
        log.warning(f'expert_config: rollback of {path} failed: {e}')
        return False


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #

def validate_candidate(device_text, secrets_text=None):
    """Validate a candidate device/secrets document pair.

    Returns ``(ok, reason, device, secrets)``. ``device`` is the normalized
    device document and ``secrets`` the normalized secrets document (empty when
    ``secrets_text`` is ``None``). On failure ``reason`` is a bounded, non-secret
    message naming a key/field or schema problem; the returned documents are
    empty.
    """
    try:
        device = config_schema.parse_device(device_text)
    except config_schema.ConfigError as e:
        return False, _scrub(str(e)), {}, {}

    secrets = {}
    if secrets_text is not None:
        try:
            secrets = config_schema.parse_secrets(secrets_text)
        except config_schema.ConfigError as e:
            return False, _scrub(str(e), secrets), {}, {}

    return True, '', device, secrets


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

def apply_candidate(device_text, secrets_text=None,
                    device_path=config_schema.DEVICE_TOML_PATH,
                    secrets_path=config_schema.SECRETS_TOML_PATH):
    """Validate a candidate pair, then atomically apply it.

    Validation happens before any write. An invalid candidate is rejected and
    the live files are left byte-identical. On success both documents are
    written atomically; if the secrets write fails the already-written device
    document is rolled back to its previous content (or removed if it did not
    exist) so the live configuration is never left half-applied.
    """
    ok, reason, device, secrets = validate_candidate(device_text, secrets_text)
    if not ok:
        return ApplyResult(False, reason)

    device_before = _read_text(device_path)
    if not config_schema.save_device(device, path=device_path):
        return ApplyResult(False, 'could not write device configuration')

    wrote = ['device']
    if secrets_text is not None:
        if not config_schema.save_secrets(secrets, path=secrets_path):
            restored = _restore(device_path, device_before, 0o640)
            note = '' if restored else ' (device rollback failed)'
            return ApplyResult(
                False,
                'could not write secrets configuration; device configuration '
                'restored' + note,
            )
        wrote.append('secrets')

    return ApplyResult(True, '', device, secrets, tuple(wrote))


# --------------------------------------------------------------------------- #
# Current live configuration
# --------------------------------------------------------------------------- #

def current_config(device_path=config_schema.DEVICE_TOML_PATH,
                   secrets_path=config_schema.SECRETS_TOML_PATH):
    """Return ``(device, secrets)`` read from the live durable files.

    A missing **or invalid** file yields schema defaults (an empty dict for
    secrets) so the expert editor can always open and correct it. Never raises.
    """
    try:
        device = config_schema.load_device(device_path)
    except config_schema.ConfigError:
        device = config_schema.default_device()
    try:
        secrets = config_schema.load_secrets(secrets_path)
    except config_schema.ConfigError:
        secrets = {}
    return device, secrets
