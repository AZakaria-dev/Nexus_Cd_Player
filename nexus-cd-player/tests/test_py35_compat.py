"""Guard against syntax and APIs that do not exist on Python 3.5.2.

The target device runs 3.5.2 and cannot be upgraded. Development happens on
newer interpreters, which will happily accept f-strings and friends, so this
test walks the AST of every source file and rejects post-3.5 constructs.
"""

import ast
import io
import os
import re
import sys
import token as token_module
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source_files():
    found = []
    for directory in ("backend", "tests", "tools", "ui_service"):
        path = os.path.join(ROOT, directory)
        for name in sorted(os.listdir(path)):
            if name.endswith(".py"):
                found.append(os.path.join(path, name))
    for name in sorted(os.listdir(ROOT)):
        if name.endswith(".py"):
            found.append(os.path.join(ROOT, name))
    return found


def read(path):
    handle = open(path)
    try:
        return handle.read()
    finally:
        handle.close()


def code_only(text):
    """Strip comments and triple-quoted strings, keeping line numbers.

    Prose explaining what is banned must not itself trip the ban. Ordinary
    quoted strings are kept so real uses (a device path, a flag) are still
    caught.
    """
    lines = text.splitlines()
    keep = [list(line) for line in lines]

    def blank(start, end):
        srow, scol = start
        erow, ecol = end
        for row in range(srow, erow + 1):
            chars = keep[row - 1]
            begin = scol if row == srow else 0
            finish = ecol if row == erow else len(chars)
            for col in range(begin, min(finish, len(chars))):
                chars[col] = " "

    readline = io.StringIO(text).readline
    try:
        for tok in tokenize.generate_tokens(readline):
            if tok[0] == tokenize.COMMENT:
                blank(tok[2], tok[3])
            elif tok[0] == token_module.STRING and tok[1][:3] in (
                    '"""', "'''", 'r""', "r''", 'b""', "b''"):
                blank(tok[2], tok[3])
    except (tokenize.TokenError, IndentationError):
        return text
    return "\n".join("".join(chars) for chars in keep)


# Textual checks for library APIs the AST cannot see. Applied to every file.
BANNED_TEXT = (
    ("capture_output", "subprocess capture_output= is Python 3.7+"),
    ("text=True", "subprocess text= is Python 3.7+"),
    ("subprocess.run", "prefer Popen; keep behaviour identical on 3.5"),
    ("shell=True", "shell=True is forbidden by the project rules"),
    ("dataclass", "dataclasses are Python 3.7+"),
    ("cdparanoia", "cdparanoia does not work on this kernel"),
)

# Applied to production code only -- tests legitimately assert that these
# strings are absent, which would otherwise trip the check on itself.
BANNED_IN_PRODUCTION = (
    ("/dev/sr0", "this kernel has no CD-ROM block device"),
    ("/dev/cdrom", "this kernel has no CD-ROM block device"),
)

# subprocess gained encoding= in 3.6; catch it without flagging str.decode.
ENCODING_KWARG = re.compile(r"\bencoding\s*=", re.MULTILINE)


class Py35CompatTest(unittest.TestCase):

    def test_files_found(self):
        names = [os.path.basename(p) for p in source_files()]
        self.assertTrue(len(names) >= 8, names)
        for expected in ("playback.py", "play_test.py", "check_hardware.py",
                         "worker.py", "server.py", "state.py"):
            self.assertIn(expected, names,
                          "compatibility guard is not scanning " + expected)

    def test_no_post_35_syntax(self):
        problems = []
        for path in source_files():
            tree = ast.parse(read(path), filename=path)
            for node in ast.walk(tree):
                name = type(node).__name__
                if name == "JoinedStr":
                    problems.append((path, node.lineno, "f-string (3.6+)"))
                elif name == "FormattedValue":
                    continue
                elif name == "AnnAssign":
                    problems.append(
                        (path, node.lineno, "variable annotation (3.6+)"))
                elif name == "NamedExpr":
                    problems.append(
                        (path, node.lineno, "walrus operator (3.8+)"))
                elif name == "Constant" and isinstance(
                        getattr(node, "value", None), (int, float)):
                    continue
        self.assertEqual([], problems, "post-3.5 syntax found: {0}".format(
            problems))

    def test_no_underscore_numeric_literals(self):
        """1_000 style literals are Python 3.6+."""
        pattern = re.compile(r"\b\d+_\d")
        for path in source_files():
            for number, line in enumerate(
                    code_only(read(path)).splitlines(), 1):
                self.assertIsNone(
                    pattern.search(line),
                    "{0}:{1} underscore numeric literal (3.6+)".format(
                        path, number))

    def test_no_banned_apis(self):
        problems = []
        for path in source_files():
            if path.endswith("test_py35_compat.py"):
                continue  # this file names them on purpose
            name = os.path.basename(path)
            is_test = os.path.basename(os.path.dirname(path)) == "tests"
            text = code_only(read(path))

            checks = list(BANNED_TEXT)
            if not is_test:
                checks.extend(BANNED_IN_PRODUCTION)
            for needle, why in checks:
                if needle in text:
                    problems.append("{0}: {1!r} -- {2}".format(
                        name, needle, why))
            if ENCODING_KWARG.search(text):
                problems.append("{0}: encoding= (subprocess "
                                "encoding= is 3.6+)".format(name))
        self.assertEqual([], problems, "\n".join([""] + problems))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
