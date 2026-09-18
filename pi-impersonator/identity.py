import hashlib
import re


_MAC_RE = re.compile(r'^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def normalize_wifi_mac(mac):
    """Match lp_app's `%02X:%02X:%02X:%02X:%02X:%02X` formatting."""
    value = mac.strip()
    if not _MAC_RE.fullmatch(value):
        raise ValueError(f'invalid Wi-Fi MAC address: {value!r}')
    return ':'.join(part.upper() for part in value.replace('-', ':').split(':'))


def fingerprint_from_mac(mac):
    """Return the lowercase MD5 wire fingerprint produced by Buddy3D firmware."""
    normalized = normalize_wifi_mac(mac)
    return hashlib.md5(normalized.encode('ascii')).hexdigest()
