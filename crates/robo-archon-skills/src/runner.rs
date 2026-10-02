//! Trusted execution adapters. Registry metadata alone never creates a runner.
use crate::PreparedSkill;
use anyhow::Result;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SkillStatus {
    Succeeded,
    Failed,
    Cancelled,
    TimedOut,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillResult {
    pub skill_id: String,
    pub version: String,
    pub status: SkillStatus,
    pub reason: String,
    pub elapsed_ms: u64,
    pub observation: Value,
}

#[derive(Debug)]
pub struct RunnerProgress {
    pub finished: bool,
    pub observation: Value,
}

/// All I/O methods must be bounded; worker implementations additionally own a command lease.
#[async_trait]
pub trait SkillRunner: Send {
    fn id(&self) -> &str;
    fn validate(&self, skill: &PreparedSkill) -> Result<()>;
    async fn start(&mut self, skill: &PreparedSkill) -> Result<()>;
    async fn poll(&mut self) -> Result<RunnerProgress>;
    async fn stop(&mut self) -> Result<Value>;
    async fn shutdown(&mut self) -> Result<()> {
        Ok(())
    }
}

/// Host-registered trusted adapters. A manifest cannot instantiate executable code.
#[derive(Default)]
pub struct RunnerRegistry {
    runners: std::collections::BTreeMap<String, Box<dyn SkillRunner>>,
}
impl RunnerRegistry {
    pub fn register(&mut self, runner: Box<dyn SkillRunner>) -> Result<()> {
        let id = runner.id().to_string();
        if self.runners.contains_key(&id) {
            anyhow::bail!("runner {id} already registered");
        }
        self.runners.insert(id, runner);
        Ok(())
    }
    pub fn get_mut(&mut self, id: &str) -> Result<&mut (dyn SkillRunner + '_)> {
        match self.runners.get_mut(id) {
            Some(runner) => Ok(runner.as_mut()),
            None => anyhow::bail!("runner {id} is not installed"),
        }
    }
}
