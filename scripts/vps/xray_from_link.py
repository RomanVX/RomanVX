#!/usr/bin/env python3
"""Конфиг Xray для VPS в РФ: запросы к Render идут через VPN владельца.

Render из РФ заблокирован, а через Cloudflare большие ответы режутся (~20 КБ).
Поэтому Caddy ходит в Render через локальный туннель Xray:

  127.0.0.1:10443 (dokodemo-door) ──VPN──▶ 216.24.57.16:443 (Render, TLS по SNI)
  127.0.0.1:10808 (socks5)        ──VPN──▶ куда угодно — для проверок и скачивания

Всё остальное на сервере (SSH, «Честный знак», сайт) идёт напрямую, VPN не трогает.

Использование:  python3 xray_from_link.py 'vless://…'  > /usr/local/etc/xray/config.json
Понимает ссылки vless://, trojan://, vmess://, ss:// и ссылку-подписку https://…
(список ссылок, в т.ч. base64) — берётся первая поддерживаемая.
"""
import base64
import json
import sys
import urllib.parse
import urllib.request

RENDER_IP = "216.24.57.16"


def b64d(s: str) -> str:
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")


def stream(q: dict, host: str) -> dict:
    """streamSettings из параметров ссылки (type/security/sni/…)."""
    g = lambda k, d="": (q.get(k) or [d])[0]
    net = g("type", "tcp")
    if net == "splithttp":
        net = "xhttp"
    sec = g("security", "none")
    st = {"network": net, "security": sec}
    sni = g("sni") or g("peer") or g("host") or host
    if sec == "reality":
        st["realitySettings"] = {"serverName": sni, "fingerprint": g("fp", "chrome"),
                                 "publicKey": g("pbk"), "shortId": g("sid"), "spiderX": g("spx")}
    elif sec == "tls":
        tls = {"serverName": sni, "fingerprint": g("fp", "chrome")}
        if g("alpn"):
            tls["alpn"] = g("alpn").split(",")
        if g("allowInsecure") in ("1", "true"):
            tls["allowInsecure"] = True
        st["tlsSettings"] = tls
    if net == "ws":
        st["wsSettings"] = {"path": g("path", "/"), "host": g("host")}
    elif net == "grpc":
        st["grpcSettings"] = {"serviceName": g("serviceName"), "multiMode": g("mode") == "multi"}
    elif net == "xhttp":
        st["xhttpSettings"] = {"path": g("path", "/"), "host": g("host"), "mode": g("mode", "auto")}
    elif net == "httpupgrade":
        st["httpupgradeSettings"] = {"path": g("path", "/"), "host": g("host")}
    elif net == "tcp" and g("headerType") == "http":
        st["tcpSettings"] = {"header": {"type": "http", "request": {
            "path": [g("path", "/")], "headers": {"Host": [g("host")] if g("host") else []}}}}
    return st


def outbound(link: str) -> dict:
    link = link.strip()
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        v = json.loads(b64d(link[8:]))
        q = {"type": [v.get("net") or "tcp"], "security": ["tls" if v.get("tls") == "tls" else "none"],
             "sni": [v.get("sni") or ""], "host": [v.get("host") or ""], "path": [v.get("path") or "/"],
             "fp": [v.get("fp") or "chrome"], "alpn": [v.get("alpn") or ""], "headerType": [v.get("type") or ""],
             "serviceName": [v.get("path") or ""]}
        return {"tag": "vpn", "protocol": "vmess",
                "settings": {"vnext": [{"address": v["add"], "port": int(v["port"]),
                                        "users": [{"id": v["id"], "alterId": int(v.get("aid") or 0),
                                                   "security": v.get("scy") or "auto"}]}]},
                "streamSettings": stream(q, v["add"])}
    u = urllib.parse.urlsplit(link)
    q = urllib.parse.parse_qs(u.query)
    if scheme == "vless":
        user = {"id": urllib.parse.unquote(u.username or ""), "encryption": "none"}
        flow = (q.get("flow") or [""])[0]
        if flow:
            user["flow"] = flow
        return {"tag": "vpn", "protocol": "vless",
                "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443, "users": [user]}]},
                "streamSettings": stream(q, u.hostname)}
    if scheme == "trojan":
        q.setdefault("security", ["tls"])
        return {"tag": "vpn", "protocol": "trojan",
                "settings": {"servers": [{"address": u.hostname, "port": u.port or 443,
                                          "password": urllib.parse.unquote(u.username or "")}]},
                "streamSettings": stream(q, u.hostname)}
    if scheme == "ss":
        body = link[5:].split("#", 1)[0]
        if "@" not in body:                      # ss://base64(method:pass@host:port)
            body = b64d(body.split("?", 1)[0])
        userinfo, hostport = body.rsplit("@", 1)
        userinfo = urllib.parse.unquote(userinfo)
        if ":" not in userinfo:                  # SIP002: base64(method:pass)
            userinfo = b64d(userinfo)
        method, password = userinfo.split(":", 1)
        host, port = hostport.split("?", 1)[0].rstrip("/").rsplit(":", 1)
        return {"tag": "vpn", "protocol": "shadowsocks",
                "settings": {"servers": [{"address": host.strip("[]"), "port": int(port),
                                          "method": method, "password": password}]}}
    raise ValueError(f"тип ссылки «{scheme}» Xray не поддерживает")


def pick(arg: str) -> dict:
    arg = arg.strip()
    if arg.lower().startswith(("http://", "https://")):     # ссылка-подписка
        req = urllib.request.Request(arg, headers={"User-Agent": "v2rayN/7.0"})
        text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace").strip()
        if "://" not in text:
            text = b64d(text)
        lines = [ln.strip() for ln in text.splitlines() if "://" in ln]
    else:
        lines = [arg]
    errors = []
    for ln in lines:
        host = urllib.parse.urlsplit(ln).hostname or ""
        if host in ("0.0.0.0", "127.0.0.1", "localhost"):   # служебные «серверы»-заглушки подписок
            errors.append(f"заглушка: {urllib.parse.unquote(ln.split('#', 1)[-1])[:60]}")
            continue
        try:
            return outbound(ln)
        except Exception as e:            # пробуем следующую ссылку из подписки
            errors.append(f"{ln.split('://')[0]}: {e}")
    raise SystemExit("Не нашёл подходящую ссылку. " + "; ".join(errors[:5]))


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        raise SystemExit("Передайте ссылку VPN: python3 xray_from_link.py 'vless://…'")
    out = pick(sys.argv[1])
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "render", "listen": "127.0.0.1", "port": 10443, "protocol": "dokodemo-door",
             "settings": {"address": RENDER_IP, "port": 443, "network": "tcp"}},
            {"tag": "socks", "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
             "settings": {"udp": False}},
        ],
        "outbounds": [out, {"tag": "direct", "protocol": "freedom"}],
    }
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    srv = out["settings"].get("vnext", out["settings"].get("servers", [{}]))[0]
    print(f"OK: {out['protocol']} через {srv.get('address')}:{srv.get('port')}", file=sys.stderr)


if __name__ == "__main__":
    main()
