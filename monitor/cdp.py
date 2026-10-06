"""Окно Edge по протоколу DevTools: свой WebSocket-клиент на стандартной библиотеке.

Это обычный Edge с отдельным постоянным профилем (data/browser): страницы грузит сам браузер,
как у посетителя. Окно стоит за краем экрана и выезжает, когда нужен человек.
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import urlsplit


class BrowserError(Exception):
    pass


# ---------------------------------------------------------------- WebSocket (RFC 6455, только клиент)


class WebSocket:
    def __init__(self, url, timeout=30):
        p = urlsplit(url)
        host, port = p.hostname, p.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = p.path + (("?" + p.query) if p.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("Браузер закрыл соединение при подключении")
            head += chunk
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise BrowserError(f"Браузер не принял подключение: {status_line.decode(errors='replace')}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise BrowserError("Окно браузера закрыто")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        data = text.encode("utf-8")
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + masked)

    def _send_control(self, opcode, payload=b""):
        mask = os.urandom(4)
        frame = (
            bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        )
        self.sock.sendall(frame)

    def recv(self, timeout=None):
        """Следующее текстовое сообщение или None по тайм-ауту."""
        self.sock.settimeout(timeout)
        parts = []
        try:
            while True:
                b1, b2 = self._read(2)
                fin, opcode = b1 & 0x80, b1 & 0x0F
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                if b2 & 0x80:
                    mask = self._read(4)
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
                else:
                    payload = self._read(n)
                if opcode == 0x9:
                    self._send_control(0xA, payload)
                    continue
                if opcode == 0x8:
                    raise BrowserError("Окно браузера закрыто")
                if opcode in (0x1, 0x2, 0x0):
                    parts.append(payload)
                    if fin:
                        return b"".join(parts).decode("utf-8", "replace")
        except TimeoutError:
            if parts:  # недочитанное сообщение — дочитываем без тайм-аута
                self.sock.settimeout(30)
                return self.recv(30)
            return None

    def close(self):
        try:
            self._send_control(0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- поиск и запуск Edge


def find_browser(custom=""):
    env = os.environ.get("OSNOVIT_BROWSER")
    for path in (custom, env):
        if path and os.path.exists(path):
            return path
    if sys.platform.startswith("win"):
        cands = []
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        for c in cands:
            if os.path.exists(c):
                return c
    for name in ("msedge", "microsoft-edge", "microsoft-edge-stable", "chromium", "chromium-browser", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _json_http(url, method="GET", timeout=5):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def debugger_alive(port):
    try:
        _json_http(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def launch(browser_path, profile_dir, port, offscreen=True, extra_args=()):
    """Запускает Edge с постоянным профилем (или подключается к уже открытому на этом порту)."""
    if debugger_alive(port):
        return None
    if not browser_path:
        raise BrowserError("Не найден Microsoft Edge. Укажите путь к msedge.exe в настройках.")
    os.makedirs(profile_dir, exist_ok=True)
    args = [
        browser_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--window-size=1280,900",
    ]
    if offscreen:
        args.append("--window-position=-2400,0")
    args += [*extra_args, "about:blank"]
    flags = 0
    if sys.platform.startswith("win"):
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    for _ in range(60):
        if debugger_alive(port):
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    raise BrowserError("Edge не открылся для сбора. Закройте все окна «Браузер для сбора» и попробуйте ещё раз.")


# ---------------------------------------------------------------- вкладка


class Tab:
    """Одна вкладка: команды DevTools и очередь событий."""

    def __init__(self, port):
        self.port = port
        try:
            info = _json_http(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
        except (urllib.error.HTTPError, urllib.error.URLError):
            info = _json_http(f"http://127.0.0.1:{port}/json/new?about:blank")
        self.target_id = info["id"]
        self.ws = WebSocket(info["webSocketDebuggerUrl"])
        self._id = 0
        self.events: deque[dict] = deque(maxlen=5000)
        self.last_headers = {}
        self.send("Page.enable")
        self.send("Network.enable", maxResourceBufferSize=8_000_000, maxTotalBufferSize=64_000_000)
        self.send("Runtime.enable")

    def send(self, method, timeout=60, **params):
        self._id += 1
        my = self._id
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.ws.recv(timeout=max(0.1, deadline - time.time()))
            if msg is None:
                continue
            data = json.loads(msg)
            if data.get("id") == my:
                if "error" in data:
                    raise BrowserError(f"{method}: {data['error'].get('message')}")
                return data.get("result", {})
            if "method" in data:
                self.events.append(data)
        raise BrowserError(f"Браузер не ответил на {method}")

    def pump(self, seconds):
        """Собирает события в течение seconds."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            msg = self.ws.recv(timeout=max(0.05, deadline - time.time()))
            if msg:
                data = json.loads(msg)
                if "method" in data:
                    self.events.append(data)

    def evaluate(self, expression, timeout=30):
        res = self.send(
            "Runtime.evaluate", timeout=timeout, expression=expression, returnByValue=True, awaitPromise=True
        )
        if res.get("exceptionDetails"):
            raise BrowserError("Ошибка скрипта на странице: " + str(res["exceptionDetails"].get("text")))
        return res.get("result", {}).get("value")

    def navigate(self, url, timeout=45):
        """Открывает адрес и ждёт загрузки. Возвращает HTTP-код основного документа (или None)."""
        self.events.clear()
        self.last_headers = {}
        res = self.send("Page.navigate", url=url)
        if res.get("errorText"):
            raise BrowserError(f"Страница не открылась: {res['errorText']}")
        status = None
        start = time.time()
        loaded = False
        while time.time() - start < timeout and not loaded:
            self.pump(0.5)
            for ev in list(self.events):
                m = ev.get("method")
                if m == "Network.responseReceived" and ev["params"].get("type") == "Document" and status is None:
                    status = ev["params"]["response"].get("status")
                    self.last_headers = {
                        str(k).lower(): str(v) for k, v in (ev["params"]["response"].get("headers") or {}).items()
                    }
                if m == "Page.loadEventFired":
                    loaded = True
            if not loaded and time.time() - start > 2:
                try:
                    state = self.evaluate("[document.readyState, location.href]", timeout=5) or ["", ""]
                    loaded = state[0] == "complete" and state[1] != "about:blank"
                except BrowserError:
                    pass
        self.pump(2.5)  # страница догружает данные скриптами
        return status

    def json_responses(self, hosts, limit=40, max_bytes=4_000_000):
        """Ответы с данными, которые страница получила сама (XHR/fetch с JSON)."""
        out = []
        for ev in list(self.events):
            if ev.get("method") != "Network.responseReceived":
                continue
            p = ev["params"]
            resp = p.get("response", {})
            mime = (resp.get("mimeType") or "").lower()
            if p.get("type") not in ("XHR", "Fetch") or "json" not in mime:
                continue
            host = urlsplit(resp.get("url", "")).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in hosts):
                continue
            if resp.get("encodedDataLength", 0) > max_bytes:
                continue
            try:
                body = self.send("Network.getResponseBody", timeout=15, requestId=p["requestId"])
            except BrowserError:
                continue
            text = body.get("body", "")
            if body.get("base64Encoded"):
                try:
                    text = base64.b64decode(text).decode("utf-8", "replace")
                except ValueError:
                    continue
            try:
                out.append((resp.get("url"), json.loads(text)))
            except ValueError:
                continue
            if len(out) >= limit:
                break
        return out

    def html(self):
        return self.evaluate("document.documentElement.outerHTML") or ""

    def show_window(self, show=True):
        """Выдвигает окно на экран (нужен человек) или убирает за край."""
        try:
            win = self.send("Browser.getWindowForTarget", targetId=self.target_id)
            bounds = {"left": 80, "top": 40, "windowState": "normal"} if show else {"left": -2400, "top": 0}
            self.send("Browser.setWindowBounds", windowId=win["windowId"], bounds=bounds)
            if show:
                self.send("Page.bringToFront")
        except BrowserError:
            pass

    def close(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/close/{self.target_id}", timeout=3).read()
        except (urllib.error.URLError, OSError):
            pass
        self.ws.close()
