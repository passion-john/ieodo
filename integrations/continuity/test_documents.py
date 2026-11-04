#!/usr/bin/env python3
"""Document changes by folder snapshot (wiki/schema.md, "수집기 ②").

Every hook compares the project folder with the last snapshot (size, mtime,
sha256 per file). Changes found when an answer ends (Stop, SessionEnd) belong
to the turn just answered; changes found when the user starts (SessionStart,
UserPromptSubmit) were made by a person and carry no turn.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock

import ieodo_hooks as hooks
from fake_ieodo import FakeServer


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "repo"
        self.project.mkdir()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.server = FakeServer()
        self.config = self.root / "config.json"
        self.global_doc = self.home / ".claude" / "CLAUDE.md"
        self.config.write_text(json.dumps({
            "projects": [{"root": str(self.project), "project": "demo"}],
            "state_dir": str(self.root / "state"),
            "server_url": self.server.url,
            "global_documents": [str(self.global_doc)],
        }))
        self.transcript = self.root / "session.jsonl"
        self.transcript.write_text("")
        self.adapter = None

    def start(self):
        self.adapter = hooks.Adapter(self.config, "claude")

    def tearDown(self):
        if self.adapter:
            self.adapter.db.close()
        self.server.close()
        self.temp.cleanup()

    def put(self, relative, content):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def hook(self, event, **fields):
        return self.adapter.hook({"hook_event_name": event, "session_id": "s1", "cwd": str(self.project),
                                  "transcript_path": str(self.transcript), **fields})

    def ops(self, since=0):
        return [item for _sid, item in self.server.documents[since:]]

    def by_path(self, since=0):
        return {(item.get("to") or item["path"]): item for item in self.ops(since)}


class KindTests(unittest.TestCase):
    def test_kind_by_suffix_name_path_and_frontmatter(self):
        r = Path("/r")
        cases = [
            ("/r/CLAUDE.md", "", "harness"), ("/r/sub/AGENTS.md", "", "harness"), ("/r/GEMINI.md", "", "harness"),
            ("/r/docs/ppt-skill.md", "", "skill"), ("/r/.claude/skills/deploy/SKILL.md", "", "skill"),
            ("/r/a-rule.md", "", "rule"), ("/r/q3-report.md", "", "report"),
            ("/r/STATUS.md", "", "record"), ("/r/HISTORY.md", "", "record"), ("/r/wiki/schema.md", "", "record"),
            ("/r/README.md", "", "readme"), ("/r/notes.md", "", "etc"),
            ("/r/notes.md", "---\nkind: report\ntitle: x\n---\n본문", "report"),
            ("/r/notes.md", "---\nkind: nonsense\n---\n", "etc"),
            ("/r/out.PPTX", "", "artifact"), ("/r/data/table.csv", "", "artifact"), ("/r/clip.mp4", "", "artifact"),
            ("/r/main.rs", "", None), ("/r/app.exe", "", None), ("/r/notes.txt", "", "artifact"),
        ]
        for path, head, expected in cases:
            with self.subTest(path=path, head=head):
                self.assertEqual(hooks.document_kind(Path(path), r, head), expected)
        # Global documents have no project root.
        self.assertEqual(hooks.document_kind(Path("/h/.claude/CLAUDE.md"), None, ""), "harness")
        self.assertEqual(hooks.document_kind(Path("/h/.claude/skills/x/SKILL.md"), None, ""), "skill")


class SnapshotTests(Base):
    def test_first_scan_sends_markdown_and_only_baselines_artifacts(self):
        self.put("STATUS.md", "# 상태\n")
        self.put("wiki/schema.md", "# 설계\n")
        self.put("notes.md", "메모\n")
        self.put("old.pptx", b"PK\x03\x04old")
        self.put("target/build.md", "빌드 산출물\n")
        self.put("node_modules/pkg/README.md", "남의 것\n")
        self.global_doc.write_text("# 전역 규칙\n")
        self.start()
        self.hook("SessionStart", source="startup")
        sent = self.by_path()
        self.assertEqual(set(sent), {str(self.project / "STATUS.md"), str(self.project / "wiki/schema.md"),
                                     str(self.project / "notes.md"), str(self.global_doc)})
        self.assertEqual(sent[str(self.project / "STATUS.md")],
                         {"op": "snapshot", "path": str(self.project / "STATUS.md"), "kind": "record",
                          "body": "# 상태\n", "at": sent[str(self.project / "STATUS.md")]["at"]})
        self.assertEqual(sent[str(self.project / "notes.md")]["kind"], "etc")
        self.assertEqual(sent[str(self.global_doc)]["kind"], "harness")
        for item in sent.values():
            self.assertNotIn("turn_no", item)

    def test_answer_end_changes_belong_to_the_turn_and_prompt_time_changes_to_a_person(self):
        self.put("STATUS.md", "# 상태\n")
        self.start()
        self.hook("SessionStart", source="startup")
        mark = len(self.server.documents)

        self.hook("UserPromptSubmit", prompt="갱신하고 보고서 만들어", prompt_id="p1")
        self.put("STATUS.md", "# 상태\n- 갱신함\n")
        deck = self.put("report.pptx", b"PK\x03\x04deck")
        self.hook("Stop", last_assistant_message="했어", prompt_id="p1")
        ai = self.by_path(mark)
        self.assertEqual(set(ai), {str(self.project / "STATUS.md"), str(deck)})
        self.assertEqual(ai[str(self.project / "STATUS.md")]["turn_no"], 1)
        self.assertEqual(ai[str(self.project / "STATUS.md")]["body"], "# 상태\n- 갱신함\n")
        artifact = ai[str(deck)]
        self.assertEqual((artifact["kind"], artifact["turn_no"], artifact["size"]), ("artifact", 1, 8))
        self.assertEqual(artifact["hash"], "sha256:" + hashlib.sha256(b"PK\x03\x04deck").hexdigest())
        self.assertNotIn("body", artifact)

        mark = len(self.server.documents)
        self.put("HISTORY.md", "# 이력\n")  # a person, between answers
        self.hook("UserPromptSubmit", prompt="다음", prompt_id="p2")
        person = self.by_path(mark)
        self.assertEqual(set(person), {str(self.project / "HISTORY.md")})
        self.assertNotIn("turn_no", person[str(self.project / "HISTORY.md")])
        # The person's change was found before the new turn was opened.
        self.assertEqual(self.server.turn("claude:s1", 2)["user_text"], "다음")

        mark = len(self.server.documents)
        self.hook("Stop", last_assistant_message="아무것도 안 고침", prompt_id="p2")
        self.assertEqual(self.ops(mark), [])

    def test_the_first_scan_carries_no_turn_even_when_it_runs_at_an_answer_end(self):
        self.put("STATUS.md", "# 상태\n")
        self.global_doc.write_text("# 전역\n")
        self.start()
        # Hooks installed mid-session: the first hook they see is a Stop, and
        # the transcript already holds the turn it ends.
        self.transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "질문"},
                                               "timestamp": "2026-10-28T01:00:00Z"}, ensure_ascii=False) + "\n")
        self.hook("Stop", last_assistant_message="답", prompt_id="p1")
        self.assertEqual(self.server.turn("claude:s1", 1)["assistant_text"], "답")
        self.assertEqual({o["path"] for o in self.ops()}, {str(self.project / "STATUS.md"), str(self.global_doc)})
        for item in self.ops():
            self.assertNotIn("turn_no", item)

    def test_a_change_is_not_credited_while_another_session_is_working(self):
        self.start()
        self.hook("SessionStart", source="startup")
        other = {"cwd": str(self.project), "session_id": "s2", "transcript_path": str(self.transcript)}
        self.hook("UserPromptSubmit", prompt="스프라이트 만들어", prompt_id="p1")
        self.adapter.hook({**other, "hook_event_name": "UserPromptSubmit", "prompt": "짧은 질문", "prompt_id": "q1"})
        mark = len(self.server.documents)
        self.put("sprite.md", "s1이 만든 것\n")
        # s2 answers first while s1 is still working: nobody can be credited.
        self.adapter.hook({**other, "hook_event_name": "Stop", "last_assistant_message": "답", "prompt_id": "q1"})
        (made,) = self.ops(mark)
        self.assertEqual(made["path"], str(self.project / "sprite.md"))
        self.assertNotIn("turn_no", made)
        # Alone again: s1's next change is its own.
        mark = len(self.server.documents)
        self.put("sprite.md", "s1이 고친 것\n")
        self.hook("Stop", last_assistant_message="다 만들었어", prompt_id="p1")
        (mine,) = self.ops(mark)
        self.assertEqual(mine["turn_no"], 1)

    def test_renames_and_deletes(self):
        self.put("notes.md", "메모\n")
        self.start()
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt="만들어", prompt_id="p1")
        deck = self.put("report.pptx", b"PK\x03\x04deck")
        self.hook("Stop", last_assistant_message="만들었어", prompt_id="p1")

        mark = len(self.server.documents)
        self.hook("UserPromptSubmit", prompt="옮기고 지워", prompt_id="p2")
        (self.project / "archive").mkdir()
        (self.project / "notes.md").rename(self.project / "archive" / "notes.md")
        deck.unlink()
        self.hook("Stop", last_assistant_message="옮기고 지웠어", prompt_id="p2")
        ops = self.ops(mark)
        rename = [o for o in ops if o["op"] == "rename"]
        delete = [o for o in ops if o["op"] == "delete"]
        self.assertEqual(len(rename), 1, ops)
        self.assertEqual((rename[0]["from"], rename[0]["to"], rename[0]["turn_no"]),
                         (str(self.project / "notes.md"), str(self.project / "archive/notes.md"), 2))
        self.assertEqual([(d["path"], d["turn_no"]) for d in delete], [(str(deck), 2)])
        self.assertEqual([o for o in ops if o["op"] == "snapshot"], [])

    def test_a_baseline_artifact_that_disappears_is_not_reported(self):
        self.put("old.pptx", b"PK\x03\x04old")
        self.start()
        self.hook("SessionStart", source="startup")
        (self.project / "old.pptx").unlink()
        self.hook("UserPromptSubmit", prompt="x", prompt_id="p1")
        self.assertEqual(self.ops(), [])

    def test_the_first_scan_does_not_hash_artifacts(self):
        self.put("STATUS.md", "# 상태\n")
        self.put("big.mp4", b"\x00" * 4096)
        self.start()
        with unittest.mock.patch.object(hooks, "file_sha256", wraps=hooks.file_sha256) as hashed:
            self.hook("SessionStart", source="startup")
        self.assertEqual([call.args[0] for call in hashed.call_args_list], [str(self.project / "STATUS.md")])

    def test_unchanged_size_and_mtime_skip_hashing(self):
        self.put("STATUS.md", "# 상태\n")
        self.put("big.mp4", b"\x00" * 4096)
        self.start()
        self.hook("SessionStart", source="startup")
        with unittest.mock.patch.object(hooks, "file_sha256", wraps=hooks.file_sha256) as hashed:
            self.hook("UserPromptSubmit", prompt="x", prompt_id="p1")
            self.assertEqual(hashed.call_count, 0)
            path = self.put("STATUS.md", "# 상태!\n")
            os.utime(path, (1, 1))
            self.hook("Stop", last_assistant_message="y", prompt_id="p1")
            self.assertEqual(hashed.call_count, 1)

    def test_git_ignored_paths_stay_out(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.put(".gitignore", "Saved/\n*.log.md\n")
        self.put("README.md", "# 읽어 줘\n")
        self.put("Saved/autosave.md", "무시됨\n")
        self.put("debug.log.md", "무시됨\n")
        self.put("draft.md", "추적 안 됐지만 무시되지 않음\n")
        subprocess.run(["git", "-C", str(self.project), "add", "README.md", ".gitignore"], check=True)
        self.start()
        self.hook("SessionStart", source="startup")
        self.assertEqual(set(self.by_path()) - {str(self.global_doc)},
                         {str(self.project / "README.md"), str(self.project / "draft.md")})


class ProjectFilesTests(unittest.TestCase):
    def git(self, path, *args):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    def test_a_folder_inside_a_bigger_repository_reads_its_nested_repositories(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            self.git(home, "init", "-q")
            root = home / "work"
            (root / "notes").mkdir(parents=True)
            (root / "notes/PLAN.md").write_text("계획")
            nested = root / "app"
            nested.mkdir()
            self.git(nested, "init", "-q")
            (nested / ".gitignore").write_text("out/\n")
            (nested / "STATUS.md").write_text("상태")
            (nested / "out").mkdir()
            (nested / "out/skip.md").write_text("무시")
            found = {str(p.relative_to(root)) for p in hooks.project_files(root)}
            self.assertIn("notes/PLAN.md", found)
            self.assertIn("app/STATUS.md", found)
            self.assertNotIn("app/out/skip.md", found)
            # A repository's own top still uses its git list, nested repositories included.
            self.git(root, "init", "-q")
            found = {str(p.relative_to(root)) for p in hooks.project_files(root)}
            self.assertIn("app/STATUS.md", found)
            self.assertNotIn("app/out/skip.md", found)


if __name__ == "__main__":
    unittest.main()
