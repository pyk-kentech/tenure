# duet — 설계 토론 + 교차 리뷰 에이전트 팀

> 구독 중인 Claude(Claude Code CLI)와 ChatGPT(Codex CLI)를 API 키 없이 헤드리스로 호출해,
> 두 모델이 **설계를 토론**하고 **서로의 코드를 교차 리뷰**하며 프로젝트를 완성하게 만드는 로컬 CLI 도구.

---

## 1. 목표와 범위

### 목표
- 한 줄(또는 파일) 과제를 받아 다음을 자동 진행한다.
  1. **설계 토론**: 두 모델이 각자 설계안 → 상호 비판·개정(N라운드) → 결정권자가 `DESIGN.md` 종합
  2. **구현 + 교차 리뷰**: 구현 담당이 코드 작성 → 리뷰 담당이 리뷰 → 승인될 때까지 수정 반복(최대 M회)
- 모든 발언·판정·커밋을 기록해서, 나중에 "둘이 어떻게 논쟁했는지" 읽을 수 있게 한다.
- 중간에 멈춰도 **이어서 재개**할 수 있다.
- 실제 CLI 없이도 전체 흐름을 테스트할 수 있다(mock).

### 비목표 (하지 않을 것)
- 외부에 공개하는 웹 서비스, 데이터베이스 (관리자 UI 는 본인 PC 의 127.0.0.1 전용 — 13장)
- 유료 API 직접 호출 (Anthropic/OpenAI SDK 사용 안 함)
- chatgpt.com / claude.ai 웹 화면 자동화 (약관 위험 + 취약)
- 병렬 실행 (v2 후보로만 남김). 역할은 워크플로 템플릿(19장)으로 여럿 둘 수 있지만 호출은 한 번에 하나씩 순차로 한다.
- Claude Code·Codex 외의 CLI (역할은 둘 중 하나의 CLI 에 모델·강도·지시를 붙인 것)

---

## 2. 사용 시나리오

```bash
# 전체 실행
duet run "마크다운 메모를 태그로 검색하는 CLI" --workdir ./memo-cli

# 긴 과제는 파일로
duet run @task.md --workdir ./memo-cli

# 설계 토론만 보고 싶을 때
duet run "..." --workdir ./x --design-only

# 사람이 DESIGN.md를 손본 뒤 구현부터
duet run "..." --workdir ./x --skip-design

# 역할 바꾸기
duet run "..." --workdir ./x --implementer codex --reviewer claude

# 중단된 실행 이어하기 (마지막 run 기준, --run <id> 로 특정 run)
duet resume --workdir ./x

# 리뷰 상한을 다 써서 미승인으로 끝난 실행을, 상한을 늘려 마지막 리뷰 반영부터 이어가기
duet resume --workdir ./x --max-review 3

# 지난 실행 기록 보기
duet log --workdir ./x            # 최근 run 요약
duet log --workdir ./x --full     # 전체 발언을 순서대로 출력
duet log --workdir ./x --all      # 모든 run 한 줄씩

# CLI 없이 흐름 테스트
duet run "..." --workdir ./x --mock

# 관리자 UI (브라우저로 보기 + 실행 제어)
duet ui
```

실행 중 터미널에는 단계별 진행 상황(누가 무엇을 하는 중인지, 소요 시간, 판정)이 한 줄씩 찍힌다.

종료 코드: 0 승인 완료/설계만 완료, 1 미승인 종료, 2 사용 오류, 3 중단(한도·반복 실패 → `resume`), 130 사용자 중단(Ctrl-C 또는 SIGTERM → `resume`).

---

## 3. 아키텍처

```
          ┌────────────── duet CLI (cli.py) ──────────────┐
          │  run / resume / log                            │
          └───────────────┬───────────────────────────────┘
                          ▼
                ┌───────────────────┐       ┌──────────────┐
                │   Orchestrator    │──────▶│  RunState    │ (run.json, 재개용)
                └──┬──────────┬─────┘       └──────────────┘
                   │          │
        ┌──────────▼──┐  ┌────▼─────────┐   ┌──────────────┐
        │ DebatePhase │  │ BuildPhase   │──▶│  Workspace   │ (git 래퍼)
        └──────┬──────┘  └────┬─────────┘   └──────────────┘
               │              │
               ▼              ▼
        ┌─────────────────────────────┐     ┌──────────────┐
        │  AgentAdapter (프로토콜)      │────▶│  Transcript  │ (발언 기록)
        │  ├ ClaudeCodeAdapter        │     └──────────────┘
        │  ├ CodexAdapter             │
        │  └ MockAdapter              │     ┌──────────────┐
        └─────────────────────────────┘     │  Prompts     │ (템플릿 파일)
                                            └──────────────┘
```

### 핵심 원칙
- **에이전트 어댑터는 얇게**: 명령어 조립 → 실행 → 결과 파싱만. 판단 로직은 Phase에.
- **모든 외부 실행은 `runner.run_process()` 한 곳을 지난다**: 타임아웃, 로그, 테스트용 가짜 실행 주입이 여기서 해결됨.
- **상태는 단계가 끝날 때마다 디스크에 저장**: 어느 지점에서 죽어도 resume 가능.
- **리뷰어는 코드를 못 바꾼다**: CLI 샌드박스 + git으로 이중 확인.

---

## 4. 디렉터리 구조

```
duet/
├── CLAUDE.md
├── DESIGN.md
├── README.md
├── pyproject.toml              # 엔트리포인트 `duet`, 의존성 없음 (dev: pytest)
├── prototype/
│   └── agent_team.py           # 참고용 단일 파일 프로토타입 (수정하지 말 것)
├── src/duet/
│   ├── __init__.py
│   ├── cli.py                  # argparse, 서브커맨드
│   ├── config.py               # 기본값 + duet.toml 로드
│   ├── orchestrator.py         # 단계 진행, resume
│   ├── state.py                # RunState 직렬화
│   ├── transcript.py           # 발언 기록
│   ├── workspace.py            # git 조작
│   ├── runner.py               # subprocess 실행 공통부
│   ├── verdict.py              # 리뷰 판정 파싱
│   ├── prompts.py              # 템플릿 로드/렌더링
│   ├── phases/
│   │   ├── debate.py
│   │   └── build.py
│   ├── agents/
│   │   ├── base.py             # AgentAdapter 프로토콜, Request/Result
│   │   ├── claude_code.py
│   │   ├── codex.py
│   │   └── mock.py
│   └── templates/              # 프롬프트 .md 파일들
│       ├── propose.md
│       ├── critique.md
│       ├── synthesize.md
│       ├── implement.md
│       ├── fix.md
│       └── review.md
└── tests/
    ├── fake_bin/               # 가짜 claude, codex 실행 파일 (셸/파이썬 스크립트)
    ├── test_verdict.py
    ├── test_agents.py          # 명령어 조립 + 출력 파싱
    ├── test_workspace.py
    ├── test_debate.py
    ├── test_build.py
    └── test_e2e_mock.py
```

---

## 5. 인터페이스

### 5.1 에이전트

```python
class Mode(Enum):
    READ = "read"     # 파일 읽기만 허용 (설계, 리뷰)
    WRITE = "write"   # 파일 수정 + 셸 실행 허용 (구현, 수정)

@dataclass
class AgentRequest:
    prompt: str
    cwd: Path
    mode: Mode
    timeout_sec: int
    step: str = ""         # "propose" | "critique" | ... 기록용. MockAdapter가 응답을 고르는 기준

@dataclass
class AgentResult:
    text: str              # 최종 응답 본문
    raw_stdout: str
    raw_stderr: str
    exit_code: int
    duration_sec: float
    rate_limited: bool     # 구독 한도 걸림 감지 여부
    timed_out: bool        # 타임아웃으로 강제 종료됨 (exit_code=-1)
    # ok 프로퍼티: exit_code==0 and not timed_out and not rate_limited

class AgentAdapter(Protocol):
    name: str              # "claude" | "codex" | "mock-claude" ...
    def check(self) -> None: ...                   # 설치/로그인 확인, 실패 시 친절한 에러
    def ask(self, req: AgentRequest) -> AgentResult: ...
```

### 5.2 CLI별 호출 방식 (✅ 2026-10-15, Claude Code 2.1.300 / codex-cli 0.162.1 의 `--help`로 검증 완료)

| 항목 | Claude Code | Codex CLI |
|---|---|---|
| 헤드리스 실행 | `claude -p [옵션] -- <prompt>` | `codex exec [옵션] <prompt>` |
| 결과 받기 | `--output-format json` → `.result` | `--output-last-message <file>` |
| 작업 디렉터리 | `cwd`로 지정 | `-C <dir>` (+ `--skip-git-repo-check`) |
| READ 모드 | `--allowedTools Read,Glob,Grep` | `--sandbox read-only` |
| WRITE 모드 | `--permission-mode acceptEdits --allowedTools Read,Edit,Write,Glob,Grep,Bash` | `--sandbox workspace-write` |

- 플래그는 어댑터 안에 하드코딩하지 말고 `config.py`의 기본값으로 두어, 버전 차이가 나면 `duet.toml`로 덮어쓸 수 있게 한다.
- 프롬프트가 매우 길어질 수 있으므로(설계안 2개 + 비판), `prompt_arg_max_bytes`(기본 100,000 바이트, UTF-8 기준)를 넘으면 stdin으로 폴백한다. Linux 의 인자 하나 한도는 128KiB 이고 한글은 글자당 3바이트라 글자 수로 재면 안 된다. 두 CLI 모두 `--help`에 명시된 방식이다:
  - Claude Code: `-p`에서 위치 인자 프롬프트를 생략하면 stdin을 프롬프트로 읽는다.
  - Codex: 프롬프트 자리에 `-`를 주면 stdin에서 읽는다.
- **Claude 는 프롬프트 앞에 `--` 가 필수**: `--allowedTools <tools...>` 가 공백 구분 가변 인자라서 뒤따르는 프롬프트를 도구 이름으로 삼킨다(첫 실제 실행에서 "Input must be provided..." 로 발견). `--` 를 넣으면 파싱을 통과함을 잘못된 API 키 + `--bare` 로 확인(인증 단계에서 실패하므로 사용량 소모 없음). JSON 의 `is_error: true` 는 exit 0 이어도 실패로 본다.
- `--help` 검증에서 확인한 추가 사실: Claude의 `--permission-mode`는 `acceptEdits|auto|bypassPermissions|manual|dontAsk|plan`, Codex의 `--sandbox`는 `read-only|workspace-write|danger-full-access`. 표의 값은 모두 유효하다.
- 명령어의 고정 부분(`-p --output-format json`, `exec --skip-git-repo-check`)도 `base_args`로 설정에 노출해 버전 차이에 대응한다.
- **한도 감지**: rate limit, usage limit 류 문구를 정규식으로 감지 → `rate_limited=True`. Orchestrator는 상태 저장 후 "한도 도달, 나중에 `duet resume`" 안내하며 종료(자동 대기는 하지 않음).
  - 호출이 **실패**했을 때만 stderr/stdout 끝부분(2000자)과 본문을 검사한다.
  - 호출이 성공했으면 본문이 **400자 이하**일 때만 검사한다(한도 안내문 자체가 응답인 경우). 정상 리뷰가 "rate limit"이나 "429"를 언급했다고 멈추던 오탐을 막는다.

### 5.3 판정 형식

리뷰 프롬프트는 응답 마지막에 아래 블록을 요구한다.

```
<verdict>
status: APPROVED | CHANGES_REQUESTED
must_fix: 3
</verdict>
```

- 파싱 실패 시 레거시 형식 `VERDICT: APPROVED` 한 줄도 허용.
- 모델이 붙이는 마크다운 장식(`**status:** APPROVED`, `` `APPROVED` ``, `- status: ...`)은 무시하고 읽는다.
- 둘 다 없으면 `CHANGES_REQUESTED`로 간주하되, Transcript에 `parse_failed` 표시.
- `status: APPROVED`인데 `must_fix > 0`이면 모순 → `CHANGES_REQUESTED`로 처리.

### 5.4 Orchestrator 흐름

```
check() 두 어댑터
DebatePhase (skip-design 아니면)
  propose(A), propose(B)
  for r in 1..debate_rounds: critique(A←B), critique(B←A)  # 각자 개정안 반환
  synthesize(synth) → workdir/DESIGN.md
  (design-only면 종료)
BuildPhase
  workspace.ensure_repo(); base = commit("duet: design")
  loop i in 1..max_review:
    implementer.ask(implement 또는 fix)          # WRITE
    commit("duet: impl round i")
    (리뷰 전 커밋 안 된 변경이 있으면 보존 커밋)
    diff = 변경 파일 요약(stat) + workspace.diff(base, HEAD, exclude=[DESIGN.md, 잠금 파일들])
    reviewer.ask(review)                          # READ
    if workspace.dirty(): 경고 + 되돌림
    verdict → APPROVED면 종료
  미승인 종료 시 마지막 리뷰 경로 안내
```

- 각 스텝 직후 `RunState`를 갱신 저장. resume은 마지막 완료 스텝 다음부터 이어간다.
- `done_unapproved`로 끝난 실행은 `resume --max-review N`(N > 기존 상한)으로만 이어갈 수 있다. 이때 다음 라운드는 마지막 리뷰를 반영하는 `fix`로 시작한다. 상한을 늘리지 않으면 안내 메시지와 함께 거부한다.
- 같은 에이전트가 두 번 연속 실패(비정상 종료)하면 1회 재시도 후 상태 저장하고 종료.

---

## 6. 기록과 상태

```
<workdir>/.duet/
└── runs/
    └── 20261014-174138/
        ├── run.json            # RunState (아래)
        ├── 01_propose_claude.md
        ├── 02_propose_codex.md
        ├── 03_r1_critique_claude.md
        ├── ...
        └── 09_review2_codex.md
```

- `.duet/`는 자동으로 `.gitignore`에 추가. `.gitignore` 가 없어서 duet 가 새로 만들 때는 빌드·테스트 부산물(`__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage`, `htmlcov/`, `*.egg-info/`, `.venv/`, `node_modules/`, `.DS_Store`)도 넣는다. 에이전트가 직접 돌린 테스트의 캐시가 구현 커밋에 섞이는 것을 막는다(실제 실행에서 발견). 이미 있는 `.gitignore` 에는 `.duet/` 만 덧붙이고 다른 규칙은 건드리지 않는다.
- 각 기록 파일 상단에 메타데이터 주석(에이전트, 모드, 소요 시간, exit code)을 넣는다.
- 실패한 호출도 `NN_label_failed.md` 로 남긴다(stderr 포함).
- `done` 은 완료한 스텝 키 목록. resume 은 이 목록에 없는 스텝부터 실행하며, 필요한 텍스트는 기록 파일에서 다시 읽는다.
- `done_design` 은 `--design-only` 로 설계만 끝낸 상태(끝난 실행이므로 resume 불가).
- `stopped_user` 는 Ctrl-C 또는 SIGTERM(관리자 UI 의 중지)으로 멈춘 상태. 진행 중이던 호출은 버리고 resume 때 그 스텝부터 다시 한다.
- `console.log`: 터미널에 찍히는 진행 로그를 그대로 run 디렉터리에도 남긴다(UI 가 실시간으로 읽는다).
- `DESIGN.prev.md`: 설계 토론이 기존 `DESIGN.md` 를 덮어쓸 때, 내용이 다르면 원본을 여기 백업한다.
- `<workdir>/.duet/lock.json`: 실행 중인 duet 의 pid 와 run_id. 같은 작업 폴더에서 run/resume 이 동시에 돌지 못하게 막고, 죽은 프로세스가 남긴 잠금은 무시한다.

```json
{
  "run_id": "20261014-174138",
  "task": "...",
  "config": { "implementer": "claude", "reviewer": "codex", "synth": "codex",
              "debate_rounds": 1, "max_review": 3 },
  "phase": "build",
  "step": "review",
  "round": 2,
  "base_commit": "5da7bc7",
  "proposals": { "claude": "01_propose_claude.md", "codex": "02_propose_codex.md" },
  "last_review": "07_review1_codex.md",
  "status": "running | done_approved | done_unapproved | done_design | stopped_rate_limit | stopped_user | failed",
  "done": ["propose:claude", "propose:codex", "critique:1:claude", "..."],
  "calls": [ { "agent": "claude", "step": "propose", "sec": 41.2, "exit": 0 } ]
}
```

---

## 7. 프롬프트 템플릿

- `src/duet/templates/*.md`에 둔다. `{task}`, `{own}`, `{other}`, `{other_name}`, `{diff}`, `{review}` 등 `str.format` 플레이스홀더 사용.
- 사용자가 `<workdir>/.duet/templates/`에 같은 이름의 파일을 두면 그걸 우선 사용(커스터마이즈).
- 초기 문구는 `prototype/agent_team.py`의 `P_*` 상수를 기반으로 하되, 판정 블록을 5.3 형식으로 바꾼다.
- 비판 프롬프트의 개정안 추출은 `## 개정안` 헤더 기준. 없으면 응답 전체를 개정안으로 사용.
- 개정안·비판 요약은 **성공한** 기록(메타 `ok: true`) 중 같은 (에이전트, 라운드)의 마지막 것을 쓴다. 한도·타임아웃으로 실패한 기록은 exit 0 이어도 쓰지 않는다.

---

## 8. 설정

우선순위: CLI 인자 > `<workdir>/duet.toml` > 기본값

```toml
implementer = "claude"
reviewer = "codex"
synth = "codex"           # 생략 시 reviewer
debate_rounds = 1
max_review = 3
timeout_sec = 1200
max_diff_chars = 60000

prompt_arg_max_bytes = 100000   # UTF-8 바이트. 넘으면 stdin 폴백 (최대 120000)
diff_excludes = ["package-lock.json", "yarn.lock", "uv.lock", "poetry.lock", "Cargo.lock", "go.sum", "..."]  # 리뷰 diff 에서 뺄 파일

[claude]
bin = "claude"
base_args = ["-p", "--output-format", "json"]
read_args = ["--allowedTools", "Read,Glob,Grep"]
write_args = ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash"]

[codex]
bin = "codex"
base_args = ["exec", "--skip-git-repo-check"]
read_args = ["--sandbox", "read-only"]
write_args = ["--sandbox", "workspace-write"]
effort = "medium"                                         # 18장
step_effort = { synthesize = "high", review = "high" }

# 에이전트·테스트 자식 프로세스에서 뺄 환경 변수(glob)와 예외 (18장)
env_strip = ["ANTHROPIC_API_KEY", "*_API_KEY", "*_TOKEN", "..."]
env_keep = ["CLAUDE_CODE_OAUTH_TOKEN"]
```

- 범위를 벗어난 값(`debate_rounds` 0~5, `max_review` 1~10, implementer == reviewer 등)은 조용히 보정하지 않고 에러로 거부한다.

`tomllib`(3.11+ 표준 라이브러리)로 읽는다.

---

## 9. 안전장치

- **작업 폴더 가드**: workdir이 홈 디렉터리 자체이거나 `/`이면 거부. 기존 git 저장소인데 커밋 안 된 변경이 있으면 거부(`--force`로 무시 가능). 단 `.duet/` 아래 변경은 항상 무시하고, `--skip-design` 일 때는 `DESIGN.md` 변경도 무시한다(설계만 → 사람이 수정 → 구현 흐름).
- **프로세스 정리**: claude/codex 는 새 프로세스 그룹으로 띄우고, 타임아웃·중단 시 그룹 전체를 SIGTERM → SIGKILL 한다. 에이전트가 띄운 테스트나 셸 명령이 남지 않는다. 출력 파이프가 닫히기가 아니라 **프로세스 종료**를 기다리므로, 백그라운드 프로세스가 파이프를 물고 있어도 정상 종료한 호출이 타임아웃으로 처리되지 않는다(2초 유예 후 그룹 정리).
- **git 오류**: 에이전트가 하위 저장소를 만들었거나 커밋이 실패하면 `failed` 로 저장하고 안내 후 종료. 구현 호출이 성공한 뒤 커밋 전에 실패하면 `impl:i:called` 가 남아 resume 때 구현을 다시 부르지 않는다. duet 커밋은 `commit.gpgsign=false` 로 한다.
- **resume 때 사용자 수정 보존**: 리뷰 직전에 커밋 안 된 변경이 있으면 'duet: uncommitted changes before review i' 로 커밋한 뒤 리뷰한다(리뷰어가 바꾼 것으로 오인해 지우지 않음).
- **동시 실행 방지**: `.duet/lock.json` 잠금.
- **반복 상한**: `debate_rounds`, `max_review` 상한을 각각 5, 10으로 강제.
- **리뷰어 쓰기 차단**: READ 모드 + 리뷰 후 `git status` 검사 → 변경 있으면 경고 후 `checkout`/`clean`으로 되돌림(`.duet/` 제외).
- **타임아웃**: 호출별 `timeout_sec`. 초과 시 프로세스 종료 후 실패 처리.
- **한도 도달**: 상태 저장 후 정상 종료, resume 안내.

---

## 10. 테스트 전략

- **실제 claude/codex를 테스트에서 절대 호출하지 않는다.**
- `tests/fake_bin/claude`, `tests/fake_bin/codex`: 받은 인자를 JSON 파일로 덤프하고, 환경변수로 지정한 시나리오에 따라 정해진 응답을 출력하는 가짜 실행 파일. 테스트에서 `PATH` 앞에 끼워 넣거나 config의 `bin`을 이 경로로 지정.
  - 이걸로 검증: 명령어 조립(모드별 플래그), 출력 파싱(JSON / last-message 파일), 비정상 종료, 한도 문구 감지, 타임아웃.
- `MockAdapter`: Phase/Orchestrator 단위 테스트용. 시나리오(예: "첫 리뷰 반려, 두 번째 승인")를 생성자로 주입.
- e2e(mock): 임시 디렉터리에서 `duet run --mock` 전체 실행 → 기록 파일 순서, 커밋 수, 최종 status 검증. 중간에 강제 실패시킨 뒤 `duet resume`으로 완주하는 시나리오 포함.

---

## 11. 구현 마일스톤

각 마일스톤은 **완료 기준을 모두 통과하고 커밋한 뒤** 다음으로 넘어간다.

### M0. 뼈대
- `pyproject.toml`(엔트리포인트 `duet`), 패키지 구조, `duet --help`, pytest 동작
- ✅ `pip install -e .` 후 `duet --help` 출력, `pytest` 통과(빈 테스트라도)

### M1. 실행부 + 어댑터
- `runner.py`, `agents/base.py`, `claude_code.py`, `codex.py`, `mock.py`, `config.py`
- **실제 CLI 검증 단계**: 사용자 PC에 claude/codex가 있으면 `claude --help`, `codex exec --help`를 실행해 5.2 표의 플래그가 맞는지 확인하고, 다르면 기본값을 수정 후 DESIGN.md 5.2를 갱신. (실제 프롬프트 호출은 하지 말 것 — 사용자 구독 한도 소모)
- ✅ fake_bin 기반 테스트로 모드별 명령어, 파싱, 에러, 한도 감지, 타임아웃 검증

### M2. 판정 + 기록 + 상태
- `verdict.py`, `transcript.py`, `state.py`
- ✅ 판정 파싱 테스트(정상/레거시/누락/모순), RunState 저장·로드 왕복 테스트

### M3. 설계 토론 단계
- `prompts.py` + 템플릿, `phases/debate.py`
- ✅ MockAdapter로 라운드 0/1/2 각각 기록 파일 개수·순서, DESIGN.md 생성 검증

### M4. 구현 + 교차 리뷰 단계
- `workspace.py`, `phases/build.py`
- ✅ 승인/미승인/리뷰어가 파일 수정한 경우(되돌림)/diff 잘림 테스트

### M5. Orchestrator + CLI 완성
- `run`, `resume`, `log` 서브커맨드, 작업 폴더 가드, 설정 우선순위
- ✅ e2e mock 테스트(완주, 중단 후 resume), `duet run ... --mock` 수동 실행 시 진행 로그가 보기 좋게 출력

### M6. 마무리
- README(설치, 로그인 전제, 사용 예, 커스텀 템플릿, 주의사항)
- ✅ 사용자가 실제 CLI로 `--design-only` 한 번 돌려볼 수 있는 상태

### M8. 설정·모델·사용량·환경 점검 (2026-10-15 사용자 요청으로 추가)
- `settings.py`, `usage.py`, `doctor.py`, `ui/terminal.py`, `duet doctor`, `--claude-model` 등
- ✅ 설정 층 우선순위, 모델 플래그, 사용량 수집(가짜 CLI·가짜 세션 기록), 점검, pty 터미널 입력 테스트

### M9. 원격 머신 (2026-10-15 사용자 요청으로 추가)
- `remote.py`(duet.pyz 배포, ssh, `duet _api`), 작업 기록 파일화(`duet _job`)
- ✅ 가짜 ssh 로 배포 → 원격 mock 실행 → 원격 기록 조회까지, 헤드리스 브라우저로 화면 확인

### M10. 반려 핑퐁 방지 + duet 테스트 (2026-10-15 사용자 요청으로 추가)
- `policy.py`(승인 판단·수렴 감지), `testing.py`(테스트 명령 결정·실행), 리뷰·수정·구현 템플릿 개정
- ✅ 심각도 판정·수렴 감지·테스트 상태 단위 테스트, build 단계 통합(테스트 실패 시 승인 보류, 부산물 정리, duet.toml 명령, 이전 리뷰 전달, 수렴 안 됨 → resume)

### M11. 리뷰 효율 + 재현 테스트 + 실시간 보기 (2026-10-15 사용자 요청으로 추가)
- 증분 리뷰 diff, 테스트 재실행 안내, `repro.py`, `activity.py`, UI 실시간 작업 보기
- ✅ 재현 테스트 파싱·흐름, 증분 diff(예전 실행 대비), 두 CLI 세션 기록 파싱, 헤드리스 브라우저 확인

### M12. 단계별 추론 강도 + 자식 환경 변수 정리 (2026-10-15 사용자 요청으로 추가)
- `AgentConfig.step_effort`, `--{agent}-step-effort`, Codex 기본 `medium`(종합·첫 리뷰 `high`), `env_strip`/`env_keep`, `doctor` 의 API 키 점검 (18장)
- ✅ 단계·라운드별 effort 결정, 설정 층 규칙(effort 만 정한 층은 단계별을 비움), resume 보존·예전 run 호환, 가짜 CLI 로 단계별 플래그와 자식 환경(비밀 변수 제거·keep 예외·테스트 명령) 검증

### M13. 워크플로 템플릿 (2026-10-16 사용자 요청으로 추가)
- `workflow.py`(역할·단계 정의, 로드·검증, classic), 내장 템플릿 `workflows/*.toml`, 단계 종류 explore/debate/build/audit, 리뷰어 여러 명, 역할별 지시·프롬프트, `--workflow`, `duet workflows`, UI 선택 (19장)
- ✅ 템플릿 검증 오류, classic 이 예전과 같은 기록·커밋을 남김, 역할별 모델·강도 플래그, 리뷰어 2명 판정 합산·수정 프롬프트, explore/audit, resume 시 저장된 정의 사용, UI 목록·실행

### M14. 벤치마크 (2026-10-16 사용자 요청으로 추가)
- `bench.py`, 내장 과제 묶음 `bench_suites/basic/`(숨은 수용 테스트 포함), `duet bench run|report|list` (20장)
- ✅ 숨은 테스트가 참조 구현에서 모두 통과(과제 묶음 자체의 검증), mock 으로 실행 → 숨은 테스트 채점 → results.json·요약표, 실제 실행은 `--yes` 없이는 거부

### M15. 리뷰 비용 줄이기 (2026-10-16 사용자 요청으로 추가)
- 리뷰 패킷(함수 단위 문맥 diff, 바뀐 함수의 호출부, duet 테스트 결과, 재열람 자제 안내), JSON 판정(`--json-schema`/`--output-schema`), 2라운드부터 리뷰어 세션 재사용(선택, 기본 꺼짐 — 실측에서 캐시를 못 써 더 비쌌다)(21장)
- ✅ 패킷 생성(호출부·문맥 diff·크기 제한), 스키마 플래그·구조화 결과 → 기존 판정·재현 테스트 형식 변환, 세션 이어가기 플래그·실패 시 새 세션 폴백·재사용 상한, Codex 이어간 세션의 사용량 차이 계산, 설정으로 끌 수 있음. 효과는 hard 벤치마크로 측정

### M16. 지적 원장과 판정 관문 (2026-10-16 사용자 요청으로 추가)
- `gates.py`: 리뷰어별 지적 원장(안정 ID), 근거 없는 막는 지적 강등, 2라운드부터 범위 밖 새 지적 강등 (22장). JSON 판정에서만
- ✅ 원장 ID 유지·해결 처리, 관문별 강등과 기록, 프롬프트의 열린 지적 목록, 끄기(`review_gates = false`), resume 보존

### M17. 분쟁 중재자 (2026-10-16 사용자 요청으로 추가)
- 리뷰가 수렴하지 않거나 상한에 닿으면 한 번 부르는 중재자(`arbitrate` 템플릿, JSON 결정), 지적별 유지·완화·기각, 중재 승인·마지막 라운드 (23장)
- ✅ 발동 조건(수렴 실패·상한, 원장 필요, 실행당 1번), 결정 적용(원장·판정), 테스트 실패 시 승인 불가, 마지막 라운드의 범위 고정, 끄기, resume, 로그·UI·벤치마크 표시

### M18. 안전장치 (2026-10-16 사용자 요청으로 추가)
- `testguard.py`(테스트 변조 감지 → 리뷰어·중재자에게 경고), UI 의 Origin·Sec-Fetch-Site 검사, Claude 구현자 샌드박스(선택) (24장)
- ✅ 감지 신호별 단위 테스트(삭제·테스트 수·assert 수·skip·설정·재현 테스트), build 통합(기록·로그·리뷰 프롬프트), 교차 출처 요청 거부, 샌드박스 플래그와 doctor 점검

### M19. 한도 대기 후 자동 재개 + 요구 근거 관문 (2026-10-16 사용자 요청으로 추가)
- `limits.py`(재개 시각 파싱·대기 계획), `--wait-on-limit`(run·bench), 리뷰 JSON 의 `basis`·`requirement`, 요구 근거 관문(`spec_gate`) (25장)
- ✅ 메시지별 재개 시각 파싱(초 단위 시각·상대 시간·시계 시각·Codex 사용률), 대기 예산·백오프, 대기 후 같은 호출 재시도, 대기 중 중지, UI·벤치마크 표시. 취향 지적과 찾을 수 없는 인용의 강등, 원장·중재 프롬프트의 요구 인용

### M20. 재제기·되돌림 방지 (2026-10-17 사용자 요청으로 추가)
- 닫힌 지적을 리뷰·수정 프롬프트에 보여 주기, 작은 모델의 재제기 확인(`dedup`, 기본 Claude Haiku), git 으로 되돌림 감지 (26장)
- ✅ 닫힌 지적 목록, 재제기 판정 적용(기각·완화 → P3, 해결됨 → 재발 표시), 확인자 실패 시 건너뜀, 되돌림 감지(해결된 지적을 가로지를 때만), 끄기, resume, 기록·UI

### M21. 어디서나 실행: Windows·Linux·SSH (2026-10-17 사용자 요청으로 추가)
- OS별 프로세스 제어(`procs.py`: 생존 확인·트리 종료·분리 실행), 중지 요청 파일, 실행 파일 찾기(PATHEXT·배치 파일·파이썬 스크립트), OS별 셸·설치 명령, Windows UI 터미널, Windows 에서의 SSH (27장)
- ✅ Windows(이 노트북)와 Linux(css53) 양쪽에서 전체 테스트 통과, Windows 에서 `duet doctor`·`duet ui`·mock 실행·중지 확인

### M22. 관리자 UI 개편 (2026-10-17 사용자 요청으로 추가)
- 접속 쿠키, 프로젝트 목록 정리(벤치마크 분리·없는 폴더·검색·이름), 벤치마크 화면, 설정 항목 정의에서 만드는 설정·새 실행 화면, 실행 상세의 '지적' 탭, Claude 사용률(%) (28장)
- ✅ HTTP 수준 시험(쿠키 접속·교차 출처, 목록 정리 API, 벤치마크 API, 설정 스키마 저장·검증), Claude 스트림 출력 파싱(가짜 CLI), 헤드리스 브라우저로 화면·JS 오류 확인(Windows·Linux)

### M23. 관리자·작업자 분업 (extended 워크플로) — 2026-10-17 제거
- 구현했다가(관리자가 작업 묶음으로 나누고 작업자가 순서대로 구현) 같은 날 사용자 결정으로 없앴다. 이유는 31.4, 남은 흔적은 29장.

### M24. 단계별 모델·추론 강도 (2026-10-17 사용자 요청으로 추가)
- `step_model`(에이전트·역할 층), 실행마다 칸 단위 `step_overrides`(`--set-step`), 세션 이어가기의 모델 유지, 새 실행 창 순서도에서 칸별 지정 (30장)
- ✅ 모델·강도 결정 규칙(층 규칙, 역할, 칸 덮어쓰기, rereview), CLI 인자에 실제로 붙는 `--model`/`-m`, run.json 저장·resume, 세션 이어가기 모델 유지, UI API 검증과 순서도 표시

### M25. 큰 과제 벤치마크 (2026-10-17 사용자 요청으로 추가)
- `large` 과제 묶음(issuetrack, stock-warehouses), 벤치 `--fixed-design`, 결과에 호출당 최대 입력·Codex 한도 환산·주간 한도 사용률 변화, 모이지 않은 숨은 테스트를 실패로 셈 (31장)
- ✅ 참조 구현이 숨은 테스트를 모두 통과, 기존 과제 시작 코드는 새 기능 테스트만 실패, 고정 설계 준비(새·기존), 결과 열 계산, mock 벤치 전 과정

### M26. 결과 요약 (2026-10-17 사용자 요청으로 추가)
- 실행이 끝나면 작은 모델이 '무엇을 만들었고 어떻게 실행하는지'를 정리한다. 실행 상세 맨 위에 보이고, 예전 실행은 버튼이나 `duet report` 로 만든다 (32장)
- ✅ 승인·미승인 뒤 한 번 호출(설계만·중단·실패는 안 함), 실패·한도여도 실행 결과를 바꾸지 않음, 이어하기에서 다시 부르지 않음, `duet report`, UI 표시·만들기 API

### M27. 최신 정보와 첨부 (2026-10-17 사용자 요청으로 추가)
- 권한 수준(`access`, 기본 full), 웹 검색·문서 읽기, 패키지 최신 버전 조회 명령, 과제 첨부 파일(CLI·UI), 웹 화면이 있는 프로젝트의 브라우저 확인(필요할 때만) (33장)
- ✅ 권한 수준·단계·설정별로 CLI 인자에 붙는 도구·플래그(`--permission-mode`, `--dangerously-bypass-approvals-and-sandbox`, `--allowedTools`·`--disallowedTools`, `--search`, `-i`, `--mcp-config`·`-c mcp_servers`), 첨부 복사·프롬프트·크기 제한·UI 업로드, 웹 앱 감지와 앱 띄우기·정리(가짜 서버), 설정·순서도 표시. 실제 모델로는 아직 확인하지 않음

### M28. 혼자 기준선 (2026-10-20 사용자 요청으로 추가)
- 벤치마크에 `solo-claude`·`solo-codex`(그 CLI 혼자 한 번 호출)를 비교 기준선으로 둔다 (34장)
- ✅ 혼자 실행의 호출(구현 자리와 같은 설정·쓰기 모드·과제의 시간 제한)·채점·자체 테스트·한도 처리(멈춤·기다렸다 다시)·요약표의 '-' 칸, classic 과 한 벤치마크에서 함께, mock 전 과정

### M29. 어려운 과제 묶음 (2026-10-21 사용자 요청으로 추가)
- frontier(명세형 5개)·backlog(기존 코드+티켓 3개) 묶음, 참조 구현·검증 테스트, 보정 결과(혼자도 98~100%) (35장)

### M30. tenure 엔진 기반 (2026-10-21 사용자 요청으로 추가)
- 이름 바꾸기 duet → tenure(옛 이름 호환), 상태 어휘·단계 결과 계약·이벤트 스트림(SSE), quick·auto 워크플로, 벤치마크에서 나온 프롬프트 고침 (36장)
- ✅ 옛 폴더·설정·환경 변수 호환, 이벤트 순번·SSE·폴링·받은편지함·seen, 단계 결과 분류, quick(과제 원문을 설계로)·auto(규칙+haiku), mock 전 과정, 실제 벤치마크 54번(36.12). Linux 미검증

### M31. 새 화면 1차 (2026-10-21)
- React 앱(app/ → src/tenure/ui/web), 받은편지함·실행 실시간 화면·새 실행·사용량·벤치마크·환경·설정, 옛 화면은 /classic (36.13)
- ✅ 서버 경로(새 화면·자산·옛 화면, 자산 경로 제한), 빌드가 패키지에 들어감, 브라우저에서 모든 화면과 mock 실행(시작→실시간→결과 요약) 확인, 함께 고친 버그들의 테스트

### M32. 작업마다 worktree·리뷰·병합 (2026-10-21)
- `worktree.py`(만들기·색인·무시 파일 복사), `merge.py`(병합 준비 점검·squash/merge·버리기), `notes.py`(줄 메모), `phases/followup.py`(충돌 해결·메모 반영 라운드), `tenure merge|discard|resolve|notes`, 화면의 병합 패널·줄 메모 diff·브랜치 표시 (36.14)
- ✅ worktree 만들기·동시 실행·id 충돌, 충돌 감지→에이전트 해결→병합, 프로젝트 쪽 변경 점검, 버리기 패치, 메모 오래됨·보내기, 대시보드 API, 브라우저에서 메모 달기→보내기→병합, 충돌 해결 맡기기(mock)

### M33. 경쟁 초안 (2026-10-21)
- `phases/race.py`(초안 worktree·병렬 1라운드·판정·승자 채택·`pick`), 워크플로 `race`(build 의 `rivals`), `tenure pick`, 화면 '초안 비교' 탭·대화의 초안 기록 (36.15)
- ✅ 판정 순서, 동점→구현 담당, 테스트로 승자 결정·승자가 이어서 고치기, 1라운드 승인이면 끝, 사람이 고르기(이전 상태 브랜치 보존·이어하기 막음), 대시보드 초안 기록·차이·고르기, worktree 안의 race, mock 벤치마크에 race 포함, 브라우저 확인
- ✅ 실제 벤치마크 51번(36.17): 정확도는 quick 과 같고(99.90% 대 99.92%) 시간 0.88배·사용량 1.06배 → auto 에 넣지 않고 옵션으로 남김

### M34. 대화형 터미널 (2026-10-21)
- `term/`(세션·훅·화면 규칙), `runner.spawn_pty`(ConPTY/pty), `agentconfig.py`, `tenure explain`, 세션 API·SSE, 화면(터미널·사이드바 세션·받은편지함 승인 카드·프로젝트/실행의 '터미널로 열기'·에이전트 설정 탭·브라우저 알림) (36.19)
- ✅ 가짜 대화형 CLI 로 Claude(훅)·Codex(화면 규칙·notify) 흐름 끝까지, 승인 대기 중 보내기 거부, 승인 키, 끝남·기록 다시 읽기, 원래 notify 이어 부르기, 세션 API, 비밀값 가리기, 브라우저에서 열기→보내기→승인 카드→본 것 처리→왜 이 상태?→에이전트 설정
- 남은 것: 원격 머신 세션, tenure 실행에서 끌 항목 고르기

### M35. 데스크톱 앱 (2026-10-22)
- `app/src-tauri/`(Tauri 2 셸·시작 화면·아이콘·업데이터), `desktop/`(엔진 얼리기·전체 빌드·아이콘), 얼린 엔진용 `selfcmd`·`entry`(숨은 명령 `_hook`·`_pick`·`_http`), `ui --ready-line --parent-pid` (36.20)
- ✅ 엔진 얼리기·확인(훅·UI 서버), Tauri 빌드 → NSIS 설치 파일·업데이트 서명·latest.json, 빌드한 앱으로 엔진 연결·창 닫기/강제 종료 때 엔진 정리·한 번만 뜨기, 얼린 엔진의 자기 호출 테스트
- ✅ 이 PC 에 설치(사용자 단위, %LOCALAPPDATA%	enure)·바탕 화면/시작 메뉴 바로 가기·제거 항목, 바로 가기로 띄운 앱에서 엔진 연결·mock 실행 승인까지 (2026-10-22)
- 주의: Claude 데스크톱 앱(MSIX) 안에서 띄운 명령으로 설치하면 AppData 쓰기가 그 앱의 가상 공간으로 돌아가 바로 가기가 깨진다 → 탐색기로 띄워 설치했다. 사용자가 설치 파일을 직접 두 번 눌러 설치하면 문제없다.
- 남은 것: 깨끗한 다른 PC 에 설치해 보기, 첫 GitHub 릴리스 올리기(요청할 때)

### M41. 지휘자 + 일꾼 중계 (2026-10-25 사용자 요청)
- 포크: 새 작업공간 창의 '지휘자 + 일꾼' 모드, 지휘자 프롬프트, CLI 회차 상한(`TENURE_RELAY_MAX_ROUNDS`), 윈도우 CLI 실행기의 `tenure.exe` 찾기 (36.24)

### M42. 중계 고치기 (2026-10-25 사용자 지적)
- 모델 목록 고르기·모델별 추론 강도, 회차 상한 선택(기본 없음), 과제 없이 시작, 탭 줄 메뉴로 기존 작업공간에서 지휘자 시작 (36.25)

### M43. SSH 실행 위치 (2026-10-25 사용자 요청)
- 원격용 orcad 템플릿을 포크 소스로 만들고(네이티브 파일은 이 PC 의 정식 오르카에서) 설치 파일에 넣기 (36.26)

### M7. 관리자 UI (2026-10-15 사용자 요청으로 추가)
- `registry.py`, `lock.py`, `ui/dashboard.py`, `ui/server.py`, `ui/static/*`, `duet ui`
- ✅ HTTP 수준 테스트(토큰·Host 검사, 경로 조작 거부, 조회, 실행·중지·이어하기), 헤드리스 브라우저로 화면·JS 오류 확인

---

## 12. 리스크와 미결 사항

| 리스크 | 대응 |
|---|---|
| CLI 플래그가 버전마다 다름 | 플래그를 설정으로 분리, M1에서 `--help`로 검증 |
| 구독 사용량 한도 | 반복 상한, 한도 감지 후 중단·resume |
| 긴 프롬프트가 인자 길이 초과 | stdin/파일 폴백 (M1에서 각 CLI 지원 방식 확인) |
| 모델이 판정 형식을 안 지킴 | 레거시 형식 허용, 실패 시 반려로 간주 + 기록 |
| 둘이 끝없이 반려 핑퐁 | `max_review` 상한, 구현자 반론을 `IMPLEMENTATION_NOTES.md`에 남기게 해 리뷰어가 참고 |
| 약관 | 공식 CLI를 본인 PC에서 개인 용도로만 사용. 웹 자동화 금지 |

**미결 (v2 후보)**: 사람 개입 모드(라운드마다 승인 받기), 리뷰어 2명(둘 다 리뷰), 토론 요약 리포트 HTML, 3번째 모델 추가.

---

## 13. 관리자 UI (`duet ui`)

본인이 여러 작업 폴더의 실행을 한눈에 보고, 브라우저에서 시작·중지·이어하기를 하기 위한 로컬 웹 대시보드.

```
duet ui [--port 8787] [--host 127.0.0.1] [--workdir PATH ...] [--no-browser]
```

### 구성
- **프로젝트 목록**: `$DUET_HOME/projects.json`(기본 `~/.duet`). `duet run`/`resume` 이 작업 폴더를 자동 등록하고, UI 에서 경로를 추가·제거(목록에서만)할 수 있다.
- **서버**: 표준 라이브러리 `http.server`(ThreadingHTTPServer) + JSON API. 화면은 의존성 없는 HTML/CSS/JS 한 벌(`ui/static`).
- **실행 제어**: UI 는 `python -m duet run|resume ...` 를 백그라운드 프로세스(새 세션)로 띄운다. 과제는 `<workdir>/.duet/ui/tasks/*.md` 에, 프로세스 출력은 `.duet/ui/jobs/*.log` 에 남는다. UI 를 꺼도 실행은 계속된다.
- **중지**: `.duet/lock.json` 의 pid 에 SIGTERM → duet 가 `stopped_user` 로 저장하고 진행 중이던 호출의 프로세스 그룹을 정리한다.
- **실시간**: 실행 중이면 2초마다 폴링. `console.log` 를 이어 읽고, 마지막 "실행 중" 줄로 지금 호출과 경과 시간을 보여준다.

### 화면
| 화면 | 내용 |
|---|---|
| 프로젝트 | 프로젝트 카드(최근 상태, 실행 중 표시), 경로 추가, UI 가 시작한 작업의 실패 로그 |
| 프로젝트 상세 | 실행 기록 표, DESIGN.md, 새 실행, 중지 |
| 실행 상세 | 상태·구성·리뷰 이력, 이어하기(미승인이면 리뷰 상한 지정)·중지, 탭: 대화(발언 타임라인 + 마크다운) / 콘솔 / 변경 사항(커밋별 diff, 리뷰어가 받는 diff) / 설계 문서(덮어쓰기 전 백업 포함) |

### UI 서버 업데이트
- 시작할 때 duet 패키지 코드의 해시를 기억하고, `GET /api/server` 로 지금 설치된 코드와 비교한다. 다르면 사이드바에 '업데이트됨 · 다시 시작' 안내를 띄운다(설정 화면에서는 언제든 다시 시작 가능).
- 다시 시작은 같은 포트·같은 토큰으로 자기 자신을 exec 한다(브라우저는 서버가 다시 뜨면 자동 새로고침). duet run 은 별도 프로세스라 계속 돈다. 설치·로그인 같은 터미널 작업이 진행 중이면 거부한다.

### 보안
UI 의 실행 버튼은 셸 권한이 있는 에이전트를 띄우므로, 다른 웹사이트가 브라우저를 통해 이 API 를 부르지 못하게 막는다.
- 기본 바인딩 127.0.0.1. 다른 주소면 시작할 때 경고하고 SSH 터널을 권한다.
- 시작할 때마다 새 토큰. 페이지는 `?t=<토큰>` 이 있어야 열리고, API 는 `X-Duet-Token` 헤더가 맞아야 응답한다(커스텀 헤더라 교차 출처 요청은 사전 요청에서 막힌다).
- 루프백 바인딩이면 Host 헤더가 `127.0.0.1:포트`/`localhost:포트` 인지 검사(DNS 리바인딩 방지).
- CSP `script-src 'self'`, 인라인 스크립트·스타일 없음, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- 모델 출력(마크다운, diff)은 innerHTML 을 쓰지 않고 DOM 노드로만 그린다. 링크는 http(s) 만 클릭 가능.
- 경로 인자는 등록된 프로젝트, 실제 run 디렉터리, `NN_*.md` 기록 파일, 16진 커밋 해시만 허용.

---

## 14. 설정·모델·사용량·환경 점검

### 전역 설정 `~/.duet/settings.json`
- `workspace_roots`: 새 실행 때 고르는 상위 폴더 목록(첫 번째가 기본). UI 는 "위치 + 폴더 이름"으로 경로를 만든다.
- `defaults`: duet.toml 과 같은 형식의 기본 실행 설정. 우선순위는 **CLI 인자 > `<workdir>/duet.toml` > settings.json defaults > 기본값**. 파일이 깨져 있으면 무시한다.
- `machines`: 원격 머신(15장).

### 모델·추론 강도
- `[claude] model/effort` → `claude --model X --effort Y`, `[codex] model/effort` → `codex exec -m X -c model_reasoning_effort="Y"`. 비우면 각 CLI 의 기본값.
- CLI: `duet run ... --claude-model opus --claude-effort high --codex-model gpt-6.1-sol --codex-effort xhigh`.
- 실행 상태(`run.json` config)에 저장해 resume 때 처음과 같은 모델을 쓴다.
- 단계마다 다른 추론 강도는 `step_effort`(18장). duet 는 Codex 의 기본 강도를 `medium` 으로 명시하므로 `~/.codex/config.toml` 의 `model_reasoning_effort` 보다 우선한다.
- UI 후보: Claude 는 별칭(fable/opus/sonnet/haiku)과 전체 이름, Codex 는 `~/.codex/models_cache.json` 의 모델과 모델별 지원 추론 강도.

### 사용량 (로컬 파일만 읽음, 구독을 쓰지 않음)
- 호출마다 `AgentResult.usage` → 기록 메타(`tokens_in`, `tokens_out`, `cost_usd`, `model`)와 `run.json` calls 에 저장.
  - Claude: `-p --output-format json` 응답의 usage·total_cost_usd·modelUsage.
  - Codex: 호출 직후 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` 중 같은 작업 폴더·시작 시각 이후의 세션에서 token_count(누적 토큰)와 rate_limits.
- 구독 한도: Codex 는 최근 세션 기록의 rate_limits(창별 사용률 %, 초기화 시각, 플랜). Claude CLI 는 한도(%)를 노출하지 않으므로 `~/.claude/projects/*.jsonl` 의 메시지 사용량을 합산해 최근 5시간·7일 토큰만 보여준다.
- UI '사용량' 화면: Codex 한도 막대, Claude 토큰, duet 실행의 에이전트별·날짜별·프로젝트별 합계. 실행 화면에는 에이전트별 토큰과 Codex 한도 70% 이상 경고.

### 환경 점검과 설치·로그인
- `duet doctor [--json]`: duet/Python, git, curl, uv, Claude Code(설치·`claude auth status`), Codex CLI(설치·`codex login status`).
- UI '환경·로그인' 화면의 버튼은 **고정된 동작만** 실행한다(임의 명령 없음):
  - 설치: `curl -fsSL https://claude.ai/install.sh | bash`, `curl -fsSL https://chatgpt.com/codex/install.sh | sh`, `curl -LsSf https://astral.sh/uv/install.sh | sh`, `uv python install 3.12`, git/curl 은 시스템 패키지 관리자(sudo).
  - 로그인: `claude auth login`, `codex login --device-auth`. 로그아웃도 제공.
- 동작은 가상 터미널(pty)에서 돌고, UI 가 출력(제어 문자 제거)을 보여주며 링크를 뽑아 주고, 입력칸으로 코드·sudo 비밀번호(숨김)를 보낼 수 있다.
- 설치 스크립트가 `~/.local/bin` 에 두므로 duet 는 자식 프로세스 PATH 끝에 `~/.local/bin`, `~/.duet/bin` 을 덧붙인다(사용자 PATH 우선).

## 15. 원격 머신

- 설정의 머신: `{id, name, ssh: "user@host", port, workspace_root, python}`. SSH 는 **키 인증만**(BatchMode), ControlMaster 로 연결 재사용.
- 원격에는 Python 3.11+ 만 필요하다. UI 의 '배포'가 설치된 duet 패키지로 실행 가능한 zip(`duet.pyz`, 내용 해시로 버전 비교)을 만들어 `~/.duet/bin/` 에 복사하고 `~/.duet/bin/duet` 래퍼를 만든다. Python 이 없으면 uv → Python 3.12 설치 버튼으로 준비한다.
- 원격 조회·동작은 `ssh <머신> <python> ~/.duet/bin/duet.pyz _api <메서드>` (인자·결과 JSON). 로컬 HTTP API 와 같은 메서드 표를 쓴다. 원격 실행 기록·프로젝트 목록은 원격 머신의 `.duet/`, `~/.duet/` 에 남는다.
- UI 는 모든 화면·주소가 머신을 구분한다(`#/m/<id>/p/<경로>/r/<run>`). 프로젝트 목록은 머신별로 묶고, 원격 목록은 8초 캐시.
- 설치·로그인 동작은 `ssh -tt <머신> '<명령>'` 으로 같은 터미널 화면에서 한다.
- UI 가 시작한 작업은 `~/.duet/jobs/*.json` 에 기록하고, `duet _job` 래퍼가 끝날 때 종료 코드를 `.rc` 에 남긴다(UI 를 다시 켜거나 원격에서도 결과를 안다).

---

## 16. 반려 핑퐁 방지와 duet 테스트

memo-cli 실험에서 리뷰 4라운드가 모두 반려됐다(반드시 고칠 것 3 → 1 → 2 → 1). 구현자가 지적된 예외만 좁게 막고, 리뷰어가 매번 더 극단적인 입력을 새로 찾았다. 또 리뷰어는 읽기 전용이라 파일을 만드는 테스트를 돌리지 못했다.

### 테스트 단계 (build: impl 커밋 → **test** → review)
- 명령 결정: 명시값(`--test-command`, 설정의 `test_command`, "auto" 가 아닌 것) → 작업 폴더 `duet.toml` 의 `test_command`(구현 프롬프트가 구현자에게 적게 한다) → 자동 감지(pyproject/setup.py + tests → `uv run pytest -q`(uv.lock 있을 때) 또는 `python3 -m pytest -q`, package.json test 스크립트 → `npm test --silent`, Cargo → `cargo test --quiet`, go.mod → `go test ./...`, Makefile test → `make test`). `none` 이면 끔.
- 결과 상태: passed / failed / timeout / error(명령 없음 126·127, pytest 수집 0개=5). 기록 `NN_test{i}.md`(agent: duet), `run.json` 의 `tests[i]`.
- 테스트가 만든 부산물은 커밋하지 않는다(테스트 후 작업 트리를 커밋 상태로 되돌림).
- 결과는 리뷰 프롬프트와 다음 수정 프롬프트에 들어간다. `require_tests_pass`(기본 true)면 failed/timeout 일 때 리뷰어가 승인해도 승인하지 않는다. error 는 환경 문제일 수 있어 막지 않는다.

### 심각도와 승인 판단 (`policy.decide`)
- 리뷰 템플릿: 지적마다 [P1] 핵심 동작·데이터 손실·보안·테스트 실패, [P2] 설계 위반·일반 입력 오류, [P3] 드문 입력·극단적 경계 사례·취향. 판정 블록에 `p1/p2/p3` 개수.
- 막는 지적 수 = `block_severity`(기본 P2) 이하 심각도 개수의 합. 심각도가 있으면 duet 가 이 수로 판단한다(0 이면 리뷰어가 반려해도 승인, 0 이 아니면 리뷰어가 승인해도 반려). 심각도가 없으면 예전 규칙(must_fix, 모순 처리).
- 리뷰어에게 이전 리뷰의 '반드시 고칠 것'을 보여주고, 해결 여부를 먼저 확인하게 하며 새 지적에는 [새] 를 붙이게 한다.
- `run.json` 의 `review_history`: 라운드별 리뷰어 판정, duet 판단, 막는 지적 수, 심각도, 테스트 상태, 이유.

### 수렴 실패 감지 (`policy.stalled`)
- 막는 지적 수가 `stall_rounds`(기본 2) 라운드 연속으로 그 전까지의 최저치보다 낮아지지 않으면 멈춘다: `done_unapproved`, `stop_reason: "stalled"`. 상한까지 가서 멈추면 `stop_reason: "max_review"`.
- 수렴 안 됨으로 일찍 멈춘 실행은 `--max-review` 없이 resume 할 수 있고, 이때 수렴 판단은 그 라운드부터 다시 센다(`stall_baseline_round`).

### 설정
`test_command = "auto"`, `test_timeout_sec = 600`, `require_tests_pass = true`, `block_severity = "P2"`, `stall_rounds = 2`. CLI: `--test-command`, `--block-severity`, `--stall-rounds`. 실행 상태에 저장해 resume 때 같은 규칙을 쓴다. UI 의 새 실행 창·설정 화면에서도 고른다.

---

## 17. 리뷰 효율, 재현 테스트, 실시간 작업 보기

### 리뷰 토큰
memo-cli 실측(Codex 세션 기록): 설계 단계 호출은 입력 약 2만 토큰인데 리뷰는 28만~39만 토큰이었다. 리뷰 프롬프트(diff 6만 자)가 리뷰어의 도구 호출마다 다시 입력되기 때문이다.
- `review_diff = "incremental"`(기본): 2라운드부터 지난 리뷰 시점 커밋(`reviewed_commits[i]`, 없으면 'duet: impl round i' 커밋) 이후의 diff 만 넣고, 처음부터의 변경은 파일 요약(stat)만 준다. `full` 이면 매번 전체.
- duet 가 테스트를 돌린 라운드에는 리뷰어에게 "전체 테스트를 다시 돌리지 마라, 필요한 것만 좁게"라고 안내한다.

### 재현 테스트 (`repro_tests = true`)
- 리뷰어는 지적을 `<repro path="tests/test_repro_x.py">…</repro>` 로 낼 수 있다. duet 가 검증(작업 폴더 안 상대 경로, 이름이나 폴더에 test/spec, 새 파일만, 최대 5개·20KB)해 저장하고 'duet: repro tests from review i' 로 커밋한 뒤 테스트 명령을 돌린다.
- 결과: failed = 재현됨(지적이 맞음), passed = 재현 안 됨. 기록 `NN_repro{i}.md`, `run.json` 의 `repros[i]`.
- 다음 수정 프롬프트에 파일 목록과 재현 여부를 넣고, 통과시키게 한다(테스트가 틀렸다면 고치고 이유를 IMPLEMENTATION_NOTES.md 에). 다음 라운드의 duet 테스트에 자연히 포함된다.

### 실시간 작업 보기
- 호출 방식은 그대로 두고, 두 CLI 가 실시간으로 쓰는 세션 기록을 읽는다(`activity.py`). Claude: `~/.claude/projects/<cwd 의 영숫자 외 문자를 '-' 로>/*.jsonl`(없으면 최근 기록 중 cwd 일치), Codex: 세션 기록 중 cwd 일치. 지금 호출 시작 이후의 파일 읽기·수정·명령·검색·메시지를 뽑는다.
- `GET /api/activity` (원격 머신도 같은 API). UI 는 실행 중이면 타임라인의 '진행 중' 항목을 자동으로 열어 2초마다 갱신한다.


---

## 18. 단계별 추론 강도와 자식 환경 변수

근거: `reports/AI 에이전트 오케스트레이션 고급 조사.md`(로컬 조사 보고서) 개선 후보 1·2번. Codex 요금표에서 출력(추론 포함)은 미캐시 입력의 5배, 캐시 입력의 50배 요율이라, 모든 단계를 xhigh 로 도는 것이 가장 큰 회피 가능 비용이다. OpenAI 가이드는 medium 을 일상 기본으로 둔다. 또 `claude -p` 는 `ANTHROPIC_API_KEY` 가 있으면 구독 대신 API 과금으로 조용히 바뀐다.

### 18.1 단계별 추론 강도 (`step_effort`)
- `[claude]`·`[codex]` 에 `step_effort = { 단계 = 강도 }`. 단계 키: `propose`, `critique`, `synthesize`, `implement`, `fix`, `review`(1라운드 리뷰), `rereview`(2라운드부터의 리뷰).
- 호출의 강도 = `step_effort[단계]` → 없으면 `effort` → 비어 있으면 강도 플래그를 붙이지 않음(CLI 기본값). 키마다 독립이다(`rereview` 가 없으면 `review` 가 아니라 `effort` 로 간다).
- 기본값: Codex `effort = "medium"`, `step_effort = { synthesize = "high", review = "high" }`. 즉 설계 종합과 첫 리뷰만 high, 증분 재리뷰와 나머지는 medium, xhigh 는 사용자가 고를 때만. Claude 는 비워 둔다(CLI 기본값).
- **층 규칙**: 한 설정 층(전역 설정 defaults, `duet.toml`, CLI 인자)이 `effort` 만 정하고 `step_effort` 를 정하지 않으면 그 층이 `step_effort` 를 비운다 = "모든 단계 같은 강도". 둘 다 정하면 둘 다 적용한다. `step_effort` 는 층마다 통째로 바뀐다(키 단위 병합 안 함).
  - 그래서 `--codex-effort xhigh` 는 예전처럼 모든 단계 xhigh, `effort = ""` 는 예전처럼 CLI 기본값(`~/.codex/config.toml`)을 쓴다.
- CLI: `--{agent}-step-effort review=high,fix=low` (같은 실행에 `--{agent}-effort` 를 함께 주면 나머지 단계는 그 값).
- `run.json` config 에 `{agent}.step_effort` 를 저장해 resume 때 같은 규칙을 쓴다. 이 키가 없는 예전 실행은 저장된 단일 `effort` 로 모든 단계를 돈다(예전과 같음).
- 기록 메타와 사용량의 `effort` 는 그 호출에 실제로 쓴 값. 시작 로그: `codex gpt-6.1-sol(medium; synthesize·review=high)`.
- UI: 새 실행 창의 추론 '기본값(단계별)'은 아무 플래그도 넘기지 않아 단계별 기본이 유지된다. 값을 고르면 모든 단계 그 값.

### 18.2 자식 프로세스 환경 변수 (`env_strip`, `env_keep`)
- claude/codex 호출과 duet 가 돌리는 테스트 명령의 자식 프로세스에서, 이름이 `env_strip`(glob, 대소문자 무시)에 맞는 환경 변수를 뺀다. 단 `env_keep` 에 있는 이름은 남긴다.
  - 기본 `env_strip`: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, `CODEX_API_KEY`, `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_SECRET_*`, `*_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`, `SSH_AUTH_SOCK`.
  - 기본 `env_keep`: `CLAUDE_CODE_OAUTH_TOKEN`(Claude 구독 인증 토큰이라 빼면 로그인이 풀린다).
  - `env_strip = []` 이면 예전처럼 전부 넘긴다.
- git, `doctor`, ssh(원격), UI 가 띄우는 duet 는 그대로 둔다. ssh 는 `SSH_AUTH_SOCK` 이 필요하고, UI 가 띄운 duet 는 자기 자식에게 같은 규칙을 다시 적용한다.
- duet 가 정한 변수(테스트의 `CI=1` 등)는 빼지 않는다. 실행 시작 로그에 뺀 변수 **이름**만 한 줄 남긴다(값은 남기지 않음).
- `duet doctor`: 셸에 `ANTHROPIC_API_KEY`·`ANTHROPIC_AUTH_TOKEN`·`OPENAI_API_KEY`·`CODEX_API_KEY` 가 있으면 경고 항목을 띄운다. duet 설정이 그 변수를 빼면 "duet 는 빼고 넘김(직접 `claude -p` 를 돌리면 API 과금)", 안 빼면 "구독 대신 API 로 과금될 수 있음".

---

## 19. 워크플로 템플릿

미리 정해 둔 **역할군**과 **단계 순서**를 템플릿 파일로 두고 골라 쓴다. 예: "일반 리뷰 + 보안 리뷰", "기존 코드 조사 후 설계", "설계 토론 없이 싸게". 실행은 지금처럼 한 번에 하나씩 순차로 하고, 흐름 제어·resume·종료 판단은 duet 코드가 맡는다(템플릿은 정해진 단계 종류를 조합할 뿐 분기·반복을 직접 쓰지 않는다).

### 19.1 역할 (`[roles.<이름>]`)
| 키 | 뜻 |
|---|---|
| `agent` | `claude` 또는 `codex` (필수) |
| `model`, `effort`, `step_effort` | 이 역할의 모델·추론 강도. 없으면 `[claude]`/`[codex]` 설정(CLI `--codex-model` 등 포함)을 따른다. 층 규칙은 18.1 과 같다(effort 만 쓰면 단계별을 비움) |
| `instructions` | 이 역할의 모든 프롬프트 끝에 붙는 추가 지시 (예: "보안 관점으로만 봐라"). 4000자 이하 |
| `templates` | 단계별 프롬프트 템플릿 교체 `{ review = "review_security" }`. 키는 단계 이름, 값은 템플릿 이름 |

- 이름은 `[a-z][a-z0-9_]{0,23}`. 같은 CLI 를 여러 역할이 써도 된다(예: Claude Opus 설계자 + Claude Sonnet 구현자). bin·args 는 `[claude]`/`[codex]` 설정을 공유한다.
- 기록·로그에는 역할 이름이 보이고(`architect ← propose`), 메타에 `agent`(CLI)와 `role` 을 함께 남긴다. 사용량 합계는 CLI 기준.

### 19.2 단계 (`[[steps]]`, 위에서 아래로 순서대로)
| kind | 키 | 하는 일 | resume 키 |
|---|---|---|---|
| `explore` | `id`, `role` | 읽기 전용으로 기존 코드를 조사(`explore` 템플릿). 결과를 이후 propose·implement 프롬프트 끝에 "사전 조사"로 붙인다 | `explore:<id>` |
| `debate` | `proposers`(1~2), `rounds`, `synth`, `output` | 지금의 설계 토론. 제안자가 1명이면 비판 없이, `synth` 가 없으면 그 제안을 그대로 설계 문서로. `output = "change"` 면 기존 프로젝트의 변경 설계(19.5) | `debate` (+ 기존 세부 키) |
| `build` | `implementer`, `reviewers`(1~3), `max_review` | 지금의 구현·테스트·리뷰 루프. 리뷰어가 여럿이면 아래 19.3 | 기존 키 + `review:i:<역할>` |
| `audit` | `id`, `roles`(1~3) | build 가 승인으로 끝난 뒤, 기준 커밋부터 전체 diff 를 한 번 감사(`audit` 템플릿). **참고용**: 결과는 기록·로그·`run.json` 의 `audits` 에 남고 실행 상태는 바꾸지 않는다. 로그는 `문제 없음` 또는 `확인 필요 — P1=… P2=… P3=… (참고용)` 으로, '반려'라고 쓰지 않는다 | `audit:<id>` |

- 규칙: 단계 1개 이상, `debate`·`build` 는 각각 최대 1개, `debate` 는 `build` 앞, `audit` 은 `build` 뒤, `explore` 는 `build` 앞. `id` 는 같은 kind 안에서 겹치지 않음(기본값 kind 이름). 참조하는 역할은 모두 정의돼 있어야 하고, 한 단계 안에서 역할이 겹치면 안 된다(리뷰어 목록에 구현자 불가).
- `rounds`·`max_review` 를 템플릿에 쓰면 그 값이 duet.toml·전역 설정보다 우선하고, CLI `--debate-rounds`·`--max-review` 가 그보다 우선한다. 정해진 값은 실행 상태에 저장한다.
- `--design-only` 는 debate 단계까지만(앞의 explore 포함), `--skip-design` 은 debate 단계를 건너뛴다.

### 19.3 리뷰어 여러 명
- 같은 라운드에서 리뷰어를 순서대로 부른다(같은 diff, 같은 테스트 결과). 각자 `policy.decide` 로 판단하고, **모두 승인해야 승인**, 막는 지적 수는 합(수렴 판단도 합 기준). 하나라도 판정 블록이 없으면 그 리뷰어는 반려로 본다(기존 규칙).
- 수정 프롬프트에는 모든 리뷰를 `### <역할> 의 리뷰` 로 이어 붙여 넣는다. 각 리뷰어의 '이전 리뷰'는 자기 직전 리뷰다. 재현 테스트는 모든 리뷰에서 모아 한 번에 처리한다.
- `review_history[i]` 의 기존 필드는 합산 값, `reviewers` 에 리뷰어별 판정. `last_reviews` 에 역할 → 마지막 리뷰 파일. 리뷰어가 1명이면 기록·필드가 예전과 같다.

### 19.4 템플릿 파일과 선택
- 위치(앞이 우선): `<workdir>/.duet/workflows/<이름>.toml` → `$DUET_HOME/workflows/<이름>.toml` → 내장(`duet/workflows/`). `--workflow @경로.toml` 로 파일을 직접 줄 수도 있다.
- 최상위 키: `name`(파일 이름과 같지 않아도 됨, 표시용), `description`, `[roles.*]`, `[[steps]]`. 알 수 없는 키는 거부한다.
- 선택: CLI `--workflow 이름`, 설정 `workflow = "이름"`(duet.toml·전역 설정). 기본 `classic`.
- **`classic`** 은 파일이 아니라 설정에서 만든다: 역할 `claude`·`codex`(설정 그대로), `debate(proposers=[claude, codex], synth=synth, rounds=debate_rounds)` → `build(implementer, [reviewer], max_review)`. 기록 이름·resume 키·출력이 M12 와 완전히 같다. `--implementer/--reviewer/--synth` 는 classic 에서만 쓸 수 있다(다른 템플릿과 함께 주면 사용 오류).
- 실행 시작 때 고른 템플릿을 해석한 정의 전체를 `run.json` 의 `workflow` 에 저장하고, resume 은 파일이 아니라 이 저장본을 쓴다. 예전 실행(`workflow` 없음)은 classic.
- 내장 템플릿: `secure`(classic + 보안 리뷰어 + 마지막 감사), `existing`(기존 코드 조사 → 변경 설계 토론(`output = "change"`) → 구현), `lean`(제안자 1명, 토론 없음, 리뷰어 1명. 가장 쌈).
- 프롬프트 템플릿 찾는 곳도 같은 순서로 넓힌다: `<workdir>/.duet/templates/` → `$DUET_HOME/templates/` → 내장. 내장에 `explore.md`, `audit.md`, `review_security.md` 추가. 이름은 `[a-z][a-z0-9_]*`.
- `duet workflows [--workdir P]`: 쓸 수 있는 템플릿 목록(이름, 출처, 설명, 역할·단계 요약). `duet workflows show 이름` 은 해석한 정의를 보여 준다.
- UI: 새 실행 창에 워크플로 선택(설명·역할·단계 미리보기), 실행 상세에 워크플로 이름과 역할 표시. 지금 호출의 실시간 보기는 역할 → CLI 로 바꿔 세션 기록을 찾는다.

### 19.5 기존 프로젝트의 변경 설계 (`output = "change"`)
실제 실행(existing 워크플로)에서 설계 단계가 프로젝트 전체 설계인 DESIGN.md(122줄)를 이번 기능만 다룬 설계(334줄)로 덮어썼다. 설계 단계가 새 프로젝트를 전제로 만들어졌기 때문이다.
- debate 단계에 `output = "change"` 를 주면 설계 결과를 `<workdir>/docs/changes/<run_id>.md` 에 쓰고 **DESIGN.md 는 건드리지 않는다**. 파일은 기준 커밋('duet: design')에 들어간다. 기본값 `output = "design"` 은 예전처럼 DESIGN.md(덮어쓸 때 백업).
- 실행 상태 `design_doc` 에 이 경로(작업 폴더 기준 상대 경로)를 저장해 resume 때도 같은 파일을 쓴다. 없으면 `DESIGN.md`.
- 프롬프트: 구현·수정·리뷰·감사 템플릿은 `DESIGN.md` 대신 `{design_doc}` 를 가리키고, 변경 설계일 때 `{design_note}` 에 "기존 프로젝트의 변경이다. 이번 변경의 설계는 그 파일, 프로젝트 전체 설계(DESIGN.md, 있다면)와 기존 코드 관례를 따르고, 과제에 필요하지 않은 기존 동작은 바꾸지 마라"를 넣는다(새 프로젝트면 빈 줄). 설계안·비판·종합 프롬프트 끝에는 "프로젝트 전체를 다시 설계하지 말고 이번 변경만 설계해라(바꿀 파일·함수, 지킬 기존 동작, 테스트, 문서 갱신)"를 붙인다.
- 구현자가 고치면 안 되는 파일은 `{design_doc}` 이다. 변경 설계에서는 DESIGN.md 가 프로젝트 문서이므로, 변경에 필요하면 구현자가 갱신할 수 있다.
- 리뷰 diff 에서 빼는 파일도 `{design_doc}` (새 프로젝트면 DESIGN.md, 변경 설계면 변경 설계 파일. DESIGN.md 갱신은 리뷰 대상).
- `--design-only` 의 끝 안내와 UI '설계 문서' 탭은 그 실행의 `design_doc` 을 보여 준다.

### 19.6 명령을 실행할 수 없는 리뷰어·감사자
실제 실행에서 Claude 감사자가 "실행 권한이 없어 정적으로만 봤다"고 답했다. Claude 의 읽기 모드는 `--allowedTools Read,Glob,Grep` 이라 명령을 못 돌리는데, 프롬프트는 "필요한 부분만 좁게 확인해라"고 했다. Codex 의 `--sandbox read-only` 는 명령을 돌릴 수 있다.
- 역할이 읽기 모드에서 명령을 실행할 수 있는지: Claude 는 `read_args` 에 `Bash` 가 있을 때만, Codex 는 항상.
- 리뷰의 `{test_instruction}` 과 감사의 `{run_instruction}` 을 이에 맞춘다. 실행할 수 없으면 "너는 이 호출에서 명령을 실행할 수 없다. 코드와 duet 테스트 결과로 판단하고, 실행해 봐야 알 수 있는 의심은 추측으로 반려하지 말고 재현 테스트로 내라(재현 테스트를 끈 실행이면 P3 로)".

### 19.7 테스트 명령의 출처 표시
작업 폴더 `duet.toml` 의 `test_command` 는 설정 층으로도 읽히므로 출처가 `설정` 으로 찍혔다. 설정값이 작업 폴더 `duet.toml` 의 값과 같으면 출처를 `duet.toml` 로 표시한다.

---

## 20. 벤치마크 (`duet bench`)

"두 모델 교차 리뷰가 실제로 나은가", "이 개선이 비용을 줄였나"를 숫자로 답하기 위한 도구. 같은 과제 묶음을 워크플로별로 돌리고, **에이전트가 보지 못한 숨은 수용 테스트**로 결과물을 채점한다.

### 20.1 과제 묶음
```
bench_suites/<묶음>/<과제>/
├── bench.toml     # name, description, kind = "new" | "existing", max_review(선택), timeout_sec(선택)
├── task.md        # 에이전트에게 주는 과제. 숨은 테스트가 부를 인터페이스(패키지 이름, 함수, CLI)를 명시
├── setup/         # (existing) 시작 코드. 작업 폴더에 복사 후 커밋하고 시작
└── hidden/        # 숨은 수용 테스트 (pytest). 실행이 끝난 뒤 채점용 복사본에만 넣는다
```
- 내장 묶음 `basic`(Python, 표준 라이브러리만): 새 프로젝트 4개(`roman` 로마 숫자, `intervals` 구간 병합, `inimini` INI 파서, `textstats` wc 비슷한 CLI) + 기존 프로젝트 1개(`textstats-maxline`: textstats 에 `-L` 추가). 패키지는 작업 폴더 최상위에 둔다(src 레이아웃 아님)고 과제에 적는다.
- 숨은 테스트의 정확성은 duet 저장소의 테스트가 참조 구현(`tests/bench_refs/`)으로 검증한다. 과제 묶음의 버그가 점수를 왜곡하지 않게.
- 사용자 묶음: `--suite <경로>` 로 같은 구조의 폴더를 줄 수 있다.

### 20.2 실행 (`duet bench run`)
```
duet bench run [--suite basic|경로] [--workflow W ...] [--task T ...] [--repeat N] [--max-review N] [--out DIR] [--mock] [--yes]
```
- 과제 × 워크플로 × 반복마다 `<out>/<시각>/<과제>/<워크플로>-<n>/` 에 작업 폴더를 만들고(existing 이면 setup 복사 후 커밋), 같은 프로세스에서 Orchestrator 로 실행한다(순차). 기본 워크플로 `classic`, 기본 out `$DUET_HOME/bench`.
- 실제 실행(`--mock` 아님)은 구독 사용량을 크게 쓰므로, 몇 번 돌지와 예상 호출 수를 보여 주고 `--yes` 가 없으면 시작하지 않는다.
- 한도 도달로 멈춘 실행은 그 자리에서 결과를 `stopped` 로 남기고 다음으로 넘어가지 않고 **벤치마크 전체를 멈춘다**(남은 실행을 돌려 봐야 다 멈춘다). `results.json` 은 실행마다 갱신하므로 중간 결과가 남는다.

### 20.3 채점
- 실행이 끝나면 작업 폴더를 채점용 폴더로 복사(`.git`, `.duet` 제외)하고 `hidden/` 을 `duet_hidden_tests/` 로 넣어 pytest 를 돌린다(`--continue-on-collection-errors`: 한 파일의 import 실패가 다른 파일의 채점을 막지 않게, 2026-10-17 추가)(`PYTHONPATH=채점 폴더`, 비밀 변수 제거, 시간 제한). pytest 는 `uv` 가 있으면 `uvx pytest`, 없으면 `python3 -m pytest`.
- 기록: 상태(approved/unapproved/stopped/failed), 리뷰 라운드, 숨은 테스트 통과/전체, 소요 시간, 호출 수, CLI 별 입력·캐시·출력 토큰, Claude 비용(USD, 참고), 에이전트 자체 테스트 결과(마지막 duet 테스트).
- 승인 여부와 숨은 테스트 통과는 따로 본다: "승인했는데 숨은 테스트 실패" = 리뷰가 놓친 결함.

### 20.4 결과
- `<out>/<시각>/results.json`(실행별 행) + `summary.md`(워크플로별 집계: 실행 수, 승인률, 숨은 테스트 통과율 평균, 전부 통과한 비율, 승인했는데 실패한 수, 평균 라운드·시간·호출, 평균 토큰).
- `duet bench report <폴더>` 로 요약을 다시 출력, `duet bench list` 로 묶음·과제 목록.

### 20.5 어려운 과제 묶음 (`hard`)
`basic` 실측(2026-10-16): classic·lean 모두 5/5 승인, 숨은 테스트 100% — 과제가 쉬워 워크플로 차이를 가릴 수 없었다. `hard` 는 한 번에 맞히기 어려운 성질을 과제마다 하나 이상 넣는다. 인터페이스(이름·시그니처)는 채점을 위해 정확히 주되, 동작 명세에 함정을 둔다.

| 과제 | 종류 | 노리는 것 |
|---|---|---|
| `ledger` | 새 | 여러 모듈(파서·장부·보고서), 돈 계산(Decimal, 통화별 자릿수, 반올림 시점), 충돌하는 요구의 우선순위(합계 = 표시된 값의 합) |
| `csvq` | 새 | CSV 위 작은 질의 언어. 모호하기 쉬운 규칙을 명세로 고정: SQL 3값 논리(빈 값 = NULL, `!=`·`NOT` 도 거짓), NULL 은 정렬 방향과 상관없이 마지막, `007` 이 있으면 텍스트 열, 출력은 원문 그대로 |
| `ratelimit` | 새 | 동시성: 스레드 여러 개가 동시에 불러도 정확히 허용 수만큼만, 키별 리미터를 한 번만 생성, 시계가 거꾸로 가도 토큰 불변, 창 경계 |
| `ttlcache` | 새 | 성능: 모든 연산 상각 O(1)(10만 항목·30만 연산 시간 제한), 만료 경계, 가득 찼을 때 만료 항목부터 제거, `in` 은 사용으로 치지 않음, memoize 의 kwargs 순서 |
| `stock-reservations` | 기존 | 더 큰 기존 프로젝트(재고 관리, 모듈 5개·자체 테스트)에 예약 기능 추가: 출고가 예약을 존중, 조정은 막지 않음, 만료 경계, 저장 형식 v1 읽기·v2 쓰기·v3 거부, 목록 출력 열 추가 |

- 성능·동시성 테스트는 환경에 덜 민감하게: 시간 제한은 참조 구현의 5배 이상(실측 참조 0.1초대, 제한 6초), 경쟁 조건은 `sys.setswitchinterval` 을 줄이고 Barrier 로 동시에 출발시켜 드러낸다. 성능 테스트는 연산 도중에도 시간을 봐서 제한을 넘으면 바로 실패한다(느린 구현이 채점 시간 제한 300초를 다 써서 다른 테스트까지 0점이 되지 않게).
- 한계(실측): 일부러 틀린 구현으로 확인했을 때, 키별 리미터 중복 생성 경쟁은 매번 잡히지만, 버킷·창 하나 안의 경쟁(락 없이 확인 후 차감)은 CPython GIL 아래에서 바깥에서 거의 드러나지 않아 숨은 테스트로 확실히 잡지 못한다. 그 밖의 함정(반올림 방식, TOTAL 규칙, NULL 3값 논리·정렬, 만료 경계, 출고가 예약 무시, O(n) 만료 처리)은 모두 잡혔다.
- 참조 구현(`tests/bench_refs/`)으로 숨은 테스트를 검증하는 규칙은 `basic` 과 같다. 기존 프로젝트 과제는 시작 코드가 새 테스트만 실패하고 기존 테스트·자체 테스트는 통과하는지도 검증한다.

---

### 20.6 분쟁 과제 묶음 (`dispute`, M19 와 함께 추가)
중재자(23장)와 요구 근거 관문(25.2)을 재려면 리뷰가 핑퐁하는 과제가 필요하다. hard 묶음은 거의 수렴해 중재자가 불리지 않았다.
- `todo`: JSON 파일 할 일 도구. "동시 접근은 고려하지 않는다"고 적었지만 원자적 쓰기·잠금·권한·백업을 요구하기 쉽다.
- `deepmerge`: 설정 깊은 병합·삭제 표시·점 경로. 값 종류를 JSON 으로 한정했지만 순환 참조·키 이스케이프·튜플을 요구하기 쉽다.
- `duration`: 엄격한 기간 파서. 대문자·공백·소수를 **거부하라**고 적었다. "관대하게 받자"는 요구를 받아들이면 숨은 테스트가 떨어진다(요구 밖 지적이 품질을 해치는 함정).
- 모두 `max_review = 4`(수렴 실패 → 중재 → 마지막 라운드까지 갈 여유). 숨은 테스트는 참조 구현으로 검증한다.

## 21. 리뷰 비용 줄이기

실측(hard 벤치마크): 두 워크플로 모두 비용의 큰 부분이 수정 라운드(구현자 + 리뷰어 재호출)다. 리뷰 한 번은 입력 12만~21만 토큰이었고 대부분 리뷰어가 도구 호출마다 다시 입력하는 문맥이다(17장). 그래서 (1) 리뷰어가 파일을 덜 열게, (2) 출력을 짧고 기계적으로, (3) 2라운드부터는 이미 읽은 것을 다시 읽지 않게 한다. 세 가지 모두 설정으로 끌 수 있어 벤치마크로 전후를 비교한다.

### 21.1 리뷰 패킷 (`review_packet = true`)
- 리뷰 프롬프트의 diff 를 **함수 단위 문맥**(`git diff --function-context`)으로 만든다. 크기 제한(`max_diff_chars`)을 넘으면 예전 diff 로 돌아간다.
- **바뀐 함수의 호출부**: diff 의 추가 줄과 hunk 머리에서 `def`/`class`/`function`/`fn`/`func` 로 정의된 이름을 모으고(최대 15개), `git grep -n -w` 로 작업 폴더에서 쓰이는 곳을 찾는다(정의 줄 제외, 이름당 5개, 전체 40줄). `[바뀐 함수가 쓰이는 곳]` 섹션.
- 안내 문구: "파일을 직접 열어봐도 된다" 대신 "패킷(diff·호출부·duet 테스트 결과)으로 먼저 판단하고, 패킷만으로 판단할 수 없을 때만 필요한 파일을 열어라. diff 에 이미 있는 내용은 다시 열지 마라".
- 템플릿 플레이스홀더 `{call_sites}`, `{read_instruction}`.

### 21.2 JSON 판정 (`review_json = true`)
- 리뷰 호출에 출력 스키마를 붙인다: Claude `--json-schema '<스키마>'`(결과 JSON 의 `structured_output`, 없으면 본문을 JSON 으로 읽음), Codex `--output-schema <파일>`(마지막 메시지를 JSON 으로 읽음).
- 스키마(엄격 모드 호환: 모든 필드 필수, 추가 필드 없음): `status`(APPROVED|CHANGES_REQUESTED), `summary`, `findings[]`(`severity` P1|P2|P3, `new`, `title`, `file`, `detail`, `evidence`), `repro[]`(`path`, `content`). 엄격 모드가 받지 않을 수 있는 `maxItems` 같은 키워드는 쓰지 않고, 개수 제한(지적 12개, 재현 5개)은 안내 문구와 변환 때 자르기로 지킨다.
- duet 는 JSON 을 **기존 마크다운 리뷰 형식으로 바꿔** 기록한다(`## 반드시 고칠 것`/`## 고치면 좋은 것`, `<repro>` 블록, `<verdict>` 블록에 심각도별 개수). 그래서 판정 파싱·수정 프롬프트·재현 테스트·UI 는 그대로다. JSON 을 못 읽으면 본문을 예전 방식으로 읽는다(`parse_failed` 규칙 그대로).
- 템플릿의 출력 형식 안내는 `{output_instruction}` 으로 바뀐다(JSON 이면 스키마 안내, 아니면 예전 문구). 이 플레이스홀더가 없는 사용자 템플릿이면 그 호출은 JSON 을 쓰지 않는다.

### 21.3 리뷰어 세션 재사용 (`review_session = "reuse"`, 선택. 기본은 `"fresh"`)
- **기본값은 `fresh`(매번 새 세션)** 이다. 아래 실측에서 이어간 리뷰가 구독 한도 기준으로 오히려 비쌌기 때문이다. `reuse` 는 설정으로 켤 수 있게 남겨 둔다.
- `reuse` 일 때: 2라운드부터 같은 역할의 리뷰어는 **자기** 직전 세션을 이어간다(구현자 세션은 절대 쓰지 않는다). 한 세션은 최대 `review_session_rounds` 라운드까지 쓰고 그다음엔 새 세션.
- Claude: 첫 리뷰 결과 JSON 의 `session_id` 를 저장하고 `claude -p --resume <id>`. Codex: 호출 직후 세션 기록에서 그 세션 id 를 찾아 저장하고 `codex exec resume <id>`. Codex 의 resume 에는 `--sandbox`·`-C` 가 없으므로 `-c sandbox_mode="read-only"` 로 읽기 전용을 유지한다.
- 이어간 리뷰의 프롬프트는 짧은 `rereview` 템플릿: 그 라운드의 diff(`review_diff` 설정을 따름, 기본은 지난 리뷰 이후만)·호출부·테스트 결과·자기 지난 '반드시 고칠 것' 목록과 "이전 지적이 해결됐는지 먼저 확인"만. 과제·설계·심각도 규칙은 세션에 이미 있다.
- 이어가기가 실패하면(한도·시간 초과가 아닌 비정상 종료) 어댑터가 같은 호출을 **새 세션 + 전체 프롬프트**로 한 번 다시 한다. 기록 메타에 `resumed`, `session_id`.
- 사용량: Codex 세션 기록의 토큰 수는 세션 누적값이라, 이어간 호출은 호출 시작 전 마지막 누적값과의 차이로 계산한다. Claude 의 `total_cost_usd` 는 이어간 세션에서 누적일 수 있다(참고값).
- `run.json` 의 `review_sessions`: 역할 → {id, round, rounds}.

#### 실측 (2026-10-16, hard 벤치마크 10회, reuse 로 측정)
- 이어간 리뷰 3번 모두 세션을 실제로 이어갔고, 입력 토큰은 이어가지 않은 2라운드 리뷰(M15 전) 평균 10만 → 3.7만(-63%), 출력 711 → 163.
- 그러나 **이어간 턴의 캐시 입력은 0** 이었다(Codex 세션 기록의 `last_token_usage.cached_input_tokens` 로 확인). 같은 세션의 첫 리뷰 안에서는 뒤 턴이 캐시를 썼는데, 프로세스를 새로 띄워 세션을 다시 불러온 턴은 캐시를 못 썼다.
- Codex 요금표 비율(미캐시 입력 1, 캐시 0.1, 출력 5)로 환산하면 2라운드 리뷰는 30.2k → 37.9k(+26%): 새 세션 리뷰는 입력이 많아도 대부분(약 8만) 캐시라 미캐시가 1.8만인데, 이어간 리뷰는 3.7만이 모두 미캐시.
- **원인(후속 실험, 2026-10-16)**: 짧은 fresh → resume 호출 쌍으로 조건을 바꿔 본 결과, Codex 는 **추론 강도가 바뀌면 대화 캐시가 깨진다**. 같은 강도(high→high)면 이어간 턴 입력 19,018 중 18,432(97%)가 캐시, 출력 스키마를 붙여도 97%, 읽기 전용 덮어쓰기 유무와 무관. high→medium 이면 기본 시스템 프롬프트(12,288)만 캐시, duet 처럼 강도 변경 + 스키마면 **0**. Claude 는 같은 강도·스키마·강도 변경 모두 이어간 호출이 캐시를 썼다(읽기 약 2.5만).
- **수정**: 이어간 리뷰는 세션을 연 리뷰의 추론 강도를 그대로 쓴다(`review_sessions[역할].effort`, 요청의 `effort` 로 덮어씀). `rereview` 단계 강도는 새 세션으로 하는 재리뷰에만 쓰인다.
- **수정 후 실측 (2026-10-16, hard 벤치마크 10회, reuse)**: 이어간 재리뷰 2번(같은 실행)이 각각 91%·93% 캐시(평균 입력 10.0만 중 9.3만), 강도 high 유지. 재리뷰 한도 환산 27.7k — 새 세션 재리뷰(M15 전) 30.2k 보다 약 8% 쌈, 캐시가 깨졌던 때(37.9k)보다 27% 쌈. 절약이 작은 이유: 첫 리뷰 강도(high)를 유지해 출력(추론)이 늘었고(1.6k~2.8k, 한도에서 5배), 세션을 이어갈수록 문맥이 쌓인다(3라운드 입력 13만). 실행당 Codex 한도 환산은 M15 전 114.5k → M15 fresh 105.4k → 이번 99.4k(누적 −13%), 숨은 테스트는 10/10 모두 통과.
- **결론**: 이득(재리뷰 약 −8%, 표본 2개)이 작고 새 세션 리뷰가 리뷰어 독립성에 낫다는 점에서 **기본값은 `fresh` 를 유지**하고, `reuse` 는 설정으로 쓸 수 있게 둔다.
- 같은 측정에서 패킷·JSON 판정은 첫 리뷰 입력 14.1만 → 9.6만(-32%), 출력 2,114 → 1,371(-35%), 한도 환산 49.3k → 45.2k(-8%). 실행당 Codex 한도 환산은 -8%, 품질(숨은 테스트)은 같았다(classic·lean 모두 100%). Claude 리뷰어 경로(Claude 의 JSON 판정·이어가기)는 이 벤치마크에서 쓰이지 않아 실제로 확인하지 못했다.

---

## 22. 지적 원장과 판정 관문 (`review_gates = true`)

반려 핑퐁(16장)은 지금까지 프롬프트로 부탁만 했다("극단 사례로 반려하지 마라", `[새]` 표시). 조사(로컬 보고서)에서 반복 리뷰의 지적 수가 15,8,12,2,8,… 로 오르내린 원인은 고친 곳이 새 문제를 만들고 리뷰 범위가 라운드마다 넓어지는 것이었다. 그래서 JSON 판정(21.2)의 구조를 이용해 duet 가 **기계적으로** 판정을 보정한다. 텍스트 판정이거나 `review_gates = false` 면 예전 그대로.

### 22.1 지적 원장
- 리뷰어(역할)마다 지적을 `I1, I2, …` 같은 안정 ID 로 관리한다(실행 전체에서 번호는 하나로 이어진다). `run.json` 의 `issues`: `{id, role, round, severity, title, file, status: open|resolved, closed_round, note}`.
- 리뷰 프롬프트에 그 리뷰어의 **열린 지적 목록**(`{open_issues}`)을 넣고, 스키마의 지적마다 `id` 를 받는다: 아직 해결되지 않은 기존 지적은 그 ID, 새 지적은 빈 문자열.
- 리뷰가 끝나면: 다시 적힌 기존 ID 는 열린 채로(심각도·제목 갱신), 적히지 않은 그 리뷰어의 열린 지적은 `resolved`, 모르는 ID 나 빈 ID 는 새 번호를 붙인다.
- 렌더한 리뷰에 `[I3]` 처럼 ID 를 보여 주고, 수정 프롬프트·IMPLEMENTATION_NOTES 반론에서 ID 로 가리킬 수 있게 한다.

### 22.2 판정 관문 (막는 지적만 대상, 강등은 P3 로)
1. **근거 관문**: P1·P2 인데 `evidence`(실패하는 입력·코드 위치·실행 결과)가 비었으면 P3 로 강등. "근거 없음".
2. **범위 관문**(2라운드부터): **새** P2 지적(기존 ID 가 아님)의 `file` 이 지난 리뷰 이후 바뀐 파일(`git diff --name-only <지난 리뷰 시점> HEAD`)에 없으면 P3 로 강등. "이번 변경과 무관한 새 지적". 파일이 비어 있으면 판단할 수 없으므로 강등. **P1 은 강등하지 않는다**(핵심 결함·보안은 언제든 막는다). 기존 ID 의 지적은 범위와 상관없이 그대로.
- 강등하면 렌더한 리뷰에 `(원래 P2: 근거 없음)` 처럼 적고, `review_history` 에 `downgraded`(관문별 개수)를 남긴다. 판정(`<verdict>` 의 심각도 개수, 막는 지적 수, 수렴 감지)은 강등 **후** 값으로 한다.
- 리뷰어의 `status` 는 참고만 한다(심각도가 있으면 duet 가 개수로 판단하는 16장 규칙 그대로).

### 22.3 실측 (2026-10-16, hard 벤치마크 10회)
- 관문으로 낮춘 지적은 **0개**였다: 리뷰어가 막는 지적마다 근거를 적었고, 2라운드 이후의 새 지적도 모두 지난 리뷰 이후 바뀐 파일에 대한 것이었다. 오탐(정당한 지적을 낮춤)도 없었지만 효과는 아직 재지 못했다.
- 원장은 유일한 미승인 실행(ratelimit × lean, 막는 지적 1 → 1 → 2 로 수렴 실패)의 원인을 보여 줬다: I1 은 2라운드에 해결, I2 는 I1 을 고치다 생긴 회귀, I3 은 과제에 없는 요구(Python 3.8·3.9 호환)였다. 숨은 테스트는 26/26 통과. I3 처럼 **고친 파일 안에서 나온 명세 밖 지적**은 범위 관문으로 거르지 못한다 — 다음 후보: 요구사항(설계 문서)에 없는 지적은 P3 로 보도록 리뷰 기준을 고정하거나 분쟁 중재자를 둔다.

### 22.4 하지 않은 것
- 재현 테스트가 통과(재현 안 됨)하면 그 지적을 강등하는 관문: 재현 테스트는 프로젝트 테스트 명령으로 한꺼번에 돌아 지적별 결과를 가를 수 없어 이번에는 넣지 않았다. 결과는 지금처럼 수정 프롬프트에 "재현 안 됨"으로 전달된다.
- 분쟁 중재자(구현자 반론 vs 리뷰어 재주장)는 다음 후보.

---

## 23. 분쟁 중재자 (`arbiter = true`)

수렴 실패(16장)로 멈추면 지금까지는 사람에게 넘겼다. 실측(22.3)의 ratelimit × lean 처럼 리뷰어가 요구사항 밖 지적으로 막고 숨은 테스트는 다 통과하는 경우가 있다. 그래서 구현자·리뷰어와 별개의 **중재자**가 열린 지적을 한 번 정리한다. 조사의 근거: 최종 결정권 역할을 명확히 두면 성공률 +9.4%(MAST), 리뷰와 판정의 분리.

### 23.1 언제
- build 에서 승인되지 않은 라운드 뒤 (a) 수렴 실패로 멈추려 할 때, 또는 (b) `max_review` 에 닿아 끝나려 할 때.
- 실행당 **한 번만**. 지적 원장(22.1)의 열린 지적이 있어야 한다(JSON 판정 + `review_gates`). 없으면 예전처럼 멈춘다.

### 23.2 누가
- build 단계의 `arbiter` 역할(워크플로 템플릿, 구현자와 같은 역할은 안 됨) → 없으면 debate 의 `synth` 역할 → 없으면 첫 리뷰어. classic 은 설정의 `synth`.
- 새 세션, 읽기 전용, 단계 이름 `arbitrate`(추론 강도 키도 `arbitrate`, Codex 기본 `high`).
- 한계: 두 모델뿐이라 중재자는 구현자나 리뷰어와 같은 모델일 수 있다(새 세션이라 그 대화는 보지 않는다).

### 23.3 무엇을 보고 무엇을 정하나
- 입력: 과제, 설계 문서(`{design_doc}`), 열린 지적(ID·심각도·리뷰어·처음 나온 라운드), 구현자 반론(IMPLEMENTATION_NOTES.md), duet 테스트·재현 테스트 결과, 변경 파일 요약.
- 출력(JSON, 엄격 모드 호환): `decisions[]`(`id`, `decision`: uphold|downgrade|reject, `reason`), `summary`.
  - uphold: 과제·설계의 요구를 어기거나 근거가 분명한 실제 결함 → 반드시 고침.
  - downgrade: 일리는 있지만 요구사항 밖이거나 드문 경우 → P3 로 낮추고 지금은 받아들임.
  - reject: 근거가 틀렸거나 이미 해결됨 → 원장에서 닫음(`status: rejected`).
  - 기준: 과제·설계에 적힌 요구. 적히지 않은 요구(명시하지 않은 버전 호환, 취향)는 uphold 하지 않는다. 결정이 빠진 지적은 uphold 로 본다(보수적).
- 결정은 원장에 적용하고 기록 `NN_arbiter{i}_<역할>.md`, `run.json` 의 `arbiter`(라운드, 파일, 결정, 결과)에 남긴다.

### 23.4 결정 뒤
- 막는 지적(유지된 것 중 `block_severity` 이상)이 없고 duet 테스트가 실패하지 않았으면 → **승인**(`done_approved`, `stop_reason: "arbiter"`). 로그·UI·벤치마크에 "중재로 승인"으로 구분해 보인다.
- 막는 지적이 남았거나 테스트가 실패하면:
  - (a) 수렴 실패이고 라운드가 남았으면 → **마지막 라운드 한 번**: 수정 프롬프트 앞에 중재 결과를 붙이고, 리뷰는 유지된 지적만 막는 기준으로 본다(그 밖의 지적은 새 것이든 완화된 것이든 P3 로 강등, "중재 후"). 그래도 승인되지 않으면 `stop_reason: "arbiter_final"` 로 멈춘다.
  - (b) 상한에 닿았으면 → 추가 라운드 없이 `done_unapproved`, `stop_reason: "arbiter_upheld"`(남은 지적을 로그에 보여 줌). `--max-review` 는 사람이 정한 예산이라 중재자가 넘기지 않는다.
- 중재 JSON 을 못 읽으면 모두 유지로 보고 예전처럼 멈춘다.

### 23.5 위험과 측정
- 중재자가 쉽게 승인하면 결함이 통과한다. 벤치마크의 "승인했는데 숨은 테스트 실패"(20.4)와 "중재 승인" 수를 함께 본다. `arbiter = false`(또는 `duet run --no-arbiter`)로 끌 수 있다.
- `arbiter_final` 로 일찍 멈춘 실행은 수렴 실패(`stalled`)처럼 `--max-review` 없이 이어갈 수 있다. 중재는 실행당 한 번이라 이어가도 다시 부르지 않는다.
- mock 중재자는 기본으로 모두 유지(보수적)해, mock 실행의 미승인 흐름이 예전과 같다(멈춘 이유만 `arbiter_upheld`).

---

## 24. 안전장치

### 24.1 테스트 변조 감지 (`test_guard = true`)
구현자가 테스트를 지우거나 약하게 고쳐서 통과시키면 duet 테스트(16장)도, 리뷰어도 놓칠 수 있다. 리뷰어는 diff 를 보지만 테스트 파일의 변경은 코드 변경 속에 묻힌다.
- 시점: 각 라운드의 구현 커밋 뒤, 리뷰 전. 비교 기준은 1라운드면 기준 커밋(`base_commit`), 2라운드부터 지난 리뷰 시점의 커밋.
- 테스트 파일: `tests/`·`test/` 아래 파일, `test_*.py`, `*_test.py`, `*_test.go`, `*.test.*`·`*.spec.*`, `conftest.py`.
- 신호(`testguard.scan`):
  - 테스트 파일 삭제
  - 파일별 테스트 수 감소(`def test_`, `func Test`, `it(`/`test(`)
  - 파일별 단언 수 감소(`assert`, `self.assert*`, `expect(`, `t.Error`/`t.Fatal`)
  - 건너뛰기 추가(`pytest.mark.skip`, `skipif`, `xfail`, `pytest.skip(`, `unittest.skip`, `.skip(`, `t.Skip`)
  - 테스트 설정 변경: `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml` 의 pytest 관련 줄(`addopts`, `testpaths`, `deselect`, `ignore`, `norecursedirs`, `collect_ignore`), `package.json` 의 test 스크립트, `duet.toml` 의 `test_command`
  - 리뷰어 재현 테스트(17장)를 지우거나 고침
- 결과는 막지 않고 **경고**만 한다. 재현 테스트가 틀렸으면 고치거나 지우라고 안내하고 있고(17장), 리팩터링으로 테스트 수가 줄 수도 있다. 대신:
  - 리뷰·중재 프롬프트의 테스트 결과 아래에 "테스트 변경 경고"를 붙인다: 통과시키려고 테스트를 지우거나 약하게 만든 것이면 [P1], IMPLEMENTATION_NOTES.md 에 타당한 이유가 있으면 지적하지 않는다.
  - 로그 "⚠️ 테스트 변경 감지 N건", `run.json` 의 `test_guard[i]`(신호 목록), 기록 `NN_testguard{i}.md`(agent: duet).
- 끄기: `test_guard = false`.

### 24.2 관리자 UI 의 교차 출처 차단
13장의 토큰 헤더(`X-Duet-Token`)는 커스텀 헤더라 교차 출처 요청이 사전 요청(preflight)에서 이미 막힌다. 여기에 이중 장치를 더한다.
- 요청에 `Origin` 헤더가 있으면 그 호스트가 허용 Host(루프백 바인딩이면 `127.0.0.1:포트` 등, 외부 바인딩이면 요청의 Host)와 같아야 한다. 다르면(`null` 포함) 403.
- `/api/` 요청에 `Sec-Fetch-Site: cross-site` 또는 `same-site` 가 오면 403(`same-origin`, `none` 만 허용). 페이지 열기(`/`)는 터미널·다른 앱에서 링크로 여는 경우가 있어 검사하지 않는다(토큰으로 보호).

### 24.3 Claude 구현자 샌드박스 (`claude_sandbox = false`, 선택)
Codex 구현자는 `--sandbox workspace-write` 로 작업 폴더 밖에 쓰지 못하지만, Claude 구현자는 `Bash` 를 제한 없이 쓴다.
- 켜면 Claude 의 **쓰기 모드** 호출에 `--settings '{"sandbox":{"enabled":true,"autoAllowBashIfSandboxed":true,"allowUnsandboxedCommands":false}}'` 를 붙인다(Claude Code 의 OS 샌드박스: Linux 는 bubblewrap·socat, macOS 는 Seatbelt). 설정 JSON 은 `[claude] sandbox_settings` 로 바꿀 수 있다.
- 읽기 모드(리뷰·중재)는 이미 `Read,Glob,Grep` 만 허용하므로 바꾸지 않는다.
- `duet doctor` 는 켜져 있을 때 Linux 에서 `bwrap`·`socat` 이 있는지 본다. 없으면 경고(Claude Code 가 샌드박스를 못 켜면 명령이 막히거나 실패한다).
- 기본이 꺼진 이유: 환경마다 동작이 다르고(WSL1 미지원, socat 필요), duet 가 실제 실행으로 검증하지 않았다. CLI `--claude-sandbox`.

### 24.4 하지 않은 것
- duet 가 직접 돌리는 테스트(16장)는 샌드박스 없이 에이전트가 쓴 코드를 실행한다. 24.3 은 에이전트가 직접 내리는 명령만 가둔다. 신뢰할 수 없는 과제는 컨테이너나 별도 계정에서 돌려라.
- 테스트 변조를 자동으로 되돌리거나 승인을 막지는 않는다(정당한 변경과 구별할 수 없다).

---

## 25. 한도 대기 후 자동 재개와 요구 근거 관문

### 25.1 한도에 걸리면 기다렸다가 이어가기 (`wait_on_limit = false`, 선택)
지금은 한도에 걸리면 `stopped_rate_limit` 으로 멈추고 사람이 `duet resume` 한다. 밤새 돌리는 실행·벤치마크는 그 뒤로 쉬게 된다.
- 켜면(`--wait-on-limit`, run·bench 둘 다) 한도에 걸린 호출을 실패로 끝내지 않고, 재개 시각까지 기다린 뒤 **같은 호출을 다시** 보낸다. 상태는 `running` 그대로, `run.json` 의 `limit_wait`(에이전트·단계·재개 시각·근거)에 대기 중임을 남긴다.
- 재개 시각(`limits.reset_after`)은 순서대로:
  1. 실패 응답·stderr 의 시각: 초 단위 시각(`|1760000000`, `resets_at: 1760000000`), 상대 시간(`try again in 2 hours 5 minutes`, `resets in 37m`), 시계 시각(`resets 3pm (Asia/Seoul)`, `try again at 3:05 PM`, 지나간 시각이면 다음 날).
  2. Codex 사용량 기록의 `rate_limits`(사용률 100% 인 창의 `resets_at`).
  3. 못 찾으면 백오프: 15분 → 30분 → 60분 → 이후 60분마다 다시 시도. 아직 한도면 다시 기다린다.
  - 재개 시각에 1분 여유를 더한다.
- 대기 예산 `limit_wait_max_min`(기본 360분, 실행 전체 합). 다 쓰면 예전처럼 `stopped_rate_limit` 으로 멈춘다. 기다린 시간은 `run.json` 의 `limit_waited_sec`.
- 기다리는 동안 Ctrl-C·UI 중지는 그대로 `stopped_user`. 한도 재시도는 `retries`(일반 실패 재시도)에 세지 않는다.
- UI 는 "한도 대기 중: HH:MM 재시도", 벤치마크는 실행별 대기 시간을 따로 적어 소요 시간 비교에서 뺄 수 있게 한다.
- 기본이 꺼진 이유: 사람이 모르는 사이 구독 한도를 다시 쓰기 시작한다.

### 25.2 요구 근거 관문 (`spec_gate = true`, JSON 판정 + `review_gates` 일 때)
실측(22.3, 23장)에서 수렴 실패는 과제에 없는 요구(명시하지 않은 버전 호환 등)를 막는 지적으로 내서 생겼다. 중재자(23장)는 사후 처리라, 지적을 낼 때부터 근거 종류를 밝히게 한다.
- 리뷰 JSON 지적에 두 필드를 더한다(엄격 모드 호환, 모두 필수):
  - `basis`: `requirement`(과제·설계 문서의 요구를 어김) | `correctness`(요구와 무관하게 틀린 동작: 크래시·잘못된 결과·데이터 손실) | `security` | `preference`(취향·스타일·과한 방어)
  - `requirement`: basis 가 requirement 면 과제·설계 문서의 해당 문장을 **그대로 인용**, 아니면 빈 문자열.
- 관문(22.2 의 관문들 뒤에 적용, 막는 지적만):
  - `preference` 인 P1·P2 → P3 ("취향").
  - `requirement` 인 P2 의 인용이 비었거나, 과제·설계 문서(공백·구두점·대소문자 무시, `...` 로 나눈 조각마다)에서 찾을 수 없으면 → P3 ("인용한 요구를 찾지 못함"). P1 은 그대로(범위 관문과 같은 원칙).
  - `correctness`·`security` 는 근거 관문(evidence)만 받는다.
- 원장(22.1)에 `basis`·`requirement` 를 남기고, 중재 프롬프트의 열린 지적에도 보인다. 렌더한 리뷰에는 "요구: «인용»" 줄.
- basis 가 없는 응답(예전 스키마, 사용자 템플릿)은 correctness 로 본다. 끄기: `spec_gate = false`.
- 위험: 요구를 말로 풀어 쓴 정당한 지적이 P3 로 떨어질 수 있다. 벤치마크의 숨은 테스트 통과율과 관문별 강등 수(`downgraded.spec`)로 본다.

### 25.3 실측 (2026-10-17, css53, 실제 Claude·Codex 22회, 모두 `--wait-on-limit`)
| 묶음 | 설정 | 승인 | 숨은 테스트 | 평균 라운드 (classic / lean) | 실행당 Codex 한도 환산 (classic / lean) |
|---|---|---|---|---|---|
| hard 10회 | M15 fresh (비교 기준) | 10/10 | 100% | 1.2 / 1.4 | 151.9k / 59.0k |
| hard 10회 | M19 기본 | 10/10 | 100% | 1.0 / 1.4 | 143.8k / 55.0k |
| dispute 6회 | M19 기본 | 6/6 | 100% | 1.0 / 1.0 | 103.9k / 32.0k |
| dispute 6회 | 요구 근거 관문 끔 | 6/6 | 100% | 1.0 / 1.0 | 107.5k / 30.3k |
- **수렴 실패 0건 → 중재자는 한 번도 불리지 않았다.** dispute 묶음은 6+6회 모두 1라운드에 지적 0개로 승인했다. 명세에 "동시 접근은 고려하지 않는다", "엄격하게 거부한다"처럼 범위를 적어 두니 리뷰어가 요구 밖 지적을 내지 않았다. 중재자(23장)의 효과는 여전히 실제 모델로 재지 못했다(mock 으로만 검증).
- 요구 근거 관문: 강등 0건. hard 에서 리뷰어가 낸 basis=requirement 지적 3개(P2 2, P3 1)는 모두 명세 원문을 정확히 인용했고(굵게 표시 `**` 포함), 관문을 통과해 P2 로 남았다. 두 P2(시계 역행 중 토큰 차감, 역환율 근사 오차)는 실제 결함이었고 다음 라운드에 고쳐졌다. 정당한 지적을 잘못 떨어뜨린 경우는 없었다.
- 품질·비용: 숨은 테스트 100% 유지. Codex 한도 환산은 M15 fresh 대비 −5%(classic)·−7%(lean)로, 지적 필드 두 개를 더한 비용 증가는 보이지 않는다(차이는 실행 간 편차 수준). 관문을 끈 B 와도 차이가 없다.
- 한도 대기·테스트 변경 경고: 이번 실행에서는 한 번도 일어나지 않았다(한도에 걸리지 않았고, 구현자가 테스트를 약하게 만들지 않았다).
- 결론: 기본값 유지(`spec_gate = true`, `arbiter = true`, `wait_on_limit = false`). 지금 모델 조합은 명세가 분명하면 거의 1~2라운드에 수렴해, 중재자·요구 근거 관문은 드문 경우의 안전장치로 남는다. 중재자를 재려면 일부러 모호한 명세나 서로 충돌하는 요구가 필요하다.

---

## 26. 재제기·되돌림 방지 ("이거 전에 해봤던 거잖아")

리뷰어는 라운드마다 새 세션이라(21.3) 지난 라운드를 기억하지 못한다. 원장(22.1)은 열린 지적만 다음 리뷰에 보여 주고, 해결·기각된 지적과 새 지적이 같은지는 비교하지 않는다. 그래서 (1) 이미 해결됐거나 중재에서 기각된 지적을 다른 말로 다시 내면 새 ID 로 다시 막고, (2) 구현자가 다른 지적을 피하려고 예전 코드로 되돌려 해결됐던 결함을 되살려도 알아채지 못한다.

### 26.1 닫힌 지적 보여 주기 (모델 호출 없음)
- 리뷰 프롬프트의 `{open_issues}` 아래에 "[이미 닫힌 지적]"을 붙인다: 해결된 것(몇 라운드에 해결), 중재에서 기각·완화된 것(이유). "회귀가 아니면 다시 내지 마라. 회귀면 그 ID 를 적어라".
- 수정 프롬프트의 지난 리뷰 아래에도 같은 목록: "이 지적들을 고친 코드를 되돌리지 마라".
- 최근 것부터 최대 15개. 원장이 없으면(JSON 판정·관문을 끈 실행) 붙이지 않는다.

### 26.2 재제기 확인 (`dedup = true`, 작은 모델)
- 언제: 리뷰 JSON 을 관문(22.2)까지 보정한 뒤, **새로 붙은 막는 지적(P1·P2)** 이 있고 닫힌 지적(해결·기각·중재 완화)이 있을 때만. 둘 중 하나라도 없으면 부르지 않는다.
- 누가: 역할 이름 `dedup`. 워크플로에 `dedup` 역할이 있으면 그것, 없으면 설정 `dedup_agent`(기본 `claude`)·`dedup_model`(기본 `claude-haiku-5-5`)·`dedup_effort`(기본 비움 = CLI 기본). 코드를 읽지 않는 짧은 분류라 작은 모델로 충분하다. Claude 로 둔 이유: 무거운 리뷰를 맡은 Codex 한도를 쓰지 않고, Haiku 는 Claude 한도 소모가 작으며, `--json-schema` 출력이 검증돼 있다.
- 입력: 새 막는 지적(ID·심각도·제목·파일·내용 요약), 닫힌 지적(ID·제목·파일·어떻게 닫혔는지). 출력(JSON, 엄격 모드 호환): `matches[]`(`new_id`, `old_id`, `reason`) — 같은 문제를 다시 말한 것만. 확실하지 않으면 넣지 않는다.
- 적용(목록에 없는 ID 는 버림):
  - 옛 지적이 **기각**되었거나 **중재에서 완화**된 것 → 새 지적을 P3 로 낮춘다("I3 재제기: 중재에서 기각됨"). `downgraded.repeat`.
  - 옛 지적이 **해결**된 것 → 심각도는 그대로 두고 "I3 재발(회귀 가능성)"으로 표시한다(`repeat_of`). 되살아난 결함이면 막아야 한다.
  - 같은 리뷰어가 지난 라운드에 낸 열린 지적을 이번에 ID 없이 다시 쓴 것(그대로 두면 원래 지적은 해결로, 새 지적은 새 ID 로 잡힌다) → 원래 ID 로 합친다("같은 지적"). 확인 대상에 이 '해결될 참인' 지적도 넣는다. 원장의 라운드 추적과 수렴 판단이 흐트러지지 않는다.
- 보정한 판정으로 리뷰 기록을 다시 렌더링해 같은 파일에 덮어쓰고, 판정·원장은 그 결과로 정한다. 확인 기록은 `NN_dedup{i}_<리뷰어>.md`, `run.json` 의 `dedup[i]`.
- 확인자 호출이 실패하면(한도 제외) 건너뛰고 리뷰 결과를 그대로 쓴다. 실행을 실패로 만들지 않는다.
- 끄기: `dedup = false`. mock 확인자는 기본으로 "중복 없음".

### 26.3 되돌림 감지 (모델 호출 없음, git)
- 2라운드부터 리뷰 전에, 지난 리뷰 이후 바뀐 파일마다 이번 변경(diff)의 추가 줄 A·삭제 줄 R 을 이전 시점(기준 커밋, 1..i-2 라운드 리뷰 시점)의 그 파일 내용과 비교한다. 공백·4글자 미만 줄은 뺀다.
- A 의 절반 이상(1줄 이상)이 시점 k 에 있었고, R 의 절반 이상이 시점 k 에 없었으면 "k 시점 코드로 되돌림" 후보. 버그 수정은 1~2줄인 경우가 많아 문턱을 낮게 둔다(경고만 하고, 아래 조건으로 소음을 거른다).
- 경고는 **그 사이에 해결된 지적을 가로지를 때만** 낸다: 같은 파일의 지적 x 가 `x.round ≤ k < x.closed_round` (k 시점 코드에 그 결함이 있었다). 리뷰어가 요청한 정당한 되돌림(불필요한 변경 원복)은 대개 여기에 걸리지 않는다.
- 결과는 테스트 변경 경고(24.1)처럼 리뷰·중재 프롬프트의 테스트 결과 아래에 붙인다. 로그 "⚠️ 되돌림 감지", `run.json` 의 `reverts[i]`.
- 끄기: `dedup = false` 와 함께 꺼진다(26장 전체 스위치).

### 26.4 하지 않은 것
- 토론 단계의 "예전에 버린 설계안 재제안" 확인: 토론은 1라운드가 기본이라 이득이 작다.
- 실제 Haiku 로의 검증: 개발 중에는 실제 모델을 부르지 않는다. 다음 실측 때 함께 본다.

---

## 27. 어디서나 실행 (Windows·Linux·macOS, SSH)

지금까지 duet 는 Linux 에서만 만들고 시험했다. Windows 에서는 `duet ui` 가 시작하자마자 죽고(pty 모듈), `duet doctor` 가 설치된 CLI 를 못 찾으며(`sh -c command -v`), 무엇보다 실행 생존 확인의 `os.kill(pid, 0)` 이 Windows 에서는 **그 프로세스를 강제 종료**한다.

### 27.1 지원 범위
| 어디서 | 지원 |
|---|---|
| 로컬: Linux, macOS | 지금처럼 |
| 로컬: Windows 10/11 (네이티브) | M21 에서 추가. Python 3.11+, git, Claude Code·Codex CLI 의 Windows 판 |
| 원격(SSH): Linux, macOS | 15장 그대로. UI·CLI 가 Windows 에 있어도 된다 |
| 원격(SSH): Windows | 하지 않음(원격 스크립트가 POSIX 셸). WSL 에 duet 를 두고 그 안으로 SSH 하면 된다 |

### 27.2 프로세스 제어 (`procs.py`)
OS 마다 다른 부분을 한곳에 모은다. 나머지 코드는 이 함수만 쓴다.
- **자식 실행 옵션**: POSIX 는 새 세션(`start_new_session`), Windows 는 새 프로세스 그룹 + 콘솔 창 없음(`CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`).
- **트리 종료**: POSIX 는 프로세스 그룹에 SIGTERM → 유예 → SIGKILL. Windows 는 `taskkill /PID <pid> /T /F`(부모-자식 관계로 손자까지).
- **생존 확인**: POSIX 는 `os.kill(pid, 0)` + 좀비 제외. Windows 는 `OpenProcess` + `GetExitCodeProcess`(ctypes, 표준 라이브러리). **Windows 에서 `os.kill` 은 쓰지 않는다.**
- **분리 실행**(UI 가 띄우는 duet): Windows 는 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`. UI 를 꺼도 실행은 계속된다.
- **duet 프로세스인지**: Linux 는 `/proc/<pid>/cmdline`, 그 밖에는 잠금 파일을 믿는다(지금처럼).
- **파일 바꿔 쓰기**(잠금·run.json·projects.json·settings.json, 임시 파일 → `os.replace`): Windows 는 다른 프로세스가 대상 파일을 열어 둔 순간 바꾸기를 거부한다(`PermissionError`). UI·시험이 잠금과 run.json 을 자주 읽으므로 그 순간에 걸리면 실행이 죽는다 → Windows 에서는 최대 5초까지 잠깐씩 다시 시도한다(`procs.replace_file`).

### 27.3 중지 요청 파일 (모든 OS)
UI 의 중지는 POSIX 에서 SIGTERM 을 보냈다. Windows 에는 다른 프로세스에 보낼 SIGTERM 이 없고(`TerminateProcess` 는 상태 저장 없이 죽인다), 분리 실행한 프로세스에는 Ctrl-Break 도 닿지 않는다.
- UI 는 `<workdir>/.duet/stop` 파일을 만든다(POSIX 에서는 SIGTERM 도 함께 보낸다 — 지금처럼 빠르게).
- 실행 중인 duet 는 잠금을 잡을 때 이 파일을 감시 대상으로 등록하고, 지우고 시작한다. 에이전트 호출을 기다리는 동안(0.5초마다), 한도 대기 중, 단계 사이에 파일을 보고, 있으면 Ctrl-C 와 같은 경로(KeyboardInterrupt)로 멈춘다: 진행 중인 호출의 프로세스 트리를 정리하고 `stopped_user` 로 저장한다.
- 그래도 20초 안에 멈추지 않으면 UI 가 "강제 종료"를 제안한다(트리 종료, 상태는 `running` 으로 남아 UI 가 '멈춤'으로 보이고 resume 할 수 있다).
- 콘솔에서 돌릴 때 Windows 의 Ctrl-C·Ctrl-Break 도 같은 경로.

### 27.4 실행 파일 찾기와 실행
- `shutil.which` + 보강한 PATH 로 찾는다. 보강 경로: POSIX `~/.local/bin`, `~/.duet/bin`. Windows 는 여기에 `%APPDATA%\npm`(npm 전역 설치), `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin`. Windows 는 PATHEXT(`.exe`, `.cmd`, `.bat`)를 따른다.
- **배치 파일**(npm 이 만드는 `codex.cmd` 등): 인자의 `%`·`^`·`&` 가 cmd.exe 에서 망가지므로 어댑터가 프롬프트를 항상 stdin 으로 넘긴다.
- **파이썬 스크립트**(확장자 없이 `#!...python` 으로 시작): Windows 는 shebang 을 모르므로 지금 인터프리터로 실행한다(테스트의 가짜 CLI, 사용자가 감싼 스크립트).
- 셸 명령(테스트 명령 등): POSIX `sh -c`, Windows `%COMSPEC% /d /s /c`. 자동 감지한 파이썬 테스트 명령은 Windows 에서 `python -m pytest -q`(`python3` 은 Microsoft Store 바로가기일 수 있다).
- 출력·파일은 UTF-8. Windows 에서 `duet` 의 표준 출력이 파일·파이프로 갈 때(UI 가 띄운 실행의 로그) UTF-8 로 쓴다(이모지가 cp949 에서 깨지며 죽지 않게).
- Windows 에서 자식 프로세스 환경에 `PYTHONUTF8=1` 을 넣는다(사용자가 정했으면 그대로). 자식 파이썬(테스트, 채점, 가짜 CLI)의 표준 입출력 기본값이 cp949 라, duet 가 UTF-8 로 넘긴 프롬프트·출력이 깨진다.
- 셸 명령은 Windows 에서 명령줄 문자열로 넘긴다(`subprocess` 의 `shell=True` 와 같은 형태). 목록으로 주면 안쪽 따옴표가 `\"` 로 바뀌는데 cmd.exe 는 그 규칙을 모른다. cmd.exe 는 명령을 못 찾아도 종료 코드 1 이라, 첫 단어를 찾을 수 없으면 테스트 결과를 '실행 불가'로 본다.
- UI 서버는 거절할 요청이라도 본문을 먼저 읽는다(안 읽고 닫으면 Windows 에서 연결이 끊긴다).
- duet 가 새로 만드는 저장소는 `core.autocrlf=false`(에이전트가 쓴 바이트를 그대로 커밋·복원). 기존 저장소의 설정은 건드리지 않는다.

### 27.5 설치·로그인 동작과 UI 터미널
- 동작은 OS 별 명령을 둔다. Windows(PowerShell):
  - Claude Code: `irm https://claude.ai/install.ps1 | iex` (공식)
  - Codex CLI: `npm install -g @openai/codex` (Node.js 필요)
  - uv: `irm https://astral.sh/uv/install.ps1 | iex` (공식)
  - git: `winget install --id Git.Git -e`
  - curl: Windows 10 이상에 들어 있다(설치 동작 없음)
  - 로그인·로그아웃 명령은 같다(`claude auth login`, `codex login --device-auth`).
- UI 터미널: Windows 에는 pty 가 없다. 설치처럼 입력이 필요 없는 동작은 파이프로 돌려 출력을 보여 준다. 로그인처럼 터미널 입력이 필요한 동작은 **새 콘솔 창**을 띄워 거기서 하게 한다(UI 가 같은 PC 에서 돌 때). 원격 머신의 동작은 지금처럼 `ssh -tt`.

### 27.6 Windows 에서의 SSH
- Windows 의 OpenSSH 는 연결 재사용(ControlMaster)을 지원하지 않는다 → Windows 에서는 `ControlMaster`·`ControlPath`·`ControlPersist` 옵션을 빼고 매번 연결한다.
- 원격 쪽은 15장 그대로(POSIX). 배포(duet.pyz)·조회·동작 모두 같은 명령.

### 27.7 시험
- 가짜 CLI 는 지금처럼 파이썬 스크립트. Windows 에서는 27.4 의 규칙으로 실행되고, PATH 로 찾는 시험을 위해 conftest 가 임시 폴더에 `.cmd` 감싸개를 만든다.
- 시험 안의 셸 명령(`false`, `sh -c ...`)은 `sys.executable -c ...` 로 바꾼다. pty 처럼 POSIX 에만 있는 기능의 시험은 Windows 에서 건너뛴다(이유 표시).
- 완료 기준: Windows(개발 노트북)와 Linux(css53) 양쪽에서 전체 시험 통과, Windows 에서 `duet doctor`·`duet ui`·mock 실행·UI 중지를 직접 확인.

---

## 28. 관리자 UI 개편

M7(13장) 이후 기능은 계속 늘었는데 UI 는 그대로였다. 실제로 열어 보고 찾은 문제:
1. 주소에서 `?t=` 가 빠지면(즐겨찾기, 새 탭) "접속 주소가 맞지 않습니다"로 막힌다.
2. 벤치마크 실행마다 프로젝트로 등록돼 카드가 수십 개이고, 이름이 모두 `classic-1`/`lean-1` 이라 구별이 안 된다. 지운 폴더도 계속 보인다.
3. 설정·새 실행 화면에 M12 이후 설정(워크플로, 리뷰 패킷·JSON 판정·관문·중재자·요구 근거·테스트 변조 감지·중복 확인·한도 대기·샌드박스·단계별 강도)이 없다.
4. 실행 상세에서 지적 원장·중재·관문 강등·중복 확인을 볼 수 없다.
5. 벤치마크 결과를 볼 화면이 없다.
6. Claude 는 사용률(%) 없이 토큰 합계만 보인다.

### 28.1 접속 (쿠키)
- 토큰이 맞는 `?t=` 로 처음 열면 쿠키 `duet_t_<포트>`(HttpOnly, SameSite=Strict, Path=/)를 심고, 주소창의 `?t=` 는 지운다(history.replaceState). 다음부터는 쿠키로 페이지가 열린다.
- API 는 지금처럼 `X-Duet-Token` 헤더만 받는다(쿠키로는 API 를 열지 않는다 → 교차 사이트 요청 위조가 막힌 그대로). 페이지가 토큰을 HTML 에 넣어 JS 에 준다.
- 쿠키 이름에 포트를 넣어 같은 PC 의 UI 여러 개가 섞이지 않게 한다(30일).
- 토큰은 `$DUET_HOME/ui-token`(사용자만 읽기)에 두고 `duet ui` 를 다시 켜도 같은 것을 쓴다. 그래서 즐겨찾기·쿠키가 계속 맞는다. `duet ui --new-token` 으로 바꾸면 예전 주소·쿠키는 막힌다.

### 28.2 프로젝트 목록
- **벤치마크 실행은 등록하지 않는다**(`duet bench run` 이 만든 작업 폴더). 이미 등록된 것은 '벤치마크 실행'으로 표시하고 기본으로 숨긴다(작업 폴더의 두 단계 위에 `results.json` 이 있으면 벤치마크 실행).
- **폴더가 없는 프로젝트**는 기본으로 숨기고, "없는 폴더 N개 목록에서 지우기" 버튼을 둔다(파일은 건드리지 않는다).
- 검색칸(이름·경로), 이름이 같은 프로젝트는 부모 폴더 이름을 붙여 구별(`ledger / lean-1`). Windows 경로도 같은 규칙.

### 28.3 벤치마크 화면 (`#/bench`)
- 결과 폴더 목록: `~/.duet/bench/*` 와, 등록된 벤치마크 실행에서 거슬러 찾은 결과 폴더(`--out` 으로 다른 곳에 둔 것). 묶음·워크플로·시작 시각·실행 수.
- 결과 하나: `results.json` 의 요약표(20.4: 승인·숨은 테스트·승인했는데 실패·라운드·시간·토큰)와 실행별 행. 행을 누르면 그 실행 상세로 간다.
- API `GET /api/bench`(목록), `GET /api/bench/result?dir=`(목록에 있는 폴더만 허용).

### 28.4 설정 항목 정의 (`options.py`)
- 설정마다 키·이름·설명·종류(bool/int/choice/text)·고를 값·묶음(실행/리뷰/안전/사용량)·새 실행 창에 보일지를 한곳에 정의한다. 설정 화면과 새 실행 창의 '고급' 칸은 이 정의에서 만든다. 앞으로 설정을 더하면 정의 한 줄로 UI 에 나온다.
- 저장은 지금처럼 `~/.duet/settings.json` 의 `defaults` 에, `load_config` 로 검증한다. 정의에 없는 키는 받지 않는다. 시험이 `Config` 의 필드와 정의를 맞춰 본다(UI 에 빠진 설정이 없게).

### 28.5 실행 상세의 '지적' 탭
- 지적 원장(22.1): ID·리뷰어·심각도·제목·파일·근거 종류·요구 인용·상태(열림/해결/기각)·열린/닫힌 라운드·메모(관문 강등·중재·재제기).
- 라운드별: 관문 강등 수(근거·범위·요구·재제기·중재 후), 중복 확인 결과(26.2), 테스트 변경·되돌림 경고(24.1·26.3).
- 중재(23장): 이유·결정별 목록·결과. 원장이 없는 실행(JSON 판정을 끈 것, 예전 실행)은 탭을 숨긴다.

### 28.6 Claude 사용률
- Claude 어댑터가 `--output-format stream-json --verbose` 로 부르고, 마지막 `result` 이벤트(지금의 JSON 출력과 같은 필드: `result`·`usage`·`structured_output`·`session_id`·`total_cost_usd`)로 결과를 만든다. 2026-10-17 실제 호출 1회로 확인.
- 같은 출력의 `rate_limit_event` 의 `rate_limit_info.unifiedWindows`(`five_hour`·`seven_day` 의 `utilization`(0~1)·`resetsAt`)를 호출 사용량(`usage.rate_limits`)에 남기고 `~/.duet/claude_limits.json` 에 최신값을 쓴다. 사용량 화면은 Codex 처럼 5시간·7일 사용률과 다시 풀리는 시각을 보인다(duet 가 Claude 를 부를 때 갱신).
- 한도 대기(25.1)는 실패 응답의 `rate_limit_event` 의 `resetsAt` 도 재개 시각으로 쓴다.
- 스트림을 못 읽으면(예전 CLI) 지금처럼 JSON 한 덩어리로 읽는다.

### 28.7 하지 않은 것
- 실행 비교 화면(두 실행 나란히), 알림.

### 28.8 디자인 v2 (2026-10-17 사용자 합의)
- **글꼴**: Pretendard Variable(SIL OFL 1.1)을 `static/fonts/` 에 함께 배포한다(약 2MB, 오프라인에서도 같은 모양). 라이선스 전문은 `static/fonts/Pretendard-LICENSE.txt`. 원격 pyz(27장)에는 넣지 않는다.
- **색 테마**: 시스템·화이트·페이퍼·슬레이트·다크·미드나잇. 배경·면·선·글자색만 바뀌고 배치는 같다. 설정 > 화면에서 고르며 브라우저(localStorage)에만 기억한다. 그라데이션은 기본 버튼에만 쓴다(제목 뒤·화면 모서리의 빛은 2026-10-17 사용자 요청으로 없앴다).
- **로고**: Claude·Codex 로고 SVG 를 `static/logos/` 에 함께 배포한다(사용자 요청, 2026-10-17). `$DUET_HOME/logos/claude.svg`·`codex.svg` 가 있으면(300KB 이하) 그것을 쓴다. `GET /logos/<이름>.svg` 는 정해진 두 이름만, `sandbox` CSP. 다크 테마에서 단색 Codex 로고는 반전한다.
- **설명 문구**: 카드의 긴 설명은 제목 옆 ⓘ 툴팁으로 접는다(이미 ? 가 있으면 그 툴팁에 이어 붙인다).
- **입력**: 고르는 칸은 모두 같은 모양의 떠 있는 메뉴(창에 잘리지 않게 화면 기준으로, 아래 자리가 모자라면 위로). 모델은 목록에서만 고른다(직접 입력 없음): '기본 모델'(CLI 기본값, 새 모델이 나오면 CLI 를 따라감) + 버전이 붙은 모델 이름과 한국어 한 줄 설명. Claude 목록은 duet 에 들어 있고(`CLAUDE_MODEL_SUGGESTIONS`), Codex 목록은 Codex CLI 가 갱신하는 `~/.codex/models_cache.json` 에서 읽는다(아는 설명은 한국어로 옮김). 목록에 없는 기존 설정값은 '목록에 없음'으로 보여 준다.
- **작업 폴더 위치**: 설정에 작업 폴더 위치가 없으면 새 실행 창의 위치 칸을 숨기고 전체 경로만 받는다. 있으면 위치를 고르고 폴더 이름만 쓴다('다른 곳 (전체 경로 입력)'도 고를 수 있음).
- **워크플로 설명**: 한 줄, 같은 말투(단계 → 단계). 출처는 내 템플릿·프로젝트 워크플로일 때만 붙인다. 추론 강도와 켬/끔은 분절 버튼.
- **고급 설정**: 칸에는 늘 지금 쓰는 실제 값을 보인다('기본·켬' 같은 선택지 없음). 기준(설정 화면은 duet 내장 기본값, 새 실행 창은 지금 설정값)과 같은 값은 저장하지 않으므로 기준이 바뀌면 따라간다. 기준과 다른 줄은 표시하고 ↺ 로 하나씩, '기본값으로' 로 전부 되돌린다. 설정 API 의 각 항목에 내장 기본값(`default`)을 함께 준다.
- **기록 크게 보기**: 실행 상세의 기록을 가운데 창으로 연다(목록에서 두 번 누르기 또는 '크게 보기'). 짝 기록을 탭으로 함께 보인다: 비판 → 비판 대상(상대의 설계안·개정안), 리뷰 반영 → 반영한 리뷰, 리뷰 → 리뷰한 구현. 요약(제목·항목 첫 줄·단락 첫 문장만, 모델을 부르지 않음)/원문(에이전트가 쓴 전부)을 고를 수 있고 고른 쪽을 기억한다. 페이지를 옮기면 닫힌다.

---

### 28.9 폴더 고르기 (2026-10-17 사용자 요청)
- 경로를 쓰는 칸(새 실행의 작업 폴더, 설정의 작업 폴더 위치, 프로젝트 목록의 '기존 작업 폴더 추가') 옆에 작은 📁 버튼을 둔다. 브라우저는 보안상 폴더의 절대 경로를 페이지에 주지 않으므로 duet 서버가 고른다.
- **네이티브 대화상자**: 고른 머신이 이 머신이고 서버가 대화상자를 띄울 수 있으면(Windows·macOS, 또는 화면(DISPLAY)이 있는 Linux, 그리고 tkinter 가 있음) OS 의 폴더 선택 창을 띄운다(`POST /api/fs/pick`). tkinter 는 표준 라이브러리이며 `duet.runner` 로 띄운 자식 파이썬에서 연다(맨 앞에, 시작 폴더 지정). 취소하면 아무것도 바꾸지 않는다. 서버가 원격(SSH 터널)인 경우 등 대화상자를 볼 수 없는 경우를 위해 아래로 넘어갈 수 있다.
- **duet 폴더 브라우저**(대화상자를 못 쓸 때, 원격 머신, css53 같은 화면 없는 서버): 가운데 창에 폴더 목록(`GET /api/fs/dirs?path=`, 원격 머신이면 SSH 로 그 머신의 duet `_api`). 시작점은 홈·드라이브(Windows)·`/`·설정의 작업 폴더 위치. 하위 폴더만 보이고(점으로 시작하는 폴더는 숨김, 500개까지), 위로 가기·경로 직접 입력·`git`/duet 프로젝트 표시, '이 안에 새 폴더' 이름 칸(작업 폴더는 없으면 duet 가 만든다), '이 폴더 선택'.
- 목록 API 는 토큰이 있어야 하고 폴더 이름만 돌려준다(파일 내용은 읽지 않는다).

## 29. (없앰) 관리자·작업자 분업

2026-10-17 에 `extended` 워크플로(build 단계의 `manager`·`workers`: 관리자가 설계를 파일 소유 단위 작업 묶음으로 나누고, 작업자가 묶음을 순서대로 구현, 관리자가 통합)를 만들었다가 같은 날 없앴다. 이유는 31.4.
- 워크플로 파일의 build 단계에 `manager`·`workers` 를 쓰면 "없앤 기능"이라는 오류를 낸다.
- 그 워크플로로 돌던 예전 실행은 이어할 수 없다(저장된 워크플로를 읽을 수 없다는 오류). 기록 보기는 그대로 된다.

## 30. 단계별 모델·추론 강도 (2026-10-17 사용자 요청)

지금까지 모델은 에이전트(Claude·Codex)마다 하나였고, 추론 강도만 단계별(18.1)이었다. 순서도의 칸(= 어떤 역할이 하는 어떤 단계)마다 모델과 강도를 따로 고를 수 있게 한다. 예: 설계안은 Opus, 구현은 Sonnet, 리뷰는 GPT-6-Astra xhigh.

### 30.1 단계 키
- 18.1 의 단계 키에 `explore`(사전 조사)·`audit`(승인 뒤 감사)를 더한다: `propose`, `critique`, `synthesize`, `implement`, `fix`, `review`, `rereview`, `arbitrate`, `explore`, `audit`. 중복 확인(26장)은 따로 정한다(`dedup_model`·`dedup_effort`).

### 30.2 에이전트 층: `step_model`
- `[claude]`·`[codex]`(와 워크플로 역할 `[roles.<이름>]`)에 `step_model = { 단계 = 모델 }`. `step_effort` 와 같은 규칙이다.
  - 호출의 모델 = `step_model[단계]` → 없으면 `model` → 비어 있으면 모델 플래그 없음(CLI 기본). 리뷰 2라운드부터는 `rereview` 키.
  - **층 규칙**: 한 층이 `model` 만 정하고 `step_model` 을 안 정하면 그 층이 `step_model` 을 비운다(= 모든 단계 같은 모델). 역할도 같다(역할이 `model` 을 정하면 에이전트의 `step_model` 을 물려받지 않는다).
- CLI: `--{agent}-step-model implement=claude-sonnet-5-5,review=claude-opus-5-5`.

### 30.3 칸 단위 지정: `step_overrides`
- 실행마다 역할·단계 단위로 덮어쓴다(가장 우선). 설정 키 `step_overrides = { 역할 = { 단계 = { model = "...", effort = "..." } } }`.
  - classic 의 역할 이름은 `claude`·`codex`, 다른 워크플로는 그 역할 이름(예: secure 의 `security`).
  - 역할의 설정(30.2·18.1 규칙)을 만든 뒤 마지막에 `step_model[단계]`·`step_effort[단계]` 를 바꾼다.
- CLI: `--set-step 역할.단계.model=값`, `--set-step 역할.단계.effort=값` (여러 번). 예: `--set-step claude.implement.model=claude-sonnet-5-5 --set-step codex.review.effort=xhigh`.
- 검증: 단계는 30.1 키, 항목은 `model`·`effort` 만, 값은 모델·강도 이름 형식. 워크플로에 없는 역할이면 실행 시작 때 오류.
- `run.json` config 에 `{agent}.step_model`·`step_overrides` 를 저장해 resume 이 같은 값을 쓴다.

### 30.4 세션 이어가기
- 리뷰어 세션을 이어갈 때(21.3)는 세션을 연 리뷰의 강도처럼 **모델도** 그대로 쓴다(`review_sessions` 에 model 기록). 1회차·2회차부터의 모델이 다르면 이어가지 않고 새 세션으로 연다.

### 30.5 화면
- 새 실행 창 순서도의 칸을 누르면 그 칸의 설정 창이 뜬다: 모델(목록, '위 설정 따름') + 그 칸 단계마다 추론 강도(분절 버튼, '위 설정 따름') + '이 칸 되돌리기'. 바꾼 칸은 테두리로 표시하고, 위 에이전트 카드의 모델·강도는 '나머지 단계 전체'의 값이 된다.
- 칸 하나에 단계가 둘이면(구현·수정, 리뷰 1회차·2회차부터, 설계안·비판) 모델은 함께, 강도는 단계마다 고른다.
- 시작 로그·역할 설명에 단계별 모델을 함께 적는다: `codex(gpt-6.1-sol; review=gpt-6-astra, 강도 medium; review=xhigh)`.

### 30.6 기본 모델 배정: 역할 이름이 아니라 판단량으로 (2026-10-17 사용자 요청)
- 원칙: **판단하고 결정하는 자리**(설계안·비판·종합, 구현·수정, 리뷰, 중재, 감사)는 강한 모델, **찾아오고·옮기고·형식만 맞추는 자리**(사전 조사, 중복 확인)는 가벼운 모델. 디버깅(테스트 실패를 읽고 고치기)은 구현자가 직접 하고, 리뷰는 작성 과정을 보지 않은 다른 에이전트가 한다(지금 구조 그대로). 쉬운 과제는 구현자도 가벼운 모델로 충분하다.
- "똑똑한 모델이 계획하고 싼 모델이 실행" 구조는 실행이 기계적일 때만 통한다. 코딩은 구현 중에 계획에서 안 보이던 문제가 나오므로 구현자를 약하게 두지 않는다(그래서 분업은 없앴다, 31.4).
- 기본값:

  | 자리 | 기본 모델 | 강도 |
  |---|---|---|
  | 설계안·비판·종합·구현·수정·리뷰·중재·감사 | 각 CLI 의 기본 모델(사용자가 고른 주 모델, 보통 가장 강한 것) | Codex: 종합·리뷰·중재 high, 나머지 medium(18.1). Claude: CLI 기본 |
  | 사전 조사(`existing` 의 scout) | `gpt-6-luna`(빠르고 저렴) | low |
  | 중복 확인(26장) | `claude-haiku-5-5` | CLI 기본 |
  | `lean` 워크플로의 Claude(설계·구현) | `claude-sonnet-5-5`(쉬운 과제용) | CLI 기본 |
- 판단 자리를 CLI 기본에 맡기는 이유: 사용자가 CLI 에서 고른 주 모델을 따르고, 새 모델이 나오면 CLI 를 따라 바뀐다(모델 이름을 duet 에 박지 않음). CLI 기본을 가벼운 모델로 쓰는 사람은 설정에서 판단 자리 모델을 고른다.
- 화면: '기본 모델'이 실제로 무엇인지 보인다. Codex 는 `~/.codex/config.toml` 의 `model`, Claude 는 `~/.claude/settings.json` 의 `model`(없으면 'Claude Code 기본값').
- 같은 버그를 여러 라운드 헛도는 경우의 "새 세션으로 다시": duet 의 수정 호출은 라운드마다 새 세션이고(리뷰 지적·테스트 결과만 넘김), 수렴 실패는 중재자(23장)가 정리한다.

### 30.7 하지 않은 것
- 설정 화면의 단계별 기본값 편집(지금은 `duet.toml`·CLI 로). 관리자가 묶음 난이도로 작업자 모델을 고르는 것(다음 후보).

## 31. 큰 과제 벤치마크와 분업 결정 (2026-10-17 사용자 요청)

처음에는 "에이전트를 나누면(extended) 나아지는가"를 재려고 만들었다. 측정 전에 extended 를 없애기로 해서(31.4), 지금은 classic·lean 을 더 큰 과제로 재는 도구로 남는다. hard 묶음은 classic 이 숨은 테스트 100%(25.3)라 차이를 가리기 어렵다.

### 31.1 `large` 과제 묶음
- 모듈 경계가 분명하고 한 번에 쓰기 버거운 크기(모듈 6~8개, 참조 구현 수백~천 줄, 숨은 테스트 60개 이상). 실제 대형 프로젝트(수만 줄)에는 한참 못 미친다는 한계를 결과에 함께 적는다.
  - `issuetrack`(새): 이슈 트래커 패키지 + CLI. 모델·상태 전이 규칙·질의 언어·JSON 저장(형식 v1 이전)·CSV 가져오기/내보내기(전부 아니면 전무)·보고서·CLI. 모듈마다 함정 하나 이상. 숨은 테스트 131개.
  - `stock-warehouses`(기존): hard 의 `stock` 시작 코드(모듈 5개)를 다중 창고로 확장. 모든 모듈을 건드린다(모델·장부·이동 기록·저장 v1→v2·CLI·새 보고). 기존 동작은 그대로(hard 의 기존 동작 숨은 테스트 재사용). 숨은 테스트 61개.
- 숨은 테스트의 정확성은 참조 구현(`tests/bench_refs/`)으로 검증한다(20.1 과 같음).
- `bench.toml` 의 `hidden_tests`(숨은 테스트 수)를 적는다. 채점 때 import 실패로 모이지 않은 테스트는 파일당 오류 1개로만 잡히므로, 알려진 수에 모자란 만큼을 실패로 센다(부분 구현의 통과율이 부풀지 않게). 참조 구현 검증이 이 수와 실제 수가 같은지 확인한다.

### 31.2 고정 설계 (`--fixed-design`)
- 과제 폴더에 `design.md` 가 있으면, `duet bench run --fixed-design` 은 설계 토론을 건너뛰고(`--skip-design`) 모든 워크플로가 같은 설계로 구현부터 시작한다. 설계 호출을 아끼고 설계 편차를 없애 구현·리뷰 단계만 비교한다.
  - 새 과제: `design.md` 를 작업 폴더의 `DESIGN.md` 로. 기존 과제: 시작 코드의 `DESIGN.md` 끝에 `## 이번 변경 (벤치마크 고정 설계)` 으로 붙인다(커밋하지 않음, `--skip-design` 이 허용).
  - 고른 과제 중 `design.md` 가 없는 것이 있으면 시작하지 않는다.

### 31.3 결과에 더하는 것
- 실행별: 호출당 최대 입력 토큰(CLI 별), Codex 한도 환산(미캐시 입력 1·캐시 0.1·출력 5, 17·21장과 같은 비율), 주간 한도 사용률 변화(CLI 별, 호출에 기록된 7일 창 `used_percent` 의 차이. 같은 벤치마크의 앞 실행 마지막 값과 비교하고, 창이 바뀌면 비움. 같은 시간에 따로 쓴 사용량이 섞일 수 있다).
- 요약표: 워크플로별 평균 Codex 한도 환산·최대 입력·주간 사용률 변화.

### 31.4 결정: extended 를 없앤다 (2026-10-17, 사용자가 준 조사 보고서 검토 후 사용자 결정)
- 보고서(2026-10 기준 조사)의 근거: 멀티에이전트의 이득은 주로 **병렬로 나눌 수 있는 과업**과 **더 쓴 연산**에서 나오고, 순차 의존이 강한 과업에서는 39~70% 떨어질 수 있다(Google·MIT 스케일링 연구). 단일 기준선이 약 45% 를 넘으면 조정 이득이 줄거나 음수가 된다(역량 포화). 중앙 검증자가 있으면 오류 증폭이 17.2배 → 4.4배로 준다.
- extended 는 작업자가 **순차**라 병렬 이득이 없고, 기준선(classic)이 hard 에서 이미 100% 라 포화 구간이다. 처음에는 실험용으로 남기려 했으나 사용자가 필요 없다고 결정해 코드·템플릿·화면에서 모두 없앴다(29장).
- classic(설계 토론 = 병렬 투표, 구현 ↔ 리뷰 = 평가자-최적화자, duet 테스트 = 중앙 검증)이 기본이다. 역할을 더 늘리지 않는다. 다음 후보는 코드를 쓰지 않는 읽기 전용 도우미(예: 테스트 실패 원인 분석)뿐이다.
- 측정 계획: `large` × classic(고정 설계) 기준선은 필요할 때 사용자에게 물어 돌린다. extended 비교는 하지 않는다.

## 32. 결과 요약 (2026-10-17 사용자 요청)

실행이 끝나면 사용자가 바로 알고 싶은 것은 "뭘 만들었고, 어떻게 돌려 보나"다. 기록(설계·구현·리뷰)은 길어서 그 답을 찾기 어렵다. 찾아서 정리하는 일이라 작은 모델로 충분하다(30.6).

### 32.1 언제
- build 단계가 끝나 **승인 또는 미승인**으로 멈췄을 때 한 번(감사 단계가 있으면 그 뒤). 설계만 한 실행, 중단·실패한 실행은 하지 않는다.
- 상태 키 `report`. 이어하기에서 이미 만들었으면 다시 부르지 않는다. 단, 미승인 실행을 이어서 리뷰를 더 돌리면 예전 요약은 지우고 끝날 때 새로 만든다.
- 보조 호출이다: 실패하면 기록만 남기고, 구독 한도에 걸려도 실행 결과(승인·미승인)를 바꾸지 않는다.

### 32.2 누가·무엇을
- 설정 `report`(기본 켬), `report_agent`(기본 claude), `report_model`(기본 `claude-haiku-5-5`), `report_effort`(기본 CLI 기본). 고급 설정 '실행' 묶음에 보인다. 순서도에 '결과 요약' 칸으로 보인다.
- 읽기 전용 호출. 프롬프트에 넣는 것: 과제, 승인 여부와 마지막 판정, 바뀐 파일 목록(`git diff --stat 기준 커밋..HEAD`), duet 커밋 목록, 테스트 명령과 마지막 결과, 설계 문서 위치, `IMPLEMENTATION_NOTES.md` 가 있는지. 에이전트는 README·package.json·pyproject.toml 같은 파일을 직접 읽어 실행 방법을 찾는다.
- 답의 형식(한국어 마크다운, 짧게): `## 만든 것`(2~4줄), `## 실행 방법`(어느 폴더에서 어떤 명령, 명령은 코드 블록에), `## 테스트`(테스트 명령과 결과), `## 확인할 것`(미승인이면 남은 지적, 사람이 직접 봐야 할 것). 모르는 것은 지어내지 말고 모른다고 쓴다.
- 기록: `NN_report_<CLI>.md`, `run.json` 의 `report` 에 파일 이름.

### 32.3 보기·만들기
- 실행 상세 맨 위에 '결과 요약' 카드(마크다운 그대로, '크게 보기'로 가운데 창). 타임라인에도 '결과 요약' 기록으로 남는다.
- 요약이 없는 끝난 실행(이 기능 전의 실행 포함)에는 '결과 요약 만들기' 버튼: `duet report --workdir <폴더> --run <실행>` 을 작업으로 띄운다(`POST /api/actions/report`). 그 실행의 저장된 설정(모델 등)을 쓰고 작업 폴더를 잠근다. 이미 있으면 '다시 만들기'.

## 33. 최신 정보와 첨부 (2026-10-17 사용자 요청)

에이전트가 자기 학습 지식만으로 설계·구현하면 새 라이브러리·바뀐 API 를 틀린다(사용자: "최신 지식을 써야지"). 웹 검색, 패키지 버전 조회, 사용자가 주는 첨부 자료, 그리고 웹 화면이 있는 결과물의 브라우저 확인을 더한다. 조사만 하는 단계를 따로 두지 않는다: 무엇이 필요한지 아는 쪽(설계·리뷰하는 모델)이 필요할 때 직접 찾는다(30.6).

duet 의 목적은 에이전트 하나보다 나은 최종 결과물이다. 그래서 사용자 결정으로 에이전트 권한을 기본 **full**(모든 도구·명령·네트워크)로 연다(33.5). 안전장치는 권한 대신 duet 쪽에 둔다: 리뷰어 변경 되돌림(16장), 테스트 변조 감지(24.1), duet 가 직접 돌리는 테스트, 소유·되돌림 검사.

### 33.1 웹 검색 (`web_search`, 기본 `on`)
- `on`: 권한이 full 이면 모든 단계, standard 면 읽기 전용 단계(설계안·비판·종합·사전 조사·리뷰·중재·감사)만. `off`: 어디에도 안 붙인다. 중복 확인·결과 요약에는 붙이지 않는다.
- Claude: standard 는 그 호출의 `--allowedTools` 에 `WebSearch`, `WebFetch` 를 더하고, full 은 권한 우회로 이미 쓸 수 있으니 `off`·제외 단계에서 `--disallowedTools` 로 뺀다. Codex: `codex --search exec ...`(최상위 옵션, 이어가기도 같음).
- standard 에서 쓰기 단계를 뺀 이유: 웹 페이지 안의 숨은 지시(프롬프트 인젝션)가 파일 쓰기·명령 실행에 바로 닿는다. full 은 사용자가 그 위험을 받아들인 설정이다.
- 프롬프트: 켜진 단계에는 "라이브러리 버전·API 처럼 바뀌는 정보는 웹에서 확인하고, 근거로 쓴 출처를 적어라"를 붙인다.

### 33.2 패키지 최신 버전 조회 (`package_lookup`, 기본 켬)
- 권한 standard 에서만 의미가 있다(full 은 모든 명령을 쓴다). Claude 의 읽기 단계에 조회 전용 명령만 허용: `Bash(npm view:*)`, `Bash(npm info:*)`, `Bash(pip index versions:*)`, `Bash(cargo search:*)`, `Bash(go list -m -versions:*)`. 아무 명령이나 여는 것이 아니다. 쓰기 단계는 원래 Bash 를 쓴다.
- Codex 의 읽기 전용 샌드박스는 네트워크가 막혀 있어 웹 검색(33.1)으로 확인한다.
- 프롬프트(설계안·종합·사전 조사): "패키지 최신 버전은 `npm view <이름> version`, `pip index versions <이름>` 으로 확인하고 고정할 버전을 정해라."

### 33.3 첨부 파일
- `duet run --attach 파일`(여러 번). 실행 폴더의 `attachments/` 로 복사하고(이름이 겹치면 번호), `run.json` 의 `attachments` 에 실행 폴더 기준 경로를 남긴다. 최대 10개, 합 20MB, 이름은 안전한 글자만.
- 프롬프트(설계안·비판·종합·구현·수정·리뷰)에 `## 첨부 파일` 절: 작업 폴더 기준 경로 목록과 "이미지·PDF 는 파일을 열어서 봐라". Claude 는 Read 로 이미지·PDF 를 본다. Codex 는 이미지(png·jpg·jpeg·gif·webp)를 `-i` 로도 붙인다(이어가기 포함).
- UI: 새 실행 창의 '첨부' 칸(여러 파일). 브라우저가 파일을 base64 로 보내고(원격 머신도 같은 `_api` stdin), 서버가 작업 폴더의 `.duet/ui/attachments/<시각>/` 에 저장한 뒤 `--attach` 로 넘긴다.

### 33.4 브라우저 확인 (`browser_check`, 기본 `auto`)
- **필요할 때만**: `auto` 는 결과물이 웹 화면이 있는 프로젝트이고(작업 폴더에 `package.json` 의 `dev`/`start` 스크립트와 `node_modules`, 또는 최상위 `index.html`) `npx` 가 있을 때만 켠다. `off` 는 끈다. 켜져도 리뷰어가 필요하다고 판단할 때만 브라우저를 쓴다.
- 리뷰 라운드마다 duet 가 앱을 띄운다: `npm run dev`(없으면 `npm start`)를 백그라운드로 띄우고 출력에서 `http://localhost:포트` 를 찾는다(60초). `index.html` 만 있으면 duet 의 파이썬으로 `http.server` 를 빈 포트에 띄운다. 리뷰가 끝나면 프로세스 트리를 끈다. 못 띄우면 기록만 남기고 브라우저 없이 리뷰한다.
- 리뷰어에게 Playwright MCP(`npx -y @playwright/mcp@latest --headless --isolated`, 브라우저는 설치된 Chrome, 없으면 Edge)를 붙인다. Claude: `--mcp-config <JSON>` + `--allowedTools mcp__playwright`. Codex: `-c mcp_servers.playwright.command=…`, `-c mcp_servers.playwright.args=[…]`. 리뷰 프롬프트에 "앱이 <주소> 에서 돌고 있다. 과제나 설계에 화면·레이아웃·화면 폭·버튼 동작 같은 요구가 있으면 열어서 직접 눌러 보고, 본 것을 지적의 근거로 적어라. 화면과 상관없는 변경이면 쓰지 않아도 된다".
- 프롬프트에 도구 이름(`mcp__playwright__browser_navigate`·`browser_snapshot`·`browser_click`·`browser_type`·`browser_resize`)을 콕 집어 적는다. 실제 실행(2026-10-18)에서 "브라우저 도구(playwright)"라고만 쓰니 Codex 가 내장 computer-use 플러그인으로 가서 브라우저를 못 찾고 열지 않았다. 이름을 적어 주자 playwright 로 열고·폭을 바꿔 확인했다. 바로 안 보이면 도구 검색으로 찾으라고, 다른 브라우저 도구는 이 앱을 보지 못한다고도 적는다.
- 실제 실행으로 확인한 것: full 권한에서 Codex 는 MCP 도구를 승인 없이 쓴다. Playwright MCP 는 내려받은 뒤 2초 안에 뜬다(첫 실행은 npx 내려받기만큼 더 걸린다).

### 33.5 권한 수준 (`access`, 기본 `full`)
- `full`(기본, 사용자 결정 2026-10-17): 에이전트가 모든 도구·명령·네트워크를 쓴다.
  - Claude: `--permission-mode bypassPermissions`(설정의 `read_args`·`write_args` 의 권한 부분을 대신한다). 읽기 전용 단계는 `--disallowedTools Edit,Write,NotebookEdit` 로 파일 편집 도구만 뺀다(명령 실행·웹·MCP 는 쓴다). `claude_sandbox`(24.3)는 쓰지 않는다.
  - Codex: `--dangerously-bypass-approvals-and-sandbox`(읽기·쓰기·이어가기 모두, `--sandbox` 대신).
  - 읽기 단계도 명령을 돌릴 수 있으니 리뷰어가 직접 테스트·앱 실행으로 확인할 수 있다(19.6 의 '명령 실행 가능'). 작업 폴더 안의 변경은 지금처럼 duet 가 되돌린다. **작업 폴더 밖의 변경과 네트워크 사용은 막지 않는다.**
- `standard`: 지금까지의 권한(Claude 읽기 Read·Glob·Grep, 쓰기 acceptEdits + Read·Edit·Write·Glob·Grep·Bash, Codex read-only·workspace-write 샌드박스) + 33.1·33.2 의 추가분.
- 설정 화면·새 실행 창의 고급 설정에서 고르고, 실행 상세의 칩과 시작 로그에 보인다.


## 34. 혼자 기준선 (2026-10-20 사용자 요청)

duet 를 만드는 이유는 "에이전트 하나보다 나은 최종 결과"다(33.5). 그러니 벤치마크의 비교 기준에 에이전트 혼자를 둔다. 첫 비교(`large/issuetrack` 1회, 2026-10-20): Claude 혼자 130/131(6분 35초, 입력 103만 토큰), classic 129/131(29분 34초, 입력 334만 토큰, 리뷰 1회차 승인). 둘 다 `update(1, id=5)` 함정에 걸렸고, classic 은 설계 토론에서 모호한 CSV 라벨 규칙을 더 엄격하게 정해 하나 더 틀렸다. 표본이 하나라 더 돌려 본다(사용자: 실효성 테스트이므로 토큰을 아끼지 않는다).

### 34.1 `solo-claude`, `solo-codex`
- `duet bench run --workflow solo-claude --workflow solo-codex --workflow classic ...`. 워크플로가 아니라 **벤치마크 전용 이름**이다(`duet run` 에서는 못 쓴다).
- 그 CLI 를 한 번 부른다: 프롬프트는 과제(`task.md`) + "이 작업 폴더에서 위 과제를 끝까지 구현하라. 답할 사람이 없는 비대화형 실행이므로 묻거나 승인을 기다리지 말고 스스로 정해 진행하라. 테스트를 직접 돌려 확인한 뒤 끝내라." (비대화형 문장은 실측 뒤 추가: 사용자의 Codex 전역 스킬 `brainstorming` 이 설계 승인을 묻고 코드 없이 끝나 0점이 됐다. 사람이 직접 쓰면 "진행해"라고 답했을 상황이라 기준선에서는 막는다.) 쓰기 모드, 단계 `implement`(구현 자리와 같은 모델·추론 강도·권한·도구), 시간 제한은 과제의 `timeout_sec`. 설계 토론·리뷰·duet 테스트 되먹임은 없다. 고정 설계(31.2)면 "작업 폴더의 DESIGN.md 설계를 따른다"를 한 줄 더한다.
- 끝나면 duet 테스트 명령(19장 감지 규칙)을 한 번 돌려 '자체 테스트'로 기록하고, 숨은 테스트로 채점한다(20.3). 응답과 호출 정보는 작업 폴더의 `.duet/solo/`(채점 복사에서 빠짐).
- 상태: 정상 끝 `solo`(표시 '혼자 완료'), 실패·시간 초과 `failed`(채점 안 함, classic 의 실패와 같게), 한도 `stopped`(벤치마크를 멈춤). `--wait-on-limit` 면 25.1 처럼 재개 시각까지 기다렸다가 같은 호출을 다시 보낸다(대기 예산 `limit_wait_max_min`).
- 요약표·UI: 승인·중재·승인했는데 실패 칸은 `-`. 통과율·시간·호출·토큰·Codex 한도 환산·주간 사용률은 같은 방식으로 센다.

### 34.2 측정 결과 (2026-10-21)
`hard`(5)·`large`(2)·`dispute`(3) 10개 과제 × 방식 3개 × 3회 = 90번. 같은 PC에서 동시에 돌림. 혼자는 34.1 의 비대화형 프롬프트, duet 는 classic(설계 토론 1라운드, 구현 Claude, 리뷰 Codex). 모델: Claude CLI 기본(claude-opus-5-5), Codex CLI 기본(gpt-daybreak-blue-latest).

| 방식 | 숨은 테스트 통과율 | 전부 통과 | 평균 시간 | 평균 입력 토큰 | 사용량 환산(미캐시 1·캐시 0.1·출력 5) |
|---|---|---|---|---|---|
| Claude 혼자 | 99.77% | 27/30 | 2.3분 | 48만 | 15만 |
| Codex 혼자 | 99.54% | 27/30 | 3.6분 | 59만 | 15만 |
| duet classic | 99.54% | 27/30 | 16.8분 | 187만 | 80만 |

- 9개 과제는 세 방식 모두 3회 다 만점. 차이는 `issuetrack` 하나뿐: 셋 다 `update(1, id=5)`(ValueError 여야 하는데 TypeError) 함정에 걸렸고, CSV 라벨 `"Bug; UI ;"` 의 공백 처리는 Claude 혼자만 맞혔다.
- duet 가 Codex 혼자와 똑같이 틀린 이유: 세 번 모두 Codex 의 설계 종합이 "명세에 없는 공백 제거·빈 조각 제거를 넣지 않는다"로 정했고, Claude 는 그 설계대로 구현했다. 설계 토론이 모호한 점을 한 모델의 해석으로 고정해 다른 모델의 더 나은 판단을 덮었다.
- 리뷰: 30번 중 27번이 1회차 승인, 지적이 나온 리뷰는 5번(P2 3·P3 2)이고 그 과제들은 혼자도 만점이었다. 리뷰가 숨은 테스트 결과를 바꾼 사례는 없다. 공통 함정(`id=` 키워드)도 리뷰가 잡지 못했다.
- duet 의 자체 테스트는 더 많다(issuetrack 기준 100개 대 65개)·설계 문서가 남는다. 숨은 테스트가 재지 않는 품질이다.
- 결론(이 과제 묶음 기준): duet classic 은 혼자보다 정확하지 않고, 시간 약 7배·사용량 약 5배를 쓴다. 과제 묶음이 포화됐다(혼자도 99% 이상) — 31.4 의 '역량 포화' 예측과 같다. duet 가 이득을 낼 곳을 찾으려면 혼자가 자주 틀리는 과제가 필요하다.
- 참고: 비대화형 문장 전(예전 프롬프트)의 Claude 혼자는 ledger 에서 2/2 회 29/30(`test_single_space_is_part_of_account_name`)이었다가 새 프롬프트로 3/3 만점. 회차·프롬프트에 따른 흔들림이 이 정도 있다.

## 35. 어려운 과제 묶음 `frontier` 와 duet 흐름 고치기 (2026-10-21 사용자 요청)

34.2 에서 기존 과제 묶음은 포화됐다(혼자도 99% 이상). duet 가 이득을 내는 곳이 있는지 보려면 **혼자가 자주 틀리는 과제**가 필요하다. 목표: 혼자 통과율 70~90%. 그 과제에서 duet 와 혼자를 비교하고, 결과로 duet 흐름을 고친다.

### 35.1 과제 후보
공통: 표준 라이브러리만, 패키지는 작업 폴더 최상위(src 레이아웃 아님), 인터페이스는 정확히 준다. **어려움은 함정이 많은 명세에서 나오게 하고 모호함으로 만들지 않는다**: 숨은 테스트가 재는 동작은 모두 과제 설명에 적혀 있어야 한다. 숨은 테스트는 과제당 80개 이상(실수 하나가 통과율을 크게 흔들지 않게), 규칙마다 고르게.
- `minire`(새): 정규식 엔진 부분집합(문자 클래스, 그룹·이름 그룹·비캡처, 탐욕·게으른 수량자, 앵커, 역참조, 전방 탐색, 대소문자 무시) + `match/search/fullmatch/findall/finditer/sub/split`. 기대 동작은 파이썬 `re` 의 같은 부분집합이고, 숨은 테스트는 채점 때 `re` 의 결과와 비교한다(신탁). 구현이 `re` 를 쓰지 않았는지 숨은 테스트가 소스를 검사한다.
- `minisql`(새): SQL 부분집합 인메모리 엔진(JOIN·LEFT JOIN, WHERE 3값 논리, GROUP BY·HAVING, 집계의 NULL 규칙, ORDER BY 의 NULL 순서, DISTINCT, LIMIT·OFFSET, IN·EXISTS 하위 질의, 정수 나눗셈, 타입 비교). 기대 동작은 SQLite 의 같은 부분집합이고, 숨은 테스트는 같은 질의를 표준 라이브러리 `sqlite3` 에 돌려 비교한다. `sqlite3` 를 쓰지 않았는지 검사한다.
- `sheet`(새): 스프레드시트 엔진(연산자 우선순위 함정, 상대·절대 참조, 복사할 때 참조 조정, 행·열 삽입·삭제에 따른 참조 조정과 `#REF!`, 오류 값 전파, 순환 참조, 재계산). 신탁이 없어 참조 구현으로 검증한다.
- `tzrule`(새): POSIX TZ 규칙 문자열(예 `EST5EDT,M3.2.0,M11.1.0`) 해석, 지역 시각↔UTC 변환(DST 틈·겹침, fold), 반복 일정 펼치기(n번째·마지막 요일, 없는 날짜 건너뛰기). 참조 구현으로 검증하고, 작성할 때 리눅스 zoneinfo 와 대조한다.
- `mdlite`(새): 마크다운 부분집합 → HTML. CommonMark 의 강조 구분자 규칙(왼쪽·오른쪽 측면, 3의 배수 규칙), 코드 스팬, 링크 참조 정의, 목록의 느슨함·들여쓰기. 참조 구현으로 검증하고, 작성할 때 CommonMark 구현과 대조한다.
- 참조 구현은 `tests/bench_refs/<과제>/`, 숨은 테스트 검증은 다른 묶음과 같다(20.1).

### 35.2 보정
- 후보마다 solo-claude·solo-codex 를 3회씩 돌린다. 혼자 평균 통과율이 95% 를 넘으면 함정을 더하거나(명세에 규칙 추가 + 숨은 테스트) 뺀다. 둘 중 하나 이상이 70~90% 면 채택.
- 보정 실행의 결과도 남긴다(어떤 함정이 실제로 틀리는지가 흐름 고치기의 단서다).

### 35.3 비교와 고치기
- 채택한 과제에서 solo-claude·solo-codex·classic 을 3회씩(34.2 와 같은 방식).
- duet 가 지거나 이긴 곳을 실행 기록(설계안·비판·종합·리뷰)으로 따라가 원인을 찾고 흐름을 고친다. 34.2 에서 나온 후보: 설계 종합이 명세에 없는 점을 한쪽 해석으로 고정하지 않기, 리뷰가 명세 조항별로 확인하기. 고친 뒤 같은 과제로 다시 재서 비교하고, 고친 내용과 결과를 이 장에 적는다.

### 35.4 보정 1차 결과 (2026-10-21): 명세형 과제는 혼자도 포화
| 과제 | Claude 혼자 (3회) | Codex 혼자 (3회) |
|---|---|---|
| minire (396) | 100% ×3 | 100%, 99.7%, 100% |
| minisql (461) | 99.8%, 99.8%, (진행) | 98.9%, 99.3%, 99.6% |
| sheet (487) | 100% ×3 | 99.8%, 99.8%, 100% |
| tzrule (289) | 100% ×3 | 100%, 100%, 98.3% |
| mdlite (505) | 100% ×3 | 99.4%, 99.4%, (진행) |

- 모든 규칙을 적은 긴 명세는 지금의 에이전트가 가장 잘하는 일이다(한 번에 4~26분, CommonMark 포팅도 만점). 신탁(`re`·`sqlite3`)이 있는 과제는 에이전트가 스스로 신탁과 비교하며 맞춘다.
- 혼자 구현 15개를 무작위 입력 3,000개로 `re` 와 비교해도 불일치 0~0.3% — 퍼징 테스트를 더해도 70~90% 에 닿지 않는다.
- 그래서 어려움의 원천을 바꾼다(35.5). 이 다섯 과제는 '명세형 대조군'으로 남긴다(혼자가 강한 곳).

### 35.5 2차 후보: 기존 코드 + 티켓 묶음
실제 일에 가까운 형태: 이미 있는 코드베이스(모듈 10~15개, 2,500~4,000줄, 보이는 테스트는 일부만)에 티켓 6~8개(버그 보고·기능 요청)를 한 번에 처리한다. 요구는 티켓에 분명히 적되, **어디를 고쳐야 하는지 찾고 아무것도 깨뜨리지 않는 것**이 어렵게 만든다.
- 함정: 같은 로직이 여러 곳에 복사돼 있어 한 곳만 고치면 틀림, 순진한 수정이 보이는 테스트에 없는(그러나 코드 주석·README 에 적힌) 기존 동작을 깨뜨림, 여러 모듈을 가로지르는 기능, 저장 형식 호환, 성능, 서로 얽힌 티켓.
- 숨은 테스트: 티켓마다 8~15개 + 기존 동작 회귀 테스트 30개 이상. 시작 코드는 회귀 테스트와 보이는 테스트는 통과하고 티켓 테스트만 실패해야 한다(검증 테스트).
- duet 의 리뷰(차이와 호출 지점을 보는 두 번째 눈)가 이득을 낼 가능성이 가장 큰 형태다.


## 36. tenure: 앱 v2 (2026-10-21 사용자 승인)

> 사용자 결정(2026-10-21): 표준 라이브러리만 쓸 필요 없음, 데스크톱 셸은 Tauri, 대화형 터미널(M34) 필요, 제품 이름은 **tenure**, 기본은 로컬이고 SSH 원격 연결 기능은 남긴다.
> 근거: 34·35장 벤치마크, `docs/research/multiagent-tools-2026-10.md`(8개 도구 분석).

### 36.1 무엇을 만드나

**여러 코딩 에이전트(Claude Code·Codex)를 한곳에서 부리고, 검증된 과정으로 결과를 끌어올리는 데스크톱 앱.** 기존 duet 엔진(과정·벤치마크) 위에, 8개 도구에서 영역별로 가장 나은 설계를 가져와 새로 짠다.

#### 36.1.1 duet 만의 것 (다른 도구에 없는 것)
- **과정**: 교차 리뷰, 경쟁 초안, 자동 경로 선택, 결과 판정. 다른 도구들은 에이전트를 띄우고 보여 주는 데서 멈춘다(Emdash 조율 1점, CodexMonitor 2점).
- **측정**: 숨은 테스트 벤치마크와 혼자 기준선. 새 기능은 벤치마크로 효과를 재고 넣는다.

#### 36.1.2 원칙
1. **기본은 싸게.** 명확한 과제에서 협업은 정확도를 못 올리고 비용만 늘렸다(34.2). 기본 흐름은 혼자 구현 + 짧은 리뷰, 비싼 흐름은 필요할 때만.
2. **사람의 시간을 아낀다.** 가치의 중심은 정확도보다 운영 경험: 무엇이 나를 기다리는지, 지금 무엇을 하는지, 무엇이 바뀌었는지, 합쳐도 되는지.
3. **정직한 상태.** 모르면 "모름"(비용 0원 아님, 성공 아님). 모든 단계는 이유와 함께 끝난다.
4. **사용자 설정을 건드리지 않는다.** `~/.claude`·`~/.codex` 를 고치지 않고, 훅·MCP 는 호출마다 붙인다(`--settings`, `--mcp-config`, `-c`). (Daintree 의 원칙, Emdash·Orca 는 전역 설정에 씀)
5. **윈도우 1급.** 이 사용자의 주 환경. 윈도우에서 매 단계 테스트한다.
6. **로컬 우선.** 로컬 API 는 토큰 인증, 텔레메트리 없음, 화면 내용을 밖으로 보내지 않음.

### 36.2 구조

```
┌──────────────── 데스크톱 셸 (Tauri 2, WebView2) ────────────────┐
│  프런트엔드: React + TypeScript + Vite                            │
│   xterm.js(터미널) · 차이 뷰어 · 디자인 시스템                     │
└───────────────▲─────────────────────────────────────────────────┘
                │ HTTP + SSE (127.0.0.1, 토큰)   ※ 브라우저로도 접속 가능(원격 css53)
┌───────────────┴──────────── duet 엔진 (Python) ──────────────────┐
│ API 서버 │ 워크플로(quick·auto·classic·경쟁 초안·사용자 리뷰) │ 벤치마크 │
│ 에이전트 런타임: ① 헤드리스 구조화  ② 대화형 터미널(PTY)            │
│ worktree·병합 │ 상태·이벤트(events.jsonl) │ 알림 │ 사용량 │ 저장       │
└──────────────────────────────────────────────────────────────────┘
        │ claude -p --output-format stream-json  /  codex app-server(JSON-RPC)
        │ claude / codex 대화형 (ConPTY, pywinpty)
```

- **엔진은 지금의 Python 을 이어 쓴다.** 테스트 600여 개가 받치는 과정·어댑터·벤치마크를 다시 짤 이유가 없다. 표준 라이브러리 제한만 풀어 필요한 패키지를 쓴다(예: `pywinpty`, `watchfiles`, 필요하면 `pydantic`).
- **데스크톱 셸은 Tauri 2.** Electron 보다 작고(설치 파일 수 MB), CodexMonitor·Schaltwerk 가 이미 같은 조합으로 윈도우 설치 파일을 낸다(MIT, 참고 가능). 엔진은 Tauri 의 sidecar 로 띄운다. 웹 화면이므로 셸 없이 브라우저로도 열린다(지금의 `duet ui`, 원격 서버 css53).
- **프런트엔드는 새로 짠다.** 지금의 바닐라 JS 화면은 교체한다. React + TypeScript, 상태는 Zustand, 터미널 xterm.js, 차이 뷰어는 `@pierre/diffs`(Schaltwerk·CodexMonitor 사용, 라이선스 확인 후) 또는 Monaco diff.
- **저장**: 프로젝트별 `.duet/`(실행 기록, `events.jsonl`) + 전역 SQLite(`~/.duet/duet.db`: 실행 색인, 알림함, 사용량 기록, 설정).

### 36.3 핵심 개념

| 개념 | 뜻 | 출처 |
|---|---|---|
| 프로젝트 | 저장소 하나 | 공통 |
| 작업(Task) | 하나의 목적. worktree + 브랜치 + 명세(spec)를 가진다. 명세는 남기고 worktree 만 버릴 수 있다 | Orca·Schaltwerk·Emdash |
| 실행(Run) | 작업 위에서 돈 duet 워크플로 한 번 (단계들의 묶음) | duet |
| 단계(Step) | 에이전트 호출 한 번. **결과 계약**으로 끝난다 | duet + OpenRig·Orca |
| 세션(Session) | 에이전트 대화. 제공자 세션 id 는 '마지막으로 본 포인터'일 뿐 정체가 아니다 | Emdash |
| 이벤트(Event) | 순번(seq)이 붙은 상태 변화. 늦게 온 옛 이벤트는 버린다 | herdr·Emdash |
| 주의 항목 | 사람이 볼 것(승인·질문·막힘·끝났는데 안 봄) | Daintree·herdr·NotchCrew |
| 리뷰 메모 | 차이의 줄에 고정된 메모(내용 해시로 고정, 줄이 바뀌면 '오래됨') | Daintree |

#### 36.3.1 상태 어휘 (정직한 상태)
- 세션 활동: `working` / `blocked(이유)` / `idle` / `done-unseen` / `failed` / `unverifiable`(출력이 끊김, 프로세스는 살아 있음) / `unconfirmed`(끝났는데 결과 블록 없음).
- `blocked` 이유: 승인 대기, 질문, 로그인 필요, 폴더 신뢰, 업데이트, 구독 한도, 모름. 이유마다 고치는 법과 버튼. (OpenRig 의 주의 코드)
- 주의 순서: blocked > done-unseen > working > idle. 열어 보면 unseen 이 지워진다. (herdr)

#### 36.3.2 단계 결과 계약
모든 단계는 `{outcome: done|blocked|failed|needs_human, reason, summary(3줄), files_changed, next}` 로 끝난다. 헤드리스 단계는 구조화 출력(JSON 스키마), 대화형 세션은 완료 표식(`DUET-DONE-<코드>: 요약 END-<코드>`, Daintree 의 handback)을 파싱한다. 표식이 없으면 `unconfirmed`. 상태 전환·단계 시작/끝·로그는 실행별 `events.jsonl` 에 순번(seq)을 붙여 append-only 로 남기고(따로 transitions 파일을 두지 않음), 화면은 `/api/stream`(SSE, 이 머신) 또는 `/api/events?since=`(폴링, 원격)로 받는다. 단계 결과는 `run.json` 의 호출 기록(outcome·reason)에도 남는다. 끝난 실행을 화면에서 열면 `seen_at` 을 적어 '끝났는데 안 봄'에서 빠진다. (OpenRig 의 hot-potato, Orca 의 worker_done)

### 36.4 에이전트 런타임

#### 36.4.1 헤드리스 구조화 (duet 워크플로의 기본)
- Claude: `claude -p --output-format stream-json --session-id <미리 만든 uuid>` (지금과 같음 + 세션 id 를 미리 정함: OpenRig·Daintree). 진행 이벤트(도구 호출, 파일)를 실시간으로 이벤트 스트림에 싣는다.
- Codex: **`codex app-server`(JSON-RPC)로 옮긴다.** 턴마다 model·effort·approvalPolicy·sandboxPolicy 를 주고, `account/rateLimits/read` 로 한도를 읽고, `review/start` 로 Codex 자체 리뷰를 쓸 수 있다. (CodexMonitor 의 클라이언트 구조를 참고해 Python 으로 다시 짬, MIT) 앱 서버는 작업마다 따로 띄운다(CodexMonitor 처럼 하나를 공유하면 하나가 죽을 때 다 죽음).
- 승인: full 권한이 기본(33.5)이라 대부분 없음. standard 에서는 승인 요청을 주의 항목(카드)으로 올리고 답을 돌려준다(Emdash 의 PermissionBroker).

#### 36.4.2 대화형 터미널 (사람이 직접 부릴 때)
- 작업 화면에서 에이전트를 터미널로 직접 열 수 있다(Orca·Emdash·Schaltwerk 의 기본 방식). 윈도우는 ConPTY(`pywinpty`), 화면은 xterm.js.
- 상태 판별 3단: ① 호출마다 붙이는 훅(`claude --settings <임시파일>` 의 SessionStart·Stop·Notification·PreToolUse → 로컬 API, 실행별 nonce) ② 화면 규칙 파일(herdr 의 우선순위·영역·정규식 규칙, `duet explain` 으로 어떤 규칙이 맞았는지 보기) ③ 프로세스 종료. 훅이 2분 조용하면 다음 단계로(Orca).
- 메시지 넣기: bracketed paste + 지연 Enter, 막힘 상태면 거부(herdr·OpenRig).

### 36.5 워크플로

| 이름 | 흐름 | 언제 |
|---|---|---|
| **quick** (기본) | Claude 혼자 구현 → duet 테스트 → Codex 짧은 리뷰(요구 조항별 확인·빠진 호출 지점) → P1·P2 있을 때만 고치기(최대 2회) | 대부분 |
| **auto** (기본 선택기) | 규칙(기존 프로젝트·분명한 요구 → quick, 짧고 모호한 새 프로젝트 → classic) → 애매하면 haiku 한 번 | 항상 먼저 |
| classic | 설계 토론 → 구현 → 교차 리뷰. 고침: 종합이 명세에 없는 동작을 새로 정하지 않음, 구현은 과제 원문 우선, 과제에 적힌 경계 사례는 P2 | 큰 설계 결정이 있을 때 |
| **경쟁 초안** | Claude·Codex 가 각자 worktree 에서 구현 → 각각 테스트 → 서로 리뷰 → 판정(테스트·리뷰·차이 크기) → 승자 병합(또는 사람이 고름) | 벤치마크로 효과 확인 뒤 옵션 |
| 사용자 리뷰 | 사람이 차이에 단 메모를 모아 고치기 라운드로 | 언제든 |

### 36.6 worktree·병합
- 작업마다 worktree: `<프로젝트>/.duet/worktrees/<이름>`, 브랜치 `duet/<이름>`(처음엔 자동 이름, 첫 작업 뒤 내용으로 이름 바꾸기: Orca). `.duetinclude` 로 .gitignore 된 파일(.env 등) 복사, `shared_dirs` 는 링크(Orca·Emdash).
- 병합(Schaltwerk 방식): 메모리 안 병합으로 **충돌 미리보기** → 필요하면 부모 브랜치로 리베이스 → 스쿼시 또는 그대로 적용, 병합 잠금. 충돌이 있으면 멈추고 충돌 파일과 "에이전트에게 해결 맡기기" 버튼. 버린 worktree 는 백업(`.duet/discarded/`). PR 은 `gh`.
- **병합 준비 점검**(Daintree): 충돌·테스트 실패·리뷰 미해결·푸시 안 됨·보호 브랜치 등 정해진 항목과 심각도. 점검은 보여 주기만 하고 병합은 사람이 누른다.
- git 저장소가 아니면 worktree 없이 지금처럼 폴더에서(Orca 도 지원).

### 36.7 화면

| 화면 | 내용 | 주요 출처 |
|---|---|---|
| **받은편지함(홈)** | 프로젝트를 가로질러 주의 순으로: 나를 기다림(승인·질문·막힘) → 끝났는데 안 봄 → 진행 중. 항목마다 한 번에 처리하는 버튼 | Daintree Canopy·OpenRig For You·herdr |
| **사이드바** | 프로젝트 → 작업 카드(상태 점·모양, 브랜치, 앞섬/뒤처짐, 마지막 한 줄) | Orca·Schaltwerk |
| **작업 화면** | 가운데: 실행 타임라인(단계별 실시간 활동, 현재 도구·파일) 또는 터미널. 오른쪽 탭: 명세 / 바뀐 것 / 리뷰 / 사용량 / 에이전트 설정 | Emdash·Schaltwerk |
| **리뷰·병합** | 차이 + 줄 고정 메모 → "에이전트에게 보내기"(자동 전송 없이 확인), 병합 준비 점검, 병합 버튼 | Daintree·Schaltwerk·Orca |
| **비교** | 경쟁 초안 두 worktree 의 차이를 나란히, 테스트·리뷰 결과와 함께 | Daintree CrossWorktreeDiff |
| **사용량** | CLI 별 5시간·주간 한도와 재설정 시각, 일·모델별 토큰 기록, 프롬프트 캐시 남은 시간, 모르면 "모름" | CodexMonitor·Orca·OpenRig |
| **에이전트 설정** | 그 폴더에서 에이전트가 실제로 쓰는 스킬·MCP·플러그인·규칙(보기 전용, 비밀값 숨김). duet 실행에서 끌 항목 고르기 | NotchCrew (34.1 실측: 전역 스킬이 실행을 망침) |
| 벤치마크 | 지금 것 + 혼자 기준선·경쟁 초안 비교 | duet |
| 설정 | 권한 프리셋(locked·standard·full), 단계별 모델·추론 강도, 알림 정책 | OpenRig·duet |
| 공통 | 명령 팔레트, 단축키(바꿀 수 있음), 상태는 색+모양(색약), 한국어 | Orca·Daintree·herdr |

알림: 상태 전환 때만(→막힘 "기다림", →끝 "완료"), 지금 보고 있는 작업은 빼고. 화면 배지 → 브라우저/OS 알림 → 소리 순으로 낮은 단계부터(Daintree 의 다섯 표면 정책). 윈도우 토스트.

### 36.8 무엇을 어디서 가져오나

| 영역 | 가져올 설계 | 출처(라이선스) | 방식 |
|---|---|---|---|
| Codex 연결 | app-server JSON-RPC 클라이언트, 턴별 정책, 한도 읽기 | CodexMonitor (MIT) | Python 으로 다시 짬 |
| 상태 판별 | 호출마다 붙이는 훅 + nonce, 화면 규칙 파일 + explain, 순번으로 옛 보고 버리기 | Emdash (Apache)·herdr (Apache)·Orca (MIT) | 다시 짬 |
| 결과 계약 | 종료 이유 강제, 완료 표식 | OpenRig·Daintree (Apache)·Orca | 다시 짬 |
| worktree | 생성 파이프라인, 포함 파일, 자동 이름 | Orca·Emdash | 다시 짬 |
| 병합 | 충돌 미리보기 → 리베이스 → 스쿼시, 백업 | Schaltwerk (MIT) | 다시 짬 |
| 리뷰 | 해시 고정 메모, 병합 준비 점검 | Daintree (Apache, 상표 주의: 이름·로고 안 씀) | 다시 짬 |
| 사용량 | 로컬 기록 스캔(~/.codex/sessions, ~/.claude/projects), 가격표, 캐시 타이머 | CodexMonitor·Orca | 다시 짬 |
| 알림 | 전환 때만, 표면 정책 | Daintree·herdr | 다시 짬 |
| 화면 | 받은편지함, 작업 카드, 비교 뷰, 에이전트 설정 보기 | Daintree·Orca·NotchCrew(아이디어만) | 새로 디자인 |
| 차이 뷰어 | `@pierre/diffs` 컴포넌트 | 패키지 라이선스 확인 후 의존성으로 | 사용 |

빌린 코드가 생기면 `THIRD_PARTY_NOTICES.md` 에 출처를 적는다. NotchCrew 는 소스가 없어 아이디어만.

### 36.9 단계별 계획

| 단계 | 내용 | 끝났다는 기준 |
|---|---|---|
| **M30 엔진 기반** | 의존성 허용(CLAUDE.md·DESIGN.md 정리), 상태 어휘·결과 계약·events.jsonl·SSE, 로컬 API 정리(토큰), quick·auto 워크플로, classic 고침 | 기존 테스트 통과, 벤치마크에서 quick 이 혼자 이상 정확도·비용 1.5배 이하 |
| **M31 새 화면 1** | React 프런트엔드: 받은편지함, 사이드바·작업 카드, 실행 타임라인(실시간), 사용량, 설정. 지금 화면 대체 | 지금 UI 가 하던 일을 모두 함, 윈도우·브라우저 확인 |
| **M32 worktree·리뷰·병합** | 작업별 worktree, 차이 뷰어 + 줄 메모 → 에이전트, 병합 준비 점검, 로컬 병합(충돌 미리보기), PR | 실제 저장소로 작업 → 리뷰 → 병합 끝까지 |
| **M33 경쟁 초안** | 두 worktree 동시 구현, 비교 화면, 판정. 벤치마크로 측정 | 효과가 있으면 옵션으로, 없으면 기록만 |
| **M34 대화형 터미널** | PTY 세션, 훅·화면 규칙 상태 판별, 승인·질문 카드, 에이전트 설정 보기, OS 알림 | 사람이 직접 부리는 세션이 상태와 함께 보임 |
| **M35 데스크톱 배포** | Tauri 셸, 엔진 sidecar, 윈도우 설치 파일, 업데이트 | 새 PC 에 설치해 바로 사용 |
| 최적화 | 토큰(리뷰 입력 줄이기·캐시 활용), 속도(병렬·미리 만든 worktree), 화면 반응성 | 측정값으로 |

### 36.10 하지 않는 것
- 30종 에이전트: Claude Code·Codex 둘에 집중(어댑터 구조는 열어 둠).
- tmux 의존, 모바일 앱, 클라우드 중계, 플러그인 마켓, 계정 바꿔치기, 화면 내용을 외부 서버로 보내는 기능(Daintree Canopy 같은 것).
- 다른 도구의 포크.

### 36.11 결정 (사용자, 2026-10-21)
1. 데스크톱 셸: **Tauri 2**.
2. 대화형 터미널(M34): **필요**.
3. 제품 이름: **tenure**. 이름 바꾸기는 M30 의 첫 일이다: 파이썬 패키지 `duet` → `tenure`, 명령 `tenure`(옛 `duet` 명령은 한동안 같은 동작의 별칭), 프로젝트 폴더 `.duet/` → `.tenure/`·전역 `~/.duet` → `~/.tenure`(옛 폴더가 있으면 읽고, 처음 쓸 때 옮김), 환경 변수 `DUET_*` → `TENURE_*`(옛 이름도 읽음). 문서의 과거 기록(1~35장)은 고치지 않는다.
4. 원격: **기본은 로컬**. SSH 원격 연결 기능(26장 계열: 원격 머신의 엔진을 SSH 로 부름)은 남긴다. 의존성이 생기므로 원격 쪽은 zip 하나(`.pyz`) 대신 `pip install` 된 tenure 를 부르거나, 순수 파이썬 의존성만 묶은 zip 을 쓴다(M30 에서 정함). 원격 API 가 쓰는 코드는 무거운 의존성 없이 돌게 둔다.

### 36.12 M30 측정: quick (2026-10-21)
기존 과제 18개 × quick 3회 = 54번(Windows, 동시 9개). 비교는 같은 과제의 이전 결과(혼자: 34.2·35.4, classic: 34.2·35장). 네 방식이 모두 있는 15개 과제(hard·large·dispute·frontier):

| 방식 | 실행 | 숨은 테스트 통과율 | 전부 통과 | 평균 시간 | 평균 사용량 환산 |
|---|---|---|---|---|---|
| Claude 혼자 | 45 | 99.92% | 39/45 | 5.8분 | 34만 |
| Codex 혼자 | 45 | 99.48% | 32/45 | 5.7분 | 22만 |
| classic | 40 | 99.86% | 35/40 | 22.6분 | 112만 |
| **quick** | 45 | **99.92%** | 39/45 | 8.9분 | 55만 |

- 정확도: quick 은 Claude 혼자와 같고 classic 보다 낫다. issuetrack 에서 classic 이 놓치던 CSV 라벨 규칙을 quick 은 3번 모두 맞혔다(설계 종합이 과제 해석을 덮는 문제가 없음). 남은 실패는 셋 다 같은 함정(`update(id=)`, minisql 의 복합 질의 열 이름)이다. backlog 3개 과제도 quick 9번 모두 만점.
- 비용: classic 의 약 절반(시간 0.4배, 사용량 0.5배). 혼자 대비는 시간 1.5배·사용량 1.6배로 **목표(1.5배 이하)를 조금 넘었다**. 54번 중 27번이 리뷰 2회차 이상까지 갔다(dispute·sheet 는 0번, backlog·large·hard-b 는 대부분).
- 리뷰가 고친 것: 예) "버전 2 파일의 선언되지 않은 창고 참조도 거부해야 함", "이항 연산자 오른쪽의 NOT 식 허용", "기존 부분 환불의 실제 지급액 반영". 숨은 테스트 밖의 실제 결함으로 보이지만 숨은 테스트 점수로는 잡히지 않는다(혼자도 같은 점수).
- 결론: quick 을 기본으로 채택(정확도 = 최선의 혼자, 비용 = classic 의 절반). 비용 목표를 맞추려면 리뷰 라운드를 줄이는 최적화가 필요하다(예: 리뷰 입력 줄이기, P2 고치기 뒤 재리뷰는 바뀐 부분만). 최적화 단계에서 다룬다.
- Linux 검증은 서버(css53) 장애로 미뤘다.

### 36.13 M31 새 화면 1차 (2026-10-21)
- `app/`(Vite + React 18 + TypeScript)에서 만들고, `npm run build` 결과를 `src/tenure/ui/web/` 에 넣어 파이썬 패키지와 함께 배포한다(사용자는 Node 없이 `pip install` 로 쓴다). 빌드 결과는 저장소에 커밋한다.
- 서버: `/` 는 새 화면(index.html 의 meta 에 토큰을 넣어 줌), `/app/assets/*` 는 자산(해시 이름이라 오래 캐시), 옛 화면은 `/classic` 에 한동안 남긴다(새 화면에 빠진 것이 보이면 비교용). `/app/` 아래는 ui/web 안의 아는 확장자만 준다.
- 화면: 받은편지함(주의 순, 끝난 실행은 열면 seen), 사이드바(머신별 프로젝트·상태 점), 프로젝트(실행 기록·설계 문서), 실행(머리·결과 요약·리뷰 이력·대화 타임라인과 보기·실시간 활동(SSE)·지적·콘솔·변경 사항·설계 문서, 이어하기·중지), 새 실행(폴더 고르기·첨부·워크플로 auto 기본·자세히(역할·라운드·모델·추론 강도·고급 설정)), 사용량, 벤치마크, 환경·로그인, 설정(시스템·밝게·어둡게 테마, 작업 폴더 위치, 기본 실행 설정, 고급 설정, 원격 머신, UI 서버).
- 함께 고친 것: M30 의 API 3개가 HTTP 경로표에 빠져 있던 것, 새 실행에서 classic·auto 를 넘기던 문제, 이름 바꾸기 전 기록 머리말(`<!-- duet`)을 못 읽던 것, M30 전 실행이 받은편지함을 채우던 것(이벤트 기록이 없으면 본 것으로), 벤치마크 색인 동시 쓰기로 깨지던 것(임시 파일 후 교체, 깨진 파일에서 경로 건지기), 윈도우 경로를 작업 폴더 위치로 저장 못 하던 것.

### 36.14 M32 worktree·리뷰·병합의 구체안 (2026-10-21)
**worktree (설정 `worktree`, 기본 `auto`)**
- `auto`: 작업 폴더가 커밋이 하나 이상 있는 git 저장소면, 실행마다 `<프로젝트>/.tenure/worktrees/<실행 id>` 에 worktree 를 만들고 브랜치 `tenure/<실행 id>` 에서 일한다(기준은 그때의 HEAD 와 그 브랜치 이름). 새 폴더·git 이 아닌 폴더·커밋 없는 저장소는 지금처럼 그 폴더에서. `off` 는 항상 그 폴더에서. 벤치마크는 `off`.
- 실행 기록은 worktree 안(`<worktree>/.tenure/runs/<id>`)에 그대로 두고(기존 코드가 그대로 동작), 프로젝트의 `.tenure/worktrees.json` 에 색인한다(실행 id, 경로, 브랜치, 기준 브랜치·커밋, 만든 시각, 상태 open/merged/discarded). 화면·API 는 프로젝트의 실행 목록에 worktree 의 실행을 합쳐 보이고, 그 실행의 git·콘솔·이벤트·잠금은 worktree 경로로 본다.
- 한 폴더에 실행 하나라는 잠금은 worktree 마다라, 한 프로젝트에서 여러 작업을 동시에 돌릴 수 있다.
- `.gitignore` 된 파일 중 필요한 것(`.env*`, 프로젝트의 `.tenureinclude` 에 적은 glob)은 새 worktree 로 복사한다.

**병합 준비 점검** (보여 주기만, 병합은 사람이 누름): 항목과 심각도(막음/주의/통과)
- 충돌(`git merge-tree --write-tree 기준 브랜치 작업 브랜치`, 막음) · 실행이 끝나지 않음(막음) · 리뷰 미승인(주의) · 마지막 테스트 실패(막음)·안 돎(주의) · worktree 에 커밋 안 한 변경(주의) · 프로젝트 쪽 작업 트리가 더러움(막음, 병합이 그 트리를 건드리므로) · 기준 브랜치가 앞서 감(주의, 충돌 없으면 그대로 합칠 수 있음) · 변경 없음(막음).

**병합 `tenure merge --workdir <프로젝트> --run <id> [--mode squash|merge]`** (기본 squash)
- 점검에서 막음이 있으면 하지 않는다. 프로젝트(메인 worktree)에서 기준 브랜치를 체크아웃한 상태여야 한다.
- squash: `git merge --squash tenure/<id>` 후 한 커밋(메시지: 과제 첫 줄 + 실행 id), merge: `git merge --no-ff`. 끝나면 색인 상태 merged, worktree 는 지운다(브랜치는 남긴다). 실패하면 `git merge --abort` 로 되돌린다.
- 충돌이 있으면: 충돌 파일 목록과 함께 멈춤. '에이전트에게 해결 맡기기' = worktree 에서 기준 브랜치를 merge 해 충돌 표시를 만든 뒤 구현 담당에게 해결·커밋을 맡기고 테스트를 돌린다(`tenure resolve`).
- 버리기 `tenure discard`: 기준 대비 변경과 커밋 안 한 변경을 `.tenure/discarded/<id>.patch` 로 남기고 worktree 를 지운다. 브랜치는 지우지 않는다.

**줄 메모 → 에이전트** (Daintree·Orca·Schaltwerk)
- 변경 사항 탭에서 줄(파일·줄 번호·쪽)을 눌러 메모. 실행 폴더의 `notes.json` 에 저장하고, 줄 내용 해시를 함께 둬 줄이 바뀌면 '오래됨'으로 표시한다.
- '에이전트에게 보내기'(확인을 거침) = `tenure notes --workdir <코드 폴더> --run <id>`: 열린 메모를 정해진 형식(파일·줄·내용·메모)으로 구현 담당에게 보내 고치게 하고, 커밋·테스트를 돈 뒤 메모를 '보냄(라운드 N)'으로 표시한다. 다시 리뷰할지는 사람이 정한다(이어하기·새 실행).

**구현하며 정한 것** (2026-10-21, M32)
- `--design-only` 실행은 worktree 를 만들지 않고 프로젝트 폴더에서 돈다(만든 DESIGN.md 를 다음 실행이 바로 쓰게).
- 화면이 실행 id 를 미리 정해 `tenure run --run-id` 로 넘긴다. 한 프로젝트에서 여러 작업이 동시에 돌아도 새 실행 창이 방금 시작한 실행으로 간다. id 는 프로젝트 기록·worktree·브랜치(`tenure/<id>`) 어디와도 겹치지 않게 고르고, 정한 id 가 이미 쓰였으면 실행하지 않는다.
- worktree 실행이면 화면의 '이 폴더에서 이미 실행 중' 막음을 쓰지 않는다. 작업 기록(jobs)에 그 작업이 쓰는 코드 폴더(scope)를 적어, 같은 worktree·같은 폴더의 작업만 겹치지 않게 본다. 중지·이어하기·결과 요약도 그 실행의 코드 폴더 잠금을 본다.
- 프로젝트 쪽 작업 트리 점검은 둘로 나눈다: 추적 파일 변경, 또는 병합할 파일과 같은 이름의 추적하지 않는 파일은 막음, 겹치지 않는 추적하지 않는 파일은 주의.
- 병합·버리기 때 실행 기록(`<worktree>/.tenure/runs/<id>`)을 프로젝트의 `.tenure/runs/` 로 옮긴 뒤 worktree 를 지운다. 병합 뒤에도 화면에서 대화·변경 사항을 볼 수 있다(변경 사항은 남겨 둔 브랜치 기준). 병합 때 worktree 에 커밋 안 한 변경이 있으면 `.tenure/discarded/<id>-uncommitted.patch` 로 남긴다.
- 병합 커밋은 사용자의 git 이름·메일로 만든다(사람이 누른 병합이라서). 설정이 없으면 tenure 이름. 에이전트 라운드의 커밋은 지금처럼 tenure 이름.
- 후속 라운드(충돌 해결·줄 메모)는 `state.followups` 에 {종류, 번호, 기록 파일, 커밋, 테스트} 로 남기고 실행 상태(승인·미승인)·리뷰 회차는 바꾸지 않는다. 병합 점검의 '마지막 테스트'는 후속 라운드의 테스트가 있으면 그것을 본다. 충돌 해결 뒤 충돌 표시가 남으면 병합을 되돌리고 실패로 알린다.
- worktree 를 만들 때 프로젝트의 DESIGN.md 가 커밋 안 된 상태면(설계 생략 등) 그 파일을 worktree 로 가져간다. 내용 비교는 하지 않는다(윈도우 autocrlf 체크아웃은 줄 끝만 달라 늘 다르게 보인다).
- 버린 작업은 받은편지함에 올리지 않는다.
- 함께 고친 버그: 윈도우에서 표준 출력을 NUL 로 보내면 `isatty()` 가 참이라 UTF-8 전환을 건너뛰어 '—' 같은 글자에서 실행이 죽고 상태가 '실행 중'으로 남던 문제. 이제 인코딩이 UTF-8 이 아니면 항상 바꾼다.

### 36.15 M33 경쟁 초안의 구체안 (2026-10-21)
**워크플로 `race`** (내장): build 단계에 `rivals = ["codex"]` 를 주면 경쟁 초안이 된다. 구현 담당과 맞수 둘이 초안을 하나씩 만든다.
1. 기준: 코드 폴더(프로젝트 또는 실행의 worktree)에서 지금처럼 기준 커밋(`tenure: design`)을 만든다.
2. 초안: 기준 커밋에서 초안마다 worktree `<코드 폴더>/.tenure/drafts/<실행 id>-<역할>`, 브랜치 `tenure/<실행 id>-<역할>` 을 만든다(무시 파일 복사 포함).
3. 두 초안을 **동시에** 1라운드씩 돈다: 구현 → 커밋 → 테스트 → **상대가 리뷰**(지금 리뷰 기계 그대로: JSON 판정·관문·지적 원장). 초안마다 기록은 `<실행 폴더>/drafts/<역할>/`(자기 run.json·대화 기록). 초안 라운드에는 중재·브라우저 확인을 쓰지 않는다(병렬로 앱 서버를 둘 띄우지 않게).
4. 판정(규칙, 모델 호출 없음): 테스트(통과 > 명령 없음 > 실패) → 리뷰 승인 → 막는 지적 수 적음 → 바뀐 줄 수 적음 → 같으면 구현 담당. 판정과 이유를 `state.race` 에 남긴다.
5. 승자 브랜치를 코드 폴더로 fast-forward 하고, 승자 초안의 1라운드(테스트·리뷰·지적 원장)를 이 실행의 1라운드로 삼는다. 승인이면 끝, 아니면 2라운드부터 **승자가 구현·패자가 리뷰**로 지금 build 루프를 그대로 잇는다(상한 `max_review`).
6. 초안 worktree 는 판정 뒤 지우고 브랜치는 남긴다(비교 화면이 브랜치로 차이를 보인다).
- 이어하기: 초안마다 자기 run.json 의 완료 키로 이어 간다(초안 worktree 가 남아 있으면).
- **사람이 고르기** `tenure pick --workdir <프로젝트> --run <id> --draft <역할>`: 끝난 실행에서 코드 폴더를 그 초안 브랜치로 바꾼다. 바꾸기 전 HEAD 는 브랜치 `tenure/<id>-prev-<n>` 로 남긴다. 상태는 그 초안의 리뷰 판정(승인/미승인, 이유 picked).
- 화면: 실행 화면의 '초안 비교' 탭(초안마다 테스트·리뷰·막는 지적·바뀐 줄·이유, 기준 대비 차이를 나란히, '이 초안으로 바꾸기'). 대화 탭에 초안 기록도 시간순으로 보인다.
- 사람이 다른 초안을 고른 실행은 이어하기를 막는다(그 뒤 라운드 기록이 처음 채택된 초안 기준이라 섞인다). 줄 메모(후속 라운드)는 고른 초안의 역할이 구현 담당으로 받는다.
- 초안 라운드의 호출은 이 실행의 사용량(calls)에 합친다(비용 비교가 맞게). 대화 기록 이름은 `drafts/<역할>/NN_*.md` 로 화면·API 가 그대로 연다.
- 벤치마크: `--workflow race` 로 quick·혼자와 비교한다. 실제 측정은 구독을 쓰므로 사용자가 정한 뒤 돌린다. 효과가 있으면 auto 의 선택지로, 없으면 기록만(36.9).

### 36.16 최적화 1차: 측정 (2026-10-21, 사용자 요청으로 M34 전에)
M30 quick 54번의 호출 기록으로 시간·사용량(Codex 한도 환산)이 어디서 나오는지 쟀다. 실행 평균 10.5분·69만, Claude 혼자 6.0분·35만.

| 단계 | 실행당 호출 | 시간 | 환산 | 몫 |
|---|---|---|---|---|
| Claude 구현 | 1.00 | 7.0분 | 45.5만 | 66% |
| Codex 리뷰 | 1.52 | 1.9분 | 10.4만 | 15% |
| Claude 리뷰 반영 | 0.52 | 0.9분 | 7.6만 | 11% |
| 결과 요약(haiku) | 1.00 | 0.4분 | 5.5만 | 8% |
| tenure 테스트 | — | 0.15분 | — | — |

- **구현은 혼자와 같다**: 같은 과제끼리 짝지으면 시간 합 6,533초 대 6,591초, 입력 +13%(웹 검색은 137세션 중 12세션뿐). 차이는 구현 뒤 단계들이다.
- **리뷰**: 1회차 103초·8.2만(강도 high, 입력 중앙값 13.8만 토큰은 대부분 Codex 가 파일을 읽은 것, diff 는 평균 4만 자), 2회차는 이미 medium 이라 20초·4.2만. 2회차로 간 27번은 모두 P2 때문이었고 25번이 고친 뒤 승인. 리뷰 품질을 건드리는 변경(강도 낮추기·재리뷰 생략)은 벤치마크 없이 하지 않는다.
- **결과 요약**: 몇 줄을 쓰는 데 입력 16만 토큰 — 작은 모델이 README·설정 파일을 도구로 찾아 읽느라 턴이 많다. 가장 싸게 줄일 수 있다.
- **화면**: 사용량 API 가 2.6초(7일 치 Claude 세션 기록 6.8만 줄을 매번 다시 파싱). 나머지 API 는 0.1초 안.

**1차로 하는 것**
1. 결과 요약에 필요한 자료(파일 목록, README 앞부분, package.json scripts·pyproject 진입점·Makefile 대상, 테스트 명령·결과, 구현 메모 앞부분, 미승인이면 마지막 리뷰의 '반드시 고칠 것')를 tenure 가 미리 모아 프롬프트에 넣고, 부족할 때만 파일을 더 읽게 한다. 목표: 요약 환산 5.5만 → 1.5만 이하, 시간 절반.
2. 사용량 집계: 세션 파일마다 (경로, 크기, 수정 시각)으로 파싱 결과를 기억하고, 덧붙은 부분만 읽는다. `"usage"` 가 없는 줄은 JSON 으로 풀지 않는다. 목표: 두 번째 호출부터 0.1초 안.
3. 효과 확인: 요약은 기존 실행 몇 개에서 다시 만들어(작은 모델 호출) 입력 토큰을 비교한다. quick 전체 비용 목표(혼자의 1.5배 이하)는 요약만 줄여도 1.62배 → 약 1.47배로 들어온다(계산).

### 36.17 M33 측정: race (2026-10-21)
기존 과제 18개 × race 3회(Windows, 동시 9개, 초안까지 에이전트 최대 18개). 51번 끝남 — ratelimit 3번은 Codex 구독 한도에 걸려 재개 대기 중이라 빠졌다. 비교는 같은 과제의 이전 결과(36.12).

| 방식 | 실행 | 숨은 테스트 통과율 | 전부 통과 | 평균 시간 | 평균 사용량 환산 |
|---|---|---|---|---|---|
| Claude 혼자 | 42 | 99.92% | 36/42 | 6.1분 | 35.5만 |
| Codex 혼자 | 42 | 99.48% | 29/42 | 5.9분 | 23.2만 |
| classic | 37 | 99.85% | 32/37 | 23.2분 | 115.6만 |
| quick | 42 | 99.92% | 36/42 | 9.2분 | 57.1만 |
| **race** | 42 | **99.90%** | 37/42 | **8.1분** | 60.6만 |

(다섯 방식이 모두 있는 14개 과제. backlog 3개를 넣은 혼자·quick·race 비교도 같은 모양: race 99.90% · 8.8분 · 69.5만, quick 99.93% · 9.9분 · 62.4만.)

- **정확도: quick 과 같다.** 나아진 곳은 minisql 1번(461/461, 지금까지 어느 방식도 못 맞힌 복합 질의 열 이름), 나빠진 곳은 issuetrack 3번 모두(129/131): Codex 초안이 채택돼 Codex 의 CSV 라벨 실수를 물려받았다(quick 은 Claude 구현이라 이 실수가 없음).
- **비용·시간: quick 대비 사용량 1.06배, 시간 0.88배.** 초안 둘을 동시에 돌리고, 51번 중 42번이 1라운드에서 끝났다(quick 은 절반이 2라운드). 두 번째 구현의 비용을 고치기 라운드가 줄어든 만큼 거의 메웠다.
- **판정의 치우침**: 승자 Codex 39 · Claude 12. 이유는 상대 리뷰 승인 25, 작은 변경 21, 막는 지적 적음 5. Claude 리뷰어는 Codex 초안을 자주 승인하고 Codex 리뷰어는 Claude 초안을 자주 반려한다(리뷰어 엄격도 차이). 그래서 혼자 정확도가 낮은 Codex(99.48%)가 더 자주 뽑히고, 리뷰가 못 잡는 실수(issuetrack)는 그대로 남는다.
- **결론(36.9 기준)**: 정확도 이득이 없어 auto 의 선택지로 넣지 않고 **옵션으로 남긴다**(빠른 쪽이 필요할 때: quick 보다 12% 빠르고 6% 비쌈). 판정을 고칠 여지: 리뷰어 엄격도 차이를 보정하거나, 초안마다 상대가 쓴 테스트를 돌려 보는 교차 테스트를 판정에 넣는 것. 벤치마크로 확인하기 전에는 바꾸지 않는다.

### 36.18 최적화 2차: CPU·메모리 (2026-10-21)
**동시 에이전트 상한** (설정 `max_agents`, 기본 0 = 자동: 논리 코어의 절반, 2~8. 이 PC(16)는 8)
- 이 머신의 모든 tenure 프로세스(실행·경쟁 초안·벤치마크·자동 선택)가 `$TENURE_HOME/slots/slot-<n>.lock` 을 나눠 쓴다. 에이전트 CLI 를 띄우는 동안만 OS 파일 잠금 하나를 잡고, 자리가 없으면 1초마다 다시 보며 기다린다(로그: "동시 실행 상한 N개가 모두 쓰이는 중이라 기다립니다"). 프로세스가 죽으면 OS 가 잠금을 풀어 슬롯이 새지 않는다. 한도 대기·테스트는 슬롯을 잡지 않는다. mock 은 세지 않는다.
- 전에는 race 벤치마크 9묶음이 초안까지 에이전트를 최대 18개 띄웠다(측정 때 claude.exe 21개 4.2GB). 이제 상한 안에서만 뜬다. 대신 상한보다 많이 띄우던 벤치마크는 그만큼 오래 걸린다.

**대시보드 읽기 캐시** (`ui/cache.py`): 화면은 4~5초마다 프로젝트 목록·받은편지함을 부른다. 가짜 프로젝트 5개 × 실행 40개(실제 벤치마크 run.json, 평균 18KB)로 쟀다.

| | 전 | 후 |
|---|---|---|
| 폴링 한 번(projects + inbox) | 684ms (4초마다면 코어 하나의 17%) | 40ms (1%) |
| 프로젝트 실행 목록 | 181ms | 15ms |
| 실행 하나 | 54ms | 10ms |
| 캐시가 쥔 메모리(실행 200개) | — | 2.8MB |

- 느렸던 곳(프로파일): 활동 상태를 구할 때 상태 전체를 `to_dict`(호출 기록까지 깊은 복사), 실행 폴더마다 run.json 존재 확인, 이벤트 기록 끝 다시 읽기, run.json 다시 풀기, 받은편지함이 프로젝트 목록을 한 번 더 계산.
- 고침: 파일의 (수정 시각, 크기)가 그대로면 기억한 값을 쓴다(목록용 작은 요약 4096개, 실행 화면용 전체 상태는 최근 64개, LRU). 실행 폴더 목록은 폴더 수정 시각으로 기억하고, run.json 이 아직 없던 폴더만 다시 확인. 잠금 확인은 1초 동안 같은 답. 받은편지함은 등록 목록을 바로 돈다. 활동 판단에는 필요한 네 칸만 넘긴다.
- 쓰기(본 것 표시 등)는 캐시를 거치지 않고 파일에서 읽어 고친다. 파일이 바뀌면 다음 읽기에서 새로 읽는다.

**캐시 크기 상한**: 사용량 집계의 파일별 기억은 7일이 지났거나 지운 파일 것을 버린다. 리뷰 판정 캐시는 2048개를 넘으면 비운다. 화면은 이미 탭이 숨으면 폴링하지 않는다.

### 36.19 M34 대화형 터미널의 구체안 (2026-10-21)
사람이 Claude Code·Codex 를 터미널로 직접 부리고, 그 세션의 상태(일하는 중·나를 기다림·입력 기다림·끝)를 화면·받은편지함에서 본다.

**세션** (`term/` 모듈, UI 서버 프로세스가 가짐)
- 가상 터미널: Windows 는 ConPTY(`pywinpty`, 윈도우에서만 의존성), 그 밖은 `pty`. 띄우기는 `runner.spawn_pty` 를 지난다. 크기는 화면에 맞춰 바꾼다.
- 띄우는 것: `claude` 또는 `codex`(대화형). 작업 폴더는 프로젝트 또는 실행의 코드 폴더(worktree). 임의 명령·셸은 받지 않는다. 이 머신에서만(원격 머신 세션은 나중에).
- 기록: `$TENURE_HOME/sessions/<id>/` 에 meta.json(에이전트·폴더·상태·시각), output.log(원본 출력, 1MB 넘으면 앞을 버림), hooks.jsonl. UI 서버를 다시 켜면 끝난 세션으로 보인다(프로세스는 서버와 함께 끝남).

**상태 판별 3단** (Orca·herdr·Emdash)
1. 훅: Claude 는 `--settings` 로 SessionStart·UserPromptSubmit·PreToolUse·Notification·Stop 훅을 호출마다 붙인다(사용자 설정 파일은 안 고침). Codex 는 `-c notify=[...]`(턴 끝). 훅 명령은 `python -m tenure _hook <세션 폴더> <nonce> <이벤트>` 로 hooks.jsonl 에 한 줄 쓴다. nonce 가 다르면 버린다(옛 세션·다른 세션).
   - 매핑: UserPromptSubmit·PreToolUse → 일하는 중(도구 이름), Notification 의 권한 요청 → 나를 기다림(승인), 그 밖의 Notification·Stop·SessionStart → 입력 기다림.
2. 화면 규칙: 출력 끝부분(제어 문자를 지운 마지막 N줄)에 정규식. 내장 규칙 + `$TENURE_HOME/screen_rules.toml`(우선순위·에이전트·줄 수·정규식·상태·승인/거절 키). 훅이 2분 조용하거나 Codex 승인처럼 훅이 없는 상태에 쓴다. `tenure explain --session <id>` 와 화면의 '왜 이 상태?'가 맞은 규칙을 보여 준다.
3. 프로세스 종료: 끝남(종료 코드).
- 규칙이 틀릴 수 있어(CLI 화면은 버전마다 바뀜) 규칙 파일로 고칠 수 있게 하고, 승인 카드에는 늘 '터미널 열기'를 함께 둔다.

**입력**
- 화면의 키 입력은 그대로 넘긴다. '메시지 보내기'는 bracketed paste(`ESC[200~ … ESC[201~`) 뒤 잠깐 쉬고 Enter. 나를 기다림(승인 대기) 상태면 거부한다(엉뚱한 답이 되지 않게, 강제로 보내기는 확인 뒤).
- 승인·거절: 맞은 규칙의 키(Claude: Enter / Esc, Codex: y / Esc 기본값)를 보낸다.

**화면**: 터미널(xterm.js), 세션 목록(사이드바), 받은편지함의 승인·질문 카드(승인·거절·열기), 프로젝트·실행 화면의 '터미널로 열기'. 알림은 상태가 나를 기다림·입력 기다림(일한 뒤)으로 바뀔 때만, 지금 보고 있는 세션은 빼고 브라우저 알림(설정에서 켬).

**에이전트 설정 보기**(NotchCrew 아이디어, 보기 전용): 그 폴더에서 Claude·Codex 가 실제로 읽는 것 — 사용자·프로젝트 설정(권한·훅·환경 변수 이름), CLAUDE.md·AGENTS.md, 스킬·에이전트·명령, 플러그인, MCP 서버(이름·명령, 비밀값은 가림). tenure 실행에서 끌 항목 고르기는 나중에.

**의존성**: `pywinpty`(Windows ConPTY, MIT), 화면 `@xterm/xterm`·`@xterm/addon-fit`(MIT, 빌드에 들어가므로 THIRD_PARTY_NOTICES 에 적음).
**테스트**: 진짜 CLI 대신 가짜 대화형 CLI(입력을 읽고, `--settings` 의 훅을 실제로 부르고, 권한 질문을 그리는 스크립트)로 세션·훅·규칙·입력을 끝까지 확인한다.

**구현하며 정한 것** (2026-10-21, M34)
- 세션이 띄우는 실행 파일은 실행과 같은 설정(`claude.bin`·`codex.bin`)을 따른다. (테스트에서 PATH 를 비웠더니 tenure 가 덧붙이는 흔한 설치 경로에서 진짜 claude 가 대화형으로 잠깐 떴다 — 입력은 보내지 않았다. 그 뒤 테스트는 없는 경로를 직접 준다.)
- Claude 훅에서 PreToolUse 는 뺐다(도구마다 0.1~0.2초 늦어짐). 승인 뒤 다시 일하는 것은 '승인·보내기 뒤의 출력에 질문이 다시 없는가'로 안다. 화면 규칙은 늘 마지막 사건(훅·승인·보내기) 뒤의 출력만 본다 — 출력 끝에는 이미 답한 질문이 한동안 남아 있기 때문.
- 사용자의 Codex 설정에 `notify` 가 있으면 세션의 `-c notify` 가 그것을 덮으므로, 우리 훅이 원래 명령도 이어서 부른다(기다리지 않음).
- ConPTY 는 프로세스가 끝나도 읽기가 몇 초 늦게 끝난다 → 끝남은 프로세스 생존으로 판단하고 남은 출력은 마저 읽는다.
- `tenure explain --session <id>` 는 기록(출력·훅)으로 보여 준다. 진행 중인 세션은 UI 서버가 가지므로 화면의 '왜 이 상태?'가 실시간이다.
- 화면: 세션 화면(xterm)은 무거워 열 때만 불러온다(첫 화면 294KB 유지). 세션 출력은 SSE(쿠키 토큰), 키 입력은 15ms 모아 보낸다.
- 동시 세션은 8개까지. 대화형 세션은 동시 에이전트 상한(36.18)에 세지 않는다(오래 입력을 기다리며 슬롯을 쥐지 않게).

### 36.20 M35 데스크톱 앱의 구체안 (2026-10-22)
**결정(사용자)**: Rust·VS C++ 빌드 도구를 이 PC 에 설치해 Tauri 2 로 만든다. 업데이트는 GitHub 릴리스(pyk-kentech/duet)로 자동 업데이트(서명 키를 만들고, 비밀 키는 이 PC 에만. 릴리스 올리기는 요청할 때만).

**구조**
- `app/src-tauri/`: Tauri 2 셸. 창 하나. 처음엔 앱에 든 작은 시작 화면("엔진을 켜는 중…")을 띄우고, 엔진이 준비되면 엔진 주소(`http://127.0.0.1:<포트>/?t=<토큰>`)로 옮긴다. 화면은 지금처럼 엔진이 준다(브라우저로 열어도 같은 화면).
- 엔진 sidecar: 파이썬 엔진을 PyInstaller 로 얼린 폴더(`tenure-engine/`, onedir — onefile 은 켤 때마다 임시 폴더에 풀어 느리고 백신에 걸리기 쉽다)를 Tauri 리소스로 넣는다. 셸이 `tenure-engine ui --no-browser --port 0 --ready-line` 으로 띄우고, 엔진은 준비되면 표준 출력에 `TENURE_READY {"url": …}` 한 줄을 쓴다. 창을 닫으면 엔진을 끝낸다. UI 에서 시작한 실행은 지금처럼 따로 돌아 창을 닫아도 계속된다.
- 앱은 하나만 뜬다(두 번째로 열면 기존 창을 앞으로).
- 설치 파일: NSIS, 사용자 단위 설치(관리자 권한 없이). WebView2 는 윈도우 11 에 들어 있고, 없으면 설치 중에 받는다. Claude Code·Codex CLI 는 앱에 넣지 않는다(환경·로그인 화면이 설치를 돕는다).
- 업데이트: `tauri-plugin-updater` 가 시작할 때 `https://github.com/pyk-kentech/duet/releases/latest/download/latest.json` 을 보고, 새 버전이 있으면 묻고(설치·다시 시작) 받는다. 서명은 `tauri signer` 키.

**얼린 엔진에서 달라지는 것**: `sys.frozen` 이면 자기 자신을 `python -m tenure` 대신 실행 파일로 부른다 — UI 가 띄우는 실행(`_job`), 세션 훅(`_hook`), UI 다시 시작, 폴더 고르기 창(`_pick`), 브라우저 확인의 정적 서버(`_http`). 벤치마크의 pytest 는 PATH 의 python 으로.

**구현하며 정한 것** (2026-10-22, M35)
- 이 PC 에 Rust 1.99.1(MSVC)·VS Build Tools 2022(C++) 설치(winget). 첫 빌드 3분, 설치 파일 18.2MB(엔진 43MB 를 압축), 업데이트 서명(.sig) 함께.
- 엔진 시작 화면 → 엔진 화면으로 옮길 때 WebView2 가 엔진(`127.0.0.1:<임의 포트>`)에 붙는다. 엔진 화면에는 Tauri 명령 권한을 주지 않는다(capabilities 는 시작 화면만). 토큰 없는 API 는 지금처럼 401.
- 엔진은 PyInstaller onedir. 엔진 소스 전체를 데이터로도 넣는다 — 템플릿·워크플로·벤치마크 과제·화면, 그리고 원격 머신 배포용 tenure.pyz 를 앱 안에서도 만들 수 있게. pyz 의 진입점도 `tenure.entry` 로(숨은 명령이 원격에서도 같게).
- 확인한 것(빌드한 앱을 직접 띄워): 엔진이 셸의 자식으로 뜨고 창이 엔진 화면에 붙음, 창을 닫으면 엔진이 끝남, 셸을 강제로 죽여도 엔진이 2초 안에 스스로 끝남(--parent-pid), 두 번째로 열면 새로 안 뜨고 기존 창을 앞으로(엔진 하나).
- 릴리스: `python desktop/build_app.py` 가 설치 파일·.sig·latest.json 을 만든다. GitHub 릴리스(태그 v<버전>)에 세 파일을 올리면 설치된 앱이 시작할 때 묻고 업데이트한다. 올리는 일은 사람이 정한다. 비밀 키는 `~/.tauri/tenure.key`(이 PC 에만, 암호 없음 — 잃으면 기존 설치본은 새 키로 서명한 업데이트를 받지 못한다. 따로 보관할 것).
- 버전은 pyproject.toml·tauri.conf.json·Cargo.toml 이 같아야 한다(빌드 스크립트·테스트가 확인).

### 36.21 화면 2차: NotchCrew 식 작업대와 섬 (2026-10-22)
**계기**: 사용자 — "UI 가 좀 거시기하다, NotchCrew 비슷한 UI 에 다른 UI 들의 장점을 채용". 시안으로는 감이 안 온다며 맡김("알아서 잘 해보셈"). NotchCrew 는 소스가 없어 앱 묶음의 changelog.json(35개 버전) 문구로 화면 구성을 읽었다(아이디어만).

**지금 화면의 문제**: 관리자 페이지처럼 흰 카드를 세로로 쌓아 한눈에 보이는 정보가 적다. 제목이 실행 id·전체 경로, 칩이 많고, 지금 에이전트가 하는 일·기다리는 승인이 묻힌다. 실행과 세션이 따로 논다.

**작업대 (메인 창)**: 세 칸.
- 왼쪽 레일(아이콘): 작업 · 사용량 · 벤치마크 · 환경 · 설정.
- 작업 목록(NotchCrew 홈 + Orca 사이드바): 실행과 대화형 세션을 한 목록에, 프로젝트 폴더별 묶음(접기). 행은 두 줄 — 위: 상태(색+모양) · 과제 첫 줄, 아래: 지금 하는 일(진행 중이면 'claude · 고치기 2회차', 끝났으면 '승인 · 테스트 통과 · 병합 대기'), 오른쪽에 시간. 머리에 상태별 개수(기다림·진행·안 봄), 찾기, 상태 필터, '새 작업'. 승인·질문 카드는 묻는 세션 행 바로 밑(예 1 · 아니요 2 · 열기 — 결정과 이동 버튼의 무게를 다르게).
- 작업 화면: 머리는 과제 첫 줄을 제목으로(실행 id 는 작게), 한 줄 요약(워크플로·회차·모델·토큰·시간·브랜치)과 동작 버튼. 가운데는 대화(타임라인), 결과 요약은 그 끝에.
- 오른쪽 얇은 아이콘 사이드바(NotchCrew 1.12): 변경 사항(파일 수) · 병합 · 초안 비교 · 지적 · 콘솔 · 설계 · 에이전트 설정. 누르면 그 판을 열고 다시 누르면 대화로.
- 아무것도 고르지 않았을 때: 나를 기다리는 것 카드, 바로 시작 칸(과제를 적으면 새 작업 창이 채워져 열림), 최근 끝난 작업.
- 키보드(NotchCrew): ↑↓ 목록 이동, Enter 열기, Esc 목록으로, Ctrl+K 명령 팔레트(작업·프로젝트·화면 찾기), 승인 카드에서 1·2.
- 색: 밝게·어둡게 모두. 상태는 색과 모양을 함께(herdr·색약): 기다림 ◆ 빨강, 진행 ● 파랑(맥박), 안 봄 ● 초록, 실패 ▲ 빨강, 끝 ○ 회색.

**섬 (데스크톱 앱, NotchCrew 의 노치를 윈도우에 맞게)**: 화면 위 가운데에 늘 떠 있는 작은 알약 창(테두리·작업 표시줄 없음). 평소엔 기다림·진행·안 봄 개수와 한 줄 소식. 누르거나 Ctrl+Alt+T 로 펼치면(이미 다른 앱이 쓰고 있으면 Ctrl+Shift+Space. Ctrl+Alt+Space 는 Claude 앱 같은 다른 앱이 차지하는 경우가 많아 뺐다) 작업 목록과 승인 카드(예·아니요·열기). 상태가 '나를 기다림'·'턴 끝'으로 바뀌면 두 줄 배너로 잠깐 펼쳐진다(20초, 지금 보고 있는 작업은 빼고). '열기'는 메인 창을 앞으로 가져와 그 작업을 연다. 화면은 엔진의 `#/island` 를 쓰고, 셸이 창 크기·위치만 바꾼다(섬 창에만 Tauri 명령 두 개를 연다: 펼치기·메인 창 열기).

**바탕 API**: `/api/works` — 프로젝트마다 최근 실행 요약(지금 하는 일 포함)과 상태별 개수를 한 번에. 세션은 지금처럼 `/api/sessions`.

### 36.22 화면 3차: 오르카 식 작업공간 (2026-10-22)
사용자 판단: 2차 작업대는 "토이 프로젝트 같아 실무엔 못 쓸 것 같다". 오르카(MIT) 화면 구조를 바탕으로 다시 짜고 tenure 기능을 그 안에 녹인다. 오르카를 통째로 포크하지는 않는다(사용자 선택, Electron·1만 2천 파일 유지 부담). 코드는 빌리지 않고 구조·치수·색만 따른다(출처 표기는 THIRD_PARTY_NOTICES 에 아이디어로).

**틀** (오르카 AppWorkspaceShell): 왼쪽 작업공간 목록(280px, 220~500 끌어서 조절, Ctrl+B 접기) · 가운데 탭 묶음(둘로 나누기) · 오른쪽 판(350px, 끌어서 조절, Ctrl+L 접기) · 아래 24px 상태 막대. 레일과 '오늘' 화면은 없앤다(사용량·벤치마크·환경·설정은 왼쪽 위 메뉴 줄에서 가운데에 연다).

**작업공간** = 작업 폴더 하나: 등록된 프로젝트 폴더(기본 작업공간) 또는 실행의 worktree. 주소는 옛 주소를 그대로 쓴다(`#/p/<폴더>`, `#/p/<폴더>/r/<실행>`, `?tab=` 로 탭). 세션 주소 `#/s/<id>` 는 그 세션의 작업공간으로 넘긴다.

**왼쪽 목록**: 위에 검색(Ctrl+K)·새 작업·사용량·벤치마크·환경·설정 줄. 그 아래 '작업공간' 머리(폴더 추가·새 작업). 묶음은 주의 순: 고정 · 나를 기다림 · 진행 중 · 끝남(안 봄) · 최근(10개) · 폴더(기본 작업공간). 행은 오르카 카드: 상태 표시 + 제목(13px), 폴더 칩(10px 굵게)과 브랜치(고정폭 11px), 마지막 소식 한 줄(에이전트 · 단계 · 시간, 안 봤으면 굵게). 상태 표시: 진행 = 노란 도는 고리, 나를 기다림 = 주황 물음표, 끝남(안 봄) = 초록 점, 실패 = 빨간 점, 소식 끊김 = 점선 고리, 끝 = 흐린 점.

**가운데 탭**: 실행 작업공간은 '흐름'(머리·결과 요약·대화 타임라인)이 첫 탭, 폴더 작업공간은 '개요'(실행 기록·설계 문서). 그 뒤로 이 작업공간의 에이전트 세션·터미널 탭, 사용자가 연 파일·차이·판(변경 전체+줄 메모·초안 비교·설계·로그·지적) 탭. "+" 메뉴: Claude · Codex(대화형 세션) · 터미널(셸) · 새 흐름 실행. 탭을 옆으로 나눠 두 개를 같이 본다. 탭 줄 오른쪽에 그 작업의 주 버튼(중지·이어하기·합치기).

**오른쪽 판**: 파일(한 단계씩 펼치는 트리, 누르면 파일 탭) · 변경(폴더: HEAD 대비 바뀐 파일, 누르면 차이 탭 / 실행: 기준 대비 파일 목록 + '줄 메모로 보기') · 합치기(worktree 실행) 또는 커밋(폴더) · 에이전트 설정.

**아래 막대**: Claude·Codex 한도(5시간·주 사용률 막대, 80%↑ 노랑 95%↑ 빨강, 모르면 '모름'), 동시 에이전트 자리(쓰는 중/상한), 엔진 연결.

**엔진 추가**: `/api/ws/tree|file|status|diff` (작업공간 폴더 안으로만 경로를 푼다, 파일 1MB·차이 400KB 상한), `/api/limits`(무거운 사용량 합산 없이 한도·자리만), 세션 종류 `shell`(Windows 는 pwsh → powershell, 그 밖은 $SHELL. 훅 없음, 에이전트 세션과 따로 8개까지).

**색**: 오르카의 무채색 토큰(밝음 #fff/#f5f5f5/#e5e5e5, 어두움 #0a0a0a/#171717/#262626, 선 흰색 7%)으로 바꾸고 강조색은 파랑 하나. 글꼴은 Pretendard 13px 기준(한국어).

### 36.23 오르카 포크 (2026-10-22, 사용자 결정)
36.22 는 오르카 배치만 흉내 내 다시 짠 것이라 "오르카 UI 를 바탕으로 발전"이라는 뜻과 달랐다. 사용자 선택: **오르카 앱(stablyai/orca, MIT)을 포크해서 발전시킨다.** 36.20 의 Tauri 셸과 36.21·36.22 화면은 포크가 대신할 때까지(M40) 남긴다.

- **어디에**: `D:\duet\orca` = 오르카 공식 저장소 복제(원격 `upstream`), 작업 브랜치 `tenure`. tenure 저장소의 .gitignore 에 넣어 따로 관리한다(오르카 업데이트를 merge 로 따라가기 위해). 라이선스: MIT 고지를 그대로 두고 THIRD_PARTY_NOTICES 에 적는다.
- **도구**: Node 24 + pnpm 12 (오르카 요구). 시스템을 건드리지 않게 휴대용 Node 를 `.tools/` (git 제외)에 둔다.
- **엔진**: 파이썬 엔진은 그대로. 포크의 main 프로세스가 엔진을 띄우고(개발: `tenure ui`, 설치본: 얼린 tenure-engine) 토큰 HTTP 로 부른다. 원격(SSH) 경로는 엔진 쪽 그대로.
- **지키는 것**: CLAUDE.md 의 '실제 claude/codex 프롬프트 실행 금지'는 포크 개발·테스트에도 적용(오르카 e2e 중 에이전트를 띄우는 것은 돌리지 않는다). 오르카의 원격 측정·크래시 보고·자동 업데이트(stablyai 서버)는 끈다. 사용자 설정(~/.claude, ~/.codex)에 훅을 설치하는 오르카 기능은 기본 꺼짐으로 바꾸고 호출마다 인자로 붙이는 tenure 방식으로 바꾼다.

**단계**
- M36 포크 기반: 복제 · 설치 · 개발 실행 확인 · 원격 측정/업데이트 끄기 · 앱 이름 tenure · 기본 언어 한국어.
- M37 엔진 연결: main 이 tenure 엔진을 띄우고 붙기, 엔진 상태·한도를 오르카 상태 막대에.
- M38 흐름 실행: 새 작업공간 창에 'tenure 흐름'(빠른·경쟁 초안·토론) → 엔진이 worktree 실행, '흐름' 탭(타임라인·리뷰 라운드·결과 요약), 사이드바 카드에 흐름 상태(주의 순).
- M39 리뷰·합치기: 오르카 차이 메모를 tenure 메모 보내기와 잇기, 병합 준비 확인·합치기·버리기, 경쟁 초안 비교 탭.
- M40 섬·설치 파일: 윈도우 섬(항상 위 작은 창), NSIS 설치 파일, tenure 릴리스로 업데이트. 이때 옛 Tauri 셸을 걷어낸다.

**M37 결과 (2026-10-23)**: 포크 `src/main/tenure-engine/` 가 엔진(`TENURE_ENGINE` → 설치본 resources/engine → 개발 체크아웃의 `../.venv`)을 `ui --ready-line --parent-pid` 로 띄우고, `TENURE_READY` 의 주소·토큰으로 `/api/*` 만 대리 호출한다(IPC `tenureEngine:get|restart|request`, 갱신 `tenureEngine:update`). 상태 막대에 연결 점·에이전트 자리, 툴팁에 Claude·Codex 한도(모르면 '모름'). 앱을 강제로 꺼도 엔진은 parent-pid 감시로 끝난다(확인). 오르카 핵심 파일은 시작 한 줄·preload 등록·상태 막대 한 줄만 고쳤다(업스트림 merge 를 쉽게).

**M38 결과 (2026-10-23)**: 계획의 '흐름 탭' 대신 오르카 **오른쪽 판에 '흐름' 판**을 넣었다(탐색기·소스 제어·체크와 같은 줄). 새 탭 종류는 세션 복원·레이아웃·탭 줄 등 오르카 핵심 20여 곳을 고쳐야 해 업스트림 merge 가 어려워지기 때문. 흐름 판: 지금 작업공간 폴더의 흐름 목록, '새 흐름'(과제·워크플로 자동/빠른/경쟁 초안/토론·커밋 안 된 변경 무시·연습 mock) → 엔진 `start_run` 에 `options.worktree=off` (작업공간이 이미 따로 떨어진 worktree), 상태·회차별 호출 기록(펼치면 원문)·결과 요약, 중지·이어하기. 사이드바 카드에 흐름 진행/기다림/실패/끝(안 봄) 배지(엔진 `/api/works` 를 5초마다 한 번만 읽어 나눠 씀). 새 작업공간 창의 흐름 선택은 M39 로 미룬다. mock 흐름으로 끝까지 확인(실제 에이전트는 부르지 않음).

**M39 결과 (2026-10-23)**: (1) 흐름 판에 경쟁 초안 상자: 초안마다 리뷰 판정·테스트·파일/줄 수, 펼치면 기준 대비 차이, '이걸로'로 채택 바꾸기(이전 상태는 브랜치로 남김). (2) 오르카 차이 메모의 보내기 메뉴에 'tenure 흐름으로 보내기 (고치기 회차)': 엔진 새 API `POST /api/flow/notes {project, notes:[{file,line,text}]}` 가 그 폴더의 마지막 끝난 흐름에 메모를 붙이고 notes 후속 회차를 띄운다. 보낸 메모는 지우지 않고 오르카의 Sent 표시(`markDiffCommentsSent`). (3) 새 작업공간 창에 '에이전트 대신 tenure 흐름으로 시작' 토글: 켜면 에이전트를 띄우지 않고(plain 셸) 만든 작업공간에서 과제로 흐름을 시작하고 흐름 판을 연다(연결 지점은 `worktree-creation-completion.ts`, 300줄 제한 때문). 오르카 병합은 오르카 소스 제어·PR 흐름에 맡긴다(작업공간 자체가 worktree 라 tenure 병합은 쓰지 않음). 확인: mock 경쟁 초안 흐름과 고르기, 가짜 CLI 를 쓰는 임시 TENURE_HOME 으로 새 작업공간 → 흐름 끝까지. 차이 메모 보내기는 엔진 테스트로만 확인(화면에서 메모를 만들어 보내는 것은 아직 손으로 안 해 봄).

**M40 결과 (2026-10-23)**: (1) 섬: 포크 main 이 엔진의 `#/island` 화면을 테두리 없는 항상-위 창(380×40 알약 / 460×84 배너 / 460×560 판, 화면 위 가운데)으로 띄운다. 전용 preload(`tenure-island`)가 `__TAURI_INTERNALS__.invoke` 를 흉내 내 섬 화면 코드는 그대로 쓴다(island_mode·open_main 두 명령만). open_main 은 메인 창을 앞으로 가져와 그 폴더의 작업공간을 고르고 흐름 판을 연다. 전역 단축키 Ctrl+Alt+T(막혀 있으면 Ctrl+Shift+Space). (2) 설치 파일: `python desktop/build_desktop.py` = 엔진 화면 → 엔진 얼리기(`build/desktop/dist/tenure-engine`) → 포크 `pnpm build:win`(모바일 웹 묶음 검사 때문에 `orca/mobile` 의존성도 설치) → `orca/dist/tenure-windows-setup.exe`(약 200MB, 서명 없음). 엔진은 resources/engine 으로 들어간다. (3) 옛 Tauri 셸(app/src-tauri, desktop/build_app.py·make_icon.py, @tauri-apps/cli)을 걷어냈다. 엔진 화면(app/)은 브라우저용·섬 화면용으로 남는다.
**사고와 수정**: 처음 만든 설치 파일이 per-user 설치 폴더를 패키지 이름("orca")으로 정해, 이 PC 에 깔린 정식 오르카(Programs\orca)에 겹쳐 깔려 파일을 덮어썼다. 잘못 깔린 것을 지우고 받아 둔 정식 오르카 설치 파일로 다시 설치해 복구했고, 빌더 `extraMetadata.name='tenure'` 로 고쳐 이제 Programs\tenure 에 따로 깔린다. 사용자 데이터는 섞이지 않았다(tenure 는 %APPDATA%\tenure, 오르카는 %APPDATA%\Orca, 데몬도 하위 폴더가 다름).
**남은 것**: 첫 실행 안내 등 화면 곳곳의 'Orca' 이름, 정식 오르카와 같이 쓰는 상위 폴더(`~/.orca`, `AppData\Local\Orca`, 로그 `%APPDATA%\Orca`) 분리, 업데이트(tenure 릴리스) 켜기, 섬 단축키를 실제 키보드로 확인(이 환경의 가짜 키 입력은 Electron 전역 단축키에 닿지 않음 — 최소 Electron 시험으로 확인), 설치본에서 실제 흐름 끝까지.

**정리 (2026-10-23, M40 뒤)**: (1) 정식 오르카와 같이 쓰던 폴더를 분리: `~/.orca` → `~/.tenure-desktop`(자격 증명·키 설정·훅 스크립트 등 16곳), `%LOCALAPPDATA%\Orca`(데몬) → `%LOCALAPPDATA%\tenure`, 로그 기본값 → `tenure`. 원격 호스트 쪽 `~/.orca`(SSH 서버 설치)와 저장소 안의 `.orca/`(프로젝트 설정)는 업스트림 호환 때문에 그대로. (2) 화면 이름: 7개 언어 카탈로그의 'Orca' → 'tenure', 그리고 문구 함수(`translate`·`translateMain`)가 표시 직전에 한 번 더 바꿔 카탈로그에 없는 영어 기본값까지 덮는다. 앱·설치 아이콘과 로고는 tenure 의 t. 모바일 동반 앱 버튼은 업스트림 클라우드를 쓰므로 기본 숨김. (3) 이 모든 전환은 빌드 표시 `TENURE_FORK_BUILD`(electron-vite main·renderer define)가 있을 때만 켜져, vitest 의 업스트림 테스트는 원래 이름·경로를 그대로 본다(관련 테스트 묶음에서 새 실패 없음 확인, 원래부터 이 환경에서 깨지는 것만 남음). 설치본에서 화면 글자에 'Orca' 0건, 데몬 폴더 분리, `~/.orca` 안 건드림을 확인.

**저장소·릴리스 (2026-10-23, 사용자 결정)**: 엔진 저장소 `pyk-kentech/duet` → `pyk-kentech/tenure` 로 이름 변경(옛 주소 자동 연결), m30-engine(M28–M40) 을 master 에 병합해 올림. 포크는 새 공개 저장소 `pyk-kentech/tenure-desktop`(기본 브랜치 tenure, 원격 `upstream` = stablyai/orca, 오르카 CI 가 헛돌지 않게 Actions 끔). 업데이트 주소는 `pyk-kentech/tenure` 릴리스로 바꾸고 업스트림 알림·변경 내역(onorca.dev)은 끔. **설치 파일에 코드 서명이 없어 자동 업데이트는 당분간 쓰지 않는다**(사용자 선택: 수동 설치. 서명 확인을 빼는 것은 보안을 낮춰 하지 않음 — 서명을 붙이면 그때 켠다). 앱 버전 = 오르카 버전 끝자리 ×100 + tenure 회차(`desktop/desktop-revision.txt`, 예: 1.4.214 + 1 → 1.4.21401) 로 오르카가 올라가도 계속 커지게 한다. 첫 릴리스 v0.1.0 = 데스크톱 1.4.21401.

### 36.24 지휘자 + 일꾼 중계 (M41, 2026-10-25 사용자 결정)
사용자 요청: "GPT 창과 Codex 창을 띄워 두고, Codex 의 답과 파일을 GPT 에 넘기고 GPT 의 지시를 다시 Codex 에 넣어 돌리기". 웹 화면 자동화는 비목표(1장, 약관·취약)라 CLI 로 한다. 사용자가 고른 모양은 **화면 두 개 자동 중계**(사람이 보면서 끼어들 수 있는 대화형)다.

- **31.4 와의 관계**: 31.4 는 헤드리스 자동 흐름에서 관리자·작업자 분업(extended)을 없앤 결정이다. 이번 것은 엔진 흐름이 아니라 포크의 대화형 터미널 기능이고 벤치마크 흐름에 넣지 않는다. 그래서 그 결정과 부딪히지 않는다. 엔진 워크플로(36.5)는 그대로다.
- **쓰는 것**: 오르카에 이미 있는 orchestration(지휘자 에이전트가 `orca orchestration …` 으로 일꾼 터미널에 일을 맡기고, `worker_done` 을 기다리고, 결과를 읽는다). 새로 짜지 않는다.
- **시작**: 새 작업공간 창의 tenure 칸이 고르기 셋이 된다: 에이전트(기본) / tenure 흐름(M39) / **지휘자 + 일꾼**. 지휘자 + 일꾼을 고르면 과제와 함께 지휘자·일꾼의 모델·추론 강도, 최대 회차(기본 5, 1~20)를 받는다. 만든 작업공간의 첫 터미널에 지휘자 Codex 를 `-m <모델> -c model_reasoning_effort=<강도>` 와 지휘자 프롬프트로 띄운다(일반 에이전트 시작과 같은 길, 터미널 방식으로 고정).
- **지휘자 프롬프트**(한국어, 한 줄, 큰따옴표 없음): 직접 코드를 고치지 말 것 → `orca skills get orchestration` 으로 지침 읽기 → `run-create` → `orca terminal split --command <일꾼 명령>` 로 옆에 일꾼 띄우고(일꾼 명령은 `[codex …]` 로 보여 주고 지휘자가 따옴표로 감싼다. 사용자의 Codex 기본 인자도 일꾼 명령에 붙인다 — 나뉜 창은 일반 에이전트 시작 길을 타지 않기 때문) `terminal wait --for tui-idle` → 회차마다 `task-create`(명세 계약: 대상·변경·제약·소유·완료 확인) → `worker-start --task --terminal <일꾼>` → `check --wait` → `worker-read` 와 `git diff` 로 확인 → 다음 회차 또는 끝 → `worker-release` → 사용자에게 회차별 결과·남은 문제를 한국어로 보고하고 마지막 줄에 `TENURE-RELAY-DONE: <한 줄 요약>`. 개발 실행에서는 명령 이름이 `orca-dev`.
- **사용자 설정을 건드리지 않는다**: 오르카의 orchestration 스킬 설치(`~/.agents/skills`, `~/.claude/skills` 에 씀)를 쓰지 않고, 지침은 프롬프트가 `skills get` 으로 읽게 한다.
- **회차 상한(진짜로 막음)**: 지휘자 터미널에 `TENURE_RELAY_MAX_ROUNDS=<N>` 를 붙인다. 포크 CLI 가 이 값이 있으면 `task-create` 와 `worker-start --spec`(새 작업을 만드는 두 명령) 전에 묶인 실행의 작업 수를 세고, N 이상이면 한국어 오류로 거부한다(`tenure_relay_round_cap`). 값이 없으면 업스트림 동작 그대로.
- **멈추기**: 지휘자 터미널에서 Codex 를 멈추면(Esc·Ctrl+C·창 닫기) 다음 지시가 나가지 않는다. 일꾼은 오르카의 `worker-stop`/창 닫기. 따로 정지 단추는 만들지 않는다(오르카 터미널이 이미 그 일을 한다).
- **설치본 CLI 고침**: 포크의 윈도우 CLI 실행기(`native/windows-cli-launcher`)가 `Orca.exe` 만 찾아, 실행 파일 이름이 `tenure.exe` 인 설치본에서는 터미널 안 `orca` 가 모든 명령에서 실패했다. `Orca.exe` 다음에 `tenure.exe` 도 찾게 고친다. tenure 터미널은 `ORCA_USER_DATA_PATH=%APPDATA%\tenure` 를 물려받아 정식 오르카가 아니라 tenure 앱에 붙는다(런타임 출처 울타리도 있음).
- **확인**: 실제 Codex 프롬프트는 돌리지 않는다(CLAUDE.md). 프롬프트·명령 조립과 회차 상한은 vitest, 실행기는 설치본 `orca.exe --version`, 화면은 개발 실행에서 가짜 codex 로 작업공간 생성 → 지휘자 명령이 터미널에 들어가는지까지.
- **한계**: WSL 터미널(`orca-ide`)·원격 SSH 작업공간은 이번에 다루지 않는다. Codex 샌드박스가 오르카 런타임 연결을 막으면(`runtime_access_denied`) 지휘자가 권한을 올려 다시 실행해야 한다(지침에 있음).

**M41 결과 (2026-10-25)**: 포크 커밋 `7b45cb2c4c`. (1) 새 작업공간 창의 tenure 칸에 '지휘자 + 일꾼 중계로 시작' 체크(흐름과 둘 중 하나): 지휘자·일꾼 모델(비우면 Codex 설정)·추론 강도, 최대 회차(기본 5, 1~20), 과제. 만들면 지휘자 Codex 가 `-m`·`-c model_reasoning_effort`·`TENURE_RELAY_MAX_ROUNDS`·지휘자 프롬프트로 첫 터미널에 뜬다(`src/shared/tenure-relay.ts`, `quick-startup-plan.ts`). (2) 포크 CLI 회차 상한(`tenure-relay-round-cap.ts`). (3) 윈도우 CLI 실행기가 `tenure.exe` 도 찾는다(가짜 배치로 `tenure.exe` 옆에서 CLI 가 뜨는 것 확인).
**사고와 수정**: 처음 프롬프트는 `--command "codex …"` 처럼 큰따옴표를 넣었는데, Windows PowerShell 5.1 이 네이티브 인자를 큰따옴표 자리에서 쪼개 프롬프트가 여러 인자로 갈라졌다(e2e 의 가짜 codex 가 받은 인자로 발견). 프롬프트에서 큰따옴표를 모두 없앴다.
**확인**: vitest(프롬프트·명령 조립 7개, 회차 상한 6개, 주변 묶음 470개), 타입 검사(cli·web), 번역 검사, e2e `tests/e2e/tenure-relay-composer.spec.ts`(실제 화면에서 모드를 켜고 만들기 → 가짜 codex 가 받은 인자·상한 환경 변수·한 덩어리 프롬프트 확인). 실제 Codex 로 지휘자가 일꾼을 부리는 끝까지의 중계는 돌리지 않았다(CLAUDE.md, 구독 한도).
**남은 것**: 실제 Codex 로 한 번 끝까지 돌려 보기(사용자가 요청할 때), 설치본 다시 빌드(실행기 고침이 설치본에 들어가려면 필요), 중계 중 에이전트 선택 칸이 다른 에이전트를 보여 주는 것(무시되지만 헷갈림).

### 36.25 중계 고치기 (M42, 2026-10-25 사용자 지적 후 결정)
M41 을 써 본 사용자 지적: ① 모델을 손으로 치게 함 ② 최대 회차 5 는 말이 안 됨 ③ (별개) SSH 실행 위치가 안 골라짐 ④ 만들 때 모든 걸 다 정해야 함 — 링크·첨부를 주며 대화로 넘기고 싶다 ⑤ Codex 추론 강도는 low~xhigh 고정이 아니고 모델마다 다르다(Daybreak 포함). ③ 은 중계와 별개라 M42 뒤에 따로 본다.

- **모델·추론 강도**: 앱이 이미 Codex 계정에서 읽어 오는 모델 목록(`agentSession.modelCatalog`, 호스트 스냅숏)을 고르기 칸으로 쓴다. 맨 위는 'Codex 설정 그대로'(인자 없음). 추론 강도는 고른 모델이 지원하는 단계만(예: Sol·Terra·Daybreak 은 low~ultra, Luna 는 max 까지), 기본은 'Codex 설정'. 목록을 못 읽으면 오르카의 씨앗 목록으로 대신한다. 모델 id·강도 값은 형식만 검사하고 그대로 넘긴다(목록에 없는 새 모델도 막지 않음).
- **회차 상한**: 기본 없음. 칸에 숫자를 넣을 때만 `TENURE_RELAY_MAX_ROUNDS` 를 붙인다(CLI 쪽 거부는 M41 그대로). 프롬프트의 회차 문장도 상한이 있을 때만.
- **과제 없이 시작**: 과제 칸은 선택. 비우면 지휘자 프롬프트가 "사용자가 이 창에 과제와 참고 자료(링크·첨부 파일·대화 내용)를 줄 때까지 기다렸다가, 받으면 아래 순서로" 가 된다. 지휘자 창은 그냥 Codex 터미널이라 붙여넣기·파일 끌어넣기가 그대로 된다. 링크를 Codex 가 직접 열 수 있는지는 Codex 의 웹 도구에 달렸다(안 되면 내용을 붙여넣는다).
- **이미 있는 작업공간에서 시작**: 탭 줄의 새 탭(+) 메뉴, 에이전트 목록 아래에 '지휘자 + 일꾼 (tenure)…'. 누르면 같은 설정 창(모델·강도·상한·과제 선택)이 뜨고, 그 작업공간에 새 탭으로 지휘자를 띄운다. 새 작업공간 창의 체크도 그대로 둔다.
- **오르카 핵심 파일 손대는 곳**: 앱 모달 종류에 `tenure-relay` 하나, 루트 모달 자리, 탭 줄 메뉴 한 줄. 나머지는 tenure 파일. (처음엔 `launchAgentInNewTab` 에 `agentEnv` 인자를 더하려 했으나 그 파일이 300줄 제한에 걸려, tenure 쪽 `launch-tenure-relay.ts` 가 composer 와 같은 방식으로 시작 계획을 만들어 새 탭에 넘긴다.)
- **확인**: vitest(프롬프트·인자·환경 변수·모델별 강도), e2e(가짜 codex: 새 작업공간 창에서 목록의 모델 고르기·과제 없이 시작, 탭 줄 메뉴로 기존 작업공간에서 시작), 타입 검사·번역 검사.

**M42 결과 (2026-10-25)**: 포크 커밋 `4425b65a53`. 새 작업공간 창과 탭 줄 메뉴 둘 다에서 같은 설정 창(계정 모델 목록·모델별 강도·상한 선택·과제 선택)으로 지휘자를 띄운다. 공용 `tenureRelayLaunch` 가 프롬프트·인자·환경 변수를 한 번에 만든다.
**확인**: vitest(공용 9개·시작 계획 4개 + 주변 묶음), 타입 검사(web), 번역 검사 3종, e2e 2개(가짜 codex: ① 새 작업공간 창에서 목록의 GPT-5.5/High·일꾼 GPT-5.2 Codex·상한 7·과제 → 인자·상한·프롬프트 확인 ② 기존 작업공간의 새 탭 메뉴 → 설정 그대로·과제 없이 시작 → 상한 없음·`-m` 없음·'기다린다' 프롬프트 확인). e2e 의 모델 목록은 가짜 codex 라 씨앗 목록이다(실제 앱에서는 계정 목록). 탭 줄 묶음의 `cli-chat-resume-run.test.ts` 하나는 'Orca'→'tenure' 화면 이름 바꾸기 때문에 이전부터 깨지던 것(이번 변경과 무관).
**남은 것**: 실제 Codex 로 끝까지 중계(사용자가 요청할 때), 설치 파일에 반영하려면 다시 빌드, SSH 실행 위치(36.25 ③, 다음 작업).

### 36.26 SSH 실행 위치 (M43, 2026-10-25 사용자 결정)
36.25 ③. 새 작업공간 창의 실행 위치에서 SSH 서버(css51~54 등)를 고를 수 없었다. 원인은 둘이다.

1. **tenure 설치본에 원격용 서버 묶음(orcad 템플릿)이 없었다.** SSH 로 연결하면 앱이 이 템플릿에서 대상 OS 용 서버를 꺼내 원격에 깐다. 업스트림은 릴리스 CI 에서 OS·libc 별 컨테이너로 네이티브 파일(node-pty 등)을 컴파일해 템플릿을 만든다. tenure 빌드는 템플릿 없이 포장돼(`[packaged-orcad-template] skipped`) 연결이 원격 설치에서 막힌다. 포크는 릴리스 태그가 아닌 업스트림 main(72864c1844)이라 그대로 가져다 쓸 정식 템플릿도 없다(가장 가까운 1.4.237 과도 원격 쪽 79개 파일이 다르고 Node 고정 버전도 다름).
2. **지금 프로젝트가 git 원격이 없는 폴더다.** 오르카 설계상 원격 없는 폴더 프로젝트(`repo:` id)는 다른 머신에 '프로젝트 위치 지정'을 할 수 없다. 서버에 프로젝트를 따로 추가하거나 git 원격을 붙여 쓴다(업스트림 설계 그대로 둔다).

**결정 (사용자, 방법 A)**: 템플릿을 포크 소스로 만들되, 이 PC 에서 컴파일할 수 없는 네이티브 파일만 이 PC 에 설치된 정식 오르카(1.4.237)의 템플릿에서 가져온다.
- 근거: node-pty 는 N-API 8 빌드로 Node 버전에 묶이지 않고, 패치(`node-pty@1.1.0.patch`)도 1.4.237 과 포크가 같다. JS(서버 묶음)·watcher 패키지(npm, lockfile 무결성 확인)·ripgrep 은 포크 소스로 빌드한다.
- 포크 `config/scripts/tenure-seed-orcad-prebuilds.mjs`: 정식 템플릿에서 대상별 node-pty 파일을 `out/orcad-prebuilds` 로(Windows 는 업스트림처럼 conpty.node 만 정식 것, 나머지는 node-pty 패키지에서), Windows 프로세스 읽기·ACL 애드온을 `.build/` 로 채운다. 그 뒤 업스트림 `build-orcad-template.mjs` 를 그대로 쓴다.
- **win32-arm64 는 뺀다**: 그 대상의 원격용 CLI 실행기를 arm64 용으로 빌드하려면 Rust arm64 표준 라이브러리와 Visual Studio ARM64 빌드 도구(수 GB)가 필요하다. 빠진 대상은 그 OS 서버에 연결할 때 '쓸 수 없음'으로 거절된다. 포장 단계 검사는 `TENURE_ORCAD_TARGETS` 가 있으면 그 목록만 본다(없으면 업스트림처럼 전부).
- `desktop/build_desktop.py`: 엔진 얼리기 뒤 템플릿을 매번 포크 소스로 다시 만들고(`TENURE_ORCAD_TEMPLATE_SOURCE` 로 정식 템플릿 위치를 바꿀 수 있음), `ORCA_REQUIRE_ORCAD_TEMPLATE=1` 로 템플릿 없는 설치 파일을 만들지 않는다.

**M43 결과 (2026-10-25)**: 포크 커밋 `6f88793ab6d`. 템플릿 빌드 성공(8개 대상: darwin-arm64/x64, linux-arm64-glibc/musl, linux-x64-glibc/musl, linux-x64-glibc217, win32-x64, 78MB, `verify-packaged-orcad-template` 통과), 설치 파일 210.8MB 에 템플릿 포함·포장 검사 통과. 업스트림 템플릿 검사 테스트 19개 통과.
**실제 연결 확인 (2026-10-25, 사용자 요청)**: 새 설치 파일을 설치하고 css53 에 연결.
- 처음엔 실패했다. 원인은 템플릿이 아니라 **키 형식**: css53 키가 암호 없는 PKCS#8(`BEGIN PRIVATE KEY`)인데, 앱의 SSH 라이브러리 ssh2 는 이 형식을 'Unsupported key format' 으로 거부한다(OpenSSH 는 읽어서 `ssh css53` 은 됐다). 업스트림 오르카에도 있는 한계.
- 고침(포크 `d7fe26c64e4`): `src/main/ssh/ssh-pkcs8-key.ts` 가 키를 읽을 때 RSA 는 PKCS#1, EC 는 SEC1 PEM 으로 메모리에서 바꿔 ssh2 에 넘긴다(키 파일은 그대로). 키를 읽는 세 곳(`ssh-auth-resolution.ts` 두 곳, `ssh-agent-identity-filter.ts`)에 붙였다. Ed25519·암호 걸린 PKCS#8 은 그대로 둔다.
- 고친 뒤: `ssh.connect` → connected, managed server. 서버에 `~/.orca-remote`(159MB, 포크가 고정한 Node 24.22.1 linux-x64-glibc) 와 `~/.orca` 가 깔리고 서버 프로세스가 돈다. 서버의 시험 폴더(`~/tenure-ssh-check`, git 저장소)를 그 서버의 프로젝트로 추가하고 터미널을 열어 `hostname`·`uname -m`·`pwd` 가 css53 에서 나오는 것을 확인했다(시험 터미널은 닫음, 시험 폴더와 프로젝트는 남김).
- 확인 수단: 앱의 CLI 클라이언트로 런타임 RPC(`ssh.connect`)를 직접 불렀다(연결 실패 이유는 화면에만 방송되고 로그·RPC 에는 일반 문구만 남는다).

**확인 못 한 것**: 화면(새 작업공간 창의 실행 위치)에서 css53 을 고르는 흐름, 정식 오르카가 업데이트돼 네이티브 파일이 바뀌어도 맞는지(node-pty 패치가 바뀌면 다시 확인).

### 36.27 에이전트 상태 되살리기와 흐름 기록 탭 (2026-10-27 사용자 승인)
포크의 탭·카드 상태(작업 중·입력 기다림·끝남)는 오르카가 `~/.claude/settings.json`, `~/.codex/hooks.json` 에 훅을 심어 받는다. 이는 '사용자 설정을 고치지 않는다' 규칙과 부딪혀 tenure 빌드에서 상태 받기가 꺼져 있었다(제목에서 짐작한 상태만 보임). 개선 1·2순위로 다음을 했다(포크 `11a54444398`).

1. **사용자 전역 설정 쓰기 차단**: `user-global-config-guard.ts` 가 홈 바로 아래 점 폴더(`~/.claude`, `~/.codex`, `~/.config/...` 등, `~/.tenure-desktop` 은 제외)를 가리키는 쓰기를 거절한다. 훅 JSON(`writeHooksJson`)과 Codex TOML(`writeTomlConfigAtomically`) 쓰기 지점에 붙이고, 시작 시 Codex 훅 고치기·신뢰 올리기·옛 훅 정리, OpenCode 플러그인 새로 고침, orcad 훅 맞추기는 tenure 빌드에서 건너뛴다.
2. **Claude 훅은 실행마다 인자로**: 엔진이 뜨면 `~/.tenure-desktop/agent-hooks/claude-settings.json`(훅 스크립트 포함)을 만들고, 터미널 환경에 `ORCA_CLAUDE_HOOK_SETTINGS` 를 넣는다. 셸의 `claude` 래퍼(POSIX·fish·PowerShell)가 그 파일이 있고 사용자가 `--settings` 를 주지 않았으면 `--settings <파일>` 을 앞에 붙인다(하위 명령 `claude mcp ...` 처럼 첫 인자가 단어면 붙이지 않음). 훅 경로의 `.orca` 는 `~/.tenure-desktop` 으로 고쳤다.
3. **상태 받기 다시 켜기**: `agentStatusHooksEnabled` 기본값을 켜고, 꺼진 채 저장된 프로필은 한 번만 켠다(`tenureStatusHooksRestored`). 전역 훅 설치 단계(`shouldInstallManagedHooks`)는 여전히 꺼져 있다.
4. **흐름 기록을 탭으로**: 흐름 패널의 각 기록과 결과 요약 옆 버튼으로 오르카의 마크다운 미리보기 탭에 연다(엔진 `TenureRunDetail.run_dir` 사용).

**확인한 것**: 개발 앱에서 설정 파일·훅 스크립트 생성, 새 터미널의 환경 변수, 훅 서버에 손으로 보낸 이벤트(204)와 탭의 알림 표시, 래퍼를 거친 `claude --version`, mock 흐름 기록이 탭으로 열림. 시험 전후로 `~/.claude/settings.json`, `~/.codex/hooks.json` 은 그대로(`~/.codex/config.toml` 은 10-26 에 바뀌었으나 시험 전이고 tenure 흔적 없음).
**확인 못 한 것 / 한계**: 실제 Claude 실행에서의 상태 전환(실제 프롬프트 필요). Codex 기본 계정은 실제 `~/.codex` 를 쓰므로 훅을 붙일 길이 없어 제목 기반 상태만 보인다(관리 계정은 자체 CODEX_HOME). 셸 스냅샷 테스트는 POSIX 전용이라 Windows 에서 건너뜀. 설치 파일에는 아직 들어가지 않았다(다시 빌드 필요).

### 36.28 흐름 결과 리뷰 화면 (2026-10-28 사용자 결정)
**계기**: Emdash·Daintree 를 깔아 비교했다(36.8 참고 도구). 사용자가 Daintree 식 리뷰 화면(왼쪽 바뀐 파일 목록 + 봤음 표시, 나란히/한 줄 보기, 파일 넘기기)부터 가져오기로 했다.
**판단**: 오르카에 같은 일을 하는 '전체 차이 보기'(CombinedDiffViewer: 파일 트리, 봤음, 나란히 보기, 차이 메모)가 이미 있다. 새로 그리지 않고 흐름 결과를 그 화면으로 연다.
- 흐름 패널의 흐름 머리에 '변경 리뷰' 버튼. 엔진이 기록한 기준 커밋(`state.base_commit`, 흐름 시작 때 커밋)부터 지금 HEAD 까지를 오르카 `git:branchCompare` 로 구해 '브랜치 변경' 모드 탭으로 연다(탭 이름은 과제 첫 줄). 기준이 SHA 여도 오르카 비교는 그대로 쓴다(`resolveWorktreeAddBaseRef` 가 못 찾으면 받은 값을 그대로 씀).
- 업스트림 `openBranchAllDiffs` 에 선택 인자(바뀐 파일 목록, 탭 이름)를 더한다. 없으면 업스트림과 같다. 소스 제어 패널의 브랜치 비교 상태는 건드리지 않는다.
- 아직 안 하는 것: 경쟁 초안(race)은 초안마다 브랜치가 따로라 이 버튼 대상이 아님(초안 비교는 기존 판), 회차별 차이, 리뷰 메모를 엔진 노트(`/api/flow/notes`)로 보내기.
**결과 (2026-10-28)**: 포크 `1b207a47840`. 개발 앱에서 mock 흐름(quick, 2회차)의 '변경 리뷰'를 누르면 'tenure: <과제>' 탭에 main.py +2 줄이 나란히 보기로 열림. 경쟁 초안 흐름에서는 버튼이 숨음. 저장소 테스트 7개, 전체 타입 검사 통과. 편집기 테스트 중 노트북·마크다운 2개 실패는 이 변경 전에도 실패(이 PC 문제). 설치 파일에는 아직 안 들어감.

### 36.29 워크플로 노드 편집기 (M44·M45, 2026-10-28 사용자 결정)
**계기**: 사용자 — "뭘 어떻게 해야 하는지 모르겠다. ComfyUI 처럼 템플릿이 있고 노드·엣지를 내가 잇고 고르는 화면, 선 정리(comfyui-zip-ties), 자세한 튜토리얼이 있을 줄 알았다". 두 범위를 설명하자 "1 개발-검사-2 확장-검사" 로 정함. 튜토리얼·선 정리는 추천안(앱 안 따라 하기, 자동 정리 먼저)으로.

**M44 — ① 지금 엔진 형식을 노드로 편집**
- 엔진의 워크플로(19장, `tenure/workflows/*.toml`)는 역할 + 단계 목록이다. 이를 그대로 그래프로 보인다.
  - 역할 노드: 에이전트(claude/codex)·모델·강도·지시문·단계별 프롬프트 템플릿. 출력 포트 하나.
  - 단계 노드: explore(역할 1) · debate(제안자 1~2, 종합 1, 비판 라운드, 결과물) · build(구현자 1, 리뷰어 1~3, 중재자 0~1, 맞수 0~1 = 경쟁 초안, 최대 리뷰, 설계 원천) · audit(감사자 1~3). 역할 칸마다 입력 포트.
  - 선: 역할 → 단계의 칸(누가 그 일을 맡나), 단계 → 단계(순서, 한 줄).
- 템플릿: 내장·내 템플릿·프로젝트 워크플로를 불러와 고치고, 프로젝트(`<폴더>/.tenure/workflows/<이름>.toml`) 또는 내 템플릿(`$TENURE_HOME/workflows`)으로 저장. 이름이 같으면 기존 규칙대로 앞이 가린다.
- 검사는 엔진이 한다(`parse_workflow`, 같은 오류 문구). 화면은 그래프 → 워크플로 dict 변환만.
- 엔진 API: `GET /api/workflow`(정의 원본), `POST /api/workflow/check`, `POST /api/workflow/save`. TOML 쓰기는 표준 라이브러리에 없어 작은 직렬화기를 둔다(형식이 단순: 문자열·정수·목록·표).
- 화면(포크): 흐름 패널 머리의 '워크플로' 버튼과 흐름 시작 창에서 여는 큰 편집 창. 왼쪽 부품 목록(역할·단계 끌어다 놓기), 가운데 캔버스, 오른쪽 고른 노드의 설정. 위쪽: 템플릿 고르기, 자동 정리, 검사, 저장. 시작 창의 워크플로 고르기에 저장한 워크플로도 나온다.
- 선 정리: '자동 정리' 버튼 = 단계는 왼→오 한 줄, 역할은 쓰는 단계 아래 열로 다시 놓고 선은 직각(smoothstep). 노드 위치는 아직 저장하지 않는다(열 때마다 자동 배치).
- 의존성: `@xyflow/react`(React Flow, MIT) — 노드·포트·선 끌기·확대를 직접 짜는 것보다 확실하다. 배치는 형태가 단순해 직접 계산(dagre·elk 안 씀).

**M45 — ② 자유 그래프로 확장** (M44 검사 뒤 따로 설계 보강): 조건 갈림(리뷰 통과/실패, 테스트 결과), 구간 반복(횟수 상한), 나란히 돌리고 합치기. 엔진 실행기를 '단계 목록' 에서 '그래프 따라가기' 로 넓히고 끝나지 않는 반복·끊긴 선 검사를 더한다. 지금 형식(한 줄)은 그 특별한 경우로 계속 돈다.

**뒤로 미룬 것**: 앱 안 따라 하기 튜토리얼(mock 흐름 하나를 끝까지, 노드 화면 사용법 포함), zip-ties 식 선 묶기(아이디어 참고, MIT).
**M44 결과 (2026-10-28)**: 엔진 `workflow_toml.py`(TOML 쓰기), `ui/workflow_api.py`(정의 읽기·검사·저장, `/api/workflow*`), 포크 `2583c1bdb6f`(`components/tenure-workflow/`).
- 개발 앱에서 확인: 템플릿(quick·secure) 불러오기 → 노드 그림, Codex 역할 추가 → 마우스로 감사 칸에 이음 → 검사 통과(`audit(auditor, codex2)`), 빈 칸은 "N번째 단계(감사): "감사자" 칸에 역할을 이으세요", 구현 단계 둘이면 엔진 오류 그대로. 프로젝트에 `my-secure` 로 저장 → 흐름 시작 창에 나옴 → 그 워크플로로 mock 흐름이 토론 → 구현 → 감사를 돌아 승인 완료.
- 고친 것: 역할을 세로로 쌓으면 선이 노드를 가로질러 가로 줄 배치로, 좁은 창에서 전체가 안 보여 최소 확대를 0.2 로, 미니맵은 노드가 적어 뺌. 흐름 기록의 제안·비판·종합·감사 단계 이름 번역.
- 검사: 엔진 테스트 `tests/test_m44.py` 7개 + 전체 통과, 포크 그래프 변환 테스트 4개, 린트·문구 검사·전체 타입 검사 통과.
- 한계: 노드 위치를 저장하지 않음(열 때마다 자동 배치), 템플릿(프롬프트) 고르기·단계별 모델은 화면에서 못 바꿈(파일에 있으면 그대로 보존), 설치 파일에는 아직 안 들어감.

### 36.30 M45 자유 그래프: 갈림·반복·나란히 (2026-10-28 사용자 결정)
**결정**: A. 블록 조립식 + 읽기 블록만 동시 실행. 구현+리뷰 블록 안(구현 → 테스트 → 리뷰 → 고치기 반복, 정체 감지·중재·테스트 지킴)은 그대로 두고, 블록 사이를 잇는 노드를 더한다. B(구현·테스트·리뷰를 낱개 노드로 분해)는 핵심 반복을 다시 짜야 해서 하지 않는다.

**워크플로 형식** (19장 TOML 확장, 옛 한 줄 형식은 그대로 돈다)
- 모든 단계에 `next = "<단계 id>" | "end"` 를 쓸 수 있다. 없으면 목록에서 다음 단계(나란히의 가지는 건너뜀). 이런 키나 아래 새 종류가 하나라도 있으면 '그래프 형식' 이고, 이때는 단계 id 가 전체에서 겹치면 안 된다.
- `branch`(갈림): `check = "approved" | "tests_pass" | "audit_clean"`, `yes`, `no` (대상 id 또는 end). approved = 마지막 리뷰 승인, tests_pass = 마지막 테스트 통과(테스트 없음은 통과 아님), audit_clean = 가장 최근에 돈 감사의 감사자가 모두 '문제 없음'.
- `continue`(이어서 고치기): `implementer`, `reviewers`(1~3), `rounds`(1~5). 끝난 구현+리뷰를 이어받아 고치기 → 테스트 → 리뷰를 rounds 회 더 한다(resume `--max-review` 와 같은 길, 정체 판단은 여기서 다시 셈). build 가 먼저 돌아야 한다.
- `parallel`(나란히): `branches = [<explore·audit 단계 id> 2~4개]`, `next`. 가지는 읽기 전용 단계만, 동시에 돌리고 모두 끝나면 next 로. 파일을 고치는 단계는 나란히 둘 수 없다(한 폴더에서 덮어씀; 고치는 경쟁은 race).
- 반복: 앞쪽 단계를 다시 가리키는 선이 곧 반복. 각 단계는 `max_visits`(1~5, 기본 1)번까지만 돈다. 한도에 닿으면 그 자리에서 끝낸다(끝없는 반복 없음). debate·build 는 한 번만(max_visits 불가).
- 검사: 대상 id 존재, 나란히 가지 규칙, continue·audit_clean·audit 앞에 필요한 단계가 워크플로에 있음, 시작 단계(목록의 첫 단계, 나란히 가지 제외)에서 닿지 않는 단계 없음.

**실행** (`tenure/graph_run.py`): 시작 단계부터 선을 따라간다. 같은 단계를 두 번째로 돌 때는 id 에 `_2` 를 붙여 기록 이름·완료 표시를 따로 둔다. 갈림 결과는 `state.graph.decisions`(`<id>#<회차>`)에 남겨 resume 이 같은 길을 다시 걷는다. 나란히는 스레드로 돌리고, 기록 번호 매기기·상태 저장은 PhaseContext 의 잠금으로 줄 세운다. 끝 상태는 지금처럼 마지막 리뷰 승인 여부로 정한다.

**화면**: 노드 편집기에 '갈림'(예/아니오 출력 두 개, 조건 고르기), '이어서 고치기'(구현자·리뷰어 칸, 회차), '나란히'(가지 출력 여러 개 + 다음) 노드를 더한다. 단계의 오른쪽 점에서 어느 단계로든(뒤로도) 이을 수 있고, 뒤로 가는 선은 다른 색으로 그린다. 노드마다 '최대 반복' 을 고른다. 자동 정리는 시작에서 선을 따라 단(段)을 매겨 왼→오, 갈림 가지는 위아래 줄로.
**M45 결과 (2026-10-28)**: 엔진 `workflow_graph.py`(길 찾기·검사), `graph_run.py`(실행), `workflow.py`(새 종류·키), PhaseContext 잠금, 로그 줄 잠금, `RunState.graph`. 포크 `c2d998cb8da`(`workflow-convert.ts`·`workflow-layout.ts`·`WorkflowEdges.tsx` 등).
- 엔진 테스트 `tests/test_m45.py` 12개: 형식 판별·왕복, 검사 8가지(겹친 id, 없는 대상, 고치는 단계를 나란히, build 반복, 닿지 않는 단계, build 없는 continue·갈림), mock 실행(1회 미승인 → 갈림 아니오 → 이어서 고치기 2회 → 승인 → 감사 둘), 감사 둘이 실제로 동시에 도는지(Barrier), 반복 상한에서 멈춤. 전체 테스트 통과.
- 개발 앱: 갈림·나란히가 든 워크플로를 편집기에서 열면 구현 → 갈림 → 예: 나란히 → 감사 둘 / 아니오: 이어서 고치기 → 주황 점선으로 갈림에 되돌아감으로 그려지고 검사 통과. quick 에서 갈림·이어서 고치기를 넣고 마우스로 '아니오 → 이어서 고치기 → 갈림' 과 역할 칸을 이어 검사 통과. 그 워크플로로 앱에서 mock 흐름이 끝까지 돌아 '승인 완료 · 3회차'.
- 고친 것: 나란히 단계의 두 스레드 로그 줄이 붙어 찍혀 로그 출력에 잠금, 되돌아가는 선이 앞으로 가는 선과 겹쳐 노드 아래로 도는 전용 선, `tenure.workflow.check` 가 이미 '검사' 버튼 문구라 조건 문구는 `cond.*`, 흐름 기록의 '중재' 단계 이름.
- 한계: 구현+리뷰 블록 안(구현·테스트·리뷰)은 낱개로 잇지 못함(결정 A). 갈림 조건은 세 가지뿐. 노드 위치는 저장하지 않음. 설치 파일에는 아직 안 들어감.
- 설치본 확인(1.4.21405, 2026-10-28): 처음 만든 1.4.21404 에서 엔진에 등록되지 않은 작업공간 폴더(데스크톱 앱에서 흔함)면 템플릿 불러오기가 "등록되지 않은 프로젝트" 로 막혀 캔버스가 비었다. 워크플로 API 가 등록 전 폴더도 받게 고쳐(tenure `워크플로 편집기: 엔진에 아직 등록되지 않은…`) 다시 빌드·설치했고, 설치본에서 secure 템플릿 그림·갈림 추가·검사 통과를 확인(저장은 사용자 폴더라 하지 않음).

### 36.31 M46 튜토리얼과 문구 검수 (2026-10-28 사용자 요청)
**요청**: "1. 튜토리얼(한국어) 생성 2. 모든 텍스트 검수하여 품위 없는(싼티 나는) 것 고칠 것".

**문구 기준** (화면·로그·문서 공통)
- 존댓말은 합니다체로 통일하고, 안내·지시는 '~하세요', 짧은 표지는 명사형('검사 통과', '수정 요청'). 해요체·반말·감탄('~네요!', '~거예요!', '껐어요', '이걸로')과 이모지(✅ ⚠️)는 쓰지 않는다. 상태 기호(✓ ✗)는 표·검사 목록에서만.
- 용어: 흐름 → **플로**(워크플로와 맞춤), 고치기 → **수정**, 살펴보기 → **탐색**, 비판 → **비평**, 갈림 → **분기**, 나란히 → **병렬 실행**(가지 → 병렬 단계), 이어서 고치기 → **추가 수정**, 맞수 → **경쟁 상대**, 일꾼 → **작업자**, 연습 실행 → **모의 실행**, 자동 정리 → **자동 정렬**. '막는 지적' 은 엔진 프롬프트·기록에 자리 잡은 용어라 그대로 둔다. 엔진 로그·오류 문구도 같은 말을 쓴다.
- 범위: 포크의 tenure 문구 전부(165개) 다시 씀, 오르카 원본 한국어 16,854개는 자동 검사(해요체·감탄·이모지)로 걸린 13개만 고침(대부분 표준 '~하세요'라 그대로), 엔진 로그의 이모지 결과 줄, 그래프 실행 로그·검사 문구.

**튜토리얼** (앱 안 따라 하기, 한국어 기본)
- 플로 패널 머리의 '사용 안내' 버튼으로 시작한다. 처음 연 작업공간에 플로 기록이 없으면 패널 위에 한 번 권한다(닫으면 다시 안 띄움, 이 PC 의 브라우저 저장소).
- 진행 카드(오른쪽 아래, 단계 n/N, 이전·다음·닫기)와 강조 테두리(짚는 화면 요소 둘레, 나머지는 살짝 어둡게). 단계마다 '대신 해 주기' 버튼으로 탭 열기·예시 과제 넣기·모의 실행 체크를 대신할 수 있다. 카드는 상태 표시줄의 엔진 표시 옆에 늘 붙어 있는 호스트가 그리므로 사이드바 탭을 바꿔도 이어진다. 편집 창이 열려 있어도 카드를 누를 수 있게 창 바깥 클릭에서 카드를 뺀다.
- 단계(14): 소개 → 플로 탭 → 새 플로 → 과제 → 진행 방식 고르기 → 모의 실행 → 시작 → 회차 기록 → 변경 사항 검토 → 워크플로 편집기 열기 → 캔버스(역할·단계·연결) → 분기·추가 수정·병렬 실행 → 검사·저장 → 마무리(실제 실행과 사용량, 상태 창).
- 문서: `docs/tutorial.md`(한국어 설명서, 같은 순서 + 워크플로 파일 형식·자주 묻는 것). README 에서 잇는다.
**M46 결과 (2026-10-28)**: 포크 `59f98fb1b3c`(`components/tenure-tutorial/`, 문구), 엔진(로그·검사 문구·내장 템플릿 설명), `docs/tutorial.md`, README.
- 개발 앱에서 '사용 안내'로 14단계를 끝까지 진행: 단계마다 대상 강조, '패널 열기 · 양식 열기 · 예시 입력 · 모의 실행 선택 · 모의 실행으로 시작 · 편집기 열기 · 검사 실행'이 동작, 대신 시작한 플로가 모의 실행으로 승인 완료, 편집 창 위에서도 카드가 눌리고 창이 닫히지 않음, '마치기'로 닫힘.
- 문구: 포크 tenure 문구 107개 고침(용어 기준 위), 오르카 원본 13개 + 오역 1개('씨 ! 또는 GitLab URL' → 'MR ! 번호 또는 GitLab URL'), 엔진 결과 로그 이모지 → '완료:'/'주의:', 그래프 로그·검사 문구 새 용어, 내장 템플릿 설명에서 '가장 저렴'·설계 번호 노출 정리, README '가장 쌈'. 바뀐 로그 문구를 확인하던 엔진 테스트 6곳을 함께 고침. 엔진 전체 테스트·포크 린트·문구 검사·전체 타입 검사 통과.
- 확인 못 한 것: 처음 쓰는 작업공간에서 뜨는 권유 배너(시험 폴더에 기록이 있어 화면으로는 못 봄). 오르카 원본 한국어는 자동 검사로 걸린 것만 고쳤다(전수 사람 검토 아님). 엔진 브라우저 화면(app/)은 같은 검사에서 걸린 것이 없어 손대지 않음. 설치 파일에는 아직 안 들어감.
- 설치본 확인(1.4.21406): 사용 안내 카드·강조 테두리와 처음 쓰는 작업공간의 권유 배너가 뜸(배너는 사용자 몫으로 누르지 않음). 같은 확인에서 오르카의 상황별 안내(contextual tours) 15단계와 버튼 3개가 번역 없이 영어로 박혀 한국어 화면에 영어로 뜨는 것을 찾아, 위치 기준 번역표(`contextual-tours/tenure-tour-copy.ts`)로 고침(포크 문구 검수 보강 커밋). 배너 제목 '처음 사용하시나요?'(해요체) → 'tenure 시작하기'. 이 두 가지는 1.4.21406 에는 없고 다음 설치 파일부터 들어감.

### 36.32 M47 노드 편집기 디자인과 실행 보기 (2026-10-28 사용자 결정)
**계기**: 사용자 — "ComfyUI 에 비하면 조잡하다, 없어 보인다". 시안 세 번(연결점과 선 어긋남, 머리글 위 선 수정) 끝에 "이렇게 하자" + "전선에 전기가 흐르듯 빛이 이동해 방향을 알려 주고, 박스에 마우스를 올리면 진행 상황을 볼 수 있는 실용적인 화면으로".
- **노드**: 머리글(종류 아이콘·이름) + 행. 역할 노드는 머리글 색이 에이전트(claude 주황, codex 청록), 출력 연결점은 오른쪽 가장자리. 단계 노드는 머리글 색이 종류(작업 블록 보라, 분기·병렬 실행 회색 점선 테두리), 머리글 높이 왼쪽에 '이전', 오른쪽에 '다음'(분기는 행마다 '예'·'아니오', 병렬 실행은 '병렬 단계' 행), 역할 칸은 행마다 왼쪽 연결점(받는 에이전트 색), 설정값은 행에 값 표시(편집은 오른쪽 설정 판).
- **배치**: 역할은 단계 왼쪽 열(처음 쓰는 단계 순), 단계는 시작에서 단을 매겨 왼→오, 분기 가지는 위아래 줄.
- **연결선**: 곡선(베지어). 역할 → 칸은 에이전트 색, 단계 순서는 굵은 회색, 되돌아가는 반복은 주황 점선(노드 아래로 돎). 모든 순서·반복 선에 작은 빛 점이 진행 방향으로 이동한다(SVG animateMotion, 동작 줄이기 설정이면 멈춤).
- **실행 보기**: 플로 패널의 실행에서 '그래프로 보기'를 누르면 그 실행이 쓴 워크플로(`state.workflow`, classic 은 설정으로 만든 정의)를 편집 창에 열고 노드마다 상태를 겹친다: 대기 · 진행 중(테두리 맥박) · 완료 · 실패 · 건너뜀. 지나온 선은 밝게, 안 간 선은 흐리게, 지금 들어가는 선은 빛이 빠르게. 진행 중인 실행은 2초마다 다시 읽는다.
  - 마우스를 올리면 노드 위에 진행 카드: 구현 + 리뷰는 회차별 판정·막는 지적 수·테스트, 추가 수정은 맡은 회차와 판정, 감사는 감사자별 결과, 분기는 회차별 결정, 탐색·설계는 완료 여부, 진행 중이면 지금 호출(에이전트·단계·경과 초).
  - 구현 + 리뷰와 추가 수정의 회차는 완료 표시(`state.done`)가 쌓인 순서로 나눈다(`build` 표시 전의 리뷰는 구현 + 리뷰, 그 뒤 `continue:<id>` 까지는 그 추가 수정).
  - 실행 보기에서는 부품 목록을 숨기고 연결을 막는다. '이 구성으로 편집'을 누르면 일반 편집으로 바뀐다.
- 편집 중(실행 없음)에도 마우스를 올리면 단계 설명과 설정 요약을 보여 준다.
**M47 결과 (2026-10-28)**: 포크 `M47` 커밋(`workflow-node-rows.ts`·`run-progress.ts`·`graph-display.ts`·`use-run-view.ts`·`WorkflowRunBanner.tsx`, 노드·선 다시 씀), `docs/tutorial.md` 5.2·5.3·5.5.
- 개발 앱: rescue-demo 실행을 '그래프로 보기'로 열면 모든 단계에 완료 표시, 분기에서 실제로 간 선만 밝음. '추가 수정'에 마우스를 올리면 '완료 / 3회차까지 진행 / 2회차 · 수정 요청 · 막는 지적 1건 / 3회차 · 승인'. 모의 실행을 시작하자마자 열면 '실시간 갱신 중', 진행 중 단계에 파란 테두리와 깜빡이는 점, 아직 안 간 병렬 실행·감사는 흐림, 끝나면 완료 6개로 바뀜. 일반 편집도 새 디자인으로 그려짐.
- 테스트: 진행 계산(회차를 완료 표시 순서로 나눔, 진행 중·실패·미도달), 지나온 선·역할 선 색 3개 추가, 배치 테스트 갱신. 포크 테스트 82개·문구 검사·전체 타입 검사 통과.
- 고친 것: 연결점 위치가 바뀌어 낡은 안내 문구(목록 도움말·사용 안내·설명서)를 새 위치로, 사용 안내에 '그래프로 보기' 추가, 테스트 결과 단어(passed 등) 번역.
- 확인 못 한 것: 실제(모의 아닌) 실행에서 '현재: 에이전트 · 단계 (n초)' 줄(모의 호출은 즉시 끝나 표시될 틈이 없음). 설치 파일에는 아직 안 들어감.
