//! Cartesian task phases go through Policy → SafetyGate → Chronos → Executive.
use anyhow::{bail, Context, Result};
use async_trait::async_trait;
use robo_archon_embodied::{
    arm_profile::ArmProfile, now_us, ActionProposal, Episode, ExecutionStatus, JointWaypoint,
    Observation, Policy, RobotBackend, SafetyGate, WorldState,
};
use robo_archon_runtime::{Executive, RuntimeEvent};
use std::{
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};
use tokio::sync::Mutex;

type Backend = Arc<Mutex<dyn RobotBackend>>;

struct CartesianPhase {
    backend: Backend,
    profile: ArmProfile,
    target: Option<[f64; 3]>,
    grip: f64,
    duration: f64,
}
#[async_trait]
impl Policy for CartesianPhase {
    fn name(&self) -> &str {
        "cartesian_pick_place"
    }
    async fn propose(&self, state: &WorldState) -> Result<ActionProposal> {
        if state.joints().names != self.profile.joint_names {
            bail!("IK observation mapping mismatch");
        }
        let positions = if let Some(target) = self.target {
            self.backend.lock().await.solve_ik(target, true).await?
        } else {
            state.joints().positions.clone()
        };
        Ok(ActionProposal {
            id: format!("cartesian-{}", now_us()),
            stamp_us: now_us(),
            source: self.name().into(),
            confidence: 1.0,
            required_resources: ActionProposal::default_resources(),
            waypoints: vec![
                JointWaypoint {
                    t_sec: 0.0,
                    positions: state.joints().positions.clone(),
                    gripper_open: Some(
                        if self.grip == 0.0
                            && state
                                .annotations
                                .iter()
                                .any(|a| a.kind == "task_state" && a.payload["grasp_seen"] == true)
                        {
                            0.0
                        } else {
                            state.gripper_open()
                        },
                    ),
                },
                JointWaypoint {
                    t_sec: self.duration,
                    positions,
                    gripper_open: Some(self.grip),
                },
            ],
            metadata: serde_json::json!({"target_m":self.target,"tool_down":true,"control_profile":self.profile}),
        })
    }
}
fn task_state(obs: &Observation) -> Result<serde_json::Value> {
    obs.annotations
        .iter()
        .find(|a| a.kind == "task_state")
        .map(|a| a.payload.clone())
        .context("binding did not return measured task_state")
}
fn xyz(state: &serde_json::Value, key: &str) -> Result<[f64; 3]> {
    let a = state[key].as_array().context("task position missing")?;
    if a.len() != 3 {
        bail!("invalid task position");
    }
    Ok([
        a[0].as_f64().context("invalid x")?,
        a[1].as_f64().context("invalid y")?,
        a[2].as_f64().context("invalid z")?,
    ])
}

pub async fn run(
    executive: &mut Executive,
    backend: Backend,
    safety: &dyn SafetyGate,
    profile: &ArmProfile,
    seed: u64,
    bundle: &Path,
) -> Result<()> {
    let mut episode = Episode::new(
        executive
            .config
            .episode_id
            .clone()
            .unwrap_or_else(|| format!("ep-{}", now_us())),
        "pick_place.v1",
        backend.lock().await.name(),
    );
    episode.push("task_config",serde_json::json!({"version":"pick_place.v1","seed":seed,"robot":profile.robot_id,"source_revision":profile.source_revision,"control_profile":profile}));
    // A task-wide stop latch spans phase boundaries; Executive resets its per-turn token.
    let stopped = Arc::new(AtomicBool::new(false));
    let flag = stopped.clone();
    let mut rx = executive.events.subscribe();
    let watch = tokio::spawn(async move {
        while let Ok(ev) = rx.recv().await {
            if matches!(
                ev,
                RuntimeEvent::UserStop { .. }
                    | RuntimeEvent::EStop { .. }
                    | RuntimeEvent::SafetyFault { .. }
            ) {
                flag.store(true, Ordering::SeqCst);
                break;
            }
        }
    });
    let result: Result<()> = async {
        let initial = task_state(&backend.lock().await.read_observation().await?)?;
        let start = xyz(&initial, "object_position")?;
        let goal = xyz(&initial, "goal_position")?;
        let height = if profile.robot_id == "so101" {
            0.085
        } else {
            0.15
        };
        let phases = [
            ("approach", Some([start[0], start[1], height]), 1.0, 2.0),
            (
                "descend",
                Some([start[0], start[1], start[2] + 0.004]),
                1.0,
                2.0,
            ),
            ("close", None, 0.0, 1.0),
            ("grasp_settle", None, 0.0, 2.0),
            ("lift", Some([start[0], start[1], height]), 0.0, 2.0),
            ("lift_settle", None, 0.0, 1.0),
            ("transfer", Some([goal[0], goal[1], height]), 0.0, 2.0),
            ("place", Some([goal[0], goal[1], goal[2] + 0.006]), 0.0, 2.0),
            ("release", None, 1.0, 2.0),
            ("retract", Some([goal[0], goal[1], height]), 1.0, 2.0),
            ("settle", None, 1.0, 1.0),
        ];
        for (name, target, grip, duration) in phases {
            if stopped.load(Ordering::SeqCst) {
                bail!("task cancelled before {name}");
            }
            eprintln!("[pick_place] phase={name}");
            episode.push(
                "task_phase",
                serde_json::json!({"name":name,"target_m":target,"gripper_open":grip}),
            );
            let policy = CartesianPhase {
                backend: backend.clone(),
                profile: profile.clone(),
                target,
                grip,
                duration,
            };
            let (result, phase) = executive
                .run_once(
                    &policy,
                    safety,
                    backend.clone(),
                    serde_json::json!({"task":"pick_place.v1","phase":name,"seed":seed}),
                )
                .await?;
            episode.events.extend(phase.events);
            if result.status != ExecutionStatus::Completed {
                bail!("phase {name}: {:?}: {}", result.status, result.message);
            }
            let measured = task_state(&backend.lock().await.read_observation().await?)?;
            episode.push(
                "task_feedback",
                serde_json::json!({"phase":name,"state":measured}),
            );
            if name == "grasp_settle" && measured["grasp_seen"] != true {
                bail!("physical grasp failed: both fingers did not contact object");
            }
            if name == "lift_settle" && measured["lifted"] != true {
                bail!("physical lift failed");
            }
        }
        if stopped.load(Ordering::SeqCst) {
            bail!("task cancelled");
        }
        let final_state = task_state(&backend.lock().await.read_observation().await?)?;
        episode.push("task_evaluation", final_state.clone());
        if final_state["success"] != true {
            bail!("measured placement criteria failed: {final_state}");
        }
        Ok(())
    }
    .await;
    watch.abort();
    let mut b = backend.lock().await;
    if result.is_err() {
        let _ = b.estop().await;
    }
    let shutdown = b.shutdown().await;
    let result = result.and(shutdown);
    episode.push("task_result",serde_json::json!({"success":result.is_ok(),"error":result.as_ref().err().map(|e|format!("{e:#}"))}));
    episode.finish();
    episode.save_bundle(bundle)?;
    println!(
        "task=pick_place.v1 success={} episode={}",
        result.is_ok(),
        bundle.join("episode.json").display()
    );
    result
}
