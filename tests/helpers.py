"""tests.helpers -- owner: M7 cli.

Drive the real CLI as a subprocess (``python -m minigit``) and run a live
server for the end-to-end tests. Nothing here imports the library, so the
helpers work no matter which modules exist yet.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 30


@dataclass
class Result:
    """Outcome of one CLI invocation (stdout/stderr are bytes)."""

    args: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    @property
    def text(self) -> str:
        """stdout and stderr together (for assertions that do not care)."""
        return self.out + self.err

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"minigit {' '.join(self.args)} -> exit {self.returncode}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def _env_with_pythonpath(env: dict | None) -> dict:
    full = dict(os.environ if env is None else env)
    prev = full.get("PYTHONPATH")
    full["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    full.setdefault("PYTHONIOENCODING", "utf-8")
    return full


def run(args, cwd, env=None, check=True) -> Result:
    """Run ``python -m minigit *args`` in ``cwd``; capture bytes; 30 s timeout.

    With ``check`` (the default) a non-zero exit raises AssertionError so a
    failing step shows its output. Pass ``check=False`` to inspect failures.
    """
    args = [str(a) for a in args]
    proc = subprocess.run(
        [sys.executable, "-m", "minigit", *args],
        cwd=cwd, env=_env_with_pythonpath(env), capture_output=True, timeout=TIMEOUT,
    )
    res = Result(args, proc.returncode, proc.stdout, proc.stderr)
    if check and res.returncode != 0:
        raise AssertionError(f"command failed:\n{res}")
    return res


def make_env(tmp, user=None, token=None) -> dict:
    """A full environment for the CLI, isolated under ``tmp``.

    Sets the global config and credentials file paths, HOME, a fixed identity
    and a fixed author date, and (optionally) MINIGIT_USER/MINIGIT_TOKEN.
    """
    env = dict(os.environ)
    tmp = os.path.abspath(tmp)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["MINIGIT_AUTHOR_NAME"] = user or "Test User"
    env["MINIGIT_AUTHOR_EMAIL"] = f"{user or 'test'}@example.com"
    env["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
    env.pop("MINIGIT_DEBUG", None)
    for key in ("MINIGIT_USER", "MINIGIT_TOKEN"):
        env.pop(key, None)
    if user is not None and token is not None:
        env["MINIGIT_USER"] = user
        env["MINIGIT_TOKEN"] = token
    return env


def write(path, data) -> None:
    """Write bytes (or UTF-8 text) to ``path``, creating parent directories."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class ServerProcess:
    """Run ``minigit serve --port 0 --ready-file F --quiet`` for a test.

    Usage::

        with ServerProcess(root, env=env) as srv:
            run(["clone", srv.url + "/proj"], cwd=tmp, env=env)

    ``__enter__`` polls the ready file for up to 10 s and sets ``.url``
    (``http://127.0.0.1:PORT``). ``__exit__`` terminates the process, waits up
    to 5 s and then kills it, so the server is always gone before the caller's
    temporary directory is removed.
    """

    def __init__(self, root, env=None, host="127.0.0.1"):
        self.root = os.path.abspath(root)
        self.env = _env_with_pythonpath(env)
        self.host = host
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self._log = None
        parent = os.path.dirname(self.root)
        self.ready_file = os.path.join(parent, f"ready-{os.path.basename(self.root)}-{os.getpid()}.txt")
        self.log_path = os.path.join(parent, f"server-{os.path.basename(self.root)}-{os.getpid()}.log")

    def start(self) -> "ServerProcess":
        if os.path.exists(self.ready_file):
            os.unlink(self.ready_file)
        self._log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "minigit", "serve", "--root", self.root, "--host", self.host,
             "--port", "0", "--ready-file", self.ready_file, "--quiet"],
            cwd=os.path.dirname(self.root), env=self.env,
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._close_log()
                raise RuntimeError(f"server exited early (code {self.proc.returncode}):\n{self.log()}")
            try:
                with open(self.ready_file, "rb") as fh:
                    line = fh.read().decode("ascii", "replace").strip()
            except OSError:
                line = ""
            if line:
                self.url = "http://" + line
                return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"server did not become ready within 10 s:\n{self.log()}")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
        self._close_log()
        try:
            os.unlink(self.ready_file)
        except OSError:
            pass

    def log(self) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            finally:
                self._log = None

    def __enter__(self) -> "ServerProcess":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
