#!/usr/bin/env python3
"""Preview, install, or inspect project-local continuity hooks without running them."""
import argparse
import copy
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse

from ieodo_hooks import ANTIGRAVITY_HOOK, EVENTS, HOOK_FILES, LEGACY_ANTIGRAVITY_HOOKS, OWNER, merge_hooks, owned


SLUG = re.compile(r"[a-z0-9_-]{1,64}\Z")


def read_object(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise ValueError("invalid JSON object: " + str(path)) from None
    if not isinstance(value, dict):
        raise ValueError("expected JSON object: " + str(path))
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def safe_root(path):
    if not path.is_absolute():
        raise ValueError("project root must be absolute")
    root = path.resolve()
    home = Path.home().resolve()
    if root == Path(root.anchor) or root == home or root in home.parents:
        raise ValueError("project root is too broad: " + str(root))
    return root


def project_root(raw):
    if raw is not None:
        root = safe_root(Path(raw).expanduser())
    else:
        cwd = Path.cwd().resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, timeout=5, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("cannot infer Git root; supply --project-dir with an absolute path") from None
        if result.returncode or not result.stdout.strip():
            raise ValueError("cannot infer Git root; supply --project-dir with an absolute path")
        root = safe_root(Path(result.stdout.strip()))
        if (root != cwd and root not in cwd.parents) or not (root / ".git").exists():
            raise ValueError("ambiguous Git root; supply --project-dir explicitly")
    if not root.is_dir():
        raise ValueError("project directory must exist: " + str(root))
    return root


def validate_path(path):
    # A project .codex/.claude symlink must not turn a local install into a global edit.
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("symlinked write target is not supported: " + str(part))
        if part.exists() and part != path and not part.is_dir():
            raise ValueError("target parent is not a directory: " + str(part))
    if path.exists() and not path.is_file():
        raise ValueError("target is not a regular file: " + str(path))


def config_path(raw):
    path = Path(raw).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("symlinked continuity config is not supported: " + str(path))
    path = path.parent.resolve() / path.name
    native_homes = [(Path.home() / ".codex").resolve(), (Path.home() / ".claude").resolve()]
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        if os.environ.get(name):
            native_homes.append(Path(os.environ[name]).expanduser().resolve())
    if any(part.name in (".codex", ".claude") for part in [path, *path.parents]) or any(
            path == native or native in path.parents for native in native_homes):
        raise ValueError("continuity config cannot be a native harness configuration")
    if path.name in (".mcp.json", "mcp.json", ".claude.json", "config.toml", "hooks.json"):
        raise ValueError("continuity config cannot replace MCP, trust, or native configuration")
    validate_path(path)
    return path


def validate_config(config):
    projects = config.get("projects", [])
    if not isinstance(projects, list):
        raise ValueError("config projects must be a list")
    roots = {}
    for entry in projects:
        if not isinstance(entry, dict) or not isinstance(entry.get("root"), str):
            raise ValueError("each project needs an absolute root and project slug")
        root = safe_root(Path(entry["root"]).expanduser())
        if not isinstance(entry.get("project"), str) or not SLUG.fullmatch(entry["project"]):
            raise ValueError("project must be a lowercase ASCII slug of 1-64 characters")
        if root in roots:
            raise ValueError("duplicate or conflicting project root: " + str(root))
        for other, other_entry in roots.items():
            if (root in other.parents or other in root.parents) and entry["project"] != other_entry["project"]:
                raise ValueError("overlapping roots with different projects require separate configuration")
        roots[root] = entry
    base = config.get("server_url", "http://127.0.0.1:7341")
    if not isinstance(base, str):
        raise ValueError("server_url must be a local HTTP origin")
    url = urllib.parse.urlsplit(base.rstrip("/"))
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username
            or url.password or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("server_url must be a local HTTP origin")
    try:
        url.port  # Reject malformed ports before the adapter would attempt a request.
    except ValueError:
        raise ValueError("server_url has an invalid port") from None
    if "state_dir" in config and (not isinstance(config["state_dir"], str) or not config["state_dir"]):
        raise ValueError("state_dir must be a nonempty path")
    for key in ("flush_limit",):
        if key in config and (isinstance(config[key], bool) or not isinstance(config[key], int)):
            raise ValueError(key + " must be an integer")
    return roots


def validate_antigravity(native, path):
    for name, events in native.items():
        if not isinstance(events, dict):
            raise ValueError("hook " + name + " must be an object of events: " + str(path))
        for groups in events.values():
            if not isinstance(groups, list):
                raise ValueError("hook event must be a list: " + str(path))


def validate_native(native, path):
    for key in ("disableAllHooks", "allowManagedHooksOnly", "allow_managed_hooks_only"):
        if key in native and not isinstance(native[key], bool):
            raise ValueError(key + " must be boolean: " + str(path))
    hooks = native.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object: " + str(path))
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise ValueError("hook event must be a list: " + str(path))
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("hook group must contain a hooks list: " + str(path))
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or not isinstance(handler.get("type"), str):
                    raise ValueError("hook handler must be an object with type: " + str(path))
                if handler["type"] == "command" and not isinstance(handler.get("command"), str):
                    raise ValueError("command hook must contain a command: " + str(path))


def desired_hooks(existing, command, harness):
    merged = merge_hooks(existing, command, harness)
    if harness == "antigravity":
        return merged
    # The shared merger drops empty groups; keep pre-existing empty native groups too.
    for event, groups in existing.get("hooks", {}).items():
        kept = []
        for group in groups:
            copied = copy.deepcopy(group)
            copied["hooks"] = [handler for handler in copied["hooks"] if not owned(handler)]
            if copied["hooks"] or not group["hooks"]:
                kept.append(copied)
        if event in EVENTS:
            kept.append(merged["hooks"][event][-1])
        merged["hooks"][event] = kept
    return merged


def local_policy(root, harness, native):
    reasons = []
    if native.get("disableAllHooks") is True:
        reasons.append("disableAllHooks")
    if native.get("allowManagedHooksOnly") is True or native.get("allow_managed_hooks_only") is True:
        reasons.append("allow_managed_hooks_only")
    if harness == "antigravity":
        for name in (ANTIGRAVITY_HOOK, *LEGACY_ANTIGRAVITY_HOOKS):
            hook = native.get(name)
            if isinstance(hook, dict) and any(isinstance(h, dict) and h.get("enabled") is False
                                              for g in hook.values() if isinstance(g, list) for h in g):
                reasons.append("enabled=false")
                break
        return reasons
    if harness == "claude":
        path = root / ".claude/settings.json"
        validate_path(path)
        other = read_object(path)
        validate_native(other, path)
        if other.get("disableAllHooks") is True:
            reasons.append("settings.json:disableAllHooks")
        if other.get("allowManagedHooksOnly") is True:
            reasons.append("settings.json:allowManagedHooksOnly")
    else:
        path = root / ".codex/config.toml"
        validate_path(path)
        if path.exists():
            try:
                import tomllib
            except ImportError:
                raise ValueError("Python 3.11+ is required to inspect existing .codex/config.toml") from None
            try:
                other = tomllib.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise ValueError("invalid native TOML: " + str(path)) from None
            features = other.get("features", {})
            if not isinstance(features, dict):
                raise ValueError("native features must be a table: " + str(path))
            for key in ("hooks", "codex_hooks"):
                if key in features and not isinstance(features[key], bool):
                    raise ValueError("native hook feature must be boolean: " + str(path))
            if features.get("hooks", features.get("codex_hooks")) is False:
                reasons.append("features.hooks=false")
            if "allow_managed_hooks_only" in other and not isinstance(other["allow_managed_hooks_only"], bool):
                raise ValueError("allow_managed_hooks_only must be boolean: " + str(path))
            if other.get("allow_managed_hooks_only") is True:
                reasons.append("allow_managed_hooks_only")
    return reasons


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".ieodo-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_changes(changes):
    # Complete filesystem preflight before creating any directory, backup, or config.
    for path, _ in changes:
        validate_path(path)
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            raise ValueError("target parent is not writable: " + str(parent))
    backups = []
    applied_paths = []
    for path, value in changes:
        try:
            if path.exists():
                backup = path.with_name(path.name + ".ieodo-backup-" + str(time.time_ns()))
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(path.read_bytes())
                backups.append(str(backup))
            atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
            applied_paths.append(str(path))
        except OSError as exc:
            return application_failure(exc, applied_paths, backups, path)
    return {"backups": backups, "applied_paths": applied_paths, "partial_application": False}


def application_failure(exc, applied_paths, backups, path=None):
    result = {"configured": False, "applied": False, "partial_application": bool(applied_paths),
              "applied_paths": list(applied_paths), "backups": list(backups), "error": str(exc),
              "repair_hint": "Rerun the same setup command to finish; recover original files from the listed backups if needed. Each file is replaced atomically; the complete installation is not a multi-file transaction."}
    if path is not None:
        result["failed_path"] = str(path)
    return result


@contextlib.contextmanager
def configuration_lock(config, timeout=5):
    """Serialize shared-config read/merge/write; keep the inode to avoid unlink races."""
    try:
        import fcntl
    except ImportError:
        raise ValueError("apply requires POSIX advisory file locking") from None
    lock = config.with_name(config.name + ".ieodo-setup.lock")
    validate_path(lock)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("setup is busy; retry after the other installer finishes") from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def run(args, locked=False):
    if not SLUG.fullmatch(args.project):
        raise ValueError("project must be a lowercase ASCII slug of 1-64 characters")
    root = project_root(args.project_dir)
    config = config_path(args.config)
    existing = read_object(config)
    roots = validate_config(existing)
    desired = copy.deepcopy(existing)
    entry = roots.get(root)
    if entry and entry["project"] != args.project:
        raise ValueError("root already belongs to another project: " + str(root))
    if entry is None and args.project_dir is None and any(e["project"] == args.project for e in roots.values()):
        raise ValueError("additional roots for an existing project require explicit --project-dir")
    desired.setdefault("projects", [])
    match = next((e for e in desired["projects"] if Path(e["root"]).expanduser().resolve() == root), None)
    if match is None:
        match = {"root": str(root), "project": args.project}
        desired["projects"].append(match)
    validate_config(desired)
    changes = []
    if desired != existing:
        changes.append((config, desired))
    harnesses = {"both": ("codex", "claude"), "all": ("codex", "claude", "antigravity")}.get(args.harness, (args.harness,))
    states = {}
    targets = []
    hook_script = Path(__file__).resolve().with_name("ieodo_hooks.py")
    if not hook_script.is_file():
        raise ValueError("sibling ieodo_hooks.py is missing")
    for harness in harnesses:
        path = root / HOOK_FILES[harness]
        validate_path(path)
        native = read_object(path)
        (validate_antigravity if harness == "antigravity" else validate_native)(native, path)
        reasons = local_policy(root, harness, native)
        argv = [str(Path(sys.executable).resolve()), str(hook_script), "--harness", harness,
                "--config", str(config), "--owner", OWNER, "hook"]
        merged = desired_hooks(native, " ".join(shlex.quote(word) for word in argv), harness)
        matches = path.exists() and merged == native
        if not matches:
            changes.append((path, merged))
        targets.append((path, merged))
        states[harness] = {"path": str(path), "state": "disabled" if reasons else (
            "configured" if matches else "needs_update" if path.exists() else "missing"),
            "hooks_present": matches, "disabled_by": reasons,
            "health_command": argv[:-1] + ["status"], "trust": "not_checked", "external_policy": "not_checked"}
    if len({path for path, _ in changes}) != len(changes):
        raise ValueError("configuration targets must be distinct")
    # Publish the shared project registry only after every requested native hook file.
    changes.sort(key=lambda item: item[0] == config)
    result = {"project": args.project, "root": str(root), "config": str(config),
              "mode": "check" if args.check else "apply" if args.apply else "dry_run",
              "configured": entry is not None and desired == existing and all(
                  state["state"] == "configured" for state in states.values()),
              "registered": entry is not None, "harnesses": states,
              "changed_paths": [str(path) for path, _ in changes], "runtime_validation": "not_checked",
              "activation": "restart_or_new_session_and_native_trust_may_be_required"}
    if args.apply:
        if not locked:
            # Validate the entire plan before creating a lock; then reread all state
            # under the lock so another project registration cannot be overwritten.
            with configuration_lock(config):
                return run(args, locked=True)
        publication = apply_changes(changes)
        result.update(publication)
        for state in states.values():
            if state["path"] in publication["applied_paths"]:
                state["hooks_present"] = True
                if not state["disabled_by"]:
                    state["state"] = "configured"
        if "error" in publication:
            return result
        # Verify saved JSON, including exact owned commands, without invoking the adapter.
        try:
            if read_object(config) != desired or any(read_object(path) != value for path, value in targets):
                raise ValueError("post-install configuration verification failed")
        except (ValueError, OSError) as exc:
            result.update(application_failure(exc, publication["applied_paths"], publication["backups"]))
            return result
        result["registered"] = True
        for state in states.values():
            state["hooks_present"] = True
            if not state["disabled_by"]:
                state["state"] = "configured"
        result["configured"] = all(state["state"] == "configured" for state in states.values())
        result["applied"] = True
    return result


def default_config():
    home = os.environ.get("IEODO_HOME", "")
    return str(Path(home if home.strip() else "~/.ieodo").expanduser() / "continuity/config.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", help="existing absolute directory; otherwise infer the Git root")
    parser.add_argument("--project", required=True)
    parser.add_argument("--harness", choices=("codex", "claude", "antigravity", "both", "all"), required=True)
    parser.add_argument("--config", default=default_config())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 1 if (args.check or args.apply) and not result["configured"] else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"configured": False, "runtime_validation": "not_checked", "error": str(exc)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
