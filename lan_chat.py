#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN CMD Chat 1.0
================

A small text chat room for a local network (LAN), built with only the Python
standard library. Everything lives in this single file and it runs on
Windows 10/11 and on Android phones (Pydroid 3 or Termux).

How it works
------------
One device becomes the host and the others join it:

    # on the host machine
    python lan_chat.py host

    # on every other device (use the IP the host prints)
    python lan_chat.py join 192.168.1.20

    # server only, without joining the chat
    python lan_chat.py serve

    # no arguments: a simple menu
    python lan_chat.py

Default port: 8765  (use --port to change it)

Windows notes
-------------
* The first run triggers the Windows firewall prompt: click "Allow access".
* If Unicode text looks wrong in CMD, use Windows Terminal instead.
* All devices must be connected to the same Wi-Fi / hotspot.

In-chat commands: /help
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# General settings
# ---------------------------------------------------------------------------

APP_NAME = "LAN CMD Chat"
APP_VERSION = "1.0"
PROTOCOL = 1

DEFAULT_PORT = 8765
MAX_NICK = 20
MAX_TEXT = 1000
MAX_LINE = 64 * 1024          # maximum size of one network message (bytes)
JOIN_TIMEOUT = 15.0           # time allowed to send the join message
FLOOD_WINDOW = 6.0            # anti-spam window (seconds)
FLOOD_LIMIT = 30              # maximum messages per window

# ANSI colors
_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_cyan": "\033[96m",
}

_NICK_STYLES = (
    "cyan", "green", "yellow", "magenta", "blue",
    "bright_cyan", "bright_green", "bright_yellow",
)

ANSI_ON = False
_ORIG_OUT_CP = None
_ORIG_IN_CP = None


# ---------------------------------------------------------------------------
# Console setup (Unicode + colors)
# ---------------------------------------------------------------------------

def setup_console() -> None:
    """Switch the Windows console to UTF-8 and enable ANSI colors."""
    global ANSI_ON, _ORIG_OUT_CP, _ORIG_IN_CP

    vt_ok = True
    if os.name == "nt":
        vt_ok = False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            _ORIG_OUT_CP = kernel32.GetConsoleOutputCP()
            _ORIG_IN_CP = kernel32.GetConsoleCP()
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)

            STD_OUTPUT_HANDLE = -11
            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                if kernel32.SetConsoleMode(
                    handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                ):
                    vt_ok = True
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Read piped (non-console) input as UTF-8 as well
    try:
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    ANSI_ON = is_tty and vt_ok and not os.environ.get("NO_COLOR")


def restore_console() -> None:
    """Restore the previous Windows console code page on exit."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if _ORIG_OUT_CP:
            kernel32.SetConsoleOutputCP(_ORIG_OUT_CP)
        if _ORIG_IN_CP:
            kernel32.SetConsoleCP(_ORIG_IN_CP)
    except Exception:
        pass


def paint(text: str, *styles: str) -> str:
    """Return the text wrapped in ANSI colors (or plain text if unsupported)."""
    if not ANSI_ON or not text:
        return text
    prefix = "".join(_CODES[s] for s in styles if s in _CODES)
    if not prefix:
        return text
    return prefix + text + _CODES["reset"]


def paint_nick(nick: str) -> str:
    """Pick a stable color for each nickname."""
    if not ANSI_ON or not nick:
        return nick
    index = sum(nick.encode("utf-8", "replace")) % len(_NICK_STYLES)
    return paint(nick, _NICK_STYLES[index], "bold")


def fmt_time(ts=None) -> str:
    if ts is None:
        ts = time.time()
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "--:--"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_nick(value, limit: int = MAX_NICK) -> str:
    """Sanitize a nickname; return '' when it is not usable."""
    if not isinstance(value, str):
        return ""
    chars = []
    for ch in value.strip():
        code = ord(ch)
        if code < 32 or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            continue
        if code in (0xFEFF, 0x200B):        # BOM and zero-width space
            continue
        if ch in '/\\()[]{}"\'`<>|':
            continue
        chars.append(ch)
    nick = "".join(chars).strip().replace(" ", "_")[:limit]
    return nick


def clean_text(value, limit: int = MAX_TEXT) -> str:
    """Make a message single-line, strip control characters and truncate it."""
    if not isinstance(value, str):
        return ""
    chars = []
    for ch in value:
        code = ord(ch)
        if code < 32:
            if ch in "\t\r\n":
                chars.append(" ")
            continue
        if code == 0x7F or 0xD800 <= code <= 0xDFFF:
            continue
        if code in (0xFEFF, 0x200B):
            continue
        chars.append(ch)
    text = " ".join("".join(chars).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def lan_addresses() -> list:
    """Local IPv4 addresses of this machine (to share with the others)."""
    found = []

    def add(ip):
        if not ip or ip.startswith("127.") or ip in ("0.0.0.0", "255.255.255.255"):
            return
        if ip not in found:
            found.append(ip)

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        try:
            probe.close()
        except OSError:
            pass

    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    if not found:
        try:
            add(socket.gethostbyname(socket.gethostname()))
        except OSError:
            pass
    return found


def suggest_nick() -> str:
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            nick = clean_nick(value)
            if nick:
                return nick
    return "user"


def ask(prompt_text: str, default: str = "") -> str:
    """input() with a default value; raises UserAbort when input is closed."""
    suffix = " [{}]".format(default) if default else ""
    try:
        value = input("{}{}: ".format(prompt_text, suffix)).strip()
    except EOFError:
        raise UserAbort()
    return value or default


# ---------------------------------------------------------------------------
# Message layer (one JSON object per line)
# ---------------------------------------------------------------------------

class ChatError(Exception):
    """An error that should be shown to the user."""


class UserAbort(Exception):
    """The user closed the input (Ctrl+D / EOF)."""


class LineChannel:
    """Send and receive line-delimited JSON messages over a TCP socket."""

    def __init__(self, sock):
        self.sock = sock
        self._buf = bytearray()

    def send(self, payload: dict) -> None:
        data = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8", "replace") + b"\n"
        self.sock.sendall(data)

    def recv(self):
        """Return one dict message, or None if the peer closed the socket."""
        while True:
            index = self._buf.find(b"\n")
            if index != -1:
                raw = bytes(self._buf[:index])
                del self._buf[: index + 1]
                raw = raw.rstrip(b"\r")
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ChatError("Invalid message received.")
                if not isinstance(obj, dict):
                    raise ChatError("Invalid message structure.")
                return obj

            if len(self._buf) > MAX_LINE:
                raise ChatError("Message is too long.")

            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self._buf.extend(chunk)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class _Peer:
    """One user connected to the server."""

    def __init__(self, sock, addr, chan):
        self.sock = sock
        self.addr = addr
        self.chan = chan
        self.nick = ""
        self.send_lock = threading.Lock()
        self.flood_start = time.monotonic()
        self.flood_count = 0

    def send(self, payload: dict) -> None:
        with self.send_lock:
            self.chan.send(payload)

    def try_send(self, payload: dict) -> bool:
        try:
            self.send(payload)
            return True
        except (OSError, ChatError):
            return False


class ChatServer:
    """Chat server: accepts connections and broadcasts messages."""

    def __init__(self, port: int = DEFAULT_PORT, host: str = "0.0.0.0",
                 room: str = "LAN", log=None):
        self.port = port
        self.host = host
        self.room = room
        self.log = log or (lambda text: None)
        self._peers = {}          # nick.casefold() -> _Peer
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._srv = None
        self._thread = None

    # -- life cycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
            srv.listen(64)
            srv.settimeout(0.5)
        except OSError:
            try:
                srv.close()
            except OSError:
                pass
            raise

        self._srv = srv
        self.port = srv.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="chat-accept", daemon=True
        )
        self._thread.start()
        return srv.getsockname()

    def stop(self) -> None:
        self._stop.set()
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass

        with self._lock:
            peers = list(self._peers.values())
            self._peers.clear()

        for peer in peers:
            try:
                peer.sock.settimeout(0.5)
            except OSError:
                pass
            peer.try_send({
                "type": "system",
                "text": "The server is shutting down.",
                "ts": time.time(),
            })
            try:
                peer.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                peer.sock.close()
            except OSError:
                pass

        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # -- accepting connections -----------------------------------------------

    def _accept_loop(self) -> None:
        srv = self._srv
        while not self._stop.is_set():
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.settimeout(JOIN_TIMEOUT)
            except OSError:
                pass
            threading.Thread(
                target=self._client_loop, args=(sock, addr),
                name="chat-peer", daemon=True,
            ).start()

    def _client_loop(self, sock, addr) -> None:
        chan = LineChannel(sock)
        peer = _Peer(sock, addr, chan)
        joined = False
        try:
            hello = chan.recv()
            if hello is None:
                return
            if hello.get("type") != "join":
                peer.try_send({
                    "type": "error",
                    "text": "The first message must be a join.",
                    "fatal": True,
                })
                return

            proto = hello.get("proto")
            if isinstance(proto, int) and proto > PROTOCOL:
                peer.try_send({
                    "type": "error",
                    "text": "This client version is not compatible with the server.",
                    "fatal": True,
                })
                return

            nick = clean_nick(hello.get("nick"))
            if not nick:
                peer.try_send({
                    "type": "error",
                    "text": "Invalid name (1 to {} characters).".format(MAX_NICK),
                    "fatal": True,
                })
                return

            with self._lock:
                if nick.casefold() in self._peers:
                    taken = True
                else:
                    taken = False
                    peer.nick = nick
                    self._peers[nick.casefold()] = peer
                    joined = True

            if taken:
                peer.try_send({
                    "type": "error",
                    "text": "The name \"{}\" is already taken.".format(nick),
                    "fatal": True,
                })
                return

            sock.settimeout(None)
            peer.try_send({
                "type": "welcome",
                "nick": nick,
                "room": self.room,
                "users": self.nick_list(),
                "proto": PROTOCOL,
                "server": APP_NAME,
            })
            self.log("join: {} from {}".format(nick, addr[0]))
            self.broadcast({
                "type": "system",
                "text": "{} joined the room.".format(nick),
                "ts": time.time(),
            }, exclude=peer)

            while not self._stop.is_set():
                msg = chan.recv()
                if msg is None:
                    break
                self._handle(peer, msg)
        except (socket.timeout, ChatError, OSError):
            pass
        finally:
            if joined:
                self._drop(peer)
            try:
                sock.close()
            except OSError:
                pass

    # -- handling messages ---------------------------------------------------

    def _handle(self, peer: _Peer, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "msg":
            text = clean_text(msg.get("text"))
            if not text or self._flooding(peer):
                return
            self.broadcast({
                "type": "chat",
                "from": peer.nick,
                "text": text,
                "ts": time.time(),
                "action": bool(msg.get("action")),
            })
        elif mtype == "pm":
            self._on_pm(peer, msg)
        elif mtype == "nick":
            self._on_nick(peer, msg)
        elif mtype == "users":
            peer.try_send({
                "type": "users",
                "users": self.nick_list(),
                "room": self.room,
            })
        elif mtype == "quit":
            raise ChatError("quit")
        elif mtype == "ping":
            peer.try_send({"type": "pong", "ts": time.time()})
        else:
            peer.try_send({
                "type": "error",
                "text": "This message type is not supported.",
            })

    def _on_pm(self, peer: _Peer, msg: dict) -> None:
        text = clean_text(msg.get("text"))
        if not text or self._flooding(peer):
            return
        target_name = msg.get("to")
        target = None
        if isinstance(target_name, str):
            with self._lock:
                target = self._peers.get(target_name.strip().casefold())
        if target is None:
            peer.try_send({
                "type": "error",
                "text": "No such user in the room.",
            })
            return

        payload = {
            "type": "pm",
            "from": peer.nick,
            "to": target.nick,
            "text": text,
            "ts": time.time(),
        }
        target.try_send(dict(payload, outgoing=False))
        if target is not peer:
            peer.try_send(dict(payload, outgoing=True))

    def _on_nick(self, peer: _Peer, msg: dict) -> None:
        new_nick = clean_nick(msg.get("nick"))
        if not new_nick:
            peer.try_send({
                "type": "error",
                "text": "The new name is invalid.",
            })
            return

        with self._lock:
            existing = self._peers.get(new_nick.casefold())
            if existing is not None and existing is not peer:
                taken = True
                old_nick = peer.nick
                changed = False
            else:
                taken = False
                old_nick = peer.nick
                changed = old_nick != new_nick
                if changed:
                    self._peers.pop(old_nick.casefold(), None)
                    self._peers[new_nick.casefold()] = peer
                    peer.nick = new_nick

        if taken:
            peer.try_send({
                "type": "error",
                "text": "The name \"{}\" is already taken.".format(new_nick),
            })
            return

        peer.try_send({
            "type": "nick",
            "old": old_nick,
            "nick": new_nick,
            "changed": changed,
            "ts": time.time(),
        })
        if changed:
            self.log("name change: {} -> {}".format(old_nick, new_nick))
            self.broadcast({
                "type": "system",
                "text": "{} changed their name to {}.".format(old_nick, new_nick),
                "ts": time.time(),
            }, exclude=peer)

    def _flooding(self, peer: _Peer) -> bool:
        now = time.monotonic()
        if now - peer.flood_start > FLOOD_WINDOW:
            peer.flood_start = now
            peer.flood_count = 0
        peer.flood_count += 1
        if peer.flood_count > FLOOD_LIMIT:
            if peer.flood_count == FLOOD_LIMIT + 1:
                peer.try_send({
                    "type": "error",
                    "text": "Slow down! Extra messages are being ignored.",
                })
            return True
        return False

    # -- broadcast helpers ---------------------------------------------------

    def nick_list(self) -> list:
        with self._lock:
            nicks = [p.nick for p in self._peers.values() if p.nick]
        return sorted(nicks, key=str.casefold)

    def broadcast(self, payload: dict, exclude=None) -> None:
        with self._lock:
            peers = list(self._peers.values())
        for peer in peers:
            if peer is exclude:
                continue
            peer.try_send(payload)

    def _drop(self, peer: _Peer) -> None:
        removed = False
        nick = peer.nick
        with self._lock:
            key = nick.casefold()
            if self._peers.get(key) is peer:
                del self._peers[key]
                removed = True
        peer.nick = ""
        if removed:
            self.log("leave: {}".format(nick))
            self.broadcast({
                "type": "system",
                "text": "{} left the room.".format(nick),
                "ts": time.time(),
            })


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ChatClient:
    """Connect to the chat server and receive messages on a background thread."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, nick: str = "",
                 on_message=None):
        self.host = host
        self.port = port
        self.nick = nick
        self.on_message = on_message or (lambda msg: None)
        self.sock = None
        self.chan = None
        self.welcome = {}
        self.alive = False
        self._send_lock = threading.Lock()
        self._reader = None

    # -- connecting ----------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> dict:
        address = "{}:{}".format(self.host, self.port)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except socket.timeout:
            raise ChatError(
                "Connection to {} timed out. Check the IP and make sure the "
                "host firewall allows Python.".format(address)
            )
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            raise ChatError("Could not connect to {} ({}).".format(address, reason))

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(timeout)
            chan = LineChannel(sock)
            chan.send({
                "type": "join",
                "nick": self.nick,
                "proto": PROTOCOL,
                "client": APP_NAME,
            })

            reply = chan.recv()
            if reply is None:
                raise ChatError("The server closed the connection.")
            if reply.get("type") == "error":
                raise ChatError(str(reply.get("text") or "Server rejected the request."))
            if reply.get("type") != "welcome":
                raise ChatError("Invalid reply from the server.")

            sock.settimeout(None)
            self.sock = sock
            self.chan = chan
            self.nick = str(reply.get("nick") or self.nick)
            self.welcome = reply
            self.alive = True
            self._reader = threading.Thread(
                target=self._recv_loop, name="chat-recv", daemon=True
            )
            self._reader.start()
            return reply
        except ChatError:
            try:
                sock.close()
            except OSError:
                pass
            raise
        except OSError as exc:
            try:
                sock.close()
            except OSError:
                pass
            raise ChatError("Connection to the server failed ({}).".format(exc))

    # -- receive loop --------------------------------------------------------

    def _recv_loop(self) -> None:
        reason = "The connection to the server was lost."
        try:
            while self.alive:
                msg = self.chan.recv()
                if msg is None:
                    break
                self.on_message(msg)
        except ChatError:
            reason = "Received an invalid message from the server."
        except OSError:
            reason = "The network connection was lost."
        finally:
            was_alive = self.alive
            self.alive = False
            if was_alive:
                self.on_message({"type": "disconnected", "text": reason})

    # -- sending and closing -------------------------------------------------

    def send(self, payload: dict) -> None:
        if not self.alive or self.chan is None:
            raise ChatError("Not connected.")
        with self._send_lock:
            self.chan.send(payload)

    def close(self, notify_server: bool = True) -> None:
        if self.alive:
            self.alive = False
            if notify_server and self.chan is not None:
                try:
                    with self._send_lock:
                        self.chan.send({"type": "quit"})
                except (OSError, ChatError):
                    pass
        try:
            if self.sock is not None:
                self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Console and line editor (keep the line you are typing intact)
# ---------------------------------------------------------------------------

class _Key:
    """Special (non-text) keys."""

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "<{}>".format(self.name)


_NOOP = _Key("noop")
_EOF = _Key("eof")


class Console:
    """Thread-safe console output that does not break the line being typed."""

    def __init__(self):
        self.lock = threading.RLock()
        self.editor = None

    def line(self, text: str = "") -> None:
        with self.lock:
            editor = self.editor
            active = editor is not None and editor.active
            if active:
                editor.erase_locked()
            sys.stdout.write(str(text) + "\n")
            sys.stdout.flush()
            if active:
                editor.redraw_locked()


class LineEditor:
    """Small cross-platform line reader with backspace and live messages."""

    def __init__(self, console: Console, prompt: str = "> ", should_stop=None):
        self.console = console
        self.prompt = prompt
        self.should_stop = should_stop or (lambda: False)
        self.buffer = []
        self._length = 0
        self.max_length = 3000
        self.active = False
        self.reading = False
        self.raw = None
        self.key_reader = None      # test hook: returns the next key
        self._msvcrt = None
        self._fd = None
        self._term_old = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    # -- setup / teardown ----------------------------------------------------

    def start(self) -> None:
        if self.raw is not None:
            return
        stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        stdout_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        if not (stdin_tty and stdout_tty):
            self.raw = False
            return

        if os.name == "nt":
            try:
                import msvcrt
                self._msvcrt = msvcrt
                self.raw = True
            except Exception:
                self.raw = False
        else:
            try:
                import termios
                import tty
                self._fd = sys.stdin.fileno()
                self._term_old = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                self.raw = True
            except Exception:
                self.raw = False
        self.active = bool(self.raw)

    def stop(self) -> None:
        was_active = self.active
        self.active = False
        self.reading = False
        if self._term_old is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._term_old)
            except Exception:
                pass
            self._term_old = None
        if was_active:
            with self.console.lock:
                sys.stdout.write("\n")
                sys.stdout.flush()

    def set_prompt(self, prompt: str) -> None:
        with self.console.lock:
            self.prompt = prompt
            if self.active and self.reading:
                self._draw_locked()

    # -- drawing -------------------------------------------------------------

    def erase_locked(self) -> None:
        """(caller holds the console lock) erase the current line."""
        if ANSI_ON:
            sys.stdout.write("\r\033[K")
        else:
            width = shutil.get_terminal_size((80, 24)).columns
            sys.stdout.write("\r" + " " * max(0, width - 1) + "\r")
        sys.stdout.flush()

    def redraw_locked(self) -> None:
        """(caller holds the console lock) redraw prompt and typed text."""
        sys.stdout.write(self.prompt + "".join(self.buffer))
        sys.stdout.flush()

    def _draw_locked(self) -> None:
        if ANSI_ON:
            sys.stdout.write("\r\033[K")
        else:
            sys.stdout.write("\r")
        sys.stdout.write(self.prompt + "".join(self.buffer))
        sys.stdout.flush()

    def _finish_locked(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()

    # -- read one line -------------------------------------------------------

    def read_line(self):
        """Read one line; None means the program should stop."""
        if not self.raw:
            try:
                return input(self.prompt).strip()
            except EOFError:
                return None

        with self.console.lock:
            self.buffer = []
            self._length = 0
            self.reading = True
            self._draw_locked()
        try:
            while True:
                if self.should_stop():
                    with self.console.lock:
                        self.reading = False
                        self._finish_locked()
                    return None

                key = self._next_key()
                if key is _NOOP:
                    continue
                if key is _EOF:
                    with self.console.lock:
                        self.reading = False
                        self._finish_locked()
                    return None
                if key in ("\r", "\n"):
                    with self.console.lock:
                        text = "".join(self.buffer).strip()
                        self.buffer = []
                        self._length = 0
                        self.reading = False
                        self._finish_locked()
                    return text
                if key == "\x03":
                    raise KeyboardInterrupt

                with self.console.lock:
                    if key in ("\x7f", "\x08"):
                        if self.buffer:
                            ch = self.buffer.pop()
                            self._length -= len(ch)
                            self._draw_locked()
                    elif key == "\x15":              # Ctrl+U
                        self.buffer = []
                        self._length = 0
                        self._draw_locked()
                    elif key == "\x0c":              # Ctrl+L
                        sys.stdout.write("\033[2J\033[H")
                        self._draw_locked()
                    elif key == "\x04":              # Ctrl+D
                        self.reading = False
                        self._finish_locked()
                        return None
                    elif len(key) == 1 and ord(key) >= 32:
                        if self._length < self.max_length:
                            self.buffer.append(key)
                            self._length += 1
                            self._draw_locked()
        except KeyboardInterrupt:
            with self.console.lock:
                self.reading = False
                self._finish_locked()
            raise

    # -- reading keys --------------------------------------------------------

    def _next_key(self):
        if self.key_reader is not None:
            return self.key_reader()
        if os.name == "nt":
            return self._next_key_windows()
        return self._next_key_posix()

    def _next_key_windows(self):
        m = self._msvcrt
        while True:
            if m.kbhit():
                ch = m.getwch()
                if ch in ("\x00", "\xe0"):          # special keys (arrows ...)
                    m.getwch()
                    return _NOOP
                return ch
            if self.should_stop():
                return _NOOP
            time.sleep(0.02)

    def _next_key_posix(self):
        import select
        fd = self._fd
        while True:
            if self.should_stop():
                return _NOOP
            try:
                ready, _, _ = select.select([fd], [], [], 0.15)
            except (OSError, ValueError):
                return _EOF
            if not ready:
                continue
            try:
                data = os.read(fd, 1)
            except OSError:
                return _EOF
            if not data:
                return _EOF
            ch = self._decoder.decode(data)
            if not ch:
                continue
            if ch == "\x1b":
                self._drain_escape(fd)
                return _NOOP
            return ch

    def _drain_escape(self, fd) -> None:
        """Swallow special-key escape sequences such as the arrow keys."""
        import select
        deadline = time.monotonic() + 0.05
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                return
            if not ready:
                return
            try:
                data = os.read(fd, 1)
            except OSError:
                return
            if not data:
                return
            if data in b"~ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
                return


# ---------------------------------------------------------------------------
# Chat application (message display + local commands)
# ---------------------------------------------------------------------------

def help_lines() -> list:
    return [
        "",
        paint("Chat commands:", "bold"),
        "  /help            show this help",
        "  /nick NAME       change your display name",
        "  /users           people in the room",
        "  /msg NAME TEXT   private message",
        "  /me TEXT         show an action (e.g. * Alice laughs)",
        "  /info            connection details",
        "  /clear           clear the screen",
        "  /quit            leave the chat",
        "",
        paint("Anything else you type is sent to everyone.", "grey"),
        "",
    ]


class ChatApp:
    """Main chat loop on the user side."""

    def __init__(self, client: ChatClient, console: Console):
        self.client = client
        self.console = console
        self.users = []
        self.running = True
        self.editor = LineEditor(
            console, "> ", should_stop=lambda: not self.client.alive
        )
        console.editor = self.editor
        client.on_message = self.on_message

    # -- start ---------------------------------------------------------------

    def _prompt(self) -> str:
        nick = self.client.nick or "?"
        return "[{}] > ".format(paint(nick, "bright_cyan", "bold"))

    def on_connected(self) -> None:
        self.users = [str(n) for n in (self.client.welcome.get("users") or [])]
        self.editor.prompt = self._prompt()
        self.console.line(self._banner())

    def _banner(self) -> str:
        room = str(self.client.welcome.get("room") or "LAN")
        count = len(self.users)
        user_word = "user" if count == 1 else "users"
        lines = [
            "",
            paint("=" * 54, "grey"),
            " " + paint(
                "Welcome to room \"{}\", {}!".format(room, self.client.nick),
                "bold", "bright_cyan",
            ),
            " " + paint(
                "Connected to {}:{}".format(self.client.host, self.client.port),
                "grey",
            ),
            " " + paint("Online: {} {}".format(count, user_word), "grey"),
            " " + paint("Type /help to see the commands.", "grey"),
            paint("=" * 54, "grey"),
            "",
        ]
        return "\n".join(lines)

    def run(self) -> None:
        self.editor.start()
        try:
            while self.running and self.client.alive:
                try:
                    line = self.editor.read_line()
                except KeyboardInterrupt:
                    break
                if line is None:
                    break
                if not line:
                    continue
                try:
                    self._handle(line)
                except ChatError as exc:
                    self.console.line(paint("! " + str(exc), "red"))
        finally:
            self.running = False
            self.client.close()
            self.editor.stop()
            self.console.editor = None
        self.console.line(paint("You left the chat.", "grey"))

    # -- user input ----------------------------------------------------------

    def _handle(self, line: str) -> None:
        if line.startswith("/"):
            self._command(line)
            return
        text = clean_text(line)
        if text:
            self.client.send({"type": "msg", "text": text})

    def _command(self, line: str) -> None:
        parts = line[1:].split(None, 1)
        if not parts:
            return
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            self.running = False
            self.client.close()
        elif cmd in ("help", "h", "?"):
            for text in help_lines():
                self.console.line(text)
        elif cmd in ("nick", "name"):
            new_nick = clean_nick(rest)
            if not new_nick:
                self.console.line(paint(
                    "Invalid name (1 to {} characters, no spaces).".format(MAX_NICK),
                    "red",
                ))
                return
            self.client.send({"type": "nick", "nick": new_nick})
        elif cmd in ("users", "who"):
            self.client.send({"type": "users"})
        elif cmd in ("msg", "pm", "whisper"):
            self._private(rest)
        elif cmd in ("me", "action"):
            text = clean_text(rest)
            if not text:
                self.console.line(paint("Usage: /me TEXT", "grey"))
                return
            self.client.send({"type": "msg", "text": text, "action": True})
        elif cmd in ("clear", "cls"):
            self._clear()
        elif cmd in ("info",):
            self._info()
        else:
            self.console.line(paint(
                "Unknown command \"{}\". Type /help.".format(cmd), "yellow"
            ))

    def _private(self, rest: str) -> None:
        parts = rest.split(None, 1)
        if len(parts) < 2:
            self.console.line(paint("Usage: /msg NAME TEXT", "grey"))
            return
        target = parts[0]
        text = clean_text(parts[1])
        if text:
            self.client.send({"type": "pm", "to": target, "text": text})

    def _clear(self) -> None:
        with self.console.lock:
            if ANSI_ON:
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
            else:
                try:
                    os.system("cls" if os.name == "nt" else "clear")
                except Exception:
                    pass

    def _info(self) -> None:
        room = str(self.client.welcome.get("room") or "LAN")
        self.console.line(
            paint("Room: ", "grey") + room
            + paint("  |  Server: ", "grey")
            + "{}:{}".format(self.client.host, self.client.port)
            + paint("  |  You: ", "grey") + paint_nick(self.client.nick)
        )

    # -- incoming messages ---------------------------------------------------

    def on_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "chat":
            self._show_chat(msg)
        elif mtype == "pm":
            self._show_pm(msg)
        elif mtype == "system":
            text = str(msg.get("text") or "")
            self.console.line(
                paint("[{}] ".format(fmt_time(msg.get("ts"))), "grey")
                + paint("* " + text, "yellow")
            )
        elif mtype == "users":
            self.users = [str(n) for n in (msg.get("users") or [])]
            self._show_users()
        elif mtype == "nick":
            self._on_nick(msg)
        elif mtype == "error":
            self.console.line(paint("! " + str(msg.get("text") or "Error"), "red"))
        elif mtype == "disconnected":
            self.console.line("")
            self.console.line(paint(
                "x " + str(msg.get("text") or "Connection lost."),
                "bright_red", "bold",
            ))
            if self.editor.raw is False:
                self.console.line(paint("Press Enter to exit.", "grey"))

    def _show_chat(self, msg: dict) -> None:
        ts = fmt_time(msg.get("ts"))
        nick = str(msg.get("from") or "?")
        text = str(msg.get("text") or "")
        prefix = paint("[{}] ".format(ts), "grey")
        if msg.get("action"):
            self.console.line(prefix + paint("* {} {}".format(nick, text), "magenta"))
        else:
            self.console.line(prefix + paint_nick(nick) + ": " + text)

    def _show_pm(self, msg: dict) -> None:
        ts = fmt_time(msg.get("ts"))
        prefix = paint("[{}] ".format(ts), "grey")
        label = paint("(private) ", "blue", "bold")
        if msg.get("outgoing"):
            self.console.line(
                prefix + label + paint("to {}: ".format(msg.get("to")), "grey")
                + str(msg.get("text") or "")
            )
        else:
            self.console.line(
                prefix + label + paint_nick(str(msg.get("from") or "?"))
                + ": " + str(msg.get("text") or "")
            )

    def _show_users(self) -> None:
        if not self.users:
            self.console.line(paint("Nobody is in the room.", "grey"))
            return
        names = ", ".join(paint_nick(n) for n in self.users)
        self.console.line(
            paint("Online ({}): ".format(len(self.users)), "cyan") + names
        )

    def _on_nick(self, msg: dict) -> None:
        new_nick = str(msg.get("nick") or "")
        if new_nick:
            self.client.nick = new_nick
            self.editor.set_prompt(self._prompt())
        if msg.get("changed"):
            self.console.line(paint(
                "You are now known as \"{}\".".format(new_nick), "green"
            ))
        else:
            self.console.line(paint(
                "Your name is still \"{}\".".format(new_nick), "grey"
            ))


# ---------------------------------------------------------------------------
# Entry flows (host, join, server, menu)
# ---------------------------------------------------------------------------

def valid_port(port) -> bool:
    return isinstance(port, int) and 1 <= port <= 65535


def ask_port(console: Console, default: int = DEFAULT_PORT) -> int:
    while True:
        raw = ask("Port", str(default))
        try:
            port = int(raw)
        except ValueError:
            console.line(paint("The port must be a number.", "red"))
            continue
        if valid_port(port):
            return port
        console.line(paint("The port must be between 1 and 65535.", "red"))


def show_addresses(console: Console, port: int) -> None:
    ips = lan_addresses()
    console.line("")
    if not ips:
        console.line(paint(
            "No network address found. Check with ipconfig.", "yellow"
        ))
        return

    console.line(paint("This machine's address on the network:", "bold"))
    for ip in ips:
        console.line("  " + paint("{}:{}".format(ip, port), "green", "bold"))

    suffix = "" if port == DEFAULT_PORT else " --port {}".format(port)
    console.line("")
    console.line(paint("On the other devices run one of these:", "grey"))
    for ip in ips[:2]:
        console.line("  " + paint(
            "python lan_chat.py join {}{}".format(ip, suffix), "cyan"
        ))
    console.line("")
    console.line(paint(
        "Windows will ask the first time: click Allow access. "
        "Everyone must be on the same Wi-Fi.", "yellow"
    ))


def run_host(console: Console, port=None, nick=None) -> None:
    console.line("")
    console.line(paint("-- Host a room on this machine --", "bold", "bright_cyan"))
    if not nick:
        nick = ask("Your display name", suggest_nick())
    nick = clean_nick(nick) or suggest_nick()

    if port is None:
        port = ask_port(console)

    server = None
    while server is None:
        candidate = ChatServer(port=port, room="LAN")
        try:
            candidate.start()
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            console.line(paint(
                "Port {} is not available or not permitted ({}).".format(port, reason),
                "red",
            ))
            console.line(paint("Try another port.", "grey"))
            port = ask_port(console, port)
            continue
        server = candidate

    console.line("")
    console.line(paint(
        "Room created on port {}.".format(server.port), "green", "bold"
    ))
    show_addresses(console, server.port)

    client = ChatClient("127.0.0.1", server.port, nick)
    app = ChatApp(client, console)
    try:
        client.connect(timeout=8.0)
    except ChatError as exc:
        console.line(paint("The host could not join the room: " + str(exc), "red"))
        console.editor = None
        server.stop()
        return

    try:
        app.on_connected()
        app.run()
    finally:
        server.stop()
        console.editor = None
    console.line(paint("Room closed and server stopped.", "grey"))


def run_join(console: Console, host: str, port: int = DEFAULT_PORT, nick=None) -> None:
    host = (host or "").strip()
    if ":" in host:
        name, _, tail = host.partition(":")
        if tail.isdigit():
            host = name
            port = int(tail)
    if not host:
        host = ask("Host IP (e.g. 192.168.1.20)", "")
        if not host:
            console.line(paint("The IP is empty.", "red"))
            return

    console.line("")
    console.line(paint("-- Join a room --", "bold", "bright_cyan"))

    while True:
        if not nick:
            nick = ask("Your display name", suggest_nick())
        nick = clean_nick(nick) or suggest_nick()

        client = ChatClient(host, port, nick)
        app = ChatApp(client, console)
        try:
            client.connect()
        except ChatError as exc:
            console.editor = None
            console.line(paint("x " + str(exc), "red"))
            console.line(paint("Troubleshooting:", "yellow"))
            console.line("  - The host must run 'host' or 'serve'.")
            console.line("  - Use the IP printed by the host (e.g. 192.168.1.20).")
            console.line("  - Both devices must be on the same Wi-Fi.")
            console.line("  - The host's Windows firewall must allow Python.")
            console.line("  - If the name is taken, choose another one.")
            answer = ask("Try again? (y/n)", "y").lower()
            if answer not in ("y", "yes"):
                return
            nick = ask("Display name", nick)
            continue

        try:
            app.on_connected()
            app.run()
        finally:
            console.editor = None
        return


def run_serve(console: Console, port=None) -> None:
    console.line("")
    console.line(paint("-- Server only (no chat) --", "bold", "bright_cyan"))

    if port is None:
        port = ask_port(console)

    server = None
    while server is None:
        candidate = ChatServer(
            port=port, room="LAN",
            log=lambda text: console.line(paint("[server] ", "grey") + text),
        )
        try:
            candidate.start()
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            console.line(paint(
                "Port {} is not available or not permitted ({}).".format(port, reason),
                "red",
            ))
            port = ask_port(console, port)
            continue
        server = candidate

    show_addresses(console, server.port)
    console.line(paint(
        "The server is running. Press Ctrl+C to stop.", "green", "bold"
    ))
    try:
        while server.running:
            time.sleep(0.4)
    except KeyboardInterrupt:
        console.line("")
    finally:
        server.stop()
    console.line(paint("Server stopped.", "grey"))


def show_about(console: Console) -> None:
    console.line("")
    console.line(paint("Run from the command line:", "bold"))
    console.line("  python lan_chat.py host")
    console.line("  python lan_chat.py join 192.168.1.20")
    console.line("  python lan_chat.py serve --port 9000")
    console.line("")
    console.line(paint("Default port: {}".format(DEFAULT_PORT), "grey"))
    console.line("")


def menu(console: Console) -> None:
    while True:
        console.line("")
        console.line(paint("=" * 52, "grey"))
        console.line("  " + paint("LAN text chat -- " + APP_NAME, "bold", "bright_cyan"))
        console.line(paint("=" * 52, "grey"))
        console.line("  1) Host a room on this machine")
        console.line("  2) Join a room")
        console.line("  3) Server only (no chat)")
        console.line("  4) Help")
        console.line("  0) Exit")
        console.line("")
        try:
            choice = input(paint("Your choice: ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            console.line("")
            return

        if choice == "1":
            run_host(console)
        elif choice == "2":
            host = ask("Host IP", "")
            if not host:
                console.line(paint("The IP is empty.", "red"))
                continue
            run_join(console, host)
        elif choice == "3":
            run_serve(console)
        elif choice == "4":
            for text in help_lines():
                console.line(text)
            show_about(console)
        elif choice in ("0", "q", "exit", "quit"):
            return
        else:
            console.line(paint("Invalid choice.", "yellow"))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args(argv: list):
    parser = argparse.ArgumentParser(
        prog="lan_chat.py",
        description="LAN text chat room -- standard library only",
    )
    sub = parser.add_subparsers(dest="mode")

    p_host = sub.add_parser("host", help="host a room on this machine and join it")
    p_host.add_argument("--port", type=int, default=None,
                        help="port (default {})".format(DEFAULT_PORT))
    p_host.add_argument("--nick", default=None, help="display name")

    p_join = sub.add_parser("join", help="join a room on another machine")
    p_join.add_argument("host", help="host IP, e.g. 192.168.1.20")
    p_join.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port (default {})".format(DEFAULT_PORT))
    p_join.add_argument("--nick", default=None, help="display name")

    p_serve = sub.add_parser("serve", help="server only, without joining the chat")
    p_serve.add_argument("--port", type=int, default=None,
                         help="port (default {})".format(DEFAULT_PORT))

    return parser.parse_args(argv)


def main(argv=None) -> int:
    setup_console()
    argv = sys.argv[1:] if argv is None else argv

    try:
        args = parse_args(argv)
    except SystemExit as exc:
        restore_console()
        return exc.code if isinstance(exc.code, int) else 0

    console = Console()
    try:
        if args.mode is None:
            menu(console)
            return 0

        if args.mode == "join":
            if not valid_port(args.port):
                console.line(paint("The port must be between 1 and 65535.", "red"))
                return 1
            run_join(console, args.host, args.port, args.nick)
            return 0

        if args.port is not None and not valid_port(args.port):
            console.line(paint("The port must be between 1 and 65535.", "red"))
            return 1

        if args.mode == "host":
            run_host(console, args.port, args.nick)
        elif args.mode == "serve":
            run_serve(console, args.port)
    except KeyboardInterrupt:
        console.line("")
        console.line(paint("Bye.", "grey"))
    except UserAbort:
        console.line("")
        console.line(paint("Input closed; exiting.", "grey"))
    except Exception:
        console.line(paint("Unexpected error:", "red"))
        traceback.print_exc()
    finally:
        restore_console()
    return 0


if __name__ == "__main__":
    sys.exit(main())
