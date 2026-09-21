"""WP-3d1 AC-20: expert TOML validate-before-apply (expert_config).

Host-only. All paths are ``tempfile`` paths; no ``/data`` or ``/etc`` file is
touched.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import config_schema  # noqa: E402
import expert_config  # noqa: E402

SECRET = 'SUPERSECRET-TOKEN-VALUE'

VALID_DEVICE = config_schema.dumps_device(config_schema.default_device())
VALID_SECRETS = config_schema.dumps_secrets(
    {'prusa': {'token': SECRET}, 'admin': {'password_hash': 'scrypt$fake'}}
)


class ExpertTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.device_path = os.path.join(self.root, 'device.toml')
        self.secrets_path = os.path.join(self.root, 'secrets.toml')

    def write_live(self, device_text, secrets_text):
        with open(self.device_path, 'w', encoding='utf-8') as f:
            f.write(device_text)
        with open(self.secrets_path, 'w', encoding='utf-8') as f:
            f.write(secrets_text)

    def read_bytes(self, path):
        with open(path, 'rb') as f:
            return f.read()


class ImportSafetyTests(unittest.TestCase):
    def test_import_runs_no_write(self):
        with patch.object(os, 'open', side_effect=AssertionError('os.open on import')):
            importlib.reload(expert_config)
        self.assertTrue(callable(expert_config.validate_candidate))
        self.assertTrue(callable(expert_config.apply_candidate))
        self.assertTrue(callable(expert_config.current_config))


class ValidateCandidateTests(ExpertTestBase):
    def test_valid_candidate_round_trips(self):
        ok, reason, device, secrets = expert_config.validate_candidate(
            VALID_DEVICE, VALID_SECRETS
        )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, '')
        self.assertEqual(device['camera_name'], 'Printer Camera')
        self.assertEqual(secrets['prusa']['token'], SECRET)

    def test_secrets_are_optional(self):
        ok, reason, device, secrets = expert_config.validate_candidate(VALID_DEVICE)
        self.assertTrue(ok, reason)
        self.assertEqual(secrets, {})

    def test_unknown_device_key_is_rejected(self):
        text = VALID_DEVICE + '\n[evil]\nx = 1\n'
        ok, reason, device, secrets = expert_config.validate_candidate(text)
        self.assertFalse(ok)
        self.assertIn('unknown', reason)
        self.assertEqual(device, {})
        self.assertEqual(secrets, {})

    def test_bad_value_is_rejected(self):
        text = VALID_DEVICE.replace('enabled = false', 'enabled = "yes"')
        ok, reason, _device, _secrets = expert_config.validate_candidate(text)
        self.assertFalse(ok)
        self.assertIn('enabled', reason)

    def test_too_new_schema_is_rejected(self):
        text = VALID_DEVICE.replace('schema_version = 1', 'schema_version = 99')
        ok, reason, _device, _secrets = expert_config.validate_candidate(text)
        self.assertFalse(ok)
        self.assertIn('newer', reason)

    def test_invalid_toml_is_rejected(self):
        ok, reason, _device, _secrets = expert_config.validate_candidate('not = = toml')
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_non_text_is_rejected(self):
        ok, reason, _device, _secrets = expert_config.validate_candidate(None)
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_unknown_secrets_key_is_rejected(self):
        secrets_text = VALID_SECRETS + '\n[unknown]\nx = "y"\n'
        ok, reason, _device, _secrets = expert_config.validate_candidate(
            VALID_DEVICE, secrets_text
        )
        self.assertFalse(ok)
        self.assertIn('unknown', reason)

    def test_secret_value_never_appears_in_reason(self):
        bad_device = VALID_DEVICE.replace(
            'camera_name = "Printer Camera"', 'camera_name = ""'
        )
        ok, reason, _device, _secrets = expert_config.validate_candidate(
            bad_device, VALID_SECRETS
        )
        self.assertFalse(ok)
        self.assertNotIn(SECRET, reason)

        malformed_secrets = '[prusa]\ntoken = "%s"\n[unknown]\nx = "y"\n' % SECRET
        ok, reason, _device, _secrets = expert_config.validate_candidate(
            VALID_DEVICE, malformed_secrets
        )
        self.assertFalse(ok)
        self.assertNotIn(SECRET, reason)


class ApplyCandidateTests(ExpertTestBase):
    def test_valid_candidate_applies_and_round_trips(self):
        result = expert_config.apply_candidate(
            VALID_DEVICE, VALID_SECRETS,
            device_path=self.device_path, secrets_path=self.secrets_path,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.wrote, ('device', 'secrets'))
        device, secrets = expert_config.current_config(
            device_path=self.device_path, secrets_path=self.secrets_path
        )
        self.assertEqual(device['camera_name'], 'Printer Camera')
        self.assertEqual(secrets['prusa']['token'], SECRET)

    def test_valid_device_without_secrets_writes_only_device(self):
        result = expert_config.apply_candidate(
            VALID_DEVICE, device_path=self.device_path, secrets_path=self.secrets_path
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.wrote, ('device',))
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_invalid_candidate_leaves_live_files_byte_identical(self):
        self.write_live(VALID_DEVICE, VALID_SECRETS)
        device_before = self.read_bytes(self.device_path)
        secrets_before = self.read_bytes(self.secrets_path)

        bad_device = VALID_DEVICE + '\n[evil]\nx = 1\n'
        result = expert_config.apply_candidate(
            bad_device, VALID_SECRETS,
            device_path=self.device_path, secrets_path=self.secrets_path,
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.read_bytes(self.device_path), device_before)
        self.assertEqual(self.read_bytes(self.secrets_path), secrets_before)

    def test_invalid_candidate_creates_no_files(self):
        result = expert_config.apply_candidate(
            'garbage = =',
            device_path=self.device_path, secrets_path=self.secrets_path,
        )
        self.assertFalse(result.ok)
        self.assertFalse(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_partial_write_rolls_back_device(self):
        original = config_schema.dumps_device(config_schema.default_device())
        with open(self.device_path, 'w', encoding='utf-8') as f:
            f.write(original)
        device_before = self.read_bytes(self.device_path)

        candidate = config_schema.dumps_device(config_schema.default_device())
        candidate = candidate.replace('Printer Camera', 'Renamed Camera')

        with patch.object(config_schema, 'save_secrets', return_value=False):
            result = expert_config.apply_candidate(
                candidate, VALID_SECRETS,
                device_path=self.device_path, secrets_path=self.secrets_path,
            )
        self.assertFalse(result.ok)
        self.assertIn('secrets', result.reason)
        self.assertEqual(self.read_bytes(self.device_path), device_before)


class CurrentConfigTests(ExpertTestBase):
    def test_missing_files_yield_defaults(self):
        device, secrets = expert_config.current_config(
            device_path=self.device_path, secrets_path=self.secrets_path
        )
        self.assertEqual(device, config_schema.default_device())
        self.assertEqual(secrets, {})

    def test_invalid_files_yield_defaults(self):
        self.write_live('nonsense = =', 'also = =')
        device, secrets = expert_config.current_config(
            device_path=self.device_path, secrets_path=self.secrets_path
        )
        self.assertEqual(device, config_schema.default_device())
        self.assertEqual(secrets, {})

    def test_reads_live_files(self):
        self.write_live(VALID_DEVICE, VALID_SECRETS)
        device, secrets = expert_config.current_config(
            device_path=self.device_path, secrets_path=self.secrets_path
        )
        self.assertEqual(device['camera_name'], 'Printer Camera')
        self.assertEqual(secrets['prusa']['token'], SECRET)


if __name__ == '__main__':
    unittest.main()
