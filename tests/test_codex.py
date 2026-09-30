"""The Codex backend: its configuration strips Codex's own tools and injected
context, its sandbox masks credentials and confines writes and network, and
its agent loop (driven here by a scripted fake app-server) runs every tool
call through the guard, declines approvals and clocks, accounts tokens,
enforces the wall clock mid-turn, and fails closed on a built-in tool."""
import asyncio
import contextlib
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from stigmergeia.agent import Ledger, _BudgetExhausted
from stigmergeia.board import Credential
from stigmergeia.codex_agent import (BASE_OVERRIDES, CodexAgent, CodexFailClosed, codex_overrides, link_auth,
                                      strip_catalog_entry)
from stigmergeia.codex_sandbox import bwrap_argv, mask_args, scoped
from stigmergeia.codex_tools import local_tool_specs, text_result
from stigmergeia.config import RunConfig
from stigmergeia.lab import AgentSlot, ToolSpec

FAKE = Path(__file__).with_name("fake_app_server.py")


class ConfigTests(unittest.TestCase):
    def test_catalog_entry_loses_codex_tool_and_prompt_addons(self):
        e = strip_catalog_entry({"slug": "gpt-6-luna", "tool_mode": "code_mode_only", "multi_agent_version": "v2",
                                 "experimental_supported_tools": ["clock"], "apply_patch_tool_type": "freeform",
                                 "model_messages": {"instructions_template": "You are Codex"},
                                 "base_instructions": "long", "supports_search_tool": True, "context_window": 1})
        self.assertNotIn("tool_mode", e)
        self.assertNotIn("multi_agent_version", e)
        self.assertEqual(e["experimental_supported_tools"], [])
        self.assertIsNone(e["apply_patch_tool_type"])
        self.assertIsNone(e["model_messages"])
        self.assertEqual(e["base_instructions"], "")
        self.assertEqual(e["context_window"], 1)

    def test_overrides_switch_off_context_and_tools(self):
        ov = codex_overrides(Path("/x/cat.json"), ["a=1"])
        for must in ("include_environment_context=false", "include_permissions_instructions=false",
                     "features.shell_tool=false", "features.unified_exec=false", "features.current_time_reminder=false",
                     "features.rollout_budget=false", "features.token_budget=false", "features.multi_agent=false",
                     "features.code_mode_host=false", 'web_search="disabled"', "project_doc_max_bytes=0",
                     'model_catalog_json="/x/cat.json"'):
            self.assertIn(must, ov)
        self.assertEqual(ov[-1], "a=1")  # extras last, so they can override
        self.assertTrue(set(BASE_OVERRIDES) <= set(ov))

    def test_auth_is_linked_never_copied(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "real-auth.json"
            real.write_text("dummy")
            home = Path(d) / "home"
            home.mkdir()
            link_auth(home, real)
            link_auth(home, real)  # idempotent
            self.assertTrue((home / "auth.json").is_symlink())
            self.assertEqual(os.readlink(home / "auth.json"), str(real))


class SandboxArgvTests(unittest.TestCase):
    def test_masks(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "ws").mkdir()
            (base / "secret").mkdir()
            (base / "secret" / "inner.token").write_text("t")
            (base / "key.pem").write_text("k")
            args = mask_args([str(base / "secret"), str(base / "secret" / "inner.token"), str(base / "key.pem"),
                              str(base), str(base / "missing")], keep=[base / "ws"])
            joined = " ".join(args)
            self.assertIn(f"--tmpfs {os.path.realpath(base / 'secret')}", joined)
            self.assertIn(f"--ro-bind /dev/null {os.path.realpath(base / 'key.pem')}", joined)
            self.assertNotIn(os.path.realpath(base / "secret" / "inner.token"), args)  # under a masked dir
            self.assertNotIn(os.path.realpath(base), args)  # would hide the workspace
            self.assertNotIn(str(base / "missing"), args)

    def test_bwrap_shape(self):
        argv = bwrap_argv(Path("/w"), [], {"PATH": "/usr/bin"}, "echo hi", Path("/s.sock"), 7460)
        self.assertEqual(argv[0], "bwrap")
        for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
            self.assertIn(flag, argv)
        self.assertIn("/run", argv)  # /run masked: no D-Bus/systemd escape
        i = argv.index("--ro-bind")
        self.assertEqual(argv[i:i + 3], ["--ro-bind", "/", "/"])
        self.assertIn("socat TCP-LISTEN:7460", " ".join(argv))
        self.assertEqual(argv[-1], "echo hi")
        s = scoped(["x"], "swarm-r-a00.slice", "13")
        self.assertEqual(s[:3], ["systemd-run", "--user", "--scope"])
        self.assertEqual(s[-4:], ["taskset", "-c", "13", "x"])


def make_agent(tmp: Path, scenario: str, deadline_s: float = 60, budget: float = 3.0):
    ws, private = tmp / "ws" / "a00", tmp / "private" / "a00"
    ws.mkdir(parents=True)
    private.mkdir(parents=True)
    (tmp / "auth.json").write_text("dummy")
    (tmp / "op.token").write_text("x")
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)
    cfg = RunConfig.model_validate({
        "run_name": "codex-test", "runs_dir": str(tmp / "runs"), "task_dir": str(tmp), "n_agents": 1,
        "backend": "codex", "model": "gpt-6-luna", "shell": False, "local_cpu_quota": None,
        "prices": {"input": 1.0, "output": 10.0, "cache_read_mult": 0.1, "cache_write_mult": 1.0},
        "per_agent_budget_usd": budget, "total_budget_usd": 100,
        "codex": {"binary": str(FAKE), "auth_file": str(tmp / "auth.json")},
        "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "op.token")}, "gate": {}})
    slot = AgentSlot(name="a00", index=0, workspace=ws, private=private, remote_ws="", cores="")
    cred = Credential(url="http://127.0.0.1:1", identity="band:aaaaaaaaaaaa", token="t", display="a00")
    gate = Credential(url="http://127.0.0.1:1", identity="band:gggggggggggg", token="g", display="gate")
    agent = CodexAgent(cfg, slot, cred, gate, None, "", [], Ledger(100), time.time() + deadline_s, "go",
                       catalog=tmp / "cat.json")
    agent.extra_env = {"FAKE_LOG": str(tmp / "fake.jsonl"), "FAKE_SCENARIO": scenario}
    posted = []

    async def korax_post(args):
        posted.append(args)
        return text_result("posted #1")

    agent.tools = {s.name: s for s in local_tool_specs(None, ws, with_shell=False)}  # type: ignore[arg-type]
    agent.tools["mcp__korax__korax_post"] = ToolSpec("mcp__korax__korax_post", "post", {"type": "object"}, korax_post)
    return agent, posted


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def _one_turn(self, agent):
        async with contextlib.AsyncExitStack() as stack:
            srv = await agent._connect(stack)
            return await agent._turn(srv, "go")

    def test_tools_guard_approvals_clock_and_usage(self):
        agent, posted = make_agent(self.base, "tools")
        active = asyncio.run(self._one_turn(agent))
        self.assertTrue(active)  # a Write is work
        self.assertEqual((agent.slot.workspace / "hello.txt").read_text(), "hi")
        self.assertFalse(Path("/etc/evil.txt").exists())
        self.assertEqual(posted, [])  # the unscoped WARN never reached the board
        fake = rows(self.base / "fake.jsonl")
        res = {r["tool_result"]: r["result"]["result"] for r in fake if "tool_result" in r}
        self.assertTrue(res["c1"]["success"])
        self.assertFalse(res["c2"]["success"])
        self.assertIn("inside your workspace", res["c2"]["contentItems"][0]["text"])
        self.assertFalse(res["c3"]["success"])
        self.assertIn("Revive if", res["c3"]["contentItems"][0]["text"])
        self.assertEqual(next(r for r in fake if "approval" in r)["approval"]["result"], {"decision": "decline"})
        self.assertIn("error", next(r for r in fake if "clock" in r)["clock"])  # no clock is served
        start = next(r["recv"] for r in fake if r.get("recv", {}).get("method") == "thread/start")["params"]
        self.assertEqual(start["baseInstructions"], "")
        names = {t["name"] for t in start["dynamicTools"]}
        self.assertIn("korax", names)  # MCP-style tools arrive as a namespace
        self.assertFalse(any(n.startswith("mcp__") for n in names))
        argv = next(r["argv"] for r in fake if "argv" in r)
        self.assertIn("include_environment_context=false", argv)
        # usage: two cumulative updates -> deltas summing to the last total: 600 uncached, 2400 cached, 120 out
        self.assertAlmostEqual(agent.state.estimate, (600 * 1 + 2400 * 0.1 + 120 * 10) / 1e6, places=9)
        t = rows(agent.transcript)
        kinds = [r["_type"] for r in t]
        self.assertIn("ResultMessage", kinds)
        calls = [b for r in t if r["_type"] == "AssistantMessage" for b in r["content"] if "name" in b]
        self.assertEqual([c["name"] for c in calls], ["Write", "Write", "mcp__korax__korax_post"])
        results = [b for r in t if r["_type"] == "UserMessage" for b in r["content"]]
        self.assertEqual([b["is_error"] for b in results], [False, True, True])
        self.assertTrue(any(b.get("text") == "done" for r in t if r["_type"] == "AssistantMessage" for b in r["content"]))

    def test_deadline_interrupts_mid_turn(self):
        agent, _ = make_agent(self.base, "hang", deadline_s=2)
        with self.assertRaises(_BudgetExhausted):
            asyncio.run(self._one_turn(agent))
        self.assertIn("wall clock", agent.state.stop_reason)
        fake = rows(self.base / "fake.jsonl")
        self.assertTrue(any(r.get("recv", {}).get("method") == "turn/interrupt" for r in fake))

    def test_budget_interrupts_mid_turn(self):
        agent, _ = make_agent(self.base, "budget", budget=1.0)
        with self.assertRaises(_BudgetExhausted):
            asyncio.run(self._one_turn(agent))
        self.assertIn("budget", agent.state.stop_reason)

    def test_builtin_tool_item_fails_closed(self):
        agent, _ = make_agent(self.base, "leak")
        with self.assertRaises(CodexFailClosed):
            asyncio.run(self._one_turn(agent))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(all(__import__("shutil").which(t) for t in ("bwrap", "socat")), "needs bwrap and socat")
class SandboxLiveTests(unittest.TestCase):
    """The real sandbox: writes confined, credentials masked, network = the board only."""

    def test_confinement(self):
        from stigmergeia.codex_sandbox import BoardForwarder
        from stigmergeia.codex_tools import SandboxSpec, ShellHost

        async def main(base: Path) -> dict:
            async def http(r, w):
                await r.read(100)
                w.write(b"HTTP/1.0 200 OK\r\n\r\nboard-ok\n")
                await w.drain()
                w.close()
            board = await asyncio.start_server(http, "127.0.0.1", 0)
            other = await asyncio.start_server(http, "127.0.0.1", 0)
            bport, oport = board.sockets[0].getsockname()[1], other.sockets[0].getsockname()[1]
            sock = Path(tempfile.mkdtemp(prefix="sb", dir="/tmp")) / "b.sock"  # short: AF_UNIX path limit
            fwd = BoardForwarder(sock, "127.0.0.1", bport)
            await fwd.start()
            spec = SandboxSpec(base / "ws", [str(base / "secret")], {"PATH": "/usr/bin:/bin", "HOME": str(base)},
                               sock, bport, None, None)
            sh = ShellHost(spec)
            out = {}
            for k, c in {"board": f"curl -s -m 5 http://127.0.0.1:{bport}/",
                         "other": f"curl -s -m 3 http://127.0.0.1:{oport}/ || echo BLOCKED",
                         "write_ws": "echo hi > f.txt && cat f.txt",
                         "write_out": f"(echo x > {base}/out.txt) 2>/dev/null || echo DENIED",
                         "secret": f"cat {base}/secret/key 2>/dev/null || echo MASKED"}.items():
                out[k] = (await sh.run(c, 20))[1].strip()
            await sh.close()
            await fwd.close()
            board.close()
            other.close()
            return out

        with tempfile.TemporaryDirectory(dir=Path.home()) as d:  # not /tmp: the sandbox's /tmp is private
            base = Path(d)
            (base / "ws").mkdir()
            (base / "secret").mkdir()
            (base / "secret" / "key").write_text("s3cret")
            out = asyncio.run(main(base))
            self.assertEqual(out["board"], "board-ok")
            self.assertTrue(out["other"].endswith("BLOCKED"))
            self.assertEqual(out["write_ws"], "hi")
            self.assertEqual(out["write_out"], "DENIED")
            self.assertFalse((base / "out.txt").exists())
            self.assertEqual(out["secret"], "MASKED")


class LabPathTests(unittest.TestCase):
    def test_own_sibling_path_means_own_workspace(self):
        from stigmergeia.lab import own_relative
        self.assertEqual(own_relative("a00", "../a00/policy.py"), "policy.py")
        self.assertEqual(own_relative("a00", "../a00/sub/p.py"), "sub/p.py")
        self.assertEqual(own_relative("a00", "../a01/policy.py"), "../a01/policy.py")  # still refused later
        self.assertEqual(own_relative("a00", "policy.py"), "policy.py")
