//! Simulator-independent body packages. Metadata does not authorize execution.
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::Path};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BodyCatalog {
    pub schema_version: u32,
    pub bodies: Vec<BodyPackage>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BodyPackage {
    pub id: String,
    pub name: String,
    pub revision: String,
    pub definition: RobotDefinition,
    pub sources: Vec<SourceLink>,
    pub manufacturing_links: Vec<SourceLink>,
    pub bindings: BTreeMap<String, PlatformBinding>,
    pub policies: BTreeMap<String, PolicyProfile>,
    pub tasks: BTreeMap<String, TaskProfile>,
    pub devices: BTreeMap<String, DeviceProfile>,
    pub validations: Vec<ValidationRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RobotDefinition {
    pub category: String,
    pub capabilities: Vec<String>,
    pub coordinate_convention: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceLink {
    pub url: String,
    pub description: String,
    pub revision: Option<String>,
    pub license: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BindingStatus {
    Planned,
    Preview,
    Controlled,
    TaskVerified,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlatformBinding {
    pub status: BindingStatus,
    pub model_format: Option<String>,
    pub model_spec: Option<String>,
    pub controller: Option<String>,
    pub observation_contract: Option<String>,
    pub action_contract: Option<String>,
    pub joint_mapping: BTreeMap<String, String>,
    pub notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PolicyProfile {
    pub bindings: Vec<String>,
    pub observation_contract: String,
    pub action_contract: String,
    pub weights: Option<SourceLink>,
    pub normalization: String,
    pub rate_hz: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskProfile {
    pub goal: String,
    pub success_condition: String,
    pub scenes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceProfile {
    pub binding: String,
    pub calibration_ref: String,
    pub stop_behavior: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValidationRecord {
    pub binding: String,
    pub policy: String,
    pub task: String,
    pub evidence: String,
}

impl BodyCatalog {
    pub fn load(path: &Path) -> Result<Self> {
        let catalog: Self = serde_json::from_slice(
            &std::fs::read(path)
                .with_context(|| format!("read body catalog {}", path.display()))?,
        )?;
        catalog.validate()?;
        Ok(catalog)
    }

    pub fn body(&self, id: &str) -> Result<&BodyPackage> {
        self.bodies
            .iter()
            .find(|body| body.id == id)
            .with_context(|| format!("unknown robot '{id}'; use --list-robots"))
    }

    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1 {
            bail!("unsupported body schema {}", self.schema_version);
        }
        let mut ids = std::collections::BTreeSet::new();
        for body in &self.bodies {
            if body.id.is_empty()
                || body.name.is_empty()
                || body.revision.is_empty()
                || !ids.insert(&body.id)
            {
                bail!("empty identity or duplicate body id '{}'", body.id);
            }
            body.validate()
                .with_context(|| format!("body {}", body.id))?;
        }
        Ok(())
    }
}

impl BodyPackage {
    fn validate(&self) -> Result<()> {
        for (id, binding) in &self.bindings {
            if id.is_empty() {
                bail!("empty binding id");
            }
            if binding.status != BindingStatus::Planned
                && binding.model_spec.as_deref().unwrap_or("").is_empty()
            {
                bail!("binding {id} requires a model reference");
            }
            if matches!(
                binding.status,
                BindingStatus::Controlled | BindingStatus::TaskVerified
            ) && [
                binding.controller.as_deref(),
                binding.observation_contract.as_deref(),
                binding.action_contract.as_deref(),
            ]
            .iter()
            .any(|v| v.unwrap_or("").is_empty())
            {
                bail!("controlled binding {id} requires controller and contracts");
            }
            if binding.status == BindingStatus::TaskVerified
                && !self.validations.iter().any(|v| &v.binding == id)
            {
                bail!("task_verified binding {id} requires evidence");
            }
        }
        for (id, policy) in &self.policies {
            if !policy.rate_hz.is_finite() || policy.rate_hz <= 0.0 || policy.bindings.is_empty() {
                bail!("policy {id} requires positive rate and bindings");
            }
            for binding in &policy.bindings {
                self.check_policy(binding, id)?;
            }
        }
        for task in self.tasks.values() {
            for binding in task.scenes.keys() {
                if !self.bindings.contains_key(binding) {
                    bail!("unknown task binding {binding}");
                }
            }
        }
        for device in self.devices.values() {
            if !self.bindings.contains_key(&device.binding)
                || device.calibration_ref.is_empty()
                || device.stop_behavior.is_empty()
            {
                bail!("device requires known binding, calibration and stop behavior");
            }
        }
        for record in &self.validations {
            self.check_policy(&record.binding, &record.policy)?;
            let task = self
                .tasks
                .get(&record.task)
                .context("validation references unknown task")?;
            if record.evidence.is_empty() || !task.scenes.contains_key(&record.binding) {
                bail!("validation requires evidence and matching scene");
            }
        }
        Ok(())
    }

    /// Exact contract IDs include units, order and normalization; names alone are insufficient.
    pub fn check_policy(&self, binding: &str, policy: &str) -> Result<()> {
        let b = self
            .bindings
            .get(binding)
            .with_context(|| format!("unknown binding {binding}"))?;
        let p = self
            .policies
            .get(policy)
            .with_context(|| format!("unknown policy {policy}"))?;
        if !matches!(
            b.status,
            BindingStatus::Controlled | BindingStatus::TaskVerified
        ) {
            bail!("binding {binding} is not controlled");
        }
        if !p.bindings.iter().any(|id| id == binding)
            || b.observation_contract.as_deref() != Some(p.observation_contract.as_str())
            || b.action_contract.as_deref() != Some(p.action_contract.as_str())
        {
            bail!("policy {policy} incompatible with binding {binding}");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn catalog() -> BodyCatalog {
        serde_json::from_str(include_str!("../../../robots/catalog.json")).unwrap()
    }
    fn controlled() -> BodyCatalog {
        let mut c = catalog();
        let body = &mut c.bodies[0];
        let b = body.bindings.get_mut("mujoco").unwrap();
        b.status = BindingStatus::Controlled;
        b.model_spec = Some("fixture.xml".into());
        b.controller = Some("fixture_pd".into());
        b.observation_contract = Some("fixture.obs.v1".into());
        b.action_contract = Some("fixture.action.v1".into());
        body.policies.insert(
            "fixture".into(),
            PolicyProfile {
                bindings: vec!["mujoco".into()],
                observation_contract: "fixture.obs.v1".into(),
                action_contract: "fixture.action.v1".into(),
                weights: None,
                normalization: "none".into(),
                rate_hz: 50.0,
            },
        );
        c
    }
    #[test]
    fn committed_catalog_valid_without_manufacturing() {
        let mut c = catalog();
        for b in &mut c.bodies {
            b.manufacturing_links.clear();
        }
        c.validate().unwrap();
        assert_eq!(c.bodies.len(), 4);
        assert!(c.body("unknown").is_err());
    }
    #[test]
    fn rejects_unknown_fields_and_versions_and_duplicate_ids() {
        let mut raw = serde_json::to_value(catalog()).unwrap();
        raw["unexpected"] = true.into();
        assert!(serde_json::from_value::<BodyCatalog>(raw).is_err());
        let mut c = catalog();
        c.schema_version = 2;
        assert!(c.validate().is_err());
        c.schema_version = 1;
        c.bodies.push(c.bodies[0].clone());
        assert!(c.validate().is_err());
    }
    #[test]
    fn compatibility_requires_controlled_binding_and_exact_contracts() {
        assert!(catalog().bodies[0]
            .check_policy("mujoco", "missing")
            .is_err());
        let mut c = controlled();
        c.validate().unwrap();
        c.bodies[0]
            .policies
            .get_mut("fixture")
            .unwrap()
            .action_contract = "other.v1".into();
        assert!(c.validate().is_err());
    }
    #[test]
    fn verified_status_requires_compatible_task_evidence() {
        let mut c = controlled();
        c.bodies[0].bindings.get_mut("mujoco").unwrap().status = BindingStatus::TaskVerified;
        assert!(c.validate().is_err());
        c.bodies[0].tasks.insert(
            "pick".into(),
            TaskProfile {
                goal: "lift".into(),
                success_condition: "height > threshold".into(),
                scenes: BTreeMap::from([("mujoco".into(), "fixture.scene".into())]),
            },
        );
        c.bodies[0].validations.push(ValidationRecord {
            binding: "mujoco".into(),
            policy: "fixture".into(),
            task: "pick".into(),
            evidence: "fixture report".into(),
        });
        c.validate().unwrap();
        c.bodies[0].validations[0].task = "missing".into();
        assert!(c.validate().is_err());
    }
    #[test]
    fn rejects_unknown_scene_device_and_invalid_rate() {
        let mut c = controlled();
        c.bodies[0].policies.get_mut("fixture").unwrap().rate_hz = f64::NAN;
        assert!(c.validate().is_err());
        c = controlled();
        c.bodies[0].tasks.insert(
            "task".into(),
            TaskProfile {
                goal: "test".into(),
                success_condition: "test".into(),
                scenes: BTreeMap::from([("missing".into(), "scene".into())]),
            },
        );
        assert!(c.validate().is_err());
        c = controlled();
        c.bodies[0].devices.insert(
            "device".into(),
            DeviceProfile {
                binding: "missing".into(),
                calibration_ref: "calibration.json".into(),
                stop_behavior: "hold".into(),
            },
        );
        assert!(c.validate().is_err());
    }
}
