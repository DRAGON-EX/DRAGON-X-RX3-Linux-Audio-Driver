# SPDX-License-Identifier: GPL-3.0-or-later
"""Use a separate PipeWire socket and configuration; never touch desktop audio."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
(root/'reports').mkdir(exist_ok=True)
helper=root/'packaging/rx3_audio_setup.py';assets=root/'packaging'
with tempfile.TemporaryDirectory(prefix='rx3-pw-',dir='/tmp') as folder:
 base=Path(folder);run=base/'run';run.mkdir(mode=0o700)
 config=base/'pipewire.conf'
 text=Path('/usr/share/pipewire/pipewire.conf').read_text()
 text+='\ncontext.exec = [ { path = '+json.dumps(sys.executable)+' args = '+json.dumps(str(helper)+' --startup --data-dir '+str(assets))+' } ]\n'
 config.write_text(text)
 env=dict(os.environ,XDG_RUNTIME_DIR=str(run),PIPEWIRE_RUNTIME_DIR=str(run),XDG_CONFIG_HOME=str(base/'config'),XDG_STATE_HOME=str(base/'state'),ALSA_PLUGIN_DIR=str(root))
 env.pop('PIPEWIRE_CONFIG_DIR',None)
 results=[]
 def call(*args):
  p=subprocess.run([sys.executable,str(helper),'--data-dir',str(assets),*args],env=env,text=True,capture_output=True,timeout=20)
  if p.returncode:raise RuntimeError(p.stderr or p.stdout)
  return json.loads(p.stdout)
 def start():
  p=subprocess.Popen(['pipewire','-c',str(config)],env=env,stdout=log,stderr=subprocess.STDOUT)
  for _ in range(50):
   if p.poll() is not None:raise RuntimeError('private PipeWire failed to start')
   if (run/'pipewire-0').exists():
    result=call('--status')
    if len(result['outputs'])==1:return p,result
   time.sleep(.1)
  p.terminate();raise RuntimeError('automatic startup did not create RX3')
 with (root/'reports/private_pipewire.log').open('w') as log:
  p=None
  try:
   p,info=start();assert info['outputs'][0]['buffer_size']==512,info
   results.append({'startup':info})
   assert call('--startup')['changed'] is False
   for size in (2048,1024,256,128,64,512):
    result=call('--set-buffer',str(size));assert result['buffer_size']==size
    assert call('--status')['outputs'][0]['managed'] is True
    results.append(result)
   call('--backend','asio','--set-buffer','128')
   call('--backend','wasapi','--set-buffer','256')
   assert call('--backend','asio','--status')['saved_buffer_size']==128
   assert call('--backend','wasapi','--status')['saved_buffer_size']==256
   native=call('--backend','linux','--set-buffer','128')
   assert native['pipewire_used'] is False
   info=call('--backend','wasapi','--status')
   assert info['active_backend']=='wasapi' and info['outputs'][0]['buffer_size']==256
   results.append({'native_preference_preserves_pipewire':native})
   p.terminate();p.wait(timeout=5);p=None
   p,info=start();assert info['outputs'][0]['buffer_size']==256,info
   results.append({'restart_saved_choice':info})
   call('--remove');assert not call('--status')['outputs']
   results.append({'removal':'passed'})
  finally:
   if p is not None:p.terminate();p.wait(timeout=5)
 (root/'reports/private_pipewire.json').write_text(json.dumps({'status':'PASS','desktop_audio_modified':False,'hardware_playback':False,'checks':results},indent=2)+'\n')
 print('PASS: automatic startup, all six presets, idempotence, persisted restart, owned removal on private PipeWire socket')
