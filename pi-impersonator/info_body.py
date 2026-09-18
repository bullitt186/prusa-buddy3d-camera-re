"""Pure construction of the ``/c/info`` JSON body from shared ``CameraState``.

Stdlib-only so the body can be unit-tested on the host (GAP-INFO-02). The body
is built from the same ``CameraState`` used by the status encoder and the
snapshot loop, so a name or quality change published on one surface is
consistent with the others.
"""
import json

from features import FEATURES_LIST, FIRMWARE_VERSION, MANUFACTURER, MODEL, TRIGGER_SCHEME


def build_info_body(state, *, mac='', ip='', ssid=''):
    """Return the firmware-shaped ``/c/info`` payload as a dict.

    Resolution comes from ``state.resolution()`` (the shared quality enum), not
    from independent width/height arguments.
    """
    width, height = state.resolution()
    resolution = {'width': width, 'height': height}
    return {
        'config': {
            'path': 'private',
            'name': state.camera_name,
            'driver': 'private',
            'model': MODEL,
            'firmware': FIRMWARE_VERSION,
            'manufacturer': MANUFACTURER,
            'trigger_scheme': TRIGGER_SCHEME,
            'resolution': dict(resolution),
            'network_info': {'wifi_mac': mac, 'wifi_ipv4': ip, 'wifi_ssid': ssid},
        },
        'options': {
            'available_resolutions': [dict(resolution)],
        },
        'capabilities': ['trigger_scheme'],
        'features': list(FEATURES_LIST),
    }


def build_info_json(state, *, mac='', ip='', ssid=''):
    """Serialize :func:`build_info_body` for the HTTP PUT body."""
    return json.dumps(build_info_body(state, mac=mac, ip=ip, ssid=ssid))
