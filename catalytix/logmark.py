"""Personal values in log messages (3.5.1, same as AssetLotse).

Usernames and typed text are written to the data-folder log between two invisible characters (Unicode "first strong
isolate" / "pop directional isolate"). The diagnostic package then replaces everything in between by a placeholder –
also values that are not (or no longer) in the database: a deleted account, a name typed at the desk, a password typed
into the username field. Other log outputs (container output) show the value unchanged.

    log.info("Sign-in of %s", personal(user.username))

Only with %s – with %r the marks would be escaped and the value stay in clear (a test checks every log call).
"""
from __future__ import annotations

import logging
import re

MARK_START, MARK_END = "⁨", "⁩"
MARKED_RX = re.compile("⁨(t:)?([^⁨⁩]*)⁩")


class Personal(str):
    """A personal value in a log message (a username)."""
    category = "user"


class PersonalText(Personal):
    """Typed free text (search words …)."""
    category = "text"


def personal(value: object, category: str = "user") -> str:
    """Mark a log argument as personal. Other log outputs show it unchanged."""
    if value is None:
        return "None"
    return (PersonalText if category == "text" else Personal)(str(value))


def _one_line(value: str) -> str:
    """The marked value on one line (the package reads the log line by line), without the marks themselves."""
    return " ".join(value.replace(MARK_START, "").replace(MARK_END, "").split())


class MarkingFormatter(logging.Formatter):
    """For the data-folder log: personal() arguments between the marks."""

    def format(self, record: logging.LogRecord) -> str:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(a, Personal) for a in args):
            record = logging.makeLogRecord(record.__dict__)
            record.args = tuple(f"{MARK_START}{'t:' if isinstance(a, PersonalText) else ''}{_one_line(a)}{MARK_END}"
                                if isinstance(a, Personal) else a for a in args)
        return super().format(record)
