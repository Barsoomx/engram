from __future__ import annotations

import io
import unittest
import urllib.error
from unittest.mock import patch

from engram_cli.http import admin_post, get_json, post_json, urllib_transport


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> 'FakeResponse':
        return self

    def __exit__(self, *args: object) -> None:
        return None


class UrllibTransportRetryTests(unittest.TestCase):
    def test_503_then_200_retries_once_then_succeeds_via_post_json(self) -> None:
        responses: list[object] = [
            urllib.error.HTTPError('u', 503, 'unavailable', {}, io.BytesIO(b'{}')),
            FakeResponse(200, b'{"ok": true}'),
        ]

        def m_urlopen(request: object, timeout: float) -> object:
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result

            return result

        with patch('engram_cli.http.urllib.request.urlopen', side_effect=m_urlopen) as mock_urlopen, \
                patch('engram_cli.http.time.sleep') as mock_sleep:
            status, body = post_json(
                transport=urllib_transport,
                server_url='https://example.test',
                path='/v1/hooks/session-start',
                api_key='key',
                payload={},
            )

        self.assertEqual(2, mock_urlopen.call_count)
        self.assertEqual(1, mock_sleep.call_count)
        self.assertEqual(200, status)
        self.assertEqual({'ok': True}, body)

    def test_502_and_504_retried(self) -> None:
        for code in (502, 504):
            with self.subTest(code=code):
                def m_urlopen(request: object, timeout: float, code: int = code) -> object:
                    raise urllib.error.HTTPError('u', code, 'err', {}, io.BytesIO(b'{}'))

                with patch('engram_cli.http.urllib.request.urlopen', side_effect=m_urlopen) as mock_urlopen, \
                        patch('engram_cli.http.time.sleep') as mock_sleep:
                    status, body = urllib_transport(
                        'GET', 'https://example.test/x', {}, None, 2.0, max_attempts=2
                    )

                self.assertEqual(2, mock_urlopen.call_count)
                self.assertEqual(1, mock_sleep.call_count)
                self.assertEqual(code, status)
                self.assertEqual({}, body)

    def test_500_and_4xx_not_retried(self) -> None:
        for code in (500, 400, 401):
            with self.subTest(code=code):
                def m_urlopen(request: object, timeout: float, code: int = code) -> object:
                    raise urllib.error.HTTPError('u', code, 'err', {}, io.BytesIO(b'{}'))

                with patch('engram_cli.http.urllib.request.urlopen', side_effect=m_urlopen) as mock_urlopen, \
                        patch('engram_cli.http.time.sleep') as mock_sleep:
                    status, body = urllib_transport(
                        'GET', 'https://example.test/x', {}, None, 2.0, max_attempts=2
                    )

                self.assertEqual(1, mock_urlopen.call_count)
                self.assertEqual(0, mock_sleep.call_count)
                self.assertEqual(code, status)

    def test_persistent_timeout_returns_503_after_two_attempts(self) -> None:
        def m_urlopen(request: object, timeout: float) -> object:
            raise TimeoutError('timed out')

        with patch('engram_cli.http.urllib.request.urlopen', side_effect=m_urlopen) as mock_urlopen, \
                patch('engram_cli.http.time.sleep') as mock_sleep:
            status, body = urllib_transport(
                'GET', 'https://example.test/x', {}, None, 2.0, max_attempts=2
            )

        self.assertEqual(2, mock_urlopen.call_count)
        self.assertEqual(1, mock_sleep.call_count)
        self.assertEqual(503, status)
        self.assertEqual(
            {'code': 'server_unavailable', 'detail': 'Server is unavailable'}, body
        )

    def test_post_json_default_timeout_is_thirty_seconds(self) -> None:
        captured: list[float] = []

        def m_urlopen(request: object, timeout: float) -> object:
            captured.append(timeout)

            return FakeResponse(200, b'{}')

        with patch('engram_cli.http.urllib.request.urlopen', side_effect=m_urlopen):
            post_json(
                transport=urllib_transport,
                server_url='https://example.test',
                path='/v1/search/',
                api_key='key',
                payload={},
            )

        self.assertEqual([30.0], captured)

    def test_get_json_default_timeout_is_thirty_seconds(self) -> None:
        captured: list[float] = []

        def m_urlopen(request: object, timeout: float) -> object:
            captured.append(timeout)

            return FakeResponse(200, b'{}')

        with patch('engram_cli.http.urllib.request.urlopen', side_effect=m_urlopen):
            get_json(
                transport=urllib_transport,
                server_url='https://example.test',
                path='/v1/observations/',
                api_key='key',
            )

        self.assertEqual([30.0], captured)

    def test_admin_post_persistent_503_makes_exactly_one_attempt(self) -> None:
        def m_urlopen(request: object, timeout: float) -> object:
            raise urllib.error.HTTPError('u', 503, 'unavailable', {}, io.BytesIO(b'{}'))

        with patch('engram_cli.http.urllib.request.urlopen', side_effect=m_urlopen) as mock_urlopen, \
                patch('engram_cli.http.time.sleep') as mock_sleep:
            status, body = admin_post(
                transport=urllib_transport,
                server_url='https://example.test',
                path='/v1/admin/api-keys/',
                drf_token='token',
                payload={'name': 'engram-cli'},
            )

        self.assertEqual(1, mock_urlopen.call_count)
        self.assertEqual(0, mock_sleep.call_count)
        self.assertEqual(503, status)
        self.assertEqual({}, body)


if __name__ == '__main__':
    unittest.main()
