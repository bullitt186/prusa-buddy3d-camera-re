"""Tests for the typed trigger dispatcher (GAP-TRIGGER-01, GAP-SNAPSHOT-02).

Stdlib-only: these tests import ``proto``, ``state`` and ``trigger`` but never
``socketio``/``aiohttp``/``gi``/GStreamer.
"""
import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from proto import encode_message  # noqa: E402
from state import CameraState  # noqa: E402
import trigger  # noqa: E402


def decode(fields):
    return trigger.decode_trigger(encode_message(fields))


class DecodeTriggerTests(unittest.TestCase):
    def test_decodes_every_varint_tag(self):
        for tag in (1, 2, 3, 4, 5, 8, 9, 10, 12, 14, 15):
            with self.subTest(tag=tag):
                decoded = decode({tag: 1})
                self.assertEqual(decoded[tag], 1)

    def test_decodes_string_fields(self):
        decoded = decode({11: 'req-123', 13: 'second-string'})
        self.assertEqual(decoded[11], 'req-123')
        self.assertEqual(decoded[13], 'second-string')
        self.assertEqual(decoded.request_id, 'req-123')
        self.assertEqual(decoded.secondary_string, 'second-string')

    def test_request_id_normalizes_bytes_payload(self):
        # Invalid UTF-8 survives decode_message as bytes and must be normalized.
        decoded = trigger.decode_trigger(encode_message({11: b'\xff\xfe'}))
        self.assertIsInstance(decoded[11], bytes)
        self.assertEqual(decoded.request_id, '\ufffd\ufffd')

    def test_absent_fields_yield_empty_mapping_and_empty_request_id(self):
        decoded = decode({})
        self.assertEqual(decoded, {})
        self.assertEqual(decoded.request_id, '')
        self.assertEqual(decoded.secondary_string, '')
        self.assertEqual(trigger.trigger_actions(decoded), [])

    def test_non_bytes_payload_is_empty_not_an_error(self):
        for data in (None, 'text', 5, {'a': 1}):
            with self.subTest(data=data):
                decoded = trigger.decode_trigger(data)
                self.assertEqual(decoded, {})
                self.assertEqual(decoded.request_id, '')

    def test_trigger_message_is_a_dict(self):
        self.assertIsInstance(decode({1: 1}), dict)


class TriggerActionTests(unittest.TestCase):
    def test_every_documented_value_maps_to_its_action(self):
        cases = {
            (1, 1): trigger.STATUS,
            (2, 1): trigger.FEATURES,
            (3, 1): trigger.SNAPSHOT,
            (4, 1): trigger.SNAPSHOT_ENABLE,
            (4, 2): trigger.SNAPSHOT_DISABLE,
            (5, 1): trigger.TIMELAPSE_ENABLE,
            (5, 2): trigger.TIMELAPSE_DISABLE,
            (8, 1): trigger.FW_UPDATE,
            (9, 1): trigger.REBOOT,
            (10, 1): trigger.RTSP_START,
            (10, 2): trigger.RTSP_STOP,
            (12, 1): trigger.PROTOCOL_INFO,
            (14, 1): trigger.TIMELAPSE_MAKE,
            (15, 1): trigger.TIMELAPSE_FILE_LIST,
        }
        for (tag, value), action in cases.items():
            with self.subTest(tag=tag, value=value):
                self.assertEqual(trigger.trigger_actions({tag: value}), [action])

    def test_multiple_fields_produce_actions_in_tag_order(self):
        # Insertion order is deliberately not tag order; the plan must be stable.
        decoded = trigger.TriggerMessage({4: 1, 12: 1, 1: 1, 2: 1})
        self.assertEqual(
            trigger.trigger_actions(decoded),
            [trigger.STATUS, trigger.FEATURES, trigger.SNAPSHOT_ENABLE,
             trigger.PROTOCOL_INFO],
        )

    def test_unknown_values_produce_no_action(self):
        for fields in (
            {1: 2}, {2: 0}, {3: 5},          # request flags require value 1
            {4: 3}, {5: 0}, {10: 5},          # enable/disable is exactly 1/2
            {12: 0}, {14: 2}, {15: 2},        # documented unsupported branches
            {6: 1}, {7: 1},                   # undocumented tags
        ):
            with self.subTest(fields=fields):
                self.assertEqual(trigger.trigger_actions(fields), [])

    def test_string_metadata_fields_produce_no_action(self):
        decoded = trigger.TriggerMessage({11: 'req', 13: 'other'})
        self.assertEqual(trigger.trigger_actions(decoded), [])

    def test_non_mapping_input_is_empty(self):
        for decoded in (None, 'x', 5):
            with self.subTest(decoded=decoded):
                self.assertEqual(trigger.trigger_actions(decoded), [])


class BareTriggerRegressionTests(unittest.TestCase):
    def test_bare_get_status_does_not_trigger_other_actions(self):
        actions = trigger.trigger_actions(decode({1: 1}))
        self.assertEqual(actions, [trigger.STATUS])
        self.assertNotIn(trigger.SNAPSHOT, actions)
        self.assertNotIn(trigger.FEATURES, actions)
        self.assertNotIn(trigger.PROTOCOL_INFO, actions)


class SnapshotControlTests(unittest.TestCase):
    def test_enable_then_disable_toggles_shared_state(self):
        state = CameraState()
        state.snapshot_upload_enabled = False

        self.assertTrue(trigger.apply_snapshot_upload(trigger.SNAPSHOT_ENABLE, state))
        self.assertTrue(state.snapshot_upload_enabled)
        self.assertTrue(state.periodic_snapshot_allowed())

        self.assertTrue(trigger.apply_snapshot_upload(trigger.SNAPSHOT_DISABLE, state))
        self.assertFalse(state.snapshot_upload_enabled)
        self.assertFalse(state.periodic_snapshot_allowed())

    def test_non_snapshot_action_leaves_state_unchanged(self):
        state = CameraState()
        state.snapshot_upload_enabled = False
        self.assertFalse(trigger.apply_snapshot_upload(trigger.STATUS, state))
        self.assertFalse(state.snapshot_upload_enabled)

    def test_get_snapshot_is_planned_even_when_upload_disabled(self):
        state = CameraState()
        state.snapshot_upload_enabled = False
        # The capture itself needs hardware; assert the plan is independent of
        # the periodic-upload switch (GAP-SNAPSHOT-02).
        self.assertEqual(trigger.trigger_actions(decode({3: 1})), [trigger.SNAPSHOT])
        self.assertFalse(state.periodic_snapshot_allowed())


if __name__ == '__main__':
    unittest.main()
