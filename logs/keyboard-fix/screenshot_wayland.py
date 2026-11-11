#!/usr/bin/env python3
"""Temporary GNOME screenshot client; uses the desktop Screenshot D-Bus API."""
import sys
import gi
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
name = 'org.gnome.Screenshot'
result = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
    'org.freedesktop.DBus', 'RequestName', GLib.Variant('(su)', (name, 4)),
    GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
if result != 1:
    raise RuntimeError('A screenshot client is already running; refusing to replace it')
try:
    result = bus.call_sync('org.gnome.Shell.Screenshot', '/org/gnome/Shell/Screenshot',
        'org.gnome.Shell.Screenshot', 'Screenshot', GLib.Variant('(bbs)', (False, False, sys.argv[1])),
        GLib.VariantType.new('(bs)'), Gio.DBusCallFlags.NONE, 15000, None)
    print(result.unpack())
finally:
    bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
        'org.freedesktop.DBus', 'ReleaseName', GLib.Variant('(s)', (name,)),
        None, Gio.DBusCallFlags.NONE, 5000, None)
