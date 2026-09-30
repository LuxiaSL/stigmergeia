"""Local core assignment: one function for both backends, local_first_cpu
upward inside the pool (local_cpus, else this process's affinity), wrapping
only inside the pool, with a warning when agents wrap or share."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stigmergeia.config import RunConfig, parse_cpus


def config(**kw) -> RunConfig:
    tmp = Path(tempfile.gettempdir())
    base = {"run_name": "cfgtest", "runs_dir": str(tmp / "runs"), "task_dir": str(tmp / "task"),
            "n_agents": 10, "per_agent_budget_usd": 1, "total_budget_usd": 10,
            "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "op.token")}, "gate": {}}
    return RunConfig.model_validate({**base, **kw})


class LocalCoreTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("os.sched_getaffinity", return_value=set(range(16)))
        p.start()
        self.addCleanup(p.stop)

    def test_ten_agents_from_zero_take_zero_to_nine_on_either_backend(self):
        for backend in ("claude", "codex"):
            c = config(backend=backend, local_first_cpu=0)
            self.assertEqual([c.local_cpu_for(i) for i in range(10)], list(range(10)))
            self.assertIsNone(c.local_core_map()[1])

    def test_codex_base_range_wraps_inside_its_pool_not_onto_core_zero(self):
        c = config(backend="codex", n_agents=4, local_first_cpu=13, local_cpus="13-15")
        self.assertEqual([c.local_cpu_for(i) for i in range(4)], [13, 14, 15, 13])
        self.assertIn("share", c.local_core_map()[1])
        # without a pool it still wraps onto 0 (the old behaviour), and says so
        c = config(backend="codex", n_agents=4, local_first_cpu=13)
        self.assertEqual([c.local_cpu_for(i) for i in range(4)], [13, 14, 15, 0])
        self.assertIn("wrap", c.local_core_map()[1])

    def test_parse_and_unavailable_pool(self):
        self.assertEqual(parse_cpus("0-2,8, 10-11"), [0, 1, 2, 8, 10, 11])
        with self.assertRaises(ValueError):
            config(local_cpus="40-41").local_pool()


if __name__ == "__main__":
    unittest.main()
