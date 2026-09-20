"""Small ONVIF facade for Home Assistant's built-in ONVIF integration.

This is intentionally not presented as certified Profile S conformance. It
implements the device/media calls Home Assistant consumes and keeps XML
construction pure so the host test suite needs no ONVIF or aiohttp package.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import uuid
from urllib.parse import quote
import xml.etree.ElementTree as ET


SOAP = 'http://www.w3.org/2003/05/soap-envelope'
# WS-Discovery 2005/04 uses the older WS-Addressing 2004/08 namespace.
# Home Assistant's WSDiscovery 2.1.x parser intentionally looks for this URI.
WSA = 'http://schemas.xmlsoap.org/ws/2004/08/addressing'
WSD = 'http://schemas.xmlsoap.org/ws/2005/04/discovery'
DN = 'http://www.onvif.org/ver10/network/wsdl'
TDS = 'http://www.onvif.org/ver10/device/wsdl'
TRT = 'http://www.onvif.org/ver10/media/wsdl'
TT = 'http://www.onvif.org/ver10/schema'
TER = 'http://www.onvif.org/ver10/error'

for prefix, namespace in (
    ('s', SOAP), ('wsa', WSA), ('d', WSD), ('dn', DN), ('tds', TDS),
    ('trt', TRT), ('tt', TT), ('ter', TER),
):
    ET.register_namespace(prefix, namespace)


def q(namespace, name):
    return f'{{{namespace}}}{name}'


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


def normalize_mac(value):
    """Return uppercase colon-separated MAC text, or an empty string."""
    if not isinstance(value, str):
        return ''
    compact = re.sub(r'[^0-9A-Fa-f]', '', value)
    if len(compact) != 12:
        return ''
    return ':'.join(compact[i:i + 2] for i in range(0, 12, 2)).upper()


def _pseudo_mac(identifier):
    raw = identifier.bytes[:6]
    raw = bytes([(raw[0] | 0x02) & 0xFE]) + raw[1:]
    return ':'.join(f'{byte:02X}' for byte in raw)


@dataclass
class OnvifContext:
    """Dynamic values exposed through ONVIF without cloud credentials."""

    state: object
    ipv4: str
    mac: str
    endpoint_uuid: uuid.UUID
    serial: str
    firmware: str
    manufacturer: str = 'Niceboy'
    model: str = 'Buddy3D-C1'
    http_port: int = 80
    rtsp_port: int = 8555
    frame_rate: int = 30
    discovery_message_number: int = 0

    @classmethod
    def create(cls, state, ipv4, mac, firmware, stable_seed=''):
        normalized = normalize_mac(mac)
        seed = normalized or str(stable_seed) or 'prusa-camera-fallback'
        endpoint_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f'prusa-camera:{seed}')
        exposed_mac = normalized or _pseudo_mac(endpoint_uuid)
        serial = normalized.replace(':', '') if normalized else endpoint_uuid.hex
        return cls(state, ipv4, exposed_mac, endpoint_uuid, serial, firmware)

    @property
    def endpoint(self):
        return f'urn:uuid:{self.endpoint_uuid}'

    @property
    def device_xaddr(self):
        return f'http://{self.ipv4}:{self.http_port}/onvif/device_service'

    @property
    def media_xaddr(self):
        return f'http://{self.ipv4}:{self.http_port}/onvif/media_service'

    @property
    def snapshot_uri(self):
        return f'http://{self.ipv4}:{self.http_port}/snapshot.jpg'

    @property
    def stream_uri(self):
        return f'rtsp://{self.ipv4}:{self.rtsp_port}/live'

    @property
    def name(self):
        return self.state.camera_name

    @property
    def resolution(self):
        return self.state.resolution()

    @property
    def scopes(self):
        return (
            'onvif://www.onvif.org/Profile/Streaming',
            f'onvif://www.onvif.org/name/{quote(self.name, safe="")}',
            f'onvif://www.onvif.org/hardware/{quote(self.model, safe="")}',
            # ':' is valid in a URI path segment. Keeping it literal makes the
            # discovery unique ID identical to GetNetworkInterfaces(), which
            # prevents duplicate HA entries after manual/automatic setup.
            f'onvif://www.onvif.org/mac/{self.mac}',
        )


def _sub(parent, namespace, name, text=None, **attributes):
    element = ET.SubElement(parent, q(namespace, name), attributes)
    if text is not None:
        element.text = str(text)
    return element


def _envelope(body_element, action=None, relates_to=None, app_sequence=None):
    envelope = ET.Element(q(SOAP, 'Envelope'))
    if action or relates_to:
        header = _sub(envelope, SOAP, 'Header')
        if action:
            _sub(header, WSA, 'Action', action)
        _sub(header, WSA, 'MessageID', f'urn:uuid:{uuid.uuid4()}')
        if relates_to:
            _sub(header, WSA, 'RelatesTo', relates_to)
        _sub(
            header, WSA, 'To',
            'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous',
        )
        if app_sequence is not None:
            instance_id, message_number = app_sequence
            _sub(
                header, WSD, 'AppSequence', None,
                InstanceId=str(instance_id), MessageNumber=str(message_number),
            )
    body = _sub(envelope, SOAP, 'Body')
    body.append(body_element)
    return ET.tostring(envelope, encoding='utf-8', xml_declaration=True)


def _request_operation(payload):
    root = ET.fromstring(payload)
    body = next((node for node in root.iter() if local_name(node.tag) == 'Body'), None)
    if body is None or not list(body):
        raise ValueError('SOAP Body has no operation')
    return list(body)[0]


def _request_text(operation, name):
    for node in operation.iter():
        if local_name(node.tag) == name:
            return node.text or ''
    return ''


def _soap_fault(reason, subcode='ter:InvalidArgVal'):
    fault = ET.Element(q(SOAP, 'Fault'))
    code = _sub(fault, SOAP, 'Code')
    _sub(code, SOAP, 'Value', 's:Sender')
    nested = _sub(code, SOAP, 'Subcode')
    subcode_value = _sub(nested, SOAP, 'Value', subcode)
    subcode_value.set('xmlns:ter', TER)
    reason_el = _sub(fault, SOAP, 'Reason')
    text = _sub(reason_el, SOAP, 'Text', reason)
    text.set('{http://www.w3.org/XML/1998/namespace}lang', 'en')
    return 500, _envelope(fault)


def _date_time(parent, now):
    time_el = _sub(parent, TT, 'Time')
    _sub(time_el, TT, 'Hour', now.hour)
    _sub(time_el, TT, 'Minute', now.minute)
    _sub(time_el, TT, 'Second', now.second)
    date_el = _sub(parent, TT, 'Date')
    _sub(date_el, TT, 'Year', now.year)
    _sub(date_el, TT, 'Month', now.month)
    _sub(date_el, TT, 'Day', now.day)


def _device_response(action, ctx, operation, now):
    response = ET.Element(q(TDS, f'{action}Response'))
    if action == 'GetServices':
        for namespace, xaddr in ((TDS, ctx.device_xaddr), (TRT, ctx.media_xaddr)):
            service = _sub(response, TDS, 'Service')
            _sub(service, TDS, 'Namespace', namespace)
            _sub(service, TDS, 'XAddr', xaddr)
            version = _sub(service, TDS, 'Version')
            _sub(version, TT, 'Major', 2)
            _sub(version, TT, 'Minor', 0)
    elif action == 'GetCapabilities':
        caps = _sub(response, TDS, 'Capabilities')
        device = _sub(caps, TT, 'Device')
        _sub(device, TT, 'XAddr', ctx.device_xaddr)
        network = _sub(device, TT, 'Network')
        for name in ('IPFilter', 'ZeroConfiguration', 'IPVersion6', 'DynDNS'):
            _sub(network, TT, name, 'false')
        media = _sub(caps, TT, 'Media')
        _sub(media, TT, 'XAddr', ctx.media_xaddr)
        streaming = _sub(media, TT, 'StreamingCapabilities')
        _sub(streaming, TT, 'RTPMulticast', 'false')
        _sub(streaming, TT, 'RTP_TCP', 'true')
        _sub(streaming, TT, 'RTP_RTSP_TCP', 'true')
    elif action == 'GetSystemDateAndTime':
        system = _sub(response, TDS, 'SystemDateAndTime')
        _sub(system, TT, 'DateTimeType', 'NTP')
        _sub(system, TT, 'DaylightSavings', 'false')
        zone = _sub(system, TT, 'TimeZone')
        _sub(zone, TT, 'TZ', 'UTC')
        utc = _sub(system, TT, 'UTCDateTime')
        _date_time(utc, now)
    elif action == 'GetDeviceInformation':
        _sub(response, TDS, 'Manufacturer', ctx.manufacturer)
        _sub(response, TDS, 'Model', ctx.model)
        _sub(response, TDS, 'FirmwareVersion', ctx.firmware)
        _sub(response, TDS, 'SerialNumber', ctx.serial)
        _sub(response, TDS, 'HardwareId', ctx.model)
    elif action == 'GetNetworkInterfaces':
        interface = _sub(response, TDS, 'NetworkInterfaces', token='wlan0')
        _sub(interface, TT, 'Enabled', 'true')
        info = _sub(interface, TT, 'Info')
        _sub(info, TT, 'Name', 'wlan0')
        _sub(info, TT, 'HwAddress', ctx.mac)
        _sub(info, TT, 'MTU', 1500)
        ipv4 = _sub(interface, TT, 'IPv4')
        _sub(ipv4, TT, 'Enabled', 'true')
        config = _sub(ipv4, TT, 'Config')
        manual = _sub(config, TT, 'Manual')
        _sub(manual, TT, 'Address', ctx.ipv4)
        _sub(manual, TT, 'PrefixLength', 24)
        _sub(config, TT, 'DHCP', 'true')
    elif action == 'GetScopes':
        for value in ctx.scopes:
            scope = _sub(response, TDS, 'Scopes')
            _sub(scope, TT, 'ScopeDef', 'Fixed')
            _sub(scope, TT, 'ScopeItem', value)
    else:
        return _soap_fault(f'Unsupported device action {action}', 'ter:ActionNotSupported')
    return 200, _envelope(response)


def _video_source(parent, ctx):
    width, height = ctx.resolution
    source = _sub(parent, TRT, 'VideoSources', token='video_source_1')
    _sub(source, TT, 'Framerate', ctx.frame_rate)
    resolution = _sub(source, TT, 'Resolution')
    _sub(resolution, TT, 'Width', width)
    _sub(resolution, TT, 'Height', height)


def _profile(parent, ctx):
    width, height = ctx.resolution
    profile = _sub(parent, TRT, 'Profiles', token='profile_1', fixed='true')
    _sub(profile, TT, 'Name', ctx.name)
    source = _sub(profile, TT, 'VideoSourceConfiguration', token='video_source_config_1')
    _sub(source, TT, 'Name', 'Video Source')
    _sub(source, TT, 'UseCount', 1)
    _sub(source, TT, 'SourceToken', 'video_source_1')
    _sub(source, TT, 'Bounds', None, x='0', y='0', width=str(width), height=str(height))
    encoder = _sub(profile, TT, 'VideoEncoderConfiguration', token='h264_main')
    _sub(encoder, TT, 'Name', 'H264 Main')
    _sub(encoder, TT, 'UseCount', 1)
    _sub(encoder, TT, 'Encoding', 'H264')
    resolution = _sub(encoder, TT, 'Resolution')
    _sub(resolution, TT, 'Width', width)
    _sub(resolution, TT, 'Height', height)
    _sub(encoder, TT, 'Quality', 5)
    rate = _sub(encoder, TT, 'RateControl')
    _sub(rate, TT, 'FrameRateLimit', ctx.frame_rate)
    _sub(rate, TT, 'EncodingInterval', 1)
    _sub(rate, TT, 'BitrateLimit', 4000)
    h264 = _sub(encoder, TT, 'H264')
    _sub(h264, TT, 'GovLength', 30)
    _sub(h264, TT, 'H264Profile', 'Baseline')
    multicast = _sub(encoder, TT, 'Multicast')
    address = _sub(multicast, TT, 'Address')
    _sub(address, TT, 'Type', 'IPv4')
    _sub(address, TT, 'IPv4Address', '0.0.0.0')
    _sub(multicast, TT, 'Port', 0)
    _sub(multicast, TT, 'TTL', 0)
    _sub(multicast, TT, 'AutoStart', 'false')
    _sub(encoder, TT, 'SessionTimeout', 'PT0S')


def _media_response(action, ctx, operation):
    response = ET.Element(q(TRT, f'{action}Response'))
    if action == 'GetServiceCapabilities':
        _sub(
            response, TRT, 'Capabilities', None,
            SnapshotUri='true', Rotation='false', VideoSourceMode='false',
            OSD='false', TemporaryOSDText='false', EXICompression='false',
        )
    elif action == 'GetProfiles':
        _profile(response, ctx)
    elif action == 'GetVideoSources':
        _video_source(response, ctx)
    elif action in ('GetStreamUri', 'GetSnapshotUri'):
        if _request_text(operation, 'ProfileToken') != 'profile_1':
            return _soap_fault('Unknown profile token')
        media_uri = _sub(response, TRT, 'MediaUri')
        uri = ctx.stream_uri if action == 'GetStreamUri' else ctx.snapshot_uri
        _sub(media_uri, TT, 'Uri', uri)
        _sub(media_uri, TT, 'InvalidAfterConnect', 'false')
        _sub(media_uri, TT, 'InvalidAfterReboot', 'false')
        _sub(media_uri, TT, 'Timeout', 'PT0S')
    else:
        return _soap_fault(f'Unsupported media action {action}', 'ter:ActionNotSupported')
    return 200, _envelope(response)


def soap_response(service, ctx, payload, now=None):
    """Return ``(http_status, XML bytes)`` for one ONVIF SOAP request."""
    try:
        operation = _request_operation(payload)
    except (ET.ParseError, ValueError) as exc:
        return _soap_fault(f'Malformed SOAP request: {exc}')
    action = local_name(operation.tag)
    now = now or datetime.now(timezone.utc)
    if service == 'device':
        return _device_response(action, ctx, operation, now)
    if service == 'media':
        return _media_response(action, ctx, operation)
    return _soap_fault(f'Unknown ONVIF service {service!r}', 'ter:ActionNotSupported')


def discovery_request(payload):
    """Return ``(operation, message_id, types, scopes, address)`` or ``None``."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    message_id = ''
    operation = None
    types = ''
    scopes = ''
    address = ''
    for node in root.iter():
        name = local_name(node.tag)
        if name == 'MessageID':
            message_id = node.text or ''
        elif name in ('Probe', 'Resolve'):
            operation = name
        elif name == 'Types':
            types = node.text or ''
        elif name == 'Scopes':
            scopes = node.text or ''
        elif operation == 'Resolve' and name == 'Address':
            address = node.text or ''
    if operation is None:
        return None
    return operation, message_id, types, scopes, address


def _discovery_match(ctx):
    match = ET.Element(q(WSD, 'ProbeMatch'))
    endpoint = _sub(match, WSA, 'EndpointReference')
    _sub(endpoint, WSA, 'Address', ctx.endpoint)
    types = _sub(match, WSD, 'Types', 'dn:NetworkVideoTransmitter')
    # QName values in element text do not make ElementTree emit the prefix
    # declaration automatically; WSDiscovery resolves it from this element.
    types.set('xmlns:dn', DN)
    _sub(match, WSD, 'Scopes', ' '.join(ctx.scopes))
    _sub(match, WSD, 'XAddrs', ctx.device_xaddr)
    _sub(match, WSD, 'MetadataVersion', 1)
    return match


def discovery_response(ctx, payload):
    """Build a WS-Discovery reply, or return ``None`` for a non-match."""
    parsed = discovery_request(payload)
    if parsed is None:
        return None
    operation, message_id, types, scopes, address = parsed
    if operation == 'Probe':
        if types and 'NetworkVideoTransmitter' not in types:
            return None
        requested_scopes = tuple(scopes.split())
        if requested_scopes and not all(scope in ctx.scopes for scope in requested_scopes):
            return None
        result = ET.Element(q(WSD, 'ProbeMatches'))
        result.append(_discovery_match(ctx))
        action = f'{WSD}/ProbeMatches'
    else:
        if address != ctx.endpoint:
            return None
        result = ET.Element(q(WSD, 'ResolveMatches'))
        match = ET.SubElement(result, q(WSD, 'ResolveMatch'))
        probe_match = _discovery_match(ctx)
        for child in list(probe_match):
            match.append(child)
        action = f'{WSD}/ResolveMatches'
    ctx.discovery_message_number += 1
    instance_id = ctx.endpoint_uuid.int & 0xFFFFFFFF
    return _envelope(
        result,
        action=action,
        relates_to=message_id or None,
        app_sequence=(instance_id, ctx.discovery_message_number),
    )
