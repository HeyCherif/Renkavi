"""Launch Renkavi as a native Windows desktop application."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from backend.instance import AlreadyRunning, InstanceLock

LOG = logging.getLogger('renkavi.launcher')


def configure_logging(data_dir):
    logs = Path(data_dir) / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    logfile = logs / 'startup.log'
    logging.basicConfig(filename=logfile, encoding='utf-8', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s', force=True)
    # PyInstaller --windowed sets these to None. Libraries and HTTP handlers print.
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, logfile.open('a', encoding='utf-8', buffering=1))
    return logfile


def show_error(message, *, native=True):
    LOG.error(message)
    if native and sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Renkavi — démarrage impossible', 0x10)
            return
        except Exception:
            LOG.exception('Affichage du diagnostic impossible')
    print(message, file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Renkavi, atelier local de traduction')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--models-dir', type=Path, help='Dossier partagé des modèles IA')
    parser.add_argument('--port', type=int, default=0, help='Port HTTP local (0 : libre automatiquement)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--browser', action='store_true', help='Ouvrir dans le navigateur pour diagnostic')
    mode.add_argument('--no-browser', action='store_true', help='Serveur uniquement, sans fenêtre')
    parser.add_argument('--smoke-test', type=float, default=0, metavar='SECONDS', help='Fermer automatiquement après chargement (vérification)')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or args.smoke_test < 0:
        parser.error('Port ou durée de vérification invalide.')
    if args.models_dir:
        os.environ['RENKAVI_MODELS_DIR'] = str(args.models_dir.expanduser().resolve())
    # Resolve paths without importing optional AI or desktop dependencies.
    from backend.runtime import default_data_dir
    data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
    logfile = configure_logging(data_dir)
    LOG.info('Démarrage Renkavi ; bibliothèque=%s ; frozen=%s', data_dir, bool(getattr(sys, 'frozen', False)))
    native = not (args.browser or args.no_browser)
    try:
        with InstanceLock(data_dir) as instance:
            from backend.api.server import make_server
            from backend.projects.store import ProjectStore
            server = make_server(ProjectStore(data_dir), args.port)
            url = f'http://127.0.0.1:{server.server_port}/'
            instance.set_url(url)
            LOG.info('Serveur local : %s', url)
            if native:
                from backend.desktop import serve_desktop
                serve_desktop(server, data_dir, smoke_test=args.smoke_test)
            else:
                timer = None
                try:
                    print(f'Renkavi : {url}\nBibliothèque : {data_dir}', flush=True)
                    if args.browser:
                        webbrowser.open(url)
                    if args.smoke_test:
                        timer = threading.Timer(args.smoke_test, server.shutdown)
                        timer.daemon = True
                        timer.start()
                    try:
                        server.serve_forever(poll_interval=0.2)
                    except KeyboardInterrupt:
                        pass
                finally:
                    if timer:
                        timer.cancel()
                    stop = getattr(server, 'shutdown_runtime', None)
                    if stop:
                        stop(timeout=8)
                    server.server_close()
        LOG.info('Arrêt terminé')
        return 0
    except AlreadyRunning:
        show_error(f'Renkavi utilise déjà cette bibliothèque :\n{data_dir}\nFermez l’autre fenêtre Renkavi avant de relancer cette bibliothèque.', native=native)
        return 1
    except Exception as exc:
        LOG.exception('Échec du démarrage ou de la fermeture')
        show_error(f'{exc}\n\nJournal : {logfile}', native=native)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
