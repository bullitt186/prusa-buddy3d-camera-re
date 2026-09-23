"""WP-3e2: static checks for the aiohttp admin transport and its unit.

The transport imports ``aiohttp`` at module top, which is not a host-test
dependency, so this module never imports :mod:`admin_app`. It parses the source
with :mod:`ast` and asserts the shape the contract requires:

* the aiohttp routes map 1:1 onto :class:`admin_http.AdminApp`'s route table;
* ``peer_ip`` comes from the socket transport, never a forwarding header;
* the ``Response`` (including ``Set-Cookie``) is passed through verbatim;
* ``prusa-admin.service`` is a valid, non-personal unit gated on
  ``data-ready.target``; and
* the factory installer installs and enables the unit.

Stdlib-only, no ``aiohttp`` import.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PI_DIR = REPO / 'pi-impersonator'
ADMIN_APP = PI_DIR / 'admin_app.py'
UNIT = PI_DIR / 'systemd' / 'prusa-admin.service'
INSTALLER = REPO / 'image' / 'assets' / 'install-factory-app.sh'

sys.path.insert(0, str(PI_DIR))

import admin_http  # noqa: E402


def _tree():
    return ast.parse(ADMIN_APP.read_text(encoding='utf-8'))


def _module_assign(tree, name):
    """Return the value node assigned to module-level ``name``, or ``None``."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
    return None


def _attr_chains(tree):
    """Return every dotted attribute chain as a list of names (e.g. admin_http.Request)."""
    chains = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts = []
            current = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
                chains.append(list(reversed(parts)))
    return chains


def _calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _docstring_values(tree):
    """Return the text of every module/class/function docstring in ``tree``."""
    values = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                values.add(doc)
    return values


def _string_constants(tree):
    """Return string constants, excluding docstrings (prose is not a header read)."""
    docstrings = _docstring_values(tree)
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value not in docstrings
    ]


def _canonical_admin_pattern(pattern):
    """Convert an admin_http regex pattern to the aiohttp path shape."""
    text = pattern
    if text.startswith('^'):
        text = text[1:]
    if text.endswith('$'):
        text = text[:-1]
    return re.sub(r'\(\?P<([^>]+)>\[\^/\]\+\)', r'{\1}', text)


def _admin_http_routes():
    """Return the accepted core's route table as ``{(method, path)}``."""
    return {
        (route.method, _canonical_admin_pattern(route.pattern.pattern))
        for route in admin_http.AdminApp()._routes
    }


class AdminTransportSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = ADMIN_APP.read_text(encoding='utf-8')
        self.tree = ast.parse(self.source)
        self.chains = _attr_chains(self.tree)

    def test_admin_app_parses_and_imports_aiohttp(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or '')
        self.assertIn('aiohttp', imported)

    def test_references_admin_http_core_symbols(self):
        self.assertIn(['admin_http', 'AdminApp'], self.chains)
        self.assertIn(['admin_http', 'Request'], self.chains)
        self.assertIn(['admin_http', 'Response'], self.chains)

    def test_route_table_matches_admin_http(self):
        routes_node = _module_assign(self.tree, 'ROUTES')
        self.assertIsNotNone(routes_node, 'admin_app must declare a ROUTES table')
        declared = {tuple(entry) for entry in ast.literal_eval(routes_node)}
        self.assertEqual(declared, _admin_http_routes())

    def test_routes_are_registered_from_the_route_table(self):
        for_loops = [
            node for node in ast.walk(self.tree) if isinstance(node, ast.For)
        ]
        self.assertTrue(
            any(
                isinstance(node.iter, ast.Name) and node.iter.id == 'ROUTES'
                for node in for_loops
            ),
            'ROUTES must drive route registration',
        )
        add_route_calls = [
            call for call in _calls(self.tree)
            if any(
                chain == ['app', 'router', 'add_route']
                for chain in _attr_chains(call)
            )
        ]
        self.assertTrue(add_route_calls, 'the transport must register aiohttp routes')

    def test_peer_ip_derives_from_transport(self):
        functions = [
            node for node in self.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == '_peer_ip'
        ]
        self.assertEqual(len(functions), 1, 'expected a single _peer_ip helper')
        helper_calls = _calls(functions[0])
        self.assertTrue(
            any(
                any(chain[-1:] == ['get_extra_info'] for chain in _attr_chains(call))
                for call in helper_calls
            ),
            'peer_ip must read the transport peername via get_extra_info',
        )
        self.assertIn(
            'peername',
            _string_constants(functions[0]),
            'peer_ip must read the socket peername',
        )

    def test_ignores_forwarding_headers(self):
        lowered = [value.lower() for value in _string_constants(self.tree)]
        for header in ('x-forwarded-for', 'x-real-ip'):
            self.assertFalse(
                any(header in value for value in lowered),
                f'{header} must never be read',
            )

    def test_does_not_import_main(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                self.assertNotIn('main', [alias.name for alias in node.names])
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, 'main')

    def test_response_headers_pass_through(self):
        response_calls = [
            call for call in _calls(self.tree)
            if any(chain == ['web', 'Response'] for chain in _attr_chains(call))
        ]
        self.assertTrue(response_calls, 'expected a web.Response translation')
        self.assertTrue(
            any(
                keyword.arg == 'headers' for call in response_calls
                for keyword in call.keywords
            ),
            'the core Response headers (incl. Set-Cookie) must be forwarded',
        )

    def test_mode_bind_ports(self):
        setup_port = _module_assign(self.tree, 'DEFAULT_SETUP_PORT')
        admin_port = _module_assign(self.tree, 'DEFAULT_ADMIN_PORT')
        self.assertEqual(ast.literal_eval(setup_port), 80)
        self.assertEqual(ast.literal_eval(admin_port), 443)

    def test_mode_is_env_selectable(self):
        self.assertIn('ADMIN_MODE', _string_constants(self.tree))
        self.assertIn(
            'resolve_mode',
            [node.name for node in ast.walk(self.tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))],
        )

    def test_build_admin_app_wires_privileged_defaults(self):
        functions = [
            node for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'build_admin_app'
        ]
        self.assertEqual(len(functions), 1, 'expected a single build_admin_app')
        admin_calls = [
            call for call in _calls(functions[0])
            if any(chain == ['admin_http', 'AdminApp'] for chain in _attr_chains(call))
        ]
        self.assertTrue(admin_calls, 'build_admin_app must construct AdminApp')
        passed_keywords = {
            keyword.arg
            for call in admin_calls for keyword in call.keywords
        }
        for keyword in ('start_camera', 'activate_station', 'hotspot'):
            self.assertIn(
                keyword, passed_keywords,
                f'build_admin_app must pass {keyword} to AdminApp',
            )
        chains = _attr_chains(functions[0])
        self.assertIn(
            ['privileged', 'start_camera'], chains,
            'start_camera must default to privileged.start_camera',
        )
        self.assertIn(
            ['privileged', 'activate_station'], chains,
            'activate_station must default to privileged.activate_station',
        )
        self.assertIn(
            ['privileged', 'PrivilegedHotspot'], chains,
            'the default hotspot controller must be privileged.PrivilegedHotspot',
        )

    def test_device_id_falls_back_to_resolve_device_id(self):
        functions = [
            node for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == '_configured_device_id'
        ]
        self.assertEqual(len(functions), 1, 'expected a single _configured_device_id')
        self.assertTrue(
            any(
                chain == ['provisioning', 'resolve_device_id']
                for chain in _attr_chains(functions[0])
            ),
            'setup mode needs provisioning.resolve_device_id for a pre-claim id',
        )

    def test_build_admin_app_injects_the_broker_probe(self):
        # WP-R2: admin_app injects mqtt_probe.probe as the default broker test.
        functions = [
            node for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'build_admin_app'
        ]
        self.assertEqual(len(functions), 1, 'expected a single build_admin_app')
        admin_calls = [
            call for call in _calls(functions[0])
            if any(chain == ['admin_http', 'AdminApp'] for chain in _attr_chains(call))
        ]
        self.assertTrue(admin_calls, 'build_admin_app must construct AdminApp')
        passed_keywords = {
            keyword.arg
            for call in admin_calls for keyword in call.keywords
        }
        self.assertIn('mqtt_probe', passed_keywords)
        self.assertIn(
            ['mqtt_probe', 'probe'], self.chains,
            'the default broker test must be mqtt_probe.probe',
        )
        # The parameter must accept an injected override.
        args = functions[0].args
        self.assertIn('mqtt_probe', [a.arg for a in args.kwonlyargs])

    def test_route_table_declares_the_broker_test(self):
        routes_node = _module_assign(self.tree, 'ROUTES')
        declared = {tuple(entry) for entry in ast.literal_eval(routes_node)}
        self.assertIn(('POST', '/api/mqtt/test'), declared)


class AdminUnitTests(unittest.TestCase):
    def setUp(self):
        self.text = UNIT.read_text(encoding='utf-8')
        self.lines = self.text.splitlines()

    def _values(self, key):
        prefix = key + '='
        return [line[len(prefix):] for line in self.lines if line.startswith(prefix)]

    def test_unit_syntax_is_key_value_lines(self):
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith('['):
                continue
            self.assertIn('=', stripped, f'not a systemd key=value line: {line!r}')
            key, _, value = stripped.partition('=')
            self.assertTrue(key, f'empty systemd key in: {line!r}')
            self.assertTrue(value, f'empty systemd value in: {line!r}')

    def test_runs_as_service_account_from_app_root(self):
        self.assertEqual(self._values('User'), ['prusa-cam'])
        self.assertEqual(self._values('WorkingDirectory'), ['/opt/prusa-cam'])

    def test_execstart_runs_the_transport_entry_point(self):
        exec_start = self._values('ExecStart')
        self.assertEqual(len(exec_start), 1)
        self.assertIn('/opt/prusa-cam/venv/bin/python', exec_start[0])
        self.assertIn('/opt/prusa-cam/admin_app.py', exec_start[0])

    def test_gated_on_data_ready(self):
        self.assertTrue(any('data-ready.target' in v for v in self._values('After')))
        self.assertTrue(any('data-ready.target' in v for v in self._values('Requires')))

    def test_binds_privileged_ports(self):
        self.assertTrue(
            any('CAP_NET_BIND_SERVICE' in v for v in self._values('AmbientCapabilities'))
        )

    def test_restart_is_bounded_on_failure(self):
        self.assertEqual(self._values('Restart'), ['on-failure'])
        self.assertTrue(self._values('RestartSec'))
        self.assertTrue(self._values('StartLimitIntervalSec'))
        self.assertTrue(self._values('StartLimitBurst'))

    def test_documents_mode(self):
        self.assertIn('ADMIN_MODE=admin', self._values('Environment'))

    def test_installs_under_camera_target(self):
        install_index = self.lines.index('[Install]')
        self.assertIn('WantedBy=prusa-camera.target', self.lines[install_index:])

    def test_no_personal_username_or_home_path(self):
        self.assertNotIn('bullitt', self.text)
        self.assertNotIn('/home/', self.text)


class FactoryInstallerTests(unittest.TestCase):
    def test_installer_installs_admin_unit_but_defers_enablement(self):
        text = INSTALLER.read_text(encoding='utf-8')
        unit_loop = text.split('systemctl enable', 1)[0]
        enable_block = text.split('systemctl enable', 1)[1]
        # Installed verbatim; enabled only transitively via prusa-camera.target
        # after claim, never directly at multi-user.target.
        self.assertIn('prusa-admin.service', unit_loop)
        self.assertNotIn('prusa-admin.service', enable_block)

    def test_installer_installs_provisioning_and_boot_mode_units(self):
        text = INSTALLER.read_text(encoding='utf-8')
        unit_loop = text.split('systemctl enable', 1)[0]
        self.assertIn('prusa-provisioning.service', unit_loop)
        self.assertIn('prusa-boot-mode.service', unit_loop)


class CoreOffloadTests(unittest.TestCase):
    """AC-27: the blocking stdlib core must not run on the aiohttp loop.

    The MQTT broker-test route blocks on DNS/TCP/TLS/paho waits, so the core is
    dispatched to a dedicated single-worker executor: off the loop, but still
    serialized because AdminApp has no internal locks.
    """

    def test_core_runs_on_a_single_worker_executor(self):
        tree = _tree()
        executor = _module_assign(tree, '_CORE_EXECUTOR')
        self.assertIsNotNone(executor, '_CORE_EXECUTOR must be defined')
        self.assertIn(
            ['concurrent', 'futures', 'ThreadPoolExecutor'],
            _attr_chains(executor),
        )
        max_workers = [
            kw.value.value for call in _calls(executor)
            for kw in call.keywords if kw.arg == 'max_workers'
        ]
        self.assertEqual(
            max_workers, [1], 'the core executor must serialize (max_workers=1)')

    def test_handle_offloads_the_core(self):
        tree = _tree()
        handle = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == '_handle'
        )
        text = ast.unparse(handle)
        self.assertIn('run_in_executor', text)
        self.assertIn('_CORE_EXECUTOR', text)
        # The core is passed to the worker, never invoked inline on the loop.
        self.assertIn('app.handle', text)
        self.assertNotIn('app.handle(_to_request', text)


if __name__ == '__main__':
    unittest.main()
