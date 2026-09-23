"""Inspect shell tool calls in completed Codex rollout JSONL files.

Format references (Codex source, commit 8d32abcd01):
  codex-rs/history/src/lib.rs (RolloutLine)
  codex-rs/history/src/rollout_payload.rs (response_item envelope)
  codex-rs/protocol/src/models.rs (function calls and outputs)
  codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs (cmd -> Bash command)
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any


CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call"}
OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}
SEPARATORS = {"|", "||", "&", "&&", ";", "(", ")"}
REDIRECTIONS = {"<", ">", "<<", ">>", "<&", ">&", "<<<"}
SHELL_NAMES = {"bash", "sh", "zsh", "dash", "ksh"}
SIMPLE_WRAPPERS = {"sudo", "env", "nohup", "command", "builtin"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=.*$", re.DOTALL)


def without_heredoc_bodies(command: str) -> str:
    """Keep shell command lines, but skip here-document data and terminators."""
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote: str | None = None
    for line in command.splitlines(keepends=True):
        if pending:
            delimiter, strip_tabs = pending[0]
            end = line.rstrip("\r\n")
            if (end.lstrip("\t") if strip_tabs else end) == delimiter:
                pending.pop(0)
                if not pending:
                    kept.append(";")  # The next line starts a new shell command.
            continue
        kept.append(line)
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if char == "\\" and quote == '"':
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char == "\\":
                index += 2
                continue
            elif char in {"'", '"'}:
                quote = char
            elif line.startswith("<<", index) and not line.startswith("<<<", index):
                end = index + 2
                strip_tabs = end < len(line) and line[end] == "-"
                end += strip_tabs
                while end < len(line) and line[end] in " \t":
                    end += 1
                marker_quote = line[end] if end < len(line) and line[end] in {"'", '"'} else None
                if marker_quote:
                    end += 1
                match = re.match(r"[A-Za-z_][A-Za-z_0-9]*", line[end:])
                if match:
                    delimiter = match.group()
                    end += len(delimiter)
                    if not marker_quote or (end < len(line) and line[end] == marker_quote):
                        pending.append((delimiter, strip_tabs))
                        index = end + int(marker_quote is not None)
                        continue
            index += 1
    return "".join(kept)


def utility_names(command: str, depth: int = 0) -> list[str]:
    """Find command-position executables, including pipeline stages.

    This deliberately isn't a complete shell grammar. It avoids counting words
    inside quoted arguments, and descends into common `sh -c` wrappers.
    """
    if depth > 3:
        return []
    lexer = shlex.shlex(without_heredoc_bodies(command), posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return []

    found: list[str] = []
    expecting_command = True
    skip_next = False
    wrapper = False
    wrapper_option_value = False
    loop_header = False
    for index, token in enumerate(tokens):
        if loop_header:
            if token == "do":
                loop_header = False
                expecting_command = True
            continue
        if token in SEPARATORS:
            expecting_command = True
            skip_next = False
            wrapper = False
            wrapper_option_value = False
            continue
        if token in REDIRECTIONS:
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        if not expecting_command:
            continue
        if wrapper_option_value:
            wrapper_option_value = False
            continue
        if token == "!" or ASSIGNMENT.match(token):
            continue
        if wrapper and token.startswith("-"):
            if token in {"-u", "--user", "-g", "--group", "-p", "--prompt"}:
                wrapper_option_value = True
            continue
        if token == "for":
            loop_header = True
            continue
        if token in {"if", "then", "do", "else", "while", "until"}:
            continue
        if token in {"fi", "done"}:
            expecting_command = False
            continue

        name = Path(token).name
        if name:
            found.append(name)
        expecting_command = False
        if name in SIMPLE_WRAPPERS:
            expecting_command = True
            wrapper = True
            continue
        wrapper = False

        if name in SHELL_NAMES:
            # A quoted script remains one token after shlex parsing.
            for option_index in range(index + 1, len(tokens) - 1):
                option = tokens[option_index]
                if option in SEPARATORS:
                    break
                if option.startswith("-") and "c" in option[1:]:
                    found.extend(utility_names(tokens[option_index + 1], depth + 1))
                    break
    return found


def utilities_for_argv(argv: list[str]) -> list[str]:
    """Skip Codex's launcher shell when an execution record wraps a script."""
    if argv and Path(argv[0]).name in SHELL_NAMES:
        for index, option in enumerate(argv[1:], 1):
            if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
                if index + 1 < len(argv):
                    return utility_names(argv[index + 1])
                break
    return utility_names(shlex.join(argv))


def rollout_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob("rollout-*.jsonl"))
    raise FileNotFoundError(path)


def record(path: Path, line_number: int, raw_line: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": str(path),
        "line": line_number,
        "timestamp": value.get("timestamp"),
        "raw_line": raw_line,
        "raw": value,
    }


def command_for(payload: dict[str, Any], arguments: Any) -> str | None:
    kind = payload.get("type")
    if kind == "function_call" and payload.get("namespace") is None:
        if payload.get("name") in {"exec_command", "shell"} and isinstance(arguments, dict):
            cmd = arguments.get("cmd")
            return cmd if isinstance(cmd, str) else None
    if kind == "local_shell_call":
        action = payload.get("action")
        if isinstance(action, dict) and action.get("type") == "exec":
            argv = action.get("command")
            if isinstance(argv, list) and all(isinstance(part, str) for part in argv):
                return shlex.join(argv)
    return None


def scan_file(path: Path) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    history_mode = "unknown"

    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            raw_line = line.rstrip("\r\n")
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as error:
                errors.append({"source": str(path), "line": line_number, "error": str(error)})
                continue
            if not isinstance(value, dict):
                errors.append({"source": str(path), "line": line_number, "error": "JSON object expected"})
                continue
            payload = value.get("payload")
            if not isinstance(payload, dict):
                continue
            outer_type = value.get("type")
            if outer_type == "session_meta" and session_id is None:
                mode = payload.get("history_mode", "legacy")
                history_mode = mode if isinstance(mode, str) and mode in {"legacy", "paginated"} else "other"
                identifier = payload.get("session_id") or payload.get("id")
                if isinstance(identifier, str):
                    session_id = identifier
                identifier = payload.get("id")
                if isinstance(identifier, str):
                    thread_id = identifier
            elif outer_type == "event_msg":
                if payload.get("type") == "item_completed":
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") in {"CommandExecution", "command_execution"}:
                        execution = record(path, line_number, raw_line, value)
                        execution["session_id"] = session_id
                        execution["thread_id"] = thread_id
                        execution["turn_id"] = payload.get("turn_id", turn_id)
                        execution["call_id"] = item.get("id") if isinstance(item.get("id"), str) else None
                        execution["tool_name"] = "command_execution"
                        argv = item.get("command")
                        valid_argv = isinstance(argv, list) and bool(argv) and all(
                            isinstance(part, str) for part in argv
                        )
                        execution["command"] = shlex.join(argv) if valid_argv else None
                        execution["utilities"] = utilities_for_argv(argv) if valid_argv else []
                        source_name = item.get("source", "agent")
                        execution["source_kind"] = source_name if isinstance(source_name, str) and source_name in {
                            "agent", "user_shell", "unified_exec_startup", "unified_exec_interaction"
                        } else "other"
                        executions.append(execution)
                if payload.get("type") in {"task_started", "turn_started"}:
                    identifier = payload.get("turn_id")
                    turn_id = identifier if isinstance(identifier, str) else None
                elif payload.get("type") in {"task_complete", "turn_complete", "turn_aborted"}:
                    turn_id = None
            elif outer_type == "response_item":
                kind = payload.get("type")
                if kind not in CALL_TYPES | OUTPUT_TYPES:
                    continue
                entry = record(path, line_number, raw_line, value)
                entry["session_id"] = session_id
                entry["thread_id"] = thread_id
                entry["turn_id"] = turn_id
                entry["call_id"] = payload.get("call_id")
                if kind in CALL_TYPES:
                    entry["tool_name"] = payload.get("name") if kind != "local_shell_call" else "local_shell_call"
                    arguments: Any = None
                    if kind == "function_call":
                        raw_arguments = payload.get("arguments")
                        if isinstance(raw_arguments, str):
                            try:
                                arguments = json.loads(raw_arguments)
                            except json.JSONDecodeError as error:
                                entry["argument_error"] = str(error)
                        else:
                            entry["argument_error"] = "string arguments expected"
                    entry["arguments"] = arguments
                    entry["command"] = command_for(payload, arguments)
                    entry["utilities"] = utility_names(entry["command"]) if entry["command"] else []
                    entry["outputs"] = []
                    calls.append(entry)
                else:
                    outputs.append(entry)

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        if isinstance(call["call_id"], str):
            by_id[call["call_id"]].append(call)
    unmatched: list[dict[str, Any]] = []
    for output in outputs:
        candidates = by_id.get(output["call_id"], []) if isinstance(output["call_id"], str) else []
        # Retain an ambiguous output separately rather than attach it to the wrong call.
        if len(candidates) == 1:
            candidates[0]["outputs"].append(output)
        else:
            unmatched.append(output)
    direct_shell_ids = {call["call_id"] for call in calls if call["command"] is not None and isinstance(call["call_id"], str)}
    for execution in executions:
        execution["duplicate_direct"] = execution["call_id"] in direct_shell_ids
    return {
        "calls": calls, "executions": executions, "unmatched_outputs": unmatched, "errors": errors,
        "history_mode": history_mode,
    }


def scan_paths(paths: list[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "files": [str(item) for item in paths], "calls": [], "executions": [],
        "unmatched_outputs": [], "errors": [], "history_modes": Counter(),
    }
    for item in paths:
        partial = scan_file(item)
        for key in ("calls", "executions", "unmatched_outputs", "errors"):
            result[key].extend(partial[key])
        result["history_modes"][partial["history_mode"]] += 1
    return result


def scan(path: Path) -> dict[str, Any]:
    return scan_paths(rollout_paths(path))


def session_paths_from(source: str) -> list[Path]:
    contents = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    return [Path(os.fsdecode(item)) for item in contents.split(b"\0") if item]


def frequencies(calls: list[dict[str, Any]], ignored: set[str]) -> Counter[str]:
    return Counter(name for call in calls for name in call["utilities"] if name not in ignored)


def ranked_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Use recorded executions absent from direct calls, but not user shell activity."""
    return result["calls"] + [
        execution for execution in result["executions"]
        if execution["command"] and not execution["duplicate_direct"]
        and execution["source_kind"] in {"agent", "unified_exec_startup"}
    ]


def code_mode_count(calls: list[dict[str, Any]]) -> int:
    return sum(
        call["raw"]["payload"].get("type") == "custom_tool_call"
        and call["tool_name"] == "exec"
        for call in calls
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, help="one completed rollout JSONL or a directory to scan recursively")
    parser.add_argument("--sessions-from", metavar="FILE", help="read NUL-separated rollout paths from FILE; use - for stdin")
    parser.add_argument("--format", choices=("summary", "calls", "json", "coverage"), default="summary")
    parser.add_argument("--ignore", action="append", default=[], metavar="UTILITY", help="hide a utility from the frequency view; repeatable")
    parser.add_argument("--min-count", type=int, default=1, metavar="N", help="show only utilities invoked at least N times (default: 1)")
    parser.add_argument("--utility", metavar="UTILITY", help="show calls invoking this utility")
    args = parser.parse_args(argv)
    if (args.path is None) == (args.sessions_from is None):
        parser.error("provide either a rollout path or --sessions-from, not both")
    if args.min_count < 1:
        parser.error("--min-count must be at least 1")
    ignored = {name for value in args.ignore for name in value.split(",") if name}
    try:
        result = scan(args.path) if args.path is not None else scan_paths(session_paths_from(args.sessions_from))
    except (OSError, UnicodeError) as error:
        parser.exit(2, f"observer: {error}\n")
    ranked = ranked_records(result)
    visible_frequencies = [
        (name, count)
        for name, count in frequencies(ranked, ignored).most_common()
        if count >= args.min_count
    ]

    if args.format == "coverage":
        print(f"Files: {len(result['files'])}")
        modes = result["history_modes"]
        print(f"History modes: legacy={modes['legacy']} paginated={modes['paginated']} other/unknown={modes['other'] + modes['unknown']}")
        print(f"Direct shell calls parsed: {sum(call['command'] is not None for call in result['calls'])}")
        executions = result["executions"]
        print(f"Completed command items: {len(executions)}")
        print(f"  with command argv: {sum(execution['command'] is not None for execution in executions)}")
        print(f"  matching direct shell call IDs: {sum(execution['duplicate_direct'] for execution in executions)}")
        print(f"  additional commands used in ranking: {len(ranked) - len(result['calls'])}")
        sources = Counter(execution["source_kind"] for execution in executions)
        print(f"  sources: agent={sources['agent']} user_shell={sources['user_shell']} startup={sources['unified_exec_startup']} interaction={sources['unified_exec_interaction']} other={sources['other']}")
        print(f"Code-mode exec cells (JavaScript not parsed): {code_mode_count(result['calls'])}")
    elif args.format == "json":
        result["frequencies"] = dict(visible_frequencies)
        result["ignored_utilities"] = sorted(ignored)
        result["code_mode_exec_cells"] = code_mode_count(result["calls"])
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
    elif args.format == "calls":
        for call in sorted(ranked, key=lambda entry: (entry["source"], entry["line"])):
            if args.utility and args.utility not in call["utilities"]:
                continue
            location = f"{call['source']}:{call['line']}"
            command = call["command"] or "(non-shell tool or unreadable command)"
            if call["tool_name"] == "command_execution":
                status = call["raw"]["payload"]["item"].get("status", "unknown")
                print(f"{location}  {call['timestamp']}  {call['tool_name']}  {call['call_id']}  status={status}\n    {command}")
            else:
                print(f"{location}  {call['timestamp']}  {call['tool_name']}  {call['call_id']}  outputs={len(call['outputs'])}\n    {command}")
    else:
        direct_shell_calls = sum(call["command"] is not None for call in result["calls"])
        additional_commands = len(ranked) - len(result["calls"])
        print(f"Files: {len(result['files'])}  Tool calls: {len(result['calls'])}  Shell commands: {direct_shell_calls + additional_commands}")
        if additional_commands:
            print(f"  Direct calls: {direct_shell_calls}  Additional recorded commands: {additional_commands}")
        print("Utility invocations (approximate):")
        for name, count in visible_frequencies:
            print(f"{count:>6}  {name}")
        print(f"Unmatched outputs: {len(result['unmatched_outputs'])}  Parse errors: {len(result['errors'])}")
        if code_mode_count(result["calls"]):
            print(f"Code-mode exec cells (JavaScript not parsed): {code_mode_count(result['calls'])}")
        if args.utility:
            matches = sorted(
                (call for call in ranked if args.utility in call["utilities"]),
                key=lambda entry: (entry["source"], entry["line"]),
            )
            print(f"Calls invoking {args.utility}: {len(matches)}")
            for call in matches:
                print(f"{call['source']}:{call['line']}  {call['timestamp']}  outputs={len(call.get('outputs', []))}\n    {call['command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
