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

impl Readiness {
    pub fn unavailable_reason(&self, skill_id: &str) -> String {
        // Runtime/asset failures reject every policy; preserve that root cause as
        // well as the skill rejection instead of hiding it behind availability.
        self.rejected
            .iter()
            .filter(|item| item["skill_id"] == skill_id || item["id"].is_string())
            .map(|item| {
                let id = item["id"].as_str().unwrap_or(skill_id);
                format!(
                    "{id}: {}",
                    item["reason"].as_str().unwrap_or("unknown reason")
                )
            })
            .collect::<Vec<_>>()
            .join("; ")
    }
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
        if !item["manifest"].is_null() {
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
                        description: format!("installed ONNX {id}"),
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
        let behavior_ready = skill.bind(&catalog, body, backend).is_ok_and(|b| {
            b.runner == "onnx_policy.v1"
                && b.resources == ["whole_body"]
                && b.policy
                    .as_ref()
                    .is_some_and(|id| packages.contains_key(id))
                && skill.parameters.is_empty()
                && {
                    let prepared = robo_archon_skills::PreparedSkill {
                        skill_id: skill.id.clone(),
                        version: skill.version.clone(),
                        parameters: json!({}),
                        timeout_ms: skill.max_duration_ms,
                        binding: b.clone(),
                    };
                    b.policy.as_deref().is_some_and(|id| {
                        robo_archon_sim_bridge::continuous::validate_call(&prepared, id).is_ok()
                    })
                }
        });
        if ready || behavior_ready {
            skills.push(skill.clone());
        } else {
            rejected.push(json!({"skill_id":skill.id,"reason":"binding/runner/policy/parameter shape is not executable here"}));
        }
    }
    for skill in registry
        .list()
        .filter(|s| s.bindings.iter().any(|b| b.runner == "sequence.v1"))
    {
        let call = robo_archon_skills::SkillCall {
            skill_id: skill.id.clone(),
            parameters: json!({}),
            timeout_ms: skill.max_duration_ms,
        };
        let sequence = robo_archon_skills::SkillSequence {
            schema_version: 1,
            timeout_ms: skill.max_duration_ms,
            steps: vec![call],
        };
        if registry
            .prepare_sequence(&catalog, body, backend, &sequence)
            .is_ok_and(|p| {
                p.steps
                    .iter()
                    .all(|leaf| skills.iter().any(|s| s.id == leaf.skill_id))
            })
        {
            rejected.retain(|r| r["skill_id"] != skill.id);
            skills.push(skill.clone());
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
    // Optional official behavior metadata is pinned in Git; do not load ONNX here.
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let receipt = root.join("python/policies/official-microduck/receipt.json");
    if receipt.is_file()
        && !receipt.is_symlink()
        && catalog.bodies.iter().any(|b| b.id == "microduck")
    {
        let lock: Value = serde_json::from_slice(&std::fs::read(
            root.join("robots/microduck-behaviors.lock.json"),
        )?)?;
        let installed: Value = serde_json::from_slice(&std::fs::read(&receipt)?)?;
        if lock != installed {
            bail!("official behavior metadata receipt mismatch");
        }
        let target = catalog
            .bodies
            .iter_mut()
            .find(|b| b.id == "microduck")
            .context("Microduck body")?;
        for (name, entry) in lock["behaviors"]
            .as_object()
            .context("behavior lock entries")?
        {
            let id = format!("official.{name}");
            if target.policies.contains_key(&id) {
                bail!("official policy metadata collides with body catalog");
            }
            target.policies.insert(
                id,
                PolicyProfile {
                    bindings: vec!["mujoco".into()],
                    observation_contract: "microduck.proprio_command.61.v1".into(),
                    action_contract: "microduck.joint_offset.14.rad.v1".into(),
                    normalization: "embedded".into(),
                    rate_hz: 50.0,
                    weights: Some(SourceLink {
                        url: format!(
                            "{}/resolve/{}/{}",
                            lock["repository"].as_str().unwrap_or(""),
                            lock["revision"].as_str().unwrap_or(""),
                            entry["file"].as_str().unwrap_or("")
                        ),
                        description: "pinned metadata; readiness not checked".into(),
                        revision: Some(
                            entry["sha256"]
                                .as_str()
                                .context("behavior checksum")?
                                .into(),
                        ),
                        license: Some("Apache-2.0".into()),
                    }),
                },
            );
        }
    }
    if !directory.exists() {
        catalog.validate()?;
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
