#!/usr/bin/env python3
"""Solaris integration checks: python3 tests/test_defer_ssh_agent.py [script]."""
import concurrent.futures
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = str(Path(sys.argv.pop(1)).resolve()) if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / 'bin/defer_ssh_agent')

class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='ca2-agent-tests-'))
        cls.bin = cls.root / 'bin'
        cls.bin.mkdir()
        cls.agents = cls.root / 'agents'
        cls.adds = cls.root / 'adds'
        for name, log in [('ssh-agent', cls.agents), ('ssh-add', cls.adds)]:
            real = shutil.which(name)
            if not real:
                raise unittest.SkipTest(name + ' is required')
            wrapper = cls.bin / name
            if name == 'ssh-agent':
                body = "output = subprocess.check_output([REAL] + sys.argv[1:])\nm = re.search(rb'SSH_AGENT_PID=([0-9]+)', output)\nif m:\n    with open(LOG, 'ab') as f: f.write(m.group(1) + b'\\n')\nsys.stdout.buffer.write(output)\n"
            else:
                body = "if sys.argv[1:] != ['-l']:\n    with open(LOG, 'a') as f: f.write('add\\n')\nos.execv(REAL, [REAL] + sys.argv[1:])\n"
            wrapper.write_text('#!' + sys.executable + '\nimport os, re, subprocess, sys\nREAL=' + repr(real) + '\nLOG=' + repr(str(log)) + '\n' + body)
            wrapper.chmod(0o700)

    @classmethod
    def tearDownClass(cls):
        if cls.agents.exists():
            for pid in cls.agents.read_text().splitlines():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        shutil.rmtree(cls.root)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=str(self.root)))
        self.env = dict(os.environ, HOME=str(self.home), AGENT_SCRIPT=SCRIPT,
                        PATH=str(self.bin) + ':' + os.environ['PATH'])
        self.env.pop('SSH_AUTH_SOCK', None)
        self.env.pop('SSH_AGENT_PID', None)
        self.before = self.count(self.agents)
        self.add_before = self.count(self.adds)

    @staticmethod
    def count(path):
        return len(path.read_text().splitlines()) if path.exists() else 0

    def shell(self, interactive=False, env=None):
        master, slave = pty.openpty()
        try:
            cmd = '. "$AGENT_SCRIPT"; . "$AGENT_SCRIPT"; printf "\\nRESULT=%s\\n" "${SSH_AUTH_SOCK-}"'
            proc = subprocess.Popen(['/usr/bin/bash', '--noprofile', '--norc', '-ic' if interactive else '-c', cmd],
                                    env=env or self.env, stdin=slave, stderr=slave, stdout=subprocess.PIPE)
            try:
                output = proc.communicate(timeout=12)[0].decode()
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            self.assertEqual(proc.returncode, 0)
            return re.search(r'^RESULT=(.*)$', output, re.M).group(1)
        finally:
            os.close(slave)
            os.close(master)

    def cache(self):
        return self.home / '.cache/ca2/ssh-agent/socket'

    def test_noninteractive_does_not_create(self):
        self.assertEqual(self.shell(), '')
        self.assertEqual(self.count(self.agents), self.before)
        self.assertFalse(self.cache().exists())

    def test_concurrent_creation_and_noninteractive_reuse(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            sockets = list(pool.map(lambda _: self.shell(True), range(5)))
        self.assertTrue(sockets[0])
        self.assertEqual(len(set(sockets)), 1)
        self.assertEqual(self.count(self.agents), self.before + 1)
        self.assertEqual(self.shell(), sockets[0])
        self.assertEqual(self.cache().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cache().parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.cache().parent / 'lock').exists())

    def test_inherited_agent_takes_precedence(self):
        shared = self.shell(True)
        output = subprocess.check_output([str(self.bin / 'ssh-agent'), '-s'], env=self.env).decode()
        inherited = re.search(r'SSH_AUTH_SOCK=([^;]+)', output).group(1)
        self.assertEqual(self.shell(env=dict(self.env, SSH_AUTH_SOCK=inherited)), inherited)
        self.assertEqual(self.cache().read_text().strip(), shared)
        self.assertEqual(self.count(self.agents), self.before + 2)

    def test_stale_socket_recovery(self):
        self.cache().parent.mkdir(parents=True)
        self.cache().write_text('/nonexistent/agent.sock\n')
        env = dict(self.env, SSH_AUTH_SOCK='/nonexistent/inherited.sock')
        recovered = self.shell(True, env)
        self.assertNotEqual(recovered, env['SSH_AUTH_SOCK'])
        self.assertEqual(self.shell(), recovered)
        self.assertEqual(self.count(self.agents), self.before + 1)

    def test_cache_is_data(self):
        self.cache().parent.mkdir(parents=True)
        marker = self.home / 'executed'
        self.cache().write_text('$(touch ' + str(marker) + ')\n')
        self.assertEqual(self.shell(), '')
        self.assertFalse(marker.exists())
        self.assertEqual(self.count(self.agents), self.before)

    def test_busy_lock_is_bounded(self):
        (self.cache().parent / 'lock').mkdir(parents=True)
        start = time.monotonic()
        self.assertEqual(self.shell(True), '')
        self.assertLess(time.monotonic() - start, 11)
        self.assertEqual(self.count(self.agents), self.before)

    def test_key_loading_only_once_and_only_interactively(self):
        key = self.home / '.ssh/id_auth'
        key.parent.mkdir()
        subprocess.check_call(['ssh-keygen', '-q', '-t', 'rsa', '-b', '2048', '-N', '', '-f', str(key)])
        self.assertEqual(self.shell(), '')
        self.assertEqual(self.count(self.adds), self.add_before)
        sock = self.shell(True)
        self.assertTrue(sock)
        self.assertEqual(self.shell(True), sock)
        self.assertEqual(self.shell(), sock)
        self.assertEqual(self.count(self.adds), self.add_before + 1)
        self.assertEqual(self.count(self.agents), self.before + 1)

if __name__ == '__main__':
    unittest.main(verbosity=2)
