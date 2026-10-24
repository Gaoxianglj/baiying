"""本机控制台：`companion web`。

只监听 127.0.0.1；启动时生成一次性令牌，首次打开带令牌的链接后写进 Cookie，
之后所有接口都校验 Cookie 和 Host 头（防止别的网站通过浏览器访问本机接口）。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from companion.config import load_config
from companion.core.companion import Companion
from companion.memory.layout import commit_memory
from companion.memory.people import SELF_ID
from companion.models.openai_compat import ModelError
from companion.runtime import AlreadyRunning, InstanceLock, run_dir, setup_logging, stop_flag
from companion.web import settings

log = logging.getLogger(__name__)

COOKIE = "companion_console"

Response = tuple[int, dict[str, str], bytes]


class ConsoleApp:
    """与 HTTP 无关的请求处理，方便测试。"""

    def __init__(self, root: Path, token: str, *, companion: Companion | None = None) -> None:
        self.root = root
        self.token = token
        self._companion = companion
        self._lock = threading.RLock()  # Companion 不能被多个请求同时使用
        self._limits_at: datetime | None = None

    @property
    def companion(self) -> Companion:
        if self._companion is None:
            self._companion = Companion(load_config(self.root))
        return self._companion

    def close(self) -> None:
        if self._companion is not None:
            self._companion.close()

    # ---- 路由 ----

    def handle(self, method: str, raw_path: str, headers: dict[str, str], body: bytes) -> Response:
        url = urlparse(raw_path)
        query = parse_qs(url.query)
        host = headers.get("host", "")
        if not (host.startswith("127.0.0.1:") or host.startswith("localhost:")):
            return _json({"error": "只接受本机访问"}, HTTPStatus.FORBIDDEN)

        if url.path in ("/", "/index.html"):
            given = (query.get("token") or [""])[0]
            if given and secrets.compare_digest(given, self.token):
                return (
                    HTTPStatus.SEE_OTHER,
                    {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/",
                    },
                    b"",
                )
            if not self._authed(headers):
                return _html(_LOCKED, HTTPStatus.UNAUTHORIZED)
            page = resources.files("companion.web").joinpath("static/index.html").read_bytes()
            return HTTPStatus.OK, {"Content-Type": "text/html; charset=utf-8"}, page

        if not url.path.startswith("/api/"):
            return _json({"error": "没有这个页面"}, HTTPStatus.NOT_FOUND)
        if not self._authed(headers):
            return _json(
                {"error": "未授权，请用启动时打印的链接打开控制台"}, HTTPStatus.UNAUTHORIZED
            )
        if method == "GET" and url.path.startswith("/api/sticker-image/"):
            digest = url.path.rsplit("/", 1)[-1]
            if not digest.isalnum():
                return _json({"error": "不合法"}, HTTPStatus.BAD_REQUEST)
            with self._lock:
                path = self.companion.stickers.image_path(digest)
            if not path.exists():
                return _json({"error": "没有这张表情"}, HTTPStatus.NOT_FOUND)
            return HTTPStatus.OK, {"Content-Type": "image/jpeg"}, path.read_bytes()
        if method == "POST" and headers.get("content-type", "").split(";")[0] != "application/json":
            return _json({"error": "只接受 JSON"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

        try:
            data = json.loads(body or b"{}") if method == "POST" else {}
            with self._lock:
                return _json(self._api(method, url.path, query, data))
        except (settings.SettingsError, KeyError, ValueError) as e:
            return _json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except ModelError as e:
            return _json({"error": f"模型服务不可用：{e}"}, HTTPStatus.SERVICE_UNAVAILABLE)

    def _api(self, method: str, path: str, query: dict[str, list[str]], data: dict) -> Any:
        parts = path.strip("/").split("/")[1:]
        route = (method, parts[0] if parts else "")
        if route == ("GET", "overview"):
            return self.overview(refresh="refresh" in query)
        if route == ("GET", "settings"):
            return {"fields": settings.describe(self.root)}
        if route == ("POST", "settings"):
            written = settings.save(self.root, data.get("changes") or {})
            return {"written": written, "fields": settings.describe(self.root)}
        if route == ("POST", "settings-reset"):
            settings.reset(self.root, str(data["path"]))
            return {"fields": settings.describe(self.root)}
        if route == ("GET", "self"):
            return self.self_files()
        if route == ("POST", "seed"):
            return self.save_seed(str(data.get("text", "")))
        if route == ("GET", "people") and len(parts) == 1:
            return self.people()
        if route == ("GET", "people") and len(parts) == 2:
            return self.person(parts[1])
        if route == ("POST", "forget"):
            return self.forget(str(data["kind"]), str(data["id"]))
        if route == ("GET", "stickers"):
            return self.sticker_list()
        if route == ("POST", "sticker-delete"):
            removed = self.companion.stickers.remove(str(data["digest"]))
            return {"removed": removed, **self.sticker_list()}
        if route == ("GET", "journal") and len(parts) == 1:
            return self.journal_list()
        if route == ("GET", "journal") and len(parts) == 2:
            return self.journal_entry(parts[1])
        raise KeyError(f"没有这个接口：{method} {path}")

    def _authed(self, headers: dict[str, str]) -> bool:
        cookie = SimpleCookie(headers.get("cookie", ""))
        value = cookie[COOKIE].value if COOKIE in cookie else ""
        return bool(value) and secrets.compare_digest(value, self.token)

    # ---- 接口实现 ----

    def overview(self, refresh: bool = False) -> dict[str, Any]:
        c = self.companion
        config = load_config(self.root)
        windows: list[dict[str, Any]] = []
        sub = c.gateway.subscription
        now = datetime.now(UTC)
        if sub is not None and (
            refresh or self._limits_at is None or (now - self._limits_at).seconds > 60
        ):
            try:
                sub.read_rate_limits()
                self._limits_at = now
            except ModelError as e:
                log.warning("读取订阅额度失败：%s", e)
        if sub is not None and sub.last_rate_limits is not None:
            seen = set()
            for w in sub.last_rate_limits.windows:
                key = (w.window_minutes, w.used_percent)
                if key in seen:
                    continue
                seen.add(key)
                windows.append(
                    {
                        "minutes": w.window_minutes,
                        "used": w.used_percent,
                        "resets_at": w.resets_at.isoformat() if w.resets_at else None,
                    }
                )
        fb = config.models.fallback
        spent = c.gateway.ledger.month_cost("fallback") if c.gateway.ledger else 0.0
        counts = c.index.counts()
        last_sleep = c.index.get_meta("sleep:last")
        journals = self.journal_list()["entries"]
        bot = config.qq.bot_account or ""
        return {
            "persona": config.persona.name,
            "backends": [
                {"name": s.name, "available": s.available, "detail": s.detail}
                for s in c.gateway.status()
            ],
            "quota_windows": windows,
            "fallback": {
                "model": fb.model,
                "spent": round(spent, 4),
                "budget": fb.monthly_budget_usd,
                "key_set": fb.api_key is not None,
            },
            "memory": {**counts, "people": len(c.people.list()), "last_sleep": last_sleep},
            "qq": {
                "account": (bot[:3] + "****" + bot[-3:]) if len(bot) > 6 else bot,
                "token_set": config.qq.access_token is not None,
                "groups": len(config.qq.groups),
                "owners": len(config.qq.owner_accounts),
            },
            "latest_journal": journals[0] if journals else None,
        }

    def self_files(self) -> dict[str, str]:
        d = self.companion.layout.self_dir
        return {
            name: (d / f"{name}.md").read_text("utf-8") if (d / f"{name}.md").exists() else ""
            for name in ("seed", "identity", "style")
        }

    def save_seed(self, text: str) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("种子不能是空的")
        c = self.companion
        path = c.layout.self_dir / "seed.md"
        path.write_text(text.replace("\r\n", "\n").rstrip() + "\n", encoding="utf-8", newline="\n")
        committed = commit_memory(c.layout, c.name, "主人在控制台修改了种子")
        return {"saved": True, "committed": committed}

    def people(self) -> dict[str, Any]:
        c = self.companion
        c.people.reload()
        out = []
        for p in c.people.list():
            out.append(
                {
                    "id": p.person_id,
                    "names": p.names,
                    "accounts": p.accounts,
                    "facts": len(c.index.current_facts(p.person_id)),
                    "episodes": len(c.index.episodes_with(p.person_id, limit=1000)),
                }
            )
        out.sort(key=lambda x: x["facts"] + x["episodes"], reverse=True)
        return {"people": out, "self_facts": len(c.index.current_facts(SELF_ID))}

    def person(self, person_id: str) -> dict[str, Any]:
        c = self.companion
        c.people.reload()
        if person_id == SELF_ID:
            names, body = [c.name], ""
        else:
            p = c.people.get(person_id)
            if p is None:
                raise KeyError(f"没有这个人：{person_id}")
            names, body = p.names, p.body
        facts = c.index.current_facts(person_id)
        facts.sort(key=lambda f: f.valid_from, reverse=True)
        episodes = c.index.episodes_with(person_id, limit=30)
        return {
            "id": person_id,
            "names": names,
            "profile": body.strip(),
            "facts": [
                {
                    "id": f.id,
                    "statement": f.statement,
                    "pinned": f.pinned,
                    "since": f.valid_from.isoformat(),
                    "confidence": f.confidence,
                }
                for f in facts
            ],
            "episodes": [
                {
                    "id": e.id,
                    "summary": e.summary,
                    "when": e.started_at.isoformat(),
                    "state": e.state,
                }
                for e in episodes
            ],
        }

    def forget(self, kind: str, memory_id: str) -> dict[str, Any]:
        if kind not in ("fact", "episode"):
            raise ValueError("kind 只能是 fact 或 episode")
        n_mem, n_msg = self.companion.forget([(kind, memory_id)])  # type: ignore[list-item]
        return {"memories": n_mem, "messages": n_msg}

    def sticker_list(self) -> dict[str, Any]:
        c = self.companion
        names = {p.person_id: (p.names[0] if p.names else p.person_id) for p in c.people.list()}
        items = []
        for s in c.stickers.all():
            items.append(
                {
                    "digest": s.digest,
                    "description": s.description,
                    "emotion": s.emotion,
                    "seen": s.seen,
                    "used": s.used,
                    "first_seen": s.first_seen,
                    "from": [names.get(p, p) for p in s.from_people],
                }
            )
        return {
            "stickers": items,
            "emotions": c.stickers.emotions(),
            "capacity": c.config.stickers.capacity,
        }

    def journal_list(self) -> dict[str, Any]:
        d = self.companion.layout.journal_dir
        entries = []
        for p in sorted(d.glob("*.md"), reverse=True):
            text = p.read_text("utf-8").split("\n", 2)[-1].strip()
            entries.append({"date": p.stem, "preview": text[:80]})
        return {"entries": entries}

    def journal_entry(self, date: str) -> dict[str, Any]:
        if not all(ch.isdigit() or ch == "-" for ch in date):
            raise ValueError("日期格式不对")
        p = self.companion.layout.journal_dir / f"{date}.md"
        if not p.exists():
            raise KeyError(f"没有 {date} 的日记")
        return {"date": date, "text": p.read_text("utf-8").split("\n", 2)[-1].strip()}


def _json(data: Any, status: int = HTTPStatus.OK) -> Response:
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    return status, {"Content-Type": "application/json; charset=utf-8"}, body


def _html(text: str, status: int) -> Response:
    return status, {"Content-Type": "text/html; charset=utf-8"}, text.encode("utf-8")


_LOCKED = """<!doctype html><meta charset="utf-8"><title>白璎控制台</title>
<body style="font-family:system-ui,'Microsoft YaHei',sans-serif;background:#f7f5ff;color:#2a2340;
display:grid;place-items:center;height:100vh;margin:0"><div style="text-align:center">
<div style="font-size:40px">🔒</div><p>请使用 <code>companion web</code> 启动时打印的链接打开控制台。</p></div>"""


def make_handler(app: ConsoleApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, extra, payload = app.handle(method, self.path, headers, body)
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug(format, *args)

    return Handler


def console_token(data_dir: Path, *, rotate: bool = False) -> str:
    """固定令牌：存在 data/run/console_token，地址可以收藏；`rotate` 时换一个新的。"""
    path = run_dir(data_dir) / "console_token"
    if not rotate and path.exists():
        token = path.read_text("utf-8").strip()
        if len(token) >= 24:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    return token


def run_web(
    root: Path,
    port: int,
    open_browser: bool,
    *,
    log_file: Path | None = None,
    rotate_token: bool = False,
) -> int:
    setup_logging(log_file)
    data_dir = load_config(root).data_dir
    try:
        lock = InstanceLock(run_dir(data_dir) / "web.lock").acquire()
    except AlreadyRunning as e:
        log.error("%s，不再重复启动", e)
        return 3
    flag = stop_flag(data_dir, "web")  # 由启停脚本创建和清理
    token = console_token(data_dir, rotate=rotate_token)
    app = ConsoleApp(root, token)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    (run_dir(data_dir) / "console_url.txt").write_text(url, encoding="utf-8")
    print(f"控制台已启动：{url}\n（只有本机能访问；按 Ctrl+C 退出）", flush=True)
    log.info("控制台已启动：http://127.0.0.1:%d/", server.server_port)

    def watch_stop() -> None:
        while not flag.exists():
            threading.Event().wait(1)
        log.info("收到停止信号，控制台退出")
        server.shutdown()

    threading.Thread(target=watch_stop, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
        lock.release()
    return 0
