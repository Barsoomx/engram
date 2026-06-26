import io
import json
import unittest

from engram_mcp.server import PROTOCOL_VERSION, handle_request, run_server


def fake_search(arguments: dict) -> str:
    return f"searched: {arguments.get('query')}"


class McpContractTests(unittest.TestCase):
    def test_initialize_returns_protocol_and_server_info(self) -> None:
        response = handle_request({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}, fake_search)

        self.assertEqual(response['id'], 1)
        self.assertEqual(response['result']['protocolVersion'], PROTOCOL_VERSION)
        self.assertEqual(response['result']['serverInfo']['name'], 'engram')
        self.assertIn('tools', response['result']['capabilities'])

    def test_initialized_notification_returns_none(self) -> None:
        response = handle_request({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, fake_search)

        self.assertIsNone(response)

    def test_tools_list_returns_engram_search(self) -> None:
        response = handle_request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, fake_search)
        names = [tool['name'] for tool in response['result']['tools']]

        self.assertEqual(names, ['engram_search'])
        schema = response['result']['tools'][0]['inputSchema']
        self.assertIn('query', schema['properties'])
        self.assertEqual(['query'], schema['required'])

    def test_tools_call_search_returns_text_content(self) -> None:
        response = handle_request(
            {
                'jsonrpc': '2.0',
                'id': 3,
                'method': 'tools/call',
                'params': {'name': 'engram_search', 'arguments': {'query': 'auth'}},
            },
            fake_search,
        )

        self.assertEqual('text', response['result']['content'][0]['type'])
        self.assertIn('searched: auth', response['result']['content'][0]['text'])

    def test_unknown_tool_returns_error(self) -> None:
        response = handle_request(
            {
                'jsonrpc': '2.0',
                'id': 4,
                'method': 'tools/call',
                'params': {'name': 'nope', 'arguments': {}},
            },
            fake_search,
        )

        self.assertEqual(-32601, response['error']['code'])

    def test_unknown_method_returns_error(self) -> None:
        response = handle_request({'jsonrpc': '2.0', 'id': 5, 'method': 'frog'}, fake_search)

        self.assertEqual(-32601, response['error']['code'])

    def test_run_server_handles_ndjson_round_trip(self) -> None:
        stdin = io.StringIO(
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}) + '\n'
            + json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n'
            + json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}) + '\n'
            + json.dumps(
                {
                    'jsonrpc': '2.0',
                    'id': 3,
                    'method': 'tools/call',
                    'params': {'name': 'engram_search', 'arguments': {'query': 'auth'}},
                },
            )
            + '\n',
        )
        stdout = io.StringIO()
        run_server(fake_search, stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(3, len(lines))
        self.assertEqual(PROTOCOL_VERSION, lines[0]['result']['protocolVersion'])
        self.assertEqual('engram_search', lines[1]['result']['tools'][0]['name'])
        self.assertIn('searched: auth', lines[2]['result']['content'][0]['text'])

    def test_run_server_skips_malformed_lines(self) -> None:
        stdin = io.StringIO('not json\n' + json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n')
        stdout = io.StringIO()
        run_server(fake_search, stdin=stdin, stdout=stdout)

        self.assertEqual(1, len(stdout.getvalue().splitlines()))


if __name__ == '__main__':
    unittest.main()
