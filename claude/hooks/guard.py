#!/usr/bin/env python3
"""Claude Code PreToolUse hook for Bash: keeps device work going through dp.

- adb / emulator run directly, or a Gradle install task -> deny (with the dp command to use)
- dp ... --yes, dp raw, dp register              -> ask (a person confirms)
- anything else                                   -> no opinion

The command line is split into simple commands (; && || | newlines, $( ), sh -c "...") and
each one's program name is checked, so `grep adb notes.txt` passes and
`timeout 9 ~/Android/Sdk/platform-tools/adb shell pm clear x` does not.
"""
import json
import os
import re
import shlex
import sys

WRAPPERS = {"sudo", "env", "nohup", "time", "nice", "exec", "command", "xargs", "stdbuf", "setsid",
            "watch", "unbuffer"}
SHELLS = {"sh", "bash", "zsh", "dash"}

DENY_HINT = ("端末の操作は dp を通してください（安全規則と記録のため）。"
             "例: dp up / dp install / dp screen / dp log / dp raw shell dumpsys …")


def split_commands(line: str):
    """Rough split into simple commands, including $( ... ) and `...` bodies."""
    inner = re.findall(r"\$\(([^()]*)\)|`([^`]*)`", line)
    parts = re.split(r"\|\||&&|[;|&\n]", line)
    for a, b in inner:
        parts += re.split(r"\|\||&&|[;|&\n]", a or b)
    return [p.strip() for p in parts if p.strip()]


def words(cmd: str):
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def program(argv):
    """(program name, its arguments) after skipping env assignments and wrapper commands."""
    i = 0
    while i < len(argv):
        w = argv[i]
        base = os.path.basename(w)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
            i += 1
        elif base in WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
        elif base == "timeout":
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", argv[i])):
                i += 1
        else:
            return base, argv[i + 1:]
    return "", []


def verdict(command: str, depth=0):
    """('deny' | 'ask' | None, reason)."""
    ask = None
    for simple in split_commands(command):
        name, args = program(words(simple))
        if name in SHELLS and "-c" in args and depth < 3:
            body = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            v, why = verdict(body, depth + 1)
            if v == "deny":
                return v, why
            ask = ask or (v and (v, why))
            continue
        if name in ("adb", "emulator", "avdmanager"):
            return "deny", f"{name} の直接実行は止めています。{DENY_HINT}"
        if name in ("gradlew", "gradle") and any(re.match(r"(:?[\w-]+:)*install", a) for a in args):
            return "deny", f"Gradle の install タスクは dp のインストールを通りません。{DENY_HINT}"
        if name == "dp":
            if "--yes" in args:
                ask = ask or ("ask", "外に出る操作（購入・送信・招待・公開など）を通そうとしています。中身を確かめてください")
            elif args[:1] == ["raw"] or "raw" in args[:3] and args[0].startswith("-"):
                ask = ask or ("ask", "dp raw は adb をそのまま実行します。中身を確かめてください")
            elif "register" in args[:3]:
                ask = ask or ("ask", "実機をテスト用（何をしてもよい端末）として登録しようとしています")
    return ask or (None, "")


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command", "")
    decision, reason = verdict(command)
    if decision:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": decision,
                                                 "permissionDecisionReason": reason}},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
