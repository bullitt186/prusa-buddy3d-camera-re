from datetime import datetime, timezone
import ast
import sys
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import onvif_facade as onvif  # noqa: E402
from state import CameraState  # noqa: E402


def request(namespace, action, inner=''):
    return (
        f'<s:Envelope xmlns:s="{onvif.SOAP}">'
        f'<s:Body><x:{action} xmlns:x="{namespace}">{inner}</x:{action}></s:Body>'
        f'</s:Envelope>'
    ).encode()


def texts(payload, name):
    root = ET.fromstring(payload)
    return [node.text or '' for node in root.iter() if onvif.local_name(node.tag) == name]


class OnvifTestCase(unittest.TestCase):
    def setUp(self):
        self.state = CameraState(camera_name='Print Room')
        self.context = onvif.OnvifContext.create(
            self.state,
            '192.0.2.10',
            'aa-bb-cc-dd-ee-ff',
            '3.1.6',
            stable_seed='must-not-appear',
        )

    def call(self, service, namespace, action, inner=''):
        return onvif.soap_response(
            service,
            self.context,
            request(namespace, action, inner),
            now=datetime(2026, 9, 20, 12, 34, 56, tzinfo=timezone.utc),
        )


class IdentityTests(OnvifTestCase):
    def test_identity_is_stable_and_normalized(self):
        again = onvif.OnvifContext.create(
            self.state, '192.0.2.10', 'AA:BB:CC:DD:EE:FF', '3.1.6'
        )
        self.assertEqual(self.context.endpoint_uuid, again.endpoint_uuid)
        self.assertEqual(self.context.mac, 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(self.context.serial, 'AABBCCDDEEFF')

    def test_fallback_seed_is_not_exposed(self):
        context = onvif.OnvifContext.create(
            self.state, '192.0.2.10', '', '3.1.6', stable_seed='SECRET-FINGERPRINT'
        )
        rendered = ' '.join((context.endpoint, context.mac, context.serial, *context.scopes))
        self.assertNotIn('SECRET-FINGERPRINT', rendered)


class DeviceSoapTests(OnvifTestCase):
    def test_services_publish_device_and_media_xaddrs(self):
        status, body = self.call('device', onvif.TDS, 'GetServices')
        self.assertEqual(status, 200)
        self.assertEqual(texts(body, 'XAddr'), [
            'http://192.0.2.10:80/onvif/device_service',
            'http://192.0.2.10:80/onvif/media_service',
        ])

    def test_capabilities_advertise_device_and_media_only(self):
        status, body = self.call('device', onvif.TDS, 'GetCapabilities')
        self.assertEqual(status, 200)
        names = {onvif.local_name(node.tag) for node in ET.fromstring(body).iter()}
        self.assertIn('Media', names)
        self.assertNotIn('PTZ', names)
        self.assertNotIn('Events', names)
        self.assertNotIn('Imaging', names)

    def test_device_information_and_network_identity_are_consistent(self):
        _, info = self.call('device', onvif.TDS, 'GetDeviceInformation')
        _, network = self.call('device', onvif.TDS, 'GetNetworkInterfaces')
        self.assertEqual(texts(info, 'Manufacturer'), ['Niceboy'])
        self.assertEqual(texts(info, 'Model'), ['Buddy3D-C1'])
        self.assertEqual(texts(info, 'FirmwareVersion'), ['3.1.6'])
        self.assertEqual(texts(info, 'SerialNumber'), ['AABBCCDDEEFF'])
        self.assertEqual(texts(network, 'HwAddress'), ['AA:BB:CC:DD:EE:FF'])
        self.assertEqual(texts(network, 'Address'), ['192.0.2.10'])

    def test_time_is_utc(self):
        _, body = self.call('device', onvif.TDS, 'GetSystemDateAndTime')
        self.assertEqual(texts(body, 'DateTimeType'), ['NTP'])
        self.assertEqual(texts(body, 'TZ'), ['UTC'])
        self.assertEqual(texts(body, 'Hour'), ['12'])
        self.assertEqual(texts(body, 'Second'), ['56'])

    def test_scopes_contain_streaming_name_hardware_and_mac(self):
        _, body = self.call('device', onvif.TDS, 'GetScopes')
        scopes = texts(body, 'ScopeItem')
        self.assertIn('onvif://www.onvif.org/Profile/Streaming', scopes)
        self.assertIn('onvif://www.onvif.org/name/Print%20Room', scopes)
        self.assertIn('onvif://www.onvif.org/mac/AA:BB:CC:DD:EE:FF', scopes)

    def test_discovery_mac_matches_manual_configuration_unique_id(self):
        mac_scope = next(scope for scope in self.context.scopes if '/mac/' in scope)
        self.assertEqual(mac_scope.rsplit('/', 1)[-1], self.context.mac)


class MediaSoapTests(OnvifTestCase):
    def test_one_dynamic_h264_profile(self):
        for quality, expected in ((1, ('640', '480')), (2, ('1280', '720')), (3, ('1920', '1080'))):
            with self.subTest(quality=quality):
                self.state.set_quality(quality)
                status, body = self.call('media', onvif.TRT, 'GetProfiles')
                self.assertEqual(status, 200)
                root = ET.fromstring(body)
                profiles = [n for n in root.iter() if onvif.local_name(n.tag) == 'Profiles']
                self.assertEqual(len(profiles), 1)
                self.assertEqual(profiles[0].attrib['token'], 'profile_1')
                self.assertEqual(texts(body, 'Encoding'), ['H264'])
                self.assertEqual(texts(body, 'Width')[-1], expected[0])
                self.assertEqual(texts(body, 'Height')[-1], expected[1])
                self.assertEqual(texts(body, 'Name')[0], 'Print Room')

    def test_stream_and_snapshot_uris(self):
        inner = '<x:ProfileToken>profile_1</x:ProfileToken>'
        _, stream = self.call('media', onvif.TRT, 'GetStreamUri', inner)
        _, snapshot = self.call('media', onvif.TRT, 'GetSnapshotUri', inner)
        self.assertEqual(texts(stream, 'Uri'), ['rtsp://192.0.2.10:8555/live'])
        self.assertEqual(texts(snapshot, 'Uri'), ['http://192.0.2.10:80/snapshot.jpg'])

    def test_snapshot_capability_is_true(self):
        _, body = self.call('media', onvif.TRT, 'GetServiceCapabilities')
        capabilities = next(
            node for node in ET.fromstring(body).iter()
            if onvif.local_name(node.tag) == 'Capabilities'
        )
        self.assertEqual(capabilities.attrib['SnapshotUri'], 'true')

    def test_unknown_profile_returns_fault(self):
        status, body = self.call(
            'media', onvif.TRT, 'GetStreamUri',
            '<x:ProfileToken>missing</x:ProfileToken>',
        )
        self.assertEqual(status, 500)
        self.assertIn('Unknown profile token', texts(body, 'Text'))


class FaultTests(OnvifTestCase):
    def test_malformed_xml_returns_soap_fault(self):
        status, body = onvif.soap_response('device', self.context, b'<broken')
        self.assertEqual(status, 500)
        self.assertEqual(texts(body, 'Fault'), [''])

    def test_unsupported_action_returns_action_not_supported(self):
        status, body = self.call('device', onvif.TDS, 'SystemReboot')
        self.assertEqual(status, 500)
        self.assertEqual(texts(body, 'Value')[-1], 'ter:ActionNotSupported')


class DiscoveryTests(OnvifTestCase):
    def discovery(self, action, body):
        message_id = f'urn:uuid:{uuid.uuid4()}'
        payload = (
            f'<s:Envelope xmlns:s="{onvif.SOAP}" xmlns:a="{onvif.WSA}" '
            f'xmlns:d="{onvif.WSD}" xmlns:dn="{onvif.DN}">'
            f'<s:Header><a:MessageID>{message_id}</a:MessageID></s:Header>'
            f'<s:Body><d:{action}>{body}</d:{action}></s:Body></s:Envelope>'
        ).encode()
        return message_id, payload

    def test_matching_probe_returns_one_match(self):
        message_id, payload = self.discovery(
            'Probe',
            '<d:Types>dn:NetworkVideoTransmitter</d:Types>'
            '<d:Scopes>onvif://www.onvif.org/Profile/Streaming</d:Scopes>',
        )
        response = onvif.discovery_response(self.context, payload)
        self.assertIsNotNone(response)
        self.assertEqual(texts(response, 'RelatesTo'), [message_id])
        self.assertEqual(texts(response, 'XAddrs'), [self.context.device_xaddr])
        self.assertEqual(len(texts(response, 'ProbeMatch')), 1)
        self.assertIn(b'xmlns:dn=', response)
        app_sequence = next(
            node for node in ET.fromstring(response).iter()
            if onvif.local_name(node.tag) == 'AppSequence'
        )
        self.assertEqual(app_sequence.attrib['MessageNumber'], '1')

    def test_unrelated_probe_is_ignored(self):
        _, payload = self.discovery('Probe', '<d:Types>dn:Printer</d:Types>')
        self.assertIsNone(onvif.discovery_response(self.context, payload))

    def test_resolve_matches_only_own_endpoint(self):
        _, own = self.discovery(
            'Resolve',
            f'<a:EndpointReference><a:Address>{self.context.endpoint}</a:Address></a:EndpointReference>',
        )
        _, other = self.discovery(
            'Resolve',
            '<a:EndpointReference><a:Address>urn:uuid:00000000-0000-0000-0000-000000000000</a:Address></a:EndpointReference>',
        )
        self.assertIsNotNone(onvif.discovery_response(self.context, own))
        self.assertIsNone(onvif.discovery_response(self.context, other))

    def test_malformed_datagram_is_ignored(self):
        self.assertIsNone(onvif.discovery_response(self.context, b'<broken'))


class MainWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((PI_DIR / 'main.py').read_text())

    def test_trigger_snapshot_has_no_streaming_guard(self):
        dispatch = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == 'dispatch_trigger_action'
        )
        streaming_reads = [
            node for node in ast.walk(dispatch)
            if isinstance(node, ast.Attribute) and node.attr == 'streaming'
        ]
        self.assertEqual(streaming_reads, [])

    def test_onvif_uses_restored_shared_state(self):
        constructor = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'create'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'OnvifContext'
        ]
        self.assertEqual(len(constructor), 1)
        self.assertIsInstance(constructor[0].args[0], ast.Name)
        self.assertEqual(constructor[0].args[0].id, 'state')


if __name__ == '__main__':
    unittest.main()
