"""Every wire method the vendored client can send is served by its channel, or declared refused, or named in a budget that only shrinks.

THE CLIENT AND THE SERVER ARE TWO SEPARATE FILES THAT NOTHING ELSE COMPARES. `src/invisible_playwright/_pw/_impl/` is Playwright's own client: it
turns each public call into `channel.send("<method>", ...)`. `src/invisible_playwright/_juggler/server.py` answers them, one dispatcher class per
channel, each with a `METHODS` table. The README promises "100% Playwright-compatible - all methods"; `test_every_refusal_is_declared.py` proves that
the refusals the server RAISES are written down, and no test proved the other half: that a method the client SENDS is on some table at all. A method on
neither is answered at run time with "<Channel> has no method '<m>', and '<m>' is INSIDE the perimeter this package covers - so this is a gap" (see
`Dispatcher.call`), and the first person to learn about it is a user whose script died.

WHAT IS COMPARED, with `ast` only (no browser, no engine, no server object):

* the client side: every string-literal first argument of `X.send(...)`, `X.send_return_as_dict(...)`, `X.send_no_reply(...)`, in every class AND every
  module-level function of `_impl/`, with the RECEIVER mapped to a channel type. A receiver this reader cannot map is RED and named: a send that is silently
  dropped is a method nobody compares. `updateSubscription` is added for the classes that subscribe to events; `__waitInfo__` for the owners of a `Waiter`.
* the server side: every class of `server.py` with a constant `TYPE` and a `METHODS` dict, inheriting `METHODS` through bases the way `Dispatcher.call`
  does (a class that defines its own table REPLACES the inherited one). A `RefusingDispatcher` refuses everything outside its table by decision.

Each (channel, method) pair is then exactly one of:

* SERVED: it is in the channel's `METHODS` and its handler does not begin by raising `ProtocolException`;
* DECLARED: it is absent from `METHODS` and `perimeter.refusal(method)` names its feature (what `Dispatcher.call` answers with), or its channel is a
  `RefusingDispatcher`; or it is in `METHODS` with a handler that refuses and the name is in `perimeter.py` (and is not `NOT_WIRED_YET`/`WITHDRAWN`, which are
  debt by their own definition);
* DEBT: anything else. Each is named in `tests/gates/wire_method_debt.json`, with a reason, and that list may only shrink against `main`
  (`test_the_wire_method_debt_does_not_grow_against_main`, in the file of the environment).

The channels the client knows and the server never defines (a Worker, a WebSocket...) are DEBT unless their ONLY creator is a method the server refuses
(`UNREACHABLE` below: the object can never exist, so no method of it can be sent). `partial` lists the gaps INSIDE a served method (a refusal for one value
of one parameter), each with a `witness`: the sentence of `server.py` that raises it today, so a gap that was closed cannot stay in the list.

Known-bad inputs (the tests at the bottom): a hole that the budget does not list is red; a budget entry that is no longer a hole is red ("it landed, take
it out"); a receiver that cannot be mapped to a channel is red; an entry without a reason is red; a partial whose witness left `server.py` is red; the file
that is not `dump(load())` is red. Plus floors, so that an empty derivation can never be green.
"""
from __future__ import annotations

import ast
import functools
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

from invisible_playwright._juggler import perimeter

pytestmark = pytest.mark.unit

PACKAGE = Path(perimeter.__file__).resolve().parent.parent
CLIENT = PACKAGE / "_pw" / "_impl"
JUGGLER = PACKAGE / "_juggler"
SERVER = JUGGLER / "server.py"
GATE_FILE = Path(__file__).resolve().parent / "gates" / "wire_method_debt.json"

SENDERS = ("send", "send_return_as_dict", "send_no_reply")

#: What the client has to send for this reader to be of any use (an empty derivation is a green that checks nothing).
FLOOR_PAIRS = 150
FLOOR_SERVED = 100

# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The receivers of a send, and what they are
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#: A receiver, whatever class it is in, to the channel it is. Each of these is an attribute that holds one fixed kind of object in `_impl/`.
GENERIC = {
    "self._frame._channel": "Frame",
    "self._main_frame._channel": "Frame",
    "self._page._channel": "Page",
    "self._browser_context._channel": "BrowserContext",
    "context._channel": "BrowserContext",             # a BrowserContext in `_selectors.py` (`_contexts_for_selectors`) and in `_set_input_files_helpers.py`
    "local_utils._channel": "LocalUtils",
    "self._local_utils._channel": "LocalUtils",
    "self.playwright._channel": "Playwright",
    "self._request._channel": "APIRequestContext",    # `APIResponse._request` is the APIRequestContext that made it (`_fetch.py`)
    "response._channel": "Response",                  # `Request.sizes`: `response = await self.response()`
    "self._pipe_channel": "JsonPipe",                 # the `pipe` that `LocalUtils.connect` answers with (`_browser_type.py`)
}

#: (class, receiver) that is not a channel at all, or that is the root, with the reason.
SPECIAL = {
    ("Connection", "self._transport"): None,          # the transport of the connection writes a frame on a socket: it is not a channel
    ("RootChannelOwner", "self._channel"): "Root",    # `initialize`, the one thing the root answers (`JugglerServer.METHODS`)
    ("ChannelOwner", "self._channel"): "<subscription>",  # `updateSubscription`: counted for the classes that subscribe (see `read_client`)
    ("Waiter", "self._channel"): "<waiter>",          # the channel of the owner of the Waiter (see `read_client`)
    ("ServerWebSocketRoute", "self._ws._channel"): "WebSocketRoute",   # `ServerWebSocketRoute(self)` is built by `WebSocketRoute` (`_network.py`)
}

#: What the client calls a type that the server serves under another name: a `JSHandle` and an `ElementHandle` are both `ElementHandleDispatcher`
#: (TYPE "ElementHandle"): the server never creates an object whose type is "JSHandle" (asserted below), so the client's JSHandle methods are the
#: ElementHandle's, and `ElementHandle(JSHandle)` inherits them in the client as well.
SERVED_AS = {"JSHandle": "ElementHandle"}

#: Server classes with a `METHODS` and no `TYPE`: the root. {class: channel}
SERVER_ROOT = {"JugglerServer": "Root"}

#: The channels that exist in the client and are never created by the server, because the ONLY way to create them is a method the server refuses.
#: {channel: (the methods that create it)}. Each creator must be sent by the client, and every send of it must be DECLARED (checked below).
UNREACHABLE = {
    "CDPSession": ("newCDPSession", "newBrowserCDPSession"),                    # `BrowserContext.new_cdp_session`, `Browser.new_browser_cdp_session`
    "WebSocketRoute": ("setWebSocketInterceptionPatterns",),                    # `BrowserContext.route_web_socket`, `Page.route_web_socket`
    "JsonPipe": ("connect",),                                                   # `BrowserType.connect`, on LocalUtils (`pipe` of the answer)
}


def _where(file: str, line: int) -> str:
    return "%s:%d" % (file, line)


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The client
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _bases(node: ast.ClassDef) -> List[str]:
    return [b.id for b in node.bases if isinstance(b, ast.Name)]


def _chain(name: str, classes: Dict[str, ast.ClassDef]) -> List[str]:
    """The class and its ancestors that are in `_impl/`, nearest first, stopping at ChannelOwner (not included)."""
    out: List[str] = []
    seen: Set[str] = set()
    while name in classes and name not in seen and name != "ChannelOwner":
        seen.add(name)
        out.append(name)
        bases = _bases(classes[name])
        name = bases[0] if bases else ""
    return out


def _is_channel_owner(name: str, classes: Dict[str, ast.ClassDef]) -> bool:
    seen: Set[str] = set()
    while name and name not in seen:
        seen.add(name)
        if name == "ChannelOwner":
            return True
        node = classes.get(name)
        if node is None:
            return False
        bases = _bases(node)
        name = bases[0] if bases else ""
    return False


def _factory_types(tree: ast.Module) -> Dict[str, str]:
    """class name -> the type name the factory builds it for (`if type == "X": return Cls(...)`)."""
    types: Dict[str, str] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create_remote_object"]:
        for branch in [n for n in ast.walk(fn) if isinstance(n, ast.If)]:
            test = branch.test
            if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "type"
                    and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant)):
                continue
            for call in [n for stmt in branch.body for n in ast.walk(stmt) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]:
                if call.func.id[:1].isupper():
                    types.setdefault(call.func.id, test.comparators[0].value)
                    break
    return types


def _subscribes(node: ast.ClassDef) -> Optional[int]:
    """The line where this class subscribes to events (calls `_update_subscription`, or gives `_set_event_to_subscription_mapping` a non-empty table)."""
    for call in [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]:
        if call.func.attr == "_update_subscription":
            return call.lineno
        if call.func.attr == "_set_event_to_subscription_mapping" and call.args:
            arg = call.args[0]
            if not (isinstance(arg, ast.Dict) and not arg.keys):
                return call.lineno
    return None


def read_client(sources: Dict[str, str]):
    """(sends, unmapped, channels): `sends` {(channel, method): where}, `unmapped` the sends whose receiver cannot be mapped (named),
    `channels` every channel type the client knows."""
    trees = {name: ast.parse(text) for name, text in sources.items()}
    classes: Dict[str, ast.ClassDef] = {}
    file_of: Dict[str, str] = {}
    for fname, tree in trees.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = node
                file_of[node.name] = fname
    types = _factory_types(trees["_object_factory.py"]) if "_object_factory.py" in trees else {}
    types = {c: t for c, t in types.items() if c in classes and _is_channel_owner(c, classes)}
    channels: Set[str] = set(types.values())

    # helpers that are given the channel of the class that builds them: `Keyboard(self._channel)` inside Page
    helper_channel: Dict[str, Set[str]] = {}
    waiter_channels: Set[str] = set()
    unmapped: List[str] = []
    for cname, cnode in classes.items():
        channel = types.get(cname)
        for call in [n for n in ast.walk(cnode) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]:
            args = [ast.unparse(a) for a in call.args]
            if call.func.id == "Waiter" and args:
                owner = "self" if args[0] == "self" else None
                if owner and channel:
                    waiter_channels.add(channel)
                elif args[0] + "._channel" in GENERIC:
                    waiter_channels.add(GENERIC[args[0] + "._channel"])
                else:
                    unmapped.append("%s: Waiter(%s) in %s (the owner of the wait is not a known channel)" % (_where(file_of[cname], call.lineno), args[0], cname))
            elif "self._channel" in args and channel:
                helper_channel.setdefault(call.func.id, set()).add(channel)

    sends: Dict[Tuple[str, str], str] = {}

    def add(channel: str, method: str, where: str) -> None:
        channels.add(channel)
        sends.setdefault((channel, method), where)

    raw: List[Tuple[str, str, str, str, int, str]] = []   # (class, receiver, method, file, line, unused)
    for fname, tree in trees.items():
        for top in tree.body:
            owner_name = top.name if isinstance(top, ast.ClassDef) else "<module>"
            for call in [n for n in ast.walk(top) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in SENDERS]:
                receiver = ast.unparse(call.func.value)
                first = call.args[0] if call.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    raw.append((owner_name, receiver, first.value, fname, call.lineno, ""))
                elif (owner_name, receiver) in SPECIAL and SPECIAL[(owner_name, receiver)] is None:
                    continue
                else:
                    unmapped.append("%s: %s.%s(%s) in %s (the method name is not a string literal)"
                                    % (_where(fname, call.lineno), receiver, call.func.attr, ast.unparse(first) if first is not None else "", owner_name))

    for owner_name, receiver, method, fname, line, _ in raw:
        where = _where(fname, line)
        special = SPECIAL.get((owner_name, receiver), "")
        if special is None:
            continue
        if special == "<subscription>" or special == "<waiter>":
            continue                                                  # added below from the classes that subscribe / the owners of a Waiter
        if special:
            add(special, method, where)
        elif receiver in GENERIC:
            add(GENERIC[receiver], method, where)
        elif receiver == "self._channel":
            if owner_name in types:
                add(types[owner_name], method, where)
            elif owner_name in helper_channel:
                for channel in sorted(helper_channel[owner_name]):
                    add(channel, method, where)
            else:
                unmapped.append("%s: %s.send(%r) in %s (a class that is neither a channel nor built with a channel)" % (where, receiver, method, owner_name))
        else:
            unmapped.append("%s: %s.send(%r) in %s (receiver unknown)" % (where, receiver, method, owner_name))

    # a ChannelOwner subclass answers the sends of its ancestors too (`ElementHandle(JSHandle)`)
    for cname, channel in sorted(types.items()):
        for ancestor in _chain(cname, classes)[1:]:
            for owner_name, receiver, method, fname, line, _ in raw:
                if owner_name == ancestor and receiver == "self._channel" and (owner_name, receiver) not in SPECIAL:
                    add(channel, method, _where(fname, line))
    # ChannelOwner._update_subscription
    for cname, channel in sorted(types.items()):
        for ancestor in _chain(cname, classes):
            line = _subscribes(classes[ancestor])
            if line:
                add(channel, "updateSubscription", _where(file_of[ancestor], line))
                break
    # the Waiter
    for owner_name, receiver, method, fname, line, _ in raw:
        if SPECIAL.get((owner_name, receiver)) == "<waiter>":
            for channel in sorted(waiter_channels):
                add(channel, method, _where(fname, line))
    # a type the server serves under another name
    for alias, target in SERVED_AS.items():
        for (channel, method), where in list(sends.items()):
            if channel == alias:
                add(target, method, where)
                del sends[(channel, method)]
        channels.discard(alias)
        channels.add(target)
    return sends, sorted(set(unmapped)), channels


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The server
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _assigned(node: ast.ClassDef, name: str):
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(getattr(t, "id", "") == name for t in stmt.targets):
            return stmt.value
        if isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", "") == name and stmt.value is not None:
            return stmt.value
    return None


def _begins_by_refusing(fn: ast.FunctionDef) -> bool:
    """The first statement that is not a docstring raises `ProtocolException`: the same reading as `test_every_refusal_is_declared.py`."""
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not body or not isinstance(body[0], ast.Raise):
        return False
    return getattr(getattr(body[0].exc, "func", None), "id", None) == "ProtocolException"


def read_server(sources: Iterable[str]):
    """(channels, problems): `channels` {type: {"class", "refusing", "methods": {name: refuses}}}, `problems` the tables that cannot be read."""
    classes: Dict[str, ast.ClassDef] = {}
    for text in sources:
        for node in ast.parse(text).body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, node)
    problems: List[str] = []

    def mro(name: str) -> List[str]:
        out: List[str] = []
        while name in classes and name not in out:
            out.append(name)
            bases = _bases(classes[name])
            name = bases[0] if bases else ""
        return out

    channels: Dict[str, dict] = {}
    for cname in classes:
        chain = mro(cname)
        channel = SERVER_ROOT.get(cname)
        if channel is None:
            for c in chain:
                value = _assigned(classes[c], "TYPE")
                if value is not None:
                    channel = value.value if isinstance(value, ast.Constant) else None
                    if channel is None:
                        problems.append("%s: TYPE is not a string literal" % c)
                    break
        if channel is None or channel == "Unknown":
            continue
        methods: Dict[str, bool] = {}
        for c in chain:
            table = _assigned(classes[c], "METHODS")
            if table is None:
                continue
            if not isinstance(table, ast.Dict) or any(k is None or not isinstance(k, ast.Constant) or not isinstance(v, ast.Constant)
                                                      for k, v in zip(table.keys, table.values)):
                problems.append("%s: METHODS is not a literal dict of name -> handler name" % c)
                break
            for key, value in zip(table.keys, table.values):
                handler = None
                for h in chain:
                    handler = next((s for s in classes[h].body if isinstance(s, ast.FunctionDef) and s.name == value.value), None)
                    if handler is not None:
                        break
                methods.setdefault(key.value, False)
                methods[key.value] = bool(handler is not None and _begins_by_refusing(handler))
            break                                   # the nearest table REPLACES the inherited ones, as `Dispatcher.call` reads `self.METHODS`
        if channel in channels:
            problems.append("two classes answer the type %s: %s and %s" % (channel, channels[channel]["class"], cname))
            continue
        channels[channel] = {"class": cname, "refusing": "RefusingDispatcher" in chain, "methods": methods}
    return channels, problems


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The comparison: a pure function over plain data
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def classify(channel: str, method: str, channels: Dict[str, dict], refusal, refused: Set[str], not_wired: Set[str]) -> str:
    """'served', 'declared' or 'debt' for one (channel, method). `refusal(method)` is `perimeter.refusal`: a non-empty sentence when the feature is named."""
    info = channels.get(channel)
    if info is None:
        return "declared" if refusal(method) else "debt"
    if method in info["methods"]:
        if not info["methods"][method]:
            return "served"
        return "declared" if method in refused and method not in not_wired else "debt"
    if info["refusing"] or refusal(method):
        return "declared"
    return "debt"


def find_holes(sends, channels, known, refusal, refused, not_wired, unreachable) -> Dict[Tuple[str, str], str]:
    """{(channel, method): why it is a hole} for what the client can send and the server neither serves nor declares."""
    holes: Dict[Tuple[str, str], str] = {}
    for (channel, method), where in sorted(sends.items()):
        if channel in unreachable:
            continue
        if classify(channel, method, channels, refusal, refused, not_wired) != "debt":
            continue
        if channel not in channels:
            holes[(channel, method)] = "the server never creates a %s object (client: %s)" % (channel, where)
        elif method in channels[channel]["methods"]:
            holes[(channel, method)] = "%s answers it with a refusal that is not a declared decision (client: %s)" % (channels[channel]["class"], where)
        else:
            holes[(channel, method)] = "%s has no handler for it (client: %s)" % (channels[channel]["class"], where)
    for channel in sorted(known):
        if channel not in channels and channel not in unreachable and not any(c == channel for c, _ in holes):
            holes[(channel, "*")] = "the channel is never created by the server"
    return holes


def unreachable_problems(sends, channels, refusal, refused, not_wired, unreachable) -> List[str]:
    """Every creator of an UNREACHABLE channel is a method `perimeter.py` names as refused, the client really sends it, and no send of it is SERVED.

    A creator that is a debt on ONE channel (a refusal missing there) is that channel's own entry in the budget; what this refuses is the day the
    server answers a creator for real, because from that day the channel can exist and its methods have to be compared."""
    out: List[str] = []
    for channel, creators in sorted(unreachable.items()):
        if channel in channels:
            out.append("%s is in UNREACHABLE and the server now defines it: take it out of the table, its methods are compared" % channel)
        for creator in creators:
            found = [(c, m) for (c, m) in sends if m == creator]
            if not found:
                out.append("%s creates %s: the client does not send it any more, correct UNREACHABLE" % (creator, channel))
            if creator not in refused:
                out.append("%s creates %s and perimeter.py does not name it as refused: %s is reachable" % (creator, channel, channel))
            for c, m in found:
                if classify(c, m, channels, refusal, refused, not_wired) == "served":
                    out.append("%s.%s creates %s and the server serves it: %s is reachable" % (c, m, channel, channel))
    return out


def key_of(item: dict, kind: str) -> tuple:
    if kind == "partial":
        return (item["channel"], item["method"], item["detail"])
    return (item["channel"], item["method"])


FIELDS = {"debt": ("channel", "method", "reason"), "partial": ("channel", "method", "detail", "witness", "reason")}


def load(path: Path = GATE_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(document: dict) -> str:
    """The file as it is written: stable order, one item per line, a newline at the end (the same bytes whoever writes it)."""
    def line(kind: str, item: dict) -> str:
        ordered = {k: item[k] for k in FIELDS[kind] if k in item}
        ordered.update({k: item[k] for k in sorted(item) if k not in ordered})
        return "  " + json.dumps(ordered, ensure_ascii=False)

    out = ["{", ' "version": %d,' % document["version"]]
    for kind, last in (("debt", False), ("partial", True)):
        items = sorted(document[kind], key=lambda i: key_of(i, kind))
        out.append(' "%s": [' % kind)
        out.append(",\n".join(line(kind, i) for i in items))
        out.append(" ]" + ("" if last else ","))
    out.append("}")
    return "\n".join(out) + "\n"


def _written(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def budget_problems(holes: Dict[Tuple[str, str], str], document: dict, server_text: str, partial_served: Dict[Tuple[str, str], bool]) -> List[str]:
    """What is wrong between what the code has and what the budget says."""
    problems: List[str] = []
    debt = {key_of(i, "debt"): i for i in document["debt"]}
    for key, why in sorted(holes.items()):
        if key not in debt:
            problems.append("NEW HOLE %s.%s: %s. Serve it, or refuse it by name in perimeter.py (the budget is not for new ones)" % (key[0], key[1], why))
    for key in sorted(debt):
        if key not in holes:
            problems.append("STALE ENTRY %s.%s: it is not a hole any more (it landed, or its refusal is declared): take it out of the budget" % key)
        if not _written(debt[key].get("reason")):
            problems.append("NO REASON for %s.%s: write why it is open, in one factual line" % key)
    for item in document["partial"]:
        key = key_of(item, "partial")
        if not _written(item.get("reason")):
            problems.append("NO REASON for the partial %s.%s [%s]" % key)
        if not _written(item.get("witness")) or item["witness"] not in server_text:
            problems.append("STALE PARTIAL %s.%s [%s]: its witness is no longer in server.py, the gap is closed: remove the entry" % key)
        if not partial_served.get(key[:2], False):
            problems.append("PARTIAL %s.%s [%s] is not a served method: a partial is a gap INSIDE a served method" % key)
    return problems


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The real tree
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def _world():
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(CLIENT.glob("*.py"))}
    sends, unmapped, known = read_client(sources)
    server_sources = [SERVER.read_text(encoding="utf-8"), (JUGGLER / "dispatcher.py").read_text(encoding="utf-8")]
    channels, server_problems = read_server(server_sources)
    refused = set(perimeter.REFUSED)
    not_wired = set(perimeter.NOT_WIRED_YET | perimeter.WITHDRAWN)
    holes = find_holes(sends, channels, known, perimeter.refusal, refused, not_wired, UNREACHABLE)
    return dict(sends=sends, unmapped=unmapped, known=known, channels=channels, server_problems=server_problems, holes=holes,
                refused=refused, not_wired=not_wired, server_text=server_sources[0])


def test_the_derivation_reads_both_sides():
    """The safety net: both reads come from the sources, and an empty one would make every check below green over nothing."""
    w = _world()
    served = sum(len(c["methods"]) for c in w["channels"].values())
    assert len(w["sends"]) > FLOOR_PAIRS, "only %d (channel, method) pairs found in the client: the reader is broken, not the client" % len(w["sends"])
    assert served > FLOOR_SERVED, "only %d methods found in the METHODS tables: the reader is broken, not the server" % served
    assert not w["server_problems"], "a METHODS table of server.py cannot be read: %s" % w["server_problems"]
    for channel in ("Page", "Frame", "BrowserContext", "Browser", "ElementHandle", "Route", "Root"):
        assert channel in w["channels"], "the server table of %s was not found" % channel


def test_every_send_of_the_client_has_a_channel():
    """A receiver that this reader cannot map is a send nobody compares: it is named here, and the table (GENERIC / SPECIAL) is corrected."""
    unmapped = _world()["unmapped"]
    assert not unmapped, "these sends of the client cannot be mapped to a channel:\n  " + "\n  ".join(unmapped)


def test_a_handle_is_served_as_an_element_handle():
    """`SERVED_AS`: the server never makes an object of type JSHandle, so the client's JSHandle methods are compared with the ElementHandle table."""
    w = _world()
    for alias, target in SERVED_AS.items():
        assert alias not in w["channels"], "the server now has a %s type: take it out of SERVED_AS" % alias
        assert target in w["channels"]
        assert not re.search(r"""["']%s["']""" % alias, w["server_text"]), "server.py builds something named %r: it is not served as %s" % (alias, target)


def test_a_channel_nobody_creates_is_created_only_by_a_refusal():
    w = _world()
    problems = unreachable_problems(w["sends"], w["channels"], perimeter.refusal, w["refused"], w["not_wired"], UNREACHABLE)
    assert not problems, "\n".join(problems)


def test_the_wire_method_budget_matches_the_code():
    """THE CHECK. Every method the client can send is served, declared, or in the budget; every budget entry is still a hole."""
    w = _world()
    document = load()
    partial_served = {(c, m): c in w["channels"] and m in w["channels"][c]["methods"] and not w["channels"][c]["methods"][m]
                      for c, m in {(i["channel"], i["method"]) for i in document["partial"]}}
    problems = budget_problems(w["holes"], document, w["server_text"], partial_served)
    assert not problems, "\n  ".join(["the wire methods and the budget (tests/gates/wire_method_debt.json) disagree:"] + problems)


def test_the_budget_file_is_the_bytes_that_dump_writes():
    text = GATE_FILE.read_bytes().decode("utf-8")
    assert "\r" not in text
    assert text == dump(json.loads(text)), "tests/gates/wire_method_debt.json is not in the order and shape that dump() writes: regenerate it with dump(load())"


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  Known-bad inputs: the reader and the comparison, on synthetic data
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
FACTORY = '''
def create_remote_object(parent, type, guid, initializer):
    if type == "Page":
        return Page(parent, type, guid, initializer)
    if type == "Worker":
        return Worker(parent, type, guid, initializer)
    return DummyObject(parent, type, guid, initializer)
'''

CLIENT_SYNTHETIC = '''
class Keyboard:
    def __init__(self, channel):
        self._channel = channel
    async def press(self):
        await self._channel.send("keyboardPress")

class Page(ChannelOwner):
    def __init__(self):
        self.keyboard = Keyboard(self._channel)
        self._set_event_to_subscription_mapping({"console": "console"})
    async def close(self):
        await self._channel.send("close")
        await self._frame._channel.send_return_as_dict("frameThing")

class Worker(ChannelOwner):
    async def evaluate(self):
        await self._channel.send("evaluateExpression")
'''


def _channels(**tables):
    return {name: {"class": name + "Dispatcher", "refusing": refusing, "methods": methods} for name, (refusing, methods) in tables.items()}


def _holes(sends, channels, known=None, unreachable=None, refusal=lambda m: "x" if m == "pdf" else "", refused=(), not_wired=()):
    return find_holes(sends, channels, known or {c for c, _ in sends}, refusal, set(refused), set(not_wired), unreachable or {})


def test_the_client_reader_maps_what_it_can_and_names_what_it_cannot():
    sends, unmapped, known = read_client({"_object_factory.py": FACTORY, "_a.py": CLIENT_SYNTHETIC})
    assert set(sends) == {("Page", "keyboardPress"), ("Page", "close"), ("Frame", "frameThing"), ("Page", "updateSubscription"),
                          ("Worker", "evaluateExpression")}
    assert unmapped == []
    _, unmapped, _ = read_client({"_object_factory.py": FACTORY, "_a.py": CLIENT_SYNTHETIC + '''
class Odd:
    async def go(self):
        await self._thing._channel.send("lost")
'''})
    assert len(unmapped) == 1 and "self._thing._channel" in unmapped[0] and "lost" in unmapped[0]
    _, unmapped, _ = read_client({"_object_factory.py": FACTORY, "_a.py": CLIENT_SYNTHETIC + '''
async def helper(x):
    await x._channel.send("lost_too")
'''})
    assert len(unmapped) == 1 and "lost_too" in unmapped[0] and "x._channel" in unmapped[0]
    _, unmapped, _ = read_client({"_object_factory.py": FACTORY, "_a.py": CLIENT_SYNTHETIC + '''
class Dyn(ChannelOwner):
    async def go(self, name):
        await self._channel.send(name)
'''})
    assert len(unmapped) == 1 and "not a string literal" in unmapped[0]


def test_a_new_hole_is_found_and_named():
    channels = _channels(Page=(False, {"close": False}))
    holes = _holes({("Page", "close"): "a:1", ("Page", "newThing"): "a:2"}, channels)
    assert list(holes) == [("Page", "newThing")]
    problems = budget_problems(holes, {"debt": [], "partial": []}, "", {})
    assert len(problems) == 1 and "NEW HOLE Page.newThing" in problems[0]


def test_a_budget_entry_that_is_not_a_hole_any_more_is_stale():
    channels = _channels(Page=(False, {"close": False}))
    holes = _holes({("Page", "close"): "a:1"}, channels)
    document = {"debt": [{"channel": "Page", "method": "close", "reason": "was open"}], "partial": []}
    problems = budget_problems(holes, document, "", {})
    assert len(problems) == 1 and "STALE ENTRY Page.close" in problems[0] and "landed" in problems[0]


def test_an_entry_without_a_reason_is_refused():
    channels = _channels(Page=(False, {}))
    holes = _holes({("Page", "newThing"): "a:2"}, channels)
    for reason in ("", "   ", None):
        problems = budget_problems(holes, {"debt": [{"channel": "Page", "method": "newThing", "reason": reason}], "partial": []}, "", {})
        assert len(problems) == 1 and "NO REASON" in problems[0]


def test_a_partial_whose_witness_left_the_server_is_stale():
    item = {"channel": "Frame", "method": "expect", "detail": "d", "witness": "is not implemented yet", "reason": "r"}
    served = {("Frame", "expect"): True}
    assert budget_problems({}, {"debt": [], "partial": [item]}, "raise X('is not implemented yet')", served) == []
    problems = budget_problems({}, {"debt": [], "partial": [item]}, "raise X('implemented')", served)
    assert len(problems) == 1 and "STALE PARTIAL Frame.expect" in problems[0] and "gap is closed" in problems[0]
    problems = budget_problems({}, {"debt": [], "partial": [item]}, "is not implemented yet", {("Frame", "expect"): False})
    assert len(problems) == 1 and "not a served method" in problems[0]


def test_what_is_served_declared_or_debt():
    channels = _channels(Page=(False, {"close": False, "setExtraHTTPHeaders": True, "drop": True}),
                         Tracing=(True, {"tracingStop": False}))
    sends = {("Page", "close"): "a:1", ("Page", "setExtraHTTPHeaders"): "a:2", ("Page", "drop"): "a:3", ("Page", "pdf"): "a:4",
             ("Page", "gone"): "a:5", ("Tracing", "tracingStop"): "a:6", ("Tracing", "tracingStart"): "a:7", ("Worker", "evaluateExpression"): "a:8"}
    holes = _holes(sends, channels, refused={"setExtraHTTPHeaders", "drop"}, not_wired={"setExtraHTTPHeaders"})
    assert sorted(holes) == [("Page", "gone"), ("Page", "setExtraHTTPHeaders"), ("Worker", "evaluateExpression")]
    # served: close; declared: drop (refuses and named), pdf (a feature), Tracing.* (a refusing dispatcher)


def test_a_channel_the_server_never_creates_is_one_entry_when_it_sends_nothing():
    channels = _channels(Page=(False, {}))
    holes = _holes({("Page", "x"): "a:1"}, channels, known={"Page", "WebSocket"})
    assert ("WebSocket", "*") in holes
    holes = _holes({("Page", "x"): "a:1"}, channels, known={"Page", "WebSocket"}, unreachable={"WebSocket": ("create",)})
    assert ("WebSocket", "*") not in holes


def test_a_creator_that_is_served_makes_its_channel_reachable():
    channels = _channels(Page=(False, {"makeSocket": False}), LocalUtils=(False, {}))
    sends = {("Page", "makeSocket"): "a:1", ("LocalUtils", "connect"): "a:2"}
    refusal = lambda m: "x" if m == "connect" else ""
    problems = unreachable_problems(sends, channels, refusal, {"connect", "makeSocket"},
                                    set(), {"Socket": ("makeSocket",), "Pipe": ("connect",), "Gone": ("never",)})
    assert any("Page.makeSocket creates Socket and the server serves it" in p for p in problems)      # served: reachable
    assert any("never creates Gone" in p or ("never" in p and "Gone" in p) for p in problems)         # not sent any more: stale table
    assert not any("connect" in p for p in problems)                                                  # refused and absent: unreachable
    problems = unreachable_problems(sends, channels, refusal, {"connect"}, set(), {"Socket": ("makeSocket",)})
    assert any("does not name it as refused" in p for p in problems)                                  # nobody wrote the refusal down


def test_a_server_table_that_cannot_be_read_is_named():
    channels, problems = read_server(['''
class Base:
    TYPE = "Base"
    METHODS = {"a": "op_a"}
    def op_a(self, p):
        """doc"""
        raise ProtocolException("no")
class Child(Base):
    TYPE = "Child"
class Own(Base):
    TYPE = "Own"
    METHODS = {"b": "op_b"}
    def op_b(self, p):
        return 1
class Broken:
    TYPE = "Broken"
    METHODS = dict(a="x")
'''])
    assert channels["Child"]["methods"] == {"a": True}          # inherited, and its handler refuses
    assert channels["Own"]["methods"] == {"b": False}           # its own table replaces the inherited one
    assert len(problems) == 1 and "Broken" in problems[0]
