#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pacman-only dispatcher: enter each active desktop user's own session.

Package hooks run as root, but PipeWire belongs to the logged-in user. Drop
privileges before reading user configuration or contacting that user's socket.
Never launch Wine as root, restart the desktop audio server, or change defaults
for other sound cards. Future sessions are handled by 90-rx3-audio.conf.
"""
import os
from pathlib import Path
import pwd
import subprocess
import sys


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in (['setup'], ['remove']):
        print('Usage (root): rx3-sessions setup|remove', file=sys.stderr)
        return 2
    action = sys.argv[1]
    for runtime in sorted(Path('/run/user').glob('[0-9]*')):
        if not runtime.name.isdecimal():
            continue
        uid = int(runtime.name)
        if uid < 1000 or runtime.is_symlink() or runtime.stat().st_uid != uid:
            continue
        if not (runtime / 'pipewire-0').is_socket():
            continue
        try:
            user = pwd.getpwuid(uid)
        except KeyError:
            continue
        args = ['/usr/bin/runuser', '-u', user.pw_name, '--', '/usr/bin/env', '-i',
                'HOME=' + user.pw_dir, 'USER=' + user.pw_name,
                'LOGNAME=' + user.pw_name, 'PATH=/usr/bin:/bin',
                'XDG_RUNTIME_DIR=' + str(runtime),
                'DBUS_SESSION_BUS_ADDRESS=unix:path=' + str(runtime / 'bus'),
                '/usr/bin/rx3-audio-setup']
        if action == 'setup':
            # Make the newly installed optional ControlPanel user unit visible.
            subprocess.run(args[:-1] + ['/usr/bin/systemctl', '--user', 'daemon-reload'],
                           capture_output=True, timeout=15, check=False)
        if action == 'remove':
            args.append('--remove')
        try:
            p = subprocess.run(args, text=True, capture_output=True, timeout=45)
            print('RX3 / ' + user.pw_name + ': ' + (p.stdout.strip() or p.stderr.strip()))
            if p.returncode:
                print('RX3: active session needs review; other audio outputs were not changed.', file=sys.stderr)
        except subprocess.SubprocessError as exc:
            print('RX3: session setup deferred: ' + str(exc), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
