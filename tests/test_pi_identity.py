import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from identity import (  # noqa: E402
    FALLBACK_SEED_ALPHABET,
    FALLBACK_SEED_LENGTH,
    fingerprint_from_mac,
    fingerprint_from_seed,
    generate_fallback_seed,
    identity_from_mac_or_fallback,
    load_or_create_fallback_seed,
    normalize_wifi_mac,
    resolve_fingerprint,
)


class FirmwareIdentityTests(unittest.TestCase):
    def test_normalizes_sysfs_mac_like_firmware_sprintf(self):
        self.assertEqual(
            normalize_wifi_mac('d8:3a:dd:32:1c:ac\n'),
            'D8:3A:DD:32:1C:AC',
        )

    def test_accepts_hyphenated_mac_and_normalizes_separators(self):
        self.assertEqual(
            normalize_wifi_mac('00-e0-4c-86-17-ff'),
            '00:E0:4C:86:17:FF',
        )

    def test_fingerprint_hashes_exact_uppercase_colon_preimage(self):
        expected = hashlib.md5(b'00:E0:4C:86:17:FF').hexdigest()
        self.assertEqual(fingerprint_from_mac('00:e0:4c:86:17:ff'), expected)

    def test_rejects_malformed_mac(self):
        for value in ('', '00:11:22:33:44', 'not-a-mac', '001122334455'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_wifi_mac(value)


class FallbackSeedTests(unittest.TestCase):
    """GAP-IDENTITY-01: random ten-character seed when wlan0 is unavailable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'identity.fallback')

    def tearDown(self):
        self._tmp.cleanup()

    def test_fingerprint_from_seed_is_md5_of_exact_text(self):
        seed = 'Ab3xZ9q1Lm'
        self.assertEqual(
            fingerprint_from_seed(seed),
            hashlib.md5(seed.encode('utf-8')).hexdigest(),
        )

    def test_normal_path_vectors_unchanged(self):
        # Regression guard: adding the fallback must not alter the MAC path.
        self.assertEqual(
            fingerprint_from_mac('00:E0:4C:86:17:FF'),
            hashlib.md5(b'00:E0:4C:86:17:FF').hexdigest(),
        )

    def test_generated_seed_has_firmware_shape(self):
        seed = generate_fallback_seed()
        self.assertEqual(len(seed), FALLBACK_SEED_LENGTH)
        self.assertTrue(all(c in FALLBACK_SEED_ALPHABET for c in seed))

    def test_fallback_seed_persists_and_reuses_across_calls(self):
        first = load_or_create_fallback_seed(self.path)
        self.assertEqual(len(first), FALLBACK_SEED_LENGTH)
        self.assertTrue(os.path.exists(self.path))

        second = load_or_create_fallback_seed(self.path)
        self.assertEqual(first, second)
        self.assertEqual(fingerprint_from_seed(first), fingerprint_from_seed(second))

    def test_existing_seed_is_never_rotated(self):
        with open(self.path, 'w') as f:
            f.write('Fixed12345')
        self.assertEqual(load_or_create_fallback_seed(self.path), 'Fixed12345')
        with open(self.path) as f:
            self.assertEqual(f.read().strip(), 'Fixed12345')

    def test_invalid_existing_seed_is_regenerated(self):
        with open(self.path, 'w') as f:
            f.write('short')
        seed = load_or_create_fallback_seed(self.path)
        self.assertEqual(len(seed), FALLBACK_SEED_LENGTH)
        with open(self.path) as f:
            self.assertEqual(f.read().strip(), seed)


class IdentityResolutionTests(unittest.TestCase):
    """GAP-IDENTITY-01 wiring: normal MAC path vs persisted fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'identity.fallback')

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_mac_path_is_byte_exact(self):
        mac, fingerprint = identity_from_mac_or_fallback('d8:3a:dd:32:1c:ac\n', self.path)
        self.assertEqual(mac, 'D8:3A:DD:32:1C:AC')
        self.assertEqual(fingerprint, hashlib.md5(b'D8:3A:DD:32:1C:AC').hexdigest())
        self.assertFalse(os.path.exists(self.path))

    def test_missing_or_invalid_mac_uses_persisted_fallback(self):
        for raw in ('', '   ', 'not-a-mac', None):
            with self.subTest(raw=raw):
                mac, fingerprint = identity_from_mac_or_fallback(raw, self.path)
                self.assertEqual(mac, '')
                self.assertEqual(len(load_or_create_fallback_seed(self.path)), FALLBACK_SEED_LENGTH)
                self.assertEqual(
                    fingerprint,
                    fingerprint_from_seed(load_or_create_fallback_seed(self.path)),
                )

    def test_fallback_fingerprint_is_deterministic_across_calls(self):
        first = identity_from_mac_or_fallback('bad', self.path)
        second = identity_from_mac_or_fallback('bad', self.path)
        self.assertEqual(first, second)


class ResolveFingerprintTests(unittest.TestCase):
    """A registered config.ini fingerprint must win over the MAC-derived one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'identity.fallback')

    def tearDown(self):
        self._tmp.cleanup()

    def test_configured_fingerprint_wins_and_is_returned_verbatim(self):
        configured = 'd54ac883deadbeefdeadbeefdeadbeef'
        mac, fingerprint = resolve_fingerprint(configured, 'd8:3a:dd:32:1c:ac', self.path)
        self.assertEqual(mac, 'D8:3A:DD:32:1C:AC')
        self.assertEqual(fingerprint, configured)
        # A configured identity must never create the fallback seed file.
        self.assertFalse(os.path.exists(self.path))

    def test_configured_fingerprint_with_unreadable_mac_still_wins(self):
        configured = 'd54ac883deadbeefdeadbeefdeadbeef'
        for raw in ('', 'not-a-mac', None):
            with self.subTest(raw=raw):
                mac, fingerprint = resolve_fingerprint(configured, raw, self.path)
                self.assertEqual(mac, '')
                self.assertEqual(fingerprint, configured)
                self.assertFalse(os.path.exists(self.path))

    def test_absent_configured_fingerprint_falls_back_to_mac(self):
        mac, fingerprint = resolve_fingerprint(None, 'd8:3a:dd:32:1c:ac', self.path)
        self.assertEqual(mac, 'D8:3A:DD:32:1C:AC')
        self.assertEqual(fingerprint, fingerprint_from_mac(mac))

    def test_absent_configured_and_bad_mac_uses_persisted_seed(self):
        mac, fingerprint = resolve_fingerprint(None, 'bad', self.path)
        self.assertEqual(mac, '')
        seed = load_or_create_fallback_seed(self.path)
        self.assertEqual(fingerprint, fingerprint_from_seed(seed))


class MainWiringTests(unittest.TestCase):
    """Guard against re-introducing the live identity regression.

    main() must pass the configured ``[identity] fingerprint`` into
    ``get_network_info`` so a registered token keeps its bound fingerprint. This
    is the second identity regression in a row, so pin the wiring with an AST
    check (main.py cannot be imported here: it needs aiohttp/socketio/gi).
    """

    def test_main_passes_configured_fingerprint(self):
        import ast

        src = (Path(__file__).resolve().parents[1] / 'pi-impersonator' / 'main.py').read_text()
        calls = [
            node for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'get_network_info'
        ]
        self.assertEqual(len(calls), 1, 'expected exactly one get_network_info call')
        self.assertEqual(len(calls[0].args), 1, 'get_network_info must receive the config value')
        arg_src = ast.unparse(calls[0].args[0])
        self.assertIn('identity', arg_src)
        self.assertIn('fingerprint', arg_src)


if __name__ == '__main__':
    unittest.main()
