import contextlib
from io import StringIO
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from observer import code_mode_count, frequencies, main, scan, utility_names


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-rollout.jsonl"
HOOK = Path(__file__).parent / "fixtures" / "synthetic-pre-tool-use.json"


class ObserverTests(unittest.TestCase):
    def test_source_shaped_rollout_preserves_calls_and_pairs_output(self):
        result = scan(FIXTURE)
        self.assertEqual(len(result["calls"]), 5)
        first = result["calls"][0]
        self.assertEqual(first["session_id"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(first["turn_id"], "turn-1")
        self.assertEqual(first["command"], "find . -type f | grep '.conf$' | awk '{print $1}'")
        self.assertEqual(first["utilities"], ["find", "grep", "awk"])
        self.assertEqual(first["outputs"][0]["call_id"], first["call_id"])
        self.assertEqual(first["raw"], json.loads(first["raw_line"]))
        self.assertIsNone(result["calls"][2]["command"])  # apply_patch is not shell
        self.assertIn("argument_error", result["calls"][3])
        self.assertEqual(len(result["unmatched_outputs"]), 1)
        self.assertEqual(code_mode_count(result["calls"]), 1)

    def test_utility_extraction_counts_pipeline_not_quoted_mentions(self):
        self.assertEqual(utility_names("printf 'try grep next'"), ["printf"])
        self.assertEqual(utility_names("bash -lc 'find . | grep x'"), ["bash", "find", "grep"])
        self.assertEqual(utility_names("A=1 grep foo file && awk '{print $1}' file"), ["grep", "awk"])
        self.assertEqual(utility_names("sudo grep needle /etc/hosts"), ["sudo", "grep"])
        self.assertEqual(utility_names("sudo -u root grep needle /etc/hosts"), ["sudo", "grep"])
        self.assertEqual(utility_names("env LC_ALL=C find ."), ["env", "find"])

    def test_heredocs_and_loop_headers_are_not_utilities(self):
        self.assertEqual(
            utility_names("python3 - <<'PY'\nprint('a', 'b')\nPY\necho done"),
            ["python3", "echo"],
        )
        self.assertEqual(
            utility_names("cat <<-EOF\n\tprint(a, b)\n\tEOF\nrg x"),
            ["cat", "rg"],
        )
        self.assertEqual(utility_names("for a in one two; do echo \"$a\"; done"), ["echo"])
        self.assertEqual(utility_names("printf '<<PY'; echo done"), ["printf", "echo"])

    def test_ignore_is_presentation_only_and_directory_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-synthetic.jsonl"
            path.write_bytes(FIXTURE.read_bytes())
            result = scan(Path(directory))
            self.assertEqual(len(result["calls"]), 5)
            self.assertEqual(frequencies(result["calls"], {"grep"})["grep"], 0)
            self.assertEqual(result["calls"][0]["utilities"], ["find", "grep", "awk"])

    def test_cli_drilldown_and_hook_mapping(self):
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--utility", "awk"]), 0)
        self.assertIn("Calls invoking awk: 1", output.getvalue())
        self.assertIn("find . -type f | grep", output.getvalue())
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--format", "json", "--ignore", "grep"]), 0)
        exported = json.loads(output.getvalue())
        self.assertEqual(exported["frequencies"].get("grep"), None)
        self.assertEqual(exported["calls"][0]["utilities"], ["find", "grep", "awk"])
        self.assertEqual(exported["unclassified_code_mode_calls"], 1)
        hook = json.loads(HOOK.read_text())
        call = scan(FIXTURE)["calls"][0]
        self.assertEqual(hook["tool_use_id"], call["call_id"])
        self.assertEqual(hook["tool_input"]["command"], call["command"])

    def test_sessions_from_stdin_aggregates_listed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"rollout-{index}.jsonl" for index in range(2)]
            for path in paths:
                path.write_bytes(FIXTURE.read_bytes())
            path_list = b"\0".join(str(path).encode() for path in paths) + b"\0"
            with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(path_list))):
                with contextlib.redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(["--sessions-from", "-", "--min-count", "2"]), 0)
            self.assertIn("Files: 2  Tool calls: 10", output.getvalue())
            self.assertIn("     2  find", output.getvalue())

    def test_min_count_filters_ranking_not_calls(self):
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--min-count", "2"]), 0)
        self.assertIn("Tool calls: 5", output.getvalue())
        self.assertNotIn("     1  find", output.getvalue())
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--format", "json", "--min-count", "2"]), 0)
        exported = json.loads(output.getvalue())
        self.assertEqual(exported["frequencies"], {})
        self.assertEqual(len(exported["calls"]), 5)


if __name__ == "__main__":
    unittest.main()
