#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/slocook/cdb.git"
INSTALL_DIR="${CDB_HOME:-$HOME/.local/share/cdb}"

# If running from a checkout, use that directory; otherwise clone
if [ -f "$(dirname "$0")/SKILL.md" ] 2>/dev/null; then
  CDB_DIR="$(cd "$(dirname "$0")" && pwd)"
else
  # curl | bash mode — clone the repo
  if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation at $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
  else
    echo "Cloning cdb to $INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
  CDB_DIR="$INSTALL_DIR"
fi

# Register MCP server (user scope = available in all projects)
# Remove first so reinstalls and updates don't fail with "already exists".
claude mcp remove --scope user cdb 2>/dev/null || true
claude mcp add --scope user cdb -- \
  uv run --directory "$CDB_DIR" cdb-mcp

# Install skill
SKILL_DIR="$HOME/.claude/skills/cdb"
mkdir -p "$SKILL_DIR"
cp "$CDB_DIR/SKILL.md" "$SKILL_DIR/SKILL.md"

echo ""
echo "Done. cdb MCP server registered and skill installed."
echo "Verify: claude mcp list"
