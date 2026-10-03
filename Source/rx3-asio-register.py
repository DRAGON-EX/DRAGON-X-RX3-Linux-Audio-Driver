#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Register the optional 64-bit RX3 PipeASIO build in one existing system-Wine prefix."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

CLSID = '{2D3CA9E2-1193-4C5D-B5FD-38798F3DC074}'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--prefix', type=Path, required=True)
    args = ap.parse_args()
    if os.geteuid() == 0:
        ap.error('Run as your desktop user, never root')
    prefix = args.prefix.expanduser().resolve()
    if not (prefix / 'system.reg').is_file() or '#arch=win64' not in (prefix / 'system.reg').read_text(errors='replace'):
        ap.error('An existing 64-bit system-Wine prefix is required')
    if any(p.exists() for p in (prefix / 'tracked_files', prefix.parent / 'tracked_files', prefix / 'bottle.yml')):
        ap.error('Proton/Bottles prefixes must use their own runner; this helper supports system Wine only')
    source = Path('/usr/lib/wine/x86_64-windows/pipeasio64.dll')
    native = Path('/usr/lib/wine/x86_64-unix/pipeasio64.so')
    if not source.is_file() or not native.is_file():
        ap.error('Install the RX3 audio driver package first')
    target = prefix / 'drive_c/windows/system32/pipeasio64.dll'
    if not target.parent.is_dir():
        ap.error('Prefix system32 directory is missing')
    # Replace this one driver file atomically; do not follow an existing DLL
    # symlink into another installation. Running hosts keep their current map.
    pending = target.with_suffix('.dll.rx3-tmp')
    shutil.copyfile(source, pending)
    pending.replace(target)
    env = dict(os.environ, WINEPREFIX=str(prefix), WINEDEBUG='-all')
    subprocess.run(['/usr/bin/wine', 'regsvr32', '/s', r'C:\windows\system32\pipeasio64.dll'], env=env, check=True, timeout=60)
    # VirtualDJ validates this path before adding an ASIO driver to its list.
    # A bare DLL filename resolves against its program directory and fails.
    subprocess.run(['/usr/bin/wine', 'reg', 'add', 'HKCR\\CLSID\\' + CLSID + '\\InprocServer32',
                    '/ve', '/t', 'REG_SZ', '/d', r'C:\windows\system32\pipeasio64.dll', '/f', '/reg:64'],
                   env=env, check=True, timeout=30)
    config_root = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    path = config_root / 'pipeasio/config.ini'
    if not path.exists():
        size = 512
        settings = config_root / 'rx3-audio-driver/settings.json'
        if settings.exists():
            value = json.loads(settings.read_text())['buffers']['asio']
            if type(value) is not int or value not in (64, 128, 256, 512, 1024, 2048):
                raise ValueError('Invalid RX3 buffer_size; PipeASIO INI was not written')
            size = value
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x') as out:
            out.write('[pipeasio]\ninputs=0\noutputs=4\nbuffer_size=' + str(size) + '\nfixed_buffer_size=1\nsample_rate=44100\nauto_connect=1\noutput_device=rx3_virtualdj\nfollow_device_clock=0\nrealtime=0\nnode_name=VirtualDJ_ASIO_RX3\n')
        path.chmod(0o600)
    print('Registered RX3 PipeASIO in ' + str(prefix))
    print('Existing PipeASIO preferences were preserved. Reload the ASIO driver in the application; a later application restart reliably loads an upgraded DLL.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('RX3 ASIO: ' + str(exc), file=sys.stderr)
        sys.exit(1)
