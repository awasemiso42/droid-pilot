"""Results and the output format: the first line is always OK / NG / BLOCKED."""
import json

OK, NG, BLOCKED = 0, 1, 3
_TAG = {OK: "OK", NG: "NG", BLOCKED: "BLOCKED"}


class Fail(Exception):
    """Stops the command with NG (or BLOCKED for subclasses)."""

    code = NG

    def __init__(self, msg, lines=None, rule=None):
        super().__init__(msg)
        self.msg = msg
        self.lines = list(lines or [])
        self.rule = rule

    def result(self):
        return Result(self.msg, self.lines, self.code, rule=self.rule)


class Blocked(Fail):
    """A safety rule refused the action. Retrying will not help: ask a person."""

    code = BLOCKED

    def __init__(self, rule, msg, lines=None):
        super().__init__(msg, lines, rule)


class Result:
    def __init__(self, head, lines=(), code=OK, rule=None, data=None):
        self.head = head
        self.lines = list(lines)
        self.code = code
        self.rule = rule
        self.data = data or {}

    @property
    def ok(self):
        return self.code == OK

    def first_line(self):
        tag = _TAG[self.code]
        if self.code == BLOCKED and self.rule:
            return f"{tag} {self.rule}: {self.head}"
        return f"{tag} {self.head}".rstrip()

    def render(self, as_json=False):
        if as_json:
            return json.dumps({"status": _TAG[self.code], "rule": self.rule, "head": self.head,
                               "lines": self.lines, **self.data}, ensure_ascii=False)
        return "\n".join([self.first_line(), *self.lines])


def ok(head, lines=(), **data):
    return Result(head, lines, OK, data=data)


def ng(head, lines=(), **data):
    return Result(head, lines, NG, data=data)
