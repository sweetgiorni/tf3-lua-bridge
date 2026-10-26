#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""End-to-end test of the lua-bridge protocol through tools/bridge_cli.py's Bridge client.
Works against the offline harness (tools/tests/bridge_harness.lua) or the real game: [--dir DIR] [--full]."""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge_cli import Bridge, BridgeError

ap = argparse.ArgumentParser()
ap.add_argument("--dir")
ap.add_argument("--full", action="store_true", help="the mod runs with full Lua access (skips the restricted-env checks)")
args = ap.parse_args()
fails = 0

def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
        print(f"FAIL {name} {detail}")

with Bridge(args.dir) as b:
    check("hello", b.info.get("ok") and b.info.get("version") == 1 and b.info.get("run") and b.info.get("context") == {"session": b.session, "seq": 0}, b.info)
    try:
        Bridge(args.dir).open(timeout=1)
        check("second client is refused", False)
    except BridgeError as e:
        check("second client is refused", "in use" in str(e), e)

    r = b.eval("1 + 1");                      check("expression", r.get("ok") and r["result"] == 2, r)
    r = b.eval("return 1, 'a'");              check("multiple returns", r["result"] == [1, "a"], r)
    r = b.eval("return nil");                 check("nil", r["ok"] and r["result"] is None, r)
    r = b.eval("x = 41 return x + 1");        check("statement", r["result"] == 42, r)
    r = b.eval("x");                          check("env persists", r["result"] == 41, r)
    r = b.eval("error('boom')");              check("runtime error", r["ok"] is False and r["kind"] == "runtime" and "boom" in r["error"] and "traceback" in r, r)
    r = b.eval("return +");                   check("syntax error", r["ok"] is False and r["kind"] == "syntax", r)
    r = b.eval("print('hi', 1) return 5");    check("print captured", r["result"] == 5 and r["log"] == ["hi\t1"], r)
    r = b.eval("return {a={1,2,{b='c'}}, n=1.5}"); check("table", r["result"] == {"a": [1, 2, {"b": "c"}], "n": 1.5}, r)
    r = b.eval("return 'h\\195\\169llo \\240\\159\\152\\128'"); check("utf8", r["result"] == "héllo \U0001F600", r)
    r = b.eval("return string.rep('x', 2*1024*1024)"); check("oversize result", r["ok"] and r.get("truncated") and len(r["result"]) <= 2100, str(r)[:200])
    r = b.eval("local t = {} t.t = t return t"); check("cycle", r["ok"] and r["result"] == {"t": "<cycle>"}, r)
    r = b.request("nope");                    check("unknown op", r["ok"] is False and "unknown_op" in r["error"], r)
    if not args.full:
        r = b.eval("return type(require), type(io), type(load), type(package), type(os.getenv)")
        check("restricted env", r["result"] == ["nil"] * 5, r)

    # a bad line gets an error response and does not stop later requests
    os.write(b._fd_in, b"this is not json\n")
    r = b._read_until(lambda m: "bad_request" in str(m.get("error", "")), 5)
    check("bad json answered", r is not None)
    r = b.eval("return 'still alive'");       check("alive after bad json", r["result"] == "still alive", r)

    def wait_ctx(ctx, t=10):
        return b._read_until(lambda m: m.get("context") == ctx, t)

    # a batch written at once: all answered, in order
    lines = "".join(json.dumps({"op": "eval", "context": {"session": "batch", "seq": i}, "code": f"return {i}"}) + "\n" for i in range(300))
    os.write(b._fd_in, lines.encode())
    got = []
    while len(got) < 300:
        m = b._read_until(lambda m: isinstance(m.get("context"), dict) and m["context"].get("session") == "batch", 10)
        if m is None: break
        got.append(m)
    check("batch complete", len(got) == 300, len(got))
    check("batch ordered and correct", [m.get("result") for m in got] == list(range(300)) and [m["context"]["seq"] for m in got] == list(range(300)))

    # requests written in awkward pieces: the end of one, a whole one, and the start of the next in a single write
    reqs = [json.dumps({"op": "eval", "context": {"session": "split", "seq": i}, "code": f"return '{w}'"}) + "\n" for i, w in enumerate(("one", "two", "three"), 1)]
    pieces = [reqs[0][:10], reqs[0][10:] + reqs[1] + reqs[2][:7], reqs[2][7:]]
    for piece in pieces:
        os.write(b._fd_in, piece.encode()); time.sleep(0.15)
    res = {i: (wait_ctx({"session": "split", "seq": i}, 5) or {}).get("result") for i in (1, 2, 3)}
    check("split writes", res == {1: "one", 2: "two", 3: "three"}, res)

    # many logical sessions interleaved on the one connection: every response carries its own request's context
    order = [("a", 1), ("b", 1), ("a", 2), ("c", 1), ("b", 2), ("a", 3), ("c", 2), ("b", 3)] * 10
    seqs = {}
    sent = []
    for sess, _ in order:
        seqs[sess] = seqs.get(sess, 0) + 1
        sent.append({"session": sess, "seq": seqs[sess]})
    os.write(b._fd_in, "".join(json.dumps({"op": "eval", "context": c, "code": f"return '{c['session']}{c['seq']}'"}) + "\n" for c in sent).encode())
    replies = [wait_ctx(c) for c in sent]
    check("interleaved sessions", all(r and r["result"] == f"{c['session']}{c['seq']}" for r, c in zip(replies, sent)))

    # the mod does not interpret `context`: any JSON value comes back as it went in
    for name, ctx in (("nested map", {"s": "x", "n": [1, 2.5, {"k": None}], "t": True}), ("empty map", {}), ("empty list", []),
                      ("string", "plain"), ("number", 7), ("null", None), ("false", False), ("unicode", {"k": "h\u00e9 \U0001F600"})):
        os.write(b._fd_in, (json.dumps({"op": "ping", "context": ctx}) + "\n").encode())
        r = b._read_until(lambda m: m.get("op") == "ping" and "context" in m and m["context"] == ctx and type(m["context"]) is type(ctx), 5)
        check("context echo: " + name, r is not None)
    os.write(b._fd_in, b'{"op":"ping"}\n')
    r = b._read_until(lambda m: m.get("op") == "ping" and "context" not in m, 5)
    check("no context in, none out", r is not None)
    # errors carry the context too
    os.write(b._fd_in, b'{"op":"eval","context":{"e":1},"code":"return +"}\n{"op":"eval","context":{"e":2},"code":"error(1)"}\n{"op":"zzz","context":{"e":3}}\n')
    errs = [wait_ctx({"e": i}, 5) for i in (1, 2, 3)]
    check("errors keep context", all(e and e["ok"] is False for e in errs), errs)

    # ---- engine state: requests travel as script events, answers come back through the game script state
    r = b.eval("return 6 * 7", state="engine");              check("engine eval", r.get("ok") and r["result"] == 42 and r.get("engineState"), r)
    r = b.eval("error('boom')", state="engine");            check("engine runtime error", r["ok"] is False and r["kind"] == "runtime" and "boom" in r["error"] and r.get("traceback"), r)
    r = b.eval("return +", state="engine");                 check("engine syntax error", r["ok"] is False and r["kind"] == "syntax", r)
    r = b.eval("print('hi') return 1", state="engine");     check("engine print captured", r["result"] == 1 and r["log"] == ["hi"], r)
    # not tested: variable persistence. Events are handled by whichever engine Lua state picks them up (3 different
    # states answered 9 requests), and each state has its own environment, so `x = 1` is not reliably visible later.
    r = b.eval("return string.rep('x', 400000)", state="engine"); check("engine oversize result", r["ok"] and r.get("truncated"), str(r)[:160])
    sent = [{"session": "eng", "seq": i} for i in range(30)]
    os.write(b._fd_in, "".join(json.dumps({"op": "eval", "state": "engine", "context": c, "code": f"return {c['seq']} * 2"}) + "\n" for c in sent).encode())
    replies = [wait_ctx(c, 30) for c in sent]
    check("engine pipelined", all(rp and rp.get("result") == c["seq"] * 2 for rp, c in zip(replies, sent)), [rp and rp.get("result") for rp in replies][:8])
    time.sleep(1.5)  # acks
    r = b.eval("""local s = api.engine.getComponent(api.engine.system.gameScriptSystem.getEntityForGameScript("lua_bridge::/lua_bridge/bridge.gs"), api.type.ComponentType.GAME_SCRIPT).state
return next(s.responses or {}) == nil""")
    check("engine answers cleared from the shared state", r.get("result") is True, r)
    r = b.eval("return 1", state="nonsense");               check("unknown state rejected", r["ok"] is False and "unsupported_state" in r["error"], r)

# lock released, files stay
check("lock removed", not os.path.exists(os.path.join(b.dir, "bridge.lock")))
print("bridge e2e: all passed" if fails == 0 else f"bridge e2e: {fails} FAILED")
sys.exit(1 if fails else 0)
