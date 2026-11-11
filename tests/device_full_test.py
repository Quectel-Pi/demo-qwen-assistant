#!/usr/bin/env python3
"""Bounded device-side scenarios using the shipped ChatApp and real inference service.

Most quality cases mute speech in this test instance to bound runtime. Explicit
speech cases use the real speech engine. No deployed source or model is edited.
Kernel input tests are performed by the host while the initial input gate is open.
"""
from __future__ import annotations
import ast
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import wave

OUT = Path(os.environ.get('RKCHAT_TEST_OUT', '/tmp/rkchat-full-test'))
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, '/data/rkllm-chat/ui/rkchat')
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, Gio, GLib
import rkchat
import voice

RESULTS = []
START = time.monotonic()
app = rkchat.ChatApp(fullscreen=True, keyboard='system')
# Only the test instance is muted for non-speech cases. This also suppresses the
# old non-streaming fallback; simply setting auto_speak=False is not enough.
original_speak_async = app._speak_async
app._speak_async = lambda text: original_speak_async(text) if app.auto_speak else None
app.cli.connect()
LAST_DONE = None
WARNINGS, ERRORS = [], []
original_done, original_warn, original_error = app._on_done, app._on_warn, app._on_error


def done_event(info):
    global LAST_DONE
    answer = app.cur_bubble.get_text() if app.cur_bubble else ''
    LAST_DONE = (dict(info), answer)
    original_done(info)


def warn_event(msg):
    WARNINGS.append(str(msg))
    original_warn(msg)


def error_event(msg):
    ERRORS.append(str(msg))
    original_error(msg)

app.cli.on_done = done_event
app.cli.on_warn = warn_event
app.cli.on_error = error_event


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if time.monotonic() - START > 780:
            raise TimeoutError('Whole test exceeded 13-minute budget')
        context = GLib.MainContext.default()
        for _ in range(100):
            if not context.pending():
                break
            context.iteration(False)
        time.sleep(0.01)


def metrics():
    data = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith(('MemAvailable:', 'MemTotal:')):
            k, value, *_ = line.split()
            data[k[:-1] + '_MiB'] = round(int(value) / 1024, 1)
    data['load1'] = os.getloadavg()[0]
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            data['ui_RSS_MiB'] = round(int(line.split()[1]) / 1024, 1)
    return data


def save_result(record):
    record.update(elapsed_run_s=round(time.monotonic() - START, 2), resource=metrics())
    RESULTS.append(record)
    with (OUT / 'results.jsonl').open('a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    print('[RESULT] %s %s %s' % (record['id'], record.get('status'), record.get('note', '')), flush=True)


def capture(name):
    # The GNOME screenshot helper uses the compositor, not an X11 root capture.
    dest = OUT / (name + '.png')
    try:
        p = subprocess.run([sys.executable, '/tmp/rkchat-screenshot-wayland.py', str(dest)],
                           capture_output=True, text=True, timeout=20)
        return dest.name if p.returncode == 0 and dest.exists() else None
    except Exception:
        return None


def clear():
    app._on_clear_clicked()
    pump(0.4)


def stop_speech():
    sp = app._stream_spk
    if sp is not None:
        sp.stop()
        sp.join(timeout=3)
        app._stream_spk = None
    if app._stop_speak is not None:
        app._stop_speak.set()


def ask_case(id_, category, prompt, grade=None, clear_first=True,
             image=None, speech=False, screenshot=False, abort=False, expected_error=False):
    global LAST_DONE
    stop_speech()
    if clear_first:
        clear()
    app.auto_speak = speech
    LAST_DONE = None
    ERRORS.clear(); WARNINGS.clear()
    before = metrics()
    t0 = time.monotonic()
    # Normal and live-camera scenarios go through the real send-button path.
    # An explicit static image uses the same _start_ask path as a camera snapshot.
    if image is None:
        app.entry.set_text(prompt)
        app._on_send_clicked()
    else:
        app._add_bubble('user', prompt, image=image)
        app.history.append({'q': prompt, 'a': None, 'img': True})
        app._start_ask(prompt, image)
    image_sent = app.cam_mode or image is not None
    aborted_sent = False
    while LAST_DONE is None and not ERRORS and time.monotonic() - t0 < 75:
        pump(0.05)
        if abort and not aborted_sent and app.cur_bubble and len(app.cur_bubble.get_text()) > 15:
            app._interrupt('自动测试：停止生成')
            aborted_sent = True
    if LAST_DONE is None and expected_error and ERRORS:
        record = dict(id=id_, category=category, prompt=prompt, status='PASS',
                      note='Invalid image rejected without generating a cached-scene answer', errors=list(ERRORS))
        save_result(record)
        return record
    if LAST_DONE is None:
        app.cli.abort()
        pump(2)
        record = dict(id=id_, category=category, prompt=prompt, status='FAIL',
                      note='No done event within bounded deadline', errors=list(ERRORS))
        save_result(record)
        return record
    done, answer = LAST_DONE
    wall_ms = round((time.monotonic() - t0) * 1000)
    pump(0.2)
    status = 'PASS' if not ERRORS else 'FAIL'
    note = 'UI send/stream/done chain completed'
    if expected_error:
        status = 'FAIL'
        note = 'Expected an image error, but generation completed'
    quality = None
    if grade is not None:
        quality, note = grade(answer)
        status = 'PASS' if quality else 'FAIL'
    if abort:
        quality = aborted_sent and bool(done.get('aborted'))
        status = 'PASS' if quality else 'FAIL'
        note = 'User-stop path returned an aborted done event' if quality else 'Abort did not complete as expected'
    spoken = None
    if speech and app._stream_spk:
        deadline = time.monotonic() + 30
        while app._stream_spk.busy and time.monotonic() < deadline:
            pump(0.1)
        spoken = app._stream_spk._spoken_any
        if not spoken:
            status = 'FAIL'
            note += '; no successful playback completion reported'
    image_file = None
    snapshot = app._last_snapshot
    if app.cam_mode and snapshot and Path(snapshot['path']).exists():
        image_file = id_ + '-camera.bmp'
        shutil.copyfile(snapshot['path'], OUT / image_file)
    shot = capture(id_) if screenshot else None
    record = dict(id=id_, category=category, prompt=prompt, answer=answer,
                  status=status, note=note, quality_pass=quality, image_sent=image_sent,
                  wall_ms=wall_ms, service_ms=done.get('ms'), ttft_ms=done.get('ttft'),
                  tokens=done.get('tokens'), chars=done.get('chars'), aborted=done.get('aborted', False),
                  warnings=list(WARNINGS), errors=list(ERRORS), spoken_completed=spoken,
                  screenshot=shot, camera_frame=image_file, before=before)
    save_result(record)
    return record


def exact(expected):
    def grade(text):
        return text.strip() == expected, 'Expected exact output: ' + expected
    return grade


def contains(word):
    return lambda text: (word in text, 'Expected to mention: ' + word)


def json_grade(text):
    try:
        clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
        return json.loads(clean) == {'status': 'ok', 'count': 3}, 'Valid JSON with exact requested fields'
    except Exception:
        return False, 'Output is not the requested JSON'


def code_grade(text):
    match = re.search(r'```(?:python)?\s*\n(.*?)```', text, re.S)
    code = match.group(1) if match else text.strip()
    try:
        tree = ast.parse(code)
        # Permit only a pure arithmetic function. No imports, attributes, loops,
        # calls, filesystem, network or generated side effects are executed.
        allowed = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
                   ast.BinOp, ast.Add, ast.Name, ast.Load, ast.Constant)
        if any(not isinstance(n, allowed) for n in ast.walk(tree)):
            return False, 'Generated code outside pure-function safety allowlist (not executed)'
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            return False, 'Expected a single function definition'
        env = {'__builtins__': {}}
        exec(compile(tree, '<generated-pure-function>', 'exec'), env)
        fn = env.get('add')
        ok = callable(fn) and all(fn(a, b) == a + b for a, b in [(2,3),(-1,1),(10,20)])
        return ok, 'Pure add function passed 3 deterministic checks' if ok else 'Function check failed'
    except Exception as e:
        return False, 'Code syntax/function check: ' + type(e).__name__


def protocol_case(id_, request, expected):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect('/run/rkllm-chat.sock')
        s.sendall(json.dumps(request).encode() + b'\n')
        raw = s.recv(4096).decode()
        parsed = json.loads(raw.splitlines()[0])
        save_result(dict(id=id_, category='错误处理', status='PASS' if parsed.get('ev') == expected else 'FAIL',
                         note='Actual protocol response', response=parsed))
    except Exception as e:
        save_result(dict(id=id_, category='错误处理', status='FAIL', note=str(e)))
    finally:
        s.close()


def run():
    print('[START] ' + json.dumps({'uid': os.getuid(), 'backend': Gdk.Display.get_default().__gtype__.name,
                                 'builtin_keyboard': app.vkb is not None, 'resource': metrics()}), flush=True)
    save_result(dict(id='native_system_keyboard', category='系统键盘',
                     status='PASS' if app.vkb is None and 'Wayland' in Gdk.Display.get_default().__gtype__.name else 'FAIL',
                     note='Native Wayland; no application keyboard object constructed'))
    events = []
    app.entry.connect('changed', lambda w: events.append(w.get_text()))
    print('[READY_FOR_KEYBOARD]', flush=True)
    deadline = time.monotonic() + 100
    while not (OUT / 'keyboard-go').exists() and time.monotonic() < deadline:
        pump(0.1)
    ok = any(x.strip() == 'hello' for x in events) and any(x.strip() == '你好' for x in events)
    save_result(dict(id='physical_keyboard_input', category='系统键盘', status='PASS' if ok else 'FAIL',
                     note='Kernel input events reached actual GtkEntry (English and Chinese)',
                     committed_text=events, screenshot=capture('keyboard-input')))
    app.entry.set_text('')
    # Do not keep the keyboard open over the scenario display.
    app.btn_clear.grab_focus()
    pump(0.4)
    while not voice.is_ready() and time.monotonic() - START < 120:
        pump(0.1)
    save_result(dict(id='speech_engine_ready', category='语音', status='PASS' if voice.is_ready() else 'FAIL',
                     note='Real ASR/TTS/VAD preload status'))
    ask_case('greeting', '闲聊', '你好呀，请用一句话回应。', screenshot=True)
    ask_case('comfort', '闲聊', '今天工作有点累，请给我一句鼓励的话，不超过30字。')
    ask_case('joke', '闲聊', '请讲一个简短的笑话，不超过80字。')
    ask_case('capabilities', '闲聊', '你能帮我做什么？',
             lambda t: ('无法提供涉及违法' not in t and len(t)>5, 'No unrelated safety refusal expected'))
    ask_case('fact_beijing', '知识', '中国的首都是哪里？只输出城市名称。', exact('北京'))
    ask_case('multiply', '数学', '12乘以13等于多少？只回答数字。', exact('156'))
    ask_case('simple_word_math', '数学', '小明有3个苹果，又买了2个，现在有几个苹果？只输出数字。', exact('5'))
    ask_case('instruction_ok', '指令遵循', '只输出大写字母OK，不要解释，不要标点。', exact('OK'))
    ask_case('instruction_json', '指令遵循', '只输出JSON对象：status为字符串ok，count为数字3。不要解释。', json_grade)
    ask_case('translation', '翻译', '将“早上好”翻译成英语，只输出翻译。',
             lambda t: (t.strip().lower().rstrip('.!') == 'good morning', 'Expected Good morning'))
    ask_case('python_add', '代码', '写一个Python函数add(a,b)，返回两个参数的和。只输出函数代码。', code_grade)
    long_result = ask_case('long_answer', '文字显示', '请用中文分三点介绍骑行的好处，总共约150字。', screenshot=True)
    clipping = []
    for widget in app.chat_box.get_children():
        if isinstance(widget, rkchat.Bubble) and widget.get_text():
            alloc = widget.label.get_allocation()
            lw, lh = widget.label.get_layout().get_pixel_size()
            clipping.append({'role':widget.role, 'allocation':[alloc.width,alloc.height], 'layout':[lw,lh]})
    save_result(dict(id='label_bounds', category='文字显示',
        status='PASS' if all(x['allocation'][1]>=x['layout'][1] and x['allocation'][0]>=x['layout'][0] for x in clipping) else 'FAIL',
        note='Actual wrapped label allocation vs Pango layout', labels=clipping))
    ask_case('remember_name', '上下文', '请记住：我叫小林，我最喜欢骑行。简单回应即可。')
    ask_case('recall_name', '上下文', '我叫什么名字？只输出名字。', contains('小林'), clear_first=False)
    ask_case('recall_hobby', '上下文', '我最喜欢什么活动？', contains('骑行'), clear_first=False, screenshot=True)
    ask_case('after_clear', '上下文', '你知道我的名字吗？',
             lambda t: ('小林' not in t, 'Cleared context must not retain previous name'))
    # Actual camera, not a fixed photograph.
    app._on_cam_clicked()
    pump(3)
    first = app.cam.latest()[1] if app.cam.running else 0
    pump(2)
    second = app.cam.latest()[1] if app.cam.running else 0
    save_result(dict(id='camera_preview', category='实时摄像头',
                     status='PASS' if app.cam.running and second>first else 'FAIL',
                     note='Live frame sequence increases on /dev/video73', frame_delta=second-first,
                     screenshot=capture('camera-preview')))
    if app.cam.running:
        ask_case('live_describe', '实时摄像头', '用两句话描述眼前画面，只描述能看到的内容。', screenshot=True)
        app._on_cam_clicked()  # Must be off for a genuine cached-image follow-up.
        pump(0.5)
        ask_case('live_followup', '视觉上下文', '刚才那幅画面里，背景有什么？', clear_first=False, screenshot=True)
    elif app.cam_mode:
        app._on_cam_clicked()
    static = '/data/rkllm_qwen35/demo.jpg'
    ask_case('static_image', '静态看图', '这张图片主要是什么人物？用一句话回答。',
             contains('宇航'), image=static, screenshot=True)
    ask_case('static_color_followup', '视觉上下文', '刚才图片里，那个人拿的瓶子是什么颜色？只回答颜色。',
             contains('绿'), clear_first=False)
    ask_case('missing_image', '错误处理', '这张图片里有什么？', image='/tmp/rkchat-test-nonexistent-image.jpg', expected_error=True)
    protocol_case('empty_prompt', {'cmd':'ask','prompt':'','keep_history':0}, 'error')
    protocol_case('unknown_command', {'cmd':'rkchat-invalid-test-command'}, 'error')
    ask_case('stop_generation', '打断恢复', '请从1一直数到100，每个数字用逗号分隔。', abort=True, screenshot=True)
    ask_case('after_stop', '打断恢复', '只回复：恢复成功。', contains('恢复成功'), clear_first=False)
    app.cli.close(); pump(0.5)
    reconnected = app.cli.connect(); pump(0.3)
    save_result(dict(id='client_reconnect', category='连接恢复', status='PASS' if reconnected else 'FAIL',
                     note='Reconnect only the UI socket; inference service was not restarted'))
    ask_case('after_reconnect', '连接恢复', '只输出pong。', exact('pong'))
    if voice.is_ready():
        import numpy as np
        pcm, rate, syn_seconds = voice._engine.synthesize('你好，这是设备语音测试。')
        with wave.open(str(OUT / 'tts-test.wav'), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes((np.clip(pcm,-1,1)*32767).astype(np.int16).tobytes())
        rms = float(np.sqrt(np.mean(pcm*pcm)))
        save_result(dict(id='tts_synthesis', category='语音', status='PASS' if len(pcm)>rate and rms>0.001 else 'FAIL',
            note='Real synthesized waveform; physical audibility requires listening',
            samples=len(pcm),sample_rate=rate,rms=rms,synthesis_seconds=syn_seconds))
        resampled = np.interp(np.arange(int(len(pcm)*16000/rate))*rate/16000,
                              np.arange(len(pcm)),pcm).astype(np.float32)
        text, cost = voice._engine.transcribe(resampled)
        save_result(dict(id='tts_asr_roundtrip', category='语音识别',
                         status='PASS' if '你好' in text else 'FAIL',
                         note='Digital TTS-to-ASR test, not acoustic microphone recognition', transcription=text,seconds=cost))
        mic = voice._mic
        frames=[]
        try:
            mic.open()
            t=time.monotonic()
            while time.monotonic()-t<2.5:
                frame=mic.read(512,timeout=0.3)
                if frame is not None: frames.append(frame)
        finally:
            mic.close()
        count=sum(len(x) for x in frames)
        sound=np.concatenate(frames) if frames else np.zeros(0,dtype=np.float32)
        save_result(dict(id='mic_capture', category='麦克风', status='PASS' if count>16000 else 'FAIL',
                         note='Real microphone device read; no claim about ambient speech accuracy',
                         samples=count, rms=float(np.sqrt(np.mean(sound*sound))) if count else 0,
                         device=mic.device))
        ask_case('ui_auto_speech', '自动朗读', '请只回答：你好，很高兴见到你。',
                 speech=True, screenshot=True)
        if app.cam_mode is False:
            app._on_cam_clicked(); pump(2)
        ask_case('camera_and_speech', '并发场景', '只用一句话说出画面里的主要物体。',
                 speech=True, screenshot=True)
        if app.cam_mode:
            app._on_cam_clicked(); pump(0.5)
    for i in range(5):
        ask_case('repeat_%d'%i, '稳定性', '只输出测试成功。', exact('测试成功'))
    save_result(dict(id='manual_visual_accuracy', category='人工复核', status='MANUAL',
                     note='Compare saved live frame and description; unknown live scene prevents automatic truth grading'))
    save_result(dict(id='manual_audio_audibility', category='人工复核', status='MANUAL',
                     note='Playback completion/waveform do not prove loudspeaker is audible at the filming position'))
    summary={'seconds':round(time.monotonic()-START,1),
             'counts':{s:sum(r['status']==s for r in RESULTS) for s in ['PASS','FAIL','MANUAL']},
             'records':RESULTS}
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    capture('final-test-screen')
    print('[COMPLETE] '+json.dumps(summary['counts']),flush=True)


try:
    run()
except Exception as exc:
    import traceback
    traceback.print_exc()
    save_result(dict(id='test_runner_exception',category='测试框架',status='FAIL',note=repr(exc)))
finally:
    stop_speech()
    if app.cam_mode:
        app._on_cam_clicked()
    app.auto_speak=True
    app.entry.set_text('')
    pump(0.3)
    app.win.destroy()
    (OUT/'finished').write_text('done\n')
