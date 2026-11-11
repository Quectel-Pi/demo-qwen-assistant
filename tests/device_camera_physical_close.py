#!/usr/bin/env python3
"""Wait for a root-side kernel touch while the real UI is answering a camera question."""
import json,os,sys,time
from pathlib import Path
OUT=Path(os.environ['RKCHAT_TEST_OUT']);OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,'/data/rkllm-chat/ui/rkchat')
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,GLib
import rkchat
app=rkchat.ChatApp(fullscreen=True,keyboard='system');app.auto_speak=False;app.cli.connect()
clicks=[];done=[]
original=app._on_done
app.cli.on_done=lambda info:(done.append(info),original(info))
app.btn_cam.connect('clicked',lambda *_:clicks.append({'busy':app.cli.busy,'mode_after_click':app.cam_mode}))
def pump(s):
 end=time.monotonic()+s
 while time.monotonic()<end:
  ctx=GLib.MainContext.default()
  for _ in range(100):
   if not ctx.pending():break
   ctx.iteration(False)
  time.sleep(0.01)
try:
 app._on_cam_clicked();pump(2)
 app.entry.set_text('请分五点描述眼前画面，每一点都解释清楚。');app._on_send_clicked()
 end=time.monotonic()+5
 while not app.cli.busy and time.monotonic()<end:pump(0.02)
 pump(0.2)
 x,y=app.btn_cam.translate_coordinates(app.win,0,0)
 position=dict(x=x+app.btn_cam.get_allocated_width()//2,y=y+app.btn_cam.get_allocated_height()//2,
               busy=app.cli.busy,button_enabled=app.btn_cam.get_sensitive())
 (OUT/'physical-close-ready.json').write_text(json.dumps(position))
 print('[READY]',json.dumps(position),flush=True)
 end=time.monotonic()+15
 while not clicks and time.monotonic()<end:pump(0.03)
 end=time.monotonic()+60
 while not done and time.monotonic()<end:pump(0.05)
 pump(0.4)
 result=dict(id='kernel_touch_closes_camera_during_question',
             status='PASS' if clicks and clicks[0]['busy'] and not app.cam_mode and not app.cam.running else 'FAIL',
             position=position,click_events=clicks,cam_mode=app.cam_mode,camera_running=app.cam.running,
             preview_visible=app.preview_revealer.get_visible(),answer_finished=bool(done))
 (OUT/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print('[RESULT]',result,flush=True)
finally:
 app._stop_reading();app.cam.stop();app.win.destroy();(OUT/'finished').write_text('done\n')
