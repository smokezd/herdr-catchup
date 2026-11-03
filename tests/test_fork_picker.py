import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('fork_picker', ROOT / 'bin/fork_picker.py')
picker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(picker)


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fork picker ')
        self.addCleanup(self.temp.cleanup)
        self.bin = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH']})
        self.env.start()
        self.addCleanup(self.env.stop)

    def executable(self, name, source):
        path = self.bin / name
        path.write_text('#!' + os.sys.executable + '\n' + source)
        path.chmod(0o755)
        return str(path)

    def test_codex_catalog_pagination_and_efforts(self):
        self.executable('codex', '''import sys,json
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='initialize': result={}
 elif r['method']=='initialized': continue
 else:
  assert r['params']['includeHidden'] is False
  cursor=r['params'].get('cursor')
  result={'data':[{'model':'small' if not cursor else 'large','displayName':'Test','supportedReasoningEfforts':[{'reasoningEffort':'low','description':'Fast'}]}], 'nextCursor':None if cursor else 'page2'}
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
''')
        models = picker.discover('codex')
        self.assertEqual([m['id'] for m in models], ['small', 'large'])
        self.assertEqual(models[0]['efforts'], ['low'])

    def test_claude_catalog(self):
        self.executable('claude', '''import sys,json
r=json.loads(sys.stdin.readline())
assert r['request']['subtype']=='initialize'
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':r['request_id'],'response':{'models':[{'value':'sonnet','displayName':'Sonnet','supportsEffort':True,'supportedEffortLevels':['low','high']}]}}}),flush=True)
''')
        self.assertEqual(picker.discover('claude'), [{'id':'sonnet','name':'Sonnet','efforts':['low','high']}])

    def test_timeout(self):
        self.executable('codex', 'import time\ntime.sleep(5)\n')
        with self.assertRaises(picker.DiscoveryError):
            picker.discover('codex', timeout=0.1)

    def test_malformed_response(self):
        self.executable('claude', "print('not json',flush=True)\n")
        with self.assertRaises(picker.DiscoveryError):
            picker.discover('claude')

    def test_empty_catalog(self):
        self.executable('claude', "import sys,json\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':r['request_id'],'response':{'models':[]}}}),flush=True)\n")
        with self.assertRaises(picker.DiscoveryError):
            picker.discover('claude')

    def test_missing_binary(self):
        with patch.dict(os.environ, {'PATH':str(self.bin)}):
            with self.assertRaises(picker.DiscoveryError):
                picker.discover('codex')

    def test_signal_exit_and_shim_cleanup(self):
        log = self.bin / 'launch.json'
        self.executable('codex', 'import os,json,sys,signal\nfrom pathlib import Path\nPath('+repr(str(log))+').write_text(json.dumps({"path":os.environ["PATH"],"args":sys.argv[1:]}))\nos.kill(os.getpid(),signal.SIGTERM)\n')
        self.executable('catchup', 'import shutil,os\nfrom pathlib import Path\nPath('+repr(str(self.bin/'shim'))+').write_text(shutil.which("codex"))\nos.execvp("codex",["codex","fork","original"])\n')
        before=os.environ['PATH']
        self.assertEqual(picker.launch('codex',['codex'],'small','low'),143)
        self.assertEqual(json.loads(log.read_text())['path'],before)
        self.assertFalse(Path((self.bin/'shim').read_text()).exists())

    def test_pane_closure_reaps_discovery(self):
        import signal
        import time
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                pidfile=self.bin/'pid'
                if pidfile.exists(): pidfile.unlink()
                self.executable('codex','import os,time\nfrom pathlib import Path\nPath('+repr(str(pidfile))+').write_text(str(os.getpid()))\ntime.sleep(60)\n')
                process=subprocess.Popen([os.sys.executable,str(ROOT/'bin/fork_picker.py'),'codex'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+5
                    while not pidfile.exists() and time.monotonic()<deadline: time.sleep(.01)
                    self.assertTrue(pidfile.exists())
                    pid=int(pidfile.read_text())
                    process.send_signal(sig)
                    process.communicate(timeout=5)
                    self.assertEqual(process.returncode,128+sig)
                    with self.assertRaises(ProcessLookupError): os.kill(pid,0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_picker_retry_and_cancel(self):
        with patch('builtins.input', side_effect=['bad','9','0']), patch('sys.stdout', new=io.StringIO()):
            self.assertIsNone(picker.choose('Model', ['Small']))

    def test_model_effort_and_catchup_arguments(self):
        for agent, expected in [('codex',['-c','model_reasoning_effort=low']), ('claude',['--effort','low'])]:
            with self.subTest(agent=agent):
                log = self.bin / 'args.json'
                self.executable(agent, 'import sys,json\nfrom pathlib import Path\nPath('+repr(str(log))+').write_text(json.dumps(sys.argv[1:]))\nsys.exit(7)\n')
                self.executable('catchup', 'import os,sys\nos.execvp('+repr(agent)+',['+repr(agent)+',"fork","session with spaces"]+sys.argv[1:])\n')
                before = os.environ['PATH']
                rc = picker.launch(agent, ['codex','--id','source id','--dir','origin path'], 'small', 'low')
                self.assertEqual(rc, 7)
                args = json.loads(log.read_text())
                self.assertEqual(args, ['fork','session with spaces','fork','codex','--id','source id','--dir','origin path','--model','small'] + expected)
                self.assertEqual(os.environ['PATH'], before)

    def test_discovery_failure_offers_unchanged_fork(self):
        with patch.object(picker, 'discover', side_effect=picker.DiscoveryError('offline')), patch.object(picker, 'launch', return_value=0) as launch, patch('builtins.input', return_value='1'), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(picker.main(['codex','--id','source']), 0)
            launch.assert_called_once_with('codex', ['codex','--id','source'])

    def test_no_effort_metadata_skips_effort_prompt(self):
        with patch.object(picker, 'discover', return_value=[{'id':'small','name':'Small','efforts':[]}]), patch.object(picker, 'launch', return_value=0) as launch, patch('builtins.input', return_value='2'), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(picker.main(['codex']), 0)
            launch.assert_called_once_with('codex',['codex'],'small',None)

    def test_cancel_launches_nothing(self):
        with patch.object(picker, 'discover', return_value=[]), patch.object(picker, 'launch') as launch, patch('builtins.input', return_value='0'), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(picker.main(['claude']), 0)
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
