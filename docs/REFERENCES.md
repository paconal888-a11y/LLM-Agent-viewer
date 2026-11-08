# 관련 프로젝트와 명세

설계와 프로토콜 구현에 참고한 자료입니다. 확인일: 2026-11-08.

## 유사 제품

LangSmith Studio, Langfuse, ADK Web, Transformer Explainer, LLM Visualization의 범위와 반영한 개선은 [유사 제품 비교](COMPARISON.md)에 정리했습니다.

## 하네스 구성

[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)는 파일 시스템·계획·컨텍스트·스킬 등을 하네스의 구성으로 설명합니다. 이 프로젝트에는 다음처럼 작은 단위로 넣었습니다.

| 구성 | 현재 구현 | 사용자가 확인할 질문 |
| --- | --- | --- |
| 계획 | set_plan, complete_plan_step과 실제 계획 상태 | 계획을 완료로 표시한 것과 작업 검증은 같은가? |
| 파일 근거 | read_file, 제한된 search_text와 실제 I/O | 어떤 파일의 어느 행이 tool 메시지로 들어왔나? |
| 스킬 | 목록 공개 후 load_skill로 본문 전달 | 디스크에서 검색한 본문과 모델에게 전달한 본문은 언제 달라지나? |
| 컨텍스트 | 역할별 messages, 원문·조립·템플릿·토큰 연결 | 규칙과 기억이 실제로 어느 메시지에 들어갔나? |
| 실행 통제 | 도구 schema·권한·경로 검사, 명시적 완료 조건 | 모델의 완료 주장 대신 어떤 실제 결과를 확인했나? |
| 기억 | 최근 두 대화와 별도 지적 목록, 동일 요청 재포함 옵션 | 저장되었지만 이번 모델 입력에서는 빠진 지적이 있나? |
| 모드·위임 | 역할별 지침·권한·예산과 독립 하위 대화, 실제 보고 합류 | 모드 선택이 어느 설정과 실행 경로를 바꿨나? |

모드는 [공개 OpenCode의 primary/subagent](https://docs.opencode.ai/docs/agents/), [OmO OpenCode판의 역할·도구 제한](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/packages/omo-opencode/src/agents/AGENTS.md), [Deep Agents의 독립 하위 컨텍스트](https://docs.langchain.com/oss/python/deepagents/subagents)를 참고했습니다. 이 프로젝트의 실제 경계와 차이는 [모드 설명](MODES.md)에 있습니다.

대화 요약은 완료한 원문을 다음 요청 전에 처리합니다. 벡터 검색, 임의 셸, 브라우저 조작, OS 격리 실행은 지원하지 않습니다.

## 프로토콜과 추론의 기준 자료

- [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28): 현재 stdio 도구 경로의 기준.
- [server/discover](https://modelcontextprotocol.io/specification/2026-07-28/server/discover), [도구](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), [전송](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports): 요청별 메타데이터·도구 목록·도구 실행과 newline framing. 표준 전체를 지원한다고 주장하지 않습니다.
- [LSP 3.17 명세](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/): Content-Length, 문서 동기화, 0부터 시작하는 위치, 진단·심볼·정의.
- [python-lsp-server](https://github.com/python-lsp/python-lsp-server): 실제 Python 진단 서버. 설치 버전은 uv.lock에 고정합니다.
- [Transformers v5.22.2 Qwen3 구현](https://github.com/huggingface/transformers/blob/v5.22.2/src/transformers/models/qwen3/modeling_qwen3.py): 직접 생성 루프에서 호출하는 신경망·캐시 구현.
- [KV 캐시 설명](https://huggingface.co/docs/transformers/en/cache_explanation): 과거 위치의 K/V를 다시 사용하는 계산 상태.
- [llama.cpp b12110 서버](https://github.com/ggml-org/llama.cpp/blob/b12110/tools/server/README.md): 선택 실행기의 실제 HTTP·SSE·통계 API.

## 학습 효과를 확인하는 방법

화려한 기록만으로 이해도가 높아졌다고 단정할 수 없습니다. [학습 순서](LEARNING.md)의 질문에 먼저 답을 예상하고, 구성을 하나 바꾸고, 실제 입력·도구 결과·다음 요청에서 예상과 차이를 찾아보세요. 마지막에는 같은 원리를 설명하는 작은 도구를 직접 추가해 보는 것이 좋습니다.

설명 가능한 범위는 프로그램이 기록한 실행과 수치입니다. 모델의 숨겨진 사고를 읽거나 attention 값으로 답변의 의미적 원인을 증명하지 않습니다.
