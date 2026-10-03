#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Short offline test of a staged or installed RX3 ALSA plugin; never opens USB."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plugin-dir', type=Path, default=Path('/usr/lib/alsa-lib'))
    ap.add_argument('--helper', type=Path, default=Path('/usr/lib/rx3-audio-driver/test_pcm'))
    ap.add_argument('--poll-helper', type=Path, default=Path('/usr/lib/rx3-audio-driver/test_poll'))
    ap.add_argument('--ring-helper', type=Path, default=Path('/usr/lib/rx3-audio-driver/test_ring'))
    ap.add_argument('--drain-helper', type=Path, default=Path('/usr/lib/rx3-audio-driver/test_drain'))
    args = ap.parse_args()
    lib = args.plugin_dir.resolve()/'libasound_module_pcm_rx3.so'
    ctypes.CDLL(str(lib))
    # Exercise the shipped native definition, not a second copy of its
    # adapter chain. Only the transport destination is changed to a file.
    definition = Path(__file__).resolve().with_name('99-rx3.conf')
    if not definition.exists():
        definition = Path('/usr/share/alsa/alsa.conf.d/99-rx3.conf')
    native_definition = definition.read_text()
    assert native_definition.count('native_profile true') == 1
    def native_pcm(output):
        return native_definition.replace('native_profile true', 'native_profile true offline_output "'+str(output)+'"')
    frames = 10003  # wraps the ALSA ring and ends outside a period/USB packet
    source = bytearray()
    for frame in range(frames):
        for channel in range(4):
            sample = ((frame*7919 + channel*104729) & 0xffffff)-0x800000
            source.extend(struct.pack('<i', sample*256))
    wanted = b''.join(source[i+1:i+4] for i in range(0,len(source),4))
    results = []
    ring = subprocess.run([str(args.ring_helper.resolve())], capture_output=True, text=True, timeout=10, check=True)
    results.append({'completion_withheld_test': ring.stdout.strip()})
    with tempfile.TemporaryDirectory(prefix='rx3-package-check-') as folder:
        root = Path(folder)
        ref = root/'reference.bin'
        ref.write_bytes(source)
        native_config = root / 'native-config/rx3-audio-driver'
        native_config.mkdir(parents=True)
        for native, period, fmt in ((n, p, f) for n in (False, True) for p in (64, 128, 256, 512, 1024, 2048) for f in ('s32', 'packed24')):
            tag = ('native-' if native else 'generic-') + f'{period}-{fmt}'
            output, config = root/(tag+'.usb'), root/(tag+'.conf')
            config.write_text('pcm.rx3_test { type rx3 offline_output "'+str(output)+'" }\n')
            env = dict(os.environ, ALSA_PLUGIN_DIR=str(args.plugin_dir.resolve()), ALSA_CONFIG_PATH=str(config), RX3_TEST_PERIOD=str(period))
            if native:
                # ALSA's plug wrapper converts formats locally. With nonexistent
                # server endpoints this exercises the direct driver path only.
                config.write_text(native_pcm(output))
                (native_config/'settings.json').write_text(json.dumps({'active_backend':'wasapi','buffers':{'linux':period,'wasapi':512,'asio':512}}))
                env.update(XDG_CONFIG_HOME=str(native_config.parent), RX3_TEST_PLUG='1', PIPEWIRE_REMOTE='rx3-no-such-server', PULSE_SERVER='unix:/nonexistent-rx3-pulse')

            cmd = [str(args.helper.resolve()), 'rx3_native' if native else 'rx3_test', str(ref)]
            if fmt == 'packed24':
                cmd.append(fmt)
            completed = subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=10)
            if completed.returncode:
                raise RuntimeError(tag + ": " + completed.stderr + completed.stdout)
            data = output.read_bytes()
            if len(data)%16:
                raise AssertionError('Partial frame in output')
            actual = b''.join(data[i:i+3] for i in range(0,len(data),4))
            if actual[:len(wanted)] != wanted or any(actual[len(wanted):]) or any(data[3::4]):
                raise AssertionError(f'Bit or padding mismatch: {fmt}')
            results.append({'path':'direct_alsa' if native else 'generic','format':fmt,'period':period,'buffer':period*(4 if period == 64 else 2),'frames':frames,'different_samples':0,'playback':completed.stdout.strip()})
        # A malformed native preference must be rejected at open, not ignored.
        (native_config/'settings.json').write_text('{"buffers":{"linux":"128"}}')
        output = root/'invalid-native.usb'
        config = root/'invalid-native.conf'
        config.write_text(native_pcm(output))
        env = dict(os.environ, ALSA_PLUGIN_DIR=str(args.plugin_dir.resolve()), ALSA_CONFIG_PATH=str(config), XDG_CONFIG_HOME=str(native_config.parent), RX3_TEST_PLUG='1')
        invalid = subprocess.run([str(args.helper.resolve()), 'rx3_native', str(ref)], env=env, capture_output=True, text=True, timeout=10)
        assert invalid.returncode != 0 and 'Invalid or unreadable RX3 Linux buffer preference' in invalid.stderr
        assert not output.exists()
        results.append({'native_invalid_preference': 'rejected before transport start'})
        config = root/'poll.conf'
        config.write_text('pcm.rx3diag { type rx3 offline_output \"'+str(root/'poll.usb')+'\" }\n')
        env = dict(os.environ, ALSA_PLUGIN_DIR=str(args.plugin_dir.resolve()), ALSA_CONFIG_PATH=str(config))
        poll = subprocess.run([str(args.poll_helper.resolve())], env=env, capture_output=True, text=True, timeout=10, check=True)
        results.append({'readiness_and_error_test': poll.stdout.strip()})
        config = root/'tail.conf'
        config.write_text('pcm.rx3tail { type rx3 offline_output "'+str(root/'tail.usb')+'" }\n')
        env = dict(os.environ, ALSA_PLUGIN_DIR=str(args.plugin_dir.resolve()), ALSA_CONFIG_PATH=str(config))
        tail = subprocess.run([str(args.drain_helper.resolve())], env=env, capture_output=True, text=True, timeout=10, check=True)
        results.append({'nonblocking_drain_test': tail.stdout.strip()})
        # The mmap adapter and format converter must also preserve the tail
        # for nonblocking clients; the generic PCM test alone cannot prove it.
        (native_config/'settings.json').write_text('{"buffers":{"linux":256}}')
        env.update(XDG_CONFIG_HOME=str(native_config.parent), PIPEWIRE_REMOTE='rx3-no-such-server', PULSE_SERVER='unix:/nonexistent-rx3-pulse')
        for fmt in ('s32', 'packed24'):
            config.write_text(native_pcm(root/('native-tail-'+fmt+'.usb')).replace('pcm.rx3_native', 'pcm.rx3tail'))
            env['RX3_TEST_FORMAT'] = fmt
            tail = subprocess.run([str(args.drain_helper.resolve())], env=env, capture_output=True, text=True, timeout=10)
            if tail.returncode:
                raise RuntimeError('native drain '+fmt+': '+tail.stderr+tail.stdout)
            results.append({'native_nonblocking_drain_format':fmt, 'result':tail.stdout.strip()})
    print(json.dumps({'status':'PASS_OFFLINE_PACKAGE','hardware_access':False,'cases':results},indent=2))


if __name__ == '__main__':
    main()
