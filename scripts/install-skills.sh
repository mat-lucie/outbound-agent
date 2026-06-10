#!/bin/bash
# Sync repo skill definitions to the Claude Code skills directory.
#
# Per §0 invariant #6: no cron, no launchd, no scheduled jobs. Every
# The workflow runs as an operator-invoked slash command in Claude
# Code. This script copies the SKILL.md files from `skills/` to
# `~/.claude/skills/` so the slash commands appear in the Claude Code
# command palette.
#
# Idempotent: re-running overwrites the destination files.
#
# Replaces scripts/install-cron.sh — there is no production cron.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_SRC="$REPO_ROOT/skills"
SKILLS_DST="$HOME/.claude/skills"

if [[ ! -d "$SKILLS_SRC" ]]; then
    echo "error: skills source not found: $SKILLS_SRC" >&2
    exit 1
fi

mkdir -p "$SKILLS_DST"

count=0
for skill_dir in "$SKILLS_SRC"/*/; do
    skill_name="$(basename "$skill_dir")"
    src="$skill_dir/SKILL.md"
    dst="$SKILLS_DST/$skill_name"

    if [[ ! -f "$src" ]]; then
        echo "warning: $skill_name has no SKILL.md; skipping" >&2
        continue
    fi

    mkdir -p "$dst"
    cp -f "$src" "$dst/SKILL.md"
    echo "synced: /$skill_name -> $dst/SKILL.md"
    count=$((count + 1))
done

echo ""
echo "synced $count skill(s) to $SKILLS_DST"
echo "verify in Claude Code: type / to see the slash-command palette"
echo ""
echo "If you previously installed the legacy cron, remove it:"
echo "  crontab -e    # delete the MAILTO + daily/weekly agent lines"
echo "  crontab -l    # verify they are gone"
