"""The guard: writes stay in the workspace; reads go anywhere but credential
locations, and outside reads are recorded; shell commands are recorded."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from stigmergeia.guard import LONG_SLEEP, long_sleep, make_guard


def decide(guard, tool, **args):
    out = asyncio.run(guard({"tool_name": tool, "tool_input": args}, None, None))
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = base / "ws"
        self.ws.mkdir()
        (base / "private").mkdir()
        (base / "private" / "korax.json").write_text("{}")
        (base / "notes.txt").write_text("hi")
        (self.ws / "policy.py").write_text("x = 1\n")
        os.symlink(base / "private", self.ws / "escape")
        self.audit = base / "audit.jsonl"
        deny = [f"{base}/private/**", "**/.env", str(Path.home() / ".ssh") + "/**"]
        self.g = make_guard(self.ws, ("mcp__korax__", "mcp__lab__"), deny, self.audit, shell=True)
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def audit_rows(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def test_workspace_reads_and_writes_are_allowed_and_not_flagged(self):
        self.assertEqual(decide(self.g, "Read", file_path="policy.py"), "allow")
        self.assertEqual(decide(self.g, "Write", file_path="new/dir/f.py", content=""), "allow")
        self.assertEqual(decide(self.g, "Glob", pattern="**/*.py"), "allow")
        self.assertEqual(self.audit_rows(), [])

    def test_outside_reads_are_allowed_and_recorded(self):
        self.assertEqual(decide(self.g, "Read", file_path=str(self.base / "notes.txt")), "allow")
        self.assertEqual(decide(self.g, "Read", file_path="/etc/hostname"), "allow")
        rows = self.audit_rows()
        self.assertEqual([r["decision"] for r in rows], ["allow", "allow"])
        self.assertTrue(all(r["outside"] for r in rows))

    def test_credential_locations_are_denied_even_via_symlink_or_tilde(self):
        self.assertEqual(decide(self.g, "Read", file_path="escape/korax.json"), "deny")
        self.assertEqual(decide(self.g, "Read", file_path=str(self.base / "private" / "korax.json")), "deny")
        self.assertEqual(decide(self.g, "Read", file_path="~/.ssh/id_ed25519"), "deny")
        self.assertEqual(decide(self.g, "Grep", pattern="token", path="escape"), "deny")
        self.assertEqual(decide(self.g, "Glob", pattern="~/.ssh/*"), "deny")
        (self.ws / ".env").write_text("K=v")
        self.assertEqual(decide(self.g, "Read", file_path=".env"), "deny")

    def test_writes_outside_are_denied(self):
        for p in [str(self.base / "notes.txt"), "../x.py", "~/x.py", "/tmp/x.py"]:
            self.assertEqual(decide(self.g, "Write", file_path=p, content=""), "deny", p)

    def test_shell_is_recorded_with_outside_paths_flagged(self):
        self.assertEqual(decide(self.g, "Bash", command="python policy.py"), "allow")
        self.assertEqual(decide(self.g, "Bash", command="cat /etc/os-release", run_in_background=False), "allow")
        rows = self.audit_rows()
        self.assertEqual([r["outside"] for r in rows], [False, True])
        self.assertEqual(rows[1]["paths_outside"], ["/etc/os-release"])

    def test_board_namespaces_and_korax_calls_are_not_flagged(self):
        decide(self.g, "Bash", command="korax post --ns /swarm/run-1 --type FINDING --payload x")
        decide(self.g, "Bash", command="python - <<'E'\nx = 7 // 2  # /korax/canon mention\nE")
        decide(self.g, "Bash", command="cat /etc/passwd")
        decide(self.g, "Bash", command=f"cat /tmp/claude-{os.getuid()}/x/tasks/b1.output; python -c 'print(6 / 2)' > /dev/null")
        self.assertEqual([r["outside"] for r in self.audit_rows()], [False, False, True, False])

    def test_unscoped_warn_is_refused_both_routes(self):
        bare = "a02 dead end: BFS direction order, no effect (70.4 vs 70.4)."
        scoped = ("a02 dead end: BFS direction order. Tested on: base BFS+tail (../a02/policy.py), 100 train eps. "
                  "Result: 70.4 vs 70.4. Revive if: several candidate paths are scored by a better position score.")
        self.assertEqual(decide(self.g, "mcp__korax__korax_post", ns="/swarm/x", type="WARN", payload=bare), "deny")
        self.assertEqual(decide(self.g, "mcp__korax__korax_post", ns="/swarm/x", type="WARN", payload=scoped), "allow")
        self.assertEqual(decide(self.g, "mcp__korax__korax_post", ns="/swarm/x", type="FINDING", payload=bare), "allow")
        self.assertEqual(decide(self.g, "Bash", command=f'korax post --ns /swarm/x --type WARN --payload "{bare}"'), "deny")
        self.assertEqual(decide(self.g, "Bash", command=f'korax post --ns /swarm/x --type WARN --payload "{scoped}"'), "allow")
        (self.ws / "w.txt").write_text(scoped)
        self.assertEqual(decide(self.g, "Bash", command="korax post --ns /swarm/x --type WARN --payload-file w.txt"), "allow")
        (self.ws / "w2.txt").write_text(bare)
        self.assertEqual(decide(self.g, "Bash", command="korax post --type WARN --ns /swarm/x --payload-file w2.txt"), "deny")
        self.assertEqual(decide(self.g, "Bash", command=f'korax post --ns /swarm/x --type FINDING --payload "{bare}"'), "allow")

    def test_shell_off_denies_shell(self):
        g = make_guard(self.ws, ("mcp__korax__",), [], None, shell=False)
        self.assertEqual(decide(g, "Bash", command="ls"), "deny")

    def test_unknown_tools_denied_mcp_passes(self):
        self.assertEqual(decide(self.g, "WebFetch", url="https://example.com"), "deny")
        self.assertEqual(decide(self.g, "Task", prompt="x"), "deny")
        self.assertEqual(decide(self.g, "mcp__korax__korax_post", ns="/swarm"), "allow")
        self.assertEqual(decide(self.g, "TaskStop", task_id="x"), "allow")


class LongSleepGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.g = make_guard(self.ws, ("mcp__korax__", "mcp__lab__"), [], self.audit, shell=True)

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, tool="Bash", **args):
        out = asyncio.run(self.g({"tool_name": tool, "tool_input": args}, None, None))
        return out.get("hookSpecificOutput", {})

    def test_long_foreground_sleeps_are_refused_with_the_pointer(self):
        for cmd in ("sleep 175; korax feed --since 607", "sleep 2m", "sleep 1m 30s", "sleep 45 && sleep 30",
                    "cd task && sleep 90 && cat log", "for i in $(seq 20); do sleep 30; korax feed; done",
                    "until grep -q done log; do\n  sleep 60\ndone", "sleep infinity", "(sleep 120; echo hi)",
                    "time sleep 120; echo slept", "command sleep 90", "nice sleep 300"):
            hso = self.decide(command=cmd)
            self.assertEqual(hso.get("permissionDecision"), "deny", cmd)
            self.assertEqual(hso["permissionDecisionReason"], LONG_SLEEP)
        self.assertIn("`wait`", LONG_SLEEP)
        self.assertIn("read what the others are doing, reply, or review someone's code", LONG_SLEEP)
        rows = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertTrue(all(r["rule"] == "long sleep" for r in rows))
        self.assertEqual(rows[0]["sleep_s"], 175)

    def test_short_background_and_unjudgeable_sleeps_pass(self):
        for cmd in ("sleep 30", "sleep 59", "sleep 0.5 && ls", "while ! test -f out; do sleep 5; done",
                    "sleep 600 &", "echo sleep 500", "sleep $N", "python -c 'import time; time.sleep(1)'",
                    "grep -r sleep ."):
            self.assertEqual(self.decide(command=cmd), {}, cmd)
        self.assertEqual(self.decide(command="sleep 175", run_in_background=True), {})
        self.assertEqual(self.decide("Monitor", command="while true; do sleep 120; korax feed; done"), {})

    def test_parser(self):
        self.assertEqual(long_sleep("sleep 175"), 175)
        self.assertIsNone(long_sleep("sleep 59"))
        self.assertEqual(long_sleep("sleep 1h"), 3600)
        self.assertEqual(long_sleep("for x in a b; do sleep 20; done"), float("inf"))
        self.assertIsNone(long_sleep("for x in a b; do sleep 19; done"))


if __name__ == "__main__":
    unittest.main()
