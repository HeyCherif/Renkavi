"""Transport failures must preserve useful diagnostics without leaking payloads."""
import io
import json
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from backend.ai import local_llm


class LocalLLMErrorTests(unittest.TestCase):
    config = {'base_url': 'http://127.0.0.1:6598', 'model': 'test'}

    def invoke(self, method):
        if method == 'complete':
            return local_llm.complete(self.config, 'private dialogue')
        return local_llm.chat(self.config, {'model': 'test', 'input': 'private dialogue'})

    def failure(self, body, status=400):
        return urllib.error.HTTPError(self.config['base_url'], status, 'Bad Request', {}, io.BytesIO(body))

    def test_both_endpoints_report_http_status_and_server_diagnostic(self):
        for method in ('complete', 'chat'):
            with self.subTest(method=method):
                error = self.failure(json.dumps({'error': {'message': 'Invalid parameter top_k'}}).encode())
                with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(Exception) as caught:
                        self.invoke(method)
                self.assertIn('HTTP 400', str(caught.exception))
                self.assertIn('Invalid parameter top_k', str(caught.exception))

    def test_engine_channel_error_is_reported_as_unavailable(self):
        error = self.failure(json.dumps({'error': 'Channel Error: Engine protocol completeRawText request failed: fetch failed'}).encode())
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(Exception, 'Moteur de traduction indisponible'):
                self.invoke('complete')

    def test_context_overflow_is_distinct_from_other_http_400_errors(self):
        error = self.failure(b'{"error":{"message":"Requested tokens exceed context length of 4096"}}')
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(Exception, 'contexte trop long'):
                self.invoke('chat')

    def test_diagnostic_redacts_credentials_and_source_dialogue(self):
        body = {'error': {'message': 'Invalid input private dialogue; Authorization: Bearer secret-value; api_key=abc123; sk-proj-abc123secret'}}
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=self.failure(json.dumps(body).encode())):
            with self.assertRaises(Exception) as caught:
                self.invoke('chat')
        message = str(caught.exception)
        for secret in ('private dialogue', 'secret-value', 'abc123', 'sk-proj-abc123secret'):
            self.assertNotIn(secret, message)
        self.assertIn('Invalid input', message)

    def test_html_response_is_not_exposed(self):
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=self.failure(b'<html>private proxy diagnostic</html>', 502)):
            with self.assertRaises(Exception) as caught:
                self.invoke('complete')
        self.assertIn('HTTP 502', str(caught.exception))
        self.assertNotIn('<html>', str(caught.exception))

    def test_oversized_server_diagnostic_is_bounded(self):
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=self.failure(json.dumps({'error': 'Invalid input ' + 'x' * 20000}).encode())):
            with self.assertRaises(Exception) as caught:
                self.invoke('chat')
        self.assertLess(len(str(caught.exception)), 800)

    def test_connection_failure_has_actionable_diagnostic(self):
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=urllib.error.URLError('connection refused')):
            with self.assertRaisesRegex(Exception, 'Connexion LM Studio indisponible'):
                self.invoke('chat')

    def test_engine_disconnect_unloads_owned_alias_once_without_retry(self):
        config = dict(self.config, model='renkavi-test', lms_cli='lms.exe')
        error = self.failure(b'{"error":"Channel Error: Engine protocol completeRawText request failed: fetch failed"}')
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=error) as request:
            with patch('backend.ai.local_llm.subprocess.run', return_value=SimpleNamespace(returncode=0)) as unload:
                with self.assertRaisesRegex(RuntimeError, 'Moteur de traduction indisponible.*HTTP 400'):
                    local_llm.chat(config, {'model': 'renkavi-test', 'input': 'private dialogue'})
        self.assertEqual(unload.call_count, 1)
        self.assertEqual(unload.call_args.args[0], ['lms.exe', 'unload', 'renkavi-test'])
        self.assertEqual(request.call_count, 1)

    def test_other_failures_and_unowned_models_are_not_unloaded(self):
        cases = [('renkavi-test', 'Invalid parameter top_k'),
                 ('renkavi-test', 'Requested tokens exceed context length'),
                 ('user-model', 'Channel Error')]
        for model, diagnostic in cases:
            with self.subTest(model=model, diagnostic=diagnostic):
                config = dict(self.config, model=model, lms_cli='lms.exe')
                error = self.failure(json.dumps({'error': diagnostic}).encode())
                with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=error):
                    with patch('backend.ai.local_llm.subprocess.run') as unload:
                        with self.assertRaises(RuntimeError):
                            local_llm.complete(config, 'private dialogue')
                unload.assert_not_called()

    def test_unload_failure_does_not_replace_original_engine_diagnostic(self):
        error = self.failure(b'{"error":"Channel Error"}')
        with patch('backend.ai.local_llm.urllib.request.urlopen', side_effect=error):
            with patch('backend.ai.local_llm.release_loaded', side_effect=RuntimeError('cleanup failed')):
                with self.assertRaisesRegex(RuntimeError, 'Moteur de traduction indisponible.*HTTP 400.*Channel Error'):
                    self.invoke('chat')


if __name__ == '__main__':
    unittest.main()

