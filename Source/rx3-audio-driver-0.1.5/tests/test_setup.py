# SPDX-License-Identifier: GPL-3.0-or-later
"""Lifecycle checks: independent sinks, active playback and failed setup are preserved."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rx3_setup', ROOT / 'packaging/rx3_audio_setup.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
ASSETS = ROOT / 'packaging'

class SetupTest(unittest.TestCase):
    def setUp(self):
        self.graph = []
        self.operations = []
        self.next_id = 20
        self.corrupt = False
        for name, effect in [('snapshot', lambda: copy.deepcopy(self.graph)), ('command', self.command)]:
            p = patch.object(s, name, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def add(self, props, state='suspended'):
        self.next_id += 1
        node = {'id': self.next_id, 'type': 'PipeWire:Interface:Node',
                'info': {'props': props.copy(), 'state': state}}
        self.graph.append(node)
        return node

    def command(self, args):
        self.operations.append(args)
        if args[:2] == ['pw-cli', 'destroy']:
            self.graph = [n for n in self.graph if n['id'] != int(args[2])]
        elif args[:3] == ['pw-cli', 'create-node', 'adapter']:
            node = self.add(json.loads(args[3]))
            if self.corrupt:
                node['info']['props']['api.alsa.period-size'] = 999
        else:
            raise AssertionError(args)
        return ''

    def test_fresh_default_idempotent_remove_preserves_other_sink(self):
        other = self.add({'node.name': 'speakers', 'api.alsa.path': 'hw:0'})
        self.assertTrue(s.configure(ASSETS, 512)['changed'])
        self.assertFalse(s.configure(ASSETS, 512)['changed'])
        self.assertEqual(len(self.operations), 1)
        s.configure(ASSETS, 512, remove=True)
        self.assertEqual(self.graph, [other])

    def test_all_presets_keep_usb_startup_capacity(self):
        for size in s.SIZES:
            with self.subTest(size=size):
                result = s.configure(ASSETS, size)
                self.assertGreaterEqual(size * result['periods'], 256)
                self.assertEqual(s.props(self.graph[0])['node.force-quantum'], size)
                self.assertTrue(s.props(self.graph[0])['api.alsa.disable-tsched'])

    def test_active_output_cannot_be_replaced_but_same_preset_is_safe(self):
        self.add(s.preset(ASSETS, 512), 'running')
        self.assertFalse(s.configure(ASSETS, 512)['changed'])
        with self.assertRaisesRegex(RuntimeError, 'Stop audio'):
            s.configure(ASSETS, 256)
        self.assertEqual(self.operations, [])

    def test_unmanaged_output_not_adopted_or_removed(self):
        props = s.preset(ASSETS, 512)
        props.pop('rx3.driver.managed')
        self.add(props)
        with self.assertRaisesRegex(RuntimeError, 'manually configured'):
            s.configure(ASSETS, 512)
        s.configure(ASSETS, 512, remove=True)
        self.assertEqual(self.operations, [])

    def test_custom_rx3_name_and_duplicate_guard(self):
        self.add(dict(s.preset(ASSETS, 512), **{'node.name': 'my-rx3'}))
        with self.assertRaisesRegex(RuntimeError, 'manually configured'):
            s.configure(ASSETS, 512)
        self.graph = []
        self.add(s.preset(ASSETS, 512))
        self.add(s.preset(ASSETS, 512))
        with self.assertRaisesRegex(RuntimeError, 'Multiple RX3'):
            s.configure(ASSETS, 512)
        self.assertEqual(self.operations, [])

    def test_failed_verification_does_not_save_selection(self):
        self.corrupt = True
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'rx3-audio-driver'
            config.mkdir()
            s.save_settings(config, 512)
            with patch.dict(s.os.environ, {'XDG_CONFIG_HOME': folder}), patch.object(s.os, 'geteuid', return_value=1000), patch('sys.argv', ['rx3-audio-setup', '--data-dir', str(ASSETS), '--set-buffer', '256']):
                self.assertEqual(s.main(), 1)
            self.assertEqual(s.read_settings(config), 512)

    def test_saved_settings_validate_type_and_range(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)
            self.assertEqual(s.read_settings(config), 512)
            for size in s.SIZES:
                s.save_settings(config, size)
                self.assertEqual(s.read_settings(config), size)
            for invalid in (True, '256', 96, -1):
                (config / 'settings.json').write_text(json.dumps({'active_backend': 'wasapi', 'buffers': {'linux': invalid}}))
                with self.assertRaises(ValueError):
                    s.read_settings(config)

    def test_native_profile_works_without_any_pipewire_access(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'rx3-audio-driver'
            config.mkdir()
            s.save_settings(config, 1024, 'wasapi')
            with patch.dict(s.os.environ, {'XDG_CONFIG_HOME': folder}), patch.object(s.os, 'geteuid', return_value=1000), patch.object(s, 'snapshot', side_effect=AssertionError('Native must not contact PipeWire')), patch('sys.argv', ['rx3-audio-setup', '--backend', 'linux', '--set-buffer', '128']):
                self.assertEqual(s.main(), 0)
                result = s.status(config, 'linux')
                self.assertFalse(result['pipewire_used'])
            self.assertEqual(s.read_settings(config, 'linux'), 128)
            self.assertEqual(s.read_document(config)['active_backend'], 'wasapi')
            self.assertEqual(s.read_settings(config, 'wasapi'), 1024)
            self.assertEqual(self.operations, [])

    def test_helper_rejects_root(self):
        with patch.object(s.os, 'geteuid', return_value=0), patch('sys.argv', ['rx3-audio-setup']):
            with self.assertRaises(SystemExit) as result:
                s.main()
            self.assertEqual(result.exception.code, 2)

    def test_asio_only_targets_rx3_and_preserves_other_device(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'rx3-audio-driver'
            config.mkdir()
            s.save_asio_buffer(s.prepare_asio_buffer(config, 128))
            asio = Path(folder) / 'pipeasio/config.ini'
            self.assertEqual(s.rx3_asio_config(config)[1].getint('pipeasio', 'buffer_size'), 128)
            other = '[pipeasio]\noutput_device=other-speakers\nbuffer_size=1024\n'
            asio.write_text(other)
            with self.assertRaisesRegex(ValueError, 'another device'):
                s.prepare_asio_buffer(config, 256)
            self.assertEqual(asio.read_text(), other)

    def test_linux_asio_and_wasapi_choices_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'rx3-audio-driver'
            config.mkdir()
            for backend, size in [('linux', 64), ('asio', 128), ('wasapi', 1024)]:
                with patch.dict(s.os.environ, {'XDG_CONFIG_HOME': folder}), patch.object(s.os, 'geteuid', return_value=1000), patch('sys.argv', ['rx3-audio-setup', '--data-dir', str(ASSETS), '--backend', backend, '--set-buffer', str(size)]):
                    self.assertEqual(s.main(), 0)
            self.assertEqual(s.read_settings(config, 'linux'), 64)
            self.assertEqual(s.read_settings(config, 'asio'), 128)
            self.assertEqual(s.read_settings(config, 'wasapi'), 1024)
            self.assertEqual(s.read_document(config)['active_backend'], 'wasapi')
            # Applying WASAPI must not rewrite the ASIO INI.
            self.assertEqual(s.rx3_asio_config(config)[1].getint('pipeasio', 'buffer_size'), 128)
            self.assertEqual(s.props(self.graph[0])['api.alsa.period-size'], 1024)

if __name__ == '__main__':
    unittest.main()
