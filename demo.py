#!/usr/bin/env python3
"""End-to-end minigit demo: two users, one server, a rejected push and a conflict.

Run from the repo root:  python demo.py
(Set PYTHONPATH=<checkout> to demo a different checkout.) Standard library only.
Everything lives in a temp directory that is removed at the end, and the server
subprocess is always killed, also on error or Ctrl-C.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ)
ENV["PYTHONPATH"] = os.pathsep.join([p for p in (os.environ.get("PYTHONPATH"), HERE) if p])
ENV["PYTHONIOENCODING"] = "utf-8"
ENV.pop("MINIGIT_USER", None)
ENV.pop("MINIGIT_TOKEN", None)


def step(title):
    print("\n" + "=" * 70 + "\n== " + title + "\n" + "=" * 70, flush=True)


def mg(*args, cwd=None, expect=0, env=None):
    """Run `minigit <args>`, echo it and its output, check the exit code."""
    shown = " ".join(re.sub(r"//(\w+):[^@/]+@", r"//\1:TOKEN@", a) for a in args)
    print("$ minigit " + shown + (f"    (in {os.path.basename(cwd)})" if cwd else ""), flush=True)
    r = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd, env=env or ENV,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    out = (r.stdout + r.stderr).rstrip()
    if out:
        print("  " + out.replace("\n", "\n  "), flush=True)
    if expect is not None and r.returncode != expect:
        raise SystemExit(f"demo failed: expected exit {expect}, got {r.returncode} from: minigit {shown}")
    return r


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def person(name):
    e = dict(ENV)
    e["MINIGIT_AUTHOR_NAME"] = name.title()
    e["MINIGIT_AUTHOR_EMAIL"] = f"{name}@example.com"
    return e


def main(tmp):
    srv = os.path.join(tmp, "srv")
    ready = os.path.join(tmp, "ready.txt")
    proc = None
    try:
        step("1. Create the server root, two users and a repository")
        mg("server", "init", srv)
        tokens = {}
        for user in ("alice", "bob"):
            r = mg("server", "add-user", srv, user)
            m = (r.stdout + " " + r.stderr).split()
            if not m:
                raise SystemExit("demo failed: could not find a token in add-user output")
            tokens[user] = m[-1]
        mg("server", "create-repo", srv, "project", "--writers", "alice,bob")

        step("2. Start the server on a free port")
        proc = subprocess.Popen([sys.executable, "-m", "minigit", "serve", "--root", srv, "--port", "0",
                                 "--ready-file", ready, "--quiet"], env=ENV,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            if os.path.exists(ready) and open(ready).read().strip():
                break
            if proc.poll() is not None:
                raise SystemExit("demo failed: server exited early")
            time.sleep(0.1)
        else:
            raise SystemExit("demo failed: server did not become ready")
        hostport = open(ready).read().strip()
        print(f"server is up at http://{hostport}/", flush=True)

        def url(user):
            return f"http://{user}:{tokens[user]}@{hostport}/project"

        a, b = os.path.join(tmp, "alice"), os.path.join(tmp, "bob")
        ea, eb = person("alice"), person("bob")

        step("3. Alice and Bob clone the (empty) repository")
        mg("clone", url("alice"), a, env=ea)
        mg("clone", url("bob"), b, env=eb)

        step("4. Alice commits a greeting and pushes")
        write(os.path.join(a, "greeting.txt"), "hello\nfrom the project\n")
        mg("add", "greeting.txt", cwd=a, env=ea)
        mg("commit", "-m", "Add greeting", cwd=a, env=ea)
        mg("push", "-u", "origin", "main", cwd=a, env=ea)
        mg("pull", "origin", "main", cwd=b, env=eb)  # Bob gets the base commit

        step("5. Alice pushes a second change; Bob, who has not pulled, edits the same line and pushes")
        write(os.path.join(a, "greeting.txt"), "hello\nfrom Alice\n")
        mg("commit", "-a", "-m", "Alice signs the greeting", cwd=a, env=ea)
        mg("push", cwd=a, env=ea)
        write(os.path.join(b, "greeting.txt"), "hello\nfrom Bob\n")
        mg("commit", "-a", "-m", "Bob signs the greeting", cwd=b, env=eb)
        print("-- Bob's push is not a fast-forward, so the server refuses it:")
        mg("push", cwd=b, env=eb, expect=1)

        step("6. Bob pulls: the same line changed on both sides, so there is a conflict")
        mg("pull", cwd=b, env=eb, expect=None)
        mg("status", cwd=b, env=eb, expect=None)
        print("conflicted file:\n  " + open(os.path.join(b, "greeting.txt")).read().replace("\n", "\n  "))

        step("7. Bob resolves it, commits and pushes")
        write(os.path.join(b, "greeting.txt"), "hello\nfrom Alice and Bob\n")
        mg("add", "greeting.txt", cwd=b, env=eb)
        mg("commit", "-m", "Merge Alice's greeting, sign as both", cwd=b, env=eb)
        mg("push", cwd=b, env=eb)

        step("8. Alice pulls and sees the full history")
        mg("pull", cwd=a, env=ea)
        mg("log", "--oneline", "--all", cwd=a, env=ea)

        step("9. Web UI")
        print(f"While the server runs, the repository is browsable at http://{hostport}/-/")
        print("(sign in as alice or bob with their token). The server stops when this script ends.")
        print("\nDemo finished OK.", flush=True)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="minigit-demo-")
    try:
        main(tmp)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
