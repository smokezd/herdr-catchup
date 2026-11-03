import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.log = self.base / 'log'
        self.env = dict(os.environ, PATH=str(self.base) + os.pathsep + os.environ['PATH'],
                        HERDR_BIN_PATH=str(self.base / 'herdr'),
                        CATCHUP_PROVIDER='codex', CATCHUP_SID='exact session',
                        HERDR_PLUGIN_CONTEXT_JSON=json.dumps({'focused_pane_cwd':str(ROOT),
                                                              'focused_pane_id':'w1:p3',
                                                              'focused_pane_agent':'codex'}),
                        LOG=str(self.log))
        self.fake('herdr', 'import sys,json,os\nfrom pathlib import Path\nif sys.argv[1:3]==["agent","get"]: print(json.dumps({"agent_session":{"value":"exact session"}}))\nelse: Path(os.environ["LOG"]).write_text(json.dumps(sys.argv[1:]))\n')
        self.fake('catchup', 'import sys,json,os\nfrom pathlib import Path\nPath(os.environ["LOG"]).write_text(json.dumps(sys.argv[1:]))\n')
        self.fake('python3', 'import sys,json,os\nfrom pathlib import Path\nPath(os.environ["LOG"]).write_text(json.dumps(sys.argv[1:]))\n')

    def fake(self,name,body):
        path=self.base/name
        path.write_text('#!'+os.sys.executable+'\n'+body)
        path.chmod(0o755)

    def run_script(self,*args):
        p=subprocess.run(['/bin/bash',str(ROOT/'bin/run.sh'),*args],env=self.env,input='',text=True,capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
        return json.loads(self.log.read_text())

    def test_manual_action_enables_picker_and_pins_split(self):
        args=self.run_script('fork')
        self.assertIn('CATCHUP_PICK_FORK=1',args)
        self.assertIn('CATCHUP_SID=exact session',args)
        self.assertEqual(args[args.index('--target-pane')+1],'w1:p3')

    def test_picker_receives_exact_selection(self):
        self.env['CATCHUP_PICK_FORK']='1'
        args=self.run_script('--in-pane','fork')
        self.assertTrue(args[0].endswith('fork_picker.py'))
        self.assertEqual(args[1:],['codex','--id','exact session'])

    def test_automatic_fork_bypasses_picker(self):
        self.env['CATCHUP_PICK_FORK']='0'
        self.assertEqual(self.run_script('--in-pane','fork'),['fork','codex','--id','exact session'])

    def test_other_agent_bypasses_picker(self):
        self.env.update(CATCHUP_PICK_FORK='1',CATCHUP_PROVIDER='opencode')
        self.assertEqual(self.run_script('--in-pane','fork'),['fork','opencode','--id','exact session'])

    def test_missing_python_allows_unchanged_fork(self):
        (self.base/'python3').unlink()
        self.env.update(PATH=str(self.base), CATCHUP_PICK_FORK='1')
        result=subprocess.run(['/bin/bash',str(ROOT/'bin/run.sh'),'--in-pane','fork'],env=self.env,input='1\n',text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('needs Python 3',result.stdout)
        self.assertEqual(json.loads(self.log.read_text()),['fork','codex','--id','exact session'])

    def test_handoff_unchanged(self):
        self.env['CATCHUP_PICK_FORK']='1'
        self.assertEqual(self.run_script('--in-pane','handoff','claude'),['fork','codex','--id','exact session','--into','claude'])


if __name__=='__main__': unittest.main()
