# Skill packages

Each immediate package directory contains `skill.json`. To register your own package, copy an example into your own directory, change its ID and tool name, then select that directory with `--skills-dir`. All names must be unique within a loaded registry. The CLI reloads this data on each invocation; there is no package installer or hot-reload service yet.

These manifests are **data only**. The Franka example references existing body metadata but has no Skill runner. The Microduck example is a draft with a planned body binding. Registration does not imply either can execute.

Use `--list-skills`, `--inspect-skill ID`, and `--validate-skill-call REQUEST.json --robot BODY --backend PLATFORM`. See [the implemented spec](../docs/specs/m2g-1-skill-registry.md) and [the extensible execution design](../docs/skills-design.md). Custom policy execution and LLM routing arrive in later milestones.
