#!/usr/bin/env python3
# ============================================================
#  server.py — локальная веб-панель кита (кроссплатформенная).
#
#  Крошечный сервер на стандартной библиотеке Python (без pip-зависимостей):
#  отдаёт одностраничную панель webgui/index.html и по allowlist дёргает
#  bin/netctl, стримя его вывод в браузер живьём (Server-Sent Events).
#
#  Слушает ТОЛЬКО 127.0.0.1 и требует секретный токен (генерится при старте,
#  вшивается в страницу) — чтобы посторонняя локальная вкладка не дёргала API.
#
#  Запуск: python3 webgui/server.py         (или через bin/netgui-web)
#          python3 webgui/server.py --port 8765 --no-browser
# ============================================================
import argparse
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

KIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBGUI_DIR = os.path.join(KIT_DIR, "webgui")
CONF = os.path.join(KIT_DIR, "config", "kit.conf")
NODES = os.path.join(KIT_DIR, "config", "nodes.list")
CONV = os.path.join(KIT_DIR, "tools", "xray-json-to-vless.sh")

# на Windows netctl.cmd, иначе POSIX netctl
if os.name == "nt":
    NETCTL = [os.path.join(KIT_DIR, "bin", "netctl.cmd")]
else:
    NETCTL = ["sh", os.path.join(KIT_DIR, "bin", "netctl")]

TOKEN = secrets.token_urlsafe(16)

# --- allowlist: имя действия -> список аргументов для netctl ------------------
# Аргументы с пользовательским вводом валидируются отдельно (см. build_argv).
STATIC_ACTIONS = {
    "status":         ["status"],
    "doctor":         ["doctor"],
    "nodes-list":     ["nodes", "list"],
    "nodes-test":     ["nodes", "test"],
    "domains-list":   ["domains", "list"],
    "apply":          ["apply"],
    "install":        ["install"],
    "pull":           ["pull"],
    "geoblock-update":["geoblock", "update"],
    "geoblock-count": ["geoblock", "count"],
    "geoblock-on":    ["geoblock", "on"],
    "geoblock-off":   ["geoblock", "off"],
    "zapret-on":      ["zapret", "on"],
    "zapret-off":     ["zapret", "off"],
    "zapret-check":   ["zapret", "check"],
    "proxy-on":       ["proxy", "on"],
    "proxy-off":      ["proxy", "off"],
}

# ключи kit.conf, которые панель читает/пишет (мастер установки + тумблеры)
CONF_KEYS = [
    "ROUTER_IP", "ROUTER_SSH_USER", "ROUTER_SSH_PORT",
    "PROXY_ENABLE", "ZAPRET_ENABLE", "GEOBLOCK_ENABLE",
    "SPLIT_RU_ENABLE", "BLOCK_QUIC", "LAN_IPV6",
]

DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
VLESS_RE = re.compile(r"vless://[^\s\"']+")


# ---------------------------------------------------------------- config I/O --
def read_conf():
    vals = {}
    try:
        with open(CONF, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'^\s*([A-Z_][A-Z0-9_]*)=(.*)$', line)
                if not m:
                    continue
                raw = m.group(2).strip()
                if raw.startswith('"'):
                    val = raw[1:].split('"', 1)[0]          # содержимое кавычек
                else:
                    val = raw.split("#", 1)[0].split()[0] if raw.split("#", 1)[0].split() else ""
                vals[m.group(1)] = val
    except FileNotFoundError:
        pass
    return {k: vals.get(k, "") for k in CONF_KEYS}


def write_conf(updates):
    """Правит kit.conf: заменяет KEY=... или дописывает в конец. Только CONF_KEYS."""
    try:
        with open(CONF, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    for key, val in updates.items():
        if key not in CONF_KEYS:
            continue
        val = str(val).replace('"', "")
        for i, line in enumerate(lines):
            if re.match(rf"^\s*{re.escape(key)}=", line):
                cm = re.search(r'(\s+#.*)$', line.rstrip("\n"))  # сохранить inline-коммент
                lines[i] = f'{key}="{val}"' + (cm.group(1) if cm else "") + "\n"
                break
        else:
            lines.append(f'{key}="{val}"\n')
    with open(CONF, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ------------------------------------------------------- build argv из запроса --
def build_argv(action, args):
    """Возвращает список argv для netctl или бросает ValueError."""
    if action in STATIC_ACTIONS:
        argv = list(STATIC_ACTIONS[action])
        if action == "doctor" and args:
            d = args[0]
            if not DOMAIN_RE.match(d):
                raise ValueError("плохой домен")
            argv.append(d)
        return argv
    if action == "nodes-use":
        if not args or not re.fullmatch(r"\d+", str(args[0])):
            raise ValueError("нужен номер ноды")
        return ["nodes", "use", str(args[0])]
    if action == "domains-add":
        doms = [a for a in args if DOMAIN_RE.match(a)]
        if not doms:
            raise ValueError("нет валидных доменов")
        return ["domains", "add"] + doms
    if action == "domains-rm":
        if not args or not DOMAIN_RE.match(args[0]):
            raise ValueError("нужен домен")
        return ["domains", "rm", args[0]]
    if action == "logs":
        svc = args[0] if args else ""
        if svc not in ("sing-box", "zapret", "dns", ""):
            raise ValueError("неизвестный лог")
        return ["logs"] + ([svc] if svc else [])
    raise ValueError(f"неизвестное действие: {action}")


# ------------------------------------------------------------ добавление нод ---
def add_nodes_argv(text):
    """Из вставленного текста (vless-ссылки или xray-JSON) готовит netctl-аргументы."""
    urls = VLESS_RE.findall(text or "")
    if urls:
        return ["nodes", "add-url"] + urls, None
    # иначе пробуем как xray-JSON через конвертер
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    tmp.write(text or "")
    tmp.close()
    return ["nodes", "add-json", tmp.name], tmp.name


# ------------------------------------------------------------------- runner ----
def stream_command(argv, on_line):
    """Запускает netctl argv, построчно отдаёт вывод в on_line, возвращает код."""
    env = dict(os.environ)
    env.setdefault("LC_ALL", env.get("LANG", "C.UTF-8"))
    try:
        proc = subprocess.Popen(
            NETCTL + argv,
            cwd=KIT_DIR,
            stdin=subprocess.DEVNULL,      # никаких интерактивных промптов ssh
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except FileNotFoundError as e:
        on_line(f"[ошибка запуска netctl: {e}]")
        return 127
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    proc.stdout.close()
    return proc.wait()


# ---------------------------------------------------------------- HTTP layer ---
class Handler(BaseHTTPRequestHandler):
    server_version = "netgui/1.0"

    def log_message(self, *a):
        pass  # тихо

    # --- helpers ---
    def _check_token(self, qs):
        tok = self.headers.get("X-Token") or (qs.get("t", [""])[0])
        return secrets.compare_digest(tok or "", TOKEN)

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n).decode("utf-8") if n else ""

    # --- SSE stream of a command ---
    def _stream(self, argv):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event, payload):
            try:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise

        try:
            rc = stream_command(argv, lambda ln: emit("line", ln))
            emit("done", {"rc": rc})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- routing ---
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/" or u.path == "/index.html":
            return self._serve_index()
        if u.path == "/api/config":
            if not self._check_token(qs):
                return self._json(403, {"error": "bad token"})
            return self._json(200, read_conf())
        if u.path == "/api/run":
            if not self._check_token(qs):
                return self._send(403, "bad token")
            action = qs.get("action", [""])[0]
            args = qs.get("arg", [])
            try:
                argv = build_argv(action, args)
            except ValueError as e:
                return self._send(400, str(e))
            return self._stream(argv)
        return self._send(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._check_token(qs):
            return self._json(403, {"error": "bad token"})
        body = self._read_body()
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if u.path == "/api/config":
            write_conf(data)
            return self._json(200, read_conf())

        if u.path == "/api/run":
            action = data.get("action", "")
            args = data.get("args", []) or []
            try:
                argv = build_argv(action, [str(a) for a in args])
            except ValueError as e:
                return self._send(400, str(e))
            return self._stream(argv)

        if u.path == "/api/addnode":
            text = data.get("text", "")
            urls = VLESS_RE.findall(text)
            if not urls and "outbounds" not in text and "vnext" not in text:
                return self._send(400, "не похоже ни на vless-ссылку, ни на xray-JSON")
            argv, tmp = add_nodes_argv(text)
            # tmp-файл убираем после стрима
            try:
                return self._stream(argv)
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        return self._json(404, {"error": "not found"})

    def _serve_index(self):
        try:
            with open(os.path.join(WEBGUI_DIR, "index.html"), encoding="utf-8") as f:
                page = f.read()
        except FileNotFoundError:
            return self._send(500, "index.html не найден")
        page = page.replace("__TOKEN__", TOKEN)
        self._send(200, page, "text/html; charset=utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="порт (0 = случайный свободный)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"

    print("OpenWrt Bypass Kit — веб-панель")
    print(f"  {url}")
    print("  Ctrl-C — остановить")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
        httpd.shutdown()


if __name__ == "__main__":
    main()
