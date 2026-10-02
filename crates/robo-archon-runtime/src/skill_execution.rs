//! Continuous skills execute through the same per-robot Executive authority as trajectories.
use crate::Executive;
use anyhow::{bail, Result};
use robo_archon_embodied::ResourceKind;
use robo_archon_skills::{PreparedSkill, SkillResult, SkillRunner, SkillStatus};
use serde_json::Value;
use std::time::{Duration, Instant};

impl Executive {
    pub async fn run_skill(
        &mut self,
        skill: &PreparedSkill,
        runner: &mut dyn SkillRunner,
    ) -> Result<SkillResult> {
        if runner.id() != skill.binding.runner {
            bail!("runner ID mismatch");
        }
        runner.validate(skill)?;
        // Conservative mapping to existing shared locks. Unknown resources never bypass arbitration.
        let mut resources = Vec::new();
        for name in &skill.binding.resources {
            match name.as_str() {
                "whole_body" => {
                    resources.extend([ResourceKind::Base, ResourceKind::Arm, ResourceKind::Gripper])
                }
                "base" => resources.push(ResourceKind::Base),
                "arm" => resources.push(ResourceKind::Arm),
                "gripper" => resources.push(ResourceKind::Gripper),
                _ => bail!("resource {name} has no runtime mapping"),
            }
        }
        self.locks.try_acquire(&resources, "skill")?;
        let started = Instant::now();
        let mut events = self.events.subscribe();
        let budget = Duration::from_millis(skill.timeout_ms);
        let mut observation = Value::Null;
        let mut status = SkillStatus::Succeeded;
        let mut reason = "duration_complete".to_string();
        let execution: Result<()> = async {
            if self.cancel.is_cancelled() {
                status = SkillStatus::Cancelled;
                reason = "cancelled before start".into();
                return Ok(());
            }
            tokio::time::timeout(budget.min(Duration::from_secs(2)), runner.start(skill)).await??;
            loop {
                if started.elapsed() >= budget {
                    status = SkillStatus::TimedOut;
                    reason = "execution deadline".into();
                    break;
                }
                if self.cancel.is_cancelled() {
                    status = SkillStatus::Cancelled;
                    reason = "cancelled".into();
                    break;
                }
                while let Ok(event) = events.try_recv() {
                    if event.should_preempt() {
                        status = SkillStatus::Cancelled;
                        reason = format!("{event:?}");
                        self.cancel.cancel();
                        break;
                    }
                }
                if status != SkillStatus::Succeeded {
                    break;
                }
                let remaining = budget
                    .saturating_sub(started.elapsed())
                    .min(Duration::from_millis(700));
                let progress = tokio::time::timeout(remaining, runner.poll()).await??;
                observation = progress.observation;
                if progress.finished {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            Ok(())
        }
        .await;
        if let Err(error) = execution {
            status = if started.elapsed() >= budget {
                SkillStatus::TimedOut
            } else {
                SkillStatus::Failed
            };
            reason = format!("{error:#}");
        }
        // Stop is explicit even when polling, start or validation-at-start fails.
        match tokio::time::timeout(Duration::from_secs(4), runner.stop()).await {
            Ok(Ok(final_observation)) => {
                if final_observation.get("fault").is_some_and(|v| !v.is_null()) {
                    status = SkillStatus::Failed;
                    reason = format!("worker fault during stop: {}", final_observation["fault"]);
                }
                observation = final_observation;
            }
            other => {
                status = SkillStatus::Failed;
                reason = format!("stop acknowledgement failed: {other:?}");
            }
        }
        self.locks.release(&resources, "skill");
        Ok(SkillResult {
            skill_id: skill.skill_id.clone(),
            version: skill.version.clone(),
            status,
            reason,
            elapsed_ms: started.elapsed().as_millis() as u64,
            observation,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use robo_archon_kinetic::Chronos;
    use robo_archon_skills::{RunnerProgress, SkillBinding};
    use serde_json::json;
    struct Mock {
        stopped: bool,
        fail: bool,
        fault_on_stop: bool,
    }
    #[async_trait]
    impl SkillRunner for Mock {
        fn id(&self) -> &str {
            "test.v1"
        }
        fn validate(&self, _: &PreparedSkill) -> Result<()> {
            Ok(())
        }
        async fn start(&mut self, _: &PreparedSkill) -> Result<()> {
            if self.fail {
                bail!("start failure")
            };
            Ok(())
        }
        async fn poll(&mut self) -> Result<RunnerProgress> {
            Ok(RunnerProgress {
                finished: false,
                observation: json!({}),
            })
        }
        async fn stop(&mut self) -> Result<Value> {
            self.stopped = true;
            Ok(
                json!({"command":[0,0,0],"fault":if self.fault_on_stop { Some("fallen") } else { None }}),
            )
        }
    }
    fn skill() -> PreparedSkill {
        PreparedSkill {
            skill_id: "test".into(),
            version: "1".into(),
            parameters: json!({}),
            timeout_ms: 30,
            binding: SkillBinding {
                body: "microduck".into(),
                backend: "mujoco".into(),
                runner: "test.v1".into(),
                policy: None,
                config: json!({}),
                resources: vec!["whole_body".into()],
            },
        }
    }
    #[tokio::test]
    async fn timeout_fault_and_cancel_stop_and_release_shared_locks() {
        for mode in 0..3 {
            let mut executive = Executive::new(Default::default(), Chronos::desktop_arm_6dof());
            if mode == 2 {
                executive.cancel.cancel();
            }
            let mut runner = Mock {
                stopped: false,
                fail: mode == 1,
                fault_on_stop: false,
            };
            let result = executive.run_skill(&skill(), &mut runner).await.unwrap();
            assert_eq!(
                result.status,
                [
                    SkillStatus::TimedOut,
                    SkillStatus::Failed,
                    SkillStatus::Cancelled
                ][mode]
            );
            assert!(runner.stopped);
            executive
                .locks
                .try_acquire(&[ResourceKind::Base, ResourceKind::Arm], "another")
                .unwrap();
        }
    }
    #[tokio::test]
    async fn whole_body_conflicts_with_trajectory_resources() {
        let mut executive = Executive::new(Default::default(), Chronos::desktop_arm_6dof());
        executive
            .locks
            .try_acquire(&[ResourceKind::Arm], "trajectory")
            .unwrap();
        let mut runner = Mock {
            stopped: false,
            fail: false,
            fault_on_stop: false,
        };
        assert!(executive.run_skill(&skill(), &mut runner).await.is_err());
        assert!(!runner.stopped);
    }
    #[tokio::test]
    async fn fault_during_stop_overrides_success_or_cancel() {
        let mut executive = Executive::new(Default::default(), Chronos::desktop_arm_6dof());
        executive.cancel.cancel();
        let mut runner = Mock {
            stopped: false,
            fail: false,
            fault_on_stop: true,
        };
        let result = executive.run_skill(&skill(), &mut runner).await.unwrap();
        assert_eq!(result.status, SkillStatus::Failed);
        assert!(result.reason.contains("fallen"));
        executive
            .locks
            .try_acquire(&[ResourceKind::Base], "next")
            .unwrap();
    }
}
