#!/usr/bin/env python3
import json, os
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib
window = Gtk.Window(title='System Keyboard Probe')
window.set_default_size(1280, 800)
window.fullscreen()
entry = Gtk.Entry()
entry.set_placeholder_text('System keyboard: touch here')
entry.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
box.set_border_width(20)
box.add(Gtk.Label(label='GNOME system keyboard + IBus test'))
box.add(entry)
window.add(box)
window.connect('destroy', Gtk.main_quit)
entry.connect('changed', lambda w: print(json.dumps({'text': w.get_text()}, ensure_ascii=False), flush=True))
window.show_all()
print('display=' + Gdk.Display.get_default().__gtype__.name, flush=True)
print('GTK_IM_MODULE=' + os.environ.get('GTK_IM_MODULE', ''), flush=True)
GLib.timeout_add(2000, lambda: (entry.grab_focus(), False)[1])
GLib.timeout_add_seconds(100, lambda: (window.destroy(), False)[1])
Gtk.main()
