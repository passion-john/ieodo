# ieodo (이어도)

[English](README.md) | 한국어

**이름의 뜻:** 이어도는 "이어줘"를 경상도 사투리로 지은 이름임. AI와 나눈 대화를 다음 대화로 이어 줘서 맥락이 끊기지 않게 한다는 뜻을 담음.

*옛 이름은 groundlink임. groundlink에서 옮기는 방법은 [CHANGELOG.md](CHANGELOG.md) 참고.*

한 컴퓨터의 AI(Claude Code, Codex, Antigravity)가 나눈 **대화**와, 그 대화가 고친 **문서의 이력**을 자동으로 남기는 로컬 MCP 서버임. AI는 기록을 읽기만 하고, 쓰는 것은 수집기(hook)가 함. 설계와 이유는 [wiki/schema.md](wiki/schema.md).

## 왜 필요한가
AI 코딩 도구는 세션이 끝나면 그 대화를 잊음. 다음 세션의 AI는 "어제 왜 이렇게 정했지?", "어디까지 했지?"를 모름. 그래서 사용자가 매번 다시 설명하게 됨. 도구를 여러 개 쓰면 더 심함. Codex에서 정한 것을 Claude Code는 모르고, Antigravity에서 고친 문서의 이유는 어디에도 남지 않음.

ieodo는 이 컴퓨터에서 일어난 대화와 문서 변경을 한곳에 자동으로 쌓음. 어느 AI든 MCP 도구로 그 기록을 찾아 읽음. 사용자가 따로 저장하거나 요약할 필요가 없음.

## 30초 요약
- **자동 기록**: 등록한 프로젝트에서 Claude Code·Codex·Antigravity와 나눈 대화(사용자 말 + 최종 답, 답한 모델)와 `.md` 문서의 수정 이력이 hook으로 저장됨.
- **AI는 읽기만**: `search`(단어 검색), `recent`(최근 대화), `history`(문서 이력) 세 도구로 찾음.
- **로컬 전용**: 서버는 `127.0.0.1`에서만 열리고, 기록은 `~/.ieodo/records.db` 파일 하나임. 저장 전에 알려진 형식의 비밀값을 가림.
- **고칠 수 없는 기록**: 쌓기만 하고 수정·삭제는 DB가 막음. 기록은 그때 있었던 일이지 정답이 아님.

## 어떻게 쓰이나 (예시)
사용자가 새 Claude Code 세션에서 이렇게 물음.

> 지난번에 Codex랑 캐시 만료 시간을 몇 분으로 정했었지?

AI는 ieodo의 `search`를 부름.

```json
{"query": "캐시 만료", "project": "my-app"}
```

결과에는 어느 세션의 몇 번째 대화인지, 어느 모델이 답했는지가 붙음.

```json
{"hits": [{"kind": "turn", "session_id": "codex:019a…", "turn_no": 7,
  "excerpt": "…캐시 만료는 15분으로 두고, 로그인 직후에는 바로 비움…",
  "models": [{"model": "gpt-5.6-sol", "effort": "medium"}]}]}
```

AI는 필요하면 `recent(session_id)`로 그 앞뒤 대화를 읽고, `history("docs/cache.md")`로 그때 문서가 어떻게 바뀌었는지 확인한 뒤 답함. 위 값은 설명용 예시임.

## 무엇을 남기나
- **대화 쌍**: 사용자가 친 말 + 모델의 최종 답. 답이 나오기 전에 더 보낸 메시지는 같은 대화 쌍의 추가 메시지로 묶음. 답한 모델 이름과 추론 강도(effort)를 함께 둠. 도구 호출·결과는 저장하지 않고, 원본 세션 기록 파일 경로만 둠.
- **질문·답**: AI가 내민 선택지 질문과 사용자가 고른 답(버린 선택지 포함).
- **문서 이력**: 프로젝트의 `.md` 파일과 전역 규칙 파일(`~/.claude/CLAUDE.md` 등)의 본문 사본과 unified diff. 어느 대화에서 고쳤는지, 사람이 직접 고쳤는지 구분됨. 산출물(pptx·pdf·png 등)은 본문 없이 경로·해시·크기만 둠.
- 파일이 정본이고 DB는 사본과 이력임. 저장 전에 알려진 형식의 비밀값을 가리지만 모든 형식을 잡는다는 보장은 없음.
- DB: `~/.ieodo/records.db`(libSQL). 표는 쌓기만 하고 고치거나 지울 수 없음(DB trigger로 강제). 검색은 한국어 형태소(lindera ko-dic) 기반 글자 검색 하나이며 의미 검색(임베딩)은 없음.

## 요구 사항
- macOS에서만 확인함. Linux는 코드상 동작할 것으로 보이나 미확인이며, 서버 상주는 launchd 대신 systemd 등으로 직접 띄워야 함. Windows는 지원하지 않음(설치기가 Unix 파일 잠금 `fcntl`과 POSIX 셸 문법의 hook 명령을 씀).
- Rust 1.85 이상 (edition 2024), Python 3 (수집기. 기존 `.codex/config.toml`을 검사할 때는 3.11 이상)
- 첫 빌드는 한국어 사전(lindera ko-dic)을 내려받아 실행 파일에 넣으므로 인터넷이 필요함

## 설치
1. 빌드하고 실행 파일을 둠.
   ```bash
   cargo build --release --locked
   mkdir -p ~/.local/bin ~/.ieodo/logs
   cp target/release/ieodo ~/.local/bin/ieodo
   ```
2. 서버를 상주시킴(macOS). `contrib/com.ieodo.server.plist`의 `HOME_DIR`을 홈 폴더 경로로 바꿔 `~/Library/LaunchAgents/`에 두고 불러옴.
   ```bash
   sed "s|HOME_DIR|$HOME|g" contrib/com.ieodo.server.plist > ~/Library/LaunchAgents/com.ieodo.server.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ieodo.server.plist
   curl -s http://127.0.0.1:7341/health
   ```
   이후 코드를 고치면 `./deploy.sh`가 빌드·교체·재시작을 함.
3. AI 클라이언트에 MCP 서버로 연결함. 클라이언트 이름(`/mcp/<이름>`, `--client <이름>`)은 기록에 남는 구분용임.
   - Claude Code: `claude mcp add --transport http --scope user ieodo http://127.0.0.1:7341/mcp/claude-code`
   - Codex (`~/.codex/config.toml`):
     ```toml
     [mcp_servers.ieodo]
     command = "/Users/you/.local/bin/ieodo"
     args = ["connect", "--client", "codex"]
     ```
   - Antigravity (`~/.gemini/antigravity/mcp_config.json`의 `mcpServers`): `"ieodo": {"command": "/Users/you/.local/bin/ieodo", "args": ["connect", "--client", "agy"]}`
4. 기록할 프로젝트마다 수집기 hook을 설치함(아래 "수집기"의 `ieodo setup`). 등록하지 않은 폴더의 대화는 남지 않음.

## MCP 도구
| 도구 | 하는 일 | 누가 씀 |
|---|---|---|
| `search` | 대화·문서·질문답에서 단어로 찾음. 결과마다 세션·대화 번호 또는 문서 경로와 답한 모델이 붙음 | AI |
| `recent` | 프로젝트나 세션의 최근 대화 쌍(새것부터)과 그 대화의 추가 메시지·모델·질문답·고친 문서 | AI |
| `history` | 문서 하나의 수정 이력(시각, 세션·대화, diff) | AI |
| `record` | 한 세션의 대화 쌍·모델·문서 변경·질문답을 묶음으로 넣음 | 수집기만 |

새 세션에는 지난 맥락을 자동으로 넣지 않음. 지난 작업이 필요하면 AI가 `search`→`recent`→`history` 순으로 찾음. 찾는 법은 MCP 연결 때 보내는 안내(`src/instructions.txt`)에 있음.

## 수집기
`integrations/continuity/ieodo_hooks.py`(Python 표준 라이브러리만 씀)가 Claude Code·Codex·Antigravity의 hook으로 돎.

| hook | 하는 일 |
|---|---|
| `UserPromptSubmit` | 사용자가 친 말(`prompt`)로 대화 쌍을 엶 |
| `Stop` | 최종 답(`last_assistant_message`)을 채움. 답하는 동안 바뀐 파일은 이 대화로 기록 |
| `SessionStart` | 세션 등록. 지난 답 이후 바뀐 파일은 사람이 고친 것으로 기록 |
| `SessionEnd` | 남은 것을 마무리 |

- hook이 놓친 대화 쌍(세션 도중 설치, hook 오류)은 원본 세션 기록 파일에서 찾아 채움. 모델·effort·질문답도 이 파일에서 가져옴.
- 문서 변경은 폴더 스냅샷(파일별 크기·수정 시각·해시) 비교로 찾음. git 저장소면 git이 무시하는 경로는 제외함.
- 보낼 것은 로컬 SQLite 대기열에 두고, 서버가 받은 것만 지움. 서버가 거부한 항목만 따로 격리함.
- Esc로 끊은 대화는 답이 빈 채로 남음.
- Antigravity는 hook에 사용자 말과 답을 주지 않아 둘 다 원본 기록 파일(`transcript_full.jsonl`, 없으면 `transcript.jsonl`)에서 읽음. 쓰는 hook은 `SessionStart`·`PreInvocation`(답의 첫 모델 호출 때만 사용자 말을 받은 시점으로 봄)·`Stop`. 다른 대화가 띄운 하위 에이전트 대화는 수집하지 않음. 이번 메시지로 띄운 하위 에이전트·백그라운드 작업이 끝나지 않았으면 답을 닫지 않고, 그 결과로 다시 깨어나 낸 마지막 답을 저장함(최대 2시간).

설치는 터미널을 쓸 수 있는 AI가 MCP 안내에 따라 직접 함. 실행 파일 안에 수집기가 들어 있어 소스 폴더가 필요 없음. Python 3 필요.

```bash
ieodo setup --project my-project --project-dir /absolute/repository --harness claude --check
ieodo setup --project my-project --project-dir /absolute/repository --harness claude --apply
```

- Codex는 `--harness codex`, Antigravity는 `--harness antigravity`, Codex·Claude 둘은 `--harness both`, 셋 다는 `--harness all`.
- 등록한 저장소만 수집하고 다른 폴더는 거절함. 프로젝트의 `.codex/hooks.json`, `.claude/settings.local.json`, `.agents/hooks.json`(Antigravity)에서 ieodo 소유 hook만 추가·교체하고, 바뀌는 파일은 백업함.
- `configured`는 설정 파일을 확인했다는 뜻일 뿐임. hook은 다음 세션부터 돎. 출력의 `health_command`로 대기·격리 상태를 봄.
- 세 hook 설정 파일에는 이 컴퓨터의 절대 경로가 들어가므로 git에 올리지 않음(이 저장소의 `.gitignore` 참고). Antigravity는 앱의 설정 > Customizations > Hooks에서 켜고 끌 수 있고, 꺼 둔 hook(`enabled: false`)은 다시 설치해도 꺼진 채로 둠([Antigravity hook 안내](https://antigravity.google/docs/hooks)).
- Codex는 프로젝트 설정과 hook 각각의 신뢰가 필요할 수 있음([Codex hook 안내](https://learn.chatgpt.com/docs/hooks), [Claude hook 안내](https://code.claude.com/docs/en/hooks)).

## 실행
```bash
ieodo serve                 # 서버 하나 (127.0.0.1:7341), 모든 클라이언트가 여기로 연결
ieodo connect --client agy  # stdio만 되는 클라이언트용 중계
ieodo stdio --client test   # 서버 없이 DB를 직접 여는 모드
./deploy.sh                      # 릴리스 빌드 → ~/.local/bin/ieodo → launchd 재시작
```
HTTP 클라이언트는 `http://127.0.0.1:7341/mcp/<클라이언트이름>`으로 연결함. `/health`는 표별 행 수를 돌려줌.

## 검증
```bash
cargo test --locked
python3 -m unittest discover -s integrations/continuity -p 'test_*.py' -v
```

## 환경변수
`IEODO_HOME`(기본 `~/.ieodo`), `IEODO_ALLOW_REMOTE=1`(loopback 외 주소 허용).

## 한계
- 비밀값 가리기는 알려진 형식(API 키, 토큰, `PASSWORD=` 꼴 등)만 잡음. 모든 비밀을 막는다는 보장은 없음.
- `tests/setup.rs`는 실제 서버를 띄워 hook이 1.2초 안에 보내기를 끝내길 기대함. 컴퓨터가 매우 바쁘면 디버그 빌드에서 시간 초과로 실패할 수 있음(`cargo test --release`로는 통과).
- Antigravity hook은 2026-10 기준 공식 문서와 실제 대화로 확인함. 하위 에이전트를 기다리는 동작은 테스트와 실제 기록 파일 재생으로만 확인함.

## 변경 내역
[CHANGELOG.md](CHANGELOG.md) 참고.

## 라이선스
MIT. [LICENSE](LICENSE) 참고.
