# CLAUDE.md

이 저장소는 **tenure**(옛 이름 duet)를 만든다. 무엇을 만드는지는 **DESIGN.md가 기준**이다. 작업 시작 전에 DESIGN.md 를 읽어라(지금 진행 중인 설계는 36장, 최근은 36.36).

## 작업 방식
- 마일스톤을 **순서대로 하나씩** 진행한다. M30–M51 은 끝났다(오르카 포크, 지휘자 + 작업자 중계, SSH 원격용 템플릿, 워크플로 노드 편집기, 자유 그래프, 사용 안내와 문구 검수, 편집기 디자인과 실행 보기, ComfyUI 식 편집 기능, 케이블 타이 선 묶기와 배치 저장, 실시간 진행 보기, 써 보며 찾은 불편 1차까지). 써 보며 찾은 나머지 불편(4~14번)은 DESIGN.md 36.36 '남은 불편'. 미룬 것: 꺾는 점·그룹 상자(이제 [editor] 표에 넣을 수 있음, 36.33·36.34), 타이가 노드를 따라가기(36.34). 문구는 DESIGN.md 36.31 기준을 따른다. 남은 확인은 DESIGN.md 36.26 '확인 못 한 것'. 남은 일은 DESIGN.md 36.23 의 '남은 것'과 '릴리스'를 본다.
- 각 마일스톤마다: 구현 → 테스트 작성 → 테스트 통과 확인 → 완료 기준 점검 → 커밋(`M30: 엔진 기반` 같은 메시지).
- 한 마일스톤을 끝내면 무엇을 했는지 3~5줄로 보고하고, 다음 마일스톤으로 넘어가기 전에 멈춘다. (사용자가 "계속"이라고 하면 진행)
- 설계에서 벗어나야 할 이유가 생기면 먼저 사용자에게 묻고, 합의되면 DESIGN.md도 함께 고친다. 몰래 바꾸지 않는다.

## 기술 규칙
- 엔진: Python 3.11+, `src/` 레이아웃, 타입 힌트, dataclass 선호. **런타임 의존성은 쓸 이유가 분명할 때만** 더하고(2026-10-21 사용자 결정으로 표준 라이브러리 제한 해제), `pyproject.toml` 과 DESIGN.md 에 이유를 적는다. 원격(SSH)에서 부르는 API 경로는 무거운 의존성 없이 돌게 둔다.
- 화면: React + TypeScript + Vite. 데스크톱 앱: 오르카(Electron) 포크 `orca/` (DESIGN.md 36.23, 2026-10-22 사용자 결정, 옛 Tauri 셸은 M40 에서 걷어냄). 엔진은 그 안에서 자식 프로세스로 돈다. 엔진 화면은 브라우저로도 열려야 한다.
- 포크(`orca/`)는 오르카 규칙(orca/AGENTS.md)을 따르고 Node 24·pnpm 12 는 `.tools/` 의 휴대용 Node 로 돌린다.
- 모든 subprocess 호출은 엔진의 `runner` 모듈을 통한다.
- 사용자에게 보이는 메시지(로그, 에러, README, 화면)는 한국어. 코드 식별자와 주석은 영어/한국어 자유.
- 사용자 설정(`~/.claude`, `~/.codex`)을 고치지 않는다. 훅·MCP 는 호출마다 인자로 붙인다.
- 다른 프로젝트의 코드를 빌리면 라이선스를 확인하고 `THIRD_PARTY_NOTICES.md` 에 출처를 적는다. NotchCrew 처럼 소스가 없는 것은 아이디어만.

## 절대 하지 말 것
- 테스트나 개발 중에 **실제 `claude -p`, `codex exec`/`codex app-server` 로 프롬프트를 실행하지 않는다.** 사용자 구독 한도를 소모한다. `--help`, `--version` 확인만 허용. (사용자가 실제 실행을 분명히 요청한 경우는 예외)
- Anthropic/OpenAI API SDK를 추가하지 않는다(구독 CLI 만 쓴다).
- `prototype/agent_team.py`를 수정하지 않는다. 참고만 한다.

## 명령어
- 설치: `pip install -e ".[dev]"` (옛 `duet` 명령도 같은 동작의 별칭으로 남아 있다)
- 테스트: `pytest -q`
- 수동 확인: `tenure run "할일 CLI" --workdir /tmp/tenure-try --mock`
- 화면 빌드: `cd app && npm install && npm run build` (결과는 src/tenure/ui/web, 커밋한다. 개발 중에는 `npm run dev` 가 /api 를 8790 의 tenure ui 로 넘김)
- 데스크톱 앱: `python desktop/build_desktop.py` (엔진 화면 → 엔진 얼리기 → SSH 원격용 orcad 템플릿(이 PC 의 정식 오르카 필요, 36.26) → 오르카 포크 `orca/` 의 NSIS 설치 파일 `orca/dist/`, 버전은 `desktop/desktop-revision.txt` 회차로). 릴리스는 `pyk-kentech/tenure` 에 수동으로 올린다(서명 없음 → 자동 업데이트 꺼짐). 포크 개발 실행: `orca/` 에서 PATH 앞에 `.tools/node24` 를 두고 `pnpm dev`
- 관리자 UI: `tenure ui` (느린 mock 으로 화면 확인: `TENURE_MOCK_DELAY=1 tenure ui`)
- UI 테스트는 `tests/test_ui.py`(HTTP 수준). 테스트는 `TENURE_HOME` 을 임시 폴더로 바꿔 사용자의 `~/.tenure` 을 건드리지 않는다.
