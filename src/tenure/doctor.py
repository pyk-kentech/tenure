"""환경 점검과 설치·로그인 동작.

점검: tenure/Python, git, curl, uv, Claude Code(설치·로그인), Codex CLI(설치·로그인).
동작: 공식 설치 스크립트와 로그인 명령만 실행한다(임의 명령은 받지 않는다).
동작은 셸 명령 문자열로 정의해 로컬(sh -c)과 원격(ssh)에서 똑같이 쓴다.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tenure import __version__
from tenure import procs
from tenure.procs import IS_WINDOWS
from tenure.runner import ExecutableNotFound, augmented_path, env_drops, run_process

PATH_PREFIX = 'export PATH="$PATH:$HOME/.local/bin:$HOME/.tenure/bin"; '  # 사용자 PATH 를 우선

INSTALL_UV = ('if command -v curl >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; '
              'elif command -v wget >/dev/null 2>&1; then wget -qO- https://astral.sh/uv/install.sh | sh; '
              'else echo "curl 또는 wget 이 필요합니다."; exit 1; fi')
INSTALL_CLAUDE = ('command -v curl >/dev/null 2>&1 || { echo "curl 이 필요합니다."; exit 1; }; '
                  'curl -fsSL https://claude.ai/install.sh | bash')
INSTALL_CODEX = ('command -v curl >/dev/null 2>&1 || { echo "curl 이 필요합니다."; exit 1; }; '
                 'curl -fsSL https://chatgpt.com/codex/install.sh | sh')
INSTALL_PYTHON = ('command -v uv >/dev/null 2>&1 || { echo "먼저 uv 를 설치하세요."; exit 1; }; '
                  'uv python install 3.12 && uv python find 3.12')
# 시스템 패키지라 관리자 권한이 필요하다. 터미널 창에서 sudo 비밀번호를 입력할 수 있다.
INSTALL_SYSTEM = (
    'PKGS="{pkgs}"; '
    'if [ "$(id -u)" = 0 ]; then S=""; else S="sudo"; fi; '
    'if command -v apt-get >/dev/null 2>&1; then $S apt-get update && $S apt-get install -y $PKGS; '
    'elif command -v dnf >/dev/null 2>&1; then $S dnf install -y $PKGS; '
    'elif command -v yum >/dev/null 2>&1; then $S yum install -y $PKGS; '
    'elif command -v pacman >/dev/null 2>&1; then $S pacman -S --noconfirm $PKGS; '
    'elif command -v zypper >/dev/null 2>&1; then $S zypper install -y $PKGS; '
    'elif command -v apk >/dev/null 2>&1; then $S apk add $PKGS; '
    'elif command -v brew >/dev/null 2>&1; then brew install $PKGS; '
    'else echo "패키지 관리자를 찾지 못했습니다. $PKGS 를 직접 설치하세요."; exit 1; fi'
)


# Windows(PowerShell) 동작 (DESIGN.md 27.5). None 이면 이 OS 에서 지원하지 않음
WIN_INSTALL_UV = "irm https://astral.sh/uv/install.ps1 | iex"
WIN_INSTALL_CLAUDE = "irm https://claude.ai/install.ps1 | iex"
WIN_INSTALL_CODEX = ("if (-not (Get-Command npm -ErrorAction SilentlyContinue)) "
                     "{ Write-Output 'Node.js(npm) 가 필요합니다: https://nodejs.org'; exit 1 }; "
                     "npm install -g @openai/codex")
WIN_INSTALL_GIT = "winget install --id Git.Git -e"
WIN_INSTALL_PYTHON = "uv python install 3.12; uv python find 3.12"


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    shell: str
    interactive: bool = False   # 로그인처럼 사용자 입력이 필요할 수 있음
    note: str = ""
    windows: str | None = "="   # Windows 명령. "=" 는 shell 과 같음, None 은 지원 안 함
    win_interactive: bool | None = None

    def local_command(self) -> str | None:
        if not IS_WINDOWS:
            return self.shell
        return self.shell if self.windows == "=" else self.windows

    def available_locally(self) -> bool:
        return self.local_command() is not None

    @property
    def interactive_here(self) -> bool:
        if IS_WINDOWS and self.win_interactive is not None:
            return self.win_interactive
        return self.interactive


ACTIONS: dict[str, Action] = {a.id: a for a in [
    Action("install_git", "git 설치", INSTALL_SYSTEM.format(pkgs="git"), True,
           "시스템 패키지라 sudo 비밀번호를 물을 수 있습니다.", windows=WIN_INSTALL_GIT),
    Action("install_curl", "curl 설치", INSTALL_SYSTEM.format(pkgs="curl"), True,
           "시스템 패키지라 sudo 비밀번호를 물을 수 있습니다.", windows=None),
    Action("install_uv", "uv 설치", INSTALL_UV, False, "공식 스크립트: astral.sh/uv/install.sh",
           windows=WIN_INSTALL_UV),
    Action("install_python", "Python 3.12 설치", INSTALL_PYTHON, False, "uv 로 사용자 폴더에 설치합니다.",
           windows=WIN_INSTALL_PYTHON),
    Action("install_claude", "Claude Code 설치", INSTALL_CLAUDE, False, "공식 스크립트: claude.ai/install.sh",
           windows=WIN_INSTALL_CLAUDE),
    Action("install_codex", "Codex CLI 설치", INSTALL_CODEX, False, "공식 스크립트: chatgpt.com/codex/install.sh",
           windows=WIN_INSTALL_CODEX),
    Action("login_claude", "Claude 로그인", "claude auth login", True,
           "표시되는 링크를 브라우저에서 열고, 코드를 요구하면 아래 입력칸에 붙여 넣으세요."),
    Action("logout_claude", "Claude 로그아웃", "claude auth logout"),
    Action("login_codex", "Codex 로그인", "codex login --device-auth", True,
           "표시되는 링크를 열고 화면의 코드를 입력하세요."),
    Action("logout_codex", "Codex 로그아웃", "codex logout"),
]}


def action_shell(action_id: str) -> Action:
    a = ACTIONS.get(action_id)
    if a is None:
        raise KeyError(action_id)
    return a


# ====================================================================== checks
@dataclass
class Check:
    id: str
    name: str
    status: str                    # ok | missing | warn | error
    version: str = ""
    path: str = ""
    detail: str = ""
    actions: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _run(cmd: list[str], timeout: int = 20) -> tuple[int | None, str, str]:
    try:
        r = run_process(cmd, cwd=Path.home(), timeout_sec=timeout)
    except ExecutableNotFound:
        return None, "", ""
    return r.exit_code, r.stdout.strip(), r.stderr.strip()


def _which(name: str) -> str:
    """PATH(+ 설치 스크립트·npm 이 쓰는 폴더)에서 찾는다. Windows 는 PATHEXT(.exe·.cmd) 를 따른다 (DESIGN.md 27.4)."""
    return procs.which(name, augmented_path(os.environ.get("PATH"))) or ""


def _first_line(s: str) -> str:
    return s.splitlines()[0].strip() if s else ""


def run_checks() -> list[dict[str, Any]]:
    checks: list[Check] = [Check(
        "tenure", "tenure", "ok", __version__, sys.executable,
        f"Python {platform.python_version()} · {platform.system()} {platform.machine()}")]

    for name, title, act in (("git", "git", "install_git"), ("curl", "curl", "install_curl")):
        path = _which(name)
        if not path:
            checks.append(Check(name, title, "missing", detail="설치되어 있지 않습니다.", actions=[act]))
        else:
            code, out, _ = _run([path, "--version"])
            checks.append(Check(name, title, "ok", _first_line(out), path))

    path = _which("uv")
    if path:
        code, out, _ = _run([path, "--version"])
        checks.append(Check("uv", "uv", "ok", _first_line(out), path, "tenure 설치·Python 설치에 씁니다."))
    else:
        checks.append(Check("uv", "uv", "missing", detail="없어도 되지만, 다른 머신에 Python 을 설치할 때 필요합니다.",
                            actions=["install_uv"]))

    checks.append(_claude_check())
    checks.append(_codex_check())
    sandbox = _sandbox_check()
    if sandbox is not None:
        checks.append(sandbox)
    keys = _api_key_check()
    if keys is not None:
        checks.append(keys)
    return [asdict(c) for c in checks]


# 있으면 CLI 가 구독 대신 API 로 과금할 수 있는 변수 (DESIGN.md 18.2)
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


def _sandbox_check() -> Check | None:
    """claude_sandbox 가 켜져 있으면 Linux 에서 bubblewrap·socat 이 있는지 본다 (DESIGN.md 24.3)."""
    from tenure.config import Config, ConfigError, load_config

    try:
        cfg = load_config(None)
    except ConfigError:
        cfg = Config()
    if not cfg.claude_sandbox:
        return None
    if not sys.platform.startswith("linux"):
        return Check("sandbox", "Claude 샌드박스", "ok", detail="켜짐 (macOS 는 Seatbelt 를 씁니다)")
    missing = [b for b in ("bwrap", "socat") if not _which(b)]
    if missing:
        return Check("sandbox", "Claude 샌드박스", "warn",
                     detail=f"켜져 있지만 {', '.join(missing)} 가 없습니다. 설치하지 않으면 Claude 구현자의 명령이 "
                            "막히거나 실패합니다 (예: sudo apt install bubblewrap socat).", extra={"missing": missing})
    return Check("sandbox", "Claude 샌드박스", "ok", detail="켜짐 (bubblewrap·socat 있음)")


def _api_key_check() -> Check | None:
    present = [k for k in API_KEY_VARS if os.environ.get(k)]
    if not present:
        return None
    from tenure.config import Config, ConfigError, load_config

    try:
        cfg = load_config(None)
    except ConfigError:
        cfg = Config()
    dropped = set(env_drops(cfg.env_strip, cfg.env_keep, {k: "" for k in present}))
    passed = [k for k in present if k not in dropped]
    if passed:
        detail = (f"{', '.join(passed)} 가 에이전트에 그대로 넘어갑니다. 구독 대신 API 로 과금될 수 있습니다 "
                  "(설정 env_strip 확인).")
    else:
        detail = (f"{', '.join(present)} 가 셸에 있습니다. tenure 는 빼고 넘기지만, "
                  "직접 `claude -p`·`codex exec` 를 돌리면 API 로 과금될 수 있습니다.")
    return Check("api_keys", "API 키 변수", "warn", detail=detail, extra={"present": present, "passed": passed})


def _claude_check() -> Check:
    path = _which("claude")
    if not path:
        return Check("claude", "Claude Code", "missing", detail="설치되어 있지 않습니다.", actions=["install_claude"])
    _, out, _ = _run([path, "--version"])
    c = Check("claude", "Claude Code", "ok", _first_line(out), path)
    code, out, err = _run([path, "auth", "status", "--json"], timeout=30)
    info: dict[str, Any] = {}
    try:
        info = json.loads(out) if out else {}
    except ValueError:
        info = {}
    if code == 0 and info.get("loggedIn"):
        c.extra = {"logged_in": True, "auth_method": info.get("authMethod"),
                   "subscription": info.get("subscriptionType"), "account": info.get("email") or ""}
        c.detail = f"로그인됨 · {info.get('authMethod') or ''} · 구독 {info.get('subscriptionType') or '?'}"
        c.actions = ["logout_claude", "install_claude"]
    else:
        c.status = "warn"
        c.extra = {"logged_in": False}
        c.detail = "로그인이 필요합니다." + (f" ({_first_line(err)})" if err else "")
        c.actions = ["login_claude", "install_claude"]
    return c


def _codex_check() -> Check:
    path = _which("codex")
    if not path:
        return Check("codex", "Codex CLI", "missing", detail="설치되어 있지 않습니다.", actions=["install_codex"])
    _, out, _ = _run([path, "--version"])
    c = Check("codex", "Codex CLI", "ok", _first_line(out), path)
    code, out, err = _run([path, "login", "status"], timeout=30)
    text = (out + "\n" + err).strip()
    logged = code == 0 and not re.search(r"not logged in", text, re.IGNORECASE)
    if logged:
        c.extra = {"logged_in": True}
        c.detail = _first_line(text) or "로그인됨"
        c.actions = ["logout_codex", "install_codex"]
    else:
        c.status = "warn"
        c.extra = {"logged_in": False}
        c.detail = "로그인이 필요합니다." + (f" ({_first_line(text)})" if text else "")
        c.actions = ["login_codex", "install_codex"]
    return c


def login_check(agent: str) -> Check:
    """한 CLI 의 설치·로그인 상태 (실행 전 확인, DESIGN.md 36.37)."""
    return _claude_check() if agent == "claude" else _codex_check()


def ready(checks: list[dict[str, Any]]) -> bool:
    """tenure 를 실제 CLI 로 돌릴 수 있는가 (git + 두 CLI 설치·로그인)."""
    by = {c["id"]: c for c in checks}
    return all(by.get(k, {}).get("status") == "ok" for k in ("git", "claude", "codex"))


def format_checks(checks: list[dict[str, Any]]) -> str:
    mark = {"ok": "✓", "warn": "!", "missing": "✗", "error": "✗"}
    lines = []
    for c in checks:
        lines.append(f"{mark.get(c['status'], '?')} {c['name']:<12} {c['version'] or '-':<34} {c['detail']}")
        for a in c["actions"]:
            if c["status"] != "ok" or a.startswith("login"):
                act = ACTIONS[a]
                cmd = act.local_command()        # 이 OS 의 명령 (DESIGN.md 27.5)
                if cmd is None:
                    lines.append(f"    → {act.title}: 이 OS 에서는 직접 설치하세요.")
                else:
                    lines.append(f"    → {act.title}: {cmd if len(cmd) < 90 else cmd[:87] + '...'}")
    return "\n".join(lines)
