//! Executive: single authority that runs the embodied control loop.

use std::sync::Arc;

use anyhow::{Context, Result};
use robo_archon_embodied::{
    CancelToken, Episode, ExecutionResult, ExecutionStatus, Observation, Policy, RobotBackend,
    RobotBackendExt, SafetyGate, SafetyVerdict, WorldState,
};
use robo_archon_kinetic::Chronos;
use robo_archon_perception::PerceptionBridge;
use tokio::sync::Mutex;

use crate::arbiter::{Arbiter, ArbiterAction};
use crate::event_bus::{EventBus, RuntimeEvent};
use crate::resource_lock::ResourceLocks;

pub struct ExecutiveConfig {
    pub task_id: String,
    pub control_step_ms: u64,
    pub max_proposal_timeout_ms: u64,
    /// Optional fixed episode id (so CLI can pre-create media dirs).
    pub episode_id: Option<String>,
    /// When true, do not call `backend.shutdown()` after a turn (multi-turn TUI / REPL).
    pub keep_backend_alive: bool,
}

impl Default for ExecutiveConfig {
    fn default() -> Self {
        Self {
            task_id: "demo_waypoints".into(),
            control_step_ms: 20, // 50 Hz
            max_proposal_timeout_ms: 30_000,
            episode_id: None,
            keep_backend_alive: false,
        }
    }
}

/// Single execution authority for one robot.
pub struct Executive {
    pub config: ExecutiveConfig,
    pub events: EventBus,
    pub locks: ResourceLocks,
    pub cancel: CancelToken,
    chronos: Chronos,
    arbiter: Arbiter,
}

impl Executive {
    pub fn new(config: ExecutiveConfig, chronos: Chronos) -> Self {
        Self {
            config,
            events: EventBus::new(64),
            locks: ResourceLocks::new(),
            cancel: CancelToken::new(),
            chronos,
            arbiter: Arbiter::new(),
        }
    }

    pub fn request_stop(&self, reason: impl Into<String>) {
        self.events.publish(RuntimeEvent::UserStop {
            reason: reason.into(),
        });
    }

    pub fn request_estop(&self, reason: impl Into<String>) {
        self.events.publish(RuntimeEvent::EStop {
            reason: reason.into(),
        });
    }

    /// Run one embodied cycle: observe → [enrich] → propose → safety → interpolate → execute.
    pub async fn run_once(
        &mut self,
        policy: &dyn Policy,
        safety: &dyn SafetyGate,
        backend: Arc<Mutex<dyn RobotBackend>>,
        task_context: serde_json::Value,
    ) -> Result<(ExecutionResult, Episode)> {
        self.run_once_with_perception(policy, safety, backend, None, task_context)
            .await
    }

    /// Same as `run_once`, optionally enriching proprio with perception modalities.
    pub async fn run_once_with_perception(
        &mut self,
        policy: &dyn Policy,
        safety: &dyn SafetyGate,
        backend: Arc<Mutex<dyn RobotBackend>>,
        mut perception: Option<&mut PerceptionBridge>,
        task_context: serde_json::Value,
    ) -> Result<(ExecutionResult, Episode)> {
        let result = self
            .run_cycle(
                policy,
                safety,
                backend.clone(),
                perception.take(),
                task_context.clone(),
            )
            .await;
        self.locks.release_all("executive");
        let mut b = backend.lock().await;
        if result
            .as_ref()
            .map_or(true, |(r, _)| r.status != ExecutionStatus::Completed)
        {
            let _ = b.estop().await;
        }
        if !self.config.keep_backend_alive {
            let _ = b.shutdown().await;
        }
        match result {
            Ok(outcome) => Ok(outcome),
            Err(error) => {
                let mut episode = Episode::new(
                    self.config
                        .episode_id
                        .clone()
                        .unwrap_or_else(|| format!("ep-{}", robo_archon_embodied::now_us())),
                    &self.config.task_id,
                    b.name(),
                );
                episode.push("task_context", task_context);
                episode.push("fault", serde_json::json!({"error":format!("{error:#}")}));
                episode.finish();
                Ok((
                    ExecutionResult {
                        status: ExecutionStatus::Fault,
                        message: format!("{error:#}"),
                        commands_sent: 0,
                        duration_ms: 0,
                        final_joints: None,
                    },
                    episode,
                ))
            }
        }
    }

    async fn run_cycle(
        &mut self,
        policy: &dyn Policy,
        safety: &dyn SafetyGate,
        backend: Arc<Mutex<dyn RobotBackend>>,
        mut perception: Option<&mut PerceptionBridge>,
        task_context: serde_json::Value,
    ) -> Result<(ExecutionResult, Episode)> {
        self.cancel.reset();
        self.arbiter.reset();

        let backend_name = {
            let b = backend.lock().await;
            b.name().to_string()
        };

        let episode_id = self
            .config
            .episode_id
            .clone()
            .unwrap_or_else(|| format!("ep-{}", robo_archon_embodied::now_us()));
        let mut episode = Episode::new(&episode_id, &self.config.task_id, &backend_name);

        // Spawn event watcher that cancels on preempt events.
        let mut rx = self.events.subscribe();
        let cancel = self.cancel.clone();
        let mut arbiter = Arbiter::new();
        let watch = AbortOnDrop(tokio::spawn(async move {
            while let Ok(ev) = rx.recv().await {
                let action = arbiter.apply(&ev, &cancel);
                if matches!(action, ArbiterAction::EStop | ArbiterAction::CancelTask) {
                    break;
                }
            }
        }));

        // Connect
        {
            let mut b = backend.lock().await;
            b.connect().await.context("backend connect")?;
        }

        let body = {
            let b = backend.lock().await;
            b.read_observation().await.context("read observation")?
        };

        let obs: Observation = if let Some(bridge) = perception.as_mut() {
            let enriched = bridge.enrich(body).await.context("perception enrich")?;
            episode.push(
                "perception",
                serde_json::json!({
                    "camera": bridge.camera_name(),
                    "modality_keys": enriched.modalities.keys().cloned().collect::<Vec<_>>(),
                    "annotations": enriched.annotations.len(),
                }),
            );
            enriched
        } else {
            body
        };

        episode.push(
            "observation",
            serde_json::to_value(&obs).unwrap_or_default(),
        );

        let state = WorldState::from_observation(&obs, task_context);

        let proposal = tokio::select! {
            _ = async { while !self.cancel.is_cancelled() { tokio::time::sleep(std::time::Duration::from_millis(10)).await; } } => { anyhow::bail!("cancelled during proposal"); },
            result = tokio::time::timeout(std::time::Duration::from_millis(self.config.max_proposal_timeout_ms), policy.propose(&state)) => result.context("policy proposal timeout")?.context("policy propose")?,
        };
        episode.push(
            "proposal",
            serde_json::to_value(&proposal).unwrap_or_default(),
        );

        let resources = if proposal.required_resources.is_empty() {
            // Empty resources: skip lock acquisition (e.g. vision miss with no motion).
            if proposal.waypoints.is_empty() {
                vec![]
            } else {
                robo_archon_embodied::ActionProposal::default_resources()
            }
        } else {
            proposal.required_resources.clone()
        };

        if !resources.is_empty() {
            if let Err(e) = self.locks.try_acquire(&resources, "executive") {
                episode.push("lock_denied", serde_json::json!({ "error": e.to_string() }));
                episode.finish();
                watch.0.abort();
                return Ok((ExecutionResult::rejected(e.to_string()), episode));
            }
        }

        let verdict = safety.check_proposal(&proposal, &state).await;
        episode.push(
            "safety_proposal",
            serde_json::to_value(&verdict).unwrap_or_default(),
        );
        if let SafetyVerdict::Deny { reason } = verdict {
            self.locks.release_all("executive");
            episode.finish();
            watch.0.abort();
            self.events.publish(RuntimeEvent::SafetyFault {
                reason: reason.clone(),
            });
            return Ok((ExecutionResult::rejected(reason), episode));
        }

        let commands = self.chronos.interpolate_proposal(&proposal);
        episode.push(
            "chronos",
            serde_json::json!({ "commands": commands.len(), "rate_hz": self.chronos.rate_hz }),
        );

        // Per-command safety sample (first, mid, last) for MVP.
        for idx in sample_indices(commands.len()) {
            if let Some(cmd) = commands.get(idx) {
                let v = safety.check_command(cmd, &state).await;
                if let SafetyVerdict::Deny { reason } = v {
                    episode.push(
                        "safety_command_deny",
                        serde_json::json!({ "index": idx, "reason": reason }),
                    );
                    self.locks.release_all("executive");
                    episode.finish();
                    watch.0.abort();
                    return Ok((ExecutionResult::rejected(reason), episode));
                }
            }
        }

        let result = {
            let mut b = backend.lock().await;
            let stream_result = b
                .execute_stream(&commands, &self.cancel, self.config.control_step_ms)
                .await
                .context("execute stream")?;

            if self.cancel.is_cancelled() {
                let _ = b.estop().await;
                episode.push("estop_or_cancel", serde_json::json!({ "cancelled": true }));
            }

            stream_result
        };

        episode.push(
            "execution_result",
            serde_json::to_value(&result).unwrap_or_default(),
        );

        if matches!(result.status, ExecutionStatus::Completed) {
            self.events.publish(RuntimeEvent::TaskCompleted {
                task_id: self.config.task_id.clone(),
            });
        }

        self.locks.release_all("executive");
        episode.finish();
        watch.0.abort();
        Ok((result, episode))
    }
}

fn sample_indices(n: usize) -> Vec<usize> {
    if n == 0 {
        return Vec::new();
    }
    if n == 1 {
        return vec![0];
    }
    vec![0, n / 2, n - 1]
}

struct AbortOnDrop(tokio::task::JoinHandle<()>);
impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    use async_trait::async_trait;
    use robo_archon_embodied::{
        ActionProposal, JointCommand, JointState, ProprioState, ResourceKind,
    };
    use std::sync::atomic::{AtomicUsize, Ordering};
    struct Backend {
        stops: Arc<AtomicUsize>,
        closes: Arc<AtomicUsize>,
        fail: bool,
    }
    #[async_trait]
    impl RobotBackend for Backend {
        fn name(&self) -> &str {
            "lifecycle_fixture"
        }
        async fn connect(&mut self) -> Result<()> {
            Ok(())
        }
        async fn shutdown(&mut self) -> Result<()> {
            self.closes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
        async fn estop(&mut self) -> Result<()> {
            self.stops.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
        async fn read_observation(&self) -> Result<Observation> {
            Ok(Observation::from_proprio(ProprioState::new(
                JointState::new(vec!["joint".into()], vec![0.]),
                1.,
            )))
        }
        async fn execute_command(
            &mut self,
            _cmd: &JointCommand,
            _cancel: &CancelToken,
        ) -> Result<()> {
            if self.fail {
                anyhow::bail!("actuator fault");
            }
            Ok(())
        }
    }
    struct PolicyFixture {
        delay: u64,
    }
    #[async_trait]
    impl Policy for PolicyFixture {
        fn name(&self) -> &str {
            "fixture"
        }
        async fn propose(&self, _state: &WorldState) -> Result<ActionProposal> {
            tokio::time::sleep(std::time::Duration::from_millis(self.delay)).await;
            Ok(ActionProposal {
                id: "fixture".into(),
                stamp_us: 0,
                source: "fixture".into(),
                confidence: 1.,
                required_resources: vec![ResourceKind::Arm],
                metadata: serde_json::json!({}),
                waypoints: vec![robo_archon_embodied::JointWaypoint {
                    t_sec: 0.,
                    positions: vec![0.],
                    gripper_open: Some(1.),
                }],
            })
        }
    }
    struct Allow;
    #[async_trait]
    impl SafetyGate for Allow {
        async fn check_proposal(&self, _p: &ActionProposal, _s: &WorldState) -> SafetyVerdict {
            SafetyVerdict::Allow
        }
        async fn check_command(&self, _c: &JointCommand, _s: &WorldState) -> SafetyVerdict {
            SafetyVerdict::Allow
        }
    }
    #[tokio::test]
    async fn timeout_and_actuator_fault_release_locks_stop_and_close() {
        for (delay, fail) in [(100, true), (0, true)] {
            let stops = Arc::new(AtomicUsize::new(0));
            let closes = Arc::new(AtomicUsize::new(0));
            let backend: Arc<Mutex<dyn RobotBackend>> = Arc::new(Mutex::new(Backend {
                stops: stops.clone(),
                closes: closes.clone(),
                fail,
            }));
            let mut executive = Executive::new(
                ExecutiveConfig {
                    max_proposal_timeout_ms: 20,
                    ..Default::default()
                },
                Chronos::new(50., vec!["joint".into()]),
            );
            let (result, episode) = executive
                .run_once(
                    &PolicyFixture { delay },
                    &Allow,
                    backend,
                    serde_json::json!({}),
                )
                .await
                .unwrap();
            assert_eq!(result.status, ExecutionStatus::Fault);
            assert!(episode
                .events
                .iter()
                .any(|e| e.kind == "fault" || e.kind == "execution_result"));
            assert!(episode.ended_us.is_some());
            assert_eq!(stops.load(Ordering::SeqCst), 1);
            assert_eq!(closes.load(Ordering::SeqCst), 1);
            executive
                .locks
                .try_acquire(&[ResourceKind::Arm], "next_task")
                .unwrap();
        }
    }
}
