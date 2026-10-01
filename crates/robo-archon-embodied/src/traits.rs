use std::path::Path;

use anyhow::Result;
use async_trait::async_trait;

use crate::cancel::CancelToken;
use crate::types::{
    ActionProposal, ExecutionResult, JointCommand, Observation, SafetyVerdict, WorldState,
};

/// Model-agnostic policy: maps world state to an action proposal.
#[async_trait]
pub trait Policy: Send + Sync {
    fn name(&self) -> &str;

    async fn propose(&self, state: &WorldState) -> Result<ActionProposal>;
}

/// Deterministic safety gate — never an LLM.
#[async_trait]
pub trait SafetyGate: Send + Sync {
    async fn check_proposal(&self, proposal: &ActionProposal, state: &WorldState) -> SafetyVerdict;

    async fn check_command(&self, cmd: &JointCommand, state: &WorldState) -> SafetyVerdict;
}

/// Hardware / simulation backend with a stable contract for sim→real migration.
#[async_trait]
pub trait RobotBackend: Send + Sync {
    fn name(&self) -> &str;

    async fn connect(&mut self) -> Result<()>;

    async fn shutdown(&mut self) -> Result<()>;

    async fn read_observation(&self) -> Result<Observation>;

    /// Execute a single high-rate joint command (or a short burst handled by the backend).
    async fn execute_command(&mut self, cmd: &JointCommand, cancel: &CancelToken) -> Result<()>;

    /// Query a binding's kinematic solver; returned targets still pass SafetyGate.
    async fn solve_ik(&self, _target: [f64; 3], _down: bool) -> Result<Vec<f64>> {
        anyhow::bail!("backend does not support Cartesian IK")
    }

    /// Immediate stop — highest priority.
    async fn estop(&mut self) -> Result<()>;

    /// Retarget where the backend writes episode media frames (multi-turn TUI).
    /// Default: no-op. Bridged MuJoCo workers honor this via `set_media_root`.
    async fn set_media_root(&mut self, _root: Option<&Path>) -> Result<()> {
        Ok(())
    }
}

/// Optional helper: run a full interpolated command stream until done or cancelled.
#[async_trait]
pub trait RobotBackendExt: RobotBackend {
    async fn execute_stream(
        &mut self,
        commands: &[JointCommand],
        cancel: &CancelToken,
        step_delay_ms: u64,
    ) -> Result<ExecutionResult> {
        let start = std::time::Instant::now();
        let mut sent = 0usize;
        for cmd in commands {
            if cancel.is_cancelled() {
                return Ok(ExecutionResult::cancelled(
                    sent,
                    start.elapsed().as_millis() as u64,
                    "cancelled",
                ));
            }
            if let Err(error) = self.execute_command(cmd, cancel).await {
                if cancel.is_cancelled() {
                    return Ok(ExecutionResult::cancelled(
                        sent,
                        start.elapsed().as_millis() as u64,
                        "cancelled",
                    ));
                }
                return Ok(ExecutionResult {
                    status: crate::types::ExecutionStatus::Fault,
                    message: format!("{error:#}"),
                    commands_sent: sent,
                    duration_ms: start.elapsed().as_millis() as u64,
                    final_joints: None,
                });
            }
            sent += 1;
            if step_delay_ms > 0 {
                tokio::time::sleep(std::time::Duration::from_millis(step_delay_ms)).await;
            }
        }
        let obs = self.read_observation().await?;
        Ok(ExecutionResult::completed(
            sent,
            start.elapsed().as_millis() as u64,
            obs.joints().clone(),
        ))
    }
}

impl<T: RobotBackend + ?Sized> RobotBackendExt for T {}
