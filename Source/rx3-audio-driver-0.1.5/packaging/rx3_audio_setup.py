#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Configure independent direct ALSA, WASAPI and ASIO buffer preferences.

Runs as the desktop user. The public package has no migration machinery and
never imports old local test configurations. Configuration is one small JSON
file; PipeWire owns the live graph. Timing and ownership invariants are
documented beside their implementations in this source and pcm_rx3.c.
"""
import argparse
import configparser
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

NAME = 'rx3_virtualdj'
SIZES = (2048, 1024, 512, 256, 128, 64)
DEFAULT_DATA = Path('/usr/share/rx3-audio-driver')
MATCH_KEYS = ('api.alsa.path', 'audio.format', 'audio.rate', 'audio.channels',
              'audio.position', 'node.driver', 'api.alsa.period-size',
              'api.alsa.period-num', 'api.alsa.disable-mmap',
              'api.alsa.disable-tsched', 'node.force-quantum', 'node.force-rate')


def period_count(size):
    # Four pending USB packets need about 176 frames at startup. A 64 x 2
    # ring cannot supply them; four 64-frame periods provide 256 frames.
    return 4 if size == 64 else 2


def read_document(config):
    try:
        value = json.loads((config / 'settings.json').read_text())
    except FileNotFoundError:
        return {'active_backend': 'wasapi', 'buffers': {'linux': 512, 'wasapi': 512, 'asio': 512}}
    if not isinstance(value, dict):
        raise ValueError('Invalid settings.json object')
    if value.get('active_backend') not in ('wasapi', 'asio'):
        raise ValueError('Invalid active_backend in settings.json')
    buffers = value.get('buffers')
    if not isinstance(buffers, dict):
        raise ValueError('Invalid buffers in settings.json')
    # Missing optional profiles use the same package default, without importing
    # or rewriting any legacy audio definitions.
    for backend in ('linux', 'wasapi', 'asio'):
        size = buffers.setdefault(backend, 512)
        if type(size) is not int or size not in SIZES:
            raise ValueError('Invalid ' + backend + ' buffer size in settings.json')
    return value


def read_settings(config, backend='linux'):
    return read_document(config)['buffers'][backend]


def save_settings(config, size, backend='linux'):
    value = read_document(config)
    # The physical output is shared, but each user-facing path keeps its own
    # preferred size. Apply activates only the selected profile; it never
    # changes the other profile's saved buffer.
    value['buffers'][backend] = size
    if backend != 'linux':
        value['active_backend'] = backend
    pending = config / 'settings.json.tmp'
    pending.write_text(json.dumps(value, indent=2) + '\n')
    pending.chmod(0o600)
    pending.replace(config / 'settings.json')


def rx3_asio_config(config):
    """Only synchronize an optional PipeASIO profile explicitly targeting RX3.

    Other ASIO hardware profiles are outside this helper's scope. Wine reads
    this native INI too, so no Windows registry buffer setting is necessary.
    """
    path = config.parent / 'pipeasio/config.ini'
    parser = configparser.ConfigParser(interpolation=None)
    if path.exists():
        parser.read_string(path.read_text())
        if parser.get('pipeasio', 'output_device', fallback='') == NAME:
            return path, parser
    return None


def prepare_asio_buffer(config, size):
    path = config.parent / 'pipeasio/config.ini'
    profile = rx3_asio_config(config)
    if path.exists() and profile is None:
        raise ValueError('PipeASIO currently targets another device. Select rx3_virtualdj in its configuration before applying the RX3 ASIO profile.')
    if profile is None:
        parser = configparser.ConfigParser(interpolation=None)
        parser['pipeasio'] = {'inputs': '0', 'outputs': '4', 'sample_rate': '44100',
                             'auto_connect': '1', 'output_device': NAME,
                             'follow_device_clock': '0', 'realtime': '0',
                             'node_name': 'VirtualDJ_ASIO_RX3'}
    else:
        parser = profile[1]
    parser.set('pipeasio', 'buffer_size', str(size))
    parser.set('pipeasio', 'fixed_buffer_size', '1')
    for key, value in {'sample_rate': '44100', 'inputs': '0', 'outputs': '4', 'auto_connect': '1'}.items():
        parser.set('pipeasio', key, value)
    return path, parser


def save_asio_buffer(profile):
    path, parser = profile
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix('.ini.tmp')
    with pending.open('w') as stream:
        parser.write(stream)
    pending.chmod(0o600)
    pending.replace(path)


def command(args):
    return subprocess.run(args, text=True, capture_output=True,
                          timeout=8, check=True).stdout


def snapshot():
    return json.loads(command(['pw-dump', '-N']))


def props(node):
    return node.get('info', {}).get('props', {})


def nodes(items):
    return [x for x in items if x.get('type') == 'PipeWire:Interface:Node'
            and props(x).get('node.name') == NAME]


def owned(node):
    return props(node).get('api.alsa.path') == 'rx3' and props(node).get('rx3.driver.managed') is True


def preset(assets, size):
    value = json.loads((assets / 'rx3-node.json').read_text())
    value.update({'api.alsa.period-size': size, 'api.alsa.period-num': period_count(size),
                  'node.force-quantum': size, 'node.max-latency': f'{size}/44100'})
    return value


def matching(actual, wanted):
    return all(str(actual.get(k)).lower() == str(wanted.get(k)).lower() for k in MATCH_KEYS)


def configure(assets, size, remove=False):
    for attempt in range(8):
        try:
            items = snapshot()
            break
        except (subprocess.SubprocessError, OSError):
            if attempt == 7:
                raise RuntimeError('PipeWire is not reachable. Start the desktop audio service and try again.')
            time.sleep(0.25)
    existing = nodes(items)
    if remove:
        for node in existing:
            if owned(node):
                command(['pw-cli', 'destroy', str(node['id'])])
        return {'status': 'removed'}
    if len(existing) > 1:
        raise RuntimeError('Multiple RX3 outputs found. Remove duplicate manual definitions first.')
    foreign = [x for x in items if x.get('type') == 'PipeWire:Interface:Node'
               and props(x).get('api.alsa.path') == 'rx3'
               and props(x).get('media.class') == 'Audio/Sink'
               and props(x).get('node.name') != NAME]
    if foreign or (existing and not owned(existing[0])):
        raise RuntimeError('A manually configured RX3 output exists. It was left unchanged. Remove its manual definition before using automatic setup.')
    wanted = preset(assets, size)
    if existing and matching(props(existing[0]), wanted):
        return {'status': 'configured', 'buffer_size': size, 'periods': period_count(size), 'changed': False}
    if existing:
        # Replacing a running node would drop audio and its live connections.
        # A buffer change is an explicit settings action, after playback stops.
        if existing[0]['info'].get('state') == 'running':
            raise RuntimeError('Stop audio playback and close the application audio device before applying a new buffer size.')
        command(['pw-cli', 'destroy', str(existing[0]['id'])])
    # One argument, no shell: node properties never become executable text.
    command(['pw-cli', 'create-node', 'adapter', json.dumps(wanted)])
    fresh = nodes(snapshot())
    if len(fresh) != 1 or not owned(fresh[0]) or not matching(props(fresh[0]), wanted):
        raise RuntimeError('RX3 output verification failed. Settings were not saved; check PipeWire and try Apply again.')
    return {'status': 'configured', 'buffer_size': size, 'periods': period_count(size), 'changed': True}


def status(config, backend):
    document = read_document(config)
    value = {'saved_buffer_size': read_settings(config, backend), 'sample_rate': 44100,
             'backend': backend, 'active_backend': document['active_backend']}
    if backend == 'linux':
        value.update({'native_pcm': 'rx3_native', 'periods': period_count(value['saved_buffer_size']),
                      'applies_on': 'next ALSA open', 'pipewire_used': False})
        return value
    profile = rx3_asio_config(config) if backend == 'asio' else None
    if profile is not None:
        value['pipeasio_buffer_size'] = profile[1].getint('pipeasio', 'buffer_size', fallback=1024)
    try:
        found = nodes(snapshot())
        value['outputs'] = [{'state': x['info'].get('state'), 'managed': owned(x),
                             'buffer_size': props(x).get('api.alsa.period-size'),
                             'periods': props(x).get('api.alsa.period-num'),
                             'poll_scheduling': props(x).get('api.alsa.disable-tsched')} for x in found]
    except (subprocess.SubprocessError, OSError):
        value['outputs'] = []
        value['connection_error'] = 'PipeWire is not reachable'
    return value


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    action = ap.add_mutually_exclusive_group()
    action.add_argument('--startup', action='store_true')
    action.add_argument('--remove', action='store_true')
    action.add_argument('--status', action='store_true')
    action.add_argument('--set-buffer', type=int, choices=SIZES)
    ap.add_argument('--backend', choices=('linux', 'wasapi', 'asio'), help='Independent buffer profile; defaults to the last applied profile')
    ap.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    args = ap.parse_args()
    if os.geteuid() == 0:
        ap.error('Run as the desktop user, not root')
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'rx3-audio-driver'
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Startup and an Apply click can overlap. Serialize check/create/save so
    # they cannot both see an empty graph and create a duplicate output.
    with (config / 'setup.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            backend = args.backend or read_document(config)['active_backend']
            if args.status:
                result = status(config, backend)
            elif backend == 'linux' and not args.remove:
                # Direct ALSA needs no pw-cli, daemon, socket, or live graph.
                # Applications see the saved constraint at their next PCM open.
                size = args.set_buffer if args.set_buffer is not None else read_settings(config, 'linux')
                if args.set_buffer is not None:
                    save_settings(config, size, 'linux')
                result = {'status': 'saved', 'backend': 'linux', 'buffer_size': size,
                          'periods': period_count(size), 'native_pcm': 'rx3_native',
                          'applies_on': 'next ALSA open', 'pipewire_used': False}
            else:
                size = args.set_buffer if args.set_buffer is not None else read_settings(config, backend)
                # Validate the selected ASIO profile before changing audio.
                asio_profile = prepare_asio_buffer(config, size) if args.set_buffer is not None and backend == 'asio' else None
                result = configure(args.data_dir, size, args.remove)
                if args.set_buffer is not None:
                    if asio_profile is not None:
                        save_asio_buffer(asio_profile)
                    save_settings(config, size, backend)
                result['backend'] = backend
        except (RuntimeError, ValueError, KeyError, OSError, configparser.Error, subprocess.SubprocessError) as exc:
            print('RX3: ' + str(exc), file=sys.stderr)
            return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
