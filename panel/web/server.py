#!/usr/bin/env python3
# ============================================================
#  server.py — веб-панель ТВОЕГО живого роутера (не кит).
#  Крошечный сервер на stdlib: отдаёт web/index.html и по allowlist
#  дёргает ../rctl (SSH к роутеру), стримит вывод в браузер (SSE).
#  Слушает только 127.0.0.1 под секретным токеном.
# ============================================================
import argparse, json, os, re, secrets, subprocess, tempfile, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlsplit, unquote

PANEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PANEL_DIR, "web")
CONF = os.path.join(PANEL_DIR, "panel.conf")
RCTL = ["sh", os.path.join(PANEL_DIR, "rctl")]
TOKEN = secrets.token_urlsafe(16)

STATIC = {
    "status":          ["status"],
    "doctor":          ["doctor"],
    "nodes":           ["nodes", "list"],
    "nodes-test":      ["nodes", "test"],
    "nodes-auto":      ["nodes", "auto"],
    "domains-list":    ["domains", "list"],
    "geoblock-update": ["geoblock", "update"],
    "geoblock-count":  ["geoblock", "count"],
    "geoblock-list":   ["geoblock", "list"],
    "info":            ["info"],
    "watchdog":        ["watchdog"],
    "quic-on":         ["quic", "on"],
    "quic-off":        ["quic", "off"],
    "restart-singbox": ["restart", "sing-box"],
    "restart-zapret":  ["restart", "zapret"],
    "restart-dns":     ["restart", "dnsmasq"],
    "restart-doh":     ["restart", "https-dns-proxy"],
    "toggle-singbox-on":  ["toggle", "sing-box", "on"],
    "toggle-singbox-off": ["toggle", "sing-box", "off"],
    "toggle-zapret-on":   ["toggle", "zapret", "on"],
    "toggle-zapret-off":  ["toggle", "zapret", "off"],
    "toggle-doh-on":      ["toggle", "https-dns-proxy", "on"],
    "toggle-doh-off":     ["toggle", "https-dns-proxy", "off"],
}
CONF_KEYS = ["ROUTER_IP", "ROUTER_SSH_USER", "ROUTER_SSH_PORT", "DOCTOR_DOMAIN"]
DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
VLESS_RE = re.compile(r"vless://[^\s\"']+")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# ---------------- vless:// / xray-JSON -> sing-box outbound (для «Добавить ноду») --
def _mk_outbound(tag, host, port, uuid, sni, pbk, sid, fp, flow, net, svc):
    ob = {"type": "vless", "tag": tag, "server": host, "server_port": int(port),
          "uuid": uuid, "tls": {"enabled": True, "server_name": sni,
                                "utls": {"enabled": True, "fingerprint": fp or "chrome"},
                                "reality": {"enabled": True, "public_key": pbk}},
          "packet_encoding": "xudp"}
    if sid:
        ob["tls"]["reality"]["short_id"] = sid
    if net == "grpc":
        ob["transport"] = {"type": "grpc", "service_name": svc or "vless"}
    elif flow:
        ob["flow"] = flow
    return ob


def vless_to_outbound(url):
    u = urlsplit(url.strip())
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    host = u.hostname or ""
    ob = _mk_outbound(unquote(u.fragment or "") or host, host, u.port or 0, u.username or "",
                      q.get("sni", ""), q.get("pbk", ""), q.get("sid", ""), q.get("fp", ""),
                      q.get("flow", ""), q.get("type", ""), q.get("serviceName", ""))
    return ob, host, int(u.port or 0)


def xray_to_outbounds(data):
    out = []
    for o in data.get("outbounds", []):
        if o.get("protocol") != "vless":
            continue
        vn = (o.get("settings", {}).get("vnext") or [{}])[0]
        usr = (vn.get("users") or [{}])[0]
        ss = o.get("streamSettings", {})
        rs = ss.get("realitySettings", {})
        host, port = vn.get("address", ""), int(vn.get("port", 0) or 0)
        ob = _mk_outbound(o.get("tag", "") or host, host, port, usr.get("id", ""),
                          rs.get("serverName", ""), rs.get("publicKey", ""), rs.get("shortId", ""),
                          rs.get("fingerprint", ""), usr.get("flow", ""), ss.get("network", "tcp"),
                          (ss.get("grpcSettings", {}) or {}).get("serviceName", ""))
        out.append((ob, host, port))
    return out


def parse_nodes(text):
    urls = VLESS_RE.findall(text or "")
    if urls:
        return [vless_to_outbound(u) for u in urls]
    try:
        return xray_to_outbounds(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        return []


def sane_tag(raw, host, used):
    t = re.sub(r"[^A-Za-z0-9._-]", "", raw or "").strip("._-")
    if not t:
        t = "n" + (host.rsplit(".", 1)[-1] if "." in host else "node")
    base, i = t, 2
    while t in used:
        t = f"{base}-{i}"; i += 1
    used.add(t)
    return t


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
                    val = raw[1:].split('"', 1)[0]
                else:
                    part = raw.split("#", 1)[0].split()
                    val = part[0] if part else ""
                vals[m.group(1)] = val
    except FileNotFoundError:
        pass
    return {k: vals.get(k, "") for k in CONF_KEYS}


def write_conf(updates):
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
                cm = re.search(r'(\s+#.*)$', line.rstrip("\n"))
                lines[i] = f'{key}="{val}"' + (cm.group(1) if cm else "") + "\n"
                break
        else:
            lines.append(f'{key}="{val}"\n')
    with open(CONF, "w", encoding="utf-8") as f:
        f.writelines(lines)


def build_argv(action, args):
    if action in STATIC:
        argv = list(STATIC[action])
        if action == "doctor" and args:
            if not DOMAIN_RE.match(args[0]):
                raise ValueError("плохой домен")
            argv.append(args[0])
        return argv
    if action == "nodes-use":
        if not args or not NAME_RE.match(args[0]):
            raise ValueError("нужно имя ноды (латиница/цифры)")
        return ["nodes", "use", args[0]]
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
        svc = args[0] if args else "sing-box"
        if svc not in ("sing-box", "zapret", "dns"):
            raise ValueError("неизвестный лог")
        return ["logs", svc]
    raise ValueError(f"неизвестное действие: {action}")


def capture(argv, timeout=15):
    """Синхронно выполнить rctl argv, вернуть (rc, текст)."""
    try:
        p = subprocess.run(RCTL + argv, cwd=PANEL_DIR, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def read_meta():
    """rctl meta -> {model, host, lan}. Пусто, если роутер недоступен."""
    rc, out = capture(["meta"], timeout=12)
    meta = {"model": "", "host": "", "lan": ""}
    if rc == 0:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in meta:
                    meta[k.strip()] = v.strip()
    return meta


def stream_command(argv, on_line):
    env = dict(os.environ)
    env.setdefault("LC_ALL", env.get("LANG", "C.UTF-8"))
    try:
        proc = subprocess.Popen(
            RCTL + argv, cwd=PANEL_DIR,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True, encoding="utf-8", errors="replace", env=env,
        )
    except FileNotFoundError as e:
        on_line(f"[ошибка запуска rctl: {e}]")
        return 127
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    proc.stdout.close()
    return proc.wait()


class Handler(BaseHTTPRequestHandler):
    server_version = "router-panel/1.0"

    def log_message(self, *a):
        pass

    def _tok(self, qs):
        t = self.headers.get("X-Token") or qs.get("t", [""])[0]
        return secrets.compare_digest(t or "", TOKEN)

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _sse(self, worker):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(ev, payload):
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        try:
            rc = worker(emit)
            emit("done", {"rc": rc})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream(self, argv):
        self._sse(lambda emit: stream_command(argv, lambda ln: emit("line", ln)))

    def _addnode(self, text, to_auto=True):
        def worker(emit):
            L = lambda s: emit("line", s)
            nodes = parse_nodes(text)
            if not nodes:
                L("✗ не нашёл нод: это не vless-ссылка и не xray-JSON"); return 1
            L(f":: разобрал нод из вставки: {len(nodes)}")
            rc, cfgtext = capture(["getconfig"], timeout=15)
            if rc != 0 or not cfgtext.strip():
                L("✗ не удалось прочитать config.json с роутера (SSH? sing-box?)"); return 1
            try:
                cfg = json.loads(cfgtext)
            except json.JSONDecodeError as e:
                L(f"✗ config.json на роутере невалиден: {e}"); return 1
            obs = cfg.setdefault("outbounds", [])
            used = {o.get("tag", "") for o in obs if o.get("tag")}
            have = {(o.get("server"), o.get("server_port")) for o in obs if o.get("server")}
            sel = next((o for o in obs if o.get("type") == "selector"), None)
            auto = next((o for o in obs if o.get("type") == "urltest"), None)
            added = []
            for ob, host, port in nodes:
                if not host or not port:
                    L("  · пропускаю ноду без адреса/порта"); continue
                if (host, port) in have:
                    L(f"  · {host}:{port} уже есть — пропускаю"); continue
                tag = sane_tag(ob.get("tag", ""), host, used)
                ob["tag"] = tag
                obs.append(ob); have.add((host, port))
                # в КОНФИГЕ sing-box список группы — "outbounds" ("all" бывает только в ответах clash-API)
                if sel is not None and tag not in sel.setdefault("outbounds", []):
                    sel["outbounds"].append(tag)
                if to_auto and auto is not None and tag not in auto.setdefault("outbounds", []):
                    auto["outbounds"].append(tag)
                added.append(f"{tag} ({host}:{port})" + ("" if to_auto else " — только ручной выбор, в auto не добавлена"))
            if not added:
                L("  ничего нового: все ноды уже в конфиге"); return 0
            for a in added:
                L(f"  + {a}")
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(cfg, tmp, ensure_ascii=False, indent=2); tmp.close()
            L(f":: заливаю config (+{len(added)}) с проверкой sing-box check…")
            try:
                return stream_command(["putconfig", tmp.name], L)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        self._sse(worker)

    def _rotate(self, text):
        """Ротация подписки: gazvpn при обновлении меняет ТОЛЬКО uuid (сервера/ключи те же).
        Принимает vless-ссылку, xray-JSON или голый uuid — меняет uuid во всех нодах конфига."""
        def worker(emit):
            L = lambda s: emit("line", s)
            found = sorted({u.lower() for u in UUID_RE.findall(text or "")})
            if not found:
                L("✗ не нашёл uuid: вставь vless-ссылку, JSON из Happ или сам uuid"); return 1
            if len(found) > 1:
                L(f"✗ во вставке {len(found)} разных uuid ({', '.join(found)}) — вставь что-то одно"); return 1
            new = found[0]
            L(f":: новый uuid: {new}")
            rc, cfgtext = capture(["getconfig"], timeout=15)
            if rc != 0 or not cfgtext.strip():
                L("✗ не удалось прочитать config.json с роутера"); return 1
            try:
                cfg = json.loads(cfgtext)
            except json.JSONDecodeError as e:
                L(f"✗ config.json на роутере невалиден: {e}"); return 1
            node_obs = [o for o in cfg.get("outbounds", []) if "uuid" in o]
            distinct = {o["uuid"].lower() for o in node_obs}
            # если во вставке есть адреса серверов — по ним понимаем, ЧЬЮ подписку ротируем
            # (в конфиге могут жить ноды разных подписок с разными uuid — чужие не трогаем)
            try:
                pasted = parse_nodes(text)
            except Exception:
                pasted = []
            pasted_hosts = {(h, p) for _, h, p in pasted if h and p}
            old_set = None
            if pasted_hosts:
                matched = [o for o in node_obs if (o.get("server"), o.get("server_port")) in pasted_hosts]
                if matched:
                    old_set = {o["uuid"].lower() for o in matched}
                    L(f":: вставка совпала с нодами {' '.join(o.get('tag','?') for o in matched)} — ротирую только их подписку")
            if old_set is None:
                if len(distinct - {new}) > 1:
                    L("✗ в конфиге ноды с РАЗНЫМИ uuid (похоже, несколько подписок).")
                    L("  Вставь vless-ссылку/JSON узла именно той подписки, которую обновил, — по адресу сервера пойму, какие ноды её.")
                    return 1
                old_set = distinct
            changed, skipped = [], []
            for o in node_obs:
                if o["uuid"].lower() in old_set and o["uuid"].lower() != new:
                    o["uuid"] = new
                    changed.append(o.get("tag", "?"))
            skipped = [o.get("tag", "?") for o in cfg.get("outbounds", []) if o.get("type") == "trojan"]
            if not changed:
                L("✓ uuid уже актуален во всех нодах — менять нечего"); return 0
            L(f":: меняю uuid в нодах ({len(changed)}): {' '.join(changed)}")
            if skipped:
                L(f"!  trojan-ноды живут по паролю, uuid их не касается: {' '.join(skipped)} (если сломались — удали и добавь заново)")
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(cfg, tmp, ensure_ascii=False, indent=2); tmp.close()
            L(":: заливаю config с проверкой sing-box check…")
            try:
                return stream_command(["putconfig", tmp.name], L)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        self._sse(worker)

    def _delnode(self, tag):
        def worker(emit):
            L = lambda s: emit("line", s)
            rc, cfgtext = capture(["getconfig"], timeout=15)
            if rc != 0 or not cfgtext.strip():
                L("✗ не удалось прочитать config.json с роутера"); return 1
            try:
                cfg = json.loads(cfgtext)
            except json.JSONDecodeError as e:
                L(f"✗ config.json на роутере невалиден: {e}"); return 1
            obs = cfg.get("outbounds", [])
            NOT_NODES = ("selector", "urltest", "direct", "block", "dns")
            target = next((o for o in obs if o.get("tag") == tag and o.get("type") not in NOT_NODES), None)
            if target is None:
                L(f"✗ сервера '{tag}' нет среди нод (проверь имя в «Все серверы + пинг»)"); return 1
            if sum(1 for o in obs if o.get("type") not in NOT_NODES) <= 1:
                L("✗ это последняя нода — удалять нельзя (прокси останется без серверов)"); return 1
            cfg["outbounds"] = [o for o in obs if o is not target]
            # вычищаем тег из групп ("outbounds" в конфиге; "all" — только в clash-API) и из default
            for o in cfg["outbounds"]:
                if o.get("type") in ("selector", "urltest"):
                    if tag in o.get("outbounds", []):
                        o["outbounds"] = [t for t in o["outbounds"] if t != tag]
                    if o.get("default") == tag:
                        o.pop("default", None)
            L(f":: удаляю сервер {tag} ({target.get('server')}:{target.get('server_port')})")
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(cfg, tmp, ensure_ascii=False, indent=2); tmp.close()
            L(":: заливаю config с проверкой sing-box check…")
            try:
                return stream_command(["putconfig", tmp.name], L)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        self._sse(worker)

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
                    page = f.read()
            except FileNotFoundError:
                return self._send(500, "index.html не найден")
            return self._send(200, page.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if not self._tok(qs):
            return self._send(403, "bad token")
        if u.path == "/api/config":
            return self._json(200, read_conf())
        if u.path == "/api/meta":
            return self._json(200, read_meta())
        if u.path == "/api/run":
            try:
                argv = build_argv(qs.get("action", [""])[0], qs.get("arg", []))
            except ValueError as e:
                return self._send(400, str(e))
            return self._stream(argv)
        return self._send(404, "not found")

    def do_POST(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        if not self._tok(qs):
            return self._json(403, {"error": "bad token"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except json.JSONDecodeError:
            data = {}
        if u.path == "/api/config":
            write_conf(data)
            return self._json(200, read_conf())
        if u.path == "/api/run":
            try:
                argv = build_argv(data.get("action", ""), [str(a) for a in data.get("args", []) or []])
            except ValueError as e:
                return self._send(400, str(e))
            return self._stream(argv)
        if u.path == "/api/addnode":
            text = data.get("text", "")
            if not VLESS_RE.search(text) and "outbounds" not in text and "vnext" not in text:
                return self._send(400, "не похоже ни на vless-ссылку, ни на xray-JSON")
            return self._addnode(text, to_auto=bool(data.get("auto", True)))
        if u.path == "/api/rotate":
            return self._rotate(data.get("text", ""))
        if u.path == "/api/delnode":
            tag = str(data.get("tag", "")).strip()
            if not NAME_RE.match(tag):
                return self._send(400, "нужно имя ноды (латиница/цифры)")
            return self._delnode(tag)
        return self._json(404, {"error": "not found"})


def lan_ip():
    """Локальный IP в LAN (для ссылки с телефона) — без реального сетевого запроса."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1",
                     help="127.0.0.1 (по умолчанию, только этот комп) или 0.0.0.0 (видно всем в твоей Wi-Fi — для входа с телефона)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    print("Панель живого роутера")
    print(f"  {url}\n  Ctrl-C — стоп")
    if a.host != "127.0.0.1":
        ip = lan_ip()
        print("  ! слушает LAN (--host {}): в твоей сети видно любому устройству, знающему адрес+токен".format(a.host))
        if ip:
            print(f"  с телефона (та же Wi-Fi): http://{ip}:{port}/?t={TOKEN}")
    if not a.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nстоп")


if __name__ == "__main__":
    main()
