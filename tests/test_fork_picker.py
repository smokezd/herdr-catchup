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
        self.env = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH'], 'HERDR_PLUGIN_STATE_DIR':str(self.bin/'state')})
        self.env.start()
        parent = patch.object(picker, 'parent_settings', return_value={})
        parent.start()
        self.real_fork_mode = picker.fork_mode
        mode = patch.object(picker, 'fork_mode', return_value=1)
        mode.start()
        self.addCleanup(mode.stop)
        self.addCleanup(parent.stop)
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

    def test_purpose_always_defaults_to_side_questions(self):
        with patch.object(picker,'choose',return_value=0) as choose:
            self.assertEqual(self.real_fork_mode(),0)
            self.assertEqual(choose.call_args.kwargs['default'],0)
            self.assertIn('Side questions',choose.call_args.args[1][0])
            self.assertEqual(choose.call_args.args[1][1],'Continue parent work')

    def test_side_question_instructions_do_not_submit_user_prompt(self):
        for agent in ['codex','claude']:
            log=self.bin/'prompt.json'
            self.executable(agent,'import sys,json\nfrom pathlib import Path\nPath('+repr(str(log))+').write_text(json.dumps(sys.argv[1:]))\n')
            self.executable('catchup','import os\nos.execvp('+repr(agent)+',['+repr(agent)+',"fork","parent id"])\n')
            with patch.object(picker,'codex_developer_instructions',return_value='Existing developer rules'):
                for effort in (None,'low'):
                    self.assertEqual(picker.launch(agent,[agent],effort=effort,prompt=picker.SIDE_QUESTION_PROMPT),0)
                    args=json.loads(log.read_text())
                    flags=[] if effort is None else (['-c','model_reasoning_effort=low'] if agent=='codex' else ['--effort','low'])
                    if agent=='codex':
                        self.assertEqual(args[:3+len(flags)],['fork','parent id']+flags+['-c'])
                        value=args[-1].split('=',1)
                        self.assertEqual(value[0],'developer_instructions')
                        self.assertEqual(json.loads(value[1]),'Existing developer rules\n\n'+picker.SIDE_QUESTION_PROMPT)
                        self.assertNotIn(picker.SIDE_QUESTION_PROMPT,args)
                    else:
                        self.assertEqual(args,['fork','parent id']+flags+['--append-system-prompt',picker.SIDE_QUESTION_PROMPT,'--system-prompt-snapshot','off'])

    def test_codex_config_read_preserves_effective_developer_instructions(self):
        with patch.object(picker,'CatalogProcess') as process:
            client=process.return_value
            client.receive.side_effect=[{'id':1,'result':{}},{'id':2,'result':{'config':{'developer_instructions':'Existing rules'}}}]
            self.assertEqual(picker.codex_developer_instructions(),'Existing rules')
            client.close.assert_called_once()
            self.assertEqual(client.send.call_args.args[0]['method'],'config/read')

    def test_config_read_failure_does_not_launch_a_fork(self):
        with patch.object(picker,'codex_developer_instructions',side_effect=picker.DiscoveryError('offline')), patch.object(picker.shutil,'which',return_value='/path/to/codex'), patch.object(picker.subprocess,'Popen') as process:
            with self.assertRaises(picker.DiscoveryError):
                picker.launch('codex',['codex'],prompt=picker.SIDE_QUESTION_PROMPT)
            process.assert_not_called()

    def test_side_question_mode_passes_prompt(self):
        with patch.object(picker,'fork_mode',return_value=0), patch.object(picker,'discover',return_value=[]), patch.object(picker,'launch',return_value=0) as launch, patch('builtins.input',return_value=''), patch('sys.stdout',new=io.StringIO()):
            picker.main(['codex'])
            launch.assert_called_once_with('codex',['codex'],None,None,prompt=picker.SIDE_QUESTION_PROMPT)

    def test_cancelling_purpose_does_not_save_or_launch(self):
        with patch.object(picker,'fork_mode',return_value=None), patch.object(picker,'discover',return_value=[]), patch.object(picker,'launch') as launch, patch.object(picker,'save_choice') as save, patch('builtins.input',return_value=''), patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(picker.main(['codex']),0)
            launch.assert_not_called()
            save.assert_not_called()

    def test_parent_model_and_effort_are_explicit(self):
        models=[{'id':'parent','name':'Parent','efforts':['low','high']}]
        with patch.object(picker,'parent_settings',return_value={'model':'parent','effort':'high'}), patch.object(picker,'discover',return_value=models), patch.object(picker,'launch',return_value=0) as launch, patch('builtins.input',side_effect=['','']), patch('sys.stdout',new=io.StringIO()):
            picker.main(['codex'])
            launch.assert_called_with('codex',['codex'],'parent','high')
        self.assertEqual(picker.load_choice('codex'),{'model':None,'effort':None})

    def test_parent_model_with_different_effort(self):
        with patch.object(picker,'parent_settings',return_value={'model':'parent','effort':'high'}), patch.object(picker,'discover',return_value=[{'id':'parent','name':'Parent','efforts':['low','high']}]), patch.object(picker,'launch',return_value=0) as launch, patch('builtins.input',side_effect=['1','2']), patch('sys.stdout',new=io.StringIO()):
            picker.main(['codex'])
            launch.assert_called_with('codex',['codex'],'parent','low')

    def test_explicit_model_can_inherit_parent_effort(self):
        with patch.object(picker,'parent_settings',return_value={'model':'parent','effort':'high'}), patch.object(picker,'discover',return_value=[{'id':'small','name':'Small','efforts':['low','high']}]), patch.object(picker,'launch',return_value=0) as launch, patch('builtins.input',side_effect=['2','1']), patch('sys.stdout',new=io.StringIO()):
            picker.main(['claude'])
            launch.assert_called_with('claude',['claude'],'small','high')

    def test_reads_latest_parent_settings_from_logs(self):
        path=self.bin/'session.jsonl'
        path.write_text('\n'.join(json.dumps(row) for row in [
            {'type':'turn_context','payload':{'model':'old','effort':'low'}},
            {'type':'turn_context','payload':{'model':'new','effort':'high'}}])+'\npartial')
        self.assertEqual(picker.read_parent_log(path,'codex'),{'model':'new','effort':'high'})
        path.write_text('\n'.join(json.dumps(row) for row in [
            {'type':'assistant','requestedModel':'opus','perTurnEffort':'high'},
            {'type':'assistant','isSidechain':True,'requestedModel':'small','effort':'low'}]))
        self.assertEqual(picker.read_parent_log(path,'claude'),{'model':'opus','effort':'high'})

    def test_incompatible_parent_effort_requires_supported_choice(self):
        with patch.object(picker,'parent_settings',return_value={'model':'parent','effort':'high'}), patch.object(picker,'discover',return_value=[{'id':'small','name':'Small','efforts':['low']}]), patch.object(picker,'launch',return_value=0) as launch, patch('builtins.input',side_effect=['2','1','2']), patch('sys.stdout',new=io.StringIO()):
            picker.main(['codex'])
            launch.assert_called_with('codex',['codex'],'small','low')

    def test_save_failure_does_not_block_launch(self):
        with patch.object(picker,'discover',return_value=[{'id':'small','name':'Small','efforts':[]}]), patch.object(picker,'launch',return_value=0) as launch, patch.object(picker.os,'replace',side_effect=OSError('read only')), patch('builtins.input',return_value='2'), patch('sys.stdout',new=io.StringIO()), patch('sys.stderr',new=io.StringIO()):
            self.assertEqual(picker.main(['codex']),0)
            launch.assert_called_once()

    def test_enter_accepts_visible_default(self):
        output=io.StringIO()
        with patch('builtins.input',return_value=''), patch('sys.stdout',new=output):
            self.assertEqual(picker.choose('Model',['A','B'],default=1),1)
        self.assertIn('B [default]',output.getvalue())

    def test_remembers_model_and_effort_per_agent(self):
        models=[{'id':'small','name':'Small','efforts':['low','high']}]
        with patch.object(picker,'discover',return_value=models), patch.object(picker,'launch',return_value=0) as launch, patch('sys.stdout',new=io.StringIO()):
            with patch('builtins.input',side_effect=['2','3']): picker.main(['codex'])
            with patch('builtins.input',side_effect=['','']): picker.main(['codex'])
            launch.assert_called_with('codex',['codex'],'small','high')
            with patch('builtins.input',return_value=''): picker.main(['claude'])
            launch.assert_called_with('claude',['claude'],None,None)
        self.assertEqual(picker.load_choice('codex'),{'model':'small','effort':'high'})
        self.assertEqual(picker.load_choice('claude'),{'model':None,'effort':None})

    def test_cancel_does_not_replace_preference(self):
        picker.save_choice('codex','small','low')
        with patch.object(picker,'discover',return_value=[]), patch('builtins.input',return_value='0'), patch('sys.stdout',new=io.StringIO()): picker.main(['codex'])
        self.assertEqual(picker.load_choice('codex'),{'model':'small','effort':'low'})

    def test_discovery_failure_preserves_saved_choice(self):
        picker.save_choice('codex','small','low')
        with patch.object(picker,'discover',side_effect=picker.DiscoveryError('offline')), patch.object(picker,'launch',return_value=0), patch('builtins.input',return_value=''), patch('sys.stdout',new=io.StringIO()): picker.main(['codex'])
        self.assertEqual(picker.load_choice('codex'),{'model':'small','effort':'low'})

    def test_stale_model_falls_back_to_no_overrides(self):
        picker.save_choice('codex','removed','low')
        with patch.object(picker,'discover',return_value=[{'id':'small','name':'Small','efforts':[]}]), patch.object(picker,'launch',return_value=0) as launch, patch('builtins.input',return_value=''), patch('sys.stdout',new=io.StringIO()):
            picker.main(['codex'])
            launch.assert_called_with('codex',['codex'],None,None)

    def test_invalid_state_ignored(self):
        picker.save_choice('codex','small','low')
        path=picker.choice_path('codex')
        for value in ['broken','[]','{"model":42,"effort":[]}']:
            path.write_text(value)
            self.assertEqual(picker.load_choice('codex'),{})

    def test_menu_line_clips_wide_characters_without_wrapping(self):
        self.assertEqual(picker.fit_line('模型 ABC',5),'模型 ')
        self.assertEqual(picker.fit_line('long option',4),'long')

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
            launch.assert_called_once_with('codex', ['codex','--id','source'],None,None)

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
