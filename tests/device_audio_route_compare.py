#!/usr/bin/env python3
"""Compare shared-codec capture against independent USB microphone capture."""
import json, os, re, subprocess, sys, threading, time, wave
from pathlib import Path
import numpy as np
OUT=Path(os.environ['RKCHAT_TEST_OUT']);OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,'/data/voice-vision')
import talk
engine=talk.VoiceEngine();engine.load()
mic=talk.Mic();speaker=talk.Speaker()
controls=('ADCL PGA','ADCR PGA')

def gain(name):
    s=subprocess.check_output(['amixer','-c','0','sget',name],text=True)
    m=re.search(r'Mono:\s+(?:Capture\s+|Playback\s+)?(\d+)\s+\[',s)
    if not m:raise RuntimeError('Cannot read gain '+name)
    return int(m.group(1))

def set_gains(values):
    for name,value in zip(controls,values):
        subprocess.run(['amixer','-q','-c','0','sset',name,str(value)],check=True)


def norm(s):return ''.join(c.lower() for c in s if c.isalnum())
def distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        nr=[i]
        for j,y in enumerate(b,1):nr.append(min(nr[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=nr
    return row[-1]

phrases=['你好，这是一段语音测试，一二三。','今天天气很好，我们一起去散步。','请告诉我，桌面上的杯子是什么颜色。']
# Synthesize once so the acoustic signal is exactly the same in both trials.
waveforms=[engine.synthesize(s) for s in phrases]
original=[gain(c) for c in controls]
candidate=original  # No mixer changes in the routing comparison.
records=[]
try:
    for label,device in [('shared_codec','hw:0,0'),('usb_microphone','plughw:CARD=U1080P,DEV=0')]:
        mic=talk.Mic(device=device)
        values=original
        time.sleep(0.3)
        for idx,(phrase,(pcm,rate,_)) in enumerate(zip(phrases,waveforms)):
            frames=[];stop=threading.Event()
            def record():
                while not stop.is_set():
                    frame=mic.read(512,timeout=0.3)
                    if frame is not None:frames.append(frame)
            mic.open();thread=threading.Thread(target=record);thread.start();time.sleep(0.4)
            error=None
            try:play_seconds=speaker.play(pcm,rate)
            except Exception as exc:error=repr(exc);play_seconds=0
            time.sleep(0.7);stop.set();thread.join(timeout=3);mic.close()
            samples=np.concatenate(frames) if frames else np.zeros(0,dtype=np.float32)
            if len(samples):text,seconds=engine.transcribe(samples)
            else:text,seconds='',0
            reference=norm(phrase);actual=norm(text)
            stats=dict(phase=label,id=idx,device=device,gains=values,reference=phrase,transcription=text,
                       samples=len(samples),rms=float(np.sqrt(np.mean(samples*samples))) if len(samples) else 0,
                       peak=float(np.abs(samples).max()) if len(samples) else 0,
                       clipped_fraction=float((np.abs(samples)>.999).mean()) if len(samples) else 0,
                       errors=distance(reference,actual),reference_chars=len(reference),
                       playback_seconds=play_seconds,playback_error=error,asr_seconds=seconds)
            records.append(stats)
            with wave.open(str(OUT/('%s-%d.wav'%(label,idx))),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
                f.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
            with (OUT/'results.jsonl').open('a') as f:f.write(json.dumps(stats,ensure_ascii=False)+'\n')
            print('[AUDIO] '+json.dumps(stats,ensure_ascii=False),flush=True)
finally:
    mic.close();set_gains(original)
    (OUT/'restored-gains.json').write_text(json.dumps({'original':original,'restored':[gain(c) for c in controls]}))
(OUT/'summary.json').write_text(json.dumps({'records':records},ensure_ascii=False,indent=2))
print('[COMPLETE]',flush=True)
