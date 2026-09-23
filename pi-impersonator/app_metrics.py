"""Runtime metrics for the optional MQTT state document (WP-R2; AC-24).

Source plan §6.3/§6.4: the retained state JSON may carry a small set of
diagnostics. This module owns those four readings and nothing else:

* ``storage_free_bytes``  free space on the durable ``/data`` mount,
* ``wifi_rssi_dbm``       raw ``wlan0`` level from ``/proc/net/wireless``,
* ``cpu_temperature_c``   SoC temperature from the thermal zone,
* ``uptime_seconds``      system uptime from ``/proc/uptime``.

Every helper takes an injectable path/mount, is bounded, and is
**exception-free**: a missing file, a malformed value, or a permission error
returns ``None`` rather than raising. :func:`metrics_provider` returns only the
keys that were actually available, so an unavailable reading is omitted instead
of published as a misleading zero. ``mqtt_service`` filters the result to its
own allowlist (``_STATE_METRIC_KEYS``), so this module stays a pure data source.

Stdlib only, and no file/network/thread side effects on import.
"""
import logging
import shutil

import network

log = logging.getLogger('prusa-cam.metrics')

#: Default paths/mounts on the appliance.
DEFAULT_STORAGE_MOUNT = '/data'
DEFAULT_WIRELESS_PATH = '/proc/net/wireless'
DEFAULT_THERMAL_PATH = '/sys/class/thermal/thermal_zone0/temp'
DEFAULT_UPTIME_PATH = '/proc/uptime'


def storage_free_bytes(mount=DEFAULT_STORAGE_MOUNT):
    """Return the free bytes on ``mount``, or None when it cannot be read.

    ``shutil.disk_usage`` raises ``OSError`` for a missing/unreadable mount;
    that is reported as ``None`` (an unavailable reading) rather than a zero.
    """
    try:
        usage = shutil.disk_usage(mount)
    except Exception as e:  # noqa: BLE001 - a metric must never raise
        log.debug('metrics: disk usage unavailable for %s: %s', mount, e)
        return None
    try:
        return int(usage.free)
    except (TypeError, ValueError):
        return None


def wifi_rssi_dbm(path=DEFAULT_WIRELESS_PATH):
    """Return the raw ``wlan0`` level in dBm, or None when unavailable.

    Reuses :func:`network.wifi_rssi_dbm` so the parsing (and the ``wlan0`` line
    selection) has one implementation.
    """
    try:
        return network.wifi_rssi_dbm(path)
    except Exception as e:  # noqa: BLE001
        log.debug('metrics: wifi rssi unavailable: %s', e)
        return None


def cpu_temperature_c(path=DEFAULT_THERMAL_PATH):
    """Return the SoC temperature in degrees Celsius, or None.

    The thermal zone reports millidegrees (e.g. ``45000`` for 45 °C); the value
    is divided by 1000. A malformed/empty file yields ``None``.
    """
    try:
        with open(path, encoding='ascii', errors='replace') as f:
            raw = f.read(64).strip()
    except OSError as e:
        log.debug('metrics: cpu temperature unavailable: %s', e)
        return None
    try:
        return int(raw) / 1000.0
    except (TypeError, ValueError):
        return None


def uptime_seconds(path=DEFAULT_UPTIME_PATH):
    """Return the system uptime in whole seconds, or None.

    ``/proc/uptime`` is ``<seconds> <idle-seconds>``; only the first field is
    used. A missing or malformed file yields ``None``.
    """
    try:
        with open(path, encoding='ascii', errors='replace') as f:
            fields = f.read(128).split()
    except OSError as e:
        log.debug('metrics: uptime unavailable: %s', e)
        return None
    if not fields:
        return None
    try:
        return int(float(fields[0]))
    except (TypeError, ValueError):
        return None


def metrics_provider(
    storage_mount=DEFAULT_STORAGE_MOUNT,
    wireless_path=DEFAULT_WIRELESS_PATH,
    thermal_path=DEFAULT_THERMAL_PATH,
    uptime_path=DEFAULT_UPTIME_PATH,
):
    """Return the available runtime metrics as a plain dict (never raises).

    Only keys with a real reading are present. The caller (``MqttService``)
    intersects this with its own allowlist, so adding a key here cannot leak an
    unexpected field into the retained state document.
    """
    metrics = {}
    value = storage_free_bytes(storage_mount)
    if value is not None:
        metrics['storage_free_bytes'] = value
    value = wifi_rssi_dbm(wireless_path)
    if value is not None:
        metrics['wifi_rssi_dbm'] = value
    value = cpu_temperature_c(thermal_path)
    if value is not None:
        metrics['cpu_temperature_c'] = value
    value = uptime_seconds(uptime_path)
    if value is not None:
        metrics['uptime_seconds'] = value
    return metrics


__all__ = [
    'DEFAULT_STORAGE_MOUNT',
    'DEFAULT_WIRELESS_PATH',
    'DEFAULT_THERMAL_PATH',
    'DEFAULT_UPTIME_PATH',
    'storage_free_bytes',
    'wifi_rssi_dbm',
    'cpu_temperature_c',
    'uptime_seconds',
    'metrics_provider',
]
