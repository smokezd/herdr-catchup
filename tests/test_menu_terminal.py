import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TerminalMenuTests(unittest.TestCase):
    def menu(self, keys, default=0, interrupt=False, labels=None, columns=80, lines=24):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        program = '''import importlib.util,json,os,termios
s=importlib.util.spec_from_file_location('picker', 'bin/fork_picker.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
before=termios.tcgetattr(0)
try:
 result=m.choose('Model',LABELS,default=DEFAULT)
 print('RESULT='+json.dumps(result),flush=True)
except KeyboardInterrupt:
 print('INTERRUPTED',flush=True)
finally:
 after=termios.tcgetattr(0)
 # Restoring canonical input can set the kernel's pending-input flag.
 before[3] &= ~getattr(termios,'PENDIN',0)
 after[3] &= ~getattr(termios,'PENDIN',0)
 print('RESTORED='+str(before==after),flush=True)
'''.replace('DEFAULT',str(default)).replace('LABELS',repr(labels or ['First','Second','Third']))
        process=subprocess.Popen([sys.executable,'-c',program],cwd=ROOT,stdin=slave,stdout=slave,stderr=slave,env=dict(os.environ,TERM='xterm',COLUMNS=str(columns),LINES=str(lines)))
        output=b''
        try:
            deadline=time.monotonic()+5
            while b'j/k' not in output and time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]: output+=os.read(master,65536)
            self.assertIn(b'j/k',output)
            if interrupt:
                import signal
                process.send_signal(signal.SIGINT)
            else:
                os.write(master,keys)
            while process.poll() is None and time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]: output+=os.read(master,65536)
            process.wait(timeout=2)
            while select.select([master],[],[],.05)[0]:
                output+=os.read(master,65536)
            self.assertEqual(process.returncode,0,output.decode())
            flags = termios.ECHO | termios.ICANON | termios.ISIG
            self.assertEqual(termios.tcgetattr(slave)[3] & flags, original[3] & flags)
            self.assertIn(b'RESTORED=True',output)
            self.output = output
            if interrupt:
                self.assertIn(b'INTERRUPTED',output)
                return None
            for line in output.decode().splitlines():
                if line.startswith('RESULT='): return json.loads(line[7:])
            self.fail(output.decode())
        finally:
            if process.poll() is None:
                process.kill();process.wait()
            os.close(master);os.close(slave)

    def test_short_terminal_scrolls_options(self):
        labels=['Option '+str(i) for i in range(12)]
        self.assertEqual(self.menu(b'jjjjj\r',labels=labels,columns=48,lines=8),5)
        self.assertIn('\x1b[7m› 6) Option 5',self.output.decode())
        self.assertIn('6/12',self.output.decode())

    def test_highlight_moves_on_option_row(self):
        self.assertEqual(self.menu(b'j\r'),1)
        self.assertIn('\x1b[7m› 1) First',self.output.decode())
        self.assertIn('\x1b[7m› 2) Second',self.output.decode())
        self.assertNotIn('selected:',self.output.decode())

    def test_interrupt_restores_terminal(self):
        self.assertIsNone(self.menu(b'', interrupt=True))

    def test_application_arrow_up(self):
        self.assertEqual(self.menu(b'\x1bOA\r'),2)

    def test_arrow_down(self):
        self.assertEqual(self.menu(b'\x1b[B\r'),1)

    def test_vim_down_and_up(self):
        self.assertEqual(self.menu(b'jjk\r'),1)

    def test_wrap_up(self):
        self.assertEqual(self.menu(b'k\r'),2)

    def test_saved_default(self):
        self.assertEqual(self.menu(b'\r',default=2),2)

    def test_number_selection(self):
        self.assertEqual(self.menu(b'3\r'),2)

    def test_escape_cancel(self):
        self.assertIsNone(self.menu(b'\x1b'))

    def test_zero_cancel(self):
        self.assertIsNone(self.menu(b'0\r'))

    def test_eof_cancel(self):
        self.assertIsNone(self.menu(b'\x04'))


if __name__=='__main__': unittest.main()
