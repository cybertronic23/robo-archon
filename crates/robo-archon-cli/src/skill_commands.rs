//! Metadata-only skill CLI. Never starts a backend or executes a runner.
use crate::Args;
use anyhow::{Context, Result};

pub fn handle(args: &Args) -> Result<bool> {
    if args.list_skills || args.inspect_skill.is_some() || args.validate_skill_call.is_some() {
        let registry = robo_archon_skills::SkillRegistry::load_dir(&args.skills_dir)?;
        if let Some(path) = &args.validate_skill_call {
            let catalog = robo_archon_embodied::body::BodyCatalog::load(&args.body_catalog)?;
            let call: robo_archon_skills::SkillCall =
                serde_json::from_slice(&std::fs::read(path)?)?;
            let prepared = registry.prepare(
                &catalog,
                args.robot.as_deref().context("--robot required")?,
                &args.backend,
                &call,
            )?;
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"validation_only":true,"execution_supported":false,"prepared":prepared})
                )?
            );
        } else if let Some(id) = &args.inspect_skill {
            let skill = registry.get(id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"manifest":skill,"tool_definition":skill.tool_definition(),"execution_supported":false})
                )?
            );
        } else {
            for skill in registry.list() {
                println!(
                    "{}@{} — {} [metadata only; runner execution not implemented]",
                    skill.id, skill.version, skill.description
                );
            }
        }
        return Ok(true);
    }

    Ok(false)
}
