"""관리자 UI HTTP 서버 (표준 라이브러리 http.server).

보안:
- 기본은 127.0.0.1 에만 바인딩. Host 헤더를 검사해 DNS 리바인딩을 막는다.
- 시작할 때마다 새 토큰을 만든다. 페이지는 ?t=<토큰> 이 있어야 열리고,
  API 는 X-Tenure-Token 헤더가 맞아야 응답한다. 다른 사이트가 브라우저를 통해
  실행 버튼(= 셸 권한이 있는 에이전트 실행)을 누르는 것을 막는다.
- 정적 파일은 화이트리스트, CSP 로 인라인 스크립트 금지.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from tenure import procs
from tenure.config import CLAUDE_MODEL_SUGGESTIONS, EFFORT_SUGGESTIONS, ConfigError, load_config
from tenure.doctor import ACTIONS, PATH_PREFIX, action_shell
from tenure import __version__
from tenure.remote import Remote, RemoteError, package_digest
from tenure.runner import run_process
from tenure.settings import LOCAL_MACHINE, SettingsError, load_settings, save_settings, validate_machine
from tenure.ui.dashboard import ApiError, Dashboard, dispatch
from tenure.agentconfig import agent_config
from tenure.selfcmd import self_argv
from tenure.runner import pid_alive
from tenure.registry import tenure_home
from tenure.term.session import SessionError, SessionManager
from tenure.ui.terminal import TermJob
from tenure.usage import codex_home

STATIC_FILES = {"app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
# 함께 배포하는 글꼴 (Pretendard, SIL OFL 1.1 — static/fonts/Pretendard-LICENSE.txt)
STATIC_BINARY = {"fonts/PretendardVariable.woff2": "font/woff2"}
# Claude·Codex 로고: static/logos/ 에 함께 배포한다. $TENURE_HOME/logos/<이름>.svg 가 있으면 그것을 쓴다
LOGO_NAMES = ("claude", "codex")
LOGO_MAX_BYTES = 300_000
MAX_BODY = 1_000_000
TOKEN_PLACEHOLDER = "__TENURE_TOKEN__"
COOKIE_PREFIX = "tenure_t_"
COOKIE_MAX_AGE = 30 * 24 * 3600
TOKEN_FILE = "ui-token"
# style 'unsafe-inline': 터미널(xterm)이 글자색·글꼴을 <style> 요소로 넣는다 (36.22). 스크립트는 여전히 'self' 만.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TenureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], dashboard: Dashboard, token: str):
        super().__init__(addr, Handler)
        self.dashboard = dashboard
        self.token = token
        self.loopback = is_loopback(addr[0])
        self.terms: dict[str, TermJob] = {}
        self._terms_lock = threading.Lock()
        self._remote_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.loaded_hash = package_digest()
        self.started_at = time.time()
        self.restart_fn: Callable[["TenureServer"], None] = exec_restart
        self._digest_cache: tuple[float, str] = (0.0, self.loaded_hash)
        self._sessions: SessionManager | None = None

    def server_close(self) -> None:
        if self._sessions is not None:           # 서버와 함께 세션도 끝낸다(기록은 남음)
            self._sessions.shutdown()
        super().server_close()

    # ================================================================ 대화형 세션 (DESIGN.md 36.19)
    @property
    def sessions(self) -> SessionManager:
        if self._sessions is None:
            self._sessions = SessionManager(tenure_home())
        return self._sessions

    def _session(self, sid: Any):
        try:
            return self.sessions.get(sid)
        except SessionError as e:
            raise ApiError(404, str(e)) from e

    def session_create(self, machine: str, body: dict[str, Any]) -> dict[str, Any]:
        if machine != LOCAL_MACHINE:
            raise ApiError(400, "원격 머신의 대화형 세션은 아직 지원하지 않습니다.")
        dash = self.dashboard
        wd = dash.project(body.get("project"))
        run_id = body.get("run") or None
        cwd = dash.code_dir(dash.run_dir(wd, run_id)) if run_id else wd
        agent = str(body.get("agent") or "")
        try:
            cfg = load_config(wd)                # 실행과 같은 실행 파일 (claude.bin·codex.bin)
        except ConfigError as e:
            raise ApiError(400, f"설정 오류: {e}") from e
        bin_ = getattr(cfg, agent).bin if agent in ("claude", "codex") else None
        try:
            s = self.sessions.create(agent, cwd, str(wd), run_id=run_id, bin=bin_,
                                     rows=_int(body.get("rows")) or 30, cols=_int(body.get("cols")) or 120,
                                     title=str(body.get("title") or "")[:120])
        except SessionError as e:
            raise ApiError(400, str(e)) from e
        return s.summary()

    def session_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        s = self._session(body.get("id"))
        try:
            if action == "input":
                data = body.get("data")
                if not isinstance(data, str) or len(data) > 65_536:
                    raise ApiError(400, "입력이 잘못되었습니다.")
                s.write(data)
            elif action == "send":
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
                    raise ApiError(400, "보낼 메시지를 입력하세요.")
                s.send(text, force=bool(body.get("force")))
            elif action == "answer":
                return {"ok": True, "keys": s.answer(bool(body.get("approve")))}
            elif action == "resize":
                s.resize(max(5, min(_int(body.get("rows")) or 30, 300)), max(20, min(_int(body.get("cols")) or 120, 500)))
            elif action == "kill":
                s.kill()
            elif action == "seen":
                self.sessions.mark_seen(s.meta.id)
            elif action == "close":
                self.sessions.close(s.meta.id)
            else:
                raise ApiError(404, "없는 동작입니다.")
        except SessionError as e:
            raise ApiError(409, str(e)) from e
        except OSError as e:
            raise ApiError(409, f"세션에 쓰지 못했습니다: {e}") from e
        return {"ok": True}

    # ================================================================ 서버 자신
    def current_hash(self) -> str:
        at, h = self._digest_cache
        if time.monotonic() - at > 5:
            h = package_digest()
            self._digest_cache = (time.monotonic(), h)
        return h

    def server_info(self) -> dict[str, Any]:
        cur = self.current_hash()
        running = [t.title for t in self.terms.values() if t.running]
        return {"version": __version__, "loaded": self.loaded_hash[:12], "current": cur[:12],
                "stale": bool(cur and self.loaded_hash and cur != self.loaded_hash),
                "uptime_sec": int(time.time() - self.started_at), "running_terms": running,
                "logos": {n: True for n in LOGO_NAMES}, "native_picker": native_picker_available()}

    def restart(self) -> dict[str, Any]:
        running = [t.title for t in self.terms.values() if t.running]
        if self._sessions is not None:
            running += [f"세션 {x['agent']}" for x in self._sessions.list() if x["live"]]
        if running:
            raise ApiError(409, f"진행 중인 터미널 작업이 있어 다시 시작할 수 없습니다: {', '.join(running)}")

        def later() -> None:
            time.sleep(0.4)  # 응답이 브라우저에 도착할 시간
            self.restart_fn(self)

        threading.Thread(target=later, daemon=True).start()
        return {"restarting": True}

    @property
    def port(self) -> int:
        return self.server_address[1]

    def allowed_hosts(self) -> set[str]:
        p = self.port
        return {f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"}

    # ================================================================ machines
    def machines(self) -> list[dict[str, Any]]:
        try:
            return list(load_settings().get("machines") or [])
        except SettingsError:
            return []

    def native_pick(self, start: Any) -> dict[str, Any]:
        """OS 폴더 선택 창을 띄우고 고른 경로를 돌려준다(취소하면 path=None). 이 머신에서만 (DESIGN.md 28.9)."""
        if not native_picker_available():
            raise ApiError(400, "이 서버에서는 폴더 선택 창을 띄울 수 없습니다.")
        if not _pick_lock.acquire(blocking=False):
            raise ApiError(409, "폴더 선택 창이 이미 열려 있습니다.")
        try:
            initial = str(start or "").strip()
            initial = str(Path(initial).expanduser()) if initial and Path(initial).expanduser().is_dir() else ""
            r = run_process(self_argv("_pick", initial), Path.home(), timeout_sec=PICK_TIMEOUT_SEC,
                            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        finally:
            _pick_lock.release()
        if r.timed_out:
            raise ApiError(504, "폴더 선택 창이 너무 오래 열려 있어 닫았습니다.")
        if r.exit_code != 0:
            raise ApiError(500, f"폴더 선택 창을 띄우지 못했습니다: {(r.stderr or '').strip()[-300:]}")
        picked = (r.stdout or "").strip()
        return {"path": os.path.normpath(picked) if picked else None}

    def machine(self, mid: str) -> dict[str, Any]:
        for m in self.machines():
            if m["id"] == mid:
                return m
        raise ApiError(404, f"등록되지 않은 머신입니다: {mid}")

    def call(self, machine: str, method: str, args: dict[str, Any]) -> Any:
        if machine == LOCAL_MACHINE:
            return dispatch(self.dashboard, method, args)
        try:
            return Remote(self.machine(machine)).api(method, args)
        except RemoteError as e:
            raise ApiError(e.status, e.message) from e

    def all_projects(self) -> dict[str, Any]:
        local = dispatch(self.dashboard, "projects", {})
        groups = [{"id": LOCAL_MACHINE, "name": "이 머신", "local": True, **local}]
        remotes = self.machines()
        results: dict[str, dict[str, Any]] = {}

        def fetch(m: dict[str, Any]) -> None:
            cached = self._remote_cache.get(m["id"])
            if cached and time.monotonic() - cached[0] < REMOTE_CACHE_SEC:
                results[m["id"]] = cached[1]
                return
            try:
                data = Remote(m).api("projects", {}, timeout=20)
                res = {"projects": data["projects"], "jobs": data["jobs"], "error": None}
            except RemoteError as e:
                res = {"projects": [], "jobs": [], "error": e.message}
            self._remote_cache[m["id"]] = (time.monotonic(), res)
            results[m["id"]] = res

        threads = [threading.Thread(target=fetch, args=(m,), daemon=True) for m in remotes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(25)
        for m in remotes:
            r = results.get(m["id"], {"projects": [], "jobs": [], "error": "응답 없음"})
            groups.append({"id": m["id"], "name": m.get("name") or m["id"], "local": False,
                           "ssh": m["ssh"], "workspace_root": m.get("workspace_root") or "", **r})
        # 예전 화면 호환: 최상위 projects/jobs 는 이 머신 것
        return {"machines": groups, "projects": local["projects"], "jobs": local["jobs"]}

    def probe_machine(self, mid: str) -> dict[str, Any]:
        m = self.machine(mid)
        info = Remote(m).probe()
        if info.get("python") and info["python"] != m.get("python"):
            data = load_settings()
            for x in data["machines"]:
                if x["id"] == mid:
                    x["python"] = info["python"]
            save_settings(data)
        return info

    def deploy_machine(self, mid: str) -> dict[str, Any]:
        m = self.machine(mid)
        if not m.get("python"):
            info = self.probe_machine(mid)
            if not info.get("reachable"):
                raise ApiError(502, info.get("error") or "연결 실패")
            if not info.get("python"):
                raise ApiError(400, "원격에 Python 3.11 이상이 없습니다. 먼저 'uv 설치' → 'Python 3.12 설치'를 하세요.")
            m = self.machine(mid)
        try:
            out = Remote(m).deploy()
        except RemoteError as e:
            raise ApiError(e.status, e.message) from e
        self._remote_cache.pop(mid, None)
        return out

    # ================================================================ settings
    def settings_view(self) -> dict[str, Any]:
        try:
            data = load_settings()
            err = None
        except SettingsError as e:
            data, err = {"workspace_roots": [], "defaults": {}, "machines": []}, str(e)
        from tenure.options import schema
        from tenure.workflow import list_workflows

        try:
            names = [w["id"] for w in list_workflows(None) if w.get("source") != "error"]
        except Exception:  # noqa: BLE001 - 목록을 못 읽어도 설정 화면은 뜬다
            names = []
        return {"settings": data, "error": err, "suggestions": model_suggestions(),
                "effective": self.dashboard._defaults(None), "options": schema(workflows=names)}

    def save_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        data = load_settings()
        if "workspace_roots" in body:
            data["workspace_roots"] = [str(r).strip() for r in body["workspace_roots"] if str(r).strip()]
        if "defaults" in body:
            # 기본 실행 설정 카드는 자기 칸만 보낸다. 고급 설정(options)에서 저장한 값은 지우지 않는다
            from tenure.options import BY_KEY

            kept = {k: v for k, v in (data.get("defaults") or {}).items() if k in BY_KEY}
            data["defaults"] = {**kept, **_clean_defaults(body["defaults"])}
        if "options" in body:
            # 고급 설정 카드 (DESIGN.md 28.4): 빈 값은 '기본값 사용' → 지운다
            from tenure.options import BY_KEY, OptionError, parse

            if not isinstance(body["options"], dict):
                raise ApiError(400, "options 는 객체여야 합니다.")
            d = dict(data.get("defaults") or {})
            for k, raw in body["options"].items():
                if k not in BY_KEY:
                    raise ApiError(400, f"알 수 없는 설정입니다: {k}")
                try:
                    v = parse(k, raw)
                except OptionError as e:
                    raise ApiError(400, str(e)) from e
                if v is None:
                    d.pop(k, None)
                else:
                    d[k] = v
            data["defaults"] = d
        try:
            save_settings(data)
        except SettingsError as e:
            raise ApiError(400, str(e)) from e
        return self.settings_view()

    def upsert_machine(self, body: dict[str, Any]) -> dict[str, Any]:
        m = {k: body.get(k) for k in ("id", "name", "ssh", "port", "workspace_root", "python") if body.get(k) not in (None, "")}
        if "port" in m:
            try:
                m["port"] = int(m["port"])
            except (TypeError, ValueError):
                raise ApiError(400, "포트는 숫자여야 합니다.") from None
        try:
            validate_machine(m)
        except SettingsError as e:
            raise ApiError(400, str(e)) from e
        data = load_settings()
        others = [x for x in data["machines"] if x["id"] != m["id"]]
        prev = next((x for x in data["machines"] if x["id"] == m["id"]), {})
        if "python" not in m and prev.get("python") and prev.get("ssh") == m["ssh"]:
            m["python"] = prev["python"]
        data["machines"] = [*others, m]
        try:
            save_settings(data)
        except SettingsError as e:
            raise ApiError(400, str(e)) from e
        self._remote_cache.pop(m["id"], None)
        return self.settings_view()

    def delete_machine(self, mid: str | None) -> dict[str, Any]:
        data = load_settings()
        data["machines"] = [x for x in data["machines"] if x["id"] != mid]
        save_settings(data)
        self._remote_cache.pop(mid or "", None)
        return self.settings_view()

    # ================================================================ setup / terminal jobs
    def actions_view(self) -> dict[str, Any]:
        return {"actions": {a.id: {"title": a.title, "interactive": a.interactive, "note": a.note, "command": a.shell,
                                   "local_command": a.local_command(), "local_available": a.available_locally()}
                            for a in ACTIONS.values()}}

    def start_action(self, machine: str, action_id: Any) -> dict[str, Any]:
        try:
            action = action_shell(str(action_id))
        except KeyError:
            raise ApiError(400, "알 수 없는 동작입니다.") from None
        if machine == LOCAL_MACHINE and not action.available_locally():
            raise ApiError(400, f"'{action.title}' 는 이 OS 에서 지원하지 않습니다. 직접 설치하세요.")
        mode = "pty"
        if machine == LOCAL_MACHINE:
            argv, mode = local_action_argv(action)
        else:
            argv = Remote(self.machine(machine)).term_argv(action.shell)
        with self._terms_lock:
            for t in self.terms.values():
                if t.running and t.machine == machine and t.title == action.title:
                    raise ApiError(409, f"'{action.title}' 가 이미 실행 중입니다.")
            tid = secrets.token_hex(6)
            job = TermJob(id=tid, title=action.title, argv=argv, machine=machine, note=action.note, mode=mode)
            self.terms[tid] = job
        job.start()
        self._remote_cache.pop(machine, None)
        return job.snapshot()

    def _term(self, tid: Any) -> TermJob:
        job = self.terms.get(str(tid))
        if job is None:
            raise ApiError(404, "터미널 작업을 찾을 수 없습니다.")
        return job

    def term(self, tid: Any, offset: int) -> dict[str, Any]:
        return self._term(tid).snapshot(offset)

    def terms_list(self) -> list[dict[str, Any]]:
        out = []
        for t in sorted(self.terms.values(), key=lambda x: x.started_at, reverse=True)[:20]:
            snap = t.snapshot(10**12)
            out.append({k: snap[k] for k in ("id", "title", "machine", "running", "returncode", "started_at")})
        return out

    def term_input(self, tid: Any, text: Any) -> dict[str, Any]:
        if not isinstance(text, str) or len(text) > 4000 or "\x03" in text:
            raise ApiError(400, "입력이 잘못되었습니다.")
        try:
            self._term(tid).write(text)
        except (RuntimeError, OSError) as e:
            raise ApiError(409, str(e)) from e
        return {"ok": True}

    def term_kill(self, tid: Any) -> dict[str, Any]:
        self._term(tid).kill()
        return {"ok": True}

    def url(self) -> str:
        host = self.server_address[0]
        shown = "127.0.0.1" if host in ("", "0.0.0.0", "::") or is_loopback(host) else host
        return f"http://{shown}:{self.port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: TenureServer
    protocol_version = "HTTP/1.1"
    server_version = "tenure-ui"

    # ---------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # 조용히
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if "Content-Security-Policy" not in (extra or {}):
            self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _host_ok(self) -> bool:
        if not self.server.loopback:
            return True  # 외부 바인딩은 토큰으로만 보호(경고는 시작할 때 출력)
        return self.headers.get("Host", "") in self.server.allowed_hosts()

    def _origin_ok(self) -> bool:
        """Origin 헤더가 있으면 허용 Host 와 같아야 한다 (DESIGN.md 24.2). 없으면(탐색·같은 출처 GET) 통과."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        u = urlsplit(origin)
        if u.scheme not in ("http", "https") or not u.netloc:
            return False                  # "null" (sandbox iframe, file://) 등
        allowed = self.server.allowed_hosts() if self.server.loopback else {self.headers.get("Host", "")}
        return u.netloc in allowed

    def _fetch_site_ok(self) -> bool:
        """API 는 같은 출처 요청만 (Sec-Fetch-Site 를 보내는 브라우저). 헤더가 없으면(curl 등) 토큰으로 판단."""
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def _token_ok(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.server.token.encode())

    # ---------------------------------------------------------------- verbs
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _discard_body(self) -> None:
        """거절할 때도 요청 본문을 읽어 버린다. 안 읽고 닫으면 윈도우가 연결을 끊어(RST) 상대가 응답을 못 받는다."""
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
        except ValueError:
            n = 0
        if n > 0:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            self._discard_body()
            self._text(HTTPStatus.FORBIDDEN, "허용되지 않은 Host 입니다.")
            return
        if not self._origin_ok():
            self._discard_body()
            self._text(HTTPStatus.FORBIDDEN, "다른 출처에서 온 요청은 받지 않습니다.")
            return
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._index(query)
            if method == "GET" and path in ("/classic", "/classic/"):      # 옛 화면 (새 화면으로 옮기는 동안, 36.9 M31)
                return self._index(query, classic=True)
            if method == "GET" and path.startswith("/app/"):
                return self._web_asset(path[len("/app/"):])
            if method == "GET" and path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if method == "GET" and path.startswith("/logos/") and path.endswith(".svg"):
                return self._logo(path[len("/logos/"):-len(".svg")])
            if path.startswith("/api/"):
                if not self._fetch_site_ok():
                    self._discard_body()
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "다른 사이트에서 온 요청은 받지 않습니다."})
                given = self.headers.get("X-Tenure-Token")
                if method == "GET" and path in ("/api/stream", "/api/session/stream") and not given:
                    given = self._cookie_token()          # EventSource 는 헤더를 못 붙인다 → 쿠키
                if not self._token_ok(given):
                    self._discard_body()
                    return self._json(HTTPStatus.UNAUTHORIZED,
                                      {"error": "토큰이 맞지 않습니다. 터미널에 출력된 새 주소로 다시 접속하세요."})
                if method == "GET" and path == "/api/stream":
                    return self._stream(query)
                if method == "GET" and path == "/api/session/stream":
                    return self._session_stream(query)
                body = self._read_json() if method == "POST" else {}
                return self._json(HTTPStatus.OK, self._api(method, path, query, body))
            self._text(HTTPStatus.NOT_FOUND, "없는 페이지입니다.")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001 - UI 는 죽지 않아야 한다
            print(f"[tenure ui] 오류: {type(e).__name__}: {e}", file=sys.stderr)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"서버 오류: {type(e).__name__}: {e}"})

    STREAM_MAX_SEC = 1800          # 한 연결의 최대 길이. 넘으면 끊고, 화면이 since 로 다시 붙는다
    STREAM_POLL_SEC = 0.5
    STREAM_BEAT_SEC = 15

    def _stream(self, q: dict[str, str]) -> None:
        """실행의 이벤트를 SSE 로 흘려보낸다 (DESIGN.md 36.3). 이 머신의 실행만. 원격은 /api/events 폴링."""
        from tenure.events import read_events
        from tenure.state import RUNNING, RunState

        dash = self.server.dashboard
        wd = dash.project(q.get("project"))
        rd = dash.run_dir(wd, q.get("run"))
        since = _int(q.get("since"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        start = last_beat = last_check = time.monotonic()
        finished = False
        try:
            while time.monotonic() - start < self.STREAM_MAX_SEC:
                evs = read_events(rd, since=since)
                for e in evs:
                    since = int(e["seq"])
                    data = json.dumps(e, ensure_ascii=False, default=str)
                    self.wfile.write(f"id: {since}\nevent: {e.get('kind', 'message')}\ndata: {data}\n\n".encode("utf-8"))
                now = time.monotonic()
                if evs:
                    self.wfile.flush()
                    last_beat = now
                elif now - last_beat >= self.STREAM_BEAT_SEC:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_beat = now
                if not evs and now - last_check >= 3:
                    last_check = now
                    try:
                        finished = RunState.load(rd).status != RUNNING
                    except (OSError, ValueError, TypeError):
                        finished = True
                    if finished:
                        self.wfile.write(b"event: end\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                time.sleep(self.STREAM_POLL_SEC)
        except OSError:
            return                     # 상대가 연결을 닫음

    SESSION_POLL_SEC = 0.05

    def _session_stream(self, q: dict[str, str]) -> None:
        """세션 출력을 SSE 로 (DESIGN.md 36.19). data 는 {data, offset, status}. 끝나면 end."""
        s = self.server._session(q.get("id"))
        offset = _int(q.get("offset"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        start = last_beat = last_state = time.monotonic()
        state = None
        try:
            while time.monotonic() - start < self.STREAM_MAX_SEC:
                snap = s.snapshot(offset)
                now = time.monotonic()
                sent = False
                if snap["data"] or snap["reset"]:
                    offset = snap["offset"]
                    payload = {"data": snap["data"], "offset": offset, "reset": snap["reset"]}
                    self.wfile.write(f"event: out\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                    sent = True
                if now - last_state >= 0.5:
                    last_state = now
                    st = snap["status"]
                    if st != state:
                        state = st
                        self.wfile.write(f"event: status\ndata: {json.dumps(st, ensure_ascii=False)}\n\n".encode("utf-8"))
                        sent = True
                    if not snap["live"] and not snap["data"]:
                        self.wfile.write(b"event: end\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                if sent:
                    self.wfile.flush()
                    last_beat = now
                elif now - last_beat >= self.STREAM_BEAT_SEC:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_beat = now
                time.sleep(self.SESSION_POLL_SEC)
        except OSError:
            return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "요청이 너무 큽니다.")
        if length == 0:
            return {}
        # 거절하더라도 본문은 먼저 읽는다. 안 읽고 응답·종료하면 Windows 에서는 연결이 끊긴다(ConnectionAborted)
        raw = self.rfile.read(length)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError(415, "JSON 요청만 받습니다.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "JSON 형식이 잘못되었습니다.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON 객체가 필요합니다.")
        return data

    # ---------------------------------------------------------------- pages
    def _cookie_name(self) -> str:
        return f"{COOKIE_PREFIX}{self.server.port}"   # 같은 PC 의 UI 여러 개가 섞이지 않게 포트별

    def _cookie_token(self) -> str | None:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:  # noqa: BLE001 - 깨진 쿠키는 없는 것으로
            return None
        morsel = jar.get(self._cookie_name())
        return morsel.value if morsel is not None else None

    def _index(self, query: dict[str, str], classic: bool = False) -> None:
        """?t= 가 맞으면 쿠키를 심고, 다음부터는 쿠키로 연다 (DESIGN.md 28.1). API 는 쿠키로 열지 않는다."""
        via_query = self._token_ok(query.get("t"))
        if not via_query and not self._token_ok(self._cookie_token()):
            # 쿠키는 주소(호스트)마다 따로라 localhost 로 열면 127.0.0.1 에서 심은 쿠키가 없다 → 같은 서버의 127.0.0.1 로
            host = self.headers.get("Host", "")
            if self.server.loopback and host.startswith("localhost:"):
                self._send(HTTPStatus.FOUND, b"", "text/plain; charset=utf-8",
                           {"Location": f"http://127.0.0.1:{self.server.port}{self.path}"})
                return
            self._send(HTTPStatus.FORBIDDEN, _forbidden_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        extra = None
        if via_query:
            extra = {"Set-Cookie": f"{self._cookie_name()}={self.server.token}; HttpOnly; SameSite=Strict; "
                                   f"Path=/; Max-Age={COOKIE_MAX_AGE}"}
        web = None if classic else _web_index()
        html = (web if web is not None else _static_text("index.html")).replace(TOKEN_PLACEHOLDER, self.server.token)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8", extra)

    def _web_asset(self, rel: str) -> None:
        """새 화면(React 빌드)의 자산. ui/web 안의 파일만, 아는 확장자만."""
        if not rel or ".." in rel.split("/") or "\\" in rel or rel.startswith("/"):
            self._text(HTTPStatus.NOT_FOUND, "없는 파일입니다.")
            return
        ctype = WEB_TYPES.get(Path(rel).suffix.lower())
        node = resources.files("tenure.ui").joinpath("web", *rel.split("/"))
        if ctype is None or not node.is_file():
            self._text(HTTPStatus.NOT_FOUND, "없는 파일입니다.")
            return
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"   # 이름에 해시가 붙은 자산
        self._send(HTTPStatus.OK, node.read_bytes(), ctype, {"Cache-Control": cache})

    def _static(self, name: str) -> None:
        if name in STATIC_BINARY:
            data = resources.files("tenure.ui").joinpath("static", name).read_bytes()
            self._send(HTTPStatus.OK, data, STATIC_BINARY[name], {"Cache-Control": "public, max-age=604800"})
            return
        ctype = STATIC_FILES.get(name)
        if ctype is None:
            self._text(HTTPStatus.NOT_FOUND, "없는 파일입니다.")
            return
        self._send(HTTPStatus.OK, _static_text(name).encode("utf-8"), ctype)

    def _logo(self, name: str) -> None:
        """사용자가 넣은 로고 SVG. <img> 로만 쓰므로 안의 스크립트는 돌지 않고, 직접 열어도 CSP 로 막는다."""
        if name not in LOGO_NAMES:
            self._text(HTTPStatus.NOT_FOUND, "로고 파일이 없습니다.")
            return
        path = logo_path(name)
        data = path.read_bytes() if path else resources.files("tenure.ui").joinpath("static", "logos", f"{name}.svg").read_bytes()
        self._send(HTTPStatus.OK, data, "image/svg+xml",
                   {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

    # ---------------------------------------------------------------- api
    # HTTP 경로 → 대시보드 메서드. 쿼리/본문의 machine 이 원격이면 SSH 로 그 머신의 tenure _api 를 부른다.
    ROUTES: dict[tuple[str, str], str] = {
        ("POST", "/api/projects"): "add_project",
        ("DELETE", "/api/projects"): "remove_project",
        ("GET", "/api/runs"): "runs",
        ("GET", "/api/run"): "run",
        ("GET", "/api/entry"): "entry",
        ("GET", "/api/console"): "console",
        ("GET", "/api/design"): "design",
        ("GET", "/api/commits"): "commits",
        ("GET", "/api/diff"): "diff",
        ("GET", "/api/jobs"): "jobs",
        ("POST", "/api/actions/run"): "start_run",
        ("POST", "/api/actions/resume"): "resume",
        ("POST", "/api/actions/report"): "make_report",
        ("POST", "/api/actions/stop"): "stop",
        ("GET", "/api/usage"): "usage",
        ("GET", "/api/activity"): "activity",
        ("GET", "/api/doctor"): "doctor",
        ("GET", "/api/defaults"): "defaults",
        ("GET", "/api/workflows"): "workflows",
        ("POST", "/api/projects/prune"): "prune_projects",
        ("GET", "/api/bench"): "bench_list",
        ("GET", "/api/bench/result"): "bench_result",
        ("GET", "/api/fs/dirs"): "list_dirs",
        ("GET", "/api/events"): "events",
        ("POST", "/api/runs/seen"): "mark_seen",
        ("GET", "/api/inbox"): "inbox",
        ("GET", "/api/works"): "works",
        ("GET", "/api/ws/tree"): "ws_tree",
        ("GET", "/api/ws/file"): "ws_file",
        ("GET", "/api/ws/status"): "ws_status",
        ("GET", "/api/ws/diff"): "ws_diff",
        ("GET", "/api/limits"): "limits",
        ("GET", "/api/merge/check"): "merge_check",
        ("POST", "/api/actions/merge"): "merge",
        ("POST", "/api/actions/discard"): "discard",
        ("POST", "/api/actions/resolve"): "resolve",
        ("GET", "/api/notes"): "notes",
        ("POST", "/api/notes"): "note_add",
        ("DELETE", "/api/notes"): "note_delete",
        ("POST", "/api/actions/notes"): "notes_send",
        ("POST", "/api/actions/pick"): "pick",
        ("POST", "/api/flow/notes"): "flow_notes",
        ("GET", "/api/workflow"): "workflow_get",
        ("POST", "/api/workflow/check"): "workflow_check",
        ("POST", "/api/workflow/save"): "workflow_save",
    }

    def _api(self, method: str, path: str, q: dict[str, str], body: dict[str, Any]) -> Any:
        srv = self.server
        args = {**q, **body}
        machine = str(args.pop("machine", "") or LOCAL_MACHINE)
        special = {
            ("GET", "/api/projects"): lambda: srv.all_projects(),
            ("GET", "/api/server"): lambda: srv.server_info(),
            ("POST", "/api/server/restart"): lambda: srv.restart(),
            ("GET", "/api/settings"): lambda: srv.settings_view(),
            ("POST", "/api/settings"): lambda: srv.save_settings(body),
            ("POST", "/api/machines"): lambda: srv.upsert_machine(body),
            ("DELETE", "/api/machines"): lambda: srv.delete_machine(q.get("id")),
            ("POST", "/api/machines/probe"): lambda: srv.probe_machine(machine),
            ("POST", "/api/machines/deploy"): lambda: srv.deploy_machine(machine),
            ("GET", "/api/setup/actions"): lambda: srv.actions_view(),
            ("POST", "/api/setup/action"): lambda: srv.start_action(machine, body.get("action")),
            ("GET", "/api/term"): lambda: srv.term(q.get("id"), _int(q.get("offset"))),
            ("GET", "/api/terms"): lambda: {"terms": srv.terms_list()},
            ("POST", "/api/term/input"): lambda: srv.term_input(body.get("id"), body.get("text")),
            ("POST", "/api/term/kill"): lambda: srv.term_kill(body.get("id")),
            ("POST", "/api/fs/pick"): lambda: srv.native_pick(body.get("start")),
            # 대화형 세션 (DESIGN.md 36.19): 이 머신에서만
            ("GET", "/api/sessions"): lambda: {"sessions": srv.sessions.list()},
            ("POST", "/api/sessions"): lambda: srv.session_create(machine, body),
            ("GET", "/api/session"): lambda: srv._session(q.get("id")).snapshot(_int(q.get("offset"))),
            ("GET", "/api/session/explain"): lambda: srv._session(q.get("id")).explain(),
            ("POST", "/api/session/input"): lambda: srv.session_action("input", body),
            ("POST", "/api/session/send"): lambda: srv.session_action("send", body),
            ("POST", "/api/session/answer"): lambda: srv.session_action("answer", body),
            ("POST", "/api/session/resize"): lambda: srv.session_action("resize", body),
            ("POST", "/api/session/kill"): lambda: srv.session_action("kill", body),
            ("POST", "/api/session/seen"): lambda: srv.session_action("seen", body),
            ("POST", "/api/session/close"): lambda: srv.session_action("close", body),
            ("GET", "/api/agent-config"): lambda: agent_config(srv.dashboard.project(q.get("project"))),
        }
        handler = special.get((method, path))
        if handler is not None:
            return handler()
        name = self.ROUTES.get((method, path))
        if name is None:
            raise ApiError(404, "없는 API 입니다.")
        return srv.call(machine, name, args)


REMOTE_CACHE_SEC = 8.0


def local_action_argv(action) -> tuple[list[str], str]:
    """이 머신에서 동작을 돌리는 argv 와 터미널 방식 (DESIGN.md 27.5)."""
    if procs.IS_WINDOWS:
        argv = procs.powershell_argv(action.local_command())
        if action.interactive_here:
            return [*argv[:-2], "-NoExit", *argv[-2:]], "console"
        return argv, "pipe"
    return ["sh", "-c", PATH_PREFIX + action.shell], "pty"


# 모델 고르기 목록의 이름·한 줄 설명
CLAUDE_MODEL_INFO = {
    "claude-fable-5-1": {"label": "Fable 5.1", "note": "가장 강력함 · 어려운 설계·대형 작업"},
    "claude-opus-5-5": {"label": "Opus 5.5", "note": "높은 품질 · 복잡한 구현·리뷰"},
    "claude-sonnet-5-5": {"label": "Sonnet 5.5", "note": "속도와 품질의 균형 · 분업 작업자"},
    "claude-haiku-5-5": {"label": "Haiku 5.5", "note": "가장 빠르고 가벼움 · 중복 확인 같은 작은 일"},
}
# Codex 는 ~/.codex/models_cache.json 의 영어 설명을 옮긴다. 모르는 설명은 그대로 둔다
CODEX_NOTE_KO = {
    "Latest workhorse model for coding and everyday work.": "최신 주력 모델 · 코딩과 일반 작업",
    "Frontier intelligence for the most demanding work.": "최상위 성능 · 가장 어려운 작업",
    "Previous generation workhorse model.": "이전 세대 주력 모델",
    "Fast and affordable model for easier tasks.": "빠르고 저렴함 · 쉬운 작업",
    "Older generation workhorse model.": "구세대 주력 모델",
    "Older balanced model for straightforward work.": "구세대 균형형 · 단순한 작업",
    "Older fast and efficient model.": "구세대 · 빠르고 효율적",
    "Latest frontier agentic coding model for broad defensive cybersecurity work.": "최신 에이전트 코딩 모델 · 방어적 보안 작업용",
}


# ---------------------------------------------------------------- 폴더 고르기 (DESIGN.md 28.9)
PICK_TIMEOUT_SEC = 600
_pick_lock = threading.Lock()


def native_picker_available() -> bool:
    """서버가 있는 머신에서 OS 폴더 선택 창을 띄울 수 있는가 (Windows·macOS, 또는 화면이 있는 Linux + tkinter)."""
    import importlib.util

    if os.environ.get("TENURE_NO_NATIVE_PICKER"):
        return False
    if sys.platform not in ("win32", "darwin") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return importlib.util.find_spec("tkinter") is not None


def cli_default_models() -> dict[str, str | None]:
    """각 CLI 가 모델을 지정하지 않을 때 쓰는 모델 (화면의 '기본 모델'이 무엇인지 보이려고, DESIGN.md 30.6).

    Codex: ~/.codex/config.toml 의 model. Claude: ~/.claude/settings.json 의 model (없으면 None = Claude Code 기본값).
    """
    import tomllib

    from tenure.usage import claude_home

    out: dict[str, str | None] = {"claude": None, "codex": None}
    try:
        data = tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8"))
        if isinstance(data.get("model"), str) and data["model"].strip():
            out["codex"] = data["model"].strip()
    except (OSError, ValueError):
        pass
    try:
        data = json.loads((claude_home() / "settings.json").read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("model"), str) and data["model"].strip():
            out["claude"] = data["model"].strip()
    except (OSError, ValueError):
        pass
    return out


def model_suggestions() -> dict[str, Any]:
    """모델 선택 상자에 보여줄 후보. Codex 는 ~/.codex/models_cache.json 에서 읽는다."""
    codex: list[dict[str, Any]] = []
    try:
        cache = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
        for m in cache.get("models", []):
            if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list":
                codex.append({"id": m["slug"], "label": m.get("display_name") or m["slug"], "note": CODEX_NOTE_KO.get(m.get("description") or "", m.get("description") or ""),
                              "efforts": [e.get("effort") for e in m.get("supported_reasoning_levels") or [] if isinstance(e, dict)],
                              "default_effort": m.get("default_reasoning_level")})
    except (OSError, ValueError, AttributeError):
        pass
    cli = cli_default_models()
    return {
        "claude": {"models": [{"id": x, **CLAUDE_MODEL_INFO.get(x, {"label": x})} for x in CLAUDE_MODEL_SUGGESTIONS],
                   "cli_default": cli["claude"],
                   "efforts": EFFORT_SUGGESTIONS["claude"]},
        "codex": {"models": codex, "efforts": EFFORT_SUGGESTIONS["codex"], "cli_default": cli["codex"]},
    }


def _clean_defaults(d: Any) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise ApiError(400, "defaults 는 객체여야 합니다.")
    out: dict[str, Any] = {}
    for k in ("implementer", "reviewer", "synth"):
        if d.get(k):
            out[k] = str(d[k])
    if str(d.get("test_command") or "").strip():
        out["test_command"] = str(d["test_command"]).strip()
    if d.get("block_severity"):
        out["block_severity"] = str(d["block_severity"])
    if isinstance(d.get("require_tests_pass"), bool):
        out["require_tests_pass"] = d["require_tests_pass"]
    if d.get("review_diff") in ("incremental", "full"):
        out["review_diff"] = d["review_diff"]
    if isinstance(d.get("repro_tests"), bool):
        out["repro_tests"] = d["repro_tests"]
    for k in ("debate_rounds", "max_review", "timeout_sec", "stall_rounds"):
        if d.get(k) not in (None, ""):
            try:
                out[k] = int(d[k])
            except (TypeError, ValueError):
                raise ApiError(400, f"{k} 는 숫자여야 합니다.") from None
    for agent in ("claude", "codex"):
        sub = d.get(agent) or {}
        if isinstance(sub, dict):
            clean = {k: str(sub[k]).strip() for k in ("model", "effort") if str(sub.get(k) or "").strip()}
            if clean:
                out[agent] = clean
    return out


def _int(v: str | None) -> int:
    try:
        return int(v or 0)
    except ValueError:
        return 0


def tenure_home_dir() -> Path:
    from tenure.registry import tenure_home

    return tenure_home()


def logo_path(name: str) -> Path | None:
    """사용자가 바꿔 넣은 로고. 없으면(또는 너무 크면) None → 함께 배포한 로고."""
    if name not in LOGO_NAMES:
        return None
    p = tenure_home_dir() / "logos" / f"{name}.svg"
    try:
        if p.is_file() and p.stat().st_size <= LOGO_MAX_BYTES:
            return p
    except OSError:
        pass
    return None


WEB_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
             ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json", ".ico": "image/x-icon"}


def _web_index() -> str | None:
    """새 화면 빌드가 있으면 그 index.html (없으면 옛 화면)."""
    node = resources.files("tenure.ui").joinpath("web", "index.html")
    try:
        return node.read_text(encoding="utf-8") if node.is_file() else None
    except OSError:
        return None


def _static_text(name: str) -> str:
    return resources.files("tenure.ui").joinpath("static", name).read_text(encoding="utf-8")


def _forbidden_page() -> str:
    return ("<!doctype html><meta charset='utf-8'><title>tenure</title>"
            "<link rel='stylesheet' href='/static/style.css'>"
            "<body><main class='main'>"
            "<h1>접속 주소가 맞지 않습니다</h1>"
            "<p><code>tenure ui</code> 를 실행한 터미널에 출력된 주소(<code>?t=</code> 포함)로 한 번 접속하세요. "
            "그다음부터는 이 브라우저가 기억해서 주소만으로 열립니다(<code>tenure ui --new-token</code> 으로 "
            "토큰을 바꾸면 다시 처음 주소가 필요합니다).</p></main></body>")


def make_server(host: str = "127.0.0.1", port: int = 8787, *, dashboard: Dashboard | None = None,
                token: str | None = None, port_tries: int = 20) -> TenureServer:
    dashboard = dashboard or Dashboard()
    token = token or secrets.token_urlsafe(24)
    last_err: OSError | None = None
    for i in range(max(1, port_tries)):
        try:
            return TenureServer((host, port + i if port else 0), dashboard, token)
        except OSError as e:  # 포트 사용 중
            last_err = e
            if not port:
                break
    raise OSError(f"포트를 열 수 없습니다 ({host}:{port}~): {last_err}")


RESTART_TOKEN_ENV = "TENURE_UI_TOKEN"
RESTART_PORT_ENV = "TENURE_UI_EXACT_PORT"


def exec_restart(server: "TenureServer") -> None:
    """같은 포트·토큰으로 자기 자신을 다시 실행한다(바뀐 tenure 코드를 읽도록). 실행 중인 tenure run 은 별도 프로세스라 영향 없음."""
    host, port = server.server_address[0], server.port
    env = dict(os.environ)
    env[RESTART_TOKEN_ENV] = server.token
    env[RESTART_PORT_ENV] = "1"
    argv = self_argv("ui", "--no-browser", "--host", str(host), "--port", str(port))
    sys.stdout.flush()
    print("\nUI 를 다시 시작합니다 (같은 주소)...", flush=True)
    if procs.IS_WINDOWS:
        # Windows 의 exec 는 새 프로세스를 띄우고 지금 것을 끝내는 흉내라, 직접 그렇게 한다.
        # 소켓을 먼저 닫지 않는다: 이 함수는 다른 스레드에서 불리는데, 메인 스레드가 기다리던 소켓을 닫으면
        # WinError 10038 로 메인 스레드가 먼저 죽어 새 프로세스를 띄우지 못한다. 새 프로세스는 포트가 풀릴 때까지
        # 다시 시도하고(serve 의 exact 경로), 이 프로세스가 끝나면 소켓은 운영체제가 닫는다.
        subprocess.Popen(argv, env=env, **procs.detached_kwargs())   # 창 없이 (위와 같은 이유)
        os._exit(0)
    server.server_close()
    os.execve(sys.executable, argv, env)


def _print_flush(line: str) -> None:
    # 파이프로 보낼 때(tenure ui | tee ...) 접속 주소가 버퍼에 갇히지 않게
    print(line, flush=True)


def saved_token(new: bool = False) -> str:
    """$TENURE_HOME/ui-token 의 토큰. 없거나 new 면 새로 만든다 (DESIGN.md 28.1).

    UI 를 다시 켜도 같은 토큰이라 즐겨찾기·쿠키가 계속 맞는다. 파일은 사용자만 읽을 수 있게 둔다.
    """
    from tenure.registry import tenure_home

    path = tenure_home() / TOKEN_FILE
    if not new:
        try:
            tok = path.read_text(encoding="utf-8").strip()
            if len(tok) >= 24:
                return tok
        except OSError:
            pass
    tok = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


def _watch_parent(server: "TenureServer", pid: int, interval: float = 2.0) -> None:
    """데스크톱 셸이 죽어도 엔진이 남지 않게: 부모가 사라지면 서버를 멈춘다."""
    def loop() -> None:
        while True:
            time.sleep(interval)
            if not pid_alive(pid):
                server.shutdown()
                return
    threading.Thread(target=loop, name="parent-watch", daemon=True).start()


def serve(host: str, port: int, *, open_browser: bool = True, extra_projects: list[str] = (),
          printer: Callable[[str], None] = _print_flush, new_token: bool = False,
          ready_line: bool = False, parent_pid: int | None = None) -> None:
    """ready_line: 준비되면 `TENURE_READY {json}` 한 줄 (데스크톱 셸이 읽음). parent_pid: 그 프로세스가 끝나면 같이 끝남 (36.20)."""
    dashboard = Dashboard()
    for p in extra_projects:
        dashboard.registry.add(p)
    # UI 의 '다시 시작'으로 뜬 경우: 같은 토큰·같은 포트 (자식 프로세스에 토큰이 새지 않게 환경에서 뺀다)
    token = os.environ.pop(RESTART_TOKEN_ENV, None) or saved_token(new_token)
    exact = os.environ.pop(RESTART_PORT_ENV, None) == "1"
    server = None
    if exact:
        for _ in range(25):
            try:
                server = make_server(host, port, dashboard=dashboard, token=token, port_tries=1)
                break
            except OSError:
                time.sleep(0.2)
    if server is None:
        server = make_server(host, port, dashboard=dashboard, token=token)
    url = server.url()
    if not server.loopback:
        printer(f"주의: {host} 에 바인딩합니다. 같은 네트워크에서 주소와 토큰을 아는 사람은 실행을 시작할 수 있습니다.")
        printer("   가능하면 기본값(127.0.0.1)과 SSH 터널을 쓰세요: ssh -L 8787:127.0.0.1:8787 <서버>")
    printer(f"tenure 관리자 UI: {url}")
    printer("종료: Ctrl-C  (UI 를 꺼도 UI 에서 시작한 실행은 계속 돕니다)")
    if ready_line:
        printer("TENURE_READY " + json.dumps({"url": url, "port": server.port, "pid": os.getpid()}))
    if parent_pid:
        _watch_parent(server, parent_pid)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        printer("\nUI 종료")
    finally:
        server.server_close()
