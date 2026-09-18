"""Host-testable video-quality control flow (GAP-QUALITY-02).

Firmware ``FUN_00072f08`` always attempts the live resolution change for raw
values 5/6/7, persists only when its flag is nonzero, and updates the in-memory
current value only after the live change succeeds. This module holds that
decision logic with the systemd restart and the persistence backend injected, so
it can be tested without ``gi``/``aiohttp``/systemd.

Review fix 1: a failed live apply must not leave the new tier in the ephemeral
live override file. The previous live-file content (or its absence) is captured
before the write and restored when the restart fails, so a later restart or
process start cannot run the encoder on a tier that was never accepted.
"""
import logging
import subprocess

import quality
from state import RAW_TO_ENUM

log = logging.getLogger('prusa-cam.quality')


def restart_services():
    """Restart the encoder/RTSP units. Returns the process return code (0 = ok)."""
    result = subprocess.run(
        ['sudo', 'systemctl', 'restart', 'rpicam-source.service', 'prusa-rtsp.service'],
        capture_output=True,
    )
    return result.returncode


def _restore_live(previous):
    try:
        quality.restore_live(previous)
    except OSError as e:
        log.error(f'quality: failed to restore live override: {e}')


def apply_live_quality(raw_byte, state, restart):
    """Live-apply raw quality byte; update shared state only on success.

    Captures the previous live override before writing so a failed restart can
    restore it (review fix 1). `restart` is a zero-argument callable returning a
    return code (0 = success); `state` is the shared CameraState.
    """
    qenum = RAW_TO_ENUM.get(raw_byte)
    if qenum is None:
        log.warning(f'quality: unknown raw byte {raw_byte!r}')
        return False
    try:
        previous = quality.read_live()
    except OSError as e:
        log.error(f'quality: cannot read live override before apply: {e}')
        return False
    try:
        quality.write_live(qenum)
    except Exception as e:
        log.error(f'quality: live write failed for enum {qenum}: {e}')
        _restore_live(previous)
        return False
    try:
        returncode = restart()
    except Exception as e:
        log.error(f'quality: live restart raised: {e}')
        _restore_live(previous)
        return False
    if returncode != 0:
        log.error(f'quality: live restart failed (rc={returncode})')
        _restore_live(previous)
        return False
    state.quality = qenum
    return True


def persist_quality(qenum):
    """Persist tier `qenum` for rpicam-source's boot EnvironmentFile (GAP-QUALITY-02)."""
    quality.write_current(qenum)


def handle_quality(raw_byte, persist, live_apply, persist_fn):
    """Shared GAP-QUALITY-02 handler: always live-apply; persist only on flag."""
    qenum = RAW_TO_ENUM.get(raw_byte)
    if qenum is None:
        log.warning(f'quality: unknown raw byte {raw_byte!r}')
        return False
    if not live_apply(raw_byte):
        return False
    if persist:
        try:
            persist_fn(qenum)
        except Exception as e:
            log.error(f'quality: persist failed for enum {qenum}: {e}')
            return False
    return True
