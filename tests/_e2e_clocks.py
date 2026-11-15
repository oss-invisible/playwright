"""What an e2e test of this repository must not depend on: the reading of the sources behind ``test_the_e2e_tests_do_not_wait_on_clocks.py``.

An e2e test is red for no fault of the product when its verdict depends on something that is not the product: the speed of the machine (a fixed pause before
a read, an absolute bound on a duration), a resource the workers share (a literal port, ``os.environ``), or the world (a real site, a service of the internet).
This module reads the e2e files and the helpers of ``tests/`` with ``ast`` (a word in a comment or in a docstring counts for nothing) and says where each of those
is. It decides nothing about what is allowed: the exceptions are a reviewed list (``tests/gates/e2e_clock_exceptions.json``), and the occurrences that are
dangerous and not yet corrected are the ``debt`` of that file, which a script regenerates (``scripts/e2e_clock_exceptions.py --write``).

THE RULES (a stable name each; ``CLASS`` is the letter of the audit of the e2e tests that the rule belongs to):

* ``sleep`` (A): ``time.sleep``, ``asyncio.sleep`` (not ``sleep(0)``), ``.wait_for_timeout(``. A fixed pause and then a read judges the speed of the machine.
* ``js-timer`` (A): ``setTimeout(`` / ``setInterval(`` in a string of the test (a page of the test): when the verdict depends on when it fires, it judges the machine.
* ``bound`` (B): ``assert <something that is a duration> < <a constant>`` (``<=`` too): an absolute upper bound on a time. A bound against a value measured in the same
  run (``took <= control * 3``) is not flagged, and neither is a lower bound (it cannot fail because the machine is slow).
* ``port`` (C): a port written as a number in a ``bind``, a ``*Server((addr, N))``, a ``port=N``, a ``"...port": N`` or a ``NAME_PORT = N`` (N not 0). Two workers that
  run at once, or a port someone else holds, make it red.
* ``env`` (C): ``os.environ`` written (``[k] = v``, ``pop``, ``setdefault``, ``update``, ``del``, ``putenv``): ``monkeypatch.setenv`` undoes itself, this does not.
* ``net`` (D): a ``http(s)://`` URL of a real host, ``urlopen``, ``requests.get``, a ``pip install``, outside the files of the round after a publication.
* ``count`` (E): ``assert len(<processes, pids, children>) == N``: a number of processes the product does not promise (``_processes.py`` and ``_waits.py`` are how it is counted).
* ``egress`` (D): the conftest has no autouse session fixture that declares the exit of the lab (tests/_lab_egress.py), so a launch asks three services of the internet.

WHAT IS ACCEPTED WITHOUT A LIST, because it is recognised by its shape: a pause that is the step of a poll with a deadline (a ``while``/``for`` that compares with a clock
or a deadline and ends with a ``return``, ``break`` or ``raise``); the body of ``pump``/``pause``/``wait_*``/``*_until``/``poll*``, a function handed by name to ``wait_until``/``watch``/``awatch``, and a lambda passed as the ``pump`` of
those; a ``sleep`` in a method of a ``*Handler``/``*Server`` class (a test server that is slow on purpose); a ``sleep(0)``; a test that
carries no ``e2e`` marker (it runs in the unit run, with fakes).

An occurrence is named by ``(file, scope, rule, nth)``: the file, the qualified name of the function or class that holds it (a module-level constant: ``<module>.NAME``), the rule,
and its position among the occurrences of that rule in that scope. No line number, so moving code does not break the list.
"""
from __future__ import annotations

import ast
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

TESTS = Path(__file__).resolve().parent

#: The rule -> the class of the audit.
CLASS = {"sleep": "A", "js-timer": "A", "bound": "B", "port": "C", "env": "C", "net": "D", "egress": "D", "count": "E"}
RULES = tuple(CLASS)

#: The files that say what is forbidden (their own patterns would be found) and the files of the round after a publication (``net`` is theirs by design).
SELF = {"_e2e_clocks.py", "test_the_e2e_tests_do_not_wait_on_clocks.py"}
POST_PUBLICATION = ("test_release_e2e.py", "test_upgrade_e2e.py")
#: How processes are counted: the one allowed way.
COUNTING = {"_processes.py", "_waits.py"}

_DURATION_WORDS = {"took", "elapsed", "seconds", "secs", "sec", "duration", "wall", "latency", "ms", "held", "late", "waited", "wait", "slowest",
                   "longest", "spent", "rtt", "cpu", "lag", "stall", "delay"}
_CLOCKS = {"monotonic", "perf_counter", "time", "process_time", "time_ns", "monotonic_ns", "perf_counter_ns"}
_DEADLINE_NAMES = re.compile(r"(deadline|until|give_?up|stop_at|expires?|budget|limit|timeout|end_at)", re.I)
_POLLING_FUNCTIONS = re.compile(r"^(_?pump\w*|_?pause\w*|_?wait\w*|\w*wait_for\w*|\w*_until|_?poll\w*|_?settle\w*|_?spin\w*)$")
_PUMP_CALLEES = {"wait_until", "watch", "awatch"}
_PROCESS_WORDS = re.compile(r"(pids?|processes|children|procs|content_processes)\b")
_SERVER_BASE = re.compile(r"(Handler|Server|Protocol)$")
_TIMER = re.compile(r"\bset(?:Timeout|Interval)\(")
_URL = re.compile(r"https?://([^\s/:'\"<>)\\]*)(?::[^\s/'\"<>)\\]*)?")
_SAFE_HOST = re.compile(r"(^|\.)(test|invalid|localhost|example|local)$|^localhost$")
_PIP = re.compile(r"\bpip\b.*\binstall\b|\buv\b.*\b(add|pip|install|sync)\b")


@dataclass(frozen=True)
class Violation:
    file: str
    scope: str
    rule: str
    nth: int
    line: int
    text: str = ""

    @property
    def key(self) -> Tuple[str, str, str, int]:
        return (self.file, self.scope, self.rule, self.nth)

    @property
    def klass(self) -> str:
        return CLASS[self.rule]


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  What a node is
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _dotted(node: ast.AST) -> str:
    """``a.b.c`` of an attribute chain, or the name; '' for anything else."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _marks(node: ast.AST) -> Set[str]:
    """The names of the ``pytest.mark.X`` of a decorator list, a ``pytestmark`` value, or one mark."""
    found: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute) and sub.value.attr == "mark":
            found.add(sub.attr)
    return found


def _identifier_words(node: ast.AST) -> Set[str]:
    words: Set[str] = set()
    for sub in ast.walk(node):
        names = []
        if isinstance(sub, ast.Name):
            names.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.append(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and len(sub.value) < 40:
            names.append(sub.value)
        for name in names:
            words.update(w for w in re.split(r"[^A-Za-z0-9]+|_", name.lower()) if w)
    return words


def _is_constant_number(node: ast.AST) -> bool:
    """A number, or arithmetic of numbers and of NAMES IN CAPITALS (a constant of the module)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.Name):
        return node.id.isupper()
    if isinstance(node, ast.Attribute):
        return node.attr.isupper() and isinstance(node.value, (ast.Name, ast.Attribute))
    if isinstance(node, ast.UnaryOp):
        return _is_constant_number(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_constant_number(node.left) and _is_constant_number(node.right)
    return False


def _reads_a_clock(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = _dotted(sub.func).rsplit(".", 1)[-1]
            if name in _CLOCKS:
                return True
    return False


def _is_a_duration(node: ast.AST) -> bool:
    if _reads_a_clock(node):
        return True
    return bool(_identifier_words(node) & _DURATION_WORDS)


def _host_is_real(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if not host or host[0] in "{%$" or "{" in host or "%" in host:
        return False
    if "." not in host and ":" not in host:                     # http://x/, http://a/: not a name anyone owns
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return not _SAFE_HOST.search(host)
    return address.is_global                                    # loopback, private, documentation, link-local: not the world


class _Reader(ast.NodeVisitor):
    def __init__(self, file: str, tree: ast.Module, e2e_file: bool) -> None:
        self.file = file
        self.is_test_file = file.startswith("test_")
        self.e2e_file = e2e_file
        self.stack: List[str] = []
        self.assign: Optional[str] = None
        self.found: List[Tuple[str, str, int, str]] = []        # scope, rule, line, text
        self.exempt = 0
        self.module_marks = set()
        self.class_marks: List[Set[str]] = []
        self.post = file in POST_PUBLICATION
        self.server_depth = 0
        #: The functions this file hands to ``wait_until``/``watch``/``awatch`` by name: their body is the step between two looks of a wait with a deadline.
        self.pumps: Set[str] = set()
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and _dotted(call.func).rsplit(".", 1)[-1] in _PUMP_CALLEES:
                self.pumps.update(a.id for a in list(call.args) + [k.value for k in call.keywords] if isinstance(a, ast.Name))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
                self.module_marks |= _marks(node.value)
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent  # type: ignore[attr-defined]
        self.visit(tree)

    # -- scopes ---------------------------------------------------------------------------------------------------------------------------------
    @property
    def scope(self) -> str:
        if self.stack:
            return ".".join(self.stack)
        return "<module>." + self.assign if self.assign else "<module>"

    def add(self, rule: str, node: ast.AST, text: str = "", line: Optional[int] = None) -> None:
        if self.exempt:
            return
        self.found.append((self.scope, rule, line or getattr(node, "lineno", 0), text))

    def _enter_class(self, node: ast.ClassDef) -> None:
        marks = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
                marks |= _marks(stmt.value)
        marks |= set().union(*[_marks(d) for d in node.decorator_list]) if node.decorator_list else set()
        self.class_marks.append(marks)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter_class(node)
        self.stack.append(node.name)
        server = any(_SERVER_BASE.search(_dotted(b).rsplit(".", 1)[-1] or "") for b in node.bases)
        self.server_depth += 1 if server else 0
        for stmt in node.body:
            self.visit(stmt)
        self.server_depth -= 1 if server else 0
        self.stack.pop()
        self.class_marks.pop()

    def _is_e2e_test(self, node: ast.AST) -> bool:
        marks = set(self.module_marks)
        for group in self.class_marks:
            marks |= group
        for decorator in getattr(node, "decorator_list", []):
            marks |= _marks(decorator)
        return "e2e" in marks

    def _visit_function(self, node: ast.AST) -> None:
        name = node.name  # type: ignore[attr-defined]
        skip = self.is_test_file and name.startswith("test_") and not self._is_e2e_test(node)
        self.stack.append(name)
        if skip:
            self.exempt += 1
        for decorator in node.decorator_list:  # type: ignore[attr-defined]
            self.visit(decorator)
        for stmt in node.body:  # type: ignore[attr-defined]
            self.visit(stmt)
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:  # type: ignore[attr-defined]
            self.visit(default)
        if skip:
            self.exempt -= 1
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Assign(self, node: ast.Assign) -> None:
        in_module = not self.stack
        previous = self.assign
        if in_module and node.targets and isinstance(node.targets[0], ast.Name):
            self.assign = node.targets[0].id
        if in_module:
            for target in node.targets:
                if isinstance(target, ast.Name) and re.search(r"PORT$", target.id) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, int) and not isinstance(node.value.value, bool) and node.value.value != 0:
                    self.add("port", node, "%s = %r" % (target.id, node.value.value))
        for target in node.targets:
            if self._is_environ_subscript(target):
                self.add("env", node, "os.environ[...] = ...")
        self.generic_visit(node)
        self.assign = previous

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        previous = self.assign
        if not self.stack and isinstance(node.target, ast.Name):
            self.assign = node.target.id
        self.generic_visit(node)
        self.assign = previous

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self._is_environ_subscript(node.target):
            self.add("env", node, "os.environ[...] op= ...")
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if self._is_environ_subscript(target):
                self.add("env", node, "del os.environ[...]")
        self.generic_visit(node)

    @staticmethod
    def _is_environ_subscript(node: ast.AST) -> bool:
        return isinstance(node, ast.Subscript) and _dotted(node.value) in ("os.environ", "environ")

    # -- strings --------------------------------------------------------------------------------------------------------------------------------
    def _is_docstring(self, node: ast.Constant) -> bool:
        parent = getattr(node, "_parent", None)
        if isinstance(parent, ast.Expr):
            return True
        return False

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str) or self._is_docstring(node):
            return
        text = node.value
        for found in _TIMER.finditer(text):
            self.add("js-timer", node, found.group(0), line=node.lineno + text.count("\n", 0, found.start()))
        if not self.post:
            for found in _URL.finditer(text):
                if _host_is_real(found.group(1)):
                    self.add("net", node, found.group(0), line=node.lineno + text.count("\n", 0, found.start()))
        if re.fullmatch(r"[\w.-]*port", text) is not None:
            pass

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.lower().endswith("port") \
                    and isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool) and value.value != 0:
                self.add("port", node, "%s: %r" % (key.value, value.value))
        self.generic_visit(node)

    # -- calls ----------------------------------------------------------------------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        leaf = name.rsplit(".", 1)[-1]
        if self._is_sleep(node, name, leaf) and not self._accepted_pause(node):
            self.add("sleep", node, "%s(%s)" % (name or leaf, ast.unparse(node.args[0]) if node.args else ""))
        self._ports_of(node, name, leaf)
        if name in ("os.environ.pop", "os.environ.setdefault", "os.environ.update", "os.environ.clear", "os.putenv", "os.unsetenv",
                    "environ.pop", "environ.setdefault", "environ.update", "environ.clear"):
            self.add("env", node, name)
        if not self.post and leaf in ("run", "check_call", "check_output", "Popen", "call") and name.startswith("subprocess."):
            installing = _PIP.search(" ".join(re.findall(r"'([^']*)'", ast.unparse(node))))
            if installing:
                self.add("net", node, "subprocess " + installing.group(0))
        if not self.post and (name in ("urllib.request.urlopen", "urlopen") or re.match(r"requests\.(get|post|put|head|request|delete)$", name)):
            self.add("net", node, name)
        self.generic_visit(node)

    def _is_sleep(self, node: ast.Call, name: str, leaf: str) -> bool:
        if leaf == "wait_for_timeout":
            return True
        if name in ("time.sleep", "asyncio.sleep") or (name == "sleep" and not isinstance(node.func, ast.Attribute)):
            argument = node.args[0] if node.args else None
            return not (isinstance(argument, ast.Constant) and argument.value == 0)
        return False

    def _accepted_pause(self, node: ast.Call) -> bool:
        """The shapes of a pause that is not a verdict on the speed of the machine."""
        function = None
        parent: Optional[ast.AST] = getattr(node, "_parent", None)
        lambda_of_a_pump = False
        loops: List[ast.AST] = []
        while parent is not None:
            if isinstance(parent, ast.Lambda):
                caller = getattr(parent, "_parent", None)
                if isinstance(caller, ast.keyword) and caller.arg in ("pump", "step", "pause"):
                    lambda_of_a_pump = True
                elif isinstance(caller, ast.Call) and _dotted(caller.func).rsplit(".", 1)[-1] in _PUMP_CALLEES:
                    lambda_of_a_pump = True
            if isinstance(parent, (ast.While, ast.For, ast.AsyncFor)):
                loops.append(parent)
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = parent
                break
            parent = getattr(parent, "_parent", None)
        if lambda_of_a_pump:
            return True
        if function is not None and not function.name.startswith("test_") and (_POLLING_FUNCTIONS.match(function.name) or function.name in self.pumps):
            return True
        if self.server_depth > 0:
            return True
        for loop in loops:
            if self._is_a_poll_with_a_deadline(loop):
                return True
        return False

    @staticmethod
    def _is_a_poll_with_a_deadline(loop: ast.AST) -> bool:
        """A loop whose pause is the step of a wait that ends: a ``range`` of tries; or a comparison with a clock or a deadline that ends the loop, as its
        condition (``while not done and time.time() < deadline``), as an assertion inside it, or as a ``return``/``break``/``raise`` it leads to."""
        if isinstance(loop, (ast.For, ast.AsyncFor)) and isinstance(loop.iter, ast.Call) and _dotted(loop.iter.func) == "range":
            return any(isinstance(sub, (ast.Return, ast.Break, ast.Raise)) for sub in ast.walk(loop))     # N tries: the deadline is N pauses

        def is_a_deadline(compare: ast.AST) -> bool:
            return isinstance(compare, ast.Compare) and (
                _reads_a_clock(compare) or any(_DEADLINE_NAMES.search(w) for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ast.unparse(compare))))

        if isinstance(loop, ast.While) and any(is_a_deadline(sub) for sub in ast.walk(loop.test)):
            return True
        if any(isinstance(sub, ast.Assert) and any(is_a_deadline(c) for c in ast.walk(sub.test)) for sub in ast.walk(loop)):
            return True
        leaves = any(isinstance(sub, (ast.Return, ast.Break, ast.Raise)) for sub in ast.walk(loop))
        return leaves and any(is_a_deadline(sub) for sub in ast.walk(loop))

    def _ports_of(self, node: ast.Call, name: str, leaf: str) -> None:
        if leaf in ("bind", "create_server", "create_datagram_endpoint", "start_server", "serve") or leaf.endswith("Server") or leaf.endswith("Proxy"):
            for argument in node.args[:1]:
                if isinstance(argument, ast.Tuple) and len(argument.elts) == 2:
                    port = argument.elts[1]
                    if isinstance(port, ast.Constant) and isinstance(port.value, int) and not isinstance(port.value, bool) and port.value != 0:
                        self.add("port", node, "%s(..., %r)" % (leaf, port.value))
        for keyword in node.keywords:
            if keyword.arg == "port" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, int) \
                    and not isinstance(keyword.value.value, bool) and keyword.value.value != 0:
                self.add("port", node, "port=%r" % keyword.value.value)

    # -- asserts --------------------------------------------------------------------------------------------------------------------------------
    def visit_Assert(self, node: ast.Assert) -> None:
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare):
                continue
            left = sub.left
            for op, right in zip(sub.ops, sub.comparators):
                upper = (isinstance(op, (ast.Lt, ast.LtE)) and _is_constant_number(right) and _is_a_duration(left)) \
                    or (isinstance(op, (ast.Gt, ast.GtE)) and _is_constant_number(left) and _is_a_duration(right))
                if upper:
                    self.add("bound", node, ast.unparse(sub)[:80])
                if isinstance(op, (ast.Eq, ast.NotEq)) and self.file not in COUNTING:
                    for side in (left, right):
                        if isinstance(side, ast.Call) and _dotted(side.func) in ("len", "sorted") and side.args \
                                and _PROCESS_WORDS.search(ast.unparse(side.args[0])):
                            self.add("count", node, ast.unparse(sub)[:80])
                left = right
        self.generic_visit(node)


def _is_e2e_file(tree: ast.Module) -> bool:
    return any(isinstance(sub, ast.Attribute) and sub.attr == "e2e" and isinstance(sub.value, ast.Attribute) and sub.value.attr == "mark"
               for sub in ast.walk(tree))


def analyze(source: str, file: str, *, force: bool = False) -> List[Violation]:
    """The occurrences of ``source`` (the content of ``tests/<file>``). A test file with no ``e2e`` marker is not read, unless ``force`` (the helpers always are)."""
    tree = ast.parse(source)
    e2e = _is_e2e_file(tree) or not file.startswith("test_") or force
    if not e2e or file in SELF:
        return []
    reader = _Reader(file, tree, e2e)
    reader.found.sort(key=lambda item: item[2])
    counts: Dict[Tuple[str, str], int] = {}
    out: List[Violation] = []
    for scope, rule, line, text in reader.found:
        counts[(scope, rule)] = counts.get((scope, rule), 0) + 1
        out.append(Violation(file, scope, rule, counts[(scope, rule)], line, text))
    return out


def analyze_conftest(source: str) -> List[Violation]:
    """``egress``: the conftest declares the exit of the lab for the session, or every launch asks the internet."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_the_lab_declares_the_exit":
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and _dotted(decorator.func).endswith("fixture"):
                    options = {kw.arg: getattr(kw.value, "value", None) for kw in decorator.keywords}
                    if options.get("scope") == "session" and options.get("autouse") is True and "install" in ast.unparse(node):
                        return []
    return [Violation("conftest.py", "<module>", "egress", 1, 1, "no autouse session fixture declares the exit of the lab")]


def files(directory: Path = TESTS) -> Iterable[Path]:
    """The python files of ``tests/`` (not the subfolders: ``gates/`` is data, ``playwright-upstream/`` is another suite)."""
    return sorted(p for p in directory.glob("*.py") if p.name not in SELF)


def scan(directory: Path = TESTS) -> List[Violation]:
    """Every occurrence of every file of ``tests/``."""
    out: List[Violation] = []
    for path in files(directory):
        source = path.read_text(encoding="utf-8")
        out.extend(analyze(source, path.name))
    conftest = directory / "conftest.py"
    if conftest.is_file():
        out.extend(analyze_conftest(conftest.read_text(encoding="utf-8")))
    return out


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The reviewed list: ``tests/gates/e2e_clock_exceptions.json``
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
GATE_FILE = TESTS / "gates" / "e2e_clock_exceptions.json"

#: Why an occurrence is deliberate, as a closed vocabulary:
#:
#: * ``negative-window``: a fixed window in which something must NOT happen; a slow machine can only make it a weaker check (a false green), never a red.
#: * ``stimulus``: the time is the input the test gives: a page timer that fires the thing the product waits for, a server, relay or client that answers late on purpose,
#:   or a pause between two events the test generates. The product waits with a deadline; the verdict does not read when it fired.
#: * ``instrument``: a measuring or a test-server tool: the test of ``wait_for_timeout`` itself, the pause that lets a value settle before it is READ back, the teardown of a
#:   test server's loop.
#: * ``virtual-port``: a port number that is never bound: the gateway of the test answers for it by name.
#: * ``post-publication``: the round after a publication (``tests/_post_publication.py``).
#: * ``hang-detector``: a limit as high as the product's own timeout, or a wait with no clock whose end is an event of the test's own server; it ends a hang, it judges no speed.
KINDS = ("negative-window", "stimulus", "instrument", "virtual-port", "post-publication", "hang-detector")

#: What a ``debt`` item says, by rule: why the occurrence can be red for a reason that is nobody's fault, and what replaces it.
DEBT_REASON = {
    "sleep": "A fixed pause and then a read judges the speed of the machine: wait for the condition with a deadline (tests/_waits.py wait_until) and read after it.",
    "js-timer": "The verdict depends on when a timer of the test page fires against what the product does: let the test decide when the thing happens, then wait for it with a deadline.",
    "bound": "An absolute upper bound on a duration is a verdict on the machine: compare with a control measured in the same run, or assert the order of the events.",
    "port": "A literal port is shared by the workers that run at once and by whoever else holds it: bind port 0 and read the port back.",
    "env": "os.environ written directly is not undone when the test ends or fails: use monkeypatch.setenv, which restores it.",
    "net": "A real host in a gate is a third party: serve the page and the answer from the test's own server.",
    "count": "The number of processes is not something the product promises: count by site name or against the pool the product keeps (tests/_processes.py, tests/_waits.py).",
    "egress": "The launch asks three services of the internet where it leaves from: declare the exit (tests/_lab_egress.py).",
}


def empty_document() -> Dict[str, object]:
    return {"version": 1, "entries": [], "debt": []}


def load(path: Path = GATE_FILE) -> Dict[str, object]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def dump(document: Dict[str, object]) -> str:
    """The file as it is written: stable order, one item per line, a newline at the end (the same bytes whoever writes it)."""
    import json

    def line(item: Dict[str, object]) -> str:
        return "  " + json.dumps(item, ensure_ascii=False)

    out = ["{", ' "version": %d,' % document["version"]]
    for name, last in (("entries", False), ("debt", True)):
        items = sorted(document[name], key=lambda i: (i["file"], i["scope"], i["rule"], i["nth"]))  # type: ignore[index, union-attr]
        out.append(' "%s": [' % name)
        out.append(",\n".join(line(i) for i in items))
        out.append(" ]" + ("" if last else ","))
    out.append("}")
    return "\n".join(out) + "\n"


def key_of(item: Dict[str, object]) -> Tuple[str, str, str, int]:
    return (str(item["file"]), str(item["scope"]), str(item["rule"]), int(item["nth"]))  # type: ignore[call-overload]


def compare(found: Iterable[Violation], document: Dict[str, object]) -> Dict[str, List[Tuple[str, str, str, int]]]:
    """``new``: occurrences in no list; ``stale_entries`` / ``stale_debt``: items of a list that match no occurrence; ``both``: in the two lists."""
    seen = {v.key for v in found}
    entries = {key_of(i) for i in document["entries"]}  # type: ignore[union-attr]
    debt = {key_of(i) for i in document["debt"]}  # type: ignore[union-attr]
    return {"new": sorted(seen - entries - debt), "stale_entries": sorted(entries - seen), "stale_debt": sorted(debt - seen),
            "both": sorted(entries & debt)}


def regenerate(document: Dict[str, object], found: Iterable[Violation], *, prune_entries: bool = False) -> Dict[str, object]:
    """The document with its ``debt`` made again: every occurrence that no ``entries`` item covers. ``entries`` are reviewed by a person and stay as they
    are (``prune_entries`` drops the ones that match nothing any more: a correction that removed a deliberate pause)."""
    found = list(found)
    seen = {v.key for v in found}
    entries = [dict(i) for i in document["entries"]]  # type: ignore[union-attr]
    if prune_entries:
        entries = [i for i in entries if key_of(i) in seen]
    covered = {key_of(i) for i in entries}
    debt = [{"file": v.file, "scope": v.scope, "rule": v.rule, "nth": v.nth, "class": v.klass, "reason": DEBT_REASON[v.rule]}
            for v in sorted(found, key=lambda v: v.key) if v.key not in covered]
    return {"version": document.get("version", 1), "entries": entries, "debt": debt}
