"""One shared settings coordinator (WP-1 AC-4/AC-5).

Every control source (Prusa signaling, web admin, MQTT, startup restore) mutates
the durable settings through one object. The coordinator owns validation,
serialization, live apply, persistence, error reporting, and authoritative-state
publication; the control modules (``quality_control``, ``rtsp_control``,
``webrtc_control``, ``timelapse``) remain the implementation of each transition
and are injected, so this module never spawns a subprocess and stays host
testable.

Rules enforced here (AC-5): a rejected mutation must not persist, must not
publish, must not restart services, and must return the authoritative current
state plus a non-secret reason. No method raises on bad input.

Stdlib only and no file side effects on import.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import quality
import quality_control
import rtsp_control
import settings_store
import timelapse
import webrtc_control
from state import RAW_TO_ENUM

log = logging.getLogger('prusa-cam.settings')

# The durable settings key each transition updates, in ``state.json`` spelling.
_KEY_QUALITY = 'quality_tier'
_KEY_CAMERA_NAME = 'camera_name'
_KEY_SNAPSHOT_INTERVAL = 'snapshot_interval'
_KEY_SNAPSHOT_UPLOAD = 'snapshot_upload_enabled'
_KEY_TIMELAPSE_INTERVAL = 'timelapse_interval'
_KEY_TIMELAPSE_ENABLED = 'timelapse_enabled'
_KEY_TIMELAPSE_FPS = 'timelapse_fps'
_KEY_RTSP_MODE = 'rtsp_mode'
_KEY_WEBRTC_MODE = 'webrtc_mode'


@dataclass
class SettingsResult:
    """Outcome of one coordinator mutation attempt.

    ``state`` is always the authoritative settings snapshot after the attempt:
    the new values on success, the unchanged values on rejection.
    """

    ok: bool
    reason: Optional[str] = None
    changed: list = field(default_factory=list)
    state: dict = field(default_factory=dict)


def persist_state(state):
    """Persist ``state.persistable_state()`` to /data; no-op when unavailable.

    Mirrors the former ``main._save_persisted_state``: returns True only when the
    store wrote the file, False when /data is not mounted or the write failed.
    """
    if not settings_store.available():
        log.debug('settings: /data not mounted; not persisting state')
        return False
    data = state.persistable_state()
    if settings_store.save(data):
        log.info(f'persisted settings: {",".join(sorted(data))}')
        return True
    log.warning('settings: could not persist state')
    return False


class SettingsCoordinator:
    """Single mutation path for the shared ``CameraState`` settings.

    All apply callables are injected so the coordinator holds no hardware or
    systemd knowledge; ``persist``/``publish`` default to the durable-store write
    and ``state.mark_info_dirty``.
    """

    def __init__(self, state, *, persist=None, publish=None,
                 quality_apply=None, quality_persist=None,
                 rtsp_start=None, rtsp_stop=None, rtsp_query=None,
                 webrtc_start=None, webrtc_stop=None,
                 timelapse_enable=None):
        self.state = state
        self._persist = persist if persist is not None else persist_state
        self._publish = publish if publish is not None else state.mark_info_dirty
        self._quality_apply = quality_apply
        self._quality_persist = quality_persist
        self._rtsp_start = rtsp_start
        self._rtsp_stop = rtsp_stop
        self._rtsp_query = rtsp_query
        self._webrtc_start = webrtc_start
        self._webrtc_stop = webrtc_stop
        self._timelapse_enable = (
            timelapse_enable if timelapse_enable is not None else timelapse.apply_enable
        )

    def authoritative(self):
        """Return the durable settings snapshot (the ``state.json`` key set)."""
        return self.state.persistable_state()

    def _result(self, ok, reason=None, changed=None):
        return SettingsResult(
            ok=ok,
            reason=reason,
            changed=list(changed or []),
            state=self.authoritative(),
        )

    def _publish_state(self):
        try:
            self._publish()
        except Exception as e:  # never let a republish failure mask the mutation
            log.warning(f'settings: publish callback failed: {e}')

    def _persist_state(self):
        try:
            return self._persist(self.state)
        except Exception as e:
            log.warning(f'settings: persist callback failed: {e}')
            return False

    def _commit(self, changed):
        """Persist + publish after a successful transition, then report it."""
        self._persist_state()
        self._publish_state()
        return self._result(True, None, changed)

    # -- startup -----------------------------------------------------------

    def restore(self, persisted):
        """Apply persisted settings at startup; never rewrites the file."""
        applied = self.state.apply_persisted(persisted)
        self._publish_state()
        return self._result(True, None, applied)

    # -- quality -----------------------------------------------------------

    def _quality_reason(self, raw_byte):
        """Non-secret rejection reason, derived with no side effects."""
        try:
            qenum = RAW_TO_ENUM.get(raw_byte)
        except TypeError:
            qenum = None
        if qenum is None:
            return 'unknown quality value'
        if not quality.quality_change_allowed(
            self.state.quality, qenum, self.state.turn_online
        ):
            return quality_control.TURN_QUALITY_LOCK_LOG
        return 'live apply failed'

    def set_quality(self, raw_byte, persist=True):
        """Apply a raw quality byte through ``quality_control.handle_quality``.

        ``persist`` is the firmware persistence flag: it gates both the
        EnvironmentFile write (inside ``handle_quality``) and the durable
        ``state.json`` write, preserving the live-only ``change_video_size``
        path. The live apply always runs first; state updates only on success.
        """
        if self._quality_apply is None:
            return self._result(False, 'live apply unavailable')
        try:
            ok = quality_control.handle_quality(
                raw_byte, persist, self._quality_apply, self._quality_persist,
                current_enum=self.state.quality, turn_online=self.state.turn_online,
            )
        except Exception as e:
            log.warning(f'settings: quality apply raised: {e}')
            ok = False
        if not ok:
            return self._result(False, self._quality_reason(raw_byte))
        if not persist:
            # Live-only event path: republish, but do not touch state.json.
            self._publish_state()
            return self._result(True, None, [_KEY_QUALITY])
        return self._commit([_KEY_QUALITY])

    # -- snapshots ---------------------------------------------------------

    def set_snapshot_upload(self, enabled):
        if type(enabled) is not bool:
            return self._result(False, 'snapshot upload flag must be a boolean')
        self.state.snapshot_upload_enabled = enabled
        return self._commit([_KEY_SNAPSHOT_UPLOAD])

    def set_snapshot_interval(self, seconds):
        if not self.state.set_snapshot_interval(seconds):
            return self._result(False, 'snapshot interval must be an int in 10..600')
        return self._commit([_KEY_SNAPSHOT_INTERVAL])

    # -- timelapse ---------------------------------------------------------

    def set_timelapse_enabled(self, action):
        try:
            ok = self._timelapse_enable(action, self.state)
        except Exception as e:
            log.warning(f'settings: timelapse enable raised: {e}')
            ok = False
        if not ok:
            return self._result(False, 'timelapse action not recognized')
        return self._commit([_KEY_TIMELAPSE_ENABLED])

    def set_timelapse_interval(self, seconds):
        if not self.state.set_timelapse_interval(seconds):
            return self._result(False, 'timelapse interval must be an int in 1..3600')
        return self._commit([_KEY_TIMELAPSE_INTERVAL])

    def set_timelapse_fps(self, fps):
        valid = timelapse.valid_fps(fps)
        if valid is None:
            return self._result(False, 'timelapse fps must be an int in 1..30')
        self.state.timelapse_fps = valid
        return self._commit([_KEY_TIMELAPSE_FPS])

    # -- streaming modes ---------------------------------------------------

    def set_rtsp_mode(self, mode):
        try:
            ok = rtsp_control.apply_mode(
                mode, self.state,
                start_service=self._rtsp_start,
                stop_service=self._rtsp_stop,
                query_service=self._rtsp_query,
                persist=rtsp_control.write_mode,
            )
        except Exception as e:
            log.warning(f'settings: rtsp apply raised: {e}')
            ok = False
        if not ok:
            return self._result(False, 'invalid rtsp mode')
        return self._commit([_KEY_RTSP_MODE])

    def set_webrtc_mode(self, mode):
        if type(mode) is not int or mode not in (
            webrtc_control.WEBRTC_DISABLED, webrtc_control.WEBRTC_ENABLED
        ):
            return self._result(False, 'invalid webrtc mode')
        try:
            ok = webrtc_control.apply_mode(
                mode, self.state,
                start_service=self._webrtc_start,
                stop_service=self._webrtc_stop,
            )
        except Exception as e:
            log.warning(f'settings: webrtc apply raised: {e}')
            ok = False
        if not ok:
            return self._result(False, 'webrtc service apply failed')
        return self._commit([_KEY_WEBRTC_MODE])

    # -- identity ----------------------------------------------------------

    def set_camera_name(self, name):
        if not self.state.set_camera_name(name):
            return self._result(False, 'camera name must be a non-empty string')
        return self._commit([_KEY_CAMERA_NAME])
