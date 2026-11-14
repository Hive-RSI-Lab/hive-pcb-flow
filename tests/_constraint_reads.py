"""Every key the flow's scripts read from a constraints.json document.

tests/test_constraints_consumer_keys.py holds scripts/constraints_lint.py
to this list, so a key a checker reads cannot be one the lint warns about.
It is a static scan: it parses scripts/**/*.py and runs none of it.

How it works
------------
Each expression gets a value: a set of tokens. A token is a path into the
document or a mark that the value is a constraints *file path*:

  ()                        the parsed document
  ("power", "[]", "net")    power[].net
    "[]"  any element of a list
    "*"   any member of a map or list (a dynamic key, .values(), .items())
    "()"  a list or set the code built; "{}" a dict it built with computed
          keys; ("=", k) the value under constant key k of a dict literal.
          These three are consumed again when the code reads them back.
  PATH                      a constraints file path
  ("<pp>", fid, x)          a path that came in through parameter x
  ("<pd>", fid, x, *keys)   a document parsed from parameter x
  ("<s>", "power")          a string constant (so d.get(k) works for
                            k in ("power", "voltages"))

A value is PATH by its name or its text, never by what it held: a hint
that fired while a value was still empty would stick after the value
turned out to be the document. The hints are:
  - a string constant or f-string that ends in "constraints" or
    "constraints.json" (PATH_STR). A subscript whose key is PATH is PATH,
    so paths["constraints"] is one;
  - a variable named like cons_path, constraints_file, constraint_json or
    cons_p (PATH_NAME);
  - an attribute with such a name, or named constraints, as in
    args.constraints (PATH_ATTR).
PATH flows through string and Path operations and open(), but not through
.parent, .name, .with_suffix() and the like, and not through subprocess.
json.load, json.loads and any load_json() of a PATH value is a ROOT.

Calls are followed across modules (import x, from lib import x, from x
import f) with a fixpoint over parameter and return values. A helper that
parses its parameter returns ("<pd>", ...); the call site turns that into
the document only when it passed a constraints path. So a generic
_load(path) is a root where it loads constraints.json, and not where it
loads netlist.json. Calling a class runs its __init__ (self is not
passed), and Class.method() runs that method (cls is not passed when it
is a classmethod). A method called on an instance is not followed: a
document value passed to it is an escape.

Arguments from all call sites are joined, with one exception. A helper
that uses a parameter as a key (d[key], d.get(key), key in d) is analysed
once per set of constant strings its call sites pass for that parameter.
Otherwise _num(enclosure, "wall_mm") and _num(entry, "irms_a") would read
every key on every section.

A callee can also hand a document value back by storing it, not by
returning it: cfg["power"] = cons["power"] into a dict its caller passed,
or a store into a closure or module variable. What a body stores into a
name it does not own (a parameter, or a name its own scope does not bind)
is kept per function and context. Each call site adds it to what the
caller passed for that parameter, when that is a name, a subscript or an
attribute; a fresh object like [] or f(x) is nobody's. A store into a
closure or global name lands on the caller's name when both are in one
module. A module variable that any of the module's functions stores into
is seen by all of them. Only document values flow out this way. A label
string that ends in "constraints" reads as a constraints path, and it
would spread through every message and dict the label went into.

A read is a constant key taken from a document value: d["k"], d.get("k"),
d.pop("k"), d.setdefault("k"), "k" in d. What the scan cannot follow is
reported, not guessed: a key it cannot name (d[name]) becomes "*", and a
document value handed to code it cannot see (a method on an object, an
attribute, a callback) is an escape.

The fixpoint runs every body once a round until nothing grows. A body
that starts from the same env as last round, and whose callees return
what they returned then, would come out the same, so its last outcome is
reused (Scan.analyse). Later rounds cost a fraction of the first.
"""
from __future__ import annotations

import ast
import builtins
import re
from collections import defaultdict
from pathlib import Path

ELEM, ANY, BUILT, COMP = "[]", "*", "()", "{}"
PATH = ("<path>",)
PP, PD, STR = "<pp>", "<pd>", "<s>"
EMPTY = frozenset()
MAX_DEPTH = 8
MAX_NEST = 3
MAX_ROUNDS = 40

PATH_STR = re.compile(r"(^|[/\\])constraints(\.json)?$")
# Names and attributes that hold a constraints file path. By name only,
# never by the value: a hint that fired while a value was still empty
# would stick after the value turned out to be the document.
PATH_NAME = re.compile(r"^(cons|constraints?)_(path|file|json|p)$", re.I)
PATH_ATTR = re.compile(r"^(constraints|(cons|constraints?)_(path|file|json|p))$", re.I)
KEY = re.compile(r"^[a-z_][a-z0-9_]*$")       # "k" in d: schema-style keys only
# Path attributes that name some other file or a property of this one.
DERIVED = frozenset({"parent", "parents", "with_name", "with_stem",
                     "with_suffix", "name", "stem", "suffix", "glob", "rglob",
                     "iterdir"})
# Calls that hand back a path built from their arguments.
PATH_CALLS = frozenset({"Path", "PurePath", "PosixPath", "str", "fspath",
                        "open", "getattr", "next", "iter", "join", "joinpath",
                        "abspath", "realpath", "expanduser", "normpath",
                        "expandvars", "glob", "rglob"})
BUILTINS = frozenset(dir(builtins))


class Tup(tuple):
    """a tuple value: one value per position"""


class Rows(tuple):
    """a collection whose elements are tuples: one value per position"""


def is_pathish(t):
    return t == PATH or (t[:1] == (PP,) and len(t) == 3)


def is_kpath(t):
    """a path the code stored under a constant key of a dict it built"""
    return (t[:1] == PATH and len(t) == 2) or (t[:1] == (PP,) and len(t) == 4)


def tracked(t):
    return is_docish(t) or is_pathish(t) or is_kpath(t)


def _built(t):
    """a value the code built: a list, a dict, or one slot of a dict"""
    body = _split(t)[1]
    return bool(body) and (body[-1] in (BUILT, COMP) or isinstance(body[-1], tuple))


def is_doc(t):
    return not t or not (isinstance(t[0], str) and t[0].startswith("<"))


def is_docish(t):
    return is_doc(t) or t[0] == PD


def _split(t):
    return (t[:3], t[3:]) if t and t[0] == PD else ((), t)


def flat(v):
    """the value as one set of tokens (a tuple becomes a list of its items)"""
    if isinstance(v, Rows):
        v = flat(Tup(v))
    elif not isinstance(v, Tup):
        return v
    else:
        v = frozenset().union(*map(flat, v))
    return frozenset(q for q in map(collection, v) if q is not None)


def norm(v, depth=0):
    """one form per value, so join only ever grows a value: a tuple that
    carries nothing is EMPTY, and tuples nest at most MAX_NEST deep (a
    recursive function could otherwise nest them forever)"""
    if not isinstance(v, (Tup, Rows)):
        return v
    if depth >= MAX_NEST:
        return flat(v)
    v = type(v)(norm(x, depth + 1) for x in v)
    return v if flat(v) else EMPTY


def join(a, b):
    if a is b:
        return a                        # nothing new: skip the normalising
    a, b = norm(a), norm(b)
    if isinstance(a, frozenset) and not a:
        return b
    if isinstance(b, frozenset) and not b:
        return a
    if type(a) is type(b) and isinstance(a, (Tup, Rows)) and len(a) == len(b):
        return norm(type(a)(join(x, y) for x, y in zip(a, b)))
    return flat(a) | flat(b)


def same(a, b):
    """a and b are one value: equal, and the same kind at every level (a
    Tup and a Rows with the same items are equal tuples, not one value)"""
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, frozenset):
        return a == b
    return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))


def same_env(a, b):
    return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)


def _grow(t, *marks):
    pre, body = _split(t)
    if len(body) + len(marks) > MAX_DEPTH:
        return None
    return pre + body + marks


def element(t):
    """one member of a collection value"""
    if not is_docish(t):
        return t
    _, body = _split(t)
    if body and body[-1] == BUILT:
        return t[:-1]
    if body and (body[-1] == COMP or isinstance(body[-1], tuple)):
        return None                     # iterating a built dict: its keys
    return _grow(t, ELEM)


def collection(t):
    """a list built from this value"""
    if not is_docish(t):
        return t
    _, body = _split(t)
    if body and body[-1] == ELEM:
        return t[:-1]
    return _grow(t, BUILT)


def members(t):
    """the values of a map (.values(), a dynamic key)"""
    if is_kpath(t):
        return t[:-1]
    if not is_docish(t):
        return t
    _, body = _split(t)
    if body and (body[-1] in (BUILT, COMP) or isinstance(body[-1], tuple)):
        return t[:-1]
    return _grow(t, ANY)


def tmap(fn, v):
    return frozenset(q for q in (fn(t) for t in flat(v)) if q is not None)


def _unbound(t):
    """t outside the call it belongs to: a path or document that came in
    through a parameter means nothing there"""
    return None if t[:1] in ((PP,), (PD,)) else t


def elem(v):
    if isinstance(v, Rows):
        return Tup(v)
    if isinstance(v, Tup):
        out = EMPTY
        for item in v:
            out = join(out, item)
        return out
    return tmap(element, v)


def coll(v):
    if isinstance(v, Tup):
        return Rows(v)
    return tmap(collection, v)


def keyed(v, key):
    """v stored under key in a dict the code builds (None: computed key)"""
    mark = ("=", key) if key is not None else COMP
    out = set()
    for t in flat(v):
        if is_docish(t):
            q = _grow(t, mark)
            if q is not None:
                out.add(q)
        elif is_kpath(t):
            out.add(t[:-1])         # a dict in a dict: any key gives the path
        elif is_pathish(t) and key is not None:
            out.add(t + (mark,))
        else:
            out.add(t)
    return frozenset(out)


def str_keys(v):
    """(constant keys, True if some part of the key is not a constant)

    A constant like "constraints" is both a key and a path name (PATH); as
    a key it is just the constant."""
    toks = flat(v)
    keys = {t[1] for t in toks if t and t[0] == STR}
    others = [t for t in toks if not (t and t[0] == STR) and t != PATH]
    return keys, bool(others) or not keys


def int_const(n):
    """n is a literal int (or -int): a list index, never a JSON key"""
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
        n = n.operand
    return isinstance(n, ast.Constant) and isinstance(n.value, int) \
        and not isinstance(n.value, bool)


def key_params(fn):
    """the parameters fn uses directly as a key: d[p], d.get(p), p in d"""
    used = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Name):
            used.add(n.slice.id)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in ("get", "pop", "setdefault") and n.args \
                and isinstance(n.args[0], ast.Name):
            used.add(n.args[0].id)
        elif isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) \
                and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops):
            used.add(n.left.id)
    a = fn.args
    return used & {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}


def own_names(fn):
    """(fn's named parameters, the other names fn's own scope binds)

    A store into a container under any other name (a closure or global
    variable) or under a parameter changes an object its caller can see."""
    a = fn.args
    params = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}
    bound, declared = set(), set()
    todo = list(fn.body)
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            continue
        if isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                          ast.GeneratorExp)):
            continue                    # a scope of its own
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            bound.add(n.id)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            declared.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.alias):
            bound.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            bound.add(n.rest)
        todo.extend(ast.iter_child_nodes(n))
    return params, bound - declared - params


def fmt(path):
    out = ""
    for k in path:
        out += "[]" if k == ELEM else "." + str(k)
    return out.lstrip(".")


RECORDS = ("reads", "roots", "writes", "dynamic", "escapes")


class Ctx:
    def __init__(self, mod, fn, scope):
        self.mod, self.fn, self.scope = mod, fn, scope
        self.ret = EMPTY
        self.outs = {}          # name -> what the body stored into it
        self.nested = []


class Done:
    """one run of a body: what it started from, the callee results it read,
    and what it left (its records, return value, stores into names it does
    not own, nested defs and env)"""
    __slots__ = ("start", "deps", "records", "ret", "outs", "nested", "end")

    def __init__(self, start, deps, records, ret, outs, nested, end):
        self.start, self.deps, self.records = start, deps, records
        self.ret, self.outs, self.nested, self.end = ret, outs, nested, end


class Module:
    def __init__(self, path, rel, tree):
        self.path, self.rel, self.tree = path, rel, tree
        self.defs = {}
        self.classes = {}
        self.mod_alias = {}
        self.from_alias = {}


class Scan:
    """the reads, roots, writes, dynamic keys and escapes of one source tree"""

    def __init__(self, root: Path, files):
        self.root = Path(root)
        self.by_stem = defaultdict(list)
        self.modules = []
        for f in files:
            tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
            m = Module(f, f.relative_to(self.root).as_posix(), tree)
            self.modules.append(m)
            self.by_stem[f.stem].append(m)
        self.fid = {}
        self.key_params = {}
        self.names = {}             # fn -> own_names(fn)
        self.fn_mod = {}            # fn -> the module that defines it
        self.mod_fns = defaultdict(list)
        for m in self.modules:
            self._index(m)
        # params and returns are kept per (function, context); a context
        # is the constant strings the call sites pass for its key params
        self.contexts = defaultdict(set)
        self.params = defaultdict(dict)
        self.defaults = defaultdict(dict)
        self.rets = defaultdict(lambda: EMPTY)
        # what a body stores into names it does not own, per (function,
        # context): {parameter or closure/global name: value}
        self.outs = defaultdict(dict)
        self.changed = False
        self.rounds = 0
        self.memo = {}          # (key, n) -> Done: one body's last outcome
        self.seen = defaultdict(int)
        self.deps = None
        self._clear()

    def _clear(self):
        self.reads = defaultdict(set)      # path -> {(file, line)}
        self.roots = defaultdict(set)      # (file, line) -> {how}
        self.writes = defaultdict(set)     # path -> {(file, line)}
        self.dynamic = defaultdict(set)    # (file, line) -> {path}
        self.escapes = defaultdict(set)    # (file, line, what) -> {path}

    def _index(self, m):
        for s in m.tree.body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                m.defs[s.name] = s
            elif isinstance(s, ast.ClassDef):
                m.classes[s.name] = s
        for n in ast.walk(m.tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.fid[n] = f"{m.rel}:{n.lineno}"
                self.key_params[n] = key_params(n)
                self.names[n] = own_names(n)
                self.fn_mod[n] = m
                self.mod_fns[m.rel].append(n)
            if isinstance(n, ast.Import):
                for a in n.names:
                    stem = a.name.split(".")[-1]
                    if stem in self.by_stem:
                        m.mod_alias[a.asname or a.name] = stem
            elif isinstance(n, ast.ImportFrom):
                src = (n.module or "").split(".")[-1]
                for a in n.names:
                    if src in self.by_stem and any(
                            a.name in x.defs or self._top_def(x, a.name)
                            for x in self.by_stem[src]):
                        m.from_alias[a.asname or a.name] = (src, a.name)
                    elif a.name in self.by_stem:
                        m.mod_alias[a.asname or a.name] = a.name

    @staticmethod
    def _top_def(m, name):
        return any(isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
                   and s.name == name for s in m.tree.body)

    # ------------------------------------------------------------ driver
    def run(self):
        for i in range(MAX_ROUNDS):
            self.changed = False
            self._clear()
            self.seen.clear()
            for m in self.modules:
                self.run_module(m)
            if not self.changed:
                self.rounds = i + 1
                return self
        raise RuntimeError("constraint read scan did not settle")

    def run_module(self, m):
        ctx = Ctx(m, None, {})
        env = {}
        self.analyse((m,), m.tree.body, env, ctx)
        # a function that stores into a module-level container: every
        # function of the module sees what it stored (as of last round)
        for fn in self.mod_fns[m.rel]:
            for ck in self.contexts[fn] | {()}:
                for name, v in self.outs[(fn, ck)].items():
                    if name in env and name not in self.names[fn][0]:
                        env[name] = join(env[name], tmap(_unbound, v))
        self.finish(ctx, env)

    def finish(self, ctx, env):
        for fn, at_def in ctx.nested:
            closure = dict(at_def)
            for k, v in env.items():
                closure[k] = join(closure.get(k, EMPTY), v)
            self.run_fn(ctx.mod, fn, closure, ctx.scope)

    def run_fn(self, m, fn, closure, scope):
        scope = dict(scope)
        for s in self._local_defs(fn.body):
            scope[s.name] = s
        for ck in sorted(self.contexts[fn] | {()}):
            key = (fn, ck)
            ctx = Ctx(m, fn, scope)
            env = dict(closure)
            a = fn.args
            for p in a.posonlyargs + a.args + a.kwonlyargs:
                env[p.arg] = join(self.params[key].get(p.arg, EMPTY),
                                  self.defaults[fn].get(p.arg, EMPTY))
            for p in (a.vararg, a.kwarg):
                if p is not None:
                    env[p.arg] = self.params[key].get(p.arg, EMPTY)
            self.analyse(key, fn.body, env, ctx)
            new = join(self.rets[key], ctx.ret)
            if new != self.rets[key]:
                self.rets[key] = new
                self.changed = True
            outs = self.outs[key]
            for name, v in ctx.outs.items():
                new = join(outs.get(name, EMPTY), v)
                if not same(new, outs.get(name, EMPTY)):
                    outs[name] = new
                    self.changed = True
            self.finish(ctx, env)

    def analyse(self, key, body, env, ctx):
        """self.block(body, env, ctx), or the outcome it had the last time
        when it would come out the same: the same env to start from and the
        same returns from the functions it calls. What it did to params,
        contexts and defaults is not redone; those only grow, so it stands.
        (The n-th run of a key in a round is matched with the n-th of the
        last: a nested def runs once per context of the def around it.)"""
        n = self.seen[key]
        self.seen[key] = n + 1
        start = dict(env)
        done = self.memo.get((key, n))
        if done is not None and same_env(done.start, start) and all(
                same(self.rets[k], r) and same_env(self.outs[k], o)
                for k, (r, o) in done.deps.items()):
            self._emit(done.records)
            ctx.ret, ctx.nested = done.ret, list(done.nested)
            ctx.outs = dict(done.outs)
            env.clear()
            env.update(done.end)
            return
        outer = tuple(getattr(self, a) for a in RECORDS)
        self._clear()
        self.deps = {}
        self.block(body, env, ctx)
        done = Done(start, self.deps, tuple(getattr(self, a) for a in RECORDS),
                    ctx.ret, dict(ctx.outs), list(ctx.nested), dict(env))
        self.deps = None
        for a, d in zip(RECORDS, outer):
            setattr(self, a, d)
        self._emit(done.records)
        self.memo[(key, n)] = done

    def _emit(self, records):
        for a, got in zip(RECORDS, records):
            mine = getattr(self, a)
            for k, v in got.items():
                mine[k] |= v

    def _local_defs(self, body):
        out = []
        for s in body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(s)
            elif not isinstance(s, ast.ClassDef):
                for f in ("body", "orelse", "finalbody", "handlers", "cases"):
                    for x in getattr(s, f, None) or []:
                        out += self._local_defs(
                            x.body if isinstance(x, (ast.excepthandler,
                                                     ast.match_case)) else [x])
        return out

    # ------------------------------------------------------------ records
    def site(self, ctx, node):
        return (ctx.mod.rel, node.lineno)

    def escape(self, v, ctx, node, what):
        docs = {t for t in flat(v) if is_docish(t)}
        if docs:
            self.escapes[(ctx.mod.rel, node.lineno, what)] |= {
                fmt(_split(t)[1]) for t in docs}

    # ------------------------------------------------------------ statements
    def block(self, stmts, env, ctx):
        for s in stmts:
            self.stmt(s, env, ctx)

    @staticmethod
    def merge(env, *others):
        for o in others:
            for k, v in o.items():
                cur = env.get(k, EMPTY)
                if cur is not v:
                    env[k] = join(cur, v)

    def loop(self, env, body):
        before = dict(env)
        for _ in range(3):
            snap = dict(env)
            body()
            self.merge(env, snap)
            if env == snap:
                break
        self.merge(env, before)

    def stmt(self, s, env, ctx):
        ev = lambda n: self.eval(n, env, ctx)  # noqa: E731
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in s.decorator_list:
                ev(d)
            a = s.args
            pos = a.posonlyargs + a.args
            for p, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                self._default(s, p.arg, ev(d))
            for p, d in zip(a.kwonlyargs, a.kw_defaults):
                if d is not None:
                    self._default(s, p.arg, ev(d))
            ctx.nested.append((s, dict(env)))
        elif isinstance(s, ast.ClassDef):
            for x in s.bases + s.decorator_list:
                ev(x)
            for x in s.body:
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.stmt(x, env, ctx)
                else:
                    self.stmt(x, dict(env), ctx)
        elif isinstance(s, ast.Assign):
            v = ev(s.value)
            for tg in s.targets:
                self.bind(tg, v, env, ctx)
        elif isinstance(s, ast.AnnAssign):
            if s.value is not None:
                self.bind(s.target, ev(s.value), env, ctx)
        elif isinstance(s, ast.AugAssign):
            v = ev(s.value)
            if isinstance(s.target, ast.Name):
                env[s.target.id] = join(env.get(s.target.id, EMPTY), v)
            else:
                self.bind(s.target, v, env, ctx)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            it = elem(ev(s.iter))

            def body():
                self.bind(s.target, it, env, ctx)
                self.block(s.body, env, ctx)
            self.loop(env, body)
            self.block(s.orelse, env, ctx)
        elif isinstance(s, ast.While):
            def body():
                ev(s.test)
                self.block(s.body, env, ctx)
            self.loop(env, body)
            self.block(s.orelse, env, ctx)
        elif isinstance(s, ast.If):
            ev(s.test)
            other = dict(env)
            self.block(s.body, env, ctx)
            self.block(s.orelse, other, ctx)
            self.merge(env, other)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for it in s.items:
                v = ev(it.context_expr)
                if it.optional_vars is not None:
                    self.bind(it.optional_vars, v, env, ctx)
            self.block(s.body, env, ctx)
        elif isinstance(s, ast.Try) or type(s).__name__ == "TryStar":
            self.block(s.body, env, ctx)
            outs = []
            for h in s.handlers:
                e2 = dict(env)
                if h.type is not None:
                    self.eval(h.type, e2, ctx)
                if h.name:
                    e2[h.name] = EMPTY
                self.block(h.body, e2, ctx)
                outs.append(e2)
            self.block(s.orelse, env, ctx)
            self.merge(env, *outs)
            self.block(s.finalbody, env, ctx)
        elif isinstance(s, ast.Return):
            if s.value is not None:
                ctx.ret = join(ctx.ret, ev(s.value))
        elif isinstance(s, ast.Match):
            subj = ev(s.subject)
            outs = []
            for c in s.cases:
                e2 = dict(env)
                for x in ast.walk(c.pattern):
                    if isinstance(x, (ast.MatchAs, ast.MatchStar)) and x.name:
                        e2[x.name] = subj
                if c.guard is not None:
                    self.eval(c.guard, e2, ctx)
                self.block(c.body, e2, ctx)
                outs.append(e2)
            self.merge(env, *outs)
        else:
            for c in ast.iter_child_nodes(s):
                if isinstance(c, ast.expr):
                    ev(c)

    def _default(self, fn, name, v):
        d = self.defaults[fn]
        new = join(d.get(name, EMPTY), v)
        if new != d.get(name, EMPTY):
            d[name] = new
            self.changed = True

    def bind(self, tg, v, env, ctx):
        if isinstance(tg, ast.Name):
            env[tg.id] = v
        elif isinstance(tg, (ast.Tuple, ast.List)):
            stars = [e for e in tg.elts if isinstance(e, ast.Starred)]
            if isinstance(v, Tup) and len(v) == len(tg.elts) and not stars:
                for t, x in zip(tg.elts, v):
                    self.bind(t, x, env, ctx)
            else:
                one = elem(v)
                for t in tg.elts:
                    if isinstance(t, ast.Starred):
                        self.bind(t.value, coll(one), env, ctx)
                    else:
                        self.bind(t, one, env, ctx)
        elif isinstance(tg, ast.Starred):
            self.bind(tg.value, v, env, ctx)
        elif isinstance(tg, ast.Subscript):
            self.store(tg.value, tg.slice, v, env, ctx, tg)
        elif isinstance(tg, ast.Attribute):
            self.eval(tg.value, env, ctx)
            self.escape(v, ctx, tg, f"attribute .{tg.attr}")

    def store(self, container, key, v, env, ctx, node):
        """container[key] = v"""
        kv = self.eval(key, env, ctx) if isinstance(key, ast.expr) else EMPTY
        keys, dyn = str_keys(kv) if not isinstance(key, ast.Slice) else ((), True)
        cur = self.eval(container, env, ctx)
        for t in flat(cur):
            if is_docish(t) and not _built(t):
                for k in keys:
                    self.writes[_split(t)[1] + (k,)].add(self.site(ctx, node))
        wrapped = EMPTY
        for k in keys:
            wrapped = join(wrapped, keyed(v, k))
        if dyn:
            wrapped = join(wrapped, keyed(v, None))
        self.into(container, wrapped, env, ctx, node)

    def into(self, container, v, env, ctx, node):
        """add v (already wrapped for its slot) to the container's value"""
        if not flat(v):
            return
        if isinstance(container, ast.Name):
            name = container.id
            env[name] = join(env.get(name, EMPTY), v)
            if ctx.fn is not None:
                params, local = self.names[ctx.fn]
                docs = frozenset(t for t in flat(v) if is_docish(t))
                if docs and (name in params or name not in local):
                    ctx.outs[name] = join(ctx.outs.get(name, EMPTY), docs)
        elif isinstance(container, ast.Subscript):
            kv = self.eval(container.slice, env, ctx)
            keys, dyn = str_keys(kv)
            wrapped = EMPTY
            for k in keys:
                wrapped = join(wrapped, keyed(v, k))
            if dyn:
                wrapped = join(wrapped, keyed(v, None))
            self.into(container.value, wrapped, env, ctx, node)
        elif isinstance(container, ast.Call) and isinstance(container.func, ast.Attribute) \
                and container.func.attr in ("setdefault", "get") and container.args:
            keys, dyn = str_keys(self.eval(container.args[0], env, ctx))
            wrapped = EMPTY
            for k in keys:
                wrapped = join(wrapped, keyed(v, k))
            if dyn:
                wrapped = join(wrapped, keyed(v, None))
            self.into(container.func.value, wrapped, env, ctx, node)
        else:
            self.escape(v, ctx, node, "store into " + ast.unparse(container)[:40])

    # ------------------------------------------------------------ expressions
    def read(self, v, key, node, ctx):
        out = set()
        for t in flat(v):
            if is_pathish(t):
                out.add(t)
                continue
            if is_kpath(t):
                if t[-1] == ("=", key):
                    out.add(t[:-1])
                continue
            if not is_docish(t):
                continue
            pre, body = _split(t)
            last = body[-1] if body else None
            if last in (BUILT, COMP):
                out.add(t[:-1])
            elif isinstance(last, tuple):
                if last[1] == key:
                    out.add(t[:-1])
            else:
                self.reads[body + (key,)].add(self.site(ctx, node))
                q = _grow(t, key)
                if q is not None:
                    out.add(q)
        return frozenset(out)

    def lookup(self, v, kv, node, ctx):
        """v[k] for a key value kv"""
        if isinstance(v, Rows):
            return elem(v)                      # a list of tuples: v[i]
        keys, dyn = str_keys(kv)
        out = frozenset(t for t in flat(kv) if is_pathish(t))
        for k in keys:
            out |= self.read(v, k, node, ctx)
        if dyn:
            plain = {fmt(_split(t)[1]) for t in flat(v)
                     if is_docish(t) and not _built(t)}
            if plain:
                self.dynamic[self.site(ctx, node)] |= plain
            out |= tmap(members, {t for t in flat(v) if is_docish(t) or is_kpath(t)})
            out |= {t for t in flat(v) if is_pathish(t)}
        return out

    def eval(self, n, env, ctx):
        if n is None:
            return EMPTY
        ev = lambda x: self.eval(x, env, ctx)  # noqa: E731
        if isinstance(n, ast.Name):
            v = env.get(n.id, EMPTY)
            if PATH_NAME.match(n.id):
                v = join(v, frozenset({PATH}))
            return v
        if isinstance(n, ast.Constant):
            if isinstance(n.value, str):
                out = {(STR, n.value)}
                if PATH_STR.search(n.value):
                    out.add(PATH)
                return frozenset(out)
            return EMPTY
        if isinstance(n, ast.JoinedStr):
            text, out = "", set()
            for part in n.values:
                if isinstance(part, ast.Constant):
                    text += str(part.value)
                else:
                    text += "\0"
                    out |= {t for t in flat(ev(part.value)) if is_pathish(t)}
                    if part.format_spec is not None:
                        ev(part.format_spec)
            if PATH_STR.search(text):
                out.add(PATH)
            return frozenset(out)
        if isinstance(n, ast.BoolOp):
            out = EMPTY
            for x in n.values:
                out = join(out, ev(x))
            return out
        if isinstance(n, ast.IfExp):
            ev(n.test)
            return join(ev(n.body), ev(n.orelse))
        if isinstance(n, ast.NamedExpr):
            v = ev(n.value)
            self.bind(n.target, v, env, ctx)
            return v
        if isinstance(n, ast.BinOp):
            a, b = flat(ev(n.left)), flat(ev(n.right))
            keep = tracked if isinstance(n.op, (ast.Add, ast.BitOr)) else is_pathish
            return frozenset(t for t in a | b if keep(t))
        if isinstance(n, ast.Subscript):
            v = ev(n.value)
            sl = n.slice
            if isinstance(sl, ast.Slice):
                for x in (sl.lower, sl.upper, sl.step):
                    ev(x)
                return v
            idx = sl.value if isinstance(sl, ast.Constant) else (
                -sl.operand.value if isinstance(sl, ast.UnaryOp)
                and isinstance(sl.op, ast.USub) and isinstance(sl.operand, ast.Constant)
                else None)
            if isinstance(idx, int) and not isinstance(idx, bool):
                if isinstance(v, Tup) and -len(v) <= idx < len(v):
                    return v[idx]
                return elem(v)
            return self.lookup(v, ev(sl), n, ctx)
        if isinstance(n, ast.Compare):
            left = n.left
            lv = ev(left)
            for op, right in zip(n.ops, n.comparators):
                rv = ev(right)
                if isinstance(op, (ast.In, ast.NotIn)):
                    keys, _ = str_keys(lv)
                    for k in keys:
                        if KEY.match(k):
                            self.read(frozenset(t for t in flat(rv) if is_docish(t)),
                                      k, n, ctx)
                left, lv = right, rv
            return EMPTY
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            env2 = dict(env)
            for g in n.generators:
                self.bind(g.target, elem(self.eval(g.iter, env2, ctx)), env2, ctx)
                for c in g.ifs:
                    self.eval(c, env2, ctx)
            if isinstance(n, ast.DictComp):
                self.eval(n.key, env2, ctx)
                return keyed(self.eval(n.value, env2, ctx), None)
            return coll(self.eval(n.elt, env2, ctx))
        if isinstance(n, ast.Call):
            return self.call(n, env, ctx)
        if isinstance(n, ast.Lambda):
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
                ev(d)
            return EMPTY
        if isinstance(n, ast.Dict):
            out = EMPTY
            for k, x in zip(n.keys, n.values):
                xv = ev(x)
                if k is None:
                    out = join(out, frozenset(t for t in flat(xv) if tracked(t)))
                    continue
                keys, dyn = str_keys(ev(k))
                for kk in keys:
                    out = join(out, keyed(xv, kk))
                if dyn:
                    out = join(out, keyed(xv, None))
            return out
        if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
            parts = []
            starred = EMPTY
            for e in n.elts:
                if isinstance(e, ast.Starred):
                    starred = join(starred, ev(e.value))
                else:
                    parts.append(ev(e))
            if isinstance(n, ast.Tuple) and not flat(starred):
                if not any(isinstance(e, ast.Starred) for e in n.elts):
                    return Tup(parts)
            out = starred
            for p in parts:
                out = join(out, coll(p))
            return out
        if isinstance(n, ast.Starred):
            return ev(n.value)
        if isinstance(n, (ast.Await,)):
            return ev(n.value)
        if isinstance(n, ast.Yield):
            if ctx.fn is not None and n.value is not None:
                ctx.ret = join(ctx.ret, coll(ev(n.value)))
            return EMPTY
        if isinstance(n, ast.YieldFrom):
            if ctx.fn is not None:
                ctx.ret = join(ctx.ret, ev(n.value))
            return EMPTY
        if isinstance(n, ast.Attribute):
            v = ev(n.value)
            out = {t for t in flat(v) if is_pathish(t)} if n.attr not in DERIVED else set()
            if PATH_ATTR.match(n.attr):
                out.add(PATH)
            return frozenset(out)
        for c in ast.iter_child_nodes(n):
            if isinstance(c, ast.expr):
                ev(c)
        return EMPTY

    # ------------------------------------------------------------ calls
    @staticmethod
    def is_parse(n):
        f = n.func
        if isinstance(f, ast.Attribute):
            if f.attr == "load_json":
                return True
            return f.attr in ("load", "loads") and isinstance(f.value, ast.Name) \
                and f.value.id == "json"
        return isinstance(f, ast.Name) and f.id == "load_json"

    def parse(self, n, env, ctx):
        a0 = n.args[0] if n.args else None
        if isinstance(a0, ast.Call) and isinstance(a0.func, ast.Attribute) \
                and a0.func.attr == "dumps" and a0.args:
            return self.eval(a0.args[0], env, ctx)        # a deep copy
        src = EMPTY
        for a in n.args:
            src = join(src, self.eval(a, env, ctx))
        for k in n.keywords:
            self.eval(k.value, env, ctx)
        out = set()
        for t in flat(src):
            if t == PATH:
                out.add(())
                self.roots[self.site(ctx, n)].add("parse")
            elif is_pathish(t):
                out.add((PD,) + t[1:])
        return frozenset(out)

    def callback(self, f, args, env, ctx, node):
        """f(*args) for a function value f the code hands to a builtin"""
        if isinstance(f, ast.Lambda):
            env2 = dict(env)
            ps = f.args.posonlyargs + f.args.args
            for i, p in enumerate(ps):
                env2[p.arg] = args[i] if i < len(args) else EMPTY
            return self.eval(f.body, env2, ctx)
        callees = self.resolve(f, ctx)
        if callees:
            return self.call_values(callees, args, {}, node, ctx, env)
        self.eval(f, env, ctx)
        for a in args:
            self.escape(a, ctx, node, "callback " + ast.unparse(f)[:40])
        return EMPTY

    def call(self, n, env, ctx):
        f = n.func
        if self.is_parse(n):
            return self.parse(n, env, ctx)
        if isinstance(f, ast.Name) and f.id not in ctx.scope \
                and f.id not in ctx.mod.defs and f.id not in ctx.mod.classes \
                and f.id not in ctx.mod.from_alias:
            got = self.builtin(n, f.id, env, ctx)
            if got is not None:
                return got
        if isinstance(f, ast.Attribute) and not (
                isinstance(f.value, ast.Name) and f.value.id in ctx.mod.mod_alias):
            got = self.method(n, f, env, ctx)
            if got is not None:
                return got
        callees = self.resolve(f, ctx)
        if callees:
            return self.user_call(n, callees, env, ctx)
        return self.opaque(n, env, ctx)

    def opaque(self, n, env, ctx):
        """a call the scan cannot see into"""
        f = n.func
        fv = self.eval(f, env, ctx) if not isinstance(f, ast.Attribute) \
            else self.eval(f.value, env, ctx)
        what = ast.unparse(f)[:40]
        out = set()
        external = isinstance(f, ast.Name) and f.id in BUILTINS
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        for a in list(n.args) + [k.value for k in n.keywords]:
            v = self.eval(a, env, ctx)
            if name in PATH_CALLS:
                out |= {t for t in flat(v) if is_pathish(t)}
            if not external:
                self.escape(v, ctx, n, "call " + what)
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id == "subprocess":
            return EMPTY
        if isinstance(f, ast.Attribute) and f.attr not in DERIVED:
            out |= {t for t in flat(fv) if is_pathish(t)}
        return frozenset(out)

    def builtin(self, n, fid, env, ctx):
        if fid in ("enumerate", "zip"):
            vals = [self.eval(a, env, ctx) for a in n.args]
            for k in n.keywords:
                self.eval(k.value, env, ctx)
            if fid == "enumerate":
                return Rows((EMPTY, elem(vals[0]))) if vals else EMPTY
            return Rows(elem(v) for v in vals) if vals else EMPTY
        if fid in ("map", "filter") and len(n.args) >= 2:
            seqs = [self.eval(a, env, ctx) for a in n.args[1:]]
            r = self.callback(n.args[0], [elem(s) for s in seqs], env, ctx, n)
            return coll(r) if fid == "map" else seqs[0]
        if fid in ("min", "max", "next", "sorted", "list", "tuple", "set",
                   "frozenset", "reversed", "iter", "dict", "sum", "any", "all"):
            if not n.args:
                if fid == "dict" and n.keywords:
                    out = EMPTY
                    for k in n.keywords:
                        v = self.eval(k.value, env, ctx)
                        out = join(out, keyed(v, k.arg) if k.arg else v)
                    return out
                for k in n.keywords:
                    self.eval(k.value, env, ctx)
                return EMPTY
            vals = [self.eval(a, env, ctx) for a in n.args]
            one = len(vals) == 1 or fid == "next"
            for k in n.keywords:
                if k.arg == "key":
                    item = elem(vals[0]) if one else self._union(vals)
                    self.callback(k.value, [item], env, ctx, n)
                elif k.arg is None:
                    self.eval(k.value, env, ctx)
                else:
                    kv = self.eval(k.value, env, ctx)
                    if fid in ("min", "max"):
                        vals.append(kv)          # default=
                    if fid == "dict":
                        vals.append(keyed(kv, k.arg))
            if fid in ("min", "max"):
                return elem(vals[0]) if len(n.args) == 1 else self._union(vals)
            if fid == "next":
                return join(elem(vals[0]), self._union(vals[1:]))
            if fid in ("sum", "any", "all"):
                return EMPTY
            if fid == "dict":
                v = vals[0]
                if isinstance(v, Rows) and len(v) == 2:
                    v = keyed(v[1], None)
                return self._union([v] + vals[1:])
            return vals[0]
        return None

    @staticmethod
    def _union(vals):
        out = EMPTY
        for v in vals:
            out = join(out, v)
        return out

    def method(self, n, f, env, ctx):
        attr = f.attr
        recv_node = f.value
        if attr in ("append", "add", "extend", "update", "insert", "appendleft"):
            recv = self.eval(recv_node, env, ctx)
            args = [self.eval(a, env, ctx) for a in n.args]
            for k in n.keywords:
                v = self.eval(k.value, env, ctx)
                if attr == "update" and k.arg:
                    args.append(keyed(v, k.arg))
            if attr == "insert":
                args = args[1:]
            add = EMPTY
            for v in args:
                add = join(add, coll(v) if attr in ("append", "add", "insert",
                                                    "appendleft") else v)
            if flat(add):
                self.into(recv_node, add, env, ctx, n)
            return EMPTY
        if isinstance(recv_node, ast.Name) and recv_node.id in ("copy",) \
                and attr in ("copy", "deepcopy") and n.args:
            return self.eval(n.args[0], env, ctx)
        recv = self.eval(recv_node, env, ctx)
        if attr == "pop" and len(n.args) == 1 and int_const(n.args[0]):
            return elem(recv)                   # list.pop(i)
        if attr in ("get", "pop", "setdefault") and n.args:
            kv = self.eval(n.args[0], env, ctx)
            rest = EMPTY
            for a in n.args[1:]:
                rest = join(rest, self.eval(a, env, ctx))
            for k in n.keywords:
                self.eval(k.value, env, ctx)
            if not any(tracked(t) for t in flat(recv)) \
                    and not any(is_pathish(t) for t in flat(kv)):
                return rest
            return join(self.lookup(recv, kv, n, ctx), rest)
        if not any(tracked(t) for t in flat(recv)):
            return None                     # not a value the scan tracks
        args = [self.eval(a, env, ctx) for a in n.args]
        for k in n.keywords:
            args.append(self.eval(k.value, env, ctx))
        if attr == "items":
            return Rows((EMPTY, tmap(members, recv)))
        if attr == "values":
            return coll(tmap(members, recv))
        if attr == "pop" and not n.args:
            return elem(recv)
        if attr in ("copy",):
            return recv
        out = set()
        if attr not in DERIVED:
            out |= {t for t in flat(recv) if is_pathish(t)}
        if attr in PATH_CALLS:
            for v in args:
                out |= {t for t in flat(v) if is_pathish(t)}
        return frozenset(out)

    def resolve(self, f, ctx):
        """the functions a call of f runs, each with the number of leading
        parameters the call does not pass (self of a constructor, cls of
        a classmethod)"""
        if isinstance(f, ast.Name):
            if f.id in ctx.scope:
                return [(ctx.scope[f.id], 0)]
        out = []
        for d in self.named(f, ctx):
            if isinstance(d, ast.ClassDef):
                init = self.method_def(d, "__init__")
                if init is not None:
                    out.append((init, 1))
            else:
                out.append((d, 0))
        if not out and isinstance(f, ast.Attribute):
            for c in self.named(f.value, ctx):
                if isinstance(c, ast.ClassDef):
                    d = self.method_def(c, f.attr)
                    if d is not None:
                        out.append((d, int(any(
                            isinstance(x, ast.Name) and x.id == "classmethod"
                            for x in d.decorator_list))))
        return out

    def named(self, f, ctx):
        """the top-level functions and classes f names: x, mod.x, from-imported x"""
        if isinstance(f, ast.Name):
            name = f.id
            for table in (ctx.mod.defs, ctx.mod.classes):
                if name in table:
                    return [table[name]]
            if name in ctx.mod.from_alias:
                src, name = ctx.mod.from_alias[name]
                mods = self.by_stem[src]
            else:
                return []
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in ctx.mod.mod_alias:
            name, mods = f.attr, self.by_stem[ctx.mod.mod_alias[f.value.id]]
        else:
            return []
        return [t[name] for m in mods for t in (m.defs, m.classes) if name in t]

    @staticmethod
    def method_def(cls, name):
        for s in cls.body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == name:
                return s
        return None

    def user_call(self, n, callees, env, ctx):
        pos, kws = [], {}
        pos_nodes, kw_nodes = [], {}
        star = False
        for a in n.args:
            if isinstance(a, ast.Starred):
                star = True
                self.escape(self.eval(a.value, env, ctx), ctx, n, "*args")
            else:
                v = self.eval(a, env, ctx)
                pos.append(None if star else v)
                pos_nodes.append(a)
        for k in n.keywords:
            v = self.eval(k.value, env, ctx)
            if k.arg:
                kws[k.arg] = v
                kw_nodes[k.arg] = k.value
            else:
                self.escape(v, ctx, n, "**kwargs")
        return self.call_values(callees, pos, kws, n, ctx, env,
                                (pos_nodes, kw_nodes))

    def call_values(self, callees, pos, kws, node, ctx, env=None, nodes=None):
        out = EMPTY
        for fn, skip in callees:
            a = fn.args
            names = [p.arg for p in a.posonlyargs + a.args][skip:]
            kwonly = [p.arg for p in a.kwonlyargs]
            bound, sent = {}, {}
            for i, v in enumerate(pos):
                if v is None:
                    continue
                if i < len(names):
                    bound[names[i]] = v
                    if nodes is not None:
                        sent[names[i]] = nodes[0][i]
                elif a.vararg is not None:
                    bound[a.vararg.arg] = join(bound.get(a.vararg.arg, EMPTY), coll(v))
                else:
                    self.escape(v, ctx, node, f"extra argument to {fn.name}")
            for k, v in kws.items():
                if k in names or k in kwonly:
                    bound[k] = v
                    if nodes is not None:
                        sent[k] = nodes[1][k]
                elif a.kwarg is not None:
                    bound[a.kwarg.arg] = join(bound.get(a.kwarg.arg, EMPTY), keyed(v, k))
                else:
                    self.escape(v, ctx, node, f"unknown keyword {k} of {fn.name}")
            fid = self.fid[fn]
            ck = self.context(fn, bound)
            if ck not in self.contexts[fn]:
                self.contexts[fn].add(ck)
                self.changed = True
            ps = self.params[(fn, ck)]
            for k, v in bound.items():
                new = join(ps.get(k, EMPTY), self.convert(v, fid, k))
                if new != ps.get(k, EMPTY):
                    ps[k] = new
                    self.changed = True
            ret = self.rets[(fn, ck)]
            outs = self.outs[(fn, ck)]
            if self.deps is not None:
                self.deps.setdefault((fn, ck), (ret, dict(outs)))
            out = join(out, self.returned(ret, fn, bound, node, ctx))
            if env is not None and outs:
                self.stored(fn, outs, bound, sent, node, env, ctx)
        return out

    def stored(self, fn, outs, bound, sent, node, env, ctx):
        """what the callee stored into the objects the call let it see: an
        argument the caller passed, or a closure or module variable"""
        params = self.names[fn][0]
        for name, v in outs.items():
            if name in params:
                target = sent.get(name)     # not passed: the default, its own
                if target is None:
                    continue
                if not isinstance(target, (ast.Name, ast.Subscript, ast.Attribute)):
                    continue                # a fresh object: [], f(x), ...
            elif self.fn_mod[fn] is ctx.mod:
                target = ast.Name(id=name, ctx=ast.Load())
            else:
                continue                    # another module's global: run_module
            self.into(target, self.returned(v, fn, bound, node, ctx), env, ctx, node)

    def context(self, fn, bound):
        """the constant strings this call passes for fn's key params"""
        ck = []
        for k in sorted(self.key_params[fn] & bound.keys()):
            v = bound[k]
            if isinstance(v, frozenset) and v and all(t and t[0] == STR for t in v):
                ck.append((k, tuple(sorted(t[1] for t in v))))
        return tuple(ck)

    def convert(self, v, fid, param):
        """a caller's argument value as the callee sees it"""
        if isinstance(v, (Tup, Rows)):
            return type(v)(self.convert(x, fid, param) for x in v)
        out = set()
        for t in v:
            if is_pathish(t):
                out.add((PP, fid, param))
            elif t and t[0] == PD:
                out.add(t[3:])
            else:
                out.add(t)
        return frozenset(out)

    def returned(self, v, fn, bound, node, ctx):
        """the callee's return value at this call site"""
        if isinstance(v, (Tup, Rows)):
            return type(v)(self.returned(x, fn, bound, node, ctx) for x in v)
        fid = self.fid[fn]
        out = set()
        for t in v:
            if t and t[0] in (PP, PD) and t[1] == fid:
                arg = flat(bound.get(t[2], EMPTY))
                for u in arg:
                    if t[0] == PP and is_pathish(u):
                        out.add(u + t[3:])
                    elif t[0] == PD and u == PATH:
                        out.add(t[3:])
                        self.roots[self.site(ctx, node)].add(f"via {fn.name}()")
                    elif t[0] == PD and u[0] == PP:
                        out.add((PD,) + u[1:] + t[3:])
            else:
                out.add(t)
        return frozenset(out)


def scripts(root: Path):
    return sorted(p for p in (Path(root) / "scripts").rglob("*.py")
                  if "__pycache__" not in p.parts)


def scan(root: Path) -> Scan:
    return Scan(root, scripts(root)).run()


if __name__ == "__main__":
    import sys
    import time
    t0 = time.time()
    s = scan(Path(sys.argv[1]))
    print(f"# {len(s.modules)} files, {s.rounds} rounds, {time.time() - t0:.1f}s, "
          f"{len(s.reads)} reads, {len(s.roots)} roots", file=sys.stderr)
    what = sys.argv[2] if len(sys.argv) > 2 else "reads"
    if what == "reads":
        for p, locs in sorted(s.reads.items(), key=lambda kv: fmt(kv[0])):
            where = sorted(f"{f}:{ln}" for f, ln in locs)
            print(f"{fmt(p)}\t{len(where)}\t{where[0]}")
    elif what == "roots":
        for (f, ln), how in sorted(s.roots.items()):
            print(f"{f}:{ln}\t{','.join(sorted(how))}")
    elif what == "dynamic":
        for (f, ln), ps in sorted(s.dynamic.items()):
            print(f"{f}:{ln}\t{' '.join(sorted(ps))}")
    elif what == "escapes":
        for (f, ln, w), ps in sorted(s.escapes.items()):
            print(f"{f}:{ln}\t{w}\t{' '.join(sorted(ps))}")
    elif what == "writes":
        for p, locs in sorted(s.writes.items(), key=lambda kv: fmt(kv[0])):
            print(f"{fmt(p)}\t{sorted(f'{f}:{ln}' for f, ln in locs)[0]}")
