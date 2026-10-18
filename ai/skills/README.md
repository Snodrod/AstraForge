# AstraForge skills for LLMs

This folder holds the skills that let any LLM (Claude Code, Codex, a local model) install, verify and drive the studio:

| Skill | Contents |
|---|---|
| `astraforge/` | the whole studio: installation from scratch (`references/install.md`), inventory of components and models (`components.md`), architecture and CLI (`studio.md`), pitfalls (`troubleshooting.md`), state check `scripts/af_status.ps1` |
| `astraforge-vfx/` | effects on a model, the VFX texture generator, external generators, export to Godot; Notion database export, check server, Godot check |

This folder is the single source. In Claude Code it is connected with links (junctions), so any session sees the skills:

```bat
mklink /J "%USERPROFILE%\.claude\skills\astraforge" "<AstraForge folder>\ai\skills\astraforge"
mklink /J "%USERPROFILE%\.claude\skills\astraforge-vfx" "<AstraForge folder>\ai\skills\astraforge-vfx"
```

For other models these are plain markdown files: start with `astraforge/SKILL.md`. Documentation for humans is `README.md`
and `docs/` in the studio root. When you change the studio, update the skill in the same commit.

Do not put anything machine-specific in this folder (paths to your own projects, role ordering, a snapshot of what is
installed, links to personal notes): keep that in `%USERPROFILE%\.claude\astraforge-local\README.md` — the skill reads it if it exists.
