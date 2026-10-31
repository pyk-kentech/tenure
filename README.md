# tenure

구독 중인 **Claude Code CLI**와 **Codex CLI**를 API 키 없이 헤드리스로 호출해, 두 모델이
**설계를 토론**하고 **서로의 코드를 교차 리뷰**하며 프로젝트를 완성하게 만드는 로컬 CLI 도구입니다.

- 설계 토론: 각자 설계안 → 상호 비판·개정(N라운드) → 결정권자가 `DESIGN.md` 종합
- 구현 + 교차 리뷰: 구현 담당이 코드 작성 → 리뷰 담당이 리뷰 → 승인될 때까지 수정 반복(최대 M회)
- 모든 발언·판정·커밋이 `<workdir>/.tenure/runs/<run_id>/`에 남고, 중단해도 `tenure resume`으로 이어갑니다.
- 실제 CLI 없이 `--mock`으로 전체 흐름을 확인할 수 있습니다.

데스크톱 앱 사용법은 [사용 설명서](docs/tutorial.md)를, 설계 문서는 [DESIGN.md](DESIGN.md)를 보세요.

## 전제 조건

| 항목 | 요구 사항 |
|---|---|
| Python | 3.11 이상 (런타임 의존성 없음) |
| git | 작업 폴더를 git 저장소로 만들어 라운드마다 커밋합니다 |
| Claude Code | `claude` 명령이 PATH에 있고 **로그인된 상태** (`claude`를 한 번 대화형으로 실행해 로그인) |
| Codex CLI | `codex` 명령이 PATH에 있고 **로그인된 상태** (`codex login`) |

검증된 버전: Claude Code 2.1.300, codex-cli 0.162.1. 플래그가 달라지면 아래 설정으로 덮어쓸 수 있습니다.

## 설치

```bash
git clone <이 저장소> tenure && cd tenure
python3 -m venv .venv && . .venv/bin/activate      # 3.11+ 인터프리터로
pip install -e ".[dev]"
tenure --help
pytest -q                                          # 실제 CLI를 호출하지 않는 테스트
```

**Windows** (10/11, 네이티브)에서도 같습니다. PowerShell 에서:

```powershell
git clone <이 저장소> tenure; cd tenure
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
tenure doctor                                        # git·Claude Code·Codex CLI 설치·로그인 확인
tenure ui                                            # 관리자 UI (브라우저가 열립니다)
```

Windows 판 Claude Code(`irm https://claude.ai/install.ps1 | iex`)와 Codex CLI(`npm install -g @openai/codex`, 또는 설치 프로그램)를 쓰고, 둘 다 로그인돼 있어야 합니다. UI 의 '환경·로그인' 화면에서 버튼으로도 설치·로그인할 수 있습니다(로그인은 새 콘솔 창에서). Windows 에서도 SSH 로 Linux·macOS 서버를 원격 머신으로 붙일 수 있습니다(원격 쪽이 Windows 인 것은 아직 지원하지 않습니다, DESIGN.md 27장).

venv를 활성화하지 않고도 어디서나 `tenure`을 쓰려면 [uv](https://docs.astral.sh/uv/)로 설치합니다
(`~/.local/bin/tenure`에 링크되며, `--editable`이라 저장소를 고치면 바로 반영됩니다):

```bash
uv tool install --python 3.12 --editable /path/to/tenure
```

## 다른 머신에서 쓰기

duet는 실행하는 머신의 `claude`, `codex`를 부릅니다. 그래서 새 머신에도 두 CLI를 설치하고 각각 로그인해야 합니다.
같은 계정으로 로그인하면 구독 사용량 한도도 함께 씁니다.

| 머신에 필요한 것 | 확인 |
|---|---|
| Python 3.11+ 또는 [uv](https://docs.astral.sh/uv/) (uv가 Python을 받아 줌) | `python3 --version` / `uv --version` |
| git | `git --version` |
| Claude Code, 로그인 | `claude --version` 후 `claude` 한 번 실행 |
| Codex CLI, 로그인 | `codex --version`, `codex login` |

설치 방법은 셋 중 하나를 고르세요.

```bash
# A. css53 저장소에서 SSH로 바로 설치 (새 머신이 css53에 SSH 접속 가능할 때)
uv tool install --python 3.12 git+ssh://yonggamman@css53/home/yonggamman/tenure

# B. 패키지 파일 복사 (css53에서 `uv build` 로 만든 dist/tenure-0.2.0-py3-none-any.whl)
scp yonggamman@css53:tenure/dist/tenure-0.2.0-py3-none-any.whl .
uv tool install --python 3.12 ./tenure-0.2.0-py3-none-any.whl
#   uv 가 없으면: python3.11 -m pip install --user ./tenure-0.2.0-py3-none-any.whl

# C. GitHub 등 원격 저장소에 올린 뒤
uv tool install --python 3.12 git+https://github.com/<계정>/tenure
```

- 최신으로 바꾸기: 같은 명령에 `--force`를 붙여 다시 설치합니다.
- 지원 OS: Linux, macOS. Windows는 WSL2 안에서 쓰세요(프로세스 그룹·시그널을 씁니다).
- 실행 기록은 각 머신의 작업 폴더(`.tenure/`)에, 프로젝트 목록은 각 머신의 `~/.tenure/`에 남습니다. 관리자 UI는 그 머신의 프로젝트만 보여줍니다.
- 다른 머신의 UI를 내 PC 브라우저로 보려면: `ssh -L 8787:127.0.0.1:8787 <그 머신>` 후 그 머신에서 `tenure ui --no-browser`.

## 사용법

```bash
# 전체 실행 (설계 토론 → 구현 → 교차 리뷰)
tenure run "마크다운 메모를 태그로 검색하는 CLI" --workdir ./memo-cli

# 긴 과제는 파일로
tenure run @task.md --workdir ./memo-cli

# 설계 토론만 (처음 써볼 때 추천: 구독 사용량이 적음)
tenure run "..." --workdir ./x --design-only

# 사람이 DESIGN.md를 손본 뒤 구현부터
tenure run "..." --workdir ./x --skip-design

# 역할 바꾸기 / 라운드 조절
tenure run "..." --workdir ./x --implementer codex --reviewer claude --debate-rounds 2 --max-review 5

# 중단된 실행 이어하기 (마지막 run 기준, --run <id> 로 특정 run)
tenure resume --workdir ./x

# 리뷰 상한을 다 써서 미승인으로 끝났을 때: 상한을 늘려 마지막 리뷰 반영부터 이어가기
tenure resume --workdir ./x --max-review 3

# 기록 보기
tenure log --workdir ./x            # 최근 run 요약
tenure log --workdir ./x --full     # 전체 발언을 순서대로 출력
tenure log --workdir ./x --all      # 모든 run 한 줄씩

# 작업마다 worktree (커밋이 있는 git 저장소면 기본): 실행은 .tenure/worktrees/<run_id> 의 브랜치 tenure/<run_id> 에서 돌고,
# 프로젝트 폴더는 사람이 병합할 때만 바뀝니다. 끄려면 --set worktree=off
tenure merge --workdir ./x --run <id> --check    # 병합 준비 점검 (충돌·테스트·리뷰·작업 트리)
tenure merge --workdir ./x --run <id>            # 한 커밋으로 병합 (--mode merge 면 병합 커밋)
tenure resolve --workdir ./x --run <id>          # 기준 브랜치와의 충돌을 구현 담당에게 풀게 함
tenure notes --workdir ./x --run <id>            # 화면에서 단 줄 메모를 구현 담당에게 보내 고치게 함
tenure discard --workdir ./x --run <id>          # 버리기 (변경은 .tenure/discarded/ 에 패치로 남음)

# 경쟁 초안: Claude·Codex 가 각자 구현 → 서로 리뷰 → 판정 → 승자가 이어서 (비용은 quick 의 약 두 배)
tenure run "..." --workdir ./x --workflow race
tenure pick --workdir ./x --run <id> --draft codex   # 판정과 다른 초안을 고르기 (이전 상태는 브랜치로 남음)

# 대화형 터미널: 관리자 UI 의 프로젝트·실행 화면에서 '터미널: Claude / Codex' 로 직접 부리고,
# 승인을 기다리면 받은편지함에 카드가 뜬다. 상태 판단이 이상하면:
tenure explain --session <id>                       # 맞은 화면 규칙·마지막 훅 (규칙은 ~/.tenure/screen_rules.toml 로 고침)

# 데스크톱 앱(윈도우) 빌드: 오르카 포크 orca/, 휴대용 Node 24(.tools/node24), VS C++ 빌드 도구, pip install -e ".[desktop]"
python desktop/build_desktop.py                   # → orca/dist/ 의 tenure 설치 파일 (DESIGN.md 36.23)

# CLI 없이 흐름 테스트
tenure run "..." --workdir ./x --mock
```

실행 중 터미널에는 단계별 진행 상황이 한 줄씩 찍힙니다:

```
[17:41:38] 설계안 작성: claude
[17:41:38] claude ← propose 실행 중...
[17:42:19] claude ← propose 완료 (41초) → 01_propose_claude.md
...
[17:55:02] [1/3] 판정: CHANGES_REQUESTED (must_fix=2)
```

### 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 리뷰 승인으로 완료, 또는 `--design-only` 완료 |
| 1 | `max_review` 안에 승인되지 않음 (마지막 리뷰 파일 경로를 출력). `resume --max-review N`으로 이어갈 수 있음 |
| 2 | 사용 오류 (설정 값, 작업 폴더 가드, CLI 미설치 등) |
| 3 | 중단됨 (구독 한도 도달 또는 에이전트 반복 실패) → `tenure resume` |
| 130 | 사용자가 멈춤 (Ctrl-C, 또는 관리자 UI의 중지) → `tenure resume` |

## 환경 점검

```bash
tenure doctor            # git, Claude Code·Codex CLI 설치·로그인 확인
```

빠진 것은 관리자 UI의 **환경·로그인** 화면에서 버튼으로 설치·로그인할 수 있습니다(공식 설치 스크립트만 실행).

## 모델 고르기

```bash
tenure run "..." --workdir ./x --claude-model opus --claude-effort high --codex-model gpt-6.1-sol --codex-effort xhigh
```

비우면 각 CLI의 기본 모델을 씁니다. 매번 쓰기 번거로우면 UI의 **설정 > 기본 실행 설정**에 저장하세요(`~/.tenure/settings.json`).
우선순위는 CLI 인자 > 작업 폴더의 `tenure.toml` > 전역 설정 > 기본값입니다.

Codex 는 기본으로 단계마다 추론 강도를 다르게 씁니다(설계 종합·첫 리뷰 `high`, 나머지 `medium`). `--codex-effort xhigh` 처럼 하나만 주면 모든 단계에 그 값을 쓰고, 단계별로 바꾸려면 `--codex-step-effort review=xhigh,fix=low` 를 씁니다.

## 워크플로 템플릿

역할군과 단계 순서를 템플릿으로 골라 씁니다. 기본은 `classic`(지금까지의 Claude·Codex 토론 → 구현 → 교차 리뷰)입니다.

```bash
tenure workflows                     # 쓸 수 있는 템플릿 목록
tenure workflows show secure         # 역할·단계 자세히
tenure run "..." --workdir ./x --workflow secure
```

| 내장 | 내용 |
|---|---|
| `secure` | classic + 보안 리뷰어(Codex, 보안 전용 프롬프트) + 승인 뒤 Claude 의 전체 감사 |
| `existing` | 기존 코드베이스용: Codex 가 먼저 코드를 조사하고, 이번 변경만 설계해 `docs/changes/<실행 ID>.md` 에 저장(프로젝트의 `DESIGN.md` 는 그대로) → 구현·리뷰 |
| `lean` | 비용이 가장 적음: Claude 혼자 설계(토론 없음) → 구현 → Codex 리뷰 |
| `extended` | 큰 과제용 분업: Codex(high) 관리자가 설계를 작업 묶음(파일 소유)으로 나눔 → Claude Sonnet 작업자가 맡은 파일만 순서대로 구현 → 관리자가 통합 → Codex 리뷰. 수정은 지적의 파일 주인 작업자가 맡습니다 (DESIGN.md 29장) |

직접 만들려면 `<작업 폴더>/.tenure/workflows/<이름>.toml` 또는 `~/.tenure/workflows/<이름>.toml` 에 둡니다. 단계 종류는 `explore`(사전 조사), `debate`(설계), `build`(구현·리뷰, 리뷰어 최대 3명), `audit`(승인 뒤 감사, 참고용)입니다.

```toml
name = "my-team"
description = "Opus 설계자 + Sonnet 구현자 + Codex 두 명 리뷰"

[roles.architect]
agent = "claude"
model = "opus"
effort = "high"

[roles.coder]
agent = "claude"
model = "sonnet"

[roles.reviewer]
agent = "codex"

[roles.security]
agent = "codex"
templates = { review = "review_security" }   # 프롬프트 템플릿 교체
instructions = "인증·권한 코드를 특히 꼼꼼히 봐라."   # 이 역할의 프롬프트 끝에 붙는 지시

[[steps]]
kind = "debate"
proposers = ["architect", "reviewer"]
synth = "architect"

[[steps]]
kind = "build"
implementer = "coder"
reviewers = ["reviewer", "security"]   # 모두 승인해야 승인
max_review = 4
```

자세한 규칙은 DESIGN.md 19장에 있습니다.

## 리뷰 규칙과 테스트

- **duet가 직접 테스트**: 구현이 커밋되면 duet가 테스트를 돌리고 결과를 리뷰어와 구현자에게 보여줍니다. 명령은 `--test-command`, 구현자가 `tenure.toml`에 적은 `test_command`, 자동 감지(pytest·npm·cargo·go·make) 순으로 정합니다. 테스트가 실패하면 리뷰어가 승인해도 승인하지 않습니다.
- **심각도 기준**: 리뷰어가 지적마다 P1(핵심 동작·보안)·P2(설계 위반·일반 입력 오류)·P3(드문 경계 사례)를 붙입니다. 기본은 P1·P2가 없으면 승인합니다(`--block-severity P3`면 P3까지 막음).
- **수렴 안 됨**: 막는 지적 수가 2라운드 연속 줄지 않으면(또는 리뷰 상한에 닿으면) 멈추기 전에 **중재자**를 한 번 부릅니다. 중재자는 열린 지적마다 유지·완화·기각을 정하고, 막는 지적이 남지 않으면 "중재로 승인"합니다. 남으면 그것만 고치는 마지막 라운드를 한 번 더 돌고(상한 안에서), 그래도 안 되면 사람에게 넘깁니다. `--no-arbiter` 로 끌 수 있고, `--stall-rounds 0` 이면 수렴 판단 자체를 끕니다. `tenure resume`으로 이어갈 수 있습니다.

- **재현 테스트**: 리뷰어가 지적을 실패하는 테스트로 내면 duet가 저장해 정말 실패하는지 돌려 보고, 구현자는 그 테스트를 통과시켜야 합니다.
- **요구 근거**: 리뷰어는 지적마다 근거 종류(요구 위반·정확성·보안·취향)를 밝히고, 요구 위반이면 과제·설계 문서의 문장을 그대로 인용합니다. 취향 지적과, 인용한 문장을 과제·설계에서 찾을 수 없는 P2 지적은 P3 로 낮춰 막지 않습니다. `spec_gate = false` 로 끕니다.
- **한도 대기 후 자동 재개**: `--wait-on-limit`(run·bench)이면 구독 한도에 걸렸을 때 멈추지 않고, 메시지에서 읽은 재개 시각(못 읽으면 15→30→60분 간격)까지 기다렸다가 같은 호출을 다시 보냅니다. 한 실행의 대기 시간 합은 `limit_wait_max_min`(기본 360분)까지입니다.
- **재제기·되돌림 방지**: 리뷰어와 구현자에게 이미 해결·기각된 지적을 보여 주고, 새 막는 지적이 그 반복인지 작은 모델(기본 Claude Haiku)이 확인합니다. 기각된 지적의 재제기는 P3 로 낮추고, 해결됐던 지적이 다시 나오면 "재발"로 표시하고, 같은 리뷰어가 ID 없이 다시 쓴 지적은 원래 ID 로 합칩니다. 구현자가 해결됐던 결함의 코드를 되살리면 git 으로 감지해 리뷰어에게 경고합니다. `dedup = false` 로 끄고, 모델은 `dedup_agent`·`dedup_model` 로 바꿉니다.
- **테스트 변조 감지**: 구현자가 테스트 파일을 지우거나, 테스트·단언 수를 줄이거나, skip 을 붙이거나, 테스트 설정을 바꾸거나, 리뷰어의 재현 테스트를 고치면 tenure 가 리뷰어와 중재자에게 경고를 붙입니다(막지는 않음). `test_guard = false` 로 끕니다.
- **Claude 샌드박스(선택)**: `--claude-sandbox`(또는 `claude_sandbox = true`)면 Claude 구현자의 명령을 OS 샌드박스 안에서 돌립니다. Linux 는 `bubblewrap`·`socat` 이 필요하고, `tenure doctor` 가 확인합니다. tenure 가 직접 돌리는 테스트는 샌드박스 밖에서 실행되니, 믿을 수 없는 과제는 컨테이너에서 돌리세요.
- **리뷰 토큰 절약**: 2번째 리뷰부터는 지난 리뷰 이후 바뀐 부분만 보여주고, duet가 이미 돌린 테스트는 다시 돌리지 않게 합니다.
- **리뷰 비용 줄이기**: 리뷰어에게 바뀐 함수 전체와 그 함수가 쓰이는 곳을 미리 보여 주고(`review_packet`), 판정을 JSON 으로 받습니다(`review_json`). 둘 다 `tenure.toml` 에서 끌 수 있습니다. 리뷰어가 2라운드부터 자기 세션을 이어서 리뷰하게 할 수도 있지만(`review_session = "reuse"`), 실측에서 이어간 턴이 캐시를 못 써 구독 한도 기준으로 더 비싸서 기본은 꺼져 있습니다(DESIGN.md 21.3).

```bash
tenure run "..." --workdir ./x --test-command "uv run pytest -q" --block-severity P2 --stall-rounds 2
```

관리자 UI에서 실행 중인 항목을 열면 AI가 지금 어떤 파일을 읽고 고치는지, 어떤 명령을 돌리는지 실시간으로 보입니다.

## 벤치마크

같은 과제 묶음을 워크플로별로 돌리고, 에이전트가 보지 못한 **숨은 수용 테스트**로 결과물을 채점해 비교합니다.

```bash
tenure bench list [--suite hard]                             # 과제 목록 (내장 basic 5개, hard 5개, dispute 3개)
tenure bench run --mock --workflow classic --workflow lean   # 흐름만 확인 (구독 안 씀)
tenure bench run --workflow classic --workflow lean --yes    # 실제 실행 (구독 사용량을 많이 씀)
tenure bench report ~/.tenure/bench/<시각>                     # 요약 다시 보기
```

결과 폴더의 `summary.md` 에 워크플로별 승인률, 숨은 테스트 통과율, "승인했는데 숨은 테스트 실패"(리뷰가 놓친 결함) 수, 평균 라운드·시간·토큰이 나옵니다. 한도에 걸리면 그 자리에서 멈추고 그때까지의 결과를 남깁니다. `basic` 은 쉬운 과제라 워크플로 차이가 잘 안 납니다(실측: classic·lean 모두 100%). `--suite hard` 는 모호한 요구, 여러 모듈, 성능·동시성 제약, 더 큰 기존 프로젝트, 함정이 있는 명세로 이루어져 있습니다. `--suite dispute` 는 리뷰가 요구 밖 지적으로 핑퐁하기 쉬운 과제로, 중재자와 요구 근거 관문의 효과를 봅니다. 직접 만든 과제 묶음은 `--suite <폴더>` 로 줍니다(구조는 DESIGN.md 20장).

## 관리자 UI

```bash
tenure ui                      # http://127.0.0.1:8787/?t=<토큰> 이 열립니다
tenure ui --no-browser         # 주소만 출력
tenure ui --workdir ~/proj     # 목록에 폴더 추가
tenure ui --new-token          # 접속 토큰을 새로 만듦 (예전 주소·즐겨찾기는 막힘)
```

- `tenure run`으로 실행한 폴더는 자동으로 목록에 올라갑니다(`~/.tenure/projects.json`).
- 실행 기록을 보고, 두 모델의 발언을 순서대로 읽고, 라운드별 diff와 진행 로그를 확인할 수 있습니다.
- 브라우저에서 새 실행, 이어하기(미승인이면 리뷰 상한 지정), 중지를 할 수 있습니다. UI를 꺼도 실행은 계속됩니다.
- 처음에는 출력된 주소(`?t=` 토큰 포함)로 엽니다. 그다음부터는 쿠키로 기억해서 `http://127.0.0.1:8787/` 이나 즐겨찾기로도 열립니다. 토큰은 `~/.tenure/ui-token` 에 두고 `tenure ui` 를 다시 켜도 같은 것을 씁니다.
- 목록은 검색할 수 있고, 벤치마크 실행과 폴더가 없어진 프로젝트는 기본으로 숨깁니다('없는 폴더 목록에서 지우기' 버튼은 목록에서만 지우고 파일은 건드리지 않습니다).
- 원격 서버에서 쓸 때는 SSH 터널을 쓰세요: `ssh -L 8787:127.0.0.1:8787 <서버>` 후 로컬 브라우저에서 출력된 주소를 엽니다.

| 화면 | 할 수 있는 것 |
|---|---|
| 프로젝트 | 머신별 프로젝트·실행 기록, 발언 타임라인, diff, 진행 로그, 새 실행(고급 설정 포함)·이어하기·중지. 실행 상세의 **지적** 탭에서 지적 원장(I1, I2 …)·중재 결정·관문 강등·중복 확인·테스트 변경 경고를 봅니다 |
| 환경·로그인 | 설치·로그인 상태 점검, 설치·로그인 버튼(터미널 창에서 코드·비밀번호 입력, Windows 의 로그인은 새 콘솔 창) |
| 벤치마크 | `tenure bench run` 결과: 워크플로별 승인률·숨은 테스트 통과율·승인했는데 실패·라운드·시간·토큰, 실행별 결과에서 그 실행 상세로 이동 |
| 사용량 | Codex·Claude 구독 한도(%)와 다시 풀리는 시각(Claude 는 tenure 가 부를 때 갱신), Claude Code 최근 5시간·7일 토큰, tenure 실행별 토큰 |
| 설정 | 작업 폴더 위치, 기본 모델·추론 강도·역할, **고급 설정**(관문·중재자·중복 확인·안전장치·한도 대기 등, 항목 정의에서 자동으로 그림), 원격 머신 |

고급 설정은 CLI 에서도 같은 키로 줍니다: `tenure run "..." --set arbiter=false --set limit_wait_max_min=120`.

### 원격 머신에서 돌리기

1. 이 머신에서 원격으로 비밀번호 없이 SSH 접속이 되게 합니다: `ssh-copy-id me@lab`
2. UI **설정 > 원격 머신**에 추가합니다(예: id `lab`, SSH `me@lab`, 작업 폴더 위치 `~/tenure-projects`).
3. **환경·로그인 > lab**에서 '배포'를 누릅니다. 원격에 Python 3.11+만 있으면 되고, 없으면 같은 화면에서 uv → Python 3.12를 설치합니다.
4. 같은 화면에서 원격의 Claude Code·Codex를 설치·로그인합니다.
5. **새 실행**에서 머신을 `lab`으로 고르면 그 머신에서 돌고, 기록도 그 머신에 남습니다.

## 기록과 상태

```
<workdir>/.tenure/runs/20261014-174138/
├── run.json                  # 상태 (resume 용)
├── 01_propose_claude.md
├── 02_propose_codex.md
├── 03_r1_critique_claude.md
├── 04_r1_critique_codex.md
├── 05_synthesize_codex.md    # → <workdir>/DESIGN.md 로 복사됨
├── 06_impl1_claude.md
├── 07_review1_codex.md
└── ...
```

- 각 기록 파일 상단에는 에이전트, 단계, 모드, 소요 시간, exit code가 HTML 주석으로 들어 있습니다.
- 호출이 실패하면 `..._failed.md` 로 stderr까지 남깁니다.
- `.tenure/`는 자동으로 `.gitignore`에 추가됩니다.
- 진행 로그는 터미널과 함께 `console.log`에도 남습니다. 기존 `DESIGN.md`를 덮어쓸 때는 `DESIGN.prev.md`로 백업합니다.
- 같은 작업 폴더에서 duet를 동시에 두 개 돌릴 수 없습니다(`.tenure/lock.json`).
- 커밋: `tenure: design`(기준) → `tenure: impl round 1` → `tenure: impl round 2` → ...

## 설정 (`<workdir>/tenure.toml`)

우선순위: CLI 인자 > `tenure.toml` > 기본값. 아래는 기본값 전체입니다.

```toml
implementer = "claude"
reviewer = "codex"
synth = "codex"                  # 생략 시 reviewer
debate_rounds = 1                # 0~5
max_review = 3                   # 1~10
timeout_sec = 1200               # 호출 1회 제한
test_command = "auto"            # tenure 가 돌릴 테스트 (auto: tenure.toml → 자동 감지, none: 끔)
test_timeout_sec = 600
require_tests_pass = true        # 테스트 실패면 승인 안 함
block_severity = "P2"            # 이 심각도 이상 지적이 남으면 반려 (P1|P2|P3)
stall_rounds = 2                 # 막는 지적 수가 N 라운드 연속 안 줄면 멈춤 (0=끔)
review_diff = "incremental"      # 2번째 리뷰부터 바뀐 부분만 (full: 매번 전체)
repro_tests = true               # 리뷰어의 재현 테스트를 저장·실행
max_diff_chars = 60000           # 리뷰 프롬프트에 넣을 diff 최대 길이
prompt_arg_max_bytes = 100000    # UTF-8 바이트. 넘으면 프롬프트를 stdin으로 전달
diff_excludes = ["package-lock.json", "yarn.lock", "uv.lock", "poetry.lock", "Cargo.lock", "go.sum", "..."]  # 리뷰 diff 에서 뺄 잠금 파일 (기본값 일부)

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
```

CLI 버전이 바뀌어 플래그가 달라지면 `claude --help`, `codex exec --help`로 확인해 여기서 고치면 됩니다.

## 프롬프트 커스터마이즈

기본 템플릿은 `src/tenure/templates/*.md`에 있습니다. `<workdir>/.tenure/templates/`에 같은 이름의 파일을
두면 그것을 우선 사용합니다.

| 파일 | 쓰는 곳 | 플레이스홀더 |
|---|---|---|
| `propose.md` | 설계안 작성 | `{task}` |
| `critique.md` | 상대 안 비판 + 개정안 | `{task}`, `{own}`, `{other}`, `{other_name}` |
| `synthesize.md` | 최종 설계 종합 | `{task}`, `{a}`, `{b}`, `{a_name}`, `{b_name}`, `{critiques}` |
| `implement.md` | 첫 구현 | `{task}` |
| `fix.md` | 리뷰 반영 | `{task}`, `{review}`, `{reviewer_name}` |
| `review.md` | 코드 리뷰 | `{task}`, `{diff}`, `{implementer_name}` |

- 중괄호를 문자 그대로 쓰려면 `{{`, `}}`.
- `critique.md`는 응답에 `## 개정안` 헤더를 요구합니다. 그 뒤가 다음 라운드의 설계안이 됩니다(없으면 응답 전체).
- `review.md`는 응답 마지막에 판정 블록을 요구합니다. 레거시 `VERDICT: APPROVED` 한 줄도 인식합니다.

```
<verdict>
status: APPROVED | CHANGES_REQUESTED
must_fix: 0
</verdict>
```

둘 다 없으면 반려로 간주하고, `APPROVED`인데 `must_fix > 0`이면 모순으로 보아 반려 처리합니다.

## 주의사항

- **구독 사용량**: 한 번의 `run`은 `2 + 2×debate_rounds + 1 + 2×(리뷰 반복 수)` 회의 CLI 호출을 합니다.
  처음에는 `--design-only`로 가볍게 시작하세요. 한도에 걸리면 상태를 저장하고 종료하므로 나중에 `tenure resume` 하면 됩니다.
- **작업 폴더 가드**: 홈 디렉터리나 `/`는 거부합니다. 기존 git 저장소에 커밋 안 된 변경이 있으면 거부합니다(`--force`로 무시).
- **리뷰어 쓰기 차단**: 리뷰는 읽기 전용 모드로 호출하고, 리뷰 후 작업 트리에 변경이 있으면 경고 후 되돌립니다(`.tenure/` 제외).
- **구현자는 셸을 실행합니다**: Claude는 `Bash`, Codex는 `workspace-write` 샌드박스로 테스트 등을 실행합니다. 믿을 수 있는 폴더에서만 쓰세요.
- **약관**: 공식 CLI를 본인 PC에서 개인 용도로만 사용합니다. 웹 화면 자동화나 비공식 API 호출은 하지 않습니다.
- 테스트(`pytest`)는 실제 `claude`/`codex`를 호출하지 않습니다(`tests/fake_bin`의 가짜 실행 파일 사용).

## 개발

```bash
pytest -q                                              # 전체 테스트
tenure run "할일 CLI" --workdir /tmp/tenure-try --mock      # 수동 흐름 확인
```

구조와 마일스톤은 [DESIGN.md](DESIGN.md), 작업 규칙은 [CLAUDE.md](CLAUDE.md)에 있습니다.
