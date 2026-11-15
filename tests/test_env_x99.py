"""env.py hands DISPLAY=:99 to children only when an X server answers on the
socket (#1166): a socket file left by an Xvfb that died made Freerouting's
Java exit with "Can't connect to X11 window server" where with no DISPLAY it
runs headless."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
import env  # noqa: E402


def test_a_dead_socket_file_is_not_a_display(tmp_path):
    p = str(tmp_path / "X99")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(p)
    s.close()                       # the file stays, nobody listens
    assert Path(p).exists() and env._x_live(p) is False


def test_a_listening_socket_is_a_display(tmp_path):
    p = str(tmp_path / "X99")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(p)
    s.listen(1)
    try:
        assert env._x_live(p) is True
    finally:
        s.close()


def test_a_missing_socket_is_not_a_display(tmp_path):
    assert env._x_live(str(tmp_path / "X99")) is False
