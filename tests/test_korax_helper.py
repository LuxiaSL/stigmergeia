"""shellbin/korax_helper.py: `korax feed`, `korax show`, and the forgiving
`--mention` list; plus the wrapper's dispatch, run through /bin/sh."""
import contextlib
import importlib.util
import io
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from test_pulse import GATE, ME, NS, OTHER, THIRD, FakeBoard, gate_result

SHELLBIN = Path(__file__).resolve().parents[1] / "stigmergeia" / "shellbin"
spec = importlib.util.spec_from_file_location("korax_helper", SHELLBIN / "korax_helper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

IDENTITIES = {"identities": [{"id": ME, "display": "t-a00"}, {"id": OTHER, "display": "t-a01"},
                             {"id": THIRD, "display": "t-a02"}, {"id": GATE, "display": "t-gate"}]}


class MentionArgsTests(unittest.TestCase):
    def test_space_separated_list_becomes_repeated_flags(self):
        self.assertEqual(helper.normalize_post_args(
            ["--ns", "/x", "--payload", "hi", "--mention", "band:a", "band:b", "--ref", "replies:3"]),
            ["--ns", "/x", "--payload", "hi", "--mention", "band:a", "--mention", "band:b", "--ref", "replies:3"])

    def test_comma_and_equals_forms(self):
        self.assertEqual(helper.normalize_post_args(["--mention", "band:a,band:b"]),
                         ["--mention", "band:a", "--mention", "band:b"])
        self.assertEqual(helper.normalize_post_args(["--mention=band:a,band:b"]),
                         ["--mention", "band:a", "--mention", "band:b"])

    def test_correct_commands_are_unchanged(self):
        for argv in (["--ns", "/x", "--type", "NOTE", "--payload", "band:a is great"],
                     ["--mention", "band:a", "--mention", "band:b", "--payload", "x"],
                     ['{"ns": "/x"}'], ["-"]):
            self.assertEqual(helper.normalize_post_args(argv), argv)

    def test_a_payload_after_the_list_is_not_swallowed(self):
        self.assertEqual(helper.normalize_post_args(["--mention", "band:a", "band:b", "--payload", "band:c"]),
                         ["--mention", "band:a", "--mention", "band:b", "--payload", "band:c"])


class FeedShowTests(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard()
        self.first = self.board.add(OTHER, NS, "PROPOSAL", "trying beam search " + "y" * 400)
        self.board.add(ME, NS, "FINDING", {"text": "depth 3 helps"}, refs=[("replies", self.first)])
        self.board.add(THIRD, NS, "OPEN", "a00, which depth?", mentions=[ME])
        self.board.add(GATE, NS + "/gate", "RESULT", gate_result(81.2, agent="a02", code="../a02/p.py"))
        self.board.add(OTHER, f"/dm/{ME}", "NOTE", "private hello")

        def get(path):
            if path == "/identities":
                return IDENTITIES
            if path.startswith("/envelope/"):
                i = int(path.rsplit("/", 1)[1])
                return {"envelope": self.board.envs[i - 1]}
            return self.board.fetch(path, "t-me")

        self.env = mock.patch.dict(os.environ, {"KORAX_IDENTITY": ME, "SWARM_NS": NS})
        self.env.start()
        self.get = mock.patch.object(helper, "_get", side_effect=get)
        self.get.start()

    def tearDown(self):
        self.get.stop()
        self.env.stop()

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = helper.main(list(argv))
        return rc, out.getvalue()

    def test_feed_is_one_line_per_post_with_names_edges_and_text(self):
        rc, out = self.run_main("feed")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)  # the namespace and its gate sub-namespace; not the DM
        self.assertTrue(lines[0].startswith("#1 a01 PROPOSAL: trying beam search"))
        self.assertLessEqual(len(lines[0]), 240)
        self.assertEqual(lines[1], "#2 you FINDING [replies #1]: depth 3 helps")
        self.assertEqual(lines[2], "#3 a02 OPEN @you: a00, which depth?")
        self.assertIn("#4 GATE RESULT: a02 p.py: 81.2 (NEW RECORD); code ../a02/p.py", lines[3])

    def test_limit_takes_the_newest_and_since_reads_forward(self):
        rc, out = self.run_main("feed", "--limit", "2")
        self.assertEqual([x.split()[0] for x in out.splitlines()[:2]], ["#3", "#4"])
        self.assertIn("2 older not shown", out)
        rc, out = self.run_main("feed", "--since", "1", "--limit", "1")
        self.assertTrue(out.startswith("#2 "))
        self.assertIn("korax feed --since 2", out)

    def test_for_me_lists_dm_and_mention(self):
        rc, out = self.run_main("feed", "--for-me")
        self.assertIn("#3 a02 OPEN", out)
        self.assertIn("#5 a01 NOTE: private hello", out)
        self.assertNotIn("#1 ", out)

    def test_show_prints_the_whole_text(self):
        rc, out = self.run_main("show", "#1")
        self.assertEqual(rc, 0)
        self.assertIn(f"#1 a01 ({OTHER}) PROPOSAL in {NS}", out)
        self.assertIn("y" * 400, out)

    def test_empty_namespace(self):
        rc, out = self.run_main("feed", "--ns", "/swarm/empty")
        self.assertIn("(no posts in /swarm/empty)", out)


class WrapperDispatchTests(unittest.TestCase):
    """The shell wrapper sends feed/show/post to the helper and everything else
    (and `read <id>`) to the real CLI, here a stub that echoes its argv."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        stub = self.bin / "korax"
        stub.write_text('#!/bin/sh\necho "REAL $*"\n')
        stub.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def run_wrapper(self, *args):
        env = {"PATH": "/usr/bin:/bin", "KORAX_REAL_BIN": str(self.bin), "NO_PROXY": "x"}
        return subprocess.run(["/bin/sh", str(SHELLBIN / "korax"), *args], env=env, capture_output=True,
                              text=True, timeout=30)

    def test_post_with_a_mention_list_reaches_the_real_cli_fixed(self):
        r = self.run_wrapper("post", "--ns", "/x", "--mention", "band:a", "band:b", "--payload", "hi there")
        self.assertEqual(r.stdout.strip(), "REAL post --ns /x --mention band:a --mention band:b --payload hi there")

    def test_read_id_alias_and_passthrough(self):
        self.assertEqual(self.run_wrapper("read", "#12").stdout.strip(), "REAL envelope 12")
        self.assertEqual(self.run_wrapper("whoami").stdout.strip(), "REAL whoami")

    def test_feed_without_a_board_fails_cleanly(self):
        r = self.run_wrapper("feed", "--ns", "/x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("KORAX_URL", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
