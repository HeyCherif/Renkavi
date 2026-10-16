"""Native desktop host. The only JavaScript bridge saves generated exports."""
from __future__ import annotations

import importlib
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from backend.runtime import resource_root

LOG = logging.getLogger('renkavi.desktop')


class DesktopError(RuntimeError):
    pass


class ExportBridge:
    def __init__(self, server, webview):
        self._server = server
        self._webview = webview
        self._window = None

    def pick_model(self, kind):
        if kind not in ('translation', 'inpainting'):
            raise ValueError('Type de modèle invalide.')
        types = ('Modèle GGUF (*.gguf)',) if kind == 'translation' else ('Inpainting (*.onnx;*.ckpt)',)
        selected = self._window.create_file_dialog(self._webview.OPEN_DIALOG, allow_multiple=False, file_types=types)
        if isinstance(selected, (tuple, list)):
            selected = selected[0] if selected else None
        return {'path': str(selected) if selected else None, 'cancelled': not bool(selected)}

    def save_export(self, kind, identifier, options=None):
        """Select a destination for a server-generated archive; accept no source path."""
        if kind not in ('project', 'workspace') or not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
            raise ValueError('Export invalide.')
        if options is None:
            options = {}
        if not isinstance(options, dict) or len(options) > 16:
            raise ValueError('Options export invalides.')
        source, filename = self._server.export_file(kind, identifier, options)
        destination = self._window.create_file_dialog(self._webview.SAVE_DIALOG, save_filename=Path(filename).name)
        if not destination:
            return {'saved': False}
        if isinstance(destination, (tuple, list)):
            destination = destination[0]
        destination = Path(destination)
        source = Path(source)
        if source.resolve() != destination.resolve():
            import tempfile
            handle,temporary=tempfile.mkstemp(prefix='.renkavi-export-',suffix=source.suffix,dir=destination.parent)
            os.close(handle)
            try:
                shutil.copyfile(source,temporary)
                os.replace(temporary,destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return {'saved': True, 'filename': destination.name}


def _webview():
    try:
        return importlib.import_module('webview')
    except Exception as exc:
        raise DesktopError('Le bureau Renkavi nécessite pywebview, pythonnet et Microsoft Edge WebView2 Runtime. Réinstallez Renkavi ou installez WebView2 Runtime (Evergreen, x64).') from exc


def serve_desktop(server, data_dir: Path, *, webview_module=None, smoke_test=0):
    """Run EdgeChromium on the main thread and own the HTTP server lifecycle."""
    thread = None
    timer = None
    runtime_stopped = False
    view = None
    try:
        view = webview_module if webview_module is not None else _webview()
        # Direct Content-Disposition navigation also uses a native save dialog.
        view.settings['ALLOW_DOWNLOADS'] = True
        bridge = ExportBridge(server, view)
        window = view.create_window('Renkavi', f'http://127.0.0.1:{server.server_port}/', js_api=bridge,
                                    width=1440, height=940, min_size=(960, 640), text_select=True)
        bridge._window = window

        def close_requested():
            nonlocal runtime_stopped
            callback = getattr(server, 'shutdown_runtime', None)
            if callback and not runtime_stopped:
                runtime_stopped = callback(timeout=8) is not False
                if not runtime_stopped:
                    LOG.warning('Fermeture différée : arrêt du traitement en cours. Réessayez dans quelques instants.')
                    window.create_confirmation_dialog('Renkavi', 'Le traitement termine son étape actuelle. Réessayez de fermer la fenêtre dans quelques instants.')
                    return False
            return True

        window.events.closing += close_requested
        if smoke_test:
            def on_loaded():
                nonlocal timer
                timer = threading.Timer(smoke_test, window.destroy)
                timer.daemon = True
                timer.start()
            window.events.loaded += on_loaded
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2}, name='renkavi-http', daemon=True)
        thread.start()
        LOG.info('Fenêtre native : http://127.0.0.1:%s/', server.server_port)
        profile = Path(data_dir) / 'webview2'
        profile.mkdir(parents=True, exist_ok=True)
        view.start(gui='edgechromium', debug=False, private_mode=False, storage_path=str(profile),icon=str(resource_root()/'assets/brand/renkavi.ico'))
    except DesktopError:
        raise
    except Exception as exc:
        raise DesktopError('Impossible de démarrer la fenêtre Renkavi. Vérifiez Microsoft Edge WebView2 Runtime (Evergreen, x64) et pythonnet. Consultez le journal de démarrage.') from exc
    finally:
        if timer:
            timer.cancel()
        try:
            if not runtime_stopped:
                callback = getattr(server, 'shutdown_runtime', None)
                if callback:
                    callback(timeout=8)
        finally:
            try:
                if thread and thread.is_alive():
                    server.shutdown()
                    thread.join(timeout=3)
            finally:
                server.server_close()
        LOG.info('Fenêtre et serveur arrêtés.')
