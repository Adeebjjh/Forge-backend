"""On-device runner: start the Forge HTTP server on this Android phone.

Used by the Android app (via Chaquopy) so tools like Python run on the device
itself. No RDP, cloud machine, or extra computer is required.
"""
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import server as _server
from .workspace import Workspace

_server_instance = None
_server_lock = threading.Lock()


def start_device_server(workspace_dir, skills_dir, token):
    """Start the runner on 127.0.0.1:8787. Returns {'url', 'workspace'}."""
    global _server_instance
    with _server_lock:
        if _server_instance is not None:
            raise ValueError('Device runner is already running.')
        workspace = Path(workspace_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        if not isinstance(token, str) or len(token) < 24:
            raise ValueError('A pairing token of at least 24 characters is required.')
        os.environ['FORGE_SKILLS_DIR'] = str(Path(skills_dir))
        Path(skills_dir).mkdir(parents=True, exist_ok=True)
        state = _server.State(str(workspace), token)
        httpd = ThreadingHTTPServer(('127.0.0.1', 8787), _server.handler(state))
        thread = threading.Thread(target=httpd.serve_forever, name='forge-device-server', daemon=True)
        thread.start()
        _server_instance = httpd
        return {'url': 'http://127.0.0.1:8787', 'workspace': str(workspace)}


def stop_device_server():
    global _server_instance
    with _server_lock:
        httpd, _server_instance = _server_instance, None
    if httpd is None:
        return False
    httpd.shutdown()
    httpd.server_close()
    return True


def device_running():
    with _server_lock:
        return _server_instance is not None
