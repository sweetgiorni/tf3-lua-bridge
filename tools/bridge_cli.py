#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Command line client for the lua-bridge mod (protocol: PROTOCOL.md).

    tools/bridge_cli.py eval 'return 1 + 1'          run Lua in the game's GUI state, print the JSON result
    tools/bridge_cli.py ping
    tools/bridge_cli.py hello

Also importable: `Bridge` is the client the MCP server will use. One process at a time holds the bridge (bridge.lock);
that process can serve many logical sessions, because every request carries an opaque `context` (here
{"session": ..., "seq": ...}) that the mod copies into the response untouched.
"""
import argparse, json, os, subprocess, sys, time, uuid

LINE_LIMIT = 8 * 1024 * 1024


BIN = getattr(os, "O_BINARY", 0)  # Windows would otherwise translate newlines on every os.open'ed file


def is_wsl() -> bool:
    try:
        return "microsoft" in open("/proc/version").read().lower()
    except OSError:
        return False


def default_dir() -> str:
    """The bridge folder the mod looks in: <temp>/tf3_bridge, with the same temp folder the game uses. TF3_BRIDGE_DIR
    overrides it."""
    if os.environ.get("TF3_BRIDGE_DIR"):
        return os.environ["TF3_BRIDGE_DIR"]
    if os.name == "nt":
        return os.path.join(os.environ.get("TEMP") or os.environ["TMP"], "tf3_bridge")
    if not is_wsl():  # Linux or macOS, the game and this client share a temp folder
        import tempfile

        return os.path.join(tempfile.gettempdir(), "tf3_bridge")
    # Linux inside WSL: the game's %TEMP% as seen from here. No stdin: when we are an MCP stdio proxy, cmd.exe would
    # otherwise read (and swallow) the client's messages.
    win = subprocess.run(["cmd.exe", "/c", "echo %TEMP%"], capture_output=True, text=True, cwd="/mnt/c", stdin=subprocess.DEVNULL).stdout.strip()
    path = subprocess.run(["wslpath", win], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
    return os.path.join(path, "tf3_bridge")


def ensure_dir(path: str) -> None:
    """Create the bridge folder private to this user (the files let whoever writes them run Lua in the game). On
    Linux and macOS, where the temp folder is shared, refuse a folder somebody else created."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "posix" and not os.path.abspath(path).startswith("/mnt/") and os.stat(path).st_uid != os.getuid():
        raise PermissionError(f"{path} belongs to another user; set TF3_BRIDGE_DIR or remove it")


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self, directory: str | None = None):
        self.dir = directory or default_dir()
        self.session = uuid.uuid4().hex[:12]  # default session for requests that do not name one
        self._lock = os.path.join(self.dir, "bridge.lock")
        self._in = os.path.join(self.dir, "in.jsonl")
        self._out = os.path.join(self.dir, "out.jsonl")
        self._seqs: dict[str, int] = {}
        self._buf = b""
        self._pending: list[dict] = []
        self._fd_in = None
        self._fd_out = None
        self.info: dict = {}

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def open(self, timeout: float = 10.0) -> dict:
        ensure_dir(self.dir)
        try:
            fd = os.open(self._lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BIN)
        except FileExistsError:
            raise BridgeError(f"bridge in use: {self._lock} exists (delete it if no client is running)")
        os.write(fd, f"{self.session} pid={os.getpid()}\n".encode())
        os.close(fd)
        try:
            self._fd_in = os.open(self._in, os.O_WRONLY | os.O_CREAT | os.O_APPEND | BIN)
            self._fd_out = os.open(self._out, os.O_RDONLY | os.O_CREAT | BIN)
            os.lseek(self._fd_out, 0, os.SEEK_END)  # only responses written from now on are ours
            os.write(self._fd_in, b"\n")  # closes off a torn line left by a crashed client
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ctx = {"session": self.session, "seq": 0}
                self._send({"op": "hello", "context": ctx})  # idempotent: resend until answered
                msg = self._read_until(lambda m: m.get("op") == "hello" and m.get("context") == ctx, 1.0)
                if msg:
                    self.info = msg
                    return msg
            raise BridgeError("no answer from the game: is a save loaded and the Lua Bridge mod enabled?")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for fd in (self._fd_in, self._fd_out):
            if fd is not None:
                os.close(fd)
        self._fd_in = self._fd_out = None
        try:
            os.remove(self._lock)
        except FileNotFoundError:
            pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- requests ----------------------------------------------------------------------------------------------
    def _send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        os.write(self._fd_in, data)

    def _read_until(self, match, timeout: float):
        """Next message satisfying `match`; messages that do not match are kept for later calls."""
        deadline = time.monotonic() + timeout
        while True:
            for i, msg in enumerate(self._pending):
                if match(msg):
                    del self._pending[i]
                    return msg
            chunk = os.read(self._fd_out, 1 << 20)
            if chunk:
                self._buf += chunk
                *lines, self._buf = self._buf.split(b"\n")  # the last piece is an unfinished line (or empty)
                for line in lines:
                    if line.strip():
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            self._pending.append(msg)
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def request(self, op: str, timeout: float = 30.0, session: str | None = None, **fields) -> dict:
        """Send one request and wait for its response. `session` (default: this client's) plus a per-session sequence
        number form the context the mod echoes back, which is how concurrent sessions are told apart."""
        session = session or self.session
        seq = self._seqs.get(session, 0) + 1
        self._seqs[session] = seq
        ctx = {"session": session, "seq": seq}
        self._send({"op": op, "context": ctx, **fields})
        msg = self._read_until(lambda m: m.get("context") == ctx, timeout)
        if msg is None:
            raise BridgeError(f"timeout after {timeout}s waiting for {op} {ctx}")
        return msg

    def eval(self, code: str, timeout: float = 30.0, session: str | None = None, **fields) -> dict:
        return self.request("eval", timeout, session, code=code, **fields)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["eval", "ping", "hello"])
    ap.add_argument("code", nargs="?", help="Lua code for eval (or - to read stdin)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--dir", help="bridge folder (default: %%TEMP%%\\tf3_bridge via WSL)")
    args = ap.parse_args()
    try:
        with Bridge(args.dir) as b:
            if args.op == "hello":
                out = b.info
            elif args.op == "ping":
                out = b.request("ping", args.timeout)
            else:
                code = sys.stdin.read() if args.code == "-" else args.code
                if code is None:
                    ap.error("eval needs code")
                out = b.eval(code, args.timeout)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if out.get("ok", True) else 2
    except BridgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
