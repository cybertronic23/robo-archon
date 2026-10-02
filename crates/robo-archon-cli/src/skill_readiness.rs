//! Structural readiness is checked locally; learned behavior acceptance is recorded separately.
use anyhow::{bail, Context, Result};
use robo_archon_embodied::body::{BodyCatalog, PolicyProfile, SourceLink};
use robo_archon_skills::{ParameterKind, SkillManifest, SkillRegistry};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    time::Duration,
};

pub struct Readiness {
    pub catalog: BodyCatalog,
    pub packages: BTreeMap<String, Option<PathBuf>>,
    pub skills: Vec<SkillManifest>,
    pub rejected: Vec<Value>,
}

pub async fn inspect(
    python: &str,
    root: &Path,
    assets: &Path,
    policies: &Path,
    registry: &SkillRegistry,
    mut catalog: BodyCatalog,
    body: &str,
    backend: &str,
) -> Result<Readiness> {
    let output = tokio::time::timeout(
        Duration::from_secs(30),
        tokio::process::Command::new(python)
            .arg(root.join("scripts/policy_inventory.py"))
            .arg("--assets")
            .arg(assets)
            .arg("--packages")
            .arg(policies)
            .kill_on_drop(true)
            .output(),
    )
    .await
    .context("policy readiness timed out")??;
    if !output.status.success() {
        bail!(
            "policy readiness failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let inventory: Value = serde_json::from_slice(&output.stdout)?;
    let mut packages = BTreeMap::new();
    let mut rejected = inventory["rejected"]
        .as_array()
        .context("readiness rejected list")?
        .clone();
    for item in inventory["ready"]
        .as_array()
        .context("readiness ready list")?
    {
        let id = item["id"].as_str().context("policy ID missing")?;
        let package = item["package"].as_str().map(PathBuf::from);
        if let Some(ref package) = package {
            let manifest = &item["manifest"];
            let target = catalog
                .bodies
                .iter_mut()
                .find(|b| b.id == body)
                .context("body not found")?;
            if target.policies.contains_key(id) {
                bail!("installed policy ID collides with body catalog: {id}");
            }
            target.policies.insert(
                id.into(),
                PolicyProfile {
                    bindings: vec![backend.into()],
                    observation_contract: manifest["observation_contract"]
                        .as_str()
                        .context("observation contract")?
                        .into(),
                    action_contract: manifest["action_contract"]
                        .as_str()
                        .context("action contract")?
                        .into(),
                    weights: Some(SourceLink {
                        url: manifest["source"].as_str().context("source")?.into(),
                        description: format!("installed ONNX {}", package.display()),
                        revision: Some(manifest["sha256"].as_str().context("checksum")?.into()),
                        license: Some(manifest["license"].as_str().context("license")?.into()),
                    }),
                    normalization: "embedded".into(),
                    rate_hz: 50.0,
                },
            );
        }
        packages.insert(id.to_string(), package);
    }
    catalog.validate()?;
    let mut skills = Vec::new();
    for skill in registry.list() {
        // Runner capability envelope is checked before it is offered to a model.
        let ready = skill.bind(&catalog, body, backend).is_ok_and(|binding| {
            binding.runner == "onnx_policy.v1"
                && binding.resources == ["whole_body"]
                && binding.config == json!({"command_adapter":"microduck.twist.v1"})
                && binding
                    .policy
                    .as_ref()
                    .is_some_and(|id| packages.contains_key(id))
                && skill.max_duration_ms <= 10000
                && skill
                    .parameters
                    .iter()
                    .all(|(name, spec)| match (&spec.kind, name.as_str()) {
                        (ParameterKind::Number { min, max }, "vx") => *min >= -0.4 && *max <= 0.4,
                        (ParameterKind::Number { min, max }, "vy") => *min >= -0.2 && *max <= 0.2,
                        (ParameterKind::Number { min, max }, "yaw_rate") => {
                            *min >= -1.0 && *max <= 1.0
                        }
                        (ParameterKind::Integer { min, max }, "duration_ms") => {
                            *min >= 1 && *max >= 1 && *max as u64 + 1000 <= skill.max_duration_ms
                        }
                        _ => false,
                    })
                && skill
                    .parameters
                    .keys()
                    .all(|p| ["vx", "vy", "yaw_rate", "duration_ms"].contains(&p.as_str()))
                && ["vx", "vy", "yaw_rate", "duration_ms"]
                    .iter()
                    .all(|p| skill.parameters.contains_key(*p))
        });
        if ready {
            skills.push(skill.clone());
        } else {
            rejected.push(json!({"skill_id":skill.id,"reason":"binding/runner/policy/parameter shape is not executable here"}));
        }
    }
    Ok(Readiness {
        catalog,
        packages,
        skills,
        rejected,
    })
}

/// Metadata-only overlay for --validate-skill-call. Never loads ONNX or checks readiness.
pub fn metadata_catalog(mut catalog: BodyCatalog, directory: &Path) -> Result<BodyCatalog> {
    if !directory.exists() {
        return Ok(catalog);
    }
    for entry in std::fs::read_dir(directory)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() || entry.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        let raw: Value = serde_json::from_slice(&std::fs::read(entry.path().join("policy.json"))?)?;
        let receipt: Value =
            serde_json::from_slice(&std::fs::read(entry.path().join(".archon-policy.json"))?)?;
        if raw != receipt || raw["schema_version"] != 1 {
            bail!("invalid installed policy metadata");
        }
        let id = raw["id"].as_str().context("policy ID")?;
        let body = raw["body"].as_str().context("body")?;
        let backend = raw["backend"].as_str().context("backend")?;
        let target = catalog
            .bodies
            .iter_mut()
            .find(|b| b.id == body)
            .context("installed policy body")?;
        if target.policies.contains_key(id) {
            bail!("duplicate installed policy ID");
        }
        target.policies.insert(
            id.into(),
            PolicyProfile {
                bindings: vec![backend.into()],
                observation_contract: raw["observation_contract"]
                    .as_str()
                    .context("observation contract")?
                    .into(),
                action_contract: raw["action_contract"]
                    .as_str()
                    .context("action contract")?
                    .into(),
                weights: Some(SourceLink {
                    url: raw["source"].as_str().context("source")?.into(),
                    description: "installed metadata; runtime readiness not checked".into(),
                    revision: Some(raw["sha256"].as_str().context("checksum")?.into()),
                    license: Some(raw["license"].as_str().context("license")?.into()),
                }),
                normalization: raw["normalization"]
                    .as_str()
                    .context("normalization")?
                    .into(),
                rate_hz: raw["rate_hz"].as_f64().context("rate")?,
            },
        );
    }
    catalog.validate()?;
    Ok(catalog)
}
