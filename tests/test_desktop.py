import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from backend.desktop import DesktopError, ExportBridge, serve_desktop


class Event:
    def __init__(self): self.handlers = []
    def __iadd__(self, callback): self.handlers.append(callback); return self


class FakeServer:
    server_port = 18999
    def __init__(self):
        self.running = threading.Event()
        self.stop = threading.Event()
        self.closed = False
        self.shutdown_runtime = Mock(return_value=True)
    def serve_forever(self, **kwargs): self.running.set(); self.stop.wait(3)
    def shutdown(self): self.stop.set()
    def server_close(self): self.closed = True


class FakeWebview:
    SAVE_DIALOG = 30
    def __init__(self, fail=False):
        self.settings = {}
        self.fail = fail
        self.window = SimpleNamespace(events=SimpleNamespace(closing=Event(), loaded=Event()), destroy=Mock(), create_confirmation_dialog=Mock())
    def create_window(self, title, url, **kwargs):
        self.url = url; self.bridge = kwargs['js_api']; return self.window
    def start(self, **kwargs):
        self.start_options = kwargs
        if self.fail: raise RuntimeError('renderer absent')
        for handler in self.window.events.closing.handlers: handler()


class DesktopTests(unittest.TestCase):
    def test_model_picker_uses_native_dialog_and_handles_cancel(self):
        bridge = ExportBridge(None, SimpleNamespace(OPEN_DIALOG=10))
        dialog = Mock(return_value=('D:/models/sample.gguf',))
        bridge._window = SimpleNamespace(create_file_dialog=dialog)
        self.assertEqual(bridge.pick_model('translation')['path'], 'D:/models/sample.gguf')
        self.assertEqual(dialog.call_args.args, (10,))
        dialog.return_value = None
        self.assertTrue(bridge.pick_model('inpainting')['cancelled'])
        with self.assertRaises(ValueError): bridge.pick_model('unknown')

    def test_native_window_owns_and_closes_server(self):
        server, view = FakeServer(), FakeWebview()
        with tempfile.TemporaryDirectory() as tmp:
            serve_desktop(server, Path(tmp), webview_module=view)
        self.assertTrue(server.running.is_set())
        self.assertTrue(server.closed)
        self.assertTrue(server.stop.is_set())
        self.assertEqual(view.start_options['gui'], 'edgechromium')
        self.assertEqual(view.url, 'http://127.0.0.1:18999/')
        server.shutdown_runtime.assert_called_once()

    def test_renderer_failure_closes_owned_server(self):
        server = FakeServer()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(DesktopError, 'WebView2'):
                serve_desktop(server, Path(tmp), webview_module=FakeWebview(fail=True))
        self.assertTrue(server.closed)
        self.assertTrue(server.stop.is_set())

    def test_busy_runtime_refuses_window_close(self):
        server, view = FakeServer(), FakeWebview()
        server.shutdown_runtime.return_value = False
        view.start = lambda **kwargs: self.assertIs(view.window.events.closing.handlers[0](), False)
        with tempfile.TemporaryDirectory() as tmp:
            serve_desktop(server, Path(tmp), webview_module=view)

    def test_export_bridge_copies_only_server_generated_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp)/'generated.cbz', Path(tmp)/'chosen.cbz'
            source.write_bytes(b'archive')
            server = SimpleNamespace(export_file=Mock(return_value=(source, 'chapter.cbz')))
            bridge = ExportBridge(server, SimpleNamespace(SAVE_DIALOG=30))
            bridge._window = SimpleNamespace(create_file_dialog=Mock(return_value=str(destination)))
            result = bridge.save_export('project', 'chapter-1', {})
            self.assertTrue(result['saved'])
            self.assertEqual(destination.read_bytes(), b'archive')
            with self.assertRaises(ValueError): bridge.save_export('file', 'C:/secret', {})
            self.assertEqual(server.export_file.call_count, 1)

    def test_cancel_export_does_not_write_file(self):
        server = SimpleNamespace(export_file=Mock(return_value=(Path('unused'), 'chapter.cbz')))
        bridge = ExportBridge(server, SimpleNamespace(SAVE_DIALOG=30))
        bridge._window = SimpleNamespace(create_file_dialog=Mock(return_value=None))
        self.assertEqual(bridge.save_export('project', 'chapter-1', {}), {'saved': False})


if __name__ == '__main__': unittest.main()

