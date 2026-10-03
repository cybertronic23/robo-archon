//! Skills describe callable capabilities, not executable plugins.
//! Loading or preparing a manifest never runs a policy, downloads assets, or grants execution authority.
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::{bail, Context, Result};
use robo_archon_embodied::body::{BindingStatus, BodyCatalog};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillManifest {
    pub schema_version: u32,
    pub id: String,
    pub version: String,
    pub tool_name: String,
    pub description: String,
    pub parameters: BTreeMap<String, ParameterSpec>,
    pub max_duration_ms: u64,
    /// Entry conditions are documentation until a runner implements the corresponding checks.
    pub entry_conditions: Vec<String>,
    pub success_description: String,
    pub bindings: Vec<SkillBinding>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillBinding {
    pub body: String,
    pub backend: String,
    pub runner: String,
    pub policy: Option<String>,
    /// Immutable adapter configuration, not user/LLM-supplied call parameters.
    pub config: Value,
    /// Body-relative names; the future runtime must qualify these with the robot instance.
    pub resources: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ParameterSpec {
    pub description: String,
    pub required: bool,
    pub default: Option<Value>,
    pub kind: ParameterKind,
}

/// Deliberately small, validated vocabulary; not an arbitrary JSON Schema interpreter.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ParameterKind {
    Number { min: f64, max: f64 },
    Integer { min: i64, max: i64 },
    Boolean,
    String { choices: Vec<String> },
}

impl ParameterSpec {
    fn validate(&self) -> Result<()> {
        match &self.kind {
            ParameterKind::Number { min, max } => {
                if !min.is_finite() || !max.is_finite() || min > max {
                    bail!("invalid finite numeric bounds");
                }
            }
            ParameterKind::Integer { min, max } if min > max => bail!("invalid integer bounds"),
            ParameterKind::String { choices } => {
                let unique: BTreeSet<_> = choices.iter().collect();
                if choices.is_empty() || unique.len() != choices.len() {
                    bail!("string choices must be nonempty and unique");
                }
            }
            _ => {}
        }
        if self.description.trim().is_empty() {
            bail!("parameter description is empty");
        }
        if let Some(default) = &self.default {
            self.check(default).context("invalid default")?;
        }
        Ok(())
    }

    fn check(&self, value: &Value) -> Result<()> {
        let valid = match &self.kind {
            ParameterKind::Number { min, max } => value
                .as_f64()
                .is_some_and(|n| n.is_finite() && n >= *min && n <= *max),
            ParameterKind::Integer { min, max } => {
                value.as_i64().is_some_and(|n| n >= *min && n <= *max)
            }
            ParameterKind::Boolean => value.is_boolean(),
            ParameterKind::String { choices } => value
                .as_str()
                .is_some_and(|s| choices.iter().any(|c| c == s)),
        };
        if !valid {
            bail!("parameter has incorrect type, range or choice");
        }
        Ok(())
    }

    fn schema(&self) -> Value {
        let mut schema = match &self.kind {
            ParameterKind::Number { min, max } => {
                json!({"type":"number", "minimum":min, "maximum":max})
            }
            ParameterKind::Integer { min, max } => {
                json!({"type":"integer", "minimum":min, "maximum":max})
            }
            ParameterKind::Boolean => json!({"type":"boolean"}),
            ParameterKind::String { choices } => json!({"type":"string", "enum":choices}),
        };
        schema["description"] = json!(self.description);
        if let Some(default) = &self.default {
            schema["default"] = default.clone();
        }
        schema
    }
}

fn identifier(s: &str, dots: bool) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || (dots && b == b'.'))
}

impl SkillManifest {
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1
            || !identifier(&self.id, true)
            || !identifier(&self.tool_name, false)
            || self.tool_name == "archon_sequence"
        {
            bail!("unsupported schema or invalid skill/tool name");
        }
        if self.version.trim().is_empty()
            || self.description.trim().is_empty()
            || self.success_description.trim().is_empty()
        {
            bail!("skill version, description and success description are required");
        }
        if self.max_duration_ms == 0
            || self.max_duration_ms > i64::MAX as u64
            || self.bindings.is_empty()
        {
            bail!("skill needs a positive bounded duration and at least one binding");
        }
        for (name, parameter) in &self.parameters {
            if !identifier(name, false) {
                bail!("invalid parameter name {name}");
            }
            parameter
                .validate()
                .with_context(|| format!("parameter {name}"))?;
        }
        let mut targets = BTreeSet::new();
        for binding in &self.bindings {
            if !identifier(&binding.body, true)
                || !identifier(&binding.backend, true)
                || !identifier(&binding.runner, true)
                || binding
                    .policy
                    .as_ref()
                    .is_some_and(|p| !identifier(p, true))
                || !binding.config.is_object()
            {
                bail!("invalid binding identifiers or config");
            }
            if !targets.insert((&binding.body, &binding.backend)) {
                bail!("duplicate body/backend binding");
            }
            let resources: BTreeSet<_> = binding.resources.iter().collect();
            if resources.is_empty()
                || resources.len() != binding.resources.len()
                || resources.iter().any(|r| !identifier(r, true))
            {
                bail!("binding resources must be nonempty, unique identifiers");
            }
        }
        Ok(())
    }

    pub fn bind<'a>(
        &'a self,
        catalog: &BodyCatalog,
        body: &str,
        backend: &str,
    ) -> Result<&'a SkillBinding> {
        let binding = self
            .bindings
            .iter()
            .find(|b| b.body == body && b.backend == backend)
            .with_context(|| format!("skill {} has no binding for {body}/{backend}", self.id))?;
        let body = catalog.body(body)?;
        let platform = body.bindings.get(backend).context("body backend missing")?;
        if !matches!(
            platform.status,
            BindingStatus::Controlled | BindingStatus::TaskVerified
        ) {
            bail!("body backend is not controlled");
        }
        if let Some(policy) = &binding.policy {
            body.check_policy(backend, policy)?;
        }
        Ok(binding)
    }

    pub fn tool_definition(&self) -> SkillToolDefinition {
        let properties: Map<String, Value> = self
            .parameters
            .iter()
            .map(|(name, p)| (name.clone(), p.schema()))
            .collect();
        let required: Vec<_> = self
            .parameters
            .iter()
            .filter(|(_, p)| p.required && p.default.is_none())
            .map(|(name, _)| name)
            .collect();
        SkillToolDefinition {
            name: self.tool_name.clone(),
            description: self.description.clone(),
            input_schema: json!({"type":"object", "properties":properties, "required":required, "additionalProperties":false}),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillCall {
    pub skill_id: String,
    pub parameters: Value,
    pub timeout_ms: u64,
}

/// A validated proposal. It still requires runner validation, runtime arbitration and live preconditions.
#[derive(Debug, Clone, Serialize)]
pub struct PreparedSkill {
    pub skill_id: String,
    pub version: String,
    pub parameters: Value,
    pub timeout_ms: u64,
    pub binding: SkillBinding,
}

/// Provider-neutral schema; provider adapters choose the API-specific wrapper.
#[derive(Debug, Clone, Serialize)]
pub struct SkillToolDefinition {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
}

#[derive(Debug, Default)]
pub struct SkillRegistry {
    skills: BTreeMap<String, SkillManifest>,
    tool_names: BTreeSet<String>,
}

impl SkillRegistry {
    pub fn load_dir(root: &Path) -> Result<Self> {
        let mut paths = Vec::new();
        for entry in std::fs::read_dir(root)
            .with_context(|| format!("read skills directory {}", root.display()))?
        {
            let entry = entry?;
            if entry.file_type()?.is_dir() && entry.path().join("skill.json").is_file() {
                paths.push(entry.path().join("skill.json"));
            }
        }
        paths.sort();
        let mut registry = Self::default();
        for path in paths {
            let bytes = std::fs::read(&path)?;
            let manifest: SkillManifest = serde_json::from_slice(&bytes)
                .with_context(|| format!("parse {}", path.display()))?;
            registry
                .register(manifest)
                .with_context(|| format!("register {}", path.display()))?;
        }
        Ok(registry)
    }

    pub fn register(&mut self, manifest: SkillManifest) -> Result<()> {
        manifest.validate()?;
        if self.skills.contains_key(&manifest.id) || self.tool_names.contains(&manifest.tool_name) {
            bail!("duplicate skill id or tool name; registration never silently replaces a skill");
        }
        self.tool_names.insert(manifest.tool_name.clone());
        self.skills.insert(manifest.id.clone(), manifest);
        Ok(())
    }

    pub fn list(&self) -> impl Iterator<Item = &SkillManifest> {
        self.skills.values()
    }

    pub fn get(&self, id: &str) -> Result<&SkillManifest> {
        self.skills
            .get(id)
            .with_context(|| format!("unknown skill {id}"))
    }

    /// Only body/platform compatibility is checked here. No executable tool availability is claimed.
    pub fn compatible_definitions(
        &self,
        catalog: &BodyCatalog,
        body: &str,
        backend: &str,
    ) -> Vec<SkillToolDefinition> {
        self.list()
            .filter(|skill| skill.bind(catalog, body, backend).is_ok())
            .map(SkillManifest::tool_definition)
            .collect()
    }

    pub fn prepare(
        &self,
        catalog: &BodyCatalog,
        body: &str,
        backend: &str,
        call: &SkillCall,
    ) -> Result<PreparedSkill> {
        let skill = self.get(&call.skill_id)?;
        if call.timeout_ms == 0 || call.timeout_ms > skill.max_duration_ms {
            bail!("timeout must be within 1..={} ms", skill.max_duration_ms);
        }
        let binding = skill.bind(catalog, body, backend)?;
        let mut parameters = call
            .parameters
            .as_object()
            .context("parameters must be an object")?
            .clone();
        for name in parameters.keys() {
            if !skill.parameters.contains_key(name) {
                bail!("unknown parameter {name}");
            }
        }
        for (name, spec) in &skill.parameters {
            if !parameters.contains_key(name) {
                if let Some(default) = &spec.default {
                    parameters.insert(name.clone(), default.clone());
                } else if spec.required {
                    bail!("missing parameter {name}");
                }
            }
            if let Some(value) = parameters.get(name) {
                spec.check(value)
                    .with_context(|| format!("parameter {name}"))?;
            }
        }
        Ok(PreparedSkill {
            skill_id: skill.id.clone(),
            version: skill.version.clone(),
            parameters: Value::Object(parameters),
            timeout_ms: call.timeout_ms,
            binding: binding.clone(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
    }
    fn catalog() -> BodyCatalog {
        BodyCatalog::load(&root().join("robots/catalog.json")).unwrap()
    }
    fn manifest() -> SkillManifest {
        serde_json::from_str(include_str!("../../../skills/franka-home/skill.json")).unwrap()
    }
    fn parameters() -> BTreeMap<String, ParameterSpec> {
        serde_json::from_value(json!({
            "speed": {"description":"m/s", "required":true, "default":0.1, "kind":{"type":"number", "min":0.0, "max":0.3}},
            "repeats": {"description":"count", "required":true, "default":null, "kind":{"type":"integer", "min":1, "max":3}},
            "mode": {"description":"mode", "required":false, "default":"slow", "kind":{"type":"string", "choices":["slow","fast"]}},
            "record": {"description":"record", "required":false, "default":false, "kind":{"type":"boolean"}}
        })).unwrap()
    }
    fn registry_with_parameters() -> SkillRegistry {
        let mut skill = manifest();
        skill.parameters = parameters();
        let mut registry = SkillRegistry::default();
        registry.register(skill).unwrap();
        registry
    }
    fn call(parameters: Value) -> SkillCall {
        SkillCall {
            skill_id: "franka.home".into(),
            parameters,
            timeout_ms: 1000,
        }
    }

    #[test]
    fn user_package_discovered_and_body_bindings_checked() {
        let registry = SkillRegistry::load_dir(&root().join("skills")).unwrap();
        assert_eq!(registry.list().count(), 11);
        assert_eq!(
            registry
                .compatible_definitions(&catalog(), "franka_panda", "mujoco")
                .len(),
            1
        );
        assert_eq!(
            registry
                .compatible_definitions(&catalog(), "microduck", "mujoco")
                .len(),
            5
        );
        let request = SkillCall {
            skill_id: "microduck.walk".into(),
            parameters: json!({"vx":0.3}),
            timeout_ms: 5000,
        };
        assert!(registry
            .prepare(&catalog(), "microduck", "mujoco", &request)
            .is_ok());
        assert!(registry
            .prepare(&catalog(), "so101", "mujoco", &call(json!({})))
            .is_err());
        assert!(registry
            .prepare(&catalog(), "franka_panda", "isaac", &call(json!({})))
            .is_err());
    }

    #[test]
    fn defaults_and_adapter_config_are_separate() {
        let registry = registry_with_parameters();
        let prepared = registry
            .prepare(
                &catalog(),
                "franka_panda",
                "mujoco",
                &call(json!({"repeats":2})),
            )
            .unwrap();
        assert_eq!(
            prepared.parameters,
            json!({"speed":0.1,"repeats":2,"mode":"slow","record":false})
        );
        assert_eq!(prepared.binding.config, json!({"primitive":"home"}));
        let schema = registry
            .get("franka.home")
            .unwrap()
            .tool_definition()
            .input_schema;
        assert_eq!(schema["required"], json!(["repeats"]));
        assert_eq!(schema["additionalProperties"], false);
    }

    #[test]
    fn malformed_or_out_of_range_calls_rejected_without_coercion() {
        let registry = registry_with_parameters();
        for parameters in [
            json!(null),
            json!({}),
            json!({"repeats":1,"speed":0.4}),
            json!({"repeats":1.5}),
            json!({"repeats":"2"}),
            json!({"repeats":1,"mode":"unknown"}),
            json!({"repeats":1,"record":1}),
            json!({"repeats":1,"runner":"shell"}),
        ] {
            assert!(
                registry
                    .prepare(
                        &catalog(),
                        "franka_panda",
                        "mujoco",
                        &call(parameters.clone())
                    )
                    .is_err(),
                "{parameters}"
            );
        }
        for timeout_ms in [0, 5001] {
            let mut request = call(json!({"repeats":1}));
            request.timeout_ms = timeout_ms;
            assert!(registry
                .prepare(&catalog(), "franka_panda", "mujoco", &request)
                .is_err());
        }
    }

    #[test]
    fn duplicate_registration_and_unknown_fields_fail() {
        let mut registry = SkillRegistry::default();
        registry.register(manifest()).unwrap();
        assert!(registry.register(manifest()).is_err());
        let mut collision = manifest();
        collision.id = "other.home".into();
        assert!(registry.register(collision).is_err());
        assert_eq!(registry.list().count(), 1);
        let mut value = serde_json::to_value(manifest()).unwrap();
        value["execute_shell"] = json!("anything");
        assert!(serde_json::from_value::<SkillManifest>(value).is_err());
        assert!(serde_json::from_value::<SkillCall>(
            json!({"skill_id":"franka.home","parameters":{},"timeout_ms":1000,"runner":"shell"})
        )
        .is_err());
    }

    #[test]
    fn invalid_bounds_defaults_and_target_collisions_fail_at_registration() {
        let mut skill = manifest();
        skill.parameters = parameters();
        skill.parameters.get_mut("speed").unwrap().default = Some(json!(0.5));
        assert!(skill.validate().is_err());
        skill.parameters = parameters();
        skill.parameters.get_mut("speed").unwrap().kind =
            ParameterKind::Number { min: 1.0, max: 0.0 };
        assert!(skill.validate().is_err());
        skill.parameters = parameters();
        skill.bindings.push(skill.bindings[0].clone());
        assert!(skill.validate().is_err());
        skill.bindings.pop();
        skill.tool_name = "robot.with.dots".into();
        assert!(skill.validate().is_err());
    }

    #[test]
    fn policy_contract_mismatch_blocks_preparation() {
        let mut skill = manifest();
        skill.bindings[0].policy = Some("nonexistent_policy".into());
        let mut registry = SkillRegistry::default();
        registry.register(skill).unwrap();
        assert!(registry
            .prepare(&catalog(), "franka_panda", "mujoco", &call(json!({})))
            .is_err());
    }
}

pub mod sequence;
pub use sequence::{PreparedSequence, SkillSequence};

pub mod runner;
pub use runner::{RunnerProgress, RunnerRegistry, SkillResult, SkillRunner, SkillStatus};
