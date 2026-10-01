//! Validated position-control profile for a specific robot/platform binding.
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArmProfile {
    pub schema_version: u32,
    pub robot_id: String,
    pub source_revision: String,
    pub joint_names: Vec<String>,
    pub actuator_names: Vec<String>,
    pub home: Vec<f64>,
    pub lower: Vec<f64>,
    pub upper: Vec<f64>,
    pub gripper: GripperProfile,
    pub keyframe: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GripperProfile {
    pub actuator_name: String,
    pub open_ctrl: f64,
    pub closed_ctrl: f64,
    pub joint_names: Vec<String>,
    pub open_positions: Vec<f64>,
    pub closed_positions: Vec<f64>,
}
impl ArmProfile {
    pub fn validate(&self) -> Result<()> {
        let n = self.joint_names.len();
        for names in [&self.joint_names, &self.actuator_names] {
            if names
                .iter()
                .collect::<std::collections::BTreeSet<_>>()
                .len()
                != names.len()
            {
                bail!("duplicate arm profile mapping");
            }
        }
        if self.schema_version != 1
            || n == 0
            || self.robot_id.is_empty()
            || self.source_revision.len() != 40
        {
            bail!("invalid arm profile identity/version");
        }
        if [
            self.actuator_names.len(),
            self.home.len(),
            self.lower.len(),
            self.upper.len(),
        ]
        .iter()
        .any(|&len| len != n)
        {
            bail!("arm profile dimension mismatch");
        }
        for i in 0..n {
            if self.joint_names[i].is_empty()
                || self.actuator_names[i].is_empty()
                || !self.home[i].is_finite()
                || !self.lower[i].is_finite()
                || !self.upper[i].is_finite()
                || self.lower[i] >= self.upper[i]
                || self.home[i] < self.lower[i]
                || self.home[i] > self.upper[i]
            {
                bail!("invalid arm profile joint {i}");
            }
        }
        let g = &self.gripper;
        if g.actuator_name.is_empty()
            || g.joint_names.is_empty()
            || g.joint_names.len() != g.open_positions.len()
            || g.joint_names.len() != g.closed_positions.len()
            || !g.open_ctrl.is_finite()
            || !g.closed_ctrl.is_finite()
            || g.open_positions
                .iter()
                .chain(&g.closed_positions)
                .any(|v| !v.is_finite())
        {
            bail!("invalid gripper profile");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_wrong_dimensions_and_out_of_range_home() {
        let mut profile: ArmProfile =
            serde_json::from_str(include_str!("../../../robots/profiles/franka_panda.json"))
                .unwrap();
        profile.validate().unwrap();
        profile.home[3] = 0.0;
        assert!(profile.validate().is_err());
        profile.home.pop();
        assert!(profile.validate().is_err());
    }
}
