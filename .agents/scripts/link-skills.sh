#!/usr/bin/env bash
# Sync .agents/skills/ into agent-native skill directories via symlinks.
#
# Canonical skill content lives in .agents/skills/<name>/SKILL.md.
# This script creates/repairs symlinks in each agent-native directory so
# every coding agent (Claude Code, Cursor, Codex, etc.) can discover the
# same skills without duplicating content.
#
# Usage: .agents/scripts/link-skills.sh
set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$AGENTS_DIR/.." && pwd)"

# Add agent-native skill directories here as support is needed.
TARGETS=(".claude/skills")

declare -A valid_skills

for target_dir in "${TARGETS[@]}"; do
  mkdir -p "$REPO_ROOT/$target_dir"

  for skill_dir in "$AGENTS_DIR"/skills/*/; do
    [ -d "$skill_dir" ] || continue
    name="$(basename "$skill_dir")"
    valid_skills["$name"]=1

    link_target="../../.agents/skills/$name"
    link_path="$REPO_ROOT/$target_dir/$name"

    if [ -L "$link_path" ]; then
      if [ "$(readlink "$link_path")" = "$link_target" ]; then
        continue
      fi
      rm "$link_path"
    elif [ -e "$link_path" ]; then
      echo "ERROR: $link_path exists but is not a symlink. Move real content into .agents/skills/ first." >&2
      exit 1
    fi

    ln -s "$link_target" "$link_path"
    echo "Linked: $target_dir/$name -> $link_target"
  done

  # Prune stale symlinks that point into .agents/skills but no longer have a source
  for link in "$REPO_ROOT/$target_dir"/*/; do
    [ -L "${link%/}" ] || continue
    link="${link%/}"
    target="$(readlink "$link")"
    if [[ "$target" == *"/.agents/skills/"* || "$target" == "../../.agents/skills/"* ]]; then
      name="$(basename "$link")"
      if [ -z "${valid_skills[$name]+x}" ]; then
        rm "$link"
        echo "Pruned: $target_dir/$name"
      fi
    fi
  done
done

echo "Done. Skills are canonical under .agents/skills/; agent-native dirs contain symlinks only."
