"""Несколько кабинетов на одном сервере Render (например, Технолюкс + Аврора на 2 ГБ).

Дашборд устроен как «один процесс — один кабинет» (кабинет, ключи и кеши
задаются окружением процесса). Этот запускатель поднимает по процессу uvicorn
на каждый кабинет и маленький маршрутизатор на $PORT перед ними:

  • кабинет выбирается cookie `mp_cab` (ставит переход /cab/<id> — им пользуется
    экран выбора кабинета) или поддоменом вида <id>.<домен>;
  • без выбора — первый кабинет из списка;
  • упавший процесс (например, по памяти) перезапускается, второй кабинет при
    этом продолжает работать.

Настройка (env сервиса на Render), старт: `python multi.py`
  MULTI_CABINETS=tl,avr      — список; первый — основной (по умолчанию)
  Основной кабинет берёт обычные переменные (CABINET, DB_SCHEMA, WB_API_KEY…).
  Остальные — переменные с префиксом <ID>__, например:
    AVR__WB_API_KEY, AVR__ADMIN_PASSWORD, AVR__DB_SCHEMA (по умолчанию = id).
  Ключи площадок, токены и пароли основного кабинета остальным НЕ передаются.
Без MULTI_CABINETS запускается обычный одиночный дашборд.
"""
import asyncio
import os
import re
import subprocess
import sys
import time

# секреты/настройки основного кабинета, которые не должны «протечь» в другие
_PRIVATE = ("WB_API_KEY", "WB_ADVERT_KEY", "OZON_CLIENT_ID", "OZON_API_KEY",
            "OZON_PERF_CLIENT_ID", "OZON_PERF_CLIENT_SECRET", "YM_API_KEY", "YM_CAMPAIGN_ID",
            "YM_BUSINESS_ID", "ADMIN_LOGIN", "ADMIN_PASSWORD", "RESET_OWNER_PASSWORD",
            "OWNER_API_TOKEN", "CABINET", "CABINET_NAME", "CABINET_MONO", "DB_SCHEMA",
            "OTHER_CABINET_URL", "OTHER_CABINET_NAME", "OTHER_CABINET_MONO", "DEMO_FILL",
            "FEEDBACK_WRITE", "READ_ONLY", "UI_PROFILE", "SESSION_COOKIE", "TG_BOT_TOKEN",
            "TG_CHAT_ID", "WB_AGENT_TOKEN", "BG_TASKS")

BASE_PORT = 8101


def _cabinets() -> list[str]:
    return [c.strip().lower() for c in os.getenv("MULTI_CABINETS", "").split(",") if c.strip()]


def _child_env(cab: str, primary: bool, cabs: list[str]) -> dict:
    env = dict(os.environ)
    env.pop("PORT", None)
    if not primary:
        for k in _PRIVATE:
            env.pop(k, None)
        pref = cab.upper() + "__"
        for k, v in os.environ.items():
            if k.startswith(pref):
                env[k[len(pref):]] = v
        env.setdefault("DB_SCHEMA", cab)
        env.setdefault("SESSION_COOKIE", f"mp_session_{cab}")
    env["CABINET"] = env.get("CABINET") or cab
    # тяжёлый прогрев финансов — по очереди, а не всеми кабинетами разом
    env.setdefault("WARM_DELAY", str(180 + 420 * cabs.index(cab)))
    # экран выбора кабинета: карточка соседнего кабинета ведёт на /cab/<id>
    others = [c for c in cabs if c != cab]
    if others and not env.get("OTHER_CABINET_URL"):
        env["OTHER_CABINET_URL"] = f"/cab/{others[0]}"
    for k in [k for k in env if re.match(r"^[A-Z0-9]+__", k)]:
        env.pop(k)
    return env


class Child:
    def __init__(self, cab: str, port: int, env: dict):
        self.cab, self.port, self.env = cab, port, env
        self.proc: subprocess.Popen | None = None
        self.restarts = 0

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--timeout-keep-alive", "75"],
            env=self.env, cwd=os.path.dirname(os.path.abspath(__file__)))
        print(f"[multi] {self.cab}: pid {self.proc.pid}, порт {self.port}", flush=True)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


CHILDREN: dict[str, Child] = {}
DEFAULT = ""


async def _supervise():
    while True:
        await asyncio.sleep(5)
        for ch in CHILDREN.values():
            if not ch.alive():
                code = ch.proc.returncode if ch.proc else None
                ch.restarts += 1
                print(f"[multi] {ch.cab} завершился (код {code}) — перезапуск #{ch.restarts}", flush=True)
                ch.start()


_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
        "trailers", "transfer-encoding", "upgrade", "host"}
_client = None


def _pick(scope) -> str:
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
    host = headers.get("host", "").split(":")[0]
    sub = host.split(".")[0] if host.count(".") >= 2 else ""
    if sub in CHILDREN:
        return sub
    m = re.search(r"(?:^|;\s*)mp_cab=([a-z0-9_-]+)", headers.get("cookie", ""))
    if m and m.group(1) in CHILDREN:
        return m.group(1)
    return DEFAULT


async def _plain(send, status: int, body: bytes, headers=None):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")] + (headers or [])})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    global _client
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                import httpx
                _client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10), follow_redirects=False)
                asyncio.get_event_loop().create_task(_supervise())
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                for ch in CHILDREN.values():
                    if ch.alive():
                        ch.proc.terminate()
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    path = scope.get("path") or "/"
    # переход в кабинет: /cab/<id>[/...] → cookie + редирект на главную
    m = re.match(r"^/cab/([a-z0-9_-]+)/?$", path)
    if m and m.group(1) in CHILDREN:
        qs = scope.get("query_string", b"").decode()
        loc = "/" + (("?" + qs) if qs else "?enter=1")
        await _plain(send, 302, b"", [(b"location", loc.encode()),
                                      (b"set-cookie", f"mp_cab={m.group(1)}; Path=/; Max-Age=31536000; SameSite=Lax".encode())])
        return
    if path == "/multi/health":
        st = {c: ("up" if ch.alive() else "down") + f", перезапусков {ch.restarts}" for c, ch in CHILDREN.items()}
        await _plain(send, 200, repr(st).encode())
        return
    ch = CHILDREN[_pick(scope)]
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    qs = scope.get("query_string", b"").decode()
    raw = scope.get("raw_path") or path.encode()
    url = f"http://127.0.0.1:{ch.port}{raw.decode('latin-1')}"
    if qs:
        url += "?" + qs
    hdrs = [(k.decode(), v.decode("latin-1")) for k, v in scope.get("headers") or [] if k.decode().lower() not in _HOP]
    client_ip = (scope.get("client") or ["", 0])[0]
    hdrs.append(("x-forwarded-for", client_ip))
    host = next((v.decode() for k, v in scope.get("headers") or [] if k.lower() == b"host"), "")
    if host:
        hdrs.append(("x-forwarded-host", host))
    try:
        req = _client.build_request(scope["method"], url, headers=hdrs, content=body)
        resp = await _client.send(req, stream=True)
    except Exception as e:
        await _plain(send, 502, f"Кабинет «{ch.cab}» перезапускается, обновите страницу через минуту ({type(e).__name__})".encode())
        return
    out_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in resp.headers.multi_items()
                   if k.lower() not in _HOP]
    await send({"type": "http.response.start", "status": resp.status_code, "headers": out_headers})
    try:
        async for chunk in resp.aiter_raw():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
    finally:
        await resp.aclose()
    await send({"type": "http.response.body", "body": b""})


def main():
    global DEFAULT
    cabs = _cabinets()
    here = os.path.dirname(os.path.abspath(__file__))
    port = int(os.getenv("PORT", "8000"))
    if not cabs:      # обычный режим: один кабинет
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0",
                                  "--port", str(port)])
    DEFAULT = cabs[0]
    for i, cab in enumerate(cabs):
        CHILDREN[cab] = Child(cab, BASE_PORT + i, _child_env(cab, i == 0, cabs))
    for ch in CHILDREN.values():
        ch.start()
        time.sleep(3)
    import uvicorn
    os.chdir(here)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
