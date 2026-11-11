#!/usr/bin/env python3
"""Targeted verification where the first run was inconclusive; keeps original results."""
import json, os, sys, time, threading, wave
from pathlib import Path
OUT=Path(os.environ['RKCHAT_TEST_OUT']); OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,'/data/rkllm-chat/ui/rkchat')
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,GLib,Gdk
import rkchat,voice
app=rkchat.ChatApp(fullscreen=True,keyboard='system')
app.cli.connect(); app.auto_speak=False
app._speak_async=lambda text: None  # Test instance only: explicit audio test below.
DONE=None; ERRORS=[]; RESULTS=[]; START=time.monotonic()
original_done,original_error=app._on_done,app._on_error

def on_done(info):
    global DONE
    DONE=(dict(info),app.cur_bubble.get_text() if app.cur_bubble else '')
    original_done(info)

def on_error(msg):
    ERRORS.append(str(msg));original_error(msg)
app.cli.on_done=on_done;app.cli.on_error=on_error

def pump(s):
    end=time.monotonic()+s
    while time.monotonic()<end:
        ctx=GLib.MainContext.default()
        for _ in range(100):
            if not ctx.pending():break
            ctx.iteration(False)
        time.sleep(0.01)

def record(id_,status,**data):
    r=dict(id=id_,status=status,elapsed_run_s=round(time.monotonic()-START,2),**data)
    RESULTS.append(r)
    with (OUT/'results.jsonl').open('a') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    print('[RESULT]',id_,status,flush=True)

def capture(name):
    import subprocess
    dest=OUT/(name+'.png')
    p=subprocess.run([sys.executable,'/tmp/rkchat-screenshot-wayland.py',str(dest)],capture_output=True,timeout=20)
    return dest.name if p.returncode==0 and dest.exists() else None

def ask(id_,q,reset=True,stop=False):
    global DONE
    if reset:app._on_clear_clicked();pump(0.4)
    DONE=None;ERRORS.clear();t0=time.monotonic();sent_stop=False
    app.entry.set_text(q);app._on_send_clicked()
    while DONE is None and not ERRORS and time.monotonic()-t0<70:
        pump(0.015)
        # Stop on the first token, not after a 15-character threshold. The first
        # run's response had only 14 chars and finished before that threshold.
        if stop and not sent_stop and app.cur_bubble and app.cur_bubble.get_text():
            app._interrupt('自动测试：立即停止');sent_stop=True
    info,answer=DONE if DONE else ({},'')
    record(id_,'PASS' if DONE and not ERRORS and (not stop or sent_stop and info.get('aborted')) else 'FAIL',
           category='打断恢复' if stop else '连接恢复',prompt=q,answer=answer,done=info,
           abort_sent=sent_stop,errors=list(ERRORS),wall_ms=round((time.monotonic()-t0)*1000))
    return answer

try:
    events=[]
    def changed(entry):
        value=entry.get_text();events.append(value)
        with (OUT/'keyboard-events.jsonl').open('a') as f:
            f.write(json.dumps({'text':value},ensure_ascii=False)+'\n')
    app.entry.connect('changed',changed)
    print('[READY_FOR_KEYBOARD]',flush=True)
    deadline=time.monotonic()+240
    while not (OUT/'keyboard-go').exists() and time.monotonic()<deadline:pump(0.1)
    record('physical_keyboard_retry','PASS' if any(t.strip()=='hello' for t in events) and any(t.strip()=='你好' for t in events) else 'FAIL',
           category='系统键盘',committed=events,builtin_keyboard=app.vkb is not None,
           backend=Gdk.Display.get_default().__gtype__.name,screenshot=capture('keyboard-retry'))
    app.entry.set_text('');app.btn_clear.grab_focus();pump(0.5)
    ask('abort_on_first_token','请详细介绍人工智能的发展历史，分五个时期展开，每个时期至少三句话，总字数500字。',stop=True)
    ask('response_after_actual_abort','现在请只回复：继续测试。',reset=False)
    ask('clear_after_actual_abort','中国的首都是哪里？只输出城市名称。')
    app.cli.close();pump(0.5);ok=app.cli.connect();pump(0.5)
    record('reconnect_transport','PASS' if ok else 'FAIL',category='连接恢复')
    ask('known_fact_after_reconnect','中国的首都是哪里？只输出城市名称。')
    # Deliberately long rendering fixtures, labelled as test data, not AI output.
    app._on_clear_clicked();pump(0.3)
    fixtures=[('long_chinese', '布局测试文字，不是模型回复。\n'+('中文长段落自动换行，检查右侧和最后一行不被截断。'*22)),
              ('long_ascii','Layout fixture, not model output.\n'+'https://example.test/'+'abcdef0123456789'*40),
              ('multiline_code','Layout fixture, not model output.\n'+'\n'.join('line_%d = %d'%(i,i) for i in range(40)))]
    for id_,text in fixtures:
        b=app._add_bubble('ai',text);pump(0.8)
        alloc=b.label.get_allocation();w,h=b.label.get_layout().get_pixel_size()
        record(id_,'PASS' if alloc.width>=w and alloc.height>=h else 'FAIL',category='文字显示',
               note='Fixed rendering fixture, not model generation',allocation=[alloc.width,alloc.height],
               layout=[w,h],screenshot=capture(id_))
    while not voice.is_ready() and time.monotonic()-START<300:pump(0.1)
    if voice.is_ready():
        import numpy as np
        text='你好，这是一段语音测试，一二三。'
        pcm,rate,cost=voice._engine.synthesize(text)
        mic=voice._mic;speaker=voice._speaker;frames=[];end_capture=threading.Event()
        def capture_audio():
            while not end_capture.is_set():
                frame=mic.read(512,timeout=0.3)
                if frame is not None:frames.append(frame)
        mic.open();thread=threading.Thread(target=capture_audio);thread.start();pump(0.4)
        playback={'error':None,'seconds':None}
        def play():
            try:playback['seconds']=speaker.play(pcm,rate)
            except Exception as e:playback['error']=repr(e)
        player=threading.Thread(target=play);player.start()
        deadline=time.monotonic()+25
        while player.is_alive() and time.monotonic()<deadline:pump(0.05)
        pump(0.8);end_capture.set();thread.join(timeout=3);mic.close();player.join(timeout=3)
        samples=np.concatenate(frames) if frames else np.zeros(0,dtype=np.float32)
        with wave.open(str(OUT/'acoustic-loopback.wav'),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
            f.writeframes((np.clip(samples,-1,1)*32767).astype(np.int16).tobytes())
        decoded,decode_seconds=voice._engine.transcribe(samples)
        record('speaker_microphone_acoustic_loop','PASS' if '语音测试' in decoded else 'FAIL',category='语音硬件',
               note='Actual speaker playback recorded through actual microphone; synthetic test utterance, not a human speech benchmark',
               expected=text,transcription=decoded,samples=len(samples),playback=playback,
               mic_device=mic.device,speaker_device=speaker.device)
        voice._engine.reset_vad();detected=False
        for i in range(0,len(samples)-511,512):
            voice._engine.vad.accept_waveform(samples[i:i+512])
            detected=detected or voice._engine.vad.is_speech_detected()
        record('vad_on_acoustic_recording','PASS' if detected else 'FAIL',category='语音检测',
               note='VAD detected speech in speaker-to-microphone recording',detected=detected)
    else:
        record('acoustic_loopback','FAIL',category='语音硬件',note='Speech engine unavailable')
    (OUT/'summary.json').write_text(json.dumps({'seconds':time.monotonic()-START,'records':RESULTS},ensure_ascii=False,indent=2))
    print('[COMPLETE]',flush=True)
except Exception as exc:
    import traceback;traceback.print_exc()
    record('followup_runner_exception','FAIL',category='测试框架',note=repr(exc))
finally:
    if app._stream_spk:app._stream_spk.stop();app._stream_spk.join(timeout=3)
    app.win.destroy();(OUT/'finished').write_text('done\n')
