//! Conservative named-arm primitives. No arbitrary generic-arm trajectories.
use crate::{MotionPrimitive, RobotKind};
use anyhow::{bail, Result};
use async_trait::async_trait;
use robo_archon_embodied::{
    arm_profile::ArmProfile, now_us, ActionProposal, JointWaypoint, Policy, ResourceKind,
    WorldState,
};

pub struct ProfilePolicy {
    pub profile: ArmProfile,
    pub instruction: String,
}
#[async_trait]
impl Policy for ProfilePolicy {
    fn name(&self) -> &str {
        "profile_instruction"
    }
    async fn propose(&self, state: &WorldState) -> Result<ActionProposal> {
        self.profile.validate()?;
        if state.joints().names != self.profile.joint_names
            || state.joints().positions.len() != self.profile.home.len()
        {
            bail!("observation joints do not match arm profile");
        }
        let text = state
            .task_context
            .get("instruction")
            .and_then(|v| v.as_str())
            .unwrap_or(&self.instruction);
        let steps = MotionPrimitive::parse_sequence_for(text, RobotKind::Arm)?;
        let mut pos = state.joints().positions.clone();
        let mut grip = state.gripper_open();
        let mut t = 0.0;
        let mut waypoints = vec![JointWaypoint {
            t_sec: t,
            positions: pos.clone(),
            gripper_open: Some(grip),
        }];
        for (primitive, _) in steps {
            match primitive {
                MotionPrimitive::Home => pos = self.profile.home.clone(),
                MotionPrimitive::OpenGripper => grip = 1.0,
                MotionPrimitive::CloseGripper => grip = 0.0,
                MotionPrimitive::Wave | MotionPrimitive::Demo => {
                    pos = self.profile.home.clone();
                    for offset in [0.0, 0.25, -0.25, 0.25, 0.0] {
                        pos[0] = (self.profile.home[0] + offset)
                            .clamp(self.profile.lower[0], self.profile.upper[0]);
                        t += 1.0;
                        waypoints.push(JointWaypoint {
                            t_sec: t,
                            positions: pos.clone(),
                            gripper_open: Some(grip),
                        });
                    }
                }
                _ => bail!(
                    "profile {} currently supports home/wave/demo/open_gripper/close_gripper only",
                    self.profile.robot_id
                ),
            }
            t += 2.0;
            waypoints.push(JointWaypoint {
                t_sec: t,
                positions: pos.clone(),
                gripper_open: Some(grip),
            });
        }
        Ok(ActionProposal {
            id: format!("profile-{}", now_us()),
            stamp_us: now_us(),
            source: self.name().into(),
            waypoints,
            confidence: 1.0,
            required_resources: vec![ResourceKind::Arm, ResourceKind::Gripper],
            metadata: serde_json::json!({"robot": self.profile.robot_id, "source_revision": self.profile.source_revision, "control_profile": self.profile, "instruction": text}),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use robo_archon_embodied::{JointState, Observation, ProprioState};
    #[tokio::test]
    async fn profiles_generate_bounded_named_motion_and_reject_generic_reach() {
        for raw in [
            include_str!("../../../robots/profiles/franka_panda.json"),
            include_str!("../../../robots/profiles/so101.json"),
        ] {
            let profile: ArmProfile = serde_json::from_str(raw).unwrap();
            profile.validate().unwrap();
            let obs = Observation::from_proprio(ProprioState::new(
                JointState::new(profile.joint_names.clone(), profile.home.clone()),
                1.0,
            ));
            let state = WorldState::from_observation(&obs, serde_json::json!({}));
            let policy = ProfilePolicy {
                profile: profile.clone(),
                instruction: "挥手然后关闭夹爪".into(),
            };
            let proposal = policy.propose(&state).await.unwrap();
            assert!(proposal
                .waypoints
                .iter()
                .any(|w| w.positions != profile.home));
            assert_eq!(proposal.waypoints.last().unwrap().gripper_open, Some(0.0));
            let limits = robo_archon_kinetic::JointLimits {
                lower: profile.lower.clone(),
                upper: profile.upper.clone(),
            };
            for wp in &proposal.waypoints {
                limits.contains(&wp.positions).unwrap();
            }
            let policy = ProfilePolicy {
                profile,
                instruction: "reach".into(),
            };
            assert!(policy.propose(&state).await.is_err());
        }
    }
}
