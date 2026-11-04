"""A loopback stand-in for the ieodo `record` tool, for the hook tests.

It keeps the store's rules that matter to the adapter: a turn's answer is
filled once, an answered turn cannot change, models and replies need their
turn, and every bad item is reported in `errors` without stopping the rest.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    def __init__(self):
        self.calls = []          # every `record` argument object, in order
        self.sessions = {}
        self.turns = {}          # (session_id, turn_no) -> turn item
        self.messages = {}       # (session_id, turn_no, seq) -> added message item
        self.models = set()      # (session_id, turn_no, model, effort)
        self.replies = {}        # (session_id, question_id) -> reply item
        self.documents = []      # (session_id, item) in arrival order
        self.fail = False        # answer HTTP 503
        self.item_error = None   # callable(list_name, item) -> error text or None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.fail:
                    self.send_error(503)
                    return
                name = request["params"]["name"]
                args = request["params"]["arguments"]
                if name == "record":
                    text, is_error = outer.record(args)
                else:
                    text, is_error = "unknown tool '" + name + "'", True
                body = json.dumps({"jsonrpc": "2.0", "id": request.get("id"),
                                   "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def record(self, args):
        self.calls.append(json.loads(json.dumps(args)))
        session = args["session"]
        sid = session["session_id"]
        known = self.sessions.get(sid)
        if known and (known["project"] != args["project"]["name"] or known["harness"] != session["harness"]):
            return f"session {sid} already belongs to project '{known['project']}'", True
        self.sessions[sid] = {"project": args["project"]["name"], "root_path": args["project"]["root_path"], **session}
        errors = []
        out = {"session_id": sid, "turns": {"inserted": 0, "unchanged": 0}, "messages": {"inserted": 0, "unchanged": 0}, "models": {"inserted": 0, "unchanged": 0},
               "replies": {"inserted": 0, "unchanged": 0}, "documents": [], "redacted": 0, "errors": errors}

        def fail(list_name, index, item, message):
            entry = {"item": f"{list_name}[{index}]", "error": message}
            for key in ("turn_no", "question_id", "path"):
                if key in item:
                    entry[key] = item[key]
            errors.append(entry)

        def injected(list_name, index, item):
            message = self.item_error(list_name, item) if self.item_error else None
            if message:
                fail(list_name, index, item, message)
            return bool(message)

        for i, item in enumerate(args.get("turns", [])):
            if injected("turns", i, item):
                continue
            key = (sid, item["turn_no"])
            old = self.turns.get(key)
            if old is None:
                self.turns[key] = dict(item)
                out["turns"]["inserted"] += 1
            elif old == item or ("assistant_text" not in item and old["user_text"] == item["user_text"]):
                out["turns"]["unchanged"] += 1
            elif "assistant_text" not in old and old["user_text"] == item["user_text"] and old["user_at"] == item["user_at"]:
                self.turns[key] = dict(item)
                out["turns"]["inserted"] += 1
            else:
                fail("turns", i, item, f"turn {sid}#{item['turn_no']} already recorded")
        for i, item in enumerate(args.get("messages", [])):
            if injected("messages", i, item):
                continue
            if (sid, item["turn_no"]) not in self.turns:
                fail("messages", i, item, "turn not found")
                continue
            key = (sid, item["turn_no"], item["seq"])
            if key in self.messages and self.messages[key] != item:
                fail("messages", i, item, "turn message already recorded")
                continue
            out["messages"]["unchanged" if key in self.messages else "inserted"] += 1
            self.messages[key] = dict(item)
        for i, item in enumerate(args.get("models", [])):
            if injected("models", i, item):
                continue
            if (sid, item["turn_no"]) not in self.turns:
                fail("models", i, item, "turn not found")
                continue
            key = (sid, item["turn_no"], item["model"], item.get("effort"))
            out["models"]["unchanged" if key in self.models else "inserted"] += 1
            self.models.add(key)
        for i, item in enumerate(args.get("documents", [])):
            if injected("documents", i, item):
                continue
            self.documents.append((sid, dict(item)))
            out["documents"].append({"path": item.get("to") or item.get("path"), "doc_id": len(self.documents), "change": item["op"]})
        for i, item in enumerate(args.get("replies", [])):
            if injected("replies", i, item):
                continue
            if (sid, item["turn_no"]) not in self.turns:
                fail("replies", i, item, "turn not found")
                continue
            key = (sid, item["question_id"])
            out["replies"]["unchanged" if key in self.replies else "inserted"] += 1
            self.replies.setdefault(key, dict(item))
        return json.dumps(out, ensure_ascii=False), False

    def turn(self, sid, turn_no):
        return self.turns.get((sid, turn_no))

    def session_turns(self, sid):
        return [self.turns[key] for key in sorted(k for k in self.turns if k[0] == sid)]
