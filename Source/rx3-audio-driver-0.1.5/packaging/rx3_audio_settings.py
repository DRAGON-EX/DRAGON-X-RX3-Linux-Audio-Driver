#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native RX3 control panel. The helper owns validation and graph changes.

GTK stays on its main thread. PipeWire inspection and configuration run in a
worker, so an unavailable audio service cannot freeze the window. A slider
movement is only a preview; Apply is the explicit audio reconfiguration step.
"""
import argparse
import json
from pathlib import Path
import subprocess
import threading
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

SIZES = (2048, 1024, 512, 256, 128, 64)
# Keep the full public warning here so the installed package needs only one
# user document (README.md). Display it at every buffer size, not just <512.
WARNING = '''⚠ Warning: Buffer sizes below 512 samples are not recommended

This is a reverse-engineered Linux audio driver. Significant adjustments have been made to compensate for the device’s timing requirements and its interaction with the Linux audio stack. This includes a new timing-control algorithm designed to prevent USB buffer underruns.

A USB buffer underrun occurs when the device needs the next portion of audio data before that data is available. The resulting interruption can produce audible clicks, crackling, or dropouts. The timing changes are intended to keep audio delivery consistent and prevent the device from running out of queued audio data.

These improvements do not guarantee stable operation at every buffer size. Smaller buffers leave less time for the system to prepare and deliver audio. Linux audio configurations vary, and the audio server, application processing, CPU load, and system scheduling can all affect the available timing margin. Running an application through Wine can introduce additional timing constraints.

The default buffer size is 512 samples. You can select 256 samples or lower to reduce latency, but these values are not recommended.

Please do not open an issue for crackling, dropouts, or similar audio problems while using a buffer size below 512 samples. Restore 512 samples first and reproduce the problem at that setting. Only open an issue if the problem persists at 512 samples, and include your audio configuration, affected application, and steps to reproduce it.'''
CSS = b'''
window { background: #171b23; color: #eef1f7; }
label { color: #eef1f7; }
.title { font-size: 26px; font-weight: bold; }
.subtitle { color: #aab5c8; font-size: 13px; }
.value { font-size: 32px; font-weight: bold; }
.warning { color: #ff817f; font-weight: bold; }
.warning-box { background: #2d2026; border: 1px solid #69363c; border-radius: 8px; padding: 16px; }
.warning-body { color: #e0c7ce; font-size: 12px; }
.card { background: #222937; border-radius: 8px; padding: 18px; }
button { color: #eef1f7; background: #303b4d; border: 1px solid #46536b; border-radius: 6px; padding: 7px 14px; }
button.suggested-action { background: #426bd1; border-color: #6288e7; }
button:disabled { color: #808a9b; background: #282d36; }
scale trough { background: #485164; min-height: 6px; }
scale highlight { background: #638af0; }
scale slider { background: #eef3ff; border-radius: 50%; min-width: 18px; min-height: 18px; }
scale mark label { color: #bdc7db; font-size: 12px; }
'''


def label(text='', css=None):
    item = Gtk.Label(label=text, xalign=0)
    item.set_line_wrap(True)
    item.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        item.get_style_context().add_class(css)
    return item


class Panel(Gtk.Window):
    def __init__(self, helper, assets, backend):
        self.backend = backend
        title = 'Pioneer RX3 (ALSA)' if backend == 'linux' else 'Pioneer RX3 Audio Setup (' + backend.upper() + ')'
        super().__init__(title=title)
        self.helper, self.assets = helper, assets
        self.set_default_size(790, 750)
        self.set_border_width(20)
        self.connect('destroy', Gtk.main_quit)
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(container)
        container.pack_start(label(title, 'title'), False, False, 0)
        container.pack_start(label({'asio': 'PipeASIO · Wine → PipeWire → RX3', 'wasapi': 'WASAPI · Wine/Pulse → PipeWire → RX3', 'linux': 'Native Linux app → ALSA driver → USB → RX3'}[backend], 'subtitle'), False, False, 0)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        card.get_style_context().add_class('card')
        container.pack_start(card, False, False, 0)
        top = Gtk.Box(spacing=15)
        top.pack_start(label('BUFFER SIZE', 'subtitle'), True, True, 0)
        top.pack_end(label('44.1 kHz  ·  4 output channels', 'subtitle'), False, False, 0)
        card.pack_start(top, False, False, 0)
        self.value = label('', 'value')
        card.pack_start(self.value, False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, len(SIZES)-1, 1)
        self.scale.set_draw_value(False)
        self.scale.set_increments(1, 1)
        self.scale.set_digits(0)
        self.scale.set_round_digits(0)
        for i, size in enumerate(SIZES):
            self.scale.add_mark(i, Gtk.PositionType.BOTTOM, str(size))
        self.scale.set_value(2)
        self.scale.connect('value-changed', self.preview)
        card.pack_start(self.scale, False, False, 0)
        self.details = label('', 'subtitle')
        card.pack_start(self.details, False, False, 0)
        self.recommendation = label('')
        card.pack_start(self.recommendation, False, False, 0)

        warning = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        warning.get_style_context().add_class('warning-box')
        title, body = WARNING.split('\n\n', 1)
        warning.pack_start(label(title, 'warning'), False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(140)
        scroll.set_vexpand(True)
        warning_text = label(body.strip(), 'warning-body')
        warning_text.set_selectable(True)
        warning_text.set_max_width_chars(94)
        scroll.add(warning_text)
        warning.pack_start(scroll, True, True, 0)
        container.pack_start(warning, True, True, 0)
        container.pack_start(label('Apply saves the direct ALSA buffer for the next device open. Reopen the audio device in your application to use the new size; the application can stay open.' if backend == 'linux' else 'Each window saves its own buffer size. Apply activates this PipeWire profile. Release the audio device before Apply; your app can stay open. Apply the other profile before switching ASIO/WASAPI.', 'subtitle'), False, False, 0)
        if backend == 'linux':
            container.pack_start(label('Select “Pioneer RX3 (ALSA)” / rx3_native in your app. This path uses no PipeWire. The RX3 USB audio device must be free.', 'subtitle'), False, False, 0)
        self.status = label('Reading current settings…', 'subtitle')
        container.pack_start(self.status, False, False, 0)
        buttons = Gtk.Box(spacing=10)
        default = Gtk.Button(label='Use default · 512')
        self.default = default
        default.connect('clicked', lambda _: self.scale.set_value(2))
        buttons.pack_start(default, False, False, 0)
        refresh = Gtk.Button(label='Refresh')
        self.refresh = refresh
        refresh.connect('clicked', lambda _: self.run_helper(['--status'], self.read_status))
        buttons.pack_start(refresh, False, False, 0)
        self.apply = Gtk.Button(label='Apply')
        self.apply.get_style_context().add_class('suggested-action')
        self.apply.connect('clicked', self.on_apply)
        buttons.pack_end(self.apply, False, False, 0)
        close = Gtk.Button(label='Close')
        close.connect('clicked', lambda _: self.destroy())
        buttons.pack_end(close, False, False, 0)
        container.pack_end(buttons, False, False, 0)
        self.preview()
        self.run_helper(['--status'], self.read_status)

    def selected(self):
        return SIZES[round(self.scale.get_value())]

    def preview(self, *_):
        size = self.selected()
        periods = 4 if size == 64 else 2
        self.value.set_text(f'{size} samples')
        self.details.set_text(f'{size / 44.1:.2f} ms per period  ·  {periods} periods  ·  {size * periods} samples ring capacity\nPeriod time is not total output latency.')
        self.recommendation.set_text('⚠ Below 512: experimental, not recommended' if size < 512 else '512 samples is the recommended default.')
        context = self.recommendation.get_style_context()
        context.add_class('warning') if size < 512 else context.remove_class('warning')

    def run_helper(self, args, callback):
        self.apply.set_sensitive(False)
        self.refresh.set_sensitive(False)
        self.scale.set_sensitive(False)
        self.default.set_sensitive(False)
        def worker():
            try:
                p = subprocess.run(self.helper + ['--data-dir', str(self.assets), '--backend', self.backend] + args,
                                   text=True, capture_output=True, timeout=40)
                if p.returncode:
                    raise RuntimeError(p.stderr.strip() or p.stdout.strip() or 'Audio setup failed')
                result = json.loads(p.stdout)
                GLib.idle_add(self.finish, callback, result, None)
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
                GLib.idle_add(self.finish, callback, None, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def finish(self, callback, result, error):
        self.apply.set_sensitive(True)
        self.refresh.set_sensitive(True)
        self.scale.set_sensitive(True)
        self.default.set_sensitive(True)
        if error:
            self.status.set_text(error)
        else:
            callback(result)
        return False

    def read_status(self, result):
        size = result['saved_buffer_size']
        self.scale.set_value(SIZES.index(size))
        if self.backend == 'linux':
            self.status.set_text(f'Saved: {size} samples, {result["periods"]} periods. ALSA device: rx3_native. Used on next open.')
            return
        outputs = result.get('outputs', [])
        if len(outputs) == 1:
            output = outputs[0]
            self.status.set_text(f"Saved: {size} samples. Live output: {output['buffer_size']} samples, {output['periods']} periods ({output['state']})." + (' Manual configuration.' if not output['managed'] else ''))
        else:
            self.status.set_text(result.get('connection_error', f'Saved: {size} samples. RX3 output is not configured yet.'))
        self.status.set_text(self.status.get_text() + ' Active profile: ' + result['active_backend'].upper() + '.')
        if self.backend == 'asio' and 'pipeasio_buffer_size' in result:
            self.status.set_text(self.status.get_text() + f" PipeASIO: {result['pipeasio_buffer_size']} samples.")

    def on_apply(self, _):
        size = self.selected()
        self.status.set_text(f'Applying {size} samples…')
        self.run_helper(['--set-buffer', str(size)],
                        lambda r: self.status.set_text(f"Saved: {r['buffer_size']} samples, {r['periods']} periods. Reopen rx3_native to use this size." if r['backend'] == 'linux' else f"Applied and saved: {r['buffer_size']} samples, {r['periods']} periods, 44.1 kHz." + (' PipeASIO configured.' if r['backend'] == 'asio' else '')))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--backend', choices=('linux', 'wasapi', 'asio'), default='linux')
    ap.add_argument('--data-dir', type=Path, default=Path('/usr/share/rx3-audio-driver'))
    ap.add_argument('--helper', nargs='+', default=['/usr/bin/rx3-audio-setup'])
    args = ap.parse_args()
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    Panel(args.helper, args.data_dir, args.backend).show_all()
    Gtk.main()


if __name__ == '__main__':
    main()
