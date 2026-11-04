# Changelog

English | 한국어 (각 항목 아래)

## Renamed to ieodo — 2026-11-04

### Changed
- **The project is now ieodo (이어도); it was groundlink.** Every identifier follows: the binary and Cargo package `ieodo`, the data folder `~/.ieodo`, the launchd service `com.ieodo.server`, the env vars `IEODO_HOME` and `IEODO_ALLOW_REMOTE`, the capture scripts `ieodo_hooks.py` and `ieodo_setup.py`, and the MCP server name. The tools and the database format are unchanged.
  - 프로젝트 이름을 groundlink에서 ieodo(이어도)로 바꿈. 실행 파일·Cargo 패키지·데이터 폴더 `~/.ieodo`·launchd `com.ieodo.server`·환경변수 `IEODO_*`·수집기 파일·MCP 서버 이름이 모두 바뀜. 도구와 DB 형식은 그대로임.
- **`ieodo setup --apply` replaces hooks installed by groundlink**, so a project does not end up with both. An Antigravity hook you switched off stays off.
  - `ieodo setup --apply`가 groundlink가 설치한 hook을 찾아 새 것으로 바꿈(중복 설치 없음). 꺼 둔 Antigravity hook은 꺼진 채 유지함.

### Upgrading from groundlink
1. Stop the old service: `launchctl bootout gui/$(id -u)/com.groundlink.server`, and remove `~/Library/LaunchAgents/com.groundlink.server.plist`.
2. Move the data: `mv ~/.groundlink ~/.ieodo`. In `~/.ieodo/continuity/config.json`, change `state_dir` to the new folder.
3. Build and install `ieodo`, and load `contrib/com.ieodo.server.plist` (README, Install).
4. Rename the MCP server in each client from `groundlink` to `ieodo` and point it at `~/.local/bin/ieodo`.
5. Run `ieodo setup … --apply` again for each project. Codex asks you to trust the new hook commands (`/hooks`).
- 옛 서비스를 내리고 plist를 지움 → `~/.groundlink`를 `~/.ieodo`로 옮기고 `config.json`의 `state_dir`을 고침 → `ieodo`를 설치하고 새 plist를 불러옴 → 각 클라이언트의 MCP 이름과 경로를 `ieodo`로 바꿈 → 프로젝트마다 `ieodo setup … --apply`를 다시 실행함(Codex는 `/hooks` 승인 필요).

## 0.3.2 — 2026-11-02

### Changed
- **Messages sent before the answer now stay in one turn (Claude Code, Codex, Antigravity).** Each such message used to become its own turn, so the request that started the work showed no answer and the final answer sat under the last short follow-up. They are now stored as added messages of the same turn, in a new append-only table `turn_message` with their own times. `recent` shows them as `added_messages`, and `search` finds them. A message starts a new turn when the user interrupted the answer, or when the turn began more than 2 hours earlier. Turns already stored are not changed.
  - 답이 나오기 전에 보낸 메시지를 같은 대화 쌍에 묶음(Claude Code, Codex, Antigravity). 예전에는 메시지마다 대화 쌍이 되어, 일을 시킨 메시지에는 답이 없고 마지막 짧은 메시지에 최종 답이 붙었음. 이제 새 표 `turn_message`에 보낸 시각과 함께 저장함(수정·삭제 금지). `recent`는 `added_messages`로 보여 주고 `search`로도 찾을 수 있음. 사용자가 중단했거나 대화 쌍 시작 뒤 2시간이 지나면 새 대화 쌍이 됨. 이미 저장된 대화 쌍은 바뀌지 않음.

### Fixed
- **Antigravity: tool output quoting an old launch notice held the answer.** Reading a log that contained "running as a background task" counted as a new task, so the answer waited for up to 2 hours. Only the tool's own notice counts now.
  - Antigravity: 옛 기록을 읽은 출력 안의 작업 시작 문구를 새 작업으로 세어 답이 최대 2시간 밀리던 문제를 고침. 이제 도구 알림 자체만 셈.

### Known limit
- **Antigravity:** a server started by the same message never reports back, so its answer is stored when your next message arrives. The answer is not lost.
  - Antigravity: 같은 메시지에서 띄운 서버는 끝났다는 알림이 오지 않아, 답이 다음 메시지 때 저장됨. 답은 사라지지 않음.

### Upgrading
Rebuild and restart the server (`./deploy.sh`). The new table is created at start; `groundlink setup` is not needed. Deploy the server and the hook together: the older server rejects added messages.
- 서버를 다시 빌드하고 재시작하면 됨(`./deploy.sh`). 새 표는 시작할 때 만들어지고 `groundlink setup`은 필요 없음.

## 0.3.1 — 2026-10-30

### Fixed
- **Antigravity: answers of long multi-agent work were cut short.** When a message started subagents or background tasks, the capture closed the turn at the first `Stop`, so an interim note ("started 3 subagents") was stored as the answer and the final report written after the subagents reported back was lost. The answer can be filled only once, so it could not be corrected later. The turn now stays open until every subagent and background task started for that message has reported back (up to 2 hours, or until the next user message), and the last answer is stored.
  - Antigravity: 하위 에이전트·백그라운드 작업을 띄운 메시지의 답이 첫 안내로 굳고 최종 보고가 빠지던 문제를 고침. 이번 메시지로 띄운 작업이 모두 끝났다고 알릴 때까지(최대 2시간, 또는 다음 사용자 메시지까지) 답을 닫지 않고 마지막 답을 저장함.
- **Antigravity: files changed by that work were credited to a person.** When the app woke the conversation with a report, changes made since the last answer were recorded as the user's own edits. They now belong to the turn that started the work.
  - Antigravity: 작업 결과로 대화가 다시 깨어날 때 그사이 바뀐 파일이 사람이 고친 것으로 기록되던 문제를 고침. 이제 작업을 띄운 대화 쌍의 변경으로 기록함.

### Upgrading
Rebuild and restart the server (`./deploy.sh`). The capture hook is embedded in the binary and rewritten at every server start, so installed project hooks pick up the fix without running `groundlink setup` again.
- 서버를 다시 빌드하고 재시작하면 됨(`./deploy.sh`). 수집기는 실행 파일에 들어 있고 서버 시작 때마다 다시 쓰이므로 `groundlink setup`을 다시 할 필요 없음.

## 0.3.0 — 2026-10-30

First public release.
- 첫 공개 버전.
