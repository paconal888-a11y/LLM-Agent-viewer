/* 학습 코스. 각 단계는 실제 저장 기록의 이벤트를 종류와 조건으로 찾는다. 이벤트 번호를 직접 적지 않는다. */
((scope) => {
  "use strict";
  const chapters = [
    { id: "input", title: "모델 입력이 만들어지기까지", note: "모델을 부르기 전에 코드와 파일이 하는 일" },
    { id: "loop", title: "모델과 도구의 왕복", note: "요청 · 검사 · 실행 · 결과 · 완료 판정" },
    { id: "long", title: "길어지는 작업", note: "기억 · 압축 · 재개 · 여러 에이전트" },
    { id: "inside", title: "모델 안쪽", note: "토큰 · 벡터 · 캐시 · 샘플링" },
    { id: "method", title: "직접 고치고 측정하기", note: "실패의 원인을 좁혀 가는 방법" },
  ];
  const owners = [
    ["code", "코드가 결정", "순서·검사·권한. 코드를 고치면 달라집니다."],
    ["model", "모델이 선택", "답할지, 어떤 도구를 부를지, 어떤 문장을 쓸지."],
    ["config", "설정·파일에서 옴", "AGENTS.md, 스킬, agent-lens.json, 실행 설정."],
    ["runtime", "실행기 계산", "토큰화, 캐시, 샘플링처럼 정해진 계산."],
    ["external", "외부 서버", "MCP·LSP 같은 별도 프로세스."],
    ["user", "사용자 입력", "직접 입력한 내용."],
  ];
  const glossary = [
    ["하네스", "모델을 감싸는 프로그램입니다. 입력을 조립하고, 모델의 도구 요청을 검사·실행하고, 결과를 다시 넣습니다."],
    ["모델", "학습된 가중치입니다. 입력 토큰을 받아 다음 토큰을 계산할 뿐 파일을 읽거나 명령을 실행하지 못합니다."],
    ["실행기", "모델을 메모리에 올려 계산하는 프로그램입니다. 이 앱은 llama.cpp와 교육용 Python 실행기를 씁니다."],
    ["messages", "모델 입력의 대화 배열입니다. 각 항목에 system·user·assistant·tool 역할이 붙습니다."],
    ["role=user", "API의 역할 표시일 뿐 작성자가 아닙니다. 하네스가 쓴 메시지도 user로 들어갈 수 있습니다."],
    ["tool call", "모델이 어떤 도구를 어떤 인자로 불러 달라고 출력한 글자입니다. 하네스가 실행해야 실제 행동이 됩니다."],
    ["스킬", "작업 절차를 적은 파일입니다. 이름·설명 목록만 먼저 입력되고 본문은 요청할 때 들어옵니다."],
    ["컨텍스트 창", "모델이 한 번에 받을 수 있는 입력의 크기입니다. 토큰 수로 잽니다."],
    ["토큰", "모델이 처리하는 텍스트 조각입니다. 글자 수와 다르고 한글 한 글자가 여러 토큰이 되기도 합니다."],
    ["KV 캐시", "앞 위치의 계산 결과를 보관해 다시 계산하지 않게 하는 저장소입니다. 대화 기록과는 별개입니다."],
    ["MCP · LSP", "외부 도구 서버와 언어 서버에 연결하는 규약입니다. 하네스가 별도 프로세스와 통신합니다."],
  ];
  const lessons = [
    {
      id: "request",
      chapter: "input",
      title: "요청이 코드에 들어오는 순간",
      question: "엔터를 누르면 모델보다 먼저 무엇이 실행되나요?",
      sample: "verified-write",
      steps: [
        {
          kind: "browser.serialize",
          title: "브라우저가 요청을 JSON으로 묶습니다",
          body: "입력한 문자열과 켜 둔 기능, 완료 조건이 하나의 JSON이 되어 서버로 갑니다. 아직 어떤 모델도 등장하지 않았습니다.",
          look: "데이터 변화 탭에서 전송된 값을 확인하세요.",
        },
        {
          kind: "http.body.read",
          tab: "code",
          title: "서버가 바이트를 읽습니다",
          body: "HTTP 본문을 바이트로 받아 UTF-8과 JSON으로 해석합니다. 한글 프롬프트도 이 단계에서는 바이트일 뿐입니다.",
          look: "실행 코드 탭에 이 순간 실행 중이던 함수가 있습니다.",
        },
        {
          kind: "thread.dispatch",
          title: "실행 하나당 스레드 하나를 만듭니다",
          body: "요청마다 실행 기록과 작업 스레드를 새로 만듭니다. 그래서 화면을 새로고침해도 작업은 서버에서 계속됩니다.",
        },
        {
          kind: "config.apply",
          title: "이번 실행의 구성을 확정합니다",
          body: "화면에서 켜고 끈 기능이 이번 실행에 고정됩니다. 이후 단계는 전부 이 값을 따릅니다.",
        },
        {
          kind: "memory.session",
          title: "이어 붙일 대화가 있는지 봅니다",
          body: "세션 ID로 이전 대화 파일을 찾습니다. 이 기록은 새 세션이라 이어 붙일 대화가 없습니다.",
        },
        {
          kind: "model.request",
          nth: 1,
          title: "46번째 기록에서야 모델이 등장합니다",
          body: "규칙을 읽고, 스킬 목록을 고르고, 입력을 조립한 뒤에 첫 호출이 나갑니다. 그 앞의 45개 기록에는 모델의 계산이 하나도 없습니다.",
          look: "아래 재생 막대를 앞뒤로 움직이며 구조도의 위치 변화를 보세요.",
        },
      ],
      takeaway: "에이전트의 첫 동작은 모델 호출이 아니라 코드 실행입니다. 입력이 어떻게 만들어지는지는 대부분 코드와 파일이 정합니다.",
      tryIt: { text: "'실제 실행을 단계마다 멈추기'를 켜고 요청을 실행하면 같은 단계를 한 칸씩 지나갈 수 있습니다.", compose: true },
    },
    {
      id: "agents-md",
      chapter: "input",
      title: "AGENTS.md는 누가 언제 읽나요",
      question: "규칙 파일은 모델이 찾아 읽는 건가요?",
      sample: "verified-write",
      steps: [
        {
          kind: "function.enter",
          titleIncludes: "규칙 로더",
          tab: "code",
          title: "규칙 로더는 코드가 부릅니다",
          body: "입력을 만드는 함수가 매 요청마다 규칙 로더를 호출합니다. 모델이 읽어 달라고 한 적이 없습니다.",
          look: "실행 코드 탭의 호출 스택에서 어느 함수가 불렀는지 보세요.",
        },
        {
          kind: "file.decode",
          path: "demo_workspace/AGENTS.md",
          title: "파일이 문자열이 됩니다",
          body: "바이트를 읽어 UTF-8로 해석했습니다. 파일을 읽었을 뿐이고, 모델은 아직 내용을 모릅니다.",
        },
        {
          kind: "rules.wrap",
          title: "규칙에 경계를 붙입니다",
          body: "원문을 `<project_rules>` 블록으로 감쌉니다. 어디서 온 지침인지 표시해 참고 자료와 구분되게 합니다.",
          look: "왼쪽은 파일 원문, 오른쪽은 입력에 들어갈 형태입니다.",
        },
        {
          kind: "model.request",
          nth: 1,
          tab: "input",
          messageIncludes: "project_rules",
          title: "system 메시지에 들어간 순간",
          body: "이 배열이 모델이 받는 입력입니다. 규칙은 system 메시지 안의 문장일 뿐 실행 권한이 아닙니다.",
          look: "모델 입력 탭의 messages[0]에서 규칙 블록을 찾아 보세요.",
        },
      ],
      takeaway: "'파일에 있음', '모델 입력에 있음', '모델이 따랐음'은 서로 다른 사실입니다. 앞의 둘은 기록으로 확인되고, 마지막은 답변과 행동을 보고 판단합니다.",
      tryIt: { text: "규칙을 한 줄 바꾸고 같은 요청을 실행해 입력과 답변이 어떻게 달라지는지 비교하세요.", file: "demo_workspace/AGENTS.md" },
    },
    {
      id: "layers",
      chapter: "input",
      title: "규칙과 설정의 층",
      question: "여러 설정 파일이 충돌하면 누가 이기나요?",
      sample: "rules-layers",
      steps: [
        {
          kind: "config.resolve",
          title: "설정값마다 출처가 남습니다",
          body: "`agent-lens.json`을 코드 기본값, 사용자 층, 프로젝트 층 순서로 덮어씁니다. 모델 호출 한도는 8 → 5 → 6이 되어 이 실행은 6회까지 허용됩니다.",
          look: "표의 loop.max_model_calls 행에서 층별 값을 보세요.",
        },
        {
          kind: "rules.resolve",
          title: "규칙 파일에 번호를 붙입니다",
          body: "사용자 층에 1번, 프로젝트 층에 2번을 붙여 system 메시지에 넣고, 충돌하면 번호가 큰 쪽을 따르라는 문장을 더합니다. 순서는 코드가 정하지만 실제로 따르는 것은 모델입니다.",
        },
        {
          kind: "model.request",
          nth: 1,
          tab: "input",
          messageIncludes: "precedence",
          title: "충돌하는 두 규칙이 같은 입력에 있습니다",
          body: "사용자 층은 영어로, 프로젝트 층은 한국어로 답하라고 합니다. 코드는 어느 쪽도 지우지 않았습니다. 두 문장이 모두 모델에 전달됩니다.",
        },
        {
          kind: "model.response",
          nth: 1,
          title: "첫 응답: 언어는 지켰고 사실은 지어냈습니다",
          body: "답변은 한국어였고 사용자 층의 `[사용자 규칙]` 줄도 넣었습니다. 그런데 파일을 읽지 않았는데 요약했다고 말합니다. 지침이 입력에 있어도 행동은 보장되지 않습니다.",
        },
        {
          kind: "rules.directory",
          title: "폴더 규칙은 그 폴더를 건드린 뒤에 들어옵니다",
          body: "완료 조건 미충족으로 다시 호출되자 모델이 `read_file`을 요청했고, 코드가 그 경로를 보고 `rules-demo/AGENTS.md`를 다음 입력에 추가했습니다. 언제 읽을지는 코드가, 어느 파일을 건드릴지는 모델이 정합니다.",
        },
        {
          kind: "output.answer",
          title: "최종 답변에서 요약이 빠졌습니다",
          body: "모델은 폴더 규칙의 문구만 출력했습니다. 완료 조건은 `read_file` 호출 여부만 검사하므로 이 실행은 '완료'로 끝났습니다.",
        },
      ],
      takeaway: "충돌 해결을 문장으로 모델에 맡기면 결과는 모델에 달립니다. 반드시 지켜야 하는 규칙은 코드 검사로 옮겨야 합니다.",
      tryIt: { text: `{"loop": {"max_model_calls": 3}}을 적고 실행해 층별 값과 실제 호출 예산을 확인하세요.`, file: "demo_workspace/agent-lens.json" },
    },
    {
      id: "skills",
      chapter: "input",
      title: "스킬의 목록과 본문",
      question: "스킬은 언제 모델에게 전달되고, 누가 그걸 정하나요?",
      sample: "skill-load",
      steps: [
        {
          kind: "skills.scan",
          tab: "code",
          title: "스킬 폴더를 훑습니다",
          body: "`skills/*/SKILL.md`를 찾아 머리말의 이름과 설명을 읽습니다. 매 요청마다 코드가 하는 일입니다.",
        },
        {
          kind: "skills.catalog",
          title: "본문은 두고 목록만 고릅니다",
          body: "입력에 넣을 것은 이름·설명·경로뿐입니다. 절차 본문은 아직 어디에도 없습니다.",
        },
        {
          kind: "model.request",
          nth: 1,
          tab: "input",
          messageIncludes: "available_skills",
          title: "모델이 처음 보는 것은 목록입니다",
          body: "`<available_skills>`에 스킬 두 개의 이름과 설명이 있습니다. 모델은 이 설명만 보고 본문을 불러올지 정해야 합니다.",
        },
        {
          kind: "validation.requirements",
          status: "error",
          nth: 1,
          title: "모델은 스킬을 부르지 않았습니다",
          body: "스킬을 쓰라고 요청했는데도 모델은 `check_file`을 부르고, 이어서 말로만 답했습니다. 코드가 `load_skill` 결과가 없다고 판정합니다.",
        },
        {
          kind: "tool.dispatch",
          tool: "load_skill",
          title: "이제서야 본문을 요청합니다",
          body: "보완 요청을 받은 뒤 모델이 `load_skill`을 호출했습니다. 본문을 불러올지 정하는 쪽은 모델입니다.",
        },
        {
          kind: "model.request",
          nth: -1,
          tab: "input",
          messageIncludes: "파일 요약 절차",
          title: "본문은 도구 결과로 들어옵니다",
          body: "스킬 본문이 `role=tool` 메시지로 다음 입력에 포함됐습니다. 최종 답변은 세 항목과 출처 표시라는 절차를 따랐습니다.",
        },
      ],
      takeaway: "이름과 설명은 코드가 항상 넣고, 본문은 모델이 요청할 때만 들어옵니다. 스킬이 안 쓰이면 모델을 바꾸기 전에 설명 문구와 완료 조건부터 살펴보세요.",
      tryIt: { text: "설명 문구를 바꾸고 같은 요청을 여러 번 실행해 load_skill 호출 비율을 비교하세요.", file: "demo_workspace/skills/file-summary/SKILL.md" },
    },
    {
      id: "input",
      chapter: "input",
      title: "모델이 받는 입력의 전부",
      question: "지시문·대화·파일·도구 설명은 어떻게 하나의 입력이 되나요?",
      sample: "verified-write",
      steps: [
        {
          kind: "context.assemble",
          title: "재료를 모아 배열로 만듭니다",
          body: "기본 지침, 프로젝트 규칙, 스킬 목록, 저장된 맥락, 현재 요청이 역할이 붙은 메시지로 정리됩니다.",
          look: "선택한 재료와 완성된 messages를 나란히 비교하세요.",
        },
        {
          kind: "model.request",
          nth: 1,
          tab: "input",
          title: "모델이 받는 전부입니다",
          body: "메시지 4개와 도구 정의 6개가 이번 호출의 입력 전부입니다. 여기에 없는 정보는 모델이 알 수 없습니다. `role=user`가 3개지만 사용자가 쓴 것은 하나뿐입니다.",
          look: "각 메시지에 붙은 출처 표시를 확인하세요.",
        },
        {
          kind: "model.template",
          nth: 1,
          title: "메시지가 하나의 문자열이 됩니다",
          body: "대화 템플릿이 messages와 도구 정의를 문자열 하나로 합칩니다. 도구 설명도 결국 모델이 읽는 글자입니다.",
        },
        {
          kind: "model.tokenize",
          nth: 1,
          title: "문자열이 토큰 ID가 됩니다",
          body: "문자열을 토큰 1,105개로 나눴습니다. 모델이 실제로 계산하는 입력은 이 숫자 배열입니다.",
          look: "토큰을 눌러 조각과 UTF-8 바이트를 확인해 보세요.",
        },
      ],
      takeaway: "컨텍스트는 코드가 고른 텍스트의 모음입니다. 어떤 정보가 없어서 틀렸는지는 이 화면에서 직접 확인할 수 있습니다.",
      tryIt: { text: "실행 설정에서 규칙이나 이력을 끄고 같은 요청을 실행한 뒤 입력과 토큰 수를 비교하세요.", compose: true },
    },
    {
      id: "tools",
      chapter: "loop",
      title: "도구 호출은 누가 실행하나요",
      question: "모델이 파일을 읽었다는 건 정확히 무슨 뜻인가요?",
      sample: "verified-write",
      steps: [
        {
          kind: "model.response",
          nth: 1,
          title: "모델은 요청만 출력합니다",
          body: "응답에 `tool_calls`가 있습니다. 어떤 도구를 어떤 인자로 부르고 싶다는 글자일 뿐, 아직 아무것도 실행되지 않았습니다.",
        },
        {
          kind: "tool.dispatch",
          nth: 1,
          title: "하네스가 출력을 해석합니다",
          body: "도구 이름과 JSON 인자를 꺼냅니다. 여기서부터는 코드의 일입니다.",
        },
        {
          kind: "tool.validate",
          nth: 1,
          tab: "code",
          title: "실행 전에 코드가 검사합니다",
          body: "등록된 도구인지, 인자가 형식에 맞는지, 경로가 작업 공간 안인지 `policy.py`가 확인합니다. 자연어 지침과 관계없이 동작합니다.",
        },
        {
          kind: "tool.result",
          nth: 1,
          title: "실제 함수가 실행된 결과입니다",
          body: "`read_file` 함수가 파일을 읽어 반환한 값입니다. 이 값이 있어야 '읽었다'고 말할 수 있습니다.",
        },
        {
          kind: "tool.dispatch",
          tool: "write_file",
          title: "쓰기도 같은 경로를 지납니다",
          body: "`write_file`은 파일 쓰기 스위치를 켠 실행에만 공개됩니다. 공개하지 않은 도구를 요청하면 검사에서 거부됩니다.",
        },
      ],
      takeaway: "모델은 불러 달라고 쓰고, 하네스가 판단하고 실행합니다. 프롬프트로 막는 것과 코드로 막는 것은 전혀 다릅니다.",
      tryIt: { text: "도구 구현과 schema를 읽고, 경로 검사가 어디서 일어나는지 찾아 보세요.", file: "local_agent_lab/policy.py" },
    },
    {
      id: "results",
      chapter: "loop",
      title: "결과와 오류는 어떻게 돌아가나요",
      question: "도구 결과와 지적은 다음 호출에 어떻게 반영되나요?",
      sample: "feedback",
      steps: [
        {
          kind: "context.append",
          nth: 1,
          title: "결과가 다음 입력에 붙습니다",
          body: "도구 결과가 `role=tool` 메시지로 대화에 추가됩니다.",
        },
        {
          kind: "model.request",
          nth: 2,
          tab: "input",
          title: "모델은 매번 전체를 다시 받습니다",
          body: "모델에는 지난 호출의 기억이 없습니다. 하네스가 이전 메시지에 새 메시지 두 개를 더해 전부 다시 보냅니다.",
          look: "'이전 입력과 비교'에서 추가된 메시지를 확인하세요.",
        },
        {
          kind: "model.response",
          nth: 3,
          title: "검사를 건너뛰고 끝났다고 말합니다",
          body: "모델이 `check_file`을 부르지 않은 채 텍스트로 답했습니다.",
        },
        {
          kind: "validation.requirements",
          status: "error",
          title: "코드가 실제 결과와 대조합니다",
          body: "완료 조건의 `check_file` 결과가 없습니다. 모델의 문장은 판정 근거가 아닙니다.",
        },
        {
          kind: "context.repair",
          title: "지적도 메시지 하나입니다",
          body: "코드가 보완 요청을 메시지로 추가합니다. `role=user`이지만 사람이 쓴 글이 아닙니다. 모델의 가중치가 바뀌는 일도 없습니다.",
        },
        {
          kind: "tool.dispatch",
          nth: -1,
          title: "다음 호출에서 행동이 바뀌었습니다",
          body: "지적이 입력에 들어간 뒤 모델이 `check_file`을 요청했습니다. 같은 지적이 항상 같은 효과를 내지는 않습니다. 반복 측정이 필요한 이유입니다.",
        },
      ],
      takeaway: "에이전트 루프는 결과를 글자로 붙여 다시 호출하는 일의 반복입니다. 오류 처리의 품질은 어떤 글자를 붙이느냐에 달려 있습니다.",
      tryIt: { text: "지적을 어떤 문장으로 넣는지 찾아 고치고, 비교 실험으로 행동 변화를 측정하세요.", file: "local_agent_lab/agent_loop.py" },
    },
    {
      id: "completion",
      chapter: "loop",
      title: "완료는 누가 판정하나요",
      question: "에이전트가 끝났다고 믿어도 되는 근거는 어디에 있나요?",
      sample: "verified-write",
      steps: [
        {
          kind: "validation.final",
          title: "수정한 파일을 검사했는가",
          body: "모델이 바꾼 파일에 대해 `check_file`이 실행됐는지 코드가 확인합니다.",
        },
        {
          kind: "validation.requirements",
          title: "요구한 결과가 실제로 있는가",
          body: "완료 조건마다 어떤 도구 결과가 근거인지 번호까지 남습니다. 파일의 `practice: true` 값도 대조했습니다.",
        },
        {
          sample: "repeat-guard",
          kind: "context.repair",
          nth: 1,
          title: "모델이 도구를 부르지 않습니다",
          body: "다른 기록입니다. 도구를 쓰라는 요청에도 모델이 답변만 냈고, 코드가 보완을 요청합니다.",
        },
        {
          sample: "repeat-guard",
          kind: "validation.repeat",
          nth: -1,
          title: "같은 답이 세 번 반복되면 멈춥니다",
          body: "보완 지시를 넣어도 같은 답이 세 번 나오자 반복 검사가 실행을 중단합니다. 실패로 끝내는 것도 코드의 판단입니다.",
        },
        {
          sample: "repeat-guard",
          kind: "run.error",
          title: "실패를 실패로 기록합니다",
          body: "모델이 완료했다고 말해도 통과시키지 않습니다. 원인 찾기 탭에서 어느 층의 문제인지 볼 수 있습니다.",
          tab: "learn",
        },
      ],
      takeaway: "'완료'는 코드가 검사한 범위까지만 보증합니다. 검사하지 않은 것, 예를 들어 요약의 품질은 완료 표시와 무관합니다.",
      tryIt: { text: "완료 조건에 결과 값까지 넣어 실행하고 판정이 어떻게 달라지는지 보세요.", compose: true },
    },
    {
      id: "memory",
      chapter: "long",
      title: "'기억'의 실체",
      question: "에이전트는 이전 대화를 어떻게 기억하나요?",
      sample: "compaction",
      steps: [
        {
          kind: "memory.compaction.check",
          title: "새 요청이 올 때 크기를 잽니다",
          body: "저장된 대화의 글자 수를 `memory.json`의 기준(2,000자)과 비교합니다. 코드가 정하는 일입니다.",
        },
        {
          kind: "model.request",
          purpose: "compaction",
          nth: 1,
          tab: "input",
          title: "요약은 별도의 모델 호출입니다",
          body: "답변과 다른 지시문으로 실제 모델을 한 번 더 호출합니다. 도구는 공개하지 않습니다.",
        },
        {
          kind: "memory.compaction.result",
          title: "2,488자가 73자가 됩니다",
          body: "무엇을 남길지는 모델이 고릅니다. 원문 파일은 지우지 않고 그대로 둡니다.",
        },
        {
          kind: "memory.summary.include",
          title: "요약이 다음 입력에 들어갑니다",
          body: "코드가 요약문을 참고 자료 메시지로 만듭니다.",
        },
        {
          kind: "model.request",
          purpose: "answer",
          nth: 1,
          tab: "input",
          messageIncludes: "이전 대화의 로컬 모델 요약",
          title: "이것이 기억의 전부입니다",
          body: "이전 대화를 기억하는 방법은 저장한 텍스트를 입력에 다시 넣는 것입니다. 요약에 없는 사실은 원문 최근 대화에 남아 있지 않으면 모델이 알 수 없습니다.",
        },
        {
          kind: "memory.save",
          title: "끝나면 다시 파일에 씁니다",
          body: "이번 대화와 답변 발췌를 세션 파일에 저장합니다. 다음 요청이 읽을 재료입니다.",
        },
      ],
      takeaway: "에이전트의 기억은 파일입니다. 가중치는 변하지 않고, KV 캐시와도 별개입니다.",
      tryIt: { text: "대화 저장소 화면에서 원문·요약·실제 입력에 같은 문자열이 남았는지 대조하세요.", view: "memory" },
    },
    {
      id: "turn-compaction",
      chapter: "long",
      title: "작업 도중 입력 줄이기",
      question: "작업이 길어져 입력이 넘치면 무슨 일이 일어나나요?",
      sample: "turn-compaction",
      steps: [
        {
          kind: "context.check",
          nth: 1,
          title: "호출 직전마다 크기를 잽니다",
          body: "입력 크기를 추정해 기준과 비교합니다. 이 실습은 기준을 일부러 1,250토큰으로 낮췄고, 첫 호출은 기준 아래입니다.",
        },
        {
          kind: "context.check",
          output: { needed: true },
          nth: 1,
          title: "기준을 넘어도 바로 줄이지는 않습니다",
          body: "최근 메시지 4개를 남기도록 설정돼 있어 아직 잘라 낼 오래된 메시지가 없습니다. 이번 호출은 그대로 나갑니다.",
        },
        {
          kind: "context.prune",
          title: "오래된 도구 결과의 본문을 뺍니다",
          body: "모델을 부르지 않는 결정적 단계입니다. 723자짜리 도구 결과가 자리표시자로 바뀌어 입력에만 반영됩니다. 원본은 대화 기록에 그대로 있습니다.",
        },
        {
          kind: "model.request",
          purpose: "compaction",
          nth: 1,
          tab: "input",
          title: "그래도 크면 앞선 작업을 요약합니다",
          body: "규칙과 현재 요청은 두고 그 뒤의 작업 메시지만 별도 모델 호출로 요약합니다. 생략 전의 원본을 입력으로 줍니다.",
        },
        {
          kind: "context.compact.result",
          title: "요약이 앞선 메시지를 대체합니다",
          body: "요약문은 모델이 썼습니다. 코드는 내용의 정확성을 보증하지 않습니다.",
        },
        {
          kind: "model.request",
          purpose: "answer",
          nth: 4,
          tab: "input",
          messageIncludes: "지금까지의 작업 요약",
          title: "다음 입력의 모습",
          body: "앞선 메시지 자리에 요약 한 개가 들어갔습니다. 대화 기록 자체는 줄지 않고, 보내는 입력만 달라집니다.",
        },
        {
          kind: "tool.dispatch",
          nth: -1,
          title: "모델이 같은 파일을 다시 읽습니다",
          body: "요약 뒤 모델이 앞서 읽은 `notes/agent-guide.md`를 다시 요청했습니다. 왜 그랬는지는 기록만으로 알 수 없지만, 호출과 토큰은 실제로 늘었습니다.",
        },
      ],
      takeaway: "압축은 정보를 버리는 일입니다. 언제 무엇을 줄일지는 코드와 설정이, 어떤 문장이 남을지는 모델이 정합니다.",
      tryIt: { text: `{"context": {"compact_at_tokens": 1500}}을 적고 긴 파일을 여러 개 읽게 해 차이를 보세요.`, file: "demo_workspace/agent-lens.json" },
    },
    {
      id: "resume",
      chapter: "long",
      title: "중단된 작업 이어가기",
      question: "멈춘 에이전트는 무엇을 저장했다가 어디서 이어가나요?",
      sample: "resume-stopped",
      steps: [
        {
          kind: "checkpoint.save",
          nth: 1,
          title: "호출하기 전에 저장합니다",
          body: "모델을 부르기 직전에 대화와 진행 상태를 파일에 씁니다. 13,465바이트, 메시지 4개입니다. 모델의 내부 계산 상태는 포함되지 않습니다.",
        },
        {
          kind: "context.append",
          nth: 1,
          title: "첫 결과를 받은 뒤 중지했습니다",
          body: "첫 도구 결과가 대화에 들어간 직후 실행을 멈췄습니다. 이 시점의 상태가 파일에 남습니다.",
        },
        {
          sample: "resume-continued",
          kind: "run.resume",
          title: "새 실행이 저장된 대화로 시작합니다",
          body: "규칙 파일을 다시 읽지 않고 저장된 메시지 6개를 복원했습니다. 호출 횟수와 도구 결과도 이어집니다.",
        },
        {
          sample: "resume-continued",
          kind: "model.request",
          nth: 1,
          tab: "input",
          title: "이어지는 첫 입력",
          body: "중단 전에 쌓인 메시지 그대로 다음 호출이 나갑니다.",
        },
        {
          sample: "resume-continued",
          kind: "validation.requirements",
          title: "이전 실행의 결과까지 합쳐 판정합니다",
          body: "두 번의 `add_numbers` 결과 중 첫 번째는 중단 전에 나온 것입니다.",
        },
      ],
      takeaway: "재개는 하네스 상태의 복원입니다. 모델이 기억해서 이어가는 것이 아니므로 응답이 중단 전과 같다는 보장은 없습니다.",
      tryIt: { text: "도구를 쓰는 요청을 실행하다 중지하고, 위쪽에 나타나는 '이어서 실행'을 눌러 보세요.", compose: true },
    },
    {
      id: "team",
      chapter: "long",
      title: "여러 에이전트의 분담과 합류",
      question: "하위 에이전트는 무엇을 공유하고 무엇을 돌려주나요?",
      sample: "team",
      steps: [
        {
          kind: "agent.delegate",
          title: "위임은 모델의 도구 호출입니다",
          body: "주 에이전트가 `delegate_tasks`로 작업 두 개를 요청했습니다. 위임할지와 작업 문구는 모델이 정했습니다.",
        },
        {
          kind: "agent.spawn",
          nth: 1,
          title: "하위 에이전트는 별도의 대화입니다",
          body: "독립된 messages와 제한된 도구 목록을 가진 새 실행이 만들어집니다. 주 대화의 내용을 자동으로 공유하지 않습니다.",
        },
        {
          kind: "agent.compute",
          outputAtLeast: { wait_ms: 1000 },
          nth: 1,
          title: "같은 모델을 번갈아 씁니다",
          body: "작업자는 둘이지만 모델은 하나라 차례를 기다립니다. 여기서 약 3초를 기다렸습니다.",
        },
        {
          kind: "agent.join",
          title: "보고만 돌아옵니다",
          body: "하위 대화 전체가 아니라 최종 답변과 통계만 주 대화로 합류합니다. Explore의 보고는 '스킬이 존재하지 않습니다'입니다.",
          look: "results의 answer를 각각 읽어 보세요.",
        },
        {
          kind: "output.answer",
          title: "최종 답변은 보고와 다릅니다",
          body: "주 에이전트는 Explore가 파일을 읽고 정리했다고 썼지만, 기록상 파일을 읽은 것은 Oracle뿐입니다. 코드는 이 요약을 대조하지 않습니다.",
        },
      ],
      takeaway: "여러 에이전트는 여러 대화입니다. 합류 지점에서 정보가 줄고, 요약의 정확성은 자동으로 보장되지 않습니다.",
      tryIt: { text: "역할별 도구와 지침을 바꾸고 하위 작업 예제를 다시 실행해 보세요.", file: "demo_workspace/agents.json" },
    },
    {
      id: "tensors",
      chapter: "inside",
      title: "토큰에서 다음 토큰까지",
      question: "모델 내부에서 실제로 계산되는 것은 무엇인가요?",
      sample: "tensors",
      steps: [
        {
          kind: "model.tokenize",
          nth: 1,
          title: "입력은 995개의 토큰입니다",
          body: "교육용 Qwen3-0.6B가 처리한 실제 입력입니다. 이 숫자들이 신경망의 입력입니다.",
        },
        {
          kind: "reference.embedding",
          nth: 1,
          title: "토큰이 벡터가 됩니다",
          body: "토큰 995개가 각각 1,024차원 벡터가 됩니다. 실제 텐서에서 뽑은 표본을 표시합니다.",
        },
        {
          kind: "reference.projection",
          nth: 1,
          title: "Q·K·V로 투영합니다",
          body: "벡터를 질의(Q), 키(K), 값(V) 공간으로 선형 변환합니다. 선택한 레이어의 실제 수치입니다.",
        },
        {
          kind: "reference.attention",
          nth: 1,
          title: "어느 위치를 참고했나",
          body: "마지막 위치가 앞의 어느 위치에 가중치를 뒀는지 보여 줍니다. 왜 그렇게 판단했는지를 설명하는 값은 아닙니다.",
        },
        {
          kind: "reference.cache.write",
          nth: 1,
          title: "처음에는 입력 전체를 계산합니다",
          body: "995개 위치의 K·V가 캐시에 저장됩니다. 약 218MiB입니다.",
        },
        {
          kind: "reference.cache.write",
          nth: 2,
          title: "다음부터는 한 토큰만 계산합니다",
          body: "forward 입력이 1개로 줄어듭니다. 앞 위치의 계산은 캐시를 다시 씁니다.",
        },
        {
          kind: "reference.sample",
          nth: 1,
          title: "다음 토큰을 고릅니다",
          body: "logits에서 만든 분포로 `<tool_call>` 토큰을 골랐습니다. 도구 요청은 이렇게 토큰 하나에서 시작됩니다.",
        },
      ],
      takeaway: "모델 안쪽은 정해진 계산입니다. 관찰할 수 있지만, 수치가 판단의 이유를 알려 주지는 않습니다.",
      tryIt: { text: "교육용 실행기에서 생성 중 KV를 끄고 forward 입력 길이와 누적 계산량을 비교하세요.", compose: true },
    },
    {
      id: "method",
      chapter: "method",
      title: "실패의 원인을 좁히고 고치기",
      question: "답이 틀렸을 때 모델 탓인지 어떻게 구분하나요?",
      sample: "compaction-lost-facts",
      steps: [
        {
          kind: "output.answer",
          title: "증상: 답변에 '내용 누락'이 있습니다",
          body: "세 파일의 핵심을 알려 달라고 했는데 첫 줄이 이상합니다. 모델이 못 한 걸까요? 결론을 내리기 전에 층을 좁힙니다. 실행기 → 하네스 → 정보 → 모델 순서입니다.",
        },
        {
          kind: "model.response",
          purpose: "answer",
          nth: -1,
          title: "실행기는 정상입니다",
          body: "응답은 `finish_reason: stop`으로 끝났습니다. 잘리거나 실패한 호출이 아닙니다. 실행기 층은 통과입니다.",
        },
        {
          kind: "model.request",
          purpose: "answer",
          nth: -1,
          tab: "input",
          messageIncludes: "지금까지의 작업 요약",
          title: "모델이 받은 입력을 봅니다",
          body: "입력의 요약 메시지에 이미 '내용 누락'이라고 적혀 있습니다. 파일 내용이 입력에 없었습니다. 모델보다 앞 단계의 문제입니다.",
        },
        {
          kind: "model.request",
          purpose: "compaction",
          nth: 1,
          tab: "input",
          messageIncludes: "omitted_by_harness",
          title: "요약 모델이 받은 입력을 봅니다",
          body: "요약할 내용 자리에 `omitted_by_harness` 자리표시자만 있습니다. 요약 모델에게 줄 정보가 없었습니다.",
        },
        {
          kind: "context.prune",
          tab: "code",
          title: "원인: 순서가 문제였습니다",
          body: "요약 직전에 본문을 생략했고, 요약은 생략된 결과를 읽었습니다. 모델이 아니라 하네스가 잘못된 입력을 만든 것입니다.",
        },
        {
          sample: "compaction-fixed",
          kind: "model.request",
          purpose: "compaction",
          nth: 1,
          tab: "input",
          title: "한 조건만 바꿉니다",
          body: "요약이 원본을 읽도록 `compaction.py`를 한 줄 고치고, 프롬프트·설정·seed를 같게 두고 다시 실행했습니다. 이제 입력에 파일 내용이 있습니다.",
        },
        {
          sample: "compaction-fixed",
          kind: "context.compact.result",
          title: "결과: 요약에 사실이 남았습니다",
          body: "같은 모델이 같은 지시로 파일 내용을 요약했습니다. 달라진 것은 입력입니다.",
        },
        {
          sample: "compaction-fixed",
          kind: "output.answer",
          title: "남은 문제도 기록합니다",
          body: "최종 답변의 첫 줄은 다른 파일의 문장을 옮겼습니다. 최근 메시지를 2개만 남기는 설정 탓에 다시 읽은 결과가 입력에서 빠져 있던 점이 원인 후보입니다. 한 번의 실행으로 해결됐다고 쓰지 않습니다.",
        },
      ],
      takeaway: "관찰 → 층 좁히기 → 한 조건 수정 → 반복 측정. 이 순서를 지키면 '모델이 부족하다'는 결론을 함부로 내리지 않게 됩니다.",
      tryIt: { text: "구성 · 비교 화면에서 두 구성을 같은 요청으로 여러 번 돌려 비교해 보세요.", view: "learn" },
    },
  ].map((lesson, index) => ({ ...lesson, number: index + 1 }));

  const normalized = (value) => String(value ?? "").replaceAll("\\", "/");
  function toolName(event) {
    if (event.kind === "tool.dispatch") return event.input?.function?.name;
    if (event.kind === "tool.result") return String(event.title || "").split(" ")[0];
    return null;
  }
  function matches(event, step) {
    if (event.kind !== step.kind) return false;
    if (step.purpose && (event.model_purpose || "answer") !== step.purpose) return false;
    if (step.tool && toolName(event) !== step.tool) return false;
    if (step.path && !normalized(event.input?.path).endsWith(step.path)) return false;
    if (step.status && event.status !== step.status) return false;
    if (step.titleIncludes && !String(event.title || "").includes(step.titleIncludes)) return false;
    if (step.output && !Object.entries(step.output).every(([k, v]) => event.output?.[k] === v)) return false;
    if (step.outputAtLeast && !Object.entries(step.outputAtLeast).every(([k, v]) => event.output?.[k] >= v)) return false;
    return true;
  }
  /** 조건에 맞는 이벤트의 위치. nth가 음수면 뒤에서부터 센다. 없으면 -1. */
  function locate(events, step) {
    const found = [];
    events.forEach((event, index) => {
      if (matches(event, step)) found.push(index);
    });
    const nth = step.nth ?? 1;
    return (nth > 0 ? found[nth - 1] : found[found.length + nth]) ?? -1;
  }
  /** 모델 입력 탭에서 펼칠 메시지. 지정이 없거나 찾지 못하면 -1. */
  function messageIndex(event, step) {
    if (!step.messageIncludes || !Array.isArray(event?.input?.messages)) return -1;
    return event.input.messages.findIndex((message) =>
      JSON.stringify(message).includes(step.messageIncludes),
    );
  }
  const byId = (id) => lessons.find((lesson) => lesson.id === id) || null;
  const sampleOf = (lesson, step) => "sample-" + (step.sample || lesson.sample);
  function nextOf(id) {
    const at = lessons.findIndex((lesson) => lesson.id === id);
    return at >= 0 ? lessons[at + 1] || null : null;
  }
  /** 주소의 #/... 부분을 해석한다. */
  function parseRoute(hash) {
    const parts = String(hash || "")
      .replace(/^#\/?/, "")
      .split("/")
      .filter(Boolean)
      .map((part) => {
        try {
          return decodeURIComponent(part);
        } catch {
          return part;
        }
      });
    if (parts[0] === "lessons") return { name: "lessons" };
    if (parts[0] === "view" && ["learn", "feedback", "memory"].includes(parts[1]))
      return parts[2] ? { name: "view", view: parts[1], run: parts[2] } : { name: "view", view: parts[1] };
    if (parts[0] === "lesson" && byId(parts[1])) {
      const steps = byId(parts[1]).steps.length,
        number = Number(parts[2]);
      return {
        name: "lesson",
        id: parts[1],
        step: Number.isInteger(number) && number >= 1 && number <= steps ? number - 1 : 0,
      };
    }
    if (parts[0] === "run" && parts[1])
      return { name: "run", id: parts[1], event: parts[2] ? Number(parts[2]) : null, tab: parts[3] || null };
    return null;
  }
  const api = { chapters, owners, glossary, lessons, byId, nextOf, locate, messageIndex, sampleOf, parseRoute };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  scope.AgentLessons = api;
})(typeof window !== "undefined" ? window : globalThis);
