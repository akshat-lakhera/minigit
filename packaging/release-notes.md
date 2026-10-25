minigit {VERSION}: a small git clone with a multi-user HTTP server. Nothing else to install.

## Downloads

| System | File | Install |
|---|---|---|
| Windows 10/11 (x64) | `minigit-{VERSION}-windows-x64-setup.exe` | run it; it installs for your user and adds `minigit` to PATH (no admin needed) |
| Windows, no installer | `minigit-{VERSION}-windows-x64.zip` | unzip `minigit.exe` anywhere on your PATH |
| Linux x64 | `minigit-{VERSION}-linux-x64.tar.gz` | `tar xzf` it, then `./minigit-{VERSION}/install.sh` (installs to `~/.local/bin`) |
| macOS Intel / Apple silicon | `minigit-{VERSION}-macos-x64.tar.gz`, `minigit-{VERSION}-macos-arm64.tar.gz` | same as Linux; the script also clears the quarantine flag |
| Any OS with Python 3.11+ | `minigit-{VERSION}-py3-none-any.whl` | `pipx install <file>` or `pip install <file>` |

`SHA256SUMS.txt` lists the checksum of every file.

The executables are not code-signed. Windows SmartScreen and macOS Gatekeeper may ask once; choose "Run anyway"
(Windows) or allow it under System Settings > Privacy & Security (macOS).

## First steps

```
minigit config --global user.name "Your Name"
minigit config --global user.email you@example.com
minigit login --user USER --token TOKEN https://your-hub.example.com
minigit clone https://your-hub.example.com/project
```

Run `minigit --help` for the command list and `minigit <command> --help` for one command.
