#!/usr/bin/env python3
"""Real GTK/speech/socket regression for engineering fixes; does not grade LLM answers."""
import json, os, socket, sys, threading, time
from pathlib import Path
OUT=Path(os.environ['RKCHAT_TEST_OUT']);OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,'/data/rkllm-chat/ui/rkchat')
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,GLib,Gdk
import rkchat,voice,client
START=time.monotonic();RESULTS=[];DONE=None
app=rkchat.ChatApp(fullscreen=True,keyboard='system');app.cli.connect()
original_done=app._on_done

def done(info):
    global DONE
    answer=app.cur_bubble.get_text() if app.cur_bubble else ''
    DONE=(dict(info),answer);original_done(info)
app.cli.on_done=done

def pump(s):
    until=time.monotonic()+s
    while time.monotonic()<until:
        ctx=GLib.MainContext.default()
        for _ in range(100):
            if not ctx.pending():break
            ctx.iteration(False)
        time.sleep(0.01)

def record(id_,ok,**kw):
    r=dict(id=id_,status='PASS' if ok else 'FAIL',seconds=round(time.monotonic()-START,2),**kw)
    RESULTS.append(r)
    with (OUT/'results.jsonl').open('a') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    print('[RESULT]',id_,r['status'],flush=True)

def wait_stream(sp,limit=60):
    deadline=time.monotonic()+limit
    while sp.busy and time.monotonic()<deadline:pump(0.05)
    return not sp.busy

def ask(q,stop=False):
    global DONE
    app._on_clear_clicked();pump(0.2);DONE=None;t=time.monotonic();stopped=False
    app.entry.set_text(q);app._on_send_clicked()
    while DONE is None and time.monotonic()-t<50:
        pump(0.02)
        if stop and not stopped and app.cur_bubble and app.cur_bubble.get_text():
            app._interrupt('工程回归：停止');stopped=True
    return DONE,round((time.monotonic()-t)*1000),stopped

try:
    record('native_ui',app.vkb is None and 'Wayland' in Gdk.Display.get_default().__gtype__.name,
           backend=Gdk.Display.get_default().__gtype__.name,builtin_keyboard=app.vkb is not None,uid=os.getuid())
    cold=not voice.is_ready();app.auto_speak=True
    response,ms,_=ask('请只说：你好，这是首句朗读测试。')
    sp=app._stream_spk
    completed=wait_stream(sp) if sp else False
    record('cold_first_sentence',cold and bool(response) and completed and sp._spoken_any,
           engine_was_loading=cold,spoken=sp._spoken_any if sp else False,wall_ms=ms)
    if not voice.is_ready():raise RuntimeError('Speech preload failed')
    record('independent_mic_route',voice.can_listen_while_playing(),mic=voice._mic.device,speaker=voice._speaker.device)
    # Instrument the real engine; no model or inference result is replaced.
    old_synth,old_play=voice._engine.synthesize,voice._speaker.play
    calls=[];plays=[];active=[0];peak=[0];lock=threading.Lock()
    def synth(text):
        with lock:active[0]+=1;peak[0]=max(peak[0],active[0]);calls.append(text)
        try:return old_synth(text)
        finally:
            with lock:active[0]-=1
    def play(pcm,rate):
        result=old_play(pcm,rate);plays.append(len(pcm));return result
    voice._engine.synthesize=synth;voice._speaker.play=play
    before=len(calls);app._on_speak_clicked()  # turn default reading off
    response,ms,_=ask('请只说：你好。');pump(0.5)
    record('mute_suppresses_audio',not app.auto_speak and len(calls)==before and app._stream_spk is None,
           new_tts_calls=len(calls)-before)
    app._on_speak_clicked()
    app._start_stream_speaker();first=app._stream_spk
    first.feed('这是一次长一点的朗读，我们马上停止并进入下一轮。');pump(0.05)
    app._start_stream_speaker();second=app._stream_spk;second.feed('现在开始新的一轮。');second.finish()
    completed=wait_stream(second);first.join(timeout=3)
    record('rapid_round_no_tts_overlap',completed and second._spoken_any and peak[0]==1,
           peak_synthesis_calls=peak[0],new_round_spoken=second._spoken_any)
    # Burst beyond the old eight-sentence queue limit.
    before_synth,before_play=len(calls),len(plays)
    burst=voice.StreamSpeaker();
    for i in range(16):burst.feed('测试%d。'%i)
    burst.start();t=time.monotonic();burst.finish();finish_ms=(time.monotonic()-t)*1000
    completed=wait_stream(burst,80)
    record('burst_no_drops_and_nonblocking_finish',completed and len(calls)-before_synth==16 and len(plays)-before_play==16 and finish_ms<100,
           fed=16,synthesized=len(calls)-before_synth,played=len(plays)-before_play,finish_ms=finish_ms)
    # A backend playback error must be visible, not counted as successful audio.
    class BrokenOutput:
        device='hw:0,0'
        def play(self,*args):raise OSError('injected output failure')
    states=[];saved=voice._speaker
    try:
        voice._speaker=BrokenOutput()
        broken=voice.StreamSpeaker(on_state=states.append);broken.start();broken.feed('测试。');broken.finish()
        completed=wait_stream(broken)
        record('playback_failure_not_silent',completed and bool(states) and not broken._spoken_any,states=states)
    finally:voice._speaker=saved
    app._start_stream_speaker();failed=app._stream_spk;failed.feed('这一句应当停止。');app._speak_buf='旧内容'
    app._on_error('回归测试模拟请求失败');pump(0.2);failed.join(timeout=3)
    record('request_error_cleans_audio',failed._stop.is_set() and app._stream_spk is None and app._speak_buf=='')
    # Only the speech worker initiates this update; GTK handles it on its thread.
    observed=[]
    signal_id=app.lbl_voice.connect('notify::label',lambda *_: observed.append(threading.get_ident()))
    worker=threading.Thread(target=app._voice_state,args=('回归测试：语音就绪',));worker.start();worker.join();pump(0.2)
    app.lbl_voice.disconnect(signal_id)
    record('worker_status_runs_on_gtk_thread',observed and set(observed)=={threading.get_ident()},thread_ids=observed)
    # Cancel an actual microphone acquisition before it can submit partial words.
    io=voice.VoiceIO(max_seconds=5);dictation=[]
    listener=threading.Thread(target=lambda:dictation.append(io.listen_once()))
    listener.start();pump(0.8);io.stop_listening();listener.join(timeout=3)
    record('cancel_mic_discards_partial_input',not listener.is_alive() and dictation==[None])
    app.auto_speak=False
    response,ms,stopped=ask('详细介绍人工智能的发展历史，分五个时期，每个时期至少三句话。',stop=True)
    record('actual_abort_recovers',bool(response) and stopped and bool(response[0].get('aborted')),done=response[0] if response else None)
    response,ms,_=ask('中国的首都是哪里？只输出城市名称。')
    record('request_after_abort_completes',bool(response),answer=response[1] if response else '',wall_ms=ms)
    success=0
    for _ in range(20):
        app.cli.close();pump(0.02)
        if app.cli.connect() and app.cli.ping():success+=1
    pump(0.3)
    receiver_count=sum('_recv_loop' in t.name for t in threading.enumerate())
    record('twenty_reconnects_no_old_receivers',success==20 and receiver_count==1,successful=success,active_receivers=receiver_count)
    response,ms,_=ask('请只说你好。')
    record('ui_after_reconnect_completes',bool(response),wall_ms=ms)
    voice._engine.synthesize=old_synth;voice._speaker.play=old_play
    # Confirm the new capture warmup returns only after audio is arriving.
    io=voice.VoiceIO();voice._mic.open();t=time.monotonic()
    try:ready=io._wait_for_capture()
    finally:voice._mic.close()
    record('real_mic_pcm_ready',ready,ready_ms=round((time.monotonic()-t)*1000))
    app.auto_speak=True;app._on_cam_clicked();pump(2)
    response,ms,_=ask('只用一句中文描述画面中的主要物体。')
    camera_speaker=app._stream_spk
    played=wait_stream(camera_speaker) and camera_speaker._spoken_any if camera_speaker else False
    record('camera_plus_reading',bool(response) and app.cam.running and played,
           camera_running=app.cam.running,spoken=played,wall_ms=ms)
    app._on_cam_clicked();pump(0.3)
    (OUT/'summary.json').write_text(json.dumps({'records':RESULTS},ensure_ascii=False,indent=2))
    print('[COMPLETE]',flush=True)
except Exception as exc:
    import traceback;traceback.print_exc();record('runtime_exception',False,error=repr(exc))
finally:
    app._stop_reading()
    if app.cam_mode:app._on_cam_clicked()
    app.win.destroy();(OUT/'finished').write_text('done\n')
