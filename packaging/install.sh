#!/bin/sh
# Installs the minigit binary next to this script into ~/.local/bin (or the directory
# given as the first argument) and makes sure that directory is on PATH.
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest=${1:-$HOME/.local/bin}
mkdir -p "$dest"
cp "$here/minigit" "$dest/minigit"
chmod 755 "$dest/minigit"
if [ "$(uname)" = "Darwin" ]; then
  xattr -d com.apple.quarantine "$dest/minigit" 2>/dev/null || true
fi
case ":$PATH:" in
  *":$dest:"*) ;;
  *)
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      if [ -f "$rc" ] || [ "$rc" = "$HOME/.profile" ]; then
        printf '\n# minigit\nexport PATH="%s:$PATH"\n' "$dest" >> "$rc"
      fi
    done
    echo "Added $dest to PATH in your shell profile; open a new terminal to use 'minigit'."
    ;;
esac
echo "Installed $("$dest/minigit" --version) to $dest/minigit"
