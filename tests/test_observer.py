import contextlib
from io import StringIO
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from observer import code_mode_count, frequencies, main, ranked_records, scan, spark_bar, utilities_for_argv, utility_names


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
        self.assertEqual(utilities_for_argv(["/bin/zsh", "-lc", "find . | rg x"]), ["find", "rg"])

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
        self.assertEqual(exported["code_mode_exec_cells"], 1)
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
            self.assertIn("Files: 2  Tool calls: 10  Shell commands: 4\nDirect calls: 4  Additional recorded commands: 0\n\nUtility invocations (approximate):\nUtility  Count   Share\n", output.getvalue())
            self.assertIn("find         2", output.getvalue())

    def test_min_count_filters_ranking_not_calls(self):
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--min-count", "2"]), 0)
        self.assertIn("\n\nUtility invocations (approximate):\nUtility  Count   Share\n", output.getvalue())
        self.assertNotIn("find         1", output.getvalue())
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--format", "json", "--min-count", "2"]), 0)
        exported = json.loads(output.getvalue())
        self.assertEqual(exported["frequencies"], {})
        self.assertEqual(len(exported["calls"]), 5)

    def test_share_uses_visible_utilities_and_bars_scale_to_leader(self):
        self.assertEqual(spark_bar(2165, 2165), "▓" * 12)
        self.assertEqual(spark_bar(645, 2165), "▓" * 4)
        self.assertEqual(spark_bar(94, 2165), "▌")
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--ignore", "find"]), 0)
        self.assertIn("grep         1   33.3%", output.getvalue())
        self.assertNotIn("find         1", output.getvalue())

    def test_exclude_file_filters_ranking_not_records(self):
        with tempfile.TemporaryDirectory() as directory:
            excluded = Path(directory) / "excluded.txt"
            excluded.write_text("# personal ranking preferences\n\n grep \nawk\n")
            with contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(main([str(FIXTURE), "--exclude-file", str(excluded), "--ignore", "find"]), 0)
            self.assertNotIn("grep         1", output.getvalue())
            self.assertNotIn("awk          1", output.getvalue())
            self.assertNotIn("find         1", output.getvalue())
            with contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(main([str(FIXTURE), "--format", "json", "--exclude-file", str(excluded)]), 0)
            exported = json.loads(output.getvalue())
            self.assertEqual(exported["ignored_utilities"], ["awk", "grep"])
            self.assertEqual(exported["calls"][0]["utilities"], ["find", "grep", "awk"])

    def test_coverage_is_aggregate_only(self):
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main([str(FIXTURE), "--format", "coverage"]), 0)
        report = output.getvalue()
        self.assertIn("History modes: legacy=1 paginated=0 other/unknown=0", report)
        self.assertIn("Files: 1  Tool calls: 5  Shell commands: 2", report)
        self.assertIn("Direct calls: 2  Additional recorded commands: 0", report)
        self.assertIn("Completed command items: 0", report)
        self.assertIn("Unmatched outputs: 1  Parse errors: 0", report)
        self.assertIn("Code-mode exec cells (JavaScript not parsed): 1", report)
        self.assertNotIn("sed -n 1,5p file", report)
        self.assertNotIn(str(FIXTURE), report)

    def test_coverage_counts_paginated_command_items_without_printing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-paginated.jsonl"
            records = [
                {"type": "session_meta", "payload": {"id": "synthetic", "history_mode": "paginated"}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                    "arguments": json.dumps({"cmd": "printf same"}), "call_id": "direct"}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "CommandExecution", "id": "direct", "command": ["bash", "-lc", "printf same"],
                }}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "CommandExecution", "id": "nested", "command": ["bash", "-lc", "rg synthetic-example"],
                }}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "CommandExecution", "id": "user", "source": "user_shell",
                    "command": ["bash", "-lc", "grep user-example"],
                }}},
            ]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
            with contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(main([str(path), "--format", "coverage"]), 0)
            self.assertIn("History modes: legacy=0 paginated=1 other/unknown=0", output.getvalue())
            self.assertIn("Completed command items: 3", output.getvalue())
            self.assertIn("with command argv: 3", output.getvalue())
            self.assertIn("matching direct shell call IDs: 1", output.getvalue())
            self.assertIn("additional commands used in ranking: 1", output.getvalue())
            self.assertNotIn("synthetic-example", output.getvalue())
            self.assertNotIn(str(path), output.getvalue())

            result = scan(path)
            self.assertEqual(len(result["executions"]), 3)
            self.assertEqual(result["executions"][1]["raw"], json.loads(result["executions"][1]["raw_line"]))
            self.assertTrue(result["executions"][0]["duplicate_direct"])
            self.assertFalse(result["executions"][1]["duplicate_direct"])
            self.assertEqual(frequencies(ranked_records(result), set())["printf"], 1)
            self.assertEqual(frequencies(ranked_records(result), set())["rg"], 1)
            self.assertEqual(frequencies(ranked_records(result), set())["grep"], 0)
            self.assertEqual(frequencies(ranked_records(result), set())["bash"], 0)
            with contextlib.redirect_stdout(StringIO()) as calls_output:
                self.assertEqual(main([str(path), "--format", "calls"]), 0)
            self.assertIn("command_execution  nested  status=unknown", calls_output.getvalue())
            self.assertNotIn("command_execution  direct", calls_output.getvalue())
            self.assertNotIn("command_execution  user", calls_output.getvalue())
            with contextlib.redirect_stdout(StringIO()) as json_output:
                self.assertEqual(main([str(path), "--format", "json"]), 0)
            exported = json.loads(json_output.getvalue())
            self.assertEqual(len(exported["executions"]), 3)
            self.assertEqual(exported["frequencies"]["rg"], 1)
            self.assertNotIn("grep", exported["frequencies"])


if __name__ == "__main__":
    unittest.main()
