#!/usr/bin/env python3
"""Device root supervisor: bound test duration and restore the official UI."""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import subprocess
import sys
import time

OUT = Path(sys.argv[1])
UI = Path('/data/rkllm-chat/ui/rkchat')
spec = importlib.util.spec_from_file_location('desktop_session', UI/'desktop_session.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
props = module.active_desktop()
env = module.desktop_environment(props)
user = pwd.getpwuid(int(props['User']))
OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(OUT, user.pw_uid, user.pw_gid)
env['RKCHAT_TEST_OUT'] = str(OUT)
service_pid = subprocess.check_output(['pgrep','-x','rkllm-service'],text=True).strip()
metadata = dict(start=time.time(), original_service_pid=service_pid, desktop_uid=user.pw_uid, desktop_user=user.pw_name)
(OUT/'supervisor.json').write_text(json.dumps(metadata))
# Stop only the official rkchat Python process. Never match an adb shell command
# containing the same string, and never stop the inference service.
for line in subprocess.check_output(['ps','-eo','pid,args'],text=True).splitlines():
    fields=line.split()
    if len(fields)<3: continue
    command=fields[1:]
    if Path(command[0]).name.startswith('python3') and any(
            arg=='rkchat.py' or arg==str(UI/'rkchat.py') for arg in command[1:]):
        os.kill(int(fields[0]), signal.SIGTERM)
time.sleep(3)
child = None
try:
    with (OUT/'runner.log').open('w') as log:
        child = subprocess.Popen(['runuser','-u',user.pw_name,'--',sys.executable,'-u','/tmp/rkchat-suite.py'],
                                 env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        (OUT/'runner.pid').write_text(str(child.pid))
        metadata['runner_exit']=child.wait(timeout=840)
except Exception as exc:
    metadata['supervisor_error']=repr(exc)
    if child and child.poll() is None:
        os.killpg(child.pid,signal.SIGTERM)
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired: os.killpg(child.pid,signal.SIGKILL)
finally:
    with (OUT/'restored-ui.log').open('w') as log:
        subprocess.Popen(['/bin/sh',str(UI/'run.sh'),'1280x800+0+0'],
                         stdout=log,stderr=subprocess.STDOUT,start_new_session=True,
                         stdin=subprocess.DEVNULL)
    metadata['end']=time.time()
    metadata['final_service_pid']=subprocess.check_output(['pgrep','-x','rkllm-service'],text=True).strip()
    (OUT/'supervisor.json').write_text(json.dumps(metadata,indent=2))
    (OUT/'supervisor-finished').write_text('done\n')
    print('TEST DONE; official UI restoration started',flush=True)
