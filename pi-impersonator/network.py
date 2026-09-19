"""Firmware network helpers (GAP-NETWORK-01).

Recovered 3.1.6 conversion `FUN_00097b38`: an RSSI in dBm maps to a 0..100
quality::

    rssi == 0 or rssi < -99  -> 0
    rssi > -51               -> 100
    otherwise                -> (rssi + 100) * 2

The input is the ``level`` column (dBm) of the ``wlan0`` line in
``/proc/net/wireless`` — not the ``link`` quality column, which the previous
implementation mapped linearly. Stdlib-only and pure so it is host-testable.
"""


def rssi_to_quality(rssi):
    try:
        rssi = int(rssi)
    except (TypeError, ValueError):
        return 0
    if rssi == 0 or rssi < -99:
        return 0
    if rssi > -51:
        return 100
    return (rssi + 100) * 2


def parse_wireless_level(line):
    """Return the ``wlan0`` level (dBm) from a /proc/net/wireless line, or None.

    A line looks like `` wlan0: 0000   54.  -56.  -256  ...`` — after the status
    token the columns are link, level, noise; the level is index 3.
    """
    fields = line.replace(':', ' ').split()
    if len(fields) < 4 or not fields[0].startswith('wlan0'):
        return None
    try:
        return int(float(fields[3].strip('.')))
    except (TypeError, ValueError):
        return None


def signal_quality_from_wireless(path='/proc/net/wireless'):
    """Read the wlan0 RSSI and convert it; 0 when unavailable."""
    try:
        with open(path) as f:
            for line in f:
                level = parse_wireless_level(line)
                if level is not None:
                    return rssi_to_quality(level)
    except OSError:
        pass
    return 0
