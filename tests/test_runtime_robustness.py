#!/usr/bin/env python3
import ast
import importlib
import os
import subprocess
from pathlib import Path
import socket
import sys
import threading
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/ui/rkchat'))
import voice
import client
import rkchat


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(synthesize=lambda text: ([0.1] * 8, 16000, 0.001))
        self.played = []
        self.speaker = SimpleNamespace(play=lambda pcm, rate: self.played.append((pcm, rate)))
        self.patchers = [patch.object(voice, '_engine', self.engine),
                         patch.object(voice, '_speaker', self.speaker),
                         patch.object(voice, 'ensure_ready', return_value=(True, 'ready')),
                         patch.object(voice.subprocess, 'run')]
        for p in self.patchers: p.start()
        self.streams = []

    def tearDown(self):
        for s in self.streams:
            if s.busy: s.stop()
            s.join(timeout=2)
        for p in reversed(self.patchers): p.stop()

    def stream(self, **kwargs):
        stream = voice.StreamSpeaker(**kwargs)
        self.streams.append(stream)
        return stream

    def test_cold_start_waits_for_engine(self):
        voice._engine = None
        def load():
            voice._engine = self.engine
            return True, 'ready'
        voice.ensure_ready.side_effect = load
        s = self.stream(); s.start(); s.feed('你好。'); s.finish(); s.join(timeout=2)
        self.assertEqual(len(self.played), 1, 'First sentence lost before the engine was ready')

    def test_text_queue_does_not_drop_sentences(self):
        seen = []
        self.engine.synthesize = lambda text: (seen.append(text) or [0.1], 16000, 0)
        s = self.stream()
        # Queue before starting workers so backpressure is deterministic.
        for i in range(20): s.feed('第%d句。' % i)
        s.start(); s.finish(); s.join(timeout=2)
        self.assertEqual(len(seen), 20)
        self.assertEqual(len(self.played), 20)

    def test_finish_never_blocks_gui_when_queue_is_full(self):
        s = self.stream()
        for _ in range(8): s.feed('你好。')
        returned = threading.Event()
        thread = threading.Thread(target=lambda: (s.finish(), returned.set()))
        thread.start()
        finished_promptly = returned.wait(0.1)
        s.start(); thread.join(timeout=2); s.join(timeout=2)
        self.assertTrue(finished_promptly, 'finish blocked instead of returning to GTK')

    def test_late_feed_after_finish_is_ignored(self):
        seen = []
        self.engine.synthesize = lambda text: (seen.append(text) or [0.1], 16000, 0)
        s = self.stream(); s.feed('第一句。'); s.finish(); s.feed('不应播放。')
        s.start(); s.join(timeout=2)
        self.assertEqual(seen, ['第一句。'])
        self.assertTrue(s._tq.empty(), 'Late text remained queued after completion')

    def test_two_rounds_never_synthesize_concurrently(self):
        gate = threading.Event(); entered = threading.Event()
        lock = threading.Lock(); active = [0]; peak = [0]
        def synthesize(text):
            with lock:
                active[0] += 1; peak[0] = max(peak[0], active[0])
            entered.set(); gate.wait(2)
            with lock: active[0] -= 1
            return [0.1], 16000, 0
        self.engine.synthesize = synthesize
        one = self.stream(); two = self.stream()
        one.start(); one.feed('第一句。'); self.assertTrue(entered.wait(1))
        two.start(); two.feed('第二句。'); one.finish(); two.finish()
        time.sleep(0.08); gate.set(); one.join(timeout=2); two.join(timeout=2)
        self.assertEqual(peak[0], 1, 'Shared TTS engine was entered by two rounds concurrently')

    def test_initialization_failure_is_reported(self):
        voice._engine = None
        voice.ensure_ready.return_value = (False, 'test load failure')
        states = []
        s = self.stream(on_state=states.append); s.start(); s.feed('你好。'); s.finish(); s.join(timeout=2)
        self.assertTrue(any('test load failure' in x for x in states))
        self.assertFalse(s.busy)


class AudioRoutingTests(unittest.TestCase):
    def test_explicit_mic_setting_wins(self):
        with patch.dict(os.environ, {'MIC_DEVICE':'hw:custom,0'}):
            self.assertEqual(voice.capture_device(Path('/missing')), 'hw:custom,0')

    def test_usb_capture_uses_stable_card_name(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            card=Path(tmp)/'card3';card.mkdir();(card/'usbid').write_text('0c45:2283')
            (card/'pcm0c').mkdir();(card/'id').write_text('U1080P\n')
            self.assertEqual(voice.capture_device(Path(tmp)), 'plughw:CARD=U1080P,DEV=0')

    def test_no_usb_capture_falls_back_to_onboard(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            self.assertEqual(voice.capture_device(Path(tmp)), 'hw:0,0')

    def test_same_card_alias_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            card=Path(tmp)/'card0';card.mkdir();(card/'id').write_text('rockchipes8389\n')
            self.assertEqual(voice._card_id('hw:0,0', Path(tmp)), 'rockchipes8389')
            self.assertEqual(voice._card_id('plughw:CARD=rockchipes8389,DEV=0', Path(tmp)), 'rockchipes8389')

    def test_no_pcm_is_not_reported_ready(self):
        io=voice.VoiceIO()
        with patch.object(voice,'_mic',SimpleNamespace(read=lambda *a,**k:None)), \
             patch.object(voice.time,'monotonic',side_effect=[0,0.5,1.1]):
            self.assertFalse(io._wait_for_capture(timeout=1))

    def test_pcm_arrival_is_required_before_ready(self):
        io=voice.VoiceIO();mic=SimpleNamespace(read=Mock(side_effect=[None,[0.1],[0.1]]))
        with patch.object(voice,'_mic',mic), \
             patch.object(voice.time,'monotonic',side_effect=[0,0.1,0.2,0.21,0.22,0.3,0.6]):
            self.assertTrue(io._wait_for_capture())
        self.assertEqual(mic.read.call_count,3)

    def test_cancelled_dictation_is_not_submitted(self):
        io=voice.VoiceIO();io._wait_for_capture=Mock(return_value=True)
        def read(*args,**kwargs):
            io.stop_listening()
            return [0.1]
        mic=SimpleNamespace(device='hw:0,0',open=Mock(),close=Mock(),read=read)
        vad=SimpleNamespace(accept_waveform=Mock(),empty=Mock(side_effect=[False,True]),
                            front=SimpleNamespace(samples=[0.1]),pop=Mock())
        engine=SimpleNamespace(vad=vad,transcribe=Mock(return_value=('过期的半句话',0.1)))
        with patch.object(voice,'ensure_ready',return_value=(True,'ready')), \
             patch.object(voice,'_mic',mic), patch.object(voice,'_engine',engine):
            self.assertIsNone(io.listen_once())
        engine.transcribe.assert_not_called()
        mic.close.assert_called_once()

    def test_cancel_during_half_duplex_wait_does_not_open_mic(self):
        io=voice.VoiceIO();ready=threading.Event();result=[]
        mic=SimpleNamespace(device='hw:0,0',open=Mock(),close=Mock())
        def initialized():
            ready.set();return True,'ready'
        with patch.object(voice,'ensure_ready',side_effect=initialized), \
             patch.object(voice,'_mic',mic), \
             patch.object(voice,'_speaker',SimpleNamespace(device='hw:0,0')):
            voice._half_duplex_lock.acquire()
            thread=threading.Thread(target=lambda:result.append(io.listen_once()))
            try:
                thread.start();self.assertTrue(ready.wait(1));io.stop_listening()
            finally:
                voice._half_duplex_lock.release()
            thread.join(timeout=1)
        self.assertEqual(result,[None]);mic.open.assert_not_called()

    def test_same_card_recording_is_half_duplex(self):
        with patch.object(voice,'_mic',SimpleNamespace(device='hw:0,0')), \
             patch.object(voice,'_speaker',SimpleNamespace(device='hw:0,0')):
            self.assertFalse(voice.can_listen_while_playing())

    def test_aplay_failure_is_not_reported_as_success(self):
        import numpy as np
        source=Path(__file__).resolve().parents[1]/'deploy/voice-vision/talk.py'
        tree=ast.parse(source.read_text())
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Speaker')
        namespace={'np':np,'os':os,'subprocess':subprocess,'time':time,'SPK_DEVICE':'hw:0,0','APlay':'aplay'}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),namespace)
        speaker=namespace['Speaker'](ratio=1)
        def playback(*args,**kwargs):
            if kwargs.get('check'):
                raise subprocess.CalledProcessError(1,args[0])
            return SimpleNamespace(returncode=1)
        with patch.object(subprocess,'run',side_effect=playback):
            with self.assertRaises(subprocess.CalledProcessError):
                speaker.play(np.array([0.1,0.2],dtype=np.float32),16000)


class FakeReader:
    def __init__(self):
        self.entered = threading.Event(); self.release = threading.Event()
        self.closed = False
    def readline(self):
        self.entered.set(); self.release.wait(3)
        return b''
    def close(self): self.closed = True


class FakeSocket:
    def __init__(self): self.reader = FakeReader()
    def settimeout(self, value): pass
    def connect(self, path): pass
    def makefile(self, mode): return self.reader
    def shutdown(self, how): pass
    def close(self): pass


class ConnectionTests(unittest.TestCase):
    def test_old_queued_gui_callbacks_are_discarded(self):
        tokens, pending = [], []
        c = client.ChatClient(on_token=tokens.append)
        with patch.object(client, '_dispatch', side_effect=lambda fn, **kw: pending.append(fn)):
            c._d(c.on_token, 'stale')
            c.close()
            for callback in pending: callback()
        self.assertEqual(tokens, [])

    def test_clear_error_ack_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'service.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(path); listener.listen(1)
                def respond():
                    conn, _ = listener.accept()
                    with conn:
                        conn.recv(4096)
                        conn.sendall(b'{"ev":"error","msg":"clear failed"}\n')
                thread = threading.Thread(target=respond); thread.start()
                c = client.ChatClient(sock_path=path, threaded_dispatch=False)
                self.assertFalse(c.clear_history())
                thread.join(timeout=1)

    def test_fragmented_control_ack_is_read_to_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'service.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(path); listener.listen(1)
                def respond():
                    conn, _ = listener.accept()
                    with conn:
                        conn.recv(4096)
                        conn.sendall(b'{"ev":')
                        time.sleep(0.01)
                        conn.sendall(b'"cleared"}\n')
                thread = threading.Thread(target=respond); thread.start()
                c = client.ChatClient(sock_path=path, threaded_dispatch=False)
                self.assertTrue(c.clear_history())
                thread.join(timeout=1)

    def test_close_clears_busy_and_handles(self):
        c = client.ChatClient(threaded_dispatch=False)
        c._busy = True
        c.close()
        self.assertFalse(c.busy)
        self.assertIsNone(c._file)
        self.assertIsNone(c._sock)

    def test_old_receiver_cannot_disconnect_reconnected_socket(self):
        old, new = FakeSocket(), FakeSocket()
        c = client.ChatClient(threaded_dispatch=False)
        try:
            with patch.object(client.socket, 'socket', side_effect=[old, new]):
                self.assertTrue(c.connect()); self.assertTrue(old.reader.entered.wait(1))
                old_thread = c._rx
                c.close()
                self.assertTrue(c.connect()); self.assertTrue(new.reader.entered.wait(1))
                old.reader.release.set(); old_thread.join(timeout=1)
                self.assertTrue(c.connected, 'Old recv thread marked the new connection dead')
        finally:
            old.reader.release.set(); new.reader.release.set(); c.close()


class UiTests(unittest.TestCase):
    def app(self):
        app = rkchat.ChatApp.__new__(rkchat.ChatApp)
        for name,value in dict(ttft_ms=1,cur_bubble=None,_interrupt_reason=None,history=[],
                               auto_speak=False,_stream_spk=None,_speak_buf='tail',_alive=True,
                               _capture_pending=False,_cam_closing=False).items():
            setattr(app,name,value)
        app.lbl_speed = Mock(); app.btn_send = Mock(); app._set_controls_busy = Mock()
        app._speak_async = Mock(); app._add_bubble = Mock(); app._stop_reading = Mock()
        return app

    def test_clear_while_busy_preserves_current_round(self):
        app = self.app(); app.cli = SimpleNamespace(busy=True)
        app._on_warn = Mock(); app.chat_box = Mock()
        app._on_clear_clicked()
        app.chat_box.get_children.assert_not_called()
        app._on_warn.assert_called_once()

    def test_failed_server_clear_does_not_erase_local_history(self):
        app = self.app(); app.cli = SimpleNamespace(busy=False, clear_history=lambda: False)
        app._on_warn = Mock(); app.chat_box = Mock()
        app.history = [{'q': 'hello', 'a': 'answer'}]
        app._on_clear_clicked()
        self.assertEqual(app.history, [{'q': 'hello', 'a': 'answer'}])
        app.chat_box.get_children.assert_not_called()

    def test_done_with_reading_enabled_uses_fallback_when_needed(self):
        app = self.app(); app.auto_speak = True
        app.cur_bubble = Mock(); app.cur_bubble.get_text.return_value = '你好。'
        app._on_done({'ms':10,'tokens':3})
        app._speak_async.assert_called_once_with('你好。')

    def test_destroyed_ui_ignores_late_voice_state(self):
        app = self.app(); app._alive = False; app.lbl_voice = Mock()
        app._voice_state('late status')
        app.lbl_voice.set_text.assert_not_called()

    def test_muted_done_does_not_fall_back_to_speech(self):
        app = self.app()
        app.cur_bubble = Mock()
        app.cur_bubble.get_text.return_value = '你好。'
        app._on_done({'ms':10,'tokens':3})
        app._speak_async.assert_not_called()

    def test_request_error_stops_speech_and_clears_buffer(self):
        app = self.app(); sp = Mock(); app._stream_spk = sp
        app._stop_reading = lambda: (sp.stop(), setattr(app,'_speak_buf',''))
        app._on_error('test failure')
        sp.stop.assert_called_once()
        self.assertEqual(app._speak_buf, '')

    def test_voice_state_from_worker_is_marshaled_to_main_thread(self):
        app = self.app(); app._ui_thread_id = threading.get_ident(); app.lbl_voice = Mock()
        pending = []
        with patch.object(rkchat.GLib, 'idle_add', side_effect=lambda fn,*args: pending.append((fn,args))):
            worker = threading.Thread(target=app._voice_state, args=('在听',))
            worker.start(); worker.join()
            app.lbl_voice.set_text.assert_not_called()
            self.assertEqual(len(pending), 1)
            fn,args = pending[0]; fn(*args)
        app.lbl_voice.set_text.assert_called_once_with('在听')


if __name__ == '__main__':
    unittest.main()
