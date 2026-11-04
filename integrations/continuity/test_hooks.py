#!/usr/bin/env python3
"""Run: python3 -m unittest discover -s integrations/continuity -p 'test_*.py'.

The capture adapter (wiki/schema.md, "수집기 ②"): each hook call turns what the
harness hands over into turn pairs and sends them to ieodo's `record`
tool. The user's words come from UserPromptSubmit's `prompt`, the answer from
Stop's `last_assistant_message`. Pairs the hooks missed are rebuilt from the
session transcript; only then are the blocks the harness appends to a user
message cut away.
"""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

import ieodo_hooks as hooks
from fake_ieodo import FakeServer


def claude_user(text, ts=None):
    line = {"type": "user", "message": {"role": "user", "content": text}}
    if ts:
        line["timestamp"] = ts
    return line


def claude_assistant(blocks, ts=None, model="claude-opus-5-5", effort="high"):
    line = {"type": "assistant", "effort": effort,
            "message": {"role": "assistant", "model": model, "content": blocks}}
    if ts:
        line["timestamp"] = ts
    return line


def text(value):
    return {"type": "text", "text": value}


def tool_use(call_id, name="Bash", tool_input=None):
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input or {}}


def tool_result(call_id, content="결과", ts=None, extra=None):
    line = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": content}]}}
    if ts:
        line["timestamp"] = ts
    if extra:
        line.update(extra)
    return line


def agy_user(value, ts="2026-10-30T03:00:00Z", notice=""):
    content = f"<USER_REQUEST>\n{value}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: x.\n</ADDITIONAL_METADATA>"
    if notice:
        content += f"\n<USER_SETTINGS_CHANGE>\n{notice}\n</USER_SETTINGS_CHANGE>"
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": ts, "content": content}


def agy_answer(value=None, tools=None, ts="2026-10-30T03:00:10Z"):
    line = {"type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE", "created_at": ts}
    if value is not None:
        line["content"] = value
    if tools:
        line["tool_calls"] = tools
    return line


def agy_started(conversation=None, task=None, ts="2026-10-30T03:00:05Z"):
    # Started work is held for at most WORKING_SECONDS, by the clock: pass now() to keep it pending.
    if conversation:
        content = ("Created At: x\nCreated the following subagent:\n{\n  \"conversationId\":  \"" + conversation
                   + "\",\n  \"workspaceUris\":  []\n}")
    else:
        content = f"Created At: x\nTool is running as a background task with task id: {task}\nTask Description: run"
    return {"type": "GENERIC", "source": "MODEL", "status": "RUNNING", "created_at": ts, "content": content}


def agy_finished(sender, ts="2026-10-30T03:10:00Z"):
    content = ("The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n<SYSTEM_MESSAGE>\n"
               f"[Message] timestamp={ts} sender={sender} priority=MESSAGE_PRIORITY_HIGH content=끝났어\n</SYSTEM_MESSAGE>")
    return {"type": "SYSTEM_MESSAGE", "source": "SYSTEM", "status": "DONE", "created_at": ts, "content": content}


ARGON = ("The user changed setting `Model Selection` from None to Gemini 4 Argon (High). "
         "No need to comment on this change if the user doesn't ask about it.")


def codex_message(role, value, ts=None):
    line = {"type": "response_item", "payload": {"type": "message", "role": role, "content": [
        {"type": "input_text" if role == "user" else "output_text", "text": value}]}}
    if ts:
        line["timestamp"] = ts
    return line


class Base(unittest.TestCase):
    harness = "claude"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "repo"
        self.project.mkdir()
        self.server = FakeServer()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "projects": [{"root": str(self.project), "project": "demo"}],
            "state_dir": str(self.root / "state"),
            "server_url": self.server.url,
            "global_documents": [],
        }))
        self.adapter = hooks.Adapter(self.config, self.harness)
        # Most tests write no answer line; waiting for one only slows them.
        self.adapter.answer_wait = 0
        self.transcript = self.root / "session.jsonl"
        self.transcript.write_text("")
        self.sid = self.harness + ":s1"

    def tearDown(self):
        self.adapter.db.close()
        self.server.close()
        self.temp.cleanup()

    def write(self, *lines):
        with self.transcript.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(json.dumps(line, ensure_ascii=False) + "\n")

    def hook(self, event, **fields):
        payload = {"hook_event_name": event, "session_id": "s1", "cwd": str(self.project),
                   "transcript_path": str(self.transcript), **fields}
        return self.adapter.hook(payload)

    def turns(self):
        return self.server.session_turns(self.sid)

    def sent_turn_items(self, turn_no):
        return [t for call in self.server.calls for t in call.get("turns", []) if t["turn_no"] == turn_no]


class TurnPairTests(Base):
    def test_prompt_then_stop_makes_one_answered_pair(self):
        out = self.hook("SessionStart", source="startup")
        # No saved context is pushed into a new session.
        self.assertNotIn("hookSpecificOutput", out)
        self.hook("UserPromptSubmit", prompt="첫 질문", prompt_id="p1")
        first = self.server.turn(self.sid, 1)
        self.assertEqual(first["user_text"], "첫 질문")
        self.assertNotIn("assistant_text", first)
        self.assertRegex(first["user_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

        self.hook("Stop", last_assistant_message="첫 답", prompt_id="p1", stop_hook_active=False)
        first = self.server.turn(self.sid, 1)
        self.assertEqual(first["assistant_text"], "첫 답")
        self.assertRegex(first["assistant_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

        envelope = self.server.calls[-1]
        self.assertEqual(envelope["project"], {"name": "demo", "root_path": str(self.project)})
        self.assertEqual(envelope["session"]["session_id"], self.sid)
        self.assertEqual(envelope["session"]["harness"], "claude-code")
        self.assertEqual(envelope["session"]["transcript_path"], str(self.transcript))
        # started_at never moves once a session is known.
        self.assertEqual(len({c["session"]["started_at"] for c in self.server.calls}), 1)

    def test_interrupted_turn_keeps_no_answer_and_the_next_answer_goes_to_its_own_prompt(self):
        self.hook("UserPromptSubmit", prompt="A 해줘", prompt_id="p1")
        # Esc: no Stop hook for p1, and the transcript says so.
        self.write(claude_user("A 해줘"), claude_user("[Request interrupted by user]"))
        self.hook("UserPromptSubmit", prompt="아니 B로 해", prompt_id="p2")
        self.hook("Stop", last_assistant_message="B로 했어", prompt_id="p2")
        first, second = self.turns()
        self.assertEqual((first["turn_no"], first["user_text"]), (1, "A 해줘"))
        self.assertNotIn("assistant_text", first)
        self.assertEqual((second["turn_no"], second["user_text"], second["assistant_text"]), (2, "아니 B로 해", "B로 했어"))

    def test_a_message_sent_before_the_answer_joins_the_turn(self):
        self.hook("UserPromptSubmit", prompt="A 해줘", prompt_id="p1")
        self.hook("UserPromptSubmit", prompt="B도 같이", prompt_id="p2")
        self.hook("Stop", last_assistant_message="둘 다 했어", prompt_id="p2")
        self.hook("UserPromptSubmit", prompt="다음", prompt_id="p3")
        self.assertEqual([(t["turn_no"], t["user_text"]) for t in self.turns()], [(1, "A 해줘"), (2, "다음")])
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "둘 다 했어")
        self.assertEqual([(k[1:], m["text"]) for k, m in self.server.messages.items()], [((1, 1), "B도 같이")])

    def test_an_added_message_seen_by_both_the_hook_and_the_walk_is_stored_once(self):
        self.hook("UserPromptSubmit", prompt="A 해줘", prompt_id="p1")
        self.hook("UserPromptSubmit", prompt="B도 같이", prompt_id="p2")
        self.write(claude_user("A 해줘"), claude_assistant([tool_use("t1")]),
                   claude_user("B도 같이"), claude_assistant([text("했어")]))
        self.hook("Stop", last_assistant_message="했어", prompt_id="p2")
        self.assertEqual(len(self.turns()), 1)
        self.assertEqual(len(self.server.messages), 1)

    def test_an_added_message_is_redacted(self):
        self.hook("UserPromptSubmit", prompt="A 해줘", prompt_id="p1")
        self.hook("UserPromptSubmit", prompt="GITHUB_TOKEN=ghp_abcdefghijklmnop1234567890 써", prompt_id="p2")
        self.assertNotIn("ghp_abcdefghijklmnop1234567890", json.dumps(self.server.calls))

    def test_text_that_is_only_harness_blocks_makes_no_turn(self):
        self.hook("UserPromptSubmit", prompt="<task-notification>\n<task-id>x</task-id>\n</task-notification>")
        self.assertEqual(self.turns(), [])
        self.hook("UserPromptSubmit", prompt="진짜 질문", prompt_id="p2")
        self.assertEqual([(t["turn_no"], t["user_text"]) for t in self.turns()], [(1, "진짜 질문")])

    def test_an_unsent_turn_is_replaced_by_its_answered_version(self):
        self.server.fail = True
        self.hook("UserPromptSubmit", prompt="질문", prompt_id="p1")
        self.hook("Stop", last_assistant_message="답", prompt_id="p1")
        self.server.fail = False
        self.hook("UserPromptSubmit", prompt="다음", prompt_id="p2")
        sent = self.sent_turn_items(1)
        self.assertEqual(len(sent), 1, sent)
        self.assertEqual(sent[0]["assistant_text"], "답")
        self.assertEqual(self.adapter.pending(), 0)


class FillInTests(Base):
    def test_missed_pairs_are_rebuilt_from_the_transcript(self):
        self.write(
            claude_user("첫 질문\n<system-reminder>\n붙은 글\n</system-reminder>\n", ts="2026-10-28T01:00:00Z"),
            claude_assistant([text("먼저 볼게"), tool_use("t1")], ts="2026-10-28T01:00:05Z"),
            tool_result("t1", ts="2026-10-28T01:00:06Z"),
            claude_assistant([text("첫 답이야")], ts="2026-10-28T01:00:10Z"),
            claude_user("둘째 질문", ts="2026-10-28T01:01:00Z"),
            claude_assistant([text("쓰다가"), tool_use("t2")], ts="2026-10-28T01:01:05Z"),
            claude_user([text("[Request interrupted by user for tool use]")], ts="2026-10-28T01:01:06Z"),
            claude_user("셋째 질문", ts="2026-10-28T01:02:00Z"),
        )
        # The hooks were installed mid-session: this is the first one they see,
        # and the transcript already holds the prompt it carries.
        self.hook("UserPromptSubmit", prompt="셋째 질문", prompt_id="p3")
        turns = self.turns()
        self.assertEqual([(t["turn_no"], t["user_text"]) for t in turns],
                         [(1, "첫 질문"), (2, "둘째 질문"), (3, "셋째 질문")])
        first = turns[0]
        self.assertEqual(first["assistant_text"], "첫 답이야")
        self.assertEqual((first["user_at"], first["assistant_at"]), ("2026-10-28T01:00:00Z", "2026-10-28T01:00:10Z"))
        # An interrupted answer is not an answer.
        self.assertNotIn("assistant_text", turns[1])
        self.assertNotIn("assistant_text", turns[2])
        self.assertIn((self.sid, 1, "claude-opus-5-5", "high"), self.server.models)

        self.write(claude_assistant([text("셋째 답")], ts="2026-10-28T01:02:30Z"))
        self.hook("Stop", last_assistant_message="셋째 답", prompt_id="p3")
        self.hook("SessionEnd", reason="exit")
        self.assertEqual(len(self.turns()), 3)
        self.assertEqual(self.server.turn(self.sid, 3)["assistant_text"], "셋째 답")
        self.assertEqual(self.adapter.status()["quarantined"], 0)

    def test_fill_in_never_resends_an_answer_the_hook_gave(self):
        self.hook("UserPromptSubmit", prompt="질문", prompt_id="p1")
        self.hook("Stop", last_assistant_message="hook이 준 답", prompt_id="p1")
        self.write(claude_user("질문", ts="2026-10-28T01:00:00Z"),
                   claude_assistant([text("파일에 적힌 답")], ts="2026-10-28T01:00:10Z"),
                   claude_user("다음", ts="2026-10-28T01:01:00Z"))
        self.hook("UserPromptSubmit", prompt="다음", prompt_id="p2")
        self.assertEqual([t["turn_no"] for t in self.turns()], [1, 2])
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "hook이 준 답")
        self.assertEqual(self.adapter.status()["quarantined"], 0)
        self.assertEqual([c for c in self.server.calls if c.get("turns") and any(
            t["turn_no"] == 1 and t.get("assistant_text") == "파일에 적힌 답" for t in c["turns"])], [])

    def test_unknown_blocks_are_kept_and_reported(self):
        self.write(claude_user("안녕\n<weird_block>\n내용\n</weird_block>", ts="2026-10-28T01:00:00Z"),
                   claude_assistant([text("응")], ts="2026-10-28T01:00:01Z"),
                   claude_user("다음", ts="2026-10-28T01:01:00Z"))
        self.hook("UserPromptSubmit", prompt="다음", prompt_id="p2")
        self.assertEqual(self.server.turn(self.sid, 1)["user_text"], "안녕\n<weird_block>\n내용\n</weird_block>")
        self.assertIn("weird_block", self.adapter.status()["unknown_tags"])


class AuditRegressionTests(Base):
    """Findings of the 2026-10-28 review of the rewritten adapter."""

    def test_a_message_typed_while_the_answer_runs_joins_its_turn(self):
        self.write(claude_user("첫 질문", ts="2026-10-28T01:00:00Z"),
                   claude_assistant([text("보는 중"), tool_use("t1")], ts="2026-10-28T01:00:05Z"),
                   {"type": "attachment", "timestamp": "2026-10-28T01:00:07Z", "attachment": {
                       "type": "queued_command", "prompt": "그것도 같이 봐줘", "commandMode": "prompt",
                       "origin": {"kind": "human"}, "timestamp": "2026-10-28T01:00:07Z", "humanTurn": True}},
                   tool_result("t1", ts="2026-10-28T01:00:08Z"),
                   claude_assistant([text("둘 다 봤어")], ts="2026-10-28T01:00:20Z"),
                   claude_user("다음", ts="2026-10-28T01:01:00Z"))
        self.hook("UserPromptSubmit", prompt="다음", prompt_id="p3")
        self.assertEqual([(t["turn_no"], t["user_text"]) for t in self.turns()],
                         [(1, "첫 질문"), (2, "다음")])
        self.assertEqual(self.server.messages[(self.sid, 1, 1)]["sent_at"], "2026-10-28T01:00:07Z")
        self.assertEqual(self.server.messages[(self.sid, 1, 1)]["text"], "그것도 같이 봐줘")
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "둘 다 봤어")

    def test_a_message_hours_after_an_unanswered_one_starts_a_new_turn(self):
        self.write(claude_user("첫 질문", ts="2026-10-28T01:00:00Z"),
                   claude_user("한참 뒤 질문", ts="2026-10-28T05:00:00Z"))
        self.hook("Stop", last_assistant_message="답", prompt_id="unseen")
        self.assertEqual([t["user_text"] for t in self.turns()], ["첫 질문", "한참 뒤 질문"])

    def test_bash_mode_output_is_not_the_users_words(self):
        self.hook("UserPromptSubmit", prompt="<bash-input>ls</bash-input>\n<bash-stdout>secret.txt</bash-stdout><bash-stderr></bash-stderr>")
        self.assertEqual(self.turns(), [])

    def test_answer_text_waiting_in_the_walk_is_redacted_on_disk(self):
        self.write(claude_user("질문", ts="2026-10-28T01:00:00Z"),
                   claude_assistant([text("GITHUB_TOKEN=ghp_abcdefghijklmnop1234567890 를 써")], ts="2026-10-28T01:00:05Z"))
        self.hook("UserPromptSubmit", prompt="질문", prompt_id="p1")
        self.adapter.db.commit()
        raw = b"".join(p.read_bytes() for p in (self.root / "state").iterdir() if p.is_file())
        self.assertNotIn(b"ghp_abcdefghijklmnop1234567890", raw)

    def test_an_answer_queued_during_delivery_is_not_deleted(self):
        self.hook("UserPromptSubmit", prompt="질문", prompt_id="p1")
        self.server.fail = True
        self.hook("Stop", last_assistant_message="답", prompt_id="p1")
        self.server.fail = False
        original = self.adapter.call

        def racing(name, args, timeout=2.0):
            result = original(name, args, timeout)
            # Another hook process re-queues the same turn while this call runs.
            other = hooks.Adapter(self.config, "claude")
            try:
                with other.db:
                    other.enqueue(f"turn:{self.sid}:1", self.sid, "turns",
                                  {"turn_no": 1, "user_text": "질문", "user_at": "x", "assistant_text": "새 답"})
            finally:
                other.db.close()
            return result

        self.adapter.call = racing
        self.adapter.flush()
        self.adapter.call = original
        self.assertEqual(self.adapter.pending(), 1)


class LiveFindingsTests(Base):
    """Findings from the first real sessions after deployment (2026-10-28)."""

    def test_capture_rebuilds_a_finished_session_from_its_transcript(self):
        self.write(claude_user("질문", ts="2026-10-28T01:00:00Z"),
                   claude_assistant([text("답")], ts="2026-10-28T01:00:10Z"),
                   claude_user("둘째", ts="2026-10-28T01:01:00Z"),
                   claude_assistant([text("둘째 답")], ts="2026-10-28T01:01:10Z"))
        with contextlib.redirect_stdout(io.StringIO()):
            code = hooks.main(["--config", str(self.config), "--harness", "claude", "capture",
                               "--transcript", str(self.transcript), "--session-id", "s1",
                               "--project-dir", str(self.project)])
        self.assertEqual(code, 0)
        self.assertEqual([(t["turn_no"], t["user_text"], t.get("assistant_text")) for t in self.turns()],
                         [(1, "질문", "답"), (2, "둘째", "둘째 답")])


class StopPairingTests(Base):
    def test_an_answer_to_a_background_report_is_not_put_on_an_open_turn(self):
        self.hook("UserPromptSubmit", prompt="질문", prompt_id="p1")
        self.hook("UserPromptSubmit", prompt="<task-notification>\n<task-id>x</task-id>\n</task-notification>", prompt_id="p2")
        self.hook("Stop", last_assistant_message="보고를 받았어", prompt_id="p2")
        self.assertNotIn("assistant_text", self.server.turn(self.sid, 1))

    def test_a_stop_for_a_prompt_the_hooks_never_saw_answers_the_rebuilt_turn(self):
        self.write(claude_user("설치 전에 친 말", ts="2026-10-28T01:00:00Z"))
        self.hook("Stop", last_assistant_message="답", prompt_id="unseen")
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "답")


class CodexAbortTests(Base):
    harness = "codex"

    def test_the_apps_suggestion_sessions_are_ignored(self):
        prompt = ("This is an app-generated request for a suggested next user message. "
                  "The user did not type it.")
        out = self.adapter.hook({"hook_event_name": "UserPromptSubmit", "session_id": "sugg",
                                 "cwd": str(self.project), "prompt": prompt})
        self.assertEqual(out, {})
        self.adapter.hook({"hook_event_name": "Stop", "session_id": "sugg", "cwd": str(self.project),
                           "last_assistant_message": "추천 문장"})
        self.assertEqual(self.server.session_turns("codex:sugg"), [])
        self.assertNotIn("codex:sugg", self.server.sessions)

    def test_an_aborted_turn_keeps_no_answer_and_the_notice_is_no_turn(self):
        self.write(codex_message("user", "refactor X", ts="2026-10-28T01:00:00Z"),
                   codex_message("assistant", "I'll start by reading the files.", ts="2026-10-28T01:00:05Z"),
                   codex_message("user", "<turn_aborted>\nThe user interrupted the previous turn.\n</turn_aborted>", ts="2026-10-28T01:00:09Z"),
                   codex_message("user", "다른 걸 해", ts="2026-10-28T01:01:00Z"))
        self.hook("UserPromptSubmit", prompt="다른 걸 해")
        self.assertEqual([(t["turn_no"], t["user_text"]) for t in self.turns()], [(1, "refactor X"), (2, "다른 걸 해")])
        self.assertNotIn("assistant_text", self.server.turn(self.sid, 1))


class StripTests(unittest.TestCase):
    def test_harness_blocks_are_cut_and_commands_keep_their_name(self):
        cases = [
            ("claude", "record 도구부터 만들어\n<system-reminder>\nAttribution\n</system-reminder>\n", "record 도구부터 만들어"),
            ("claude", "<command-message>init</command-message>\n<command-name>/init</command-name>\n<command-args></command-args>", "/init"),
            ("claude", "<local-command-caveat>Caveat</local-command-caveat>\n<command-name>/model</command-name>\n"
                       "<command-message>model</command-message>\n<command-args>claude-opus-5-5</command-args>", "/model"),
            ("claude", "<local-command-stdout>Set model</local-command-stdout>", ""),
            ("claude", "<task-notification>\n<task-id>x</task-id>\n</task-notification>", ""),
            ("claude", "[Request interrupted by user for tool use]", ""),
            ("claude", "[Request interrupted by user]", ""),
            ("claude", "버튼은 <b>굵게</b> 해줘", "버튼은 <b>굵게</b> 해줘"),
            ("codex", "# AGENTS.md instructions for /r\n\n<INSTRUCTIONS>\n규칙\n</INSTRUCTIONS>", ""),
            ("codex", "<environment_context>\n  <cwd>/r</cwd>\n</environment_context>", ""),
            ("codex", "<environment_context>\n  <cwd>/r</cwd>\n</environment_context>\n고쳐줘", "고쳐줘"),
            ("codex", "<image name=\"shot.png\">\n</image>\n이거 봐", "[이미지: shot.png]\n이거 봐"),
            ("claude", "<image path=\"/tmp/a/b.png\"/>\n이거 봐", "[이미지: b.png]\n이거 봐"),
            ("claude", "<image>\n</image>", "[이미지]"),
        ]
        for harness, raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(hooks.strip_injected(raw, harness), (expected, []))

    def test_unknown_line_blocks_are_kept_and_named(self):
        self.assertEqual(hooks.strip_injected("안녕\n<weird_block>\n내용\n</weird_block>", "claude"),
                         ("안녕\n<weird_block>\n내용\n</weird_block>", ["weird_block"]))


class ModelAndReplyTests(Base):
    def test_claude_button_answers_attach_to_the_asking_turn(self):
        self.write(claude_user("정리해줘", ts="2026-10-28T01:00:00Z"))
        self.hook("UserPromptSubmit", prompt="정리해줘", prompt_id="p1")
        question = {"question": "어디까지 지울까?", "header": "삭제 범위", "multiSelect": False,
                    "options": [{"label": "지침만", "description": "a"}, {"label": "문서까지", "description": "b"}]}
        self.write(claude_assistant([tool_use("toolu_q", "AskUserQuestion", {"questions": [question]})], ts="2026-10-28T01:00:05Z"),
                   tool_result("toolu_q", "answered", ts="2026-10-28T01:00:40Z",
                               extra={"toolUseResult": {"questions": [question], "answers": {"어디까지 지울까?": "지침만"}}}),
                   claude_assistant([text("지침만 지울게")], ts="2026-10-28T01:00:50Z"))
        self.hook("Stop", last_assistant_message="지침만 지울게", prompt_id="p1")
        reply = self.server.replies[(self.sid, "toolu_q#0")]
        self.assertEqual((reply["turn_no"], reply["question"], reply["answer"]), (1, "어디까지 지울까?", "지침만"))
        self.assertEqual(reply["options"], ["지침만", "문서까지"])
        self.assertEqual(reply["answered_at"], "2026-10-28T01:00:40Z")


class AnswerLineWaitTests(Base):
    def test_stop_waits_for_an_answer_line_written_just_after_it(self):
        self.adapter.answer_wait = 1.0
        self.hook("UserPromptSubmit", prompt="안녕", prompt_id="p1")
        self.write(claude_user("안녕"))
        late = threading.Timer(0.2, self.write, [claude_assistant([text("안녕!")], model="claude-opus-5-5", effort="medium")])
        late.start()
        self.hook("Stop", last_assistant_message="안녕!", prompt_id="p1")
        late.join()
        self.assertIn((self.sid, 1, "claude-opus-5-5", "medium"), self.server.models)

    def test_stop_gives_up_when_no_answer_line_comes(self):
        self.adapter.answer_wait = 0.3
        self.hook("UserPromptSubmit", prompt="안녕", prompt_id="p1")
        started = time.monotonic()
        self.hook("Stop", last_assistant_message="안녕!", prompt_id="p1")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "안녕!")
        # The model is still picked up by the next hook.
        self.write(claude_user("안녕"), claude_assistant([text("안녕!")]))
        self.hook("SessionEnd", reason="exit")
        self.assertIn((self.sid, 1, "claude-opus-5-5", "high"), self.server.models)


class AntigravityTests(Base):
    harness = "antigravity"

    def setUp(self):
        super().setUp()
        # <app>/brain/<conversation>/.system_generated/logs/transcript.jsonl
        self.app = self.root / "app"
        self.logs = self.app / "brain/s1/.system_generated/logs"
        self.logs.mkdir(parents=True)
        self.transcript = self.logs / "transcript.jsonl"
        self.transcript.write_text("")

    def hook(self, event, **fields):
        payload = {"conversationId": "s1", "workspacePaths": [str(self.project)],
                   "transcriptPath": str(self.transcript), "artifactDirectoryPath": str(self.root),
                   "modelName": "gemini-4-argon", "hook_event_name": event, **fields}
        return self.adapter.hook(payload)

    def test_pairs_come_from_the_transcript_with_the_last_answer(self):
        self.hook("SessionStart")
        self.write(agy_user("이 프로젝트 분석해 줘", notice=ARGON))
        self.hook("PreInvocation")
        self.write(agy_answer("먼저 볼게", tools=[{"name": "run_command"}]),
                   {"type": "GENERIC", "source": "MODEL", "content": "결과"},
                   agy_answer("분석 끝났어", ts="2026-10-30T03:01:00Z"))
        self.hook("PreInvocation")
        self.hook("Stop")
        turn = self.server.turn(self.sid, 1)
        self.assertEqual((turn["user_text"], turn["assistant_text"], turn["assistant_at"]),
                         ("이 프로젝트 분석해 줘", "분석 끝났어", "2026-10-30T03:01:00Z"))
        self.assertEqual(self.server.calls[-1]["session"]["harness"], "antigravity")
        # The change notice gives model and effort; the hook's name is not added twice.
        self.assertEqual({m for m in self.server.models if m[0] == self.sid}, {(self.sid, 1, "Gemini 4 Argon", "High")})

    def test_the_hooks_model_name_is_used_without_a_change_notice(self):
        self.write(agy_user("안녕"), agy_answer("안녕!"))
        self.hook("Stop")
        self.assertIn((self.sid, 1, "gemini-4-argon", None), self.server.models)

    def test_a_message_that_cuts_an_answer_short_joins_its_turn(self):
        self.write(agy_user("A 해줘"), agy_answer("하는 중", tools=[{"name": "run_command"}]),
                   agy_user("아니 B로 해", ts="2026-10-30T03:02:00Z"), agy_answer("B로 했어", ts="2026-10-30T03:03:00Z"))
        self.hook("Stop")
        only, = self.turns()
        self.assertEqual((only["user_text"], only["assistant_text"]), ("A 해줘", "B로 했어"))
        self.assertEqual(self.server.messages[(self.sid, 1, 1)]["text"], "아니 B로 해")

    def test_the_untruncated_copy_is_read_when_there(self):
        self.write(agy_user("길게 설명해"), agy_answer("잘린 답"))
        (self.logs / "transcript_full.jsonl").write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in (agy_user("길게 설명해"), agy_answer("잘리지 않은 긴 답"))))
        self.hook("Stop")
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "잘리지 않은 긴 답")

    def test_a_subagent_conversation_is_ignored(self):
        with sqlite3.connect(self.app / "conversation_summaries.db") as db:
            db.execute("CREATE TABLE conversation_summaries(conversation_id TEXT, nesting_depth INTEGER, parent_conversation_id TEXT)")
            db.execute("INSERT INTO conversation_summaries VALUES('s1', 1, 'parent')")
        (self.project / "NOTES.md").write_text("부모가 고치는 중")
        self.write(agy_answer("하위 작업 끝"))
        self.hook("SessionStart")
        self.hook("Stop")
        self.assertEqual(self.server.calls, [])

    def test_only_the_first_model_call_of_an_answer_marks_the_users_turn(self):
        self.hook("SessionStart")
        (self.project / "BEFORE.md").write_text("사람이 먼저 고침")
        self.write(agy_user("문서 고쳐"))
        self.hook("PreInvocation")
        (self.project / "AFTER.md").write_text("에이전트가 고침")
        # A later model call of the same answer must not take the change as a person's.
        self.hook("PreInvocation")
        self.write(agy_answer("고쳤어"))
        self.hook("Stop")
        docs = {item["path"]: item for _sid, item in self.server.documents}
        self.assertNotIn("turn_no", docs[str(self.project / "BEFORE.md")])
        self.assertEqual(docs[str(self.project / "AFTER.md")]["turn_no"], 1)

    def test_the_answer_waits_for_subagents_and_background_tasks(self):
        self.hook("SessionStart")
        self.write(agy_user("하위 에이전트로 검증해"))
        self.hook("PreInvocation")
        self.write(agy_answer(tools=[{"name": "invoke_subagent"}]), agy_started(conversation="c1", ts=hooks.now()),
                   agy_answer("하위 에이전트를 띄웠어"))
        self.hook("Stop")
        self.assertNotIn("assistant_text", self.server.turn(self.sid, 1))
        # The app wakes the conversation with the subagent's report, not the user.
        self.write(agy_finished("c1"))
        self.hook("PreInvocation")
        (self.project / "REPORT.md").write_text("에이전트가 고침")
        self.write(agy_answer(tools=[{"name": "run_command"}]), agy_started(task="s1/task-9", ts=hooks.now()),
                   agy_answer("벤치마크 도는 중"))
        self.hook("Stop")
        self.assertNotIn("assistant_text", self.server.turn(self.sid, 1))
        # The task wrote while nothing ran in the conversation.
        (self.project / "OUT.md").write_text("작업 결과")
        self.write(agy_finished("s1/task-9", ts="2026-10-30T03:20:00Z"))
        self.hook("PreInvocation")
        self.write(agy_answer("최종 보고", ts="2026-10-30T03:21:00Z"))
        self.hook("Stop")
        (turn,) = self.turns()
        self.assertEqual((turn["assistant_text"], turn["assistant_at"]), ("최종 보고", "2026-10-30T03:21:00Z"))
        docs = {item["path"]: item for _sid, item in self.server.documents}
        self.assertEqual(docs[str(self.project / "REPORT.md")]["turn_no"], 1)
        self.assertEqual(docs[str(self.project / "OUT.md")]["turn_no"], 1)

    def test_work_that_never_reports_stops_holding_the_answer(self):
        self.write(agy_user("검증해"), agy_started(conversation="c1", ts="2026-01-01T00:00:00Z"),
                   agy_answer("띄웠어"))
        self.hook("Stop")
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "띄웠어")

    def test_messages_sent_while_it_works_join_the_turn_with_the_final_answer(self):
        self.write(agy_user("혼자 테스트해줘", ts="2026-10-30T11:32:57Z"),
                   agy_user("서브에이전트를 쓰지말라는 건 아니고", ts="2026-10-30T11:33:13Z"),
                   agy_answer(tools=[{"name": "read_file"}], ts="2026-10-30T11:35:00Z"),
                   agy_user("막히면 Opus를 불러", ts="2026-10-30T11:37:37Z"),
                   agy_answer("최종 보고", ts="2026-10-30T11:53:21Z"))
        self.hook("Stop")
        turns = self.turns()
        self.assertEqual([(t["user_text"], t["assistant_text"]) for t in turns], [("혼자 테스트해줘", "최종 보고")])
        self.assertEqual([m["text"] for m in self.server.messages.values()],
                         ["서브에이전트를 쓰지말라는 건 아니고", "막히면 Opus를 불러"])

    def test_a_launch_notice_quoted_in_tool_output_holds_nothing(self):
        quoted = {"type": "GENERIC", "source": "MODEL", "status": "DONE", "created_at": hooks.now(),
                  "content": "Created At: x\nCompleted At: x\nCommand output:\nTool is running as a background "
                             "task with task id: conv/task-1\\nTask Description: old"}
        self.write(agy_user("로그 봐줘"), quoted, agy_answer("다 봤어"))
        self.hook("Stop")
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "다 봤어")

    def test_work_left_running_by_an_earlier_message_does_not_hold_the_next_answer(self):
        self.write(agy_user("서버 띄워"), agy_started(task="s1/task-1", ts=hooks.now()), agy_answer("띄웠어"),
                   agy_user("이제 검사해", ts="2026-10-30T03:05:00Z"), agy_answer("검사했어", ts="2026-10-30T03:06:00Z"))
        self.hook("Stop")
        first, second = self.turns()
        self.assertEqual((first["assistant_text"], second["assistant_text"]), ("띄웠어", "검사했어"))

    def test_a_new_message_closes_a_waiting_turn(self):
        self.write(agy_user("A 해줘"), agy_started(conversation="c1", ts=hooks.now()), agy_answer("띄웠어"))
        self.hook("Stop")
        self.write(agy_user("B 해줘", ts="2026-10-30T03:05:00Z"))
        self.hook("PreInvocation")
        first, second = self.turns()
        self.assertEqual(first["assistant_text"], "띄웠어")
        self.assertNotIn("assistant_text", second)

    def test_output_never_keeps_the_agent_running(self):
        self.assertEqual(hooks.hook_output("antigravity", "Stop", {"systemMessage": "x"}), {"decision": "stop"})
        self.assertEqual(hooks.hook_output("antigravity", "PreInvocation", {"systemMessage": "x"}), {})
        self.assertEqual(hooks.hook_output("claude", "Stop", {"systemMessage": "x"}), {"systemMessage": "x"})

    def test_hooks_json_keeps_other_hooks_and_a_users_off_switch(self):
        mine = hooks.merge_hooks({"other": {"Stop": [{"command": "x"}]}}, "run", "antigravity")
        self.assertEqual(mine["other"], {"Stop": [{"command": "x"}]})
        self.assertEqual(set(mine[hooks.ANTIGRAVITY_HOOK]), {"SessionStart", "PreInvocation", "Stop"})
        self.assertEqual(mine[hooks.ANTIGRAVITY_HOOK]["Stop"][0]["command"], "run --event Stop")
        mine[hooks.ANTIGRAVITY_HOOK]["Stop"][0]["enabled"] = False
        again = hooks.merge_hooks(mine, "run", "antigravity")
        self.assertTrue(all(g[0]["enabled"] is False for g in again[hooks.ANTIGRAVITY_HOOK].values()))


class CodexTests(Base):
    harness = "codex"

    def test_stop_without_prompt_id_answers_the_latest_open_turn(self):
        self.hook("UserPromptSubmit", prompt="질문", turn_id="t1")
        self.hook("Stop", last_assistant_message="답", turn_id="t1")
        (turn,) = self.turns()
        self.assertEqual((turn["user_text"], turn["assistant_text"]), ("질문", "답"))
        self.assertEqual(self.server.calls[-1]["session"]["harness"], "codex")

    def test_models_come_from_turn_context(self):
        self.write({"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "xhigh", "cwd": str(self.project)}},
                   codex_message("user", "질문", ts="2026-10-28T01:00:00Z"))
        self.hook("UserPromptSubmit", prompt="질문")
        self.write(codex_message("assistant", "답", ts="2026-10-28T01:00:10Z"))
        self.hook("Stop", last_assistant_message="답")
        self.assertIn((self.sid, 1, "gpt-6.1-sol", "xhigh"), self.server.models)
        self.assertEqual(len(self.turns()), 1)

    def test_a_question_reply_attaches_to_the_turn_that_asked_and_makes_no_turn(self):
        self.write(codex_message("user", "고쳐줘", ts="2026-10-28T01:00:00Z"))
        self.hook("UserPromptSubmit", prompt="고쳐줘")
        self.write({"type": "response_item", "payload": {
                        "type": "function_call", "name": "request_user_input_async", "call_id": "c1",
                        "arguments": json.dumps({"questions": [{"title": "언제 떠?", "options": ["켤 때", "열 때"]}]}, ensure_ascii=False)}},
                   {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "{\"accepted\":true}"}},
                   codex_message("assistant", "답을 기다릴게", ts="2026-10-28T01:00:10Z"))
        self.hook("Stop", last_assistant_message="답을 기다릴게")
        qid = json.dumps(["request_user_input_async", "c1", 0], separators=(",", ":"))
        reply_text = ("<send_user_message_question_reply>\n"
                      + json.dumps([{"questionItemId": qid, "question": "언제 떠?", "answer": "모르겠어"}], ensure_ascii=False)
                      + "\n</send_user_message_question_reply>\n")
        self.write(codex_message("user", reply_text, ts="2026-10-28T01:03:00Z"))
        self.hook("UserPromptSubmit", prompt=reply_text)
        self.assertEqual(len(self.turns()), 1)
        reply = self.server.replies[(self.sid, qid)]
        self.assertEqual((reply["turn_no"], reply["question"], reply["answer"], reply["options"]),
                         (1, "언제 떠?", "모르겠어", ["켤 때", "열 때"]))
        self.assertEqual(reply["answered_at"], "2026-10-28T01:03:00Z")


class DeliveryTests(Base):
    def test_items_wait_while_the_server_is_down(self):
        self.server.fail = True
        self.hook("UserPromptSubmit", prompt="질문", prompt_id="p1")
        self.assertGreater(self.adapter.pending(), 0)
        restarted = hooks.Adapter(self.config, "claude")
        try:
            self.server.fail = False
            self.assertEqual(restarted.flush()["error"], None)
            self.assertEqual(restarted.pending(), 0)
        finally:
            restarted.db.close()
        self.assertEqual(self.server.turn(self.sid, 1)["user_text"], "질문")

    def test_only_the_item_the_server_rejects_is_quarantined(self):
        self.server.item_error = lambda list_name, _item: "bad model" if list_name == "models" else None
        self.write(claude_user("질문", ts="2026-10-28T01:00:00Z"))
        self.hook("UserPromptSubmit", prompt="질문", prompt_id="p1")
        self.write(claude_assistant([text("답")], ts="2026-10-28T01:00:10Z"))
        self.hook("Stop", last_assistant_message="답", prompt_id="p1")
        self.assertEqual(self.server.turn(self.sid, 1)["assistant_text"], "답")
        self.assertEqual(self.adapter.status()["quarantined"], 1)
        self.assertEqual(self.adapter.pending(), 0)
        calls = len(self.server.calls)
        self.hook("SessionEnd", reason="exit")
        resent = [m for c in self.server.calls[calls:] for m in c.get("models", [])]
        self.assertEqual(resent, [])

    def test_secrets_are_hidden_before_the_queue_touches_disk(self):
        self.server.fail = True
        self.hook("UserPromptSubmit", prompt="token ghp_abcdefghijklmnopqrstuvwxyz123456 써도 돼?", prompt_id="p1")
        self.adapter.db.commit()
        raw = b"".join(p.read_bytes() for p in (self.root / "state").iterdir() if p.is_file())
        self.assertNotIn(b"ghp_abcdefghijklmnopqrstuvwxyz123456", raw)
        self.server.fail = False
        self.adapter.flush()
        self.assertNotIn("ghp_abcdef", self.server.turn(self.sid, 1)["user_text"])

    def test_a_cwd_outside_registered_projects_is_refused(self):
        with self.assertRaises(ValueError):
            self.adapter.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                               "cwd": str(self.root), "prompt": "x"})


class InstallTests(Base):
    def test_hooks_cover_four_events_and_drop_an_old_post_tool_use(self):
        self.assertEqual(hooks.EVENTS, ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"))
        unrelated = {"type": "command", "command": "echo keep"}
        old = {"type": "command", "command": "python /old/ieodo_hooks.py --owner " + hooks.OWNER}
        existing = {"permissions": {"allow": ["Read"]}, "hooks": {
            "Stop": [{"matcher": "x", "hooks": [unrelated, old]}],
            "PostToolUse": [{"hooks": [old]}]}}
        command = "python /new/ieodo_hooks.py --owner " + hooks.OWNER
        merged = hooks.merge_hooks(existing, command, "claude")
        self.assertEqual(merged["permissions"], existing["permissions"])
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"], [unrelated])
        self.assertEqual(merged["hooks"].get("PostToolUse", []), [])
        self.assertEqual(merged, hooks.merge_hooks(merged, command, "claude"))

    def test_hooks_installed_under_the_old_groundlink_name_are_replaced(self):
        legacy = {"type": "command", "command": "python /x/groundlink_hooks.py --owner groundlink-continuity-v1 hook"}
        foreign = {"type": "command", "command": "python /x/groundlink_hooks.py --owner someone-else hook"}
        command = "python /new/ieodo_hooks.py --owner " + hooks.OWNER
        merged = hooks.merge_hooks({"hooks": {"Stop": [{"hooks": [legacy, foreign]}]}}, command, "codex")
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"], [foreign])
        self.assertEqual(merged["hooks"]["Stop"][1]["hooks"][0]["command"], command)
        old = {"groundlink-continuity": {"Stop": [{"type": "command", "command": "old", "enabled": False}]}}
        agy = hooks.merge_hooks(old, "run", "antigravity")
        self.assertNotIn("groundlink-continuity", agy)
        self.assertTrue(all(g[0]["enabled"] is False for g in agy[hooks.ANTIGRAVITY_HOOK].values()))

    def test_install_preview_and_apply_preserve_existing_and_backup(self):
        target = self.project / ".claude" / "settings.local.json"
        target.parent.mkdir()
        target.write_text('{"permissions":{"allow":["Read"]}}\n')
        args = ["--config", str(self.config), "--harness", "claude", "install", "--project-dir", str(self.project)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hooks.main(args), 0)
        self.assertEqual(json.loads(target.read_text()), {"permissions": {"allow": ["Read"]}})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hooks.main(args + ["--apply"]), 0)
        installed = json.loads(target.read_text())
        self.assertEqual(installed["permissions"], {"allow": ["Read"]})
        self.assertEqual(len(list(target.parent.glob("settings.local.json.ieodo-backup-*"))), 1)
        self.assertIn(hooks.OWNER, installed["hooks"]["SessionStart"][0]["hooks"][0]["command"])


class RedactionParityTests(unittest.TestCase):
    """The local queue must not hold what the server would hide."""

    def test_tokens_the_server_hides_are_hidden_before_disk(self):
        for secret, value in [
            ("IEODO_TURSO_TOKEN=eyJhbGciOiJIUzI1NiJ9abcdef", "eyJhbGciOiJIUzI1NiJ9abcdef"),
            ("SLACK_BOT_TOKEN=xoxb-1234567890-abcdefghijk", "xoxb-1234567890"),
            ("key AIzaSyA1234567890abcdefghijklmnopqrstuv", "AIzaSyA123"),
            ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopq", "eyJzdWIi"),
            ("access_token: abcdef123456", "abcdef123456"),
        ]:
            self.assertNotIn(value, hooks.redact(secret), secret)

    def test_ordinary_words_stay(self):
        for plain in ["tokenizer: lindera", "max_tokens: 128000", "secrets: none here", "token_count: 128000", "secret_name: prod"]:
            self.assertEqual(hooks.redact(plain), plain)


if __name__ == "__main__":
    unittest.main()
