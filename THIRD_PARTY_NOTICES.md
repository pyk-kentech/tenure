# 제3자 소프트웨어 고지

tenure 가 함께 배포하거나 실행 중에 쓰는 다른 프로젝트의 코드와 그 라이선스입니다.
다른 도구(OpenRig, Orca, herdr, Emdash, Schaltwerk, CodexMonitor, Daintree, NotchCrew)는 설계 아이디어만 참고했고 코드는 가져오지 않았습니다(DESIGN.md 36.8).
데스크톱 앱 `orca/` 는 Orca(https://github.com/stablyai/orca, MIT, © stablyai)의 포크입니다(DESIGN.md 36.23). 원래 LICENSE 와 저작권 고지를 그 안에 그대로 둡니다.
화면 3차(DESIGN.md 36.22)의 작업공간 배치·치수·무채색 색 토큰은 Orca(https://github.com/stablyai/orca, MIT, © stablyai)의 화면 구조를 따랐습니다. 코드는 옮기지 않고 다시 짰습니다.

## 화면 빌드에 들어가는 것 (`src/tenure/ui/web/`)

| 패키지 | 버전 | 라이선스 | 저작권 |
|---|---|---|---|
| [@xterm/xterm](https://github.com/xtermjs/xterm.js) | 6.0.0 | MIT | Copyright (c) 2017-2019, The xterm.js authors; (c) 2014-2016, SourceLair Private Company; (c) 2012-2013, Christopher Jeffrey |
| [@xterm/addon-fit](https://github.com/xtermjs/xterm.js) | 0.11.0 | MIT | Copyright (c) 2019, The xterm.js authors |
| [react](https://github.com/facebook/react) | 18.3.1 | MIT | Copyright (c) Facebook, Inc. and its affiliates |
| [react-dom](https://github.com/facebook/react) | 18.3.1 | MIT | Copyright (c) Facebook, Inc. and its affiliates |

## 데스크톱 앱(포크 `orca/`)에 더한 것

| 패키지 | 버전 | 라이선스 | 저작권 | 용도 |
|---|---|---|---|---|
| [@xyflow/react](https://github.com/xyflow/xyflow) (React Flow) | 12.12.0 | MIT | Copyright (c) 2019-2025 webkid GmbH | 워크플로 노드 편집기 캔버스 (DESIGN.md 36.29) |

선 묶기 아이디어는 [comfyui-zip-ties](https://github.com/safzanpirani/comfyui-zip-ties)(MIT)를 참고했고 코드는 가져오지 않았습니다.

## 실행 중에 쓰는 파이썬 패키지 (설치 시 따로 받음)

| 패키지 | 버전 | 라이선스 | 저작권 | 용도 |
|---|---|---|---|---|
| [pywinpty](https://github.com/andfoy/pywinpty) | ≥2.0 (Windows 에서만) | MIT | Copyright (c) 2017 Spyder IDE | 대화형 터미널 세션의 ConPTY (DESIGN.md 36.19) |

## MIT 라이선스 본문

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of
the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
