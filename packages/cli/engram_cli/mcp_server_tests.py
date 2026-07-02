import io
import json
import unittest

from engram_cli.mcp_server import PROTOCOL_VERSION, handle_request, run_server


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
            ],
        )
        link_schema = response["result"]["tools"][2]["inputSchema"]
        self.assertEqual(["memory_id", "link_type", "target"], link_schema["required"])
        observations_schema = response["result"]["tools"][3]["inputSchema"]
        self.assertEqual([], observations_schema["required"])
        version_schema = response["result"]["tools"][4]["inputSchema"]
        self.assertEqual(["memory_id", "body"], version_schema["required"])

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
        self.assertEqual(6, len(lines[1]["result"]["tools"]))
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


if __name__ == "__main__":
    unittest.main()
