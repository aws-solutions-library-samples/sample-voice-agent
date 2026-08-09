# .agents/

Agent-agnostic configuration for this repo, following the [Agent Skills](https://agentskills.io)
open standard and [AGENTS.md](https://agents.md) conventions.

## Why

Skills previously lived only in `.claude/skills/`, which meant only Claude Code could
discover them. Other coding agents (Cursor, Codex, Gemini CLI, etc.) that support the
Agent Skills spec look in agent-agnostic locations. Canonicalizing skill content here
means any compliant agent can use these skills without duplicating files.

## Layout

- `skills/` — Shared skills (committed). Each is a directory with a `SKILL.md`.
- `skills-local/` — Private/local-only skills (gitignored).
- `scripts/link-skills.sh` — Creates/repairs symlinks from agent-native directories
  (e.g. `.claude/skills/`) into `skills/`.

## Adding a skill

1. Create `.agents/skills/<name>/SKILL.md` with `name` + `description` frontmatter.
2. Run `.agents/scripts/link-skills.sh` to create the symlink(s).
3. Commit the skill directory and the new symlink together.

## Rules

- Edit skills in `.agents/skills/` only. Never edit through the `.claude/skills/`
  symlinks directly (they resolve to the same files, but keeping edits centralized
  avoids confusion).
- Do not commit real files under `.claude/skills/` (or other agent-native skill dirs) —
  only symlinks belong there.
- If you add support for a new agent's skill directory, add it to the `TARGETS` array
  in `scripts/link-skills.sh` and re-run the script.
