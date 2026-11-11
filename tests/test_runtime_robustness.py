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


ROOT = Path(__file__).resolve().parents[1]
SERVICE_SRC = ROOT / 'deploy/native/rkllm_service.cpp'


def _extract(name):
    """Pull a `// <<<extract:name` ... `// <<<end` block out of the service source.

    The protocol helpers are hand-written C++ rather than Python, so the only
    honest way to unit-test them is to compile the very same bytes the service
    ships. The markers keep the two copies from drifting.
    """
    text = SERVICE_SRC.read_text(encoding='utf-8')
    start = text.index('// <<<extract:%s\n' % name) + len('// <<<extract:%s\n' % name)
    end = text.index('// <<<end', start)
    return text[start:end]


def _compile_block(name, body, out):
    """Compile the extracted `name` block, unless `body` already embeds it."""
    src = out.with_suffix('.cpp')
    prelude = ('#include <atomic>\n#include <string>\n#include <cstring>\n'
               '#include <sys/types.h>\n')
    already = '// <<<extract:%s' % name in body or 'static bool json_find' in body
    src.write_text(prelude + ('' if already else _extract(name)) + body, encoding='utf-8')
    subprocess.run(['g++', '-std=c++17', '-O0', '-o', str(out), str(src)],
                   check=True, capture_output=True)


def _run_probe(name, body):
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / name
        _compile_block(name, body, exe)
        return subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout


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

    def test_aplay_failure_is_absorbed_not_fatal(self):
        """P0-1 契约：aplay 非零退出只能记日志，不能让异常冲出 Speaker.play。

        板端 aplay 返回非零是常态（ES8389 时钟未锁定、USB 声卡热插拔），
        CalledProcessError 是 SubprocessError 而非 OSError，漏接会带崩整个免提进程。
        """
        import numpy as np
        source=Path(__file__).resolve().parents[1]/'deploy/voice-vision/talk.py'
        tree=ast.parse(source.read_text())
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Speaker')
        logged=[]
        namespace={'np':np,'os':os,'subprocess':subprocess,'time':time,
                   'SPK_DEVICE':'hw:0,0','APlay':'aplay',
                   'log':lambda tag,msg,color='dim': logged.append((tag,msg))}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),namespace)
        speaker=namespace['Speaker'](ratio=1)
        def playback(*args,**kwargs):
            if kwargs.get('check'):
                raise subprocess.CalledProcessError(1,args[0])
            return SimpleNamespace(returncode=1)
        with patch.object(subprocess,'run',side_effect=playback):
            # 不抛异常，正常返回耗时
            elapsed=speaker.play(np.array([0.1,0.2],dtype=np.float32),16000)
        self.assertGreaterEqual(elapsed,0.0)
        self.assertTrue(any(t=='ERR' for t,_ in logged),'播放失败必须记 ERR 日志')


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


class RequestParsingTests(unittest.TestCase):
    """P1-2: json_find must not let prompt text hijack real parameters."""

    PROBE = r'''
#include <cstdio>
#include <iostream>
static std::string val(const std::string &js, const char *key, bool *found) {
    size_t b = 0, e = 0;
    *found = json_find(js, key, &b, &e);
    return *found ? js.substr(b, e - b) : std::string();
}
int main() {
    const char *keys[] = {"cmd", "prompt", "image", "keep_history", "max_new"};
    std::string line;
    while (std::getline(std::cin, line)) {
        for (const char *k : keys) {
            bool found = false;
            std::string v = val(line, k, &found);
            printf("%s|%d|%s\n", k, found ? 1 : 0, v.c_str());
        }
        printf("--\n");
    }
    return 0;
}
'''

    def parse(self, line):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'json_probe'
            _compile_block('json_find', self.PROBE, exe)
            res = subprocess.run([str(exe)], input=line + '\n', check=True,
                                 capture_output=True, text=True).stdout
        fields = {}
        for row in res.split('--')[0].strip().splitlines():
            key, found, value = row.split('|', 2)
            fields[key] = value if found == '1' else None
        return fields

    def test_image_mentioned_inside_prompt_is_not_a_parameter(self):
        line = ('{"cmd":"ask","prompt":"你收到的 image 参数是什么？'
                '还有 \\"keep_history\\":0","keep_history":1}')
        got = self.parse(line)
        self.assertEqual(got['cmd'], 'ask')
        self.assertIsNone(got['image'], 'prompt text hijacked the image parameter')
        self.assertEqual(got['keep_history'], '1',
                         'prompt text hijacked keep_history')
        self.assertIn('你收到的 image 参数是什么', got['prompt'])

    def test_real_image_after_prompt_is_still_found(self):
        line = ('{"cmd":"ask","prompt":"<image> \\"image\\" 是什么",'
                '"keep_history":0,"image":"/tmp/a.jpg"}')
        got = self.parse(line)
        self.assertEqual(got['image'], '/tmp/a.jpg')
        self.assertEqual(got['keep_history'], '0')

    def test_string_value_with_comma_brace_and_colon_survives(self):
        line = '{"cmd":"ask","prompt":"a, b } c: d","max_new":32}'
        got = self.parse(line)
        self.assertEqual(got['prompt'], 'a, b } c: d')
        self.assertEqual(got['max_new'], '32')

    def test_escaped_quote_does_not_end_the_token(self):
        line = '{"prompt":"say \\"image\\":1","image":""}'
        got = self.parse(line)
        self.assertEqual(got['image'], '')
        self.assertEqual(got['prompt'], 'say \\"image\\":1')

    def test_missing_key_is_reported_as_missing(self):
        got = self.parse('{"cmd":"ping"}')
        self.assertEqual(got['cmd'], 'ping')
        self.assertIsNone(got['image'])
        self.assertIsNone(got['prompt'])


class AbortRoundTests(unittest.TestCase):
    """P1-5: abort must keep targeting the round the user was looking at."""

    PROBE = r'''
#include <cstdio>
int main() {
    // (round, abort_round, expected)
    const struct { unsigned r, a; bool want; } cases[] = {
        {1, 0, false},   // 没停过
        {1, 1, true},    // 停的正是第 1 轮
        {2, 1, false},   // 上一轮的 abort 不能误伤第 2 轮
        {3, 2, false},   // 同上，差一轮
        {2, 3, true},    // 停到更后的轮次，其中一个
        {0, 5, false},   // 未领轮次的上下文不算被打断
    };
    int bad = 0;
    for (auto &c : cases) {
        bool got = round_aborted(c.r, c.a);
        printf("%u/%u -> %d (want %d)\n", c.r, c.a, (int)got, (int)c.want);
        if (got != c.want) bad++;
    }
    return bad == 0 ? 0 : 1;
}
'''

    def test_abort_targets_the_intended_round_only(self):
        out = _run_probe('round_aborted', self.PROBE)
        self.assertIn('1/1 -> 1', out)
        self.assertIn('2/1 -> 0', out)

    def test_abort_while_waiting_for_the_lock_still_wins(self):
        # 这是 P1-5 的原始症状：请求还在等锁/编码时点了停止。
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'probe'
            body = r'''
#include <cstdio>
int main() {
    unsigned round = 7;             // 已经领到的轮次
    unsigned abort_round = 7;       // 等锁期间用户点了停止
    printf("%d\n", (int)round_aborted(round, abort_round));
    abort_round = 6;                // 只停了上一轮
    printf("%d\n", (int)round_aborted(round, abort_round));
    return 0;
}
'''
            _compile_block('round_aborted', body, exe)
            out = subprocess.run([str(exe)], check=True, capture_output=True,
                                 text=True).stdout.split()
        self.assertEqual(out, ['1', '0'])

    def test_abort_before_any_round_does_not_kill_the_next_one(self):
        # 用户先点了停止、然后才提问：那一轮不能被误杀。
        body = _extract('round_aborted') + r'''
#include <cstdio>
int main() {
    unsigned abort_round = 0;
    abort_round = abort_target_round(0, abort_round);   // 无轮次在跑
    printf("%u\n", abort_round);
    abort_round = abort_target_round(3, abort_round);   // 第 3 轮在跑
    printf("%u\n", abort_round);
    abort_round = abort_target_round(2, abort_round);   // 更早的轮次不倒退
    printf("%u\n", abort_round);
    printf("%d %d\n", (int)round_aborted(1, abort_round),
                       (int)round_aborted(3, abort_round));
    return 0;
}
'''
        out = _run_probe('abort_target_round', body).split()
        self.assertEqual(out[:3], ['0', '3', '3'])
        self.assertEqual(out[3:], ['1', '1'])   # 第 1、3 轮都算被停（abort_round=3）

    def test_abort_command_uses_the_helper(self):
        src = SERVICE_SRC.read_text(encoding='utf-8')
        abort_at = src.index('if (cmd == "abort")')
        body = src[abort_at:abort_at + 900]
        self.assertIn('abort_target_round(', body)
        self.assertNotIn('g_abort.store', body)


class DoneSentTests(unittest.TestCase):
    """P1-6: the fallback done must be suppressed once the callback sent one."""

    def test_done_is_sent_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'probe'
            body = r'''
#include <cstdio>
int main() {
    printf("%d", (int)should_send_done(false, false));
    printf("%d", (int)should_send_done(false, true));   // callback 已发
    printf("%d", (int)should_send_done(true, false));   // 客户端已断
    printf("%d\n", (int)should_send_done(true, true));
    return 0;
}
'''
            _compile_block('should_send_done', body, exe)
            out = subprocess.run([str(exe)], check=True, capture_output=True,
                                 text=True).stdout.strip()
        self.assertEqual(out, '1000')

    def test_callback_marks_done_sent_on_finish(self):
        src = SERVICE_SRC.read_text(encoding='utf-8')
        finish = src.index('state == RKLLM_RUN_FINISH')
        self.assertIn('ctx->done_sent.store(true)', src[finish:finish + 2000],
                      'RUN_FINISH never marks done_sent, so done is emitted twice')


class PeerUidTests(unittest.TestCase):
    """P1-4: the socket only serves root and the one desktop user."""

    def allowed(self, peer, allow):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'probe'
            body = ('#include <cstdio>\nint main() {\n'
                    '    printf("%%d%%d%%d%%d", (int)peer_uid_allowed(%d, %d),'
                    ' (int)peer_uid_allowed(%d, %d), (int)peer_uid_allowed(%d, %d),'
                    ' (int)peer_uid_allowed(%d, %d));\n    return 0;\n}\n'
                    % (peer, allow, 0, allow, 1001, allow, 1000, allow))
            _compile_block('peer_uid_allowed', body, exe)
            out = subprocess.run([str(exe)], check=True, capture_output=True,
                                 text=True).stdout.strip()
        return [c == '1' for c in out]

    def test_root_and_desktop_user_are_accepted_others_rejected(self):
        peer, root, desktop, other = self.allowed(1001, 1001)
        self.assertTrue(peer); self.assertTrue(root)
        self.assertTrue(desktop); self.assertFalse(other)

    def test_zero_allow_uid_disables_the_check(self):
        peer, root, desktop, other = self.allowed(1001, 0)
        self.assertTrue(peer and root and desktop and other)

    def test_service_drops_socket_mode_when_uid_list_is_on(self):
        src = SERVICE_SRC.read_text(encoding='utf-8')
        self.assertIn('SO_PEERCRED', src)
        self.assertIn('--allow-uid', src)
        body = ('#include <cstdio>\n#include <sys/types.h>\nint main() {\n'
                '    printf("%o %o\\n", (unsigned)socket_mode_for(true),'
                ' (unsigned)socket_mode_for(false));\n    return 0;\n}\n')
        self.assertEqual(_run_probe('socket_mode', body).split(), ['660', '666'],
                         'uid-listed mode must be 0660, not world-writable 0666')

    def test_socket_is_group_owned_by_the_allowed_user(self):
        # 板端实测：root:root 的 0660 会把 uid 1001 挡在 connect() 外面，
        # 所以降权限的同时必须把属组改成被允许用户的组。
        src = SERVICE_SRC.read_text(encoding='utf-8')
        chmod_at = src.index('chmod(sock_path.c_str(), socket_mode_for(listed))')
        self.assertIn('chown(', src[max(0, chmod_at - 1200):chmod_at],
                      '0660 without chowning to the desktop group locks the UI out')

    def test_unit_passes_the_desktop_uid(self):
        unit = (ROOT / 'deploy/rkllm-chat.service').read_text(encoding='utf-8')
        self.assertIn('--allow-uid 1001', unit)
        self.assertIn('TimeoutStopSec=', unit)
        self.assertIn('KillMode=mixed', unit)


class BufferAndModelGuardTests(unittest.TestCase):
    """P1-3 (read buffer cap) and P1-7 (ftell validation)."""

    def test_incomplete_request_line_is_capped(self):
        src = SERVICE_SRC.read_text(encoding='utf-8')
        thread = src[src.index('static void *client_thread'):]
        thread = thread[:thread.index('static void usage')]
        self.assertIn('buf.size() > 64 * 1024', thread,
                      'unbounded single-connection buffer can be exhausted')
        # 上限必须在消化完整行之后检查，否则长行已处理完还会被误断。
        self.assertLess(thread.index("buf.find('\\n')"),
                        thread.index('buf.size() > 64 * 1024'))

    def test_oversized_stream_is_dropped_by_a_real_cap(self):
        # 行为验证：模拟同一段 recv 循环，喂 10MB 无换行数据，必须在 64KB 附近停。
        body = r'''
#include <cstdio>
#include <string>
int main() {
    std::string buf;
    std::string chunk(8192, 'x');
    long consumed = 0;
    for (int i = 0; i < 1280; ++i) {          // 10 MB
        buf.append(chunk);
        consumed += (long)chunk.size();
        size_t nl;
        while ((nl = buf.find('\n')) != std::string::npos) buf.erase(0, nl + 1);
        if (buf.size() > 64 * 1024) break;
    }
    printf("%ld %zu\n", consumed, buf.size());
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'cap'
            src = exe.with_suffix('.cpp')
            src.write_text(body, encoding='utf-8')
            subprocess.run(['g++', '-std=c++17', '-O0', '-o', str(exe), str(src)], check=True)
            consumed, held = subprocess.run([str(exe)], check=True, capture_output=True,
                                            text=True).stdout.split()
        self.assertLessEqual(int(held), 64 * 1024 + 8192)
        self.assertLess(int(consumed), 10 * 1024 * 1024, 'cap never triggered')

    def test_vision_model_size_is_validated_before_allocating(self):
        src = SERVICE_SRC.read_text(encoding='utf-8')
        at = src.index('long len = ftell(fp);')
        guard = src[at:at + 400]
        self.assertIn('len <= 0', guard, 'ftell()==-1 must not reach std::vector ctor')
        self.assertIn('2UL << 30', guard)
        self.assertLess(guard.index('len <= 0'), guard.index('std::vector<char> buf'))


if __name__ == '__main__':
    unittest.main()
