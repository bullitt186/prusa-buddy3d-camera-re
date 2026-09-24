"""main.load_config must bridge the appliance documents (hardware-found).

The appliance stores configuration in /data/prusa-cam/config/device.toml and
secrets.toml, not the dev-Pi config.ini. Before the fix, main.py died with
``KeyError: 'identity'`` on a freshly claimed device, so prusa-cam crash-looped
and the camera target failed. These tests pin the bridge using AST only, since
main.py cannot be imported on the host (it needs aiohttp/socketio).
"""

import ast
import unittest
from pathlib import Path

MAIN = (
    Path(__file__).resolve().parent.parent
    / 'pi-impersonator'
    / 'main.py'
)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f'missing function {name!r}')


class ApplianceConfigBridgeTests(unittest.TestCase):
    def setUp(self):
        self.source = MAIN.read_text(encoding='utf-8')
        self.tree = ast.parse(self.source)

    def test_load_config_reads_the_durable_documents(self):
        text = ast.unparse(_func(self.tree, 'load_config'))
        self.assertIn('config_schema.load_device', text)
        self.assertIn('config_schema.load_secrets', text)
        # The legacy cfg shape main() consumes.
        self.assertIn("'identity'", text)
        self.assertIn("'upload'", text)
        self.assertIn("'token'", text)
        self.assertIn("'server'", text)

    def test_main_loads_config_before_reading_identity(self):
        main = _func(self.tree, 'main')
        text = ast.unparse(main)
        self.assertIn('cfg = load_config()', text)
        self.assertIn("cfg['identity']['token']", text)


if __name__ == '__main__':
    unittest.main()
