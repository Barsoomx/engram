import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from engram_cli.mcp_server import (
    CODEX_MCP_SCOPE_ENV,
    PROTOCOL_VERSION,
    handle_request,
    run_mcp_serve,
    run_server,
)


class StubTransport:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.calls: list[tuple[str, str, dict, dict | None, float]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append((method, url, headers, payload, timeout))

        return self.status, self.body


def fake_search(arguments: dict) -> str:
    return f"searched: {arguments.get('query')}"


def fake_context(arguments: dict) -> str:
    return f"context for {arguments.get('session_id')}"


def fake_link(arguments: dict) -> str:
    return f"linked {arguments.get('link_type')} -> {arguments.get('target')}"


def fake_observations(arguments: dict) -> str:
    return f"observations limit={arguments.get('limit')}"


def fake_memory_version(arguments: dict) -> str:
    return f"versioned {arguments.get('memory_id')} body={arguments.get('body')}"


def fake_feedback(arguments: dict) -> str:
    return f"feedback {arguments.get('action')} on {arguments.get('memory_id')}"


def build_tools() -> dict:
    return {
        "engram_search": fake_search,
        "engram_context": fake_context,
        "engram_memory_link": fake_link,
        "engram_observations": fake_observations,
        "engram_memory_version": fake_memory_version,
        "engram_memory_feedback": fake_feedback,
    }


class McpContractTests(unittest.TestCase):
    def test_initialize_returns_protocol_and_server_info(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, build_tools()
        )

        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(response["result"]["serverInfo"]["name"], "engram")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_initialized_notification_returns_none(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, build_tools()
        )

        self.assertIsNone(response)

    def test_tools_list_returns_all_tools(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, build_tools()
        )
        names = [tool["name"] for tool in response["result"]["tools"]]

        self.assertEqual(
            names,
            [
                "engram_search",
                "engram_context",
                "engram_memory_link",
                "engram_observations",
                "engram_memory_version",
                "engram_memory_feedback",
                "engram_memory_get",
                "engram_audit",
            ],
        )
        link_schema = response["result"]["tools"][2]["inputSchema"]
        self.assertEqual(["memory_id", "link_type", "target"], link_schema["required"])
        observations_schema = response["result"]["tools"][3]["inputSchema"]
        self.assertEqual([], observations_schema["required"])
        version_schema = response["result"]["tools"][4]["inputSchema"]
        self.assertEqual(["memory_id", "body"], version_schema["required"])
        tools_by_name = {tool["name"]: tool for tool in response["result"]["tools"]}
        memory_get_schema = tools_by_name["engram_memory_get"]["inputSchema"]
        self.assertEqual(["memory_id"], memory_get_schema["required"])
        self.assertEqual(
            {"memory_id", "project_id", "from_version", "to_version"},
            set(memory_get_schema["properties"]),
        )
        audit_schema = tools_by_name["engram_audit"]["inputSchema"]
        self.assertEqual([], audit_schema["required"])
        self.assertEqual(
            {
                "memory_id",
                "target_id",
                "target_type",
                "event_type",
                "correlation_id",
                "since",
                "until",
                "limit",
                "project_id",
            },
            set(audit_schema["properties"]),
        )

    def test_tools_list_all_eight_schemas_expose_optional_project_id(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 11, "method": "tools/list"}, build_tools()
        )
        tools = response["result"]["tools"]

        self.assertEqual(8, len(tools))
        for tool in tools:
            properties = tool["inputSchema"]["properties"]
            self.assertIn(
                "project_id", properties, f"{tool['name']} missing project_id"
            )
            self.assertEqual({"type": "string"}, properties["project_id"])
            self.assertNotIn("project_id", tool["inputSchema"]["required"])

    def test_tools_list_feedback_schema(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}, build_tools()
        )
        feedback = response["result"]["tools"][5]

        self.assertEqual("engram_memory_feedback", feedback["name"])
        self.assertEqual(
            ["memory_id", "action", "reason"], feedback["inputSchema"]["required"]
        )
        self.assertEqual(
            ["stale", "refuted"],
            feedback["inputSchema"]["properties"]["action"]["enum"],
        )

    def test_tools_list_descriptions_direct_proactive_search(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 12, "method": "tools/list"}, build_tools()
        )
        descriptions = {
            tool["name"]: tool["description"] for tool in response["result"]["tools"]
        }

        self.assertEqual(
            descriptions["engram_search"],
            "Step 1 - ALWAYS search project memory BEFORE starting any non-trivial task (bug fix, feature, refactor, debugging). Returns prior decisions, gotchas, incidents and architecture notes ranked by relevance. Call it when the user references past work ('did we', 'last time', 'as before'), names a subsystem, or reports an error you have not seen this session. Prefer short 2-4 word queries (symptom, component, error text).",
        )
        self.assertEqual(
            descriptions["engram_observations"],
            "Step 2 - list recent raw observations (prompts, tool activity, hook events) captured for the connected project. Use to corroborate a memory found via engram_search with ground-truth detail, or to audit what Engram captured.",
        )
        self.assertEqual(
            descriptions["engram_context"],
            "Re-request the memory context bundle that is injected at session start (recent and relevant approved memories for this project). Use after /clear or context compaction, or when the injected Engram context looks stale.",
        )
        self.assertEqual(
            descriptions["engram_memory_feedback"],
            "Step 3 - close the loop: the moment you discover an injected or retrieved memory is outdated or wrong, mark it stale or refuted with a reason. Clean memory improves every future session; do not silently ignore bad memory.",
        )
        self.assertEqual(
            descriptions["engram_memory_link"],
            "Attach a file/symbol/commit/issue link to an approved memory so future retrieval can find it by exact file path or symbol match.",
        )
        self.assertEqual(
            descriptions["engram_memory_version"],
            "Update an approved memory body, creating a new reviewed version. Use when you verified materially better information than what the memory states.",
        )

    def test_tools_list_read_tool_descriptions_are_verbatim_and_unnumbered(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 13, "method": "tools/list"}, build_tools()
        )
        descriptions = {
            tool["name"]: tool["description"] for tool in response["result"]["tools"]
        }

        self.assertEqual(
            descriptions["engram_memory_get"],
            "Read one memory in full by memory_id — the complete untruncated current body, version history, and links, not the 400-char session-start preview. Use before revising, linking, or giving feedback so you act on the full stored text. Kind, confidence, and conflict/stale/refuted validity come from engram_search, not this tool.",
        )
        self.assertEqual(
            descriptions["engram_audit"],
            "Show a memory's own recorded audit events — every transition committed against it (promotion, revise, refute, stale, restore, supersede, archive, a candidate merged into it, and a merge where it is the source), most recent first. Use to explain why a memory is in its current state. Not returned: the winner side of a supersession (a direct merge is recorded under the source memory; a candidate supersession that creates a new winner is recorded under the superseded loser), confidence-decay, and link add/remove events — those are keyed to a different audit target.",
        )
        self.assertFalse(descriptions["engram_memory_get"].startswith("Step "))
        self.assertFalse(descriptions["engram_audit"].startswith("Step "))

    def test_tools_call_search_returns_text_content(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "engram_search", "arguments": {"query": "auth"}},
            },
            build_tools(),
        )

        self.assertEqual("text", response["result"]["content"][0]["type"])
        self.assertIn("searched: auth", response["result"]["content"][0]["text"])

    def test_tools_call_scopes_codex_mcp_from_per_turn_workspace(self) -> None:
        captured: list[dict] = []

        def capture(arguments: dict) -> str:
            captured.append(arguments)

            return "ok"

        tools = build_tools()
        tools["engram_search"] = capture
        request = {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "engram_search",
                "arguments": {"query": "auth"},
                "_meta": {
                    "threadId": "thread-1",
                    "x-codex-turn-metadata": {
                        "session_id": "thread-1",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "workspaces": {
                            "/workspace/project": {
                                "associated_remote_urls": {
                                    "origin": "https://example.test/acme/project.git"
                                }
                            }
                        },
                    },
                },
            },
        }

        with mock.patch(
            "engram_cli.mcp_server.git_remote_url",
            return_value="https://example.test/acme/project.git",
        ) as m_remote:
            response = handle_request(request, tools)

        self.assertEqual("ok", response["result"]["content"][0]["text"])
        self.assertEqual(
            "https://example.test/acme/project.git",
            captured[0]["__engram_repository_url"],
        )
        m_remote.assert_called_once_with("/workspace/project")

    def test_tools_call_accepts_json_string_codex_turn_metadata(self) -> None:
        captured: list[dict] = []
        tools = build_tools()
        tools["engram_search"] = lambda arguments: captured.append(arguments) or "ok"
        turn_metadata = json.dumps(
            {
                "session_id": "thread-1",
                "thread_id": "thread-1",
                "workspaces": {"/workspace/project": {}},
            }
        )

        with mock.patch(
            "engram_cli.mcp_server.git_remote_url",
            return_value="git@example.test:acme/project.git",
        ):
            handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 32,
                    "method": "tools/call",
                    "params": {
                        "name": "engram_search",
                        "arguments": {"query": "auth"},
                        "_meta": {
                            "threadId": "thread-1",
                            "x-codex-turn-metadata": turn_metadata,
                        },
                    },
                },
                tools,
            )

        self.assertEqual(
            "git@example.test:acme/project.git",
            captured[0]["__engram_repository_url"],
        )

    def test_tools_call_fails_closed_for_ambiguous_or_mismatched_codex_scope(
        self,
    ) -> None:
        captured: list[dict] = []
        tools = build_tools()
        tools["engram_search"] = lambda arguments: captured.append(arguments) or "ok"
        metadata = {
            "session_id": "different-thread",
            "thread_id": "different-thread",
            "workspaces": {"/workspace/a": {}, "/workspace/b": {}},
        }

        with (
            mock.patch("engram_cli.mcp_server.git_remote_url") as m_remote,
            mock.patch.dict(os.environ, {CODEX_MCP_SCOPE_ENV: "1"}),
        ):
            handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 33,
                    "method": "tools/call",
                    "params": {
                        "name": "engram_search",
                        "arguments": {
                            "query": "auth",
                            "__engram_repository_url": "https://attacker.invalid/repo",
                        },
                        "_meta": {
                            "threadId": "thread-1",
                            "x-codex-turn-metadata": metadata,
                        },
                    },
                },
                tools,
            )

        self.assertEqual("", captured[0]["__engram_repository_url"])
        m_remote.assert_not_called()

    def test_tools_call_context_returns_text_content(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "engram_context",
                    "arguments": {"session_id": "sess-1"},
                },
            },
            build_tools(),
        )

        self.assertIn("context for sess-1", response["result"]["content"][0]["text"])

    def test_tools_call_memory_link_returns_text_content(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "engram_memory_link",
                    "arguments": {
                        "memory_id": "mem-1",
                        "link_type": "file",
                        "target": "a.py",
                    },
                },
            },
            build_tools(),
        )

        self.assertIn("linked file -> a.py", response["result"]["content"][0]["text"])

    def test_tools_call_observations_returns_text_content(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "engram_observations", "arguments": {"limit": 3}},
            },
            build_tools(),
        )

        self.assertIn("observations limit=3", response["result"]["content"][0]["text"])

    def test_tools_call_memory_version_returns_text_content(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "engram_memory_version",
                    "arguments": {"memory_id": "mem-1", "body": "new body"},
                },
            },
            build_tools(),
        )

        self.assertIn(
            "versioned mem-1 body=new body", response["result"]["content"][0]["text"]
        )

    def test_unknown_tool_returns_error(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            },
            build_tools(),
        )

        self.assertEqual(-32601, response["error"]["code"])

    def test_unknown_method_returns_error(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 7, "method": "frog"}, build_tools()
        )

        self.assertEqual(-32601, response["error"]["code"])

    def test_tool_exception_returns_error_and_loop_survives(self) -> None:
        def broken(arguments: dict) -> str:
            raise RuntimeError("boom")

        tools = build_tools()
        tools["engram_search"] = broken
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "engram_search", "arguments": {"query": "x"}},
                },
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n",
        )
        stdout = io.StringIO()
        run_server(tools, stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(2, len(lines))
        self.assertEqual(-32603, lines[0]["error"]["code"])
        self.assertEqual(1, lines[0]["id"])
        self.assertIn("tools", lines[1]["result"])

    def test_run_server_handles_ndjson_round_trip(self) -> None:
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "engram_search", "arguments": {"query": "auth"}},
                },
            )
            + "\n",
        )
        stdout = io.StringIO()
        run_server(build_tools(), stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(3, len(lines))
        self.assertEqual(PROTOCOL_VERSION, lines[0]["result"]["protocolVersion"])
        self.assertEqual(8, len(lines[1]["result"]["tools"]))
        self.assertIn("searched: auth", lines[2]["result"]["content"][0]["text"])

    def test_run_server_skips_malformed_lines(self) -> None:
        stdin = io.StringIO(
            "not json\n"
            + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
        )
        stdout = io.StringIO()
        run_server(build_tools(), stdin=stdin, stdout=stdout)

        self.assertEqual(1, len(stdout.getvalue().splitlines()))

    def test_non_dict_params_returns_error_and_loop_survives(self) -> None:
        stdin = io.StringIO(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"},
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n",
        )
        stdout = io.StringIO()
        run_server(build_tools(), stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(2, len(lines))
        self.assertEqual(-32601, lines[0]["error"]["code"])
        self.assertEqual(1, lines[0]["id"])
        self.assertIn("tools", lines[1]["result"])


class RunMcpServeTests(unittest.TestCase):
    def test_run_mcp_serve_wires_build_tools_and_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="engram-mcp-serve-tests-") as config_dir:
            args = Namespace(config_dir=config_dir)
            stdin = io.StringIO(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                + "\n",
            )
            stdout = io.StringIO()
            exit_code = run_mcp_serve(args, stdin, stdout, StubTransport())
            lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(0, exit_code)
        self.assertEqual(2, len(lines))
        self.assertEqual(8, len(lines[1]["result"]["tools"]))


if __name__ == "__main__":
    unittest.main()
