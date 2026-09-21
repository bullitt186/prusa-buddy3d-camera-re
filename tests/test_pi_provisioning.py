"""WP-3a AC-15: provisioning state machine and device identity (provisioning).

Host-only. Every persistence path is a ``tempfile`` path and ``/data``
availability is patched, so no real ``/data`` is touched.
"""
import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import config_schema  # noqa: E402
import provisioning  # noqa: E402
import settings_store  # noqa: E402

FIXED_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
FIXED_ISO = '2026-09-21T12:00:00+00:00'

FACTS_ALL = provisioning.Facts(
    storage_ready=True,
    camera_validated=True,
    admin_password_set=True,
    device_valid=True,
    prusa_token_set=True,
    camera_running=True,
)


class ProvisioningTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.path = os.path.join(self.root, 'provisioning.json')
        available = patch.object(settings_store, 'available', return_value=True)
        available.start()
        self.addCleanup(available.stop)

    def make_state(self, state='factory', reason='', path=None, **kwargs):
        return provisioning.ProvisioningState(
            state=state, reason=reason, path=path or self.path, **kwargs
        )

    def read_payload(self, path=None):
        with open(path or self.path, encoding='utf-8') as f:
            return json.load(f)


class ContractPinsTests(ProvisioningTestBase):
    def test_states_and_path_are_pinned(self):
        self.assertEqual(
            provisioning.STATES,
            ('factory', 'storage_ready', 'camera_validated',
             'unclaimed', 'claimed', 'configured', 'running'),
        )
        self.assertEqual(provisioning.RECOVERY, 'recovery')
        self.assertEqual(
            provisioning.PROVISIONING_PATH, '/data/prusa-cam/provisioning.json'
        )

    def test_next_state_chain_is_forward_only(self):
        for index, state in enumerate(provisioning.STATES[:-1]):
            with self.subTest(state=state):
                self.assertEqual(
                    provisioning.NEXT_STATE[state], provisioning.STATES[index + 1]
                )
        self.assertNotIn(provisioning.STATES[-1], provisioning.NEXT_STATE)


class DeviceIdentityTests(ProvisioningTestBase):
    def test_derive_device_id_matches_onvif_uuid5(self):
        expected = uuid.uuid5(uuid.NAMESPACE_URL, 'prusa-camera:seed').hex
        self.assertEqual(provisioning.derive_device_id('seed'), expected)
        self.assertEqual(
            provisioning.derive_device_id('seed'),
            provisioning.derive_device_id('seed'),
        )

    def test_derive_device_id_rejects_empty_and_non_string(self):
        for seed in ('', '   ', None, 123, b'seed', []):
            with self.subTest(seed=seed):
                with self.assertRaises(ValueError):
                    provisioning.derive_device_id(seed)

    def test_ssid_suffix_sanitizes_and_takes_last_six(self):
        self.assertEqual(
            provisioning.setup_ssid_suffix('AA:BB:CC:DD:EE:FF'), 'ddeeff'
        )
        self.assertEqual(
            provisioning.setup_ssid_suffix(uuid.UUID(expected_uuid())),
            expected_uuid().replace('-', '')[-6:],
        )
        self.assertEqual(provisioning.setup_ssid_suffix('abc'), 'abc')
        self.assertEqual(provisioning.setup_ssid_suffix(''), '')
        self.assertEqual(provisioning.setup_ssid_suffix(None), '')

    def test_setup_ssid_derivation(self):
        self.assertEqual(
            provisioning.setup_ssid('AA:BB:CC:DD:EE:FF'),
            'Buddy3D-Setup-ddeeff',
        )
        self.assertEqual(provisioning.setup_ssid(''), '')
        ssid = provisioning.setup_ssid('AA:BB:CC:DD:EE:FF')
        self.assertLessEqual(len(ssid), 32)

    def test_admin_hostname_is_a_valid_dns_label(self):
        self.assertEqual(
            provisioning.admin_hostname('AA:BB:CC:DD:EE:FF'), 'buddy3d-ddeeff'
        )
        self.assertEqual(provisioning.admin_hostname(''), '')
        hostname = provisioning.admin_hostname('AA:BB:CC:DD:EE:FF')
        self.assertLessEqual(len(hostname), 63)
        self.assertRegex(hostname, re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'))


def expected_uuid():
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'prusa-camera:seed'))


class ClaimPredicateTests(ProvisioningTestBase):
    def test_token_alone_is_not_a_claim(self):
        self.assertFalse(
            provisioning.is_claimed(
                {'prusa': {'token': 'tok-synthetic'}},
                config_schema.default_device(),
            )
        )

    def test_claim_requires_password_hash_and_valid_device(self):
        secrets = {'admin': {'password_hash': 'hash-synthetic'}}
        self.assertTrue(provisioning.is_claimed(secrets, config_schema.default_device()))
        self.assertFalse(provisioning.is_claimed(secrets, {}))
        self.assertFalse(provisioning.is_claimed(secrets, None))

    def test_blank_password_hash_is_not_set(self):
        for value in ('', '   ', None, 123):
            with self.subTest(value=value):
                self.assertFalse(
                    provisioning.is_claimed(
                        {'admin': {'password_hash': value}},
                        config_schema.default_device(),
                    )
                )

    def test_device_is_valid(self):
        self.assertTrue(provisioning.device_is_valid(config_schema.default_device()))
        for bad in ({}, None, [], 'x', {'camera_name': 123}):
            with self.subTest(bad=bad):
                self.assertFalse(provisioning.device_is_valid(bad))

    def test_advance_token_only_stays_unclaimed(self):
        facts = provisioning.Facts(
            storage_ready=True,
            camera_validated=True,
            admin_password_set=False,
            device_valid=True,
            prusa_token_set=True,
        )
        inst = self.make_state('camera_validated')
        result = inst.advance(now=FIXED_NOW, facts=facts)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'unclaimed')
        again = inst.advance(now=FIXED_NOW, facts=facts)
        self.assertEqual(inst.state, 'unclaimed')
        self.assertEqual(again.reason, 'no change')


class LegalTransitionTests(ProvisioningTestBase):
    def test_every_forward_edge_is_legal_and_persisted(self):
        for state in provisioning.STATES[:-1]:
            with self.subTest(state=state):
                path = os.path.join(self.root, f'{state}.json')
                inst = self.make_state(state, path=path)
                target = provisioning.NEXT_STATE[state]
                result = inst.transition(target, reason='go', now=FIXED_NOW)
                self.assertTrue(result.ok, result.reason)
                self.assertTrue(result.persisted)
                self.assertEqual(result.previous, state)
                self.assertEqual(inst.state, target)
                self.assertEqual(
                    provisioning.ProvisioningState.load(path).state, target
                )

    def test_any_chain_state_may_enter_recovery(self):
        for state in provisioning.STATES:
            with self.subTest(state=state):
                path = os.path.join(self.root, f'rec-{state}.json')
                inst = self.make_state(state, path=path)
                result = inst.transition(
                    provisioning.RECOVERY, reason='fault', now=FIXED_NOW
                )
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(inst.state, 'recovery')

    def test_recovery_may_return_to_any_chain_state(self):
        for target in provisioning.STATES:
            with self.subTest(target=target):
                path = os.path.join(self.root, f'ret-{target}.json')
                inst = self.make_state('recovery', path=path)
                result = inst.transition(target, now=FIXED_NOW)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(inst.state, target)

    def test_transition_accepts_an_object_with_state(self):
        inst = self.make_state('factory')
        result = inst.transition(SimpleNamespace(state='storage_ready'), now=FIXED_NOW)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'storage_ready')

    def test_same_state_is_a_no_op(self):
        inst = self.make_state('unclaimed')
        result = inst.transition('unclaimed')
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, 'already in state')
        self.assertFalse(result.persisted)


class IllegalTransitionTests(ProvisioningTestBase):
    def test_illegal_edge_is_rejected_and_state_is_unchanged(self):
        inst = self.make_state('factory')
        inst.reason = 'seed'
        inst.updated_at = FIXED_ISO
        self.assertTrue(inst.save())
        before = self.read_payload()

        result = inst.transition('running')

        self.assertFalse(result.ok)
        self.assertIn('illegal transition factory -> running', result.reason)
        self.assertEqual(inst.state, 'factory')
        self.assertFalse(result.persisted)
        self.assertEqual(self.read_payload(), before)

    def test_unknown_state_is_rejected(self):
        inst = self.make_state('factory')
        result = inst.transition('bogus')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'unknown provisioning state')
        self.assertEqual(inst.state, 'factory')

    def test_backward_edge_is_rejected(self):
        inst = self.make_state('camera_validated')
        result = inst.transition('storage_ready')
        self.assertFalse(result.ok)
        self.assertIn('illegal transition', result.reason)
        self.assertEqual(inst.state, 'camera_validated')


class StorageGateTests(ProvisioningTestBase):
    def test_unavailable_data_makes_transition_and_save_noops(self):
        with patch.object(settings_store, 'available', return_value=False):
            inst = self.make_state('factory')
            result = inst.transition('storage_ready', now=FIXED_NOW)
            self.assertFalse(result.ok)
            self.assertIn('durable storage unavailable', result.reason)
            self.assertEqual(inst.state, 'factory')
            self.assertFalse(result.persisted)
            self.assertFalse(inst.save())
            self.assertFalse(os.path.exists(self.path))


class PersistenceTests(ProvisioningTestBase):
    def test_atomic_save_and_reload_round_trip(self):
        inst = self.make_state('unclaimed', reason='ready')
        inst.updated_at = FIXED_ISO
        self.assertTrue(inst.save())

        payload = self.read_payload()
        self.assertEqual(
            set(payload),
            {'schema_version', 'state', 'updated_at', 'reason'},
        )
        self.assertEqual(
            payload['schema_version'], provisioning.PROVISIONING_SCHEMA_VERSION
        )
        self.assertEqual(payload['state'], 'unclaimed')
        self.assertEqual(payload['reason'], 'ready')
        self.assertEqual(payload['updated_at'], FIXED_ISO)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)

        loaded = provisioning.ProvisioningState.load(self.path)
        self.assertEqual(loaded.state, 'unclaimed')
        self.assertEqual(loaded.reason, 'ready')
        self.assertEqual(loaded.updated_at, FIXED_ISO)

    def test_missing_file_loads_factory(self):
        loaded = provisioning.ProvisioningState.load(self.path)
        self.assertEqual(loaded.state, 'factory')
        self.assertEqual(loaded.reason, '')

    def test_corrupt_file_loads_factory_with_reason(self):
        cases = [
            ('not json at all', 'provisioning state unreadable'),
            ('[]', 'provisioning state is not an object'),
            ('{"state": "bogus"}', 'provisioning state value is invalid'),
        ]
        for text, expected_reason in cases:
            with self.subTest(text=text):
                path = os.path.join(self.root, 'corrupt.json')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                loaded = provisioning.ProvisioningState.load(path)
                self.assertEqual(loaded.state, 'factory')
                self.assertEqual(loaded.reason, expected_reason)

    def test_persisted_payload_never_contains_secrets(self):
        device_path = os.path.join(self.root, 'device.toml')
        secrets_path = os.path.join(self.root, 'secrets.toml')
        config_schema.save_device(config_schema.default_device(), path=device_path)
        config_schema.save_secrets(
            {
                'admin': {'password_hash': 'HASH-SYNTHETIC-VALUE'},
                'prusa': {'token': 'TOKEN-SYNTHETIC-VALUE'},
            },
            path=secrets_path,
        )
        facts = provisioning.observe_facts(
            device_path=device_path,
            secrets_path=secrets_path,
            camera_validated=True,
        )
        inst = self.make_state('camera_validated')
        self.assertTrue(inst.advance(now=FIXED_NOW, facts=facts).ok)

        with open(self.path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('HASH-SYNTHETIC-VALUE', text)
        self.assertNotIn('TOKEN-SYNTHETIC-VALUE', text)


class ReasonSanitizationTests(ProvisioningTestBase):
    def test_control_characters_are_stripped(self):
        inst = self.make_state('factory')
        result = inst.transition(
            'storage_ready', reason='bad\nreason\twith\x00null', now=FIXED_NOW
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.reason, 'badreasonwithnull')

    def test_reason_is_bounded(self):
        inst = self.make_state('factory')
        inst.transition('storage_ready', reason='x' * 500, now=FIXED_NOW)
        self.assertEqual(len(inst.reason), 200)

    def test_transition_result_has_no_secret_field(self):
        inst = self.make_state('factory')
        result = inst.transition('storage_ready', reason='ok', now=FIXED_NOW)
        self.assertEqual(
            set(result.__dataclass_fields__),
            {'ok', 'reason', 'state', 'previous', 'persisted'},
        )


class AdvanceTests(ProvisioningTestBase):
    def test_advance_walks_one_edge_per_call_toward_the_milestone(self):
        inst = self.make_state('factory')
        expected = [
            'storage_ready', 'camera_validated', 'unclaimed',
            'claimed', 'configured', 'running',
        ]
        for expected_state in expected:
            with self.subTest(expected=expected_state):
                result = inst.advance(now=FIXED_NOW, facts=FACTS_ALL)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(inst.state, expected_state)

        settled = inst.advance(now=FIXED_NOW, facts=FACTS_ALL)
        self.assertEqual(inst.state, 'running')
        self.assertEqual(settled.reason, 'no change')

    def test_camera_validated_is_visited_before_unclaimed(self):
        inst = self.make_state('storage_ready')
        inst.advance(now=FIXED_NOW, facts=FACTS_ALL)
        self.assertEqual(inst.state, 'camera_validated')

    def test_facts_short_of_the_next_milestone_do_not_advance(self):
        inst = self.make_state('factory')
        result = inst.advance(
            now=FIXED_NOW, facts=provisioning.Facts(storage_ready=False)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, 'no change')
        self.assertEqual(inst.state, 'factory')

    def test_advance_accepts_a_facts_mapping(self):
        inst = self.make_state('factory')
        result = inst.advance(now=FIXED_NOW, facts={'storage_ready': True})
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'storage_ready')

    def test_recovery_reason_forces_recovery(self):
        inst = self.make_state('factory')
        result = inst.advance(
            now=FIXED_NOW,
            facts=provisioning.Facts(recovery_reason='sensor fault'),
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'recovery')
        self.assertEqual(inst.reason, 'sensor fault')

        held = inst.advance(
            now=FIXED_NOW,
            facts=provisioning.Facts(recovery_reason='sensor fault'),
        )
        self.assertTrue(held.ok)
        self.assertEqual(inst.state, 'recovery')

        cleared = inst.advance(now=FIXED_NOW, facts=provisioning.Facts())
        self.assertTrue(cleared.ok, cleared.reason)
        self.assertEqual(inst.state, 'configured')

    def test_invalid_durable_configuration_enters_recovery(self):
        inst = self.make_state('configured')
        result = inst.advance(
            now=FIXED_NOW, facts=provisioning.Facts(storage_ready=False)
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'recovery')
        self.assertIn('durable configuration is invalid', inst.reason)

    def test_claimed_with_invalid_device_enters_recovery(self):
        inst = self.make_state('claimed')
        facts = provisioning.Facts(
            storage_ready=True, admin_password_set=True, device_valid=False
        )
        result = inst.advance(now=FIXED_NOW, facts=facts)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'recovery')


class RecoverTests(ProvisioningTestBase):
    def test_recover_returns_to_last_good(self):
        inst = self.make_state('recovery', last_good='configured')
        result = inst.recover(now=FIXED_NOW)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'configured')

    def test_recover_defaults_to_configured(self):
        inst = self.make_state('recovery')
        result = inst.recover(now=FIXED_NOW)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(inst.state, 'configured')

    def test_recover_outside_recovery_is_rejected(self):
        inst = self.make_state('factory')
        result = inst.recover(now=FIXED_NOW)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'not in recovery')
        self.assertEqual(inst.state, 'factory')


class ObserveFactsTests(ProvisioningTestBase):
    def test_reads_device_and_secrets(self):
        device_path = os.path.join(self.root, 'device.toml')
        secrets_path = os.path.join(self.root, 'secrets.toml')
        config_schema.save_device(config_schema.default_device(), path=device_path)
        config_schema.save_secrets(
            {
                'admin': {'password_hash': 'hash-synthetic'},
                'prusa': {'token': 'token-synthetic'},
            },
            path=secrets_path,
        )

        facts = provisioning.observe_facts(
            device_path=device_path,
            secrets_path=secrets_path,
            camera_validated=True,
            camera_running=True,
            recovery_reason='why',
        )
        self.assertTrue(facts.storage_ready)
        self.assertTrue(facts.camera_validated)
        self.assertTrue(facts.admin_password_set)
        self.assertTrue(facts.device_valid)
        self.assertTrue(facts.prusa_token_set)
        self.assertTrue(facts.camera_running)
        self.assertEqual(facts.recovery_reason, 'why')

    def test_invalid_device_is_not_valid(self):
        device_path = os.path.join(self.root, 'device.toml')
        with open(device_path, 'w', encoding='utf-8') as f:
            f.write('camera_name = 5\n')
        facts = provisioning.observe_facts(
            device_path=device_path,
            secrets_path=os.path.join(self.root, 'missing-secrets.toml'),
        )
        self.assertFalse(facts.device_valid)
        self.assertFalse(facts.admin_password_set)
        self.assertFalse(facts.prusa_token_set)

    def test_missing_files_yield_empty_secrets(self):
        facts = provisioning.observe_facts(
            device_path=os.path.join(self.root, 'nope.toml'),
            secrets_path=os.path.join(self.root, 'nope-secrets.toml'),
        )
        self.assertFalse(facts.device_valid)
        self.assertFalse(facts.admin_password_set)
        self.assertFalse(facts.prusa_token_set)


if __name__ == '__main__':
    unittest.main()
