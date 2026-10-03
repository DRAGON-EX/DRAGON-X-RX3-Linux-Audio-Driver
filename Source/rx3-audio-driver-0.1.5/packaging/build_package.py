#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the native CachyOS/Arch package entirely from local project sources."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile

VERSION = '0.1.5'
NAME = 'rx3-audio-driver'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(__file__).resolve().parent.parent
    dest = root.parent/'dist'
    build = dest/'packagebuild'
    build.mkdir(parents=True,exist_ok=True)
    files = sorted(str(p.relative_to(root)) for p in root.rglob('*')
                   if p.is_file() and '__pycache__' not in p.parts and 'reports' not in p.parts
                   and p.suffix not in ('.o', '.so', '.pyc')
                   and (p.suffix in ('.c', '.h', '.py', '.md', '.png', '.json', '.txt', '.conf', '.rules', '.install', '.desktop', '.service')
                        or p.name in ('Makefile', 'LICENSE')))
    tarpath = build/f'{NAME}-{VERSION}.tar.gz'
    with tarpath.open('wb') as output:
        with gzip.GzipFile(fileobj=output,mode='wb',mtime=0,filename='') as zipped:
            with tarfile.open(fileobj=zipped,mode='w') as tar:
                for file in files:
                    data = (root/file).read_bytes()
                    entry = tarfile.TarInfo(f'{NAME}-{VERSION}/{file}')
                    entry.size = len(data); entry.mtime = 0; entry.mode = 0o644
                    tar.addfile(entry,io.BytesIO(data))
    shutil.copy2(root/'packaging/rx3.install',build/'rx3.install')
    pkgbuild = '''# Local project sources; no network retrieval or dependency installation.
pkgname=rx3-audio-driver
pkgver=0.1.5
pkgrel=2
pkgdesc='Four-channel 24-bit ALSA playback driver for Pioneer XDJ-RX3'
arch=('x86_64')
license=('GPL-3.0-or-later')
depends=('alsa-lib' 'libusb' 'json-c' 'systemd' 'python' 'python-gobject' 'gtk3' 'util-linux')
optdepends=('pipewire: RX3 output for WASAPI and PipeASIO' 'pipewire-pulse: Wine WASAPI/Pulse bridge' 'wireplumber: PipeWire desktop device management')
makedepends=('gcc' 'make' 'pkgconf')
install=rx3.install
options=('!debug' '!lto')
source=("$pkgname-$pkgver.tar.gz" 'rx3.install')
sha256sums=('SOURCE_HASH' 'INSTALL_HASH')

build() {
    cd "$srcdir/$pkgname-$pkgver"
    make CFLAGS="$CFLAGS -std=c11 -Wall -Wextra -Werror -fPIC -DPIC" LDFLAGS="$LDFLAGS"
}

check() {
    cd "$srcdir/$pkgname-$pkgver"
    python3 -m unittest discover -s tests -v
    python3 packaging/package_selftest.py --plugin-dir . --helper ./test_pcm --poll-helper ./test_poll --ring-helper ./test_ring --drain-helper ./test_drain
}

package() {
    cd "$srcdir/$pkgname-$pkgver"
    install -Dm755 libasound_module_pcm_rx3.so "$pkgdir/usr/lib/alsa-lib/libasound_module_pcm_rx3.so"
    install -Dm755 test_pcm "$pkgdir/usr/lib/rx3-audio-driver/test_pcm"
    install -Dm755 test_poll "$pkgdir/usr/lib/rx3-audio-driver/test_poll"
    install -Dm755 test_ring "$pkgdir/usr/lib/rx3-audio-driver/test_ring"
    install -Dm755 test_drain "$pkgdir/usr/lib/rx3-audio-driver/test_drain"
    install -Dm755 packaging/package_selftest.py "$pkgdir/usr/bin/rx3-audio-selftest"
    install -Dm644 packaging/99-rx3.conf "$pkgdir/usr/share/alsa/alsa.conf.d/99-rx3.conf"
    install -d "$pkgdir/etc/alsa/conf.d"
    ln -s /usr/share/alsa/alsa.conf.d/99-rx3.conf "$pkgdir/etc/alsa/conf.d/99-rx3.conf"
    install -Dm644 packaging/70-rx3-audio.rules "$pkgdir/usr/lib/udev/rules.d/70-rx3-audio.rules"
    install -Dm755 packaging/rx3_audio_setup.py "$pkgdir/usr/bin/rx3-audio-setup"
    install -Dm755 packaging/rx3_audio_settings.py "$pkgdir/usr/bin/rx3-audio-settings"
    install -Dm755 packaging/rx3_sessions.py "$pkgdir/usr/lib/rx3-audio-driver/rx3-sessions"
    install -Dm644 packaging/rx3-node.json "$pkgdir/usr/share/rx3-audio-driver/rx3-node.json"
    install -Dm644 packaging/90-rx3-audio.conf "$pkgdir/usr/share/pipewire/pipewire.conf.d/90-rx3-audio.conf"
    install -Dm644 packaging/pioneer-rx3-linux.desktop "$pkgdir/usr/share/applications/pioneer-rx3-linux.desktop"
    install -Dm644 packaging/pioneer-rx3-asio.desktop "$pkgdir/usr/share/applications/pioneer-rx3-asio.desktop"
    install -Dm644 packaging/pioneer-rx3-wasapi.desktop "$pkgdir/usr/share/applications/pioneer-rx3-wasapi.desktop"
    install -Dm644 packaging/rx3-audio-settings.service "$pkgdir/usr/lib/systemd/user/rx3-audio-settings.service"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
'''.replace('SOURCE_HASH',digest(tarpath)).replace('INSTALL_HASH',digest(build/'rx3.install'))
    (build/'PKGBUILD').write_text(pkgbuild)
    (build/'makepkg.conf').write_text('''source /etc/makepkg.conf
# Baseline x86_64, not the build machine's -march=native.
CFLAGS="-march=x86-64 -mtune=generic -O2 -pipe -fstack-protector-strong -fstack-clash-protection -D_FORTIFY_SOURCE=2"
CXXFLAGS="$CFLAGS"
LDFLAGS="-Wl,-z,relro,-z,now"
MAKEFLAGS="-j2"
BUILDENV=(!distcc !color !ccache check !sign)
OPTIONS=(strip docs !libtool !staticlibs !emptydirs !zipman !purge !debug !lto)
PKGEXT='.pkg.tar.zst'
COMPRESSZST=(zstd -c -z -q -T2 -19)
''')
    subprocess.run(['makepkg','--config','./makepkg.conf','--cleanbuild','--force'],cwd=build,check=True)
    package_name = f'{NAME}-{VERSION}-2-x86_64.pkg.tar.zst'
    package = dest/package_name
    shutil.copy2(build/package_name,package)
    installer = '''#!/usr/bin/env bash
set -euo pipefail
package_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
package_file="$package_dir/rx3-audio-driver-0.1.5-2-x86_64.pkg.tar.zst"
if [[ ! -f "$package_file" ]]; then
    printf '%s\\n' 'The installation package is missing from this directory.' >&2
    exit 1
fi
cd -- "$package_dir"
sha256sum --check SHA256SUMS.txt
if (( EUID == 0 )); then
    pacman -U -- "$package_file"
else
    sudo pacman -U -- "$package_file"
fi
'''
    uninstaller = '''#!/usr/bin/env bash
set -euo pipefail
if (( EUID == 0 )); then
    pacman -R -- rx3-audio-driver
else
    sudo pacman -R -- rx3-audio-driver
fi
'''
    (dest/'Install.sh').write_text(installer)
    (dest/'Uninstall.sh').write_text(uninstaller)
    for file in ('Install.sh','Uninstall.sh'):
        (dest/file).chmod(0o755)
    shutil.copy2(root/'README.md',dest/'README.md')
    checksums = f'{digest(package)}  {package.name}\n'
    (dest/'SHA256SUMS.txt').write_text(checksums)
    bundle_files = [package,'Install.sh','Uninstall.sh','README.md','SHA256SUMS.txt']
    with zipfile.ZipFile(dest/f'Pioneer_XDJ_RX3_Linux_Audio_Driver_Core_Setup.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
        for file in bundle_files:
            file = file if isinstance(file,Path) else dest/file
            z.write(file,f'Pioneer_XDJ_RX3_Linux_Audio_Driver_Core_Setup/{file.name}')
    with zipfile.ZipFile(dest/f'Pioneer_XDJ_RX3_Linux_Audio_Driver_Core_Source.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
        for file in ('PKGBUILD','makepkg.conf','rx3.install',tarpath.name):
            z.write(build/file,f'Pioneer_XDJ_RX3_Linux_Audio_Driver_Core_Source/{file}')
    with zipfile.ZipFile(dest/f'Pioneer_XDJ_RX3_Linux_Audio_Driver_GitHub_Source.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
        for file in files:
            z.write(root/file, f'rx3-audio-driver/{file}')
    report = {'package':str(package),'sha256':digest(package),'version':VERSION,
              'arch':'x86_64','system_installation_performed':False,'downloads':False}
    (dest/'build_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
