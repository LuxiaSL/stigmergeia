"""Test startup isolation without requiring a torch install on the host."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class StartupTests(unittest.TestCase):
    def run_startup(self, jailed, mode):
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parent / 'python_compat'),
               'SWARM_JAILED': jailed, 'SWARM_TORCH_IPC': mode}
        code = '''
import sys
assert 'torch' not in sys.modules
print(sum(type(f).__name__ == '_Finder' for f in sys.meta_path))
'''
        return subprocess.check_output([sys.executable, '-c', code], env=env, text=True).strip()

    def test_only_opted_in_jailed_python_installs_hook(self):
        self.assertEqual(self.run_startup('1', 'copy'), '1')
        self.assertEqual(self.run_startup('1', 'default'), '0')
        self.assertEqual(self.run_startup('0', 'copy'), '0')


if __name__ == '__main__':
    unittest.main()
