#!/usr/bin/env python3
"""Real GTK camera closing/new-frame/cache tests. Synthetic color images are labelled fixtures."""
import hashlib,json,os,signal,socket,sys,time
from pathlib import Path
OUT=Path(os.environ['RKCHAT_TEST_OUT']);OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,'/data/rkllm-chat/ui/rkchat')
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,GLib,Gdk
import numpy as np
import camera,rkchat
app=rkchat.ChatApp(fullscreen=True,keyboard='system');app.cli.connect()
default_reading=app.auto_speak;app.auto_speak=False
START=time.monotonic();RESULTS=[];DONE=None;ERRORS=[];REQUESTS=[];IMAGES=[]
old_done,old_error,old_ask,old_image=app._on_done,app._on_error,app.cli.ask,app._on_image

def done(info):
    global DONE
    answer=app.cur_bubble.get_text() if app.cur_bubble else ''
    DONE=(dict(info),answer);old_done(info)

def error(msg):ERRORS.append(str(msg));old_error(msg)
def ask(prompt,image=None,keep_history=True):
    digest=hashlib.sha256(Path(image).read_bytes()).hexdigest() if image and Path(image).exists() else None
    REQUESTS.append(dict(prompt=prompt,image=image,file_hash=digest))
    return old_ask(prompt,image,keep_history)
def on_image(info):IMAGES.append(dict(info));old_image(info)
app.cli.on_done=done;app.cli.on_error=error;app.cli.ask=ask;app.cli.on_image=on_image

def pump(s):
    until=time.monotonic()+s
    while time.monotonic()<until:
        ctx=GLib.MainContext.default()
        for _ in range(100):
            if not ctx.pending():break
            ctx.iteration(False)
        time.sleep(0.01)

def record(id_,ok,**kw):
    row=dict(id=id_,status='PASS' if ok else 'FAIL',seconds=round(time.monotonic()-START,2),**kw)
    RESULTS.append(row)
    with (OUT/'results.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    print('[RESULT]',id_,row['status'],flush=True)

def until(fn,timeout=60):
    end=time.monotonic()+timeout
    while not fn() and time.monotonic()<end:pump(0.03)
    return bool(fn())

def capture(name):
    import subprocess
    dest=OUT/(name+'.png')
    p=subprocess.run([sys.executable,'/tmp/rkchat-screenshot-wayland.py',str(dest)],capture_output=True,timeout=20)
    return dest.name if p.returncode==0 and dest.exists() else None

def open_camera():
    until(lambda:not app._cam_closing,5)
    if not app.cam_mode:app._on_cam_clicked()
    return until(lambda:app.cam.running and app.cam.frame_age is not None and app.cam.frame_age<0.5,5)

def close_camera():
    if app.cam_mode:app._on_cam_clicked()
    return until(lambda:not app._cam_closing and not app.cam.running,5)

def send(q,image=None):
    global DONE
    DONE=None;ERRORS.clear()
    if image:app._send_round(q,image)
    else:app.entry.set_text(q);app._on_send_clicked()

def wait_answer():
    until(lambda:DONE is not None or bool(ERRORS),60)
    return DONE

def rpc(request):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(8);s.connect('/run/rkllm-chat.sock')
        s.sendall(json.dumps(request).encode()+b'\n')
        with s.makefile('rb') as f:return json.loads(f.readline())

try:
    record('native_system_ui',app.vkb is None and 'Wayland' in Gdk.Display.get_default().__gtype__.name)
    ready=open_camera();record('live_camera_ready',ready)
    if not ready:raise RuntimeError('No fresh camera frames')
    # Close immediately while acquisition is pending: no question may be sent.
    before=len(REQUESTS);send('这是什么？');pending=app._capture_pending
    t=time.monotonic();app._on_cam_clicked();close_ms=(time.monotonic()-t)*1000
    until(lambda:not app._cam_closing,5);pump(0.3)
    record('close_during_capture_cancels_request',pending and not app.cam_mode and not app._capture_pending and len(REQUESTS)==before,
           close_ms=close_ms,new_requests=len(REQUESTS)-before)
    ready=open_camera();old_seq=app.cam.latest()[1]
    send('用一句话描述现在的画面。')
    busy=until(lambda:app.cli.busy or bool(ERRORS),5)
    enabled=app.btn_cam.get_sensitive()
    t=time.monotonic();app._on_cam_clicked();close_ms=(time.monotonic()-t)*1000
    invisible=not app.cam_mode and not app.preview_revealer.get_visible()
    finished=wait_answer();until(lambda:not app._cam_closing,5)
    first_snapshot=dict(app._last_snapshot or {});first_request=dict(REQUESTS[-1])
    record('close_camera_while_answering',busy and enabled and invisible and close_ms<200 and not app.cam.running,
           button_enabled=enabled,immediate_preview_hidden=invisible,close_ms=close_ms,
           screenshot=capture('closed-during-answer'))
    record('request_uses_newer_frame',bool(finished) and first_snapshot.get('seq',-1)>old_seq and first_snapshot.get('age_ms',9999)<1000,
           snapshot=first_snapshot,sequence_at_click=old_seq,image_event=IMAGES[-1] if IMAGES else None,request=first_request)
    # Text-only follow-up: do not accidentally grab a new frame.
    before=len(IMAGES);send('刚才那幅画面的背景是什么？');answer=wait_answer()
    record('text_followup_keeps_latest_scene',bool(answer) and REQUESTS[-1]['image'] is None and len(IMAGES)==before,
           answer=answer[1] if answer else '',new_images=len(IMAGES)-before)
    ready=open_camera();send('现在看到了什么？用一句话回答。');answer=wait_answer()
    second_snapshot=dict(app._last_snapshot or {});second_request=dict(REQUESTS[-1])
    record('reopen_gets_new_session_and_unique_file',ready and bool(answer) and second_snapshot.get('epoch',0)>first_snapshot.get('epoch',0)
           and second_request['image']!=first_request['image'],first=first_snapshot,second=second_snapshot,
           different_pixels=second_request['file_hash']!=first_request['file_hash'],request=second_request)
    close_camera()
    # Explicit fixture test: same prompt, different pixels, keep_history=True.
    # This proves encoder identity and KV reset independently of model accuracy.
    colors=[('red1',(255,0,0),'红'),('blue',(0,0,255),'蓝'),('red2',(255,0,0),'红')]
    color_rows=[]
    for name,color,expected in colors:
        path=OUT/(name+'.bmp');camera.write_bmp(str(path),np.full((448,448,3),color,dtype=np.uint8))
        before=len(IMAGES);send('这张图片的主要颜色是什么？只回答颜色。',str(path));answer=wait_answer()
        event=IMAGES[-1] if len(IMAGES)>before else {}
        color_rows.append(dict(fixture=name,expected=expected,answer=answer[1] if answer else '',event=event))
    events=[r['event'] for r in color_rows]
    hashes=[e.get('embedding_hash') for e in events]
    record('new_image_resets_old_kv',all(e.get('kv_reset')==1 and e.get('kv_after')==0 for e in events)
           and events[1].get('kv_before',0)>0 and events[2].get('kv_before',0)>0,fixtures=color_rows)
    record('different_pixels_reach_encoder',None not in hashes and hashes[0]!=hashes[1] and hashes[1]!=hashes[2],hashes=hashes,
           identical_red_embedding=hashes[0]==hashes[2])
    record('color_answer_sample',all(r['expected'] in r['answer'] for r in color_rows),fixtures=color_rows,
           note='Model sample only, not the proof of frame freshness')
    # No image or invalid image must not fall through to a cached scene answer.
    for name,request in [('bad_image_path',{'cmd':'ask','prompt':'<image>看到了什么？','image':'/tmp/rkchat-no-such-image.bmp'}),
                         ('missing_image_payload',{'cmd':'ask','prompt':'<image>看到了什么？'})]:
        response=rpc(request);record(name,response.get('ev')=='error',response=response)
    # Freeze this UI's own ffmpeg process to simulate a stalled USB camera.
    ready=open_camera();proc=app.cam._proc;proc.send_signal(signal.SIGSTOP);pump(1.4)
    before=len(REQUESTS);send('现在的新画面是什么？')
    until(lambda:not app._capture_pending,4);pump(0.2)
    error_shown=any(isinstance(w,rkchat.Bubble) and w.role=='error' and '未取得新画面' in w.get_text()
                    for w in app.chat_box.get_children())
    record('stalled_camera_is_not_sent_as_old_image',len(REQUESTS)==before and app.cur_bubble is None and error_shown,
           new_requests=len(REQUESTS)-before,errors_shown=error_shown,screenshot=capture('stalled-camera'))
    proc.send_signal(signal.SIGCONT);until(lambda:app.cam.frame_age is not None and app.cam.frame_age<0.5,3)
    # A busy round cannot be replaced by a voice callback or another submit.
    app.auto_speak=True
    send('只用一句话描述画面。');until(lambda:app.cli.busy,5)
    before=len(REQUESTS);app._submit('此问题不应覆盖正在进行的那一轮。')
    record('busy_round_not_overwritten',len(REQUESTS)==before,new_requests=len(REQUESTS)-before)
    wait_answer();close_camera()
    speaker=app._stream_spk
    if speaker:until(lambda:not speaker.busy,30)
    record('reading_still_enabled',default_reading and speaker is not None and speaker._spoken_any,
           spoken=speaker._spoken_any if speaker else False)
    (OUT/'summary.json').write_text(json.dumps({'records':RESULTS},ensure_ascii=False,indent=2))
    print('[COMPLETE]',flush=True)
except Exception as exc:
    import traceback;traceback.print_exc();record('runner_exception',False,error=repr(exc))
finally:
    if app.cam._proc is not None:
        try:app.cam._proc.send_signal(signal.SIGCONT)
        except OSError:pass
    app._stop_reading();app.cam.stop();app.win.destroy();(OUT/'finished').write_text('done\n')
