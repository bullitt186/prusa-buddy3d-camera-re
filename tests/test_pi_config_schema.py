"""WP-1 AC-3: versioned TOML configuration schema (config_schema)."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import config_schema  # noqa: E402


class ConstantsTests(unittest.TestCase):
    def test_schema_version_and_paths(self):
        self.assertEqual(config_schema.SCHEMA_VERSION, 1)
        self.assertEqual(config_schema.CONFIG_DIR, '/data/prusa-cam/config')
        self.assertEqual(config_schema.DEVICE_TOML_PATH, '/data/prusa-cam/config/device.toml')
        self.assertEqual(config_schema.SECRETS_TOML_PATH, '/data/prusa-cam/config/secrets.toml')

    def test_exception_hierarchy(self):
        for exc in (config_schema.SchemaTooNewError, config_schema.UnknownKeyError,
                    config_schema.ValidationError):
            self.assertTrue(issubclass(exc, config_schema.ConfigError))
        self.assertTrue(issubclass(config_schema.ConfigError, Exception))


class DefaultDeviceTests(unittest.TestCase):
    def test_defaults(self):
        cfg = config_schema.default_device()
        self.assertEqual(cfg['schema_version'], 1)
        self.assertEqual(cfg['camera_name'], 'Printer Camera')
        self.assertEqual(cfg['fingerprint'], '')
        self.assertEqual(cfg['prusa']['server'], 'webcam.connect.prusa3d.com')
        self.assertIs(cfg['mqtt']['enabled'], False)
        self.assertEqual(cfg['mqtt']['uri'], 'mqtts://broker.example:8883')
        self.assertEqual(cfg['mqtt']['client_id'], '')
        self.assertEqual(cfg['mqtt']['discovery_prefix'], 'homeassistant')
        self.assertEqual(cfg['mqtt']['topic_prefix'], 'buddy3d')
        self.assertEqual(cfg['mqtt']['ca_file'], '')
        self.assertEqual(cfg['admin']['hostname'], '')

    def test_defaults_are_independent(self):
        a = config_schema.default_device()
        a['mqtt']['enabled'] = True
        self.assertIs(config_schema.default_device()['mqtt']['enabled'], False)


class ParseDeviceTests(unittest.TestCase):
    def test_minimal_document_fills_defaults_and_version(self):
        cfg = config_schema.parse_device('camera_name = "Shop"\n')
        self.assertEqual(cfg['schema_version'], 1)
        self.assertEqual(cfg['camera_name'], 'Shop')
        self.assertEqual(cfg['prusa']['server'], 'webcam.connect.prusa3d.com')

    def test_full_round_trip(self):
        text = config_schema.dumps_device(config_schema.default_device())
        self.assertEqual(config_schema.parse_device(text), config_schema.default_device())

    def test_camera_name_is_stripped(self):
        cfg = config_schema.parse_device('camera_name = "  Bench  "\n')
        self.assertEqual(cfg['camera_name'], 'Bench')

    def test_meta_table_allows_arbitrary_string_keys(self):
        text = 'camera_name = "x"\n[meta]\norigin = "legacy"\nfuture-key = "v"\n'
        cfg = config_schema.parse_device(text)
        self.assertEqual(cfg['meta'], {'origin': 'legacy', 'future-key': 'v'})

    def test_meta_round_trips(self):
        cfg = config_schema.parse_device('camera_name = "x"\n[meta]\norigin = "legacy"\n')
        again = config_schema.parse_device(config_schema.dumps_device(cfg))
        self.assertEqual(again['meta'], {'origin': 'legacy'})

    def test_meta_value_must_be_string(self):
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_device('camera_name = "x"\n[meta]\ncount = 3\n')

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.parse_device('camera_name = "x"\nmystery = 1\n')
        self.assertIn('mystery', str(ctx.exception))

    def test_unknown_table_rejected(self):
        with self.assertRaises(config_schema.UnknownKeyError):
            config_schema.parse_device('[stream]\nmode = "x"\n')

    def test_unknown_nested_key_rejected(self):
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.parse_device('[mqtt]\nsecret = "nope"\n')
        self.assertIn('mqtt.secret', str(ctx.exception))

    def test_secret_misplaced_in_device_rejected(self):
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.parse_device('[prusa]\ntoken = "abc"\n')
        self.assertIn('prusa.token', str(ctx.exception))

    def test_schema_version_must_be_int(self):
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_device('schema_version = "1"\n')

    def test_too_new_schema_rejected(self):
        with self.assertRaises(config_schema.SchemaTooNewError):
            config_schema.parse_device('schema_version = 2\n')

    def test_mqtt_enabled_must_be_bool(self):
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_device('[mqtt]\nenabled = 1\n')

    def test_mqtt_uri_scheme_validated(self):
        for uri in ('http://broker.example', 'broker.example', ''):
            with self.assertRaises(config_schema.ValidationError):
                config_schema.parse_device(f'[mqtt]\nuri = "{uri}"\n')

    def test_mqtt_uri_accepts_mqtt_schemes(self):
        for uri in ('mqtt://broker.example:1883', 'mqtts://broker.example:8883'):
            cfg = config_schema.parse_device(f'[mqtt]\nuri = "{uri}"\n')
            self.assertEqual(cfg['mqtt']['uri'], uri)

    def test_mqtt_uri_userinfo_rejected(self):
        with self.assertRaises(config_schema.ValidationError) as ctx:
            config_schema.parse_device(
                '[mqtt]\nuri = "mqtts://user:hunter2@broker.example:8883"\n')
        self.assertNotIn('hunter2', str(ctx.exception))

    def test_mqtt_uri_out_of_range_port_rejected(self):
        for uri in ('mqtt://broker.example:99999', 'mqtt://broker.example:0'):
            with self.assertRaises(config_schema.ValidationError):
                config_schema.parse_device(f'[mqtt]\nuri = "{uri}"\n')

    def test_prefixes_validated(self):
        for key in ('discovery_prefix', 'topic_prefix'):
            for value in ('', 'bad prefix', 'bad!prefix'):
                with self.assertRaises(config_schema.ValidationError):
                    config_schema.parse_device(f'[mqtt]\n{key} = "{value}"\n')
            cfg = config_schema.parse_device(f'[mqtt]\n{key} = "a-b_c/d"\n')
            self.assertEqual(cfg['mqtt'][key], 'a-b_c/d')

    def test_camera_name_bounds(self):
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_device('camera_name = "   "\n')
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_device('camera_name = "' + 'x' * 65 + '"\n')

    def test_wrong_table_type_rejected(self):
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_device('prusa = 5\n')

    def test_invalid_toml_is_validation_error(self):
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_device('camera_name = "unterminated\n')


class ParseSecretsTests(unittest.TestCase):
    def test_allowlisted_keys(self):
        text = ('[prusa]\ntoken = "t"\n[mqtt]\nusername = "u"\npassword = "p"\n'
                '[wifi]\npsk = "s"\n[admin]\npassword_hash = "h"\n')
        cfg = config_schema.parse_secrets(text)
        self.assertEqual(cfg, {
            'prusa': {'token': 't'},
            'mqtt': {'username': 'u', 'password': 'p'},
            'wifi': {'psk': 's'},
            'admin': {'password_hash': 'h'},
        })

    def test_round_trip(self):
        cfg = {'prusa': {'token': 't'}, 'mqtt': {'password': 'p'}}
        self.assertEqual(config_schema.parse_secrets(config_schema.dumps_secrets(cfg)), cfg)

    def test_empty_document(self):
        self.assertEqual(config_schema.parse_secrets(''), {})
        self.assertEqual(config_schema.dumps_secrets({}), '')

    def test_unknown_key_rejected(self):
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.parse_secrets('[prusa]\napi_key = "x"\n')
        self.assertIn('prusa.api_key', str(ctx.exception))

    def test_unknown_table_rejected(self):
        with self.assertRaises(config_schema.UnknownKeyError):
            config_schema.parse_secrets('[token]\nvalue = "x"\n')

    def test_exception_never_contains_secret_value(self):
        text = '[prusa]\ntoken = "supersecret-value"\n[rogue]\nkey = "supersecret-value"\n'
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.parse_secrets(text)
        self.assertNotIn('supersecret-value', str(ctx.exception))

    def test_schema_version_too_new_rejected(self):
        with self.assertRaises(config_schema.SchemaTooNewError):
            config_schema.parse_secrets('schema_version = 99\n[prusa]\ntoken = "t"\n')

    def test_schema_version_is_not_returned(self):
        cfg = config_schema.parse_secrets('schema_version = 1\n[prusa]\ntoken = "t"\n')
        self.assertEqual(cfg, {'prusa': {'token': 't'}})

    def test_non_string_value_rejected(self):
        with self.assertRaises(config_schema.ValidationError):
            config_schema.parse_secrets('[prusa]\ntoken = 5\n')


class MigrateTests(unittest.TestCase):
    def test_same_version_is_pure(self):
        original = {'schema_version': 1, 'camera_name': 'a'}
        result = config_schema.migrate_device(original, 1)
        self.assertEqual(result, original)
        self.assertIsNot(result, original)

    def test_too_new_rejected(self):
        with self.assertRaises(config_schema.SchemaTooNewError):
            config_schema.migrate_device({}, config_schema.SCHEMA_VERSION + 1)

    def test_synthetic_migration_chain(self):
        def v1_to_v2(cfg):
            migrated = dict(cfg)
            migrated['camera_name'] = cfg.get('camera_name', '') + ' v2'
            return migrated

        with patch.object(config_schema, 'SCHEMA_VERSION', 2), \
                patch.dict(config_schema.MIGRATIONS, {1: v1_to_v2}):
            result = config_schema.migrate_device({'schema_version': 1, 'camera_name': 'cam'}, 1)
        self.assertEqual(result['camera_name'], 'cam v2')
        self.assertEqual(result['schema_version'], 2)

    def test_missing_migration_raises(self):
        with patch.object(config_schema, 'SCHEMA_VERSION', 3), \
                patch.dict(config_schema.MIGRATIONS, {}):
            with self.assertRaises(config_schema.ConfigError):
                config_schema.migrate_device({'schema_version': 1}, 1)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'sub', 'device.toml')

    def test_write_creates_file_with_mode(self):
        config_schema.write_atomic(self.path, 'x = 1\n', mode=0o600)
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'x = 1\n')
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_failure_removes_temp_and_raises(self):
        with patch('config_schema.os.replace', side_effect=OSError('boom')):
            with self.assertRaises(config_schema.ConfigError):
                config_schema.write_atomic(self.path, 'x = 1\n')
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_midwrite_encoding_failure_removes_temp(self):
        with self.assertRaises(config_schema.ConfigError):
            config_schema.write_atomic(self.path, 'x = "\ud800"\n')
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + '.tmp'))


class SaveLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.device_path = os.path.join(self._tmp.name, 'device.toml')
        self.secrets_path = os.path.join(self._tmp.name, 'secrets.toml')

    def test_device_round_trip_and_mode(self):
        cfg = config_schema.default_device()
        cfg['camera_name'] = 'Bench'
        self.assertTrue(config_schema.save_device(cfg, path=self.device_path))
        self.assertEqual(config_schema.load_device(self.device_path)['camera_name'], 'Bench')
        self.assertEqual(stat.S_IMODE(os.stat(self.device_path).st_mode), 0o640)

    def test_secrets_round_trip_and_mode(self):
        cfg = {'prusa': {'token': 'tok'}, 'wifi': {'psk': 'psk'}}
        self.assertTrue(config_schema.save_secrets(cfg, path=self.secrets_path))
        self.assertEqual(config_schema.load_secrets(self.secrets_path), cfg)
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_path).st_mode), 0o600)

    def test_missing_files(self):
        self.assertEqual(config_schema.load_device(self.device_path),
                         config_schema.default_device())
        self.assertEqual(config_schema.load_secrets(self.secrets_path), {})

    def test_save_invalid_device_raises_before_write(self):
        cfg = config_schema.default_device()
        cfg['mqtt']['uri'] = 'http://bad'
        with self.assertRaises(config_schema.ConfigError):
            config_schema.save_device(cfg, path=self.device_path)
        self.assertFalse(os.path.exists(self.device_path))

    def test_save_io_failure_returns_false(self):
        with patch('config_schema.os.replace', side_effect=OSError('boom')):
            self.assertFalse(config_schema.save_device(
                config_schema.default_device(), path=self.device_path))

    def test_save_non_table_and_bad_scalar_raise_config_error(self):
        with self.assertRaises(config_schema.ConfigError):
            config_schema.save_device(['not', 'a', 'table'], path=self.device_path)
        with self.assertRaises(config_schema.ConfigError):
            config_schema.save_device({'camera_name': 5}, path=self.device_path)
        with self.assertRaises(config_schema.ConfigError):
            config_schema.save_secrets({'prusa': {'token': 5}}, path=self.secrets_path)
        self.assertFalse(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_save_device_unknown_top_level_key_rejected(self):
        cfg = config_schema.default_device()
        cfg['rogue_table'] = {'x': 1}
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.save_device(cfg, path=self.device_path)
        self.assertIn('rogue_table', str(ctx.exception))
        self.assertFalse(os.path.exists(self.device_path))

    def test_save_device_unknown_nested_key_rejected(self):
        cfg = config_schema.default_device()
        cfg['mqtt']['rogue'] = 'x'
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.save_device(cfg, path=self.device_path)
        self.assertIn('mqtt.rogue', str(ctx.exception))
        self.assertFalse(os.path.exists(self.device_path))

    def test_save_device_misplaced_secret_rejected(self):
        cfg = config_schema.default_device()
        cfg['prusa']['token'] = 'SECRET-VALUE'
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.save_device(cfg, path=self.device_path)
        self.assertIn('prusa.token', str(ctx.exception))
        self.assertNotIn('SECRET-VALUE', str(ctx.exception))
        self.assertFalse(os.path.exists(self.device_path))

    def test_save_secrets_unknown_key_rejected(self):
        cfg = {'prusa': {'token': 't', 'api_key': 'SECRET-VALUE'}}
        with self.assertRaises(config_schema.UnknownKeyError) as ctx:
            config_schema.save_secrets(cfg, path=self.secrets_path)
        self.assertIn('prusa.api_key', str(ctx.exception))
        self.assertNotIn('SECRET-VALUE', str(ctx.exception))
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_save_secrets_unknown_table_rejected(self):
        with self.assertRaises(config_schema.UnknownKeyError):
            config_schema.save_secrets({'rogue': {'x': 'y'}}, path=self.secrets_path)
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_load_too_new_does_not_modify_file(self):
        text = 'schema_version = 2\ncamera_name = "Future"\n'
        with open(self.device_path, 'w', encoding='utf-8') as f:
            f.write(text)
        with self.assertRaises(config_schema.SchemaTooNewError):
            config_schema.load_device(self.device_path)
        with open(self.device_path, encoding='utf-8') as f:
            self.assertEqual(f.read(), text)


class HostileStringTests(unittest.TestCase):
    """The bounded writer must round-trip arbitrary UTF-8 through tomllib."""

    HOSTILE_VALUES = [
        '"',
        '\\',
        '\n',
        '\r',
        '\t',
        '\x00',
        '\x01\x1f',
        '\x7f',
        '',
        'quote"and\\slash',
        '#',
        '[',
        '=',
        'a=b',
        'line\nbreak',
        '😀',
        '日本語',
        'nul\x00end',
        'carriage\rreturn',
    ]

    META_KEYS = ['', '#', '[', '=', 'a.b', 'weird key', 'ключ', '😀', 'tab\tkey']

    def test_hostile_values_round_trip_device(self):
        for value in self.HOSTILE_VALUES:
            with self.subTest(value=value):
                cfg = config_schema.default_device()
                cfg['fingerprint'] = value
                cfg['prusa']['server'] = value
                cfg['mqtt']['client_id'] = value
                cfg['mqtt']['ca_file'] = value
                cfg['admin']['hostname'] = value
                cfg['meta'] = {'weird key': value, 'a.b': value}
                out = config_schema.parse_device(config_schema.dumps_device(cfg))
                self.assertEqual(out['fingerprint'], value)
                self.assertEqual(out['prusa']['server'], value)
                self.assertEqual(out['mqtt']['client_id'], value)
                self.assertEqual(out['mqtt']['ca_file'], value)
                self.assertEqual(out['admin']['hostname'], value)
                self.assertEqual(out['meta'], {'weird key': value, 'a.b': value})

    def test_unusual_meta_keys_round_trip(self):
        cfg = config_schema.default_device()
        cfg['meta'] = {key: 'value' for key in self.META_KEYS}
        out = config_schema.parse_device(config_schema.dumps_device(cfg))
        self.assertEqual(out['meta'], cfg['meta'])

    def test_hostile_values_round_trip_secrets(self):
        for value in self.HOSTILE_VALUES:
            with self.subTest(value=value):
                cfg = {
                    'prusa': {'token': value},
                    'mqtt': {'username': value, 'password': value},
                    'wifi': {'psk': value},
                    'admin': {'password_hash': value},
                }
                out = config_schema.parse_secrets(config_schema.dumps_secrets(cfg))
                self.assertEqual(out, cfg)


if __name__ == '__main__':
    unittest.main()
