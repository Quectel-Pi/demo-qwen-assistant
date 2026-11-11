#!/usr/bin/env python3
"""Device test: actual GtkEntry events via kernel input, no callback shortcuts."""
import json, os, sys
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk
sys.path.insert(0, '/data/rkllm-chat/ui/rkchat')
import rkchat
# Do not load a second set of speech models for a keyboard-only test.
class QuietVoice:
    class VoiceIO:
        def __init__(self): pass
        def stop_listening(self): pass
    @staticmethod
    def ensure_ready(): return False, 'Keyboard test: speech preload skipped'
rkchat.voice = QuietVoice
app = rkchat.ChatApp(fullscreen=True, keyboard='system')
app.cli.connect()
print(json.dumps({'uid': os.getuid(), 'backend': Gdk.Display.get_default().__gtype__.name,
                  'builtin_keyboard': app.vkb is not None}), flush=True)

def event_log(name):
    def callback(widget, event):
        print(json.dumps({'event': name, 'coords': event.get_coords()}), flush=True)
        return False
    return callback
app.win.connect('touch-event', event_log('window-touch'))
app.entry.connect('touch-event', event_log('entry-touch'))
app.entry.connect('button-press-event', event_log('entry-button'))
app.entry.connect('focus-in-event', lambda *a: (print('entry-focused', flush=True), False)[1])
app.entry.connect('changed', lambda w: print(json.dumps({'committed_text': w.get_text()}, ensure_ascii=False), flush=True))
app.entry.connect('preedit-changed', lambda w, t: print(json.dumps({'preedit': t}, ensure_ascii=False), flush=True))
def report():
    print(json.dumps({'entry_xy': app.entry.translate_coordinates(app.win, 0, 0),
        'entry_size': [app.entry.get_allocated_width(), app.entry.get_allocated_height()],
        'focused': app.entry.has_focus(), 'sensitive': app.entry.get_sensitive()}), flush=True)
    return True
GLib.timeout_add_seconds(10, report)
GLib.timeout_add_seconds(600, lambda: (app.win.destroy(), False)[1])
Gtk.main()
