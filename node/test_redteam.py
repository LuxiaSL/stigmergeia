"""Regression checks for false positives in the defensive harness."""
import unittest

from redteam import passed


class VerdictTests(unittest.TestCase):
    def test_launch_failure_is_not_protection(self):
        for kind in ('ok', 'oom', 'timeout'):
            self.assertFalse(passed(kind, 1, '', 3, 'timeout'))

    def test_success_requires_completion(self):
        self.assertFalse(passed('ok', 0, 'REDTEAM:started\n', .1))
        self.assertTrue(passed('ok', 0, 'REDTEAM:started\nREDTEAM:ok\n', .1))

    def test_resource_failures_need_correct_manager_evidence(self):
        output = 'REDTEAM:started\nREDTEAM:armed\n'
        self.assertFalse(passed('oom', 1, output, 1, 'exit-code'))
        self.assertFalse(passed('oom', 137, output, 1))
        self.assertTrue(passed('oom', 1, output, 1, 'oom-kill'))
        self.assertFalse(passed('timeout', 1, output, 3, 'oom-kill'))
        self.assertTrue(passed('timeout', 1, output, 3, 'timeout'))
        self.assertFalse(passed('timeout', 1, output, 18, 'timeout'))

    def test_completed_workload_does_not_prove_resource_limit(self):
        output = 'REDTEAM:started\nREDTEAM:armed\nREDTEAM:ok\n'
        self.assertFalse(passed('oom', 1, output, 1, 'oom-kill'))
        self.assertFalse(passed('timeout', 1, output, 3, 'timeout'))


if __name__ == '__main__':
    unittest.main()
