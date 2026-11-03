#!/bin/sh
# Copy the self-contained skill to both hosts; preserve unrelated target files.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_source="$script_dir/../skills/paper-foundations"

if [ ! -f "$skill_source/SKILL.md" ] || [ ! -f "$skill_source/scripts/paperread.py" ]; then
    printf '%s\n' "Incomplete skill: expected SKILL.md and scripts/paperread.py in $skill_source" >&2
    exit 1
fi
if [ -z "${HOME:-}" ]; then
    printf '%s\n' 'HOME must be set to install the skill.' >&2
    exit 1
fi

claude_target="$HOME/.claude/skills/paper-foundations"
codex_target="${CODEX_HOME:-$HOME/.codex}/skills/paper-foundations"

for target in "$claude_target" "$codex_target"; do
    mkdir -p "$target"
    cp -R "$skill_source/." "$target/"
done

printf 'Installed Claude Code skill: %s\n' "$claude_target"
printf 'Installed Codex skill: %s\n' "$codex_target"
printf '%s\n' 'Restart Claude Code / Codex to load the skill.'
