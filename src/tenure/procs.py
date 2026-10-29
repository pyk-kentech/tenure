"""OS 별 프로세스 제어 (DESIGN.md 27.2·27.4).

나머지 코드는 OS 를 직접 따지지 않고 이 모듈의 함수만 쓴다.
- 자식 실행 옵션, 프로세스 트리 종료, 생존 확인, 분리 실행
- 실행 파일 찾기(PATHEXT·배치 파일·파이썬 스크립트), 셸 명령, 파이썬 명령 이름
- 파일 바꿔 쓰기(Windows 는 다른 프로세스가 읽는 중이면 거부)

Windows 에서는 os.kill 을 쓰지 않는다: os.kill(pid, 0) 이 그 프로세스를 강제 종료한다.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Sequence

IS_WINDOWS = os.name == "nt"
KILL_GRACE_SEC = 3.0
# 자동 감지한 파이썬 테스트 명령·채점에서 쓸 이름. Windows 의 python3 은 Microsoft Store 바로가기일 수 있다
PYTHON_CMD = "python" if IS_WINDOWS else "python3"
BATCH_SUFFIXES = (".cmd", ".bat")
# Windows 명령줄은 32767 글자까지. 넉넉히 남기고 그보다 긴 프롬프트는 stdin 으로
WINDOWS_ARG_MAX_BYTES = 24_000
# Windows: 다른 프로세스가 대상 파일을 읽는 동안 os.replace 가 거부되면 이 시간까지 다시 시도
REPLACE_RETRY_SEC = 5.0

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010


# ====================================================================== 실행 옵션
def child_kwargs() -> dict:
    """에이전트·git·테스트처럼 tenure 가 기다리는 자식. 트리째 정리할 수 있게 새 그룹으로."""
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def detached_kwargs() -> dict:
    """UI 가 띄우는 tenure run/resume. UI 를 꺼도 계속 돈다.

    Windows: 콘솔 없이(DETACHED_PROCESS) 띄우면, 그 프로세스가 띄우는 콘솔 프로그램(venv 의 python.exe 가 다시
    띄우는 진짜 Python, 플래그 없이 불리는 도구 등)마다 새 콘솔 창이 떴다 꺼진다(2026-10-29 사용자 지적). 숨은 새
    콘솔(CREATE_NO_WINDOW)로 띄우면 자식들이 그 콘솔을 물려받아 창이 뜨지 않고, 부모와 콘솔을 나누지 않으니 UI 를
    꺼도 계속 돈다.
    """
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def console_kwargs() -> dict:
    """Windows: 새 콘솔 창 (로그인처럼 터미널 입력이 필요한 동작, DESIGN.md 27.5)."""
    return {"creationflags": _CREATE_NEW_CONSOLE} if IS_WINDOWS else {"start_new_session": True}


# ====================================================================== 종료·생존
def kill_tree(proc: subprocess.Popen, grace: float = KILL_GRACE_SEC) -> None:
    """proc 과 그 자손을 모두 종료한다. POSIX: 그룹에 TERM → 유예 → KILL. Windows: taskkill /T /F."""
    if IS_WINDOWS:
        _taskkill(proc.pid)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # 리더가 먼저 끝났어도 그룹에 남은 손자 프로세스가 있을 수 있다
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_pid_tree(pid: int) -> None:
    """pid 로만 아는 프로세스(잠금 파일의 tenure)를 트리째 강제 종료한다 (UI 의 '강제 종료')."""
    if pid <= 0:
        return
    if IS_WINDOWS:
        _taskkill(pid)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return


def terminate_pid(pid: int) -> bool:
    """정상 중지 신호. POSIX 는 SIGTERM(tenure 가 KeyboardInterrupt 로 처리). Windows 에는 없다 → False."""
    if IS_WINDOWS or pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # 좀비는 살아 있는 것으로 치지 않는다 (Linux)
    stat = Path(f"/proc/{pid}/stat")
    try:
        state = stat.read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


def _win_pid_alive(pid: int) -> bool:  # pragma: no cover - Windows 전용
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5      # ERROR_ACCESS_DENIED: 있지만 볼 권한이 없음
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def looks_like_tenure(pid: int) -> bool:
    """잠금 파일의 pid 가 tenure 인가. Linux 는 /proc 의 명령줄로 확인, 그 밖에는 잠금 파일을 믿는다."""
    if not pid_alive(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return True
    try:
        data = cmdline.read_bytes()
        return b"tenure" in data or b"duet" in data   # 옛 이름으로 띄운 것도
    except OSError:
        return False


# ====================================================================== 파일
def replace_file(src: str | Path, dst: str | Path) -> None:
    """임시 파일로 dst 를 바꿔 쓴다(os.replace).

    Windows 는 다른 프로세스가 dst 를 열어 둔 순간 바꾸기를 거부한다(PermissionError). UI·시험이 잠금·run.json 을
    자주 읽으므로 그 순간에 걸리면 실행이 죽는다. 읽기는 금방 끝나니 잠깐씩 다시 시도한다.
    """
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    deadline = time.monotonic() + REPLACE_RETRY_SEC
    wait = 0.005
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(wait)
            wait = min(wait * 2, 0.1)


# ====================================================================== 실행 파일
def extra_bin_dirs() -> list[str]:
    """공식 설치 스크립트·npm 이 실행 파일을 두는 곳. 셸 설정을 안 읽는 환경(UI, ssh)에서도 찾게 한다."""
    dirs = ["~/.local/bin", "~/.tenure/bin"]
    if IS_WINDOWS:
        if os.environ.get("APPDATA"):
            dirs.append(os.path.join(os.environ["APPDATA"], "npm"))
        if os.environ.get("LOCALAPPDATA"):
            dirs.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "OpenAI", "Codex", "bin"))
    return [os.path.expanduser(d) for d in dirs]


def which(name: str, path: str | None = None) -> str | None:
    return shutil.which(name, path=path)


def is_batch(executable: str, path: str | None = None) -> bool:
    """Windows 의 .cmd/.bat (npm 이 만드는 감싸개). 인자를 cmd.exe 가 다시 해석하므로 프롬프트는 stdin 으로."""
    if not IS_WINDOWS:
        return False
    resolved = _resolve(executable, path)
    return bool(resolved) and resolved.lower().endswith(BATCH_SUFFIXES)


def resolve_argv(cmd: Sequence[str] | str, path: str | None = None) -> list[str] | str:
    """Windows 에서 실행할 수 있는 형태로. POSIX 는 그대로(Popen 이 PATH 로 찾고 shebang 을 따른다).

    - PATH·PATHEXT 로 찾은 전체 경로를 쓴다(.exe/.cmd/.bat).
    - 확장자 없는 파이썬 스크립트(#!...python)는 지금 인터프리터로 실행한다.
    """
    if isinstance(cmd, str):          # Windows 셸 명령줄 (shell_argv)
        return cmd
    cmd = list(cmd)
    if not IS_WINDOWS or not cmd:
        return cmd
    resolved = _resolve(cmd[0], path)
    if resolved is None:
        script = Path(cmd[0])
        if script.is_file() and _is_python_script(script):
            return [sys.executable, str(script), *cmd[1:]]
        return cmd
    if _is_python_script(Path(resolved)):
        return [sys.executable, resolved, *cmd[1:]]
    return [resolved, *cmd[1:]]


def _resolve(executable: str, path: str | None) -> str | None:
    return _resolve_cached(executable, path or os.environ.get("PATH", ""))


@lru_cache(maxsize=256)
def _resolve_cached(executable: str, path: str) -> str | None:
    found = shutil.which(executable, path=path)
    if found:
        return found
    p = Path(executable)
    if p.is_file():
        return str(p)
    return None


def _is_python_script(p: Path) -> bool:
    if p.suffix.lower() in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        return False
    if p.suffix.lower() == ".py":
        return True
    try:
        with p.open("rb") as f:
            head = f.readline(200)
    except OSError:
        return False
    return head.startswith(b"#!") and b"python" in head


def shell_argv(command: str) -> list[str] | str:
    """셸 명령 한 줄을 실행하는 argv. POSIX sh -c, Windows %COMSPEC% /d /s /c.

    Windows 는 명령줄 문자열을 그대로 돌려준다: 목록으로 주면 subprocess 가 안쪽 따옴표를 \\" 로 바꾸는데
    cmd.exe 는 그 규칙을 모른다. subprocess 의 shell=True 와 같은 형태(바깥 따옴표를 /s 가 벗긴다).
    """
    if IS_WINDOWS:
        return f'{os.environ.get("COMSPEC") or "cmd.exe"} /d /s /c "{command}"'
    return ["sh", "-c", command]


CMD_BUILTINS = {"echo", "set", "cd", "call", "if", "for", "exit", "type", "dir", "copy", "del", "move", "ren",
                "mkdir", "md", "rmdir", "rd", "start", "pushd", "popd", "ver", "vol", "title", "cls", "rem", "setlocal",
                "endlocal", "goto", "shift", "pause", "assoc", "ftype", "path", "prompt", "verify", "color", "date", "time"}


def shell_command_missing(command: str, path: str | None = None) -> bool:
    """Windows: 셸 명령의 첫 단어를 찾을 수 없는가. cmd.exe 는 못 찾아도 종료 코드 1 이라 테스트 실패와 구별이 안 된다."""
    if not IS_WINDOWS:
        return False
    s = command.strip()
    if not s:
        return False
    first = s[1:].split('"', 1)[0] if s.startswith('"') else s.split()[0]
    if first.lower() in CMD_BUILTINS or first.startswith(("(", "@")):
        return False
    return _resolve(first, path) is None


def powershell_argv(command: str) -> list[str]:
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
