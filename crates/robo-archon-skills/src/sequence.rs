//! Bounded data-only composition. Expand and validate every leaf before motion.
use crate::{PreparedSkill, SkillCall, SkillRegistry};
use anyhow::{bail, Result};
use robo_archon_embodied::body::BodyCatalog;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

pub const MAX_STEPS: usize = 16;
pub const MAX_BUDGET_MS: u64 = 120_000;
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillSequence {
    pub schema_version: u32,
    pub timeout_ms: u64,
    pub steps: Vec<SkillCall>,
}
#[derive(Debug, Clone, Serialize)]
pub struct PreparedSequence {
    pub timeout_ms: u64,
    pub steps: Vec<PreparedSkill>,
}
impl SkillRegistry {
    pub fn prepare_sequence(
        &self,
        catalog: &BodyCatalog,
        body: &str,
        backend: &str,
        sequence: &SkillSequence,
    ) -> Result<PreparedSequence> {
        if sequence.schema_version != 1
            || sequence.steps.is_empty()
            || sequence.steps.len() > MAX_STEPS
            || sequence.timeout_ms == 0
            || sequence.timeout_ms > MAX_BUDGET_MS
        {
            bail!("sequence requires schema 1, 1..16 steps and 1..120000 ms budget");
        }
        let mut leaves = Vec::new();
        for call in &sequence.steps {
            self.expand(
                catalog,
                body,
                backend,
                call,
                &mut BTreeSet::new(),
                &mut leaves,
            )?;
        }
        let total = leaves
            .iter()
            .try_fold(0u64, |sum, s| sum.checked_add(s.timeout_ms))
            .ok_or_else(|| anyhow::anyhow!("sequence budget overflow"))?;
        if total > sequence.timeout_ms {
            bail!("sequence budget must cover all child budgets");
        }
        Ok(PreparedSequence {
            timeout_ms: sequence.timeout_ms,
            steps: leaves,
        })
    }
    fn expand(
        &self,
        catalog: &BodyCatalog,
        body: &str,
        backend: &str,
        call: &SkillCall,
        path: &mut BTreeSet<String>,
        leaves: &mut Vec<PreparedSkill>,
    ) -> Result<()> {
        let prepared = self.prepare(catalog, body, backend, call)?;
        if prepared.binding.runner == "sequence.v1" {
            if !path.insert(call.skill_id.clone()) || path.len() > 4 {
                bail!("recursive or deeply nested composition");
            }
            if !prepared
                .parameters
                .as_object()
                .is_some_and(|p| p.is_empty())
                || prepared.binding.policy.is_some()
                || prepared.binding.resources != ["whole_body"]
            {
                bail!("sequence.v1 currently requires fixed steps, no parameters/policy and whole_body");
            }
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Config {
                steps: Vec<SkillCall>,
            }
            let config: Config = serde_json::from_value(prepared.binding.config.clone())?;
            let nested = SkillSequence {
                schema_version: 1,
                timeout_ms: prepared.timeout_ms,
                steps: config.steps,
            };
            // Keep ancestor set across recursion so cycles are detected.
            if nested.steps.is_empty() || nested.steps.len() > MAX_STEPS {
                bail!("invalid child sequence length");
            }
            let start = leaves.len();
            for child in &nested.steps {
                self.expand(catalog, body, backend, child, path, leaves)?;
            }
            let child_budget = leaves[start..]
                .iter()
                .try_fold(0u64, |sum, s| sum.checked_add(s.timeout_ms))
                .ok_or_else(|| anyhow::anyhow!("child budget overflow"))?;
            if child_budget > nested.timeout_ms {
                bail!("composite child budgets exceed its timeout");
            }
            path.remove(&call.skill_id);
        } else {
            leaves.push(prepared);
            if leaves.len() > MAX_STEPS {
                bail!("expanded sequence exceeds 16 steps");
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::Path;
    fn environment() -> (SkillRegistry, BodyCatalog) {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        (
            SkillRegistry::load_dir(&root.join("skills")).unwrap(),
            BodyCatalog::load(&root.join("robots/catalog.json")).unwrap(),
        )
    }
    fn call(id: &str, timeout_ms: u64) -> SkillCall {
        SkillCall {
            skill_id: id.into(),
            parameters: json!({}),
            timeout_ms,
        }
    }
    #[test]
    fn composition_expands_preserving_order_and_rejects_insufficient_budget() {
        let (registry, catalog) = environment();
        let mut sequence = SkillSequence {
            schema_version: 1,
            timeout_ms: 15000,
            steps: vec![call("microduck.patrol", 15000)],
        };
        let plan = registry
            .prepare_sequence(&catalog, "microduck", "mujoco", &sequence)
            .unwrap();
        assert_eq!(plan.steps.len(), 4);
        assert_eq!(plan.steps[1].parameters["yaw_rate"], 1.0);
        assert_eq!(plan.steps[3].skill_id, "microduck.stop");
        sequence.timeout_ms = 14999;
        assert!(registry
            .prepare_sequence(&catalog, "microduck", "mujoco", &sequence)
            .is_err());
        sequence.timeout_ms = 15000;
        sequence.steps.push(call("unknown", 1));
        assert!(registry
            .prepare_sequence(&catalog, "microduck", "mujoco", &sequence)
            .is_err());
    }
    #[test]
    fn rejects_cycles_expansion_limit_and_untrusted_configuration() {
        let (mut registry, catalog) = environment();
        let mut manifest = registry.get("microduck.patrol").unwrap().clone();
        manifest.id = "user.cycle".into();
        manifest.tool_name = "user_cycle".into();
        manifest.bindings[0].config = json!({"steps":[call("user.cycle",15000)]});
        registry.register(manifest.clone()).unwrap();
        let sequence = SkillSequence {
            schema_version: 1,
            timeout_ms: 15000,
            steps: vec![call("user.cycle", 15000)],
        };
        assert!(registry
            .prepare_sequence(&catalog, "microduck", "mujoco", &sequence)
            .is_err());
        manifest.id = "user.too_many".into();
        manifest.tool_name = "user_too_many".into();
        manifest.bindings[0].config = json!({"steps":vec![call("microduck.stop",1000);17]});
        registry.register(manifest).unwrap();
        let sequence = SkillSequence {
            steps: vec![call("user.too_many", 15000)],
            ..sequence
        };
        assert!(registry
            .prepare_sequence(&catalog, "microduck", "mujoco", &sequence)
            .is_err());
    }
}
