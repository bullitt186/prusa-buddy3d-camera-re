"""WP-R2 (AC-23/AC-24/AC-27): static checks for main.py's MQTT wiring.

``main.py`` imports aiohttp/socketio/gi, so it is parsed as source with
:mod:`ast` rather than imported. These checks pin the lifecycle contract:
durable-document config, the service's injected providers/callables, an
off-event-loop start/stop, and one shared timelapse-build function.
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PI_DIR = REPO / 'pi-impersonator'
MAIN_PY = PI_DIR / 'main.py'
sys.path.insert(0, str(PI_DIR))


def _tree():
    return ast.parse(MAIN_PY.read_text(encoding='utf-8'))


def _functions(tree, name):
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _attr_chain(node):
    """Return the dotted attribute chain ending at ``node`` as a list."""
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


def _call_names(tree):
    return {_attr_chain(call.func)[-1] for call in _calls(tree) if _attr_chain(call.func)}


class MainMqttSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding='utf-8')
        cls.tree = _tree()

    def test_imports_the_mqtt_modules(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for name in ('mqtt_service', 'config_schema', 'app_metrics', 'app_version',
                     'privileged', 'updater_install'):
            self.assertIn(name, imported)

    def test_build_timelapse_video_is_defined_and_returns_bool(self):
        functions = _functions(self.tree, 'build_timelapse_video')
        self.assertEqual(len(functions), 1)
        # It is the only place that builds the .avi.
        avi_calls = [
            call for call in _calls(self.tree)
            if _attr_chain(call.func)[-1] == 'build_avi'
        ]
        self.assertEqual(len(avi_calls), 1)
        self.assertTrue(
            any(
                isinstance(node, ast.Return)
                for node in ast.walk(functions[0])
            )
        )

    def test_trigger_handler_reuses_the_shared_build(self):
        handlers = _functions(self.tree, 'dispatch_trigger_action')
        self.assertEqual(len(handlers), 1)
        names = [
            _attr_chain(call.func)[-1] for call in _calls(handlers[0])
        ]
        self.assertIn('build_timelapse_video', names)

    def test_main_builds_config_from_durable_documents(self):
        main = _functions(self.tree, 'main')
        self.assertEqual(len(main), 1)
        chains = {tuple(_attr_chain(call.func)) for call in _calls(main[0])}
        self.assertIn(('config_schema', 'load_device'), chains)
        self.assertIn(('config_schema', 'load_secrets'), chains)
        self.assertIn(('mqtt_service', 'MqttConfig', 'from_documents'), chains)

    def test_main_constructs_the_service_with_injected_providers(self):
        main = _functions(self.tree, 'main')
        service_calls = [
            call for call in _calls(main[0])
            if _attr_chain(call.func) == ['mqtt_service', 'MqttService']
        ]
        self.assertEqual(len(service_calls), 1)
        keywords = {kw.arg: kw.value for kw in service_calls[0].keywords}
        for name in (
            'application_version', 'serial', 'mac', 'build_timelapse',
            'restart', 'metrics_provider', 'secrets',
            'update_state_provider', 'update_install',
        ):
            self.assertIn(name, keywords, f'MqttService missing {name}')
        # The callables are the real module-level providers.
        self.assertEqual(_attr_chain(keywords['build_timelapse']), ['build_timelapse_video'])
        self.assertEqual(_attr_chain(keywords['restart']), ['reboot_device'])
        self.assertEqual(
            _attr_chain(keywords['metrics_provider']), ['app_metrics', 'metrics_provider'])
        # AC-31: the update state comes from the root-written state file and the
        # install trigger is the fixed-verb privileged helper, never in-process.
        self.assertEqual(
            _attr_chain(keywords['update_install']),
            ['privileged', 'install_update'])
        provider = keywords['update_state_provider']
        self.assertIsInstance(provider, ast.Lambda)
        self.assertEqual(
            _attr_chain(getattr(provider.body, 'func', None)),
            ['updater_install', 'read_update_state'])
        version_node = keywords['application_version']
        if isinstance(version_node, ast.Call):
            version_node = version_node.func
        self.assertEqual(
            _attr_chain(version_node),
            ['app_version', 'application_version'])

    def test_service_start_is_scheduled_not_awaited(self):
        # AC-27: a slow/unreachable broker must not delay signaling, snapshots,
        # RTSP, ONVIF, or WebRTC, so main() schedules the start and never awaits
        # it. The blocking connect runs off the loop in the helper.
        helpers = _functions(self.tree, '_start_mqtt_service')
        self.assertEqual(len(helpers), 1, 'missing _start_mqtt_service helper')
        to_thread_calls = [
            call for call in _calls(helpers[0])
            if _attr_chain(call.func) == ['asyncio', 'to_thread']
        ]
        self.assertTrue(
            any(
                call.args and _attr_chain(call.args[0])[-1] == 'start'
                for call in to_thread_calls
            ),
            'the MQTT start must run via asyncio.to_thread',
        )

        main = _functions(self.tree, 'main')[0]
        scheduled = [
            call for call in _calls(main)
            if _attr_chain(call.func)[-1:] == ['create_task']
            and call.args
            and _attr_chain(getattr(call.args[0], 'func', None))
            == ['_start_mqtt_service']
        ]
        self.assertTrue(
            scheduled, 'main must schedule _start_mqtt_service with create_task')
        direct_start = [
            node for node in ast.walk(main)
            if isinstance(node, ast.Await)
            and _attr_chain(getattr(node.value, 'func', None)) == ['asyncio', 'to_thread']
            and node.value.args
            and _attr_chain(node.value.args[0])[-1:] == ['start']
        ]
        self.assertEqual(
            direct_start, [],
            'main must not await the MQTT start (startup must not block)',
        )

    def test_service_is_stopped_off_the_event_loop(self):
        main = _functions(self.tree, 'main')[0]
        stopped = any(
            isinstance(node, ast.Await)
            and _attr_chain(getattr(node.value, 'func', None)) == ['asyncio', 'to_thread']
            and node.value.args
            and _attr_chain(node.value.args[0])[-1] == 'stop'
            for node in ast.walk(main)
        )
        self.assertTrue(stopped, 'MqttService.stop must run via asyncio.to_thread')

    def test_service_construction_is_isolated_in_a_try(self):
        main = _functions(self.tree, 'main')
        construction = [
            call for call in _calls(main[0])
            if _attr_chain(call.func) == ['mqtt_service', 'MqttService']
        ]
        self.assertTrue(construction)
        inside_try = any(
            isinstance(node, ast.Try)
            and any(
                child is construction[0]
                for child in ast.walk(node)
            )
            for node in ast.walk(main[0])
        )
        self.assertTrue(
            inside_try,
            'MqttService construction must be inside try/except (AC-27 isolation)',
        )

    def test_reboot_device_is_never_called_at_import(self):
        calls = [
            call for call in _calls(self.tree)
            if _attr_chain(call.func) == ['reboot_device']
        ]
        self.assertEqual(calls, [], 'reboot_device must be passed, never called directly')


if __name__ == '__main__':
    unittest.main()
