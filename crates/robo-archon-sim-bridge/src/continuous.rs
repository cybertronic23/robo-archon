//! Continuous worker bridge, independent of the waypoint protocol.
use anyhow::{bail, Context, Result};
use async_trait::async_trait;
use robo_archon_skills::{PreparedSkill, RunnerProgress, SkillRunner};
use serde_json::{json, Value};
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};

pub struct ContinuousRunner {
    child: Child,
    input: ChildStdin,
    output: Lines<BufReader<ChildStdout>>,
    sequence: u64,
}

impl ContinuousRunner {
    pub async fn launch(
        python: &str,
        script: &Path,
        package: &Path,
        viewer: bool,
        record_dir: Option<&Path>,
    ) -> Result<Self> {
        let mut command = Command::new(python);
        command
            .arg("-u")
            .arg(script)
            .arg("--package")
            .arg(package)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true);
        if viewer {
            command.arg("--viewer");
        }
        if let Some(path) = record_dir {
            command.arg("--record-dir").arg(path);
        }
        let mut child = command.spawn().context("launch continuous worker")?;
        let input = child.stdin.take().context("worker stdin")?;
        let output = BufReader::new(child.stdout.take().context("worker stdout")?).lines();
        let mut runner = Self {
            child,
            input,
            output,
            sequence: 0,
        };
        runner
            .exchange(json!({"op":"hello"}), Duration::from_secs(30))
            .await?;
        Ok(runner)
    }

    pub async fn exchange(&mut self, mut request: Value, timeout: Duration) -> Result<Value> {
        self.sequence += 1;
        request["id"] = json!(self.sequence);
        request["protocol"] = json!("archon.continuous.v1");
        let expected = self.sequence;
        tokio::time::timeout(timeout, async {
            self.input
                .write_all(format!("{}\n", request).as_bytes())
                .await?;
            self.input.flush().await?;
            let response = loop {
                let line = self
                    .output
                    .next_line()
                    .await?
                    .context("continuous worker closed")?;
                let response: Value = serde_json::from_str(&line)?;
                if response["protocol"] != "archon.continuous.v1" {
                    bail!("worker protocol mismatch");
                }
                let id = response["id"].as_u64().context("response ID missing")?;
                if id < expected {
                    continue;
                }
                if id != expected {
                    bail!("worker correlation mismatch");
                }
                break response;
            };
            if response["ok"] != true {
                bail!("worker rejected command: {}", response["error"]);
            }
            Ok(response["observation"].clone())
        })
        .await
        .context("worker response timeout")?
    }

    pub async fn shutdown(&mut self) -> Result<()> {
        let result = self
            .exchange(json!({"op":"shutdown"}), Duration::from_secs(1))
            .await;
        if result.is_err() {
            let _ = self.child.kill().await;
        }
        let status = tokio::time::timeout(Duration::from_secs(2), self.child.wait()).await??;
        if !status.success() {
            bail!("worker exited with {status}");
        }
        result.map(|_| ())
    }
}

#[async_trait]
impl SkillRunner for ContinuousRunner {
    async fn shutdown(&mut self) -> Result<()> {
        ContinuousRunner::shutdown(self).await
    }
    fn id(&self) -> &str {
        "onnx_policy.v1"
    }
    fn validate(&self, skill: &PreparedSkill) -> Result<()> {
        if skill.binding.body != "microduck"
            || skill.binding.backend != "mujoco"
            || skill.binding.policy.as_deref() != Some("velstand")
            || skill.binding.config != json!({"command_adapter":"microduck.twist.v1"})
            || skill.binding.resources != ["whole_body"]
        {
            bail!("unsupported continuous binding");
        }
        let duration = skill.parameters["duration_ms"]
            .as_u64()
            .context("duration_ms required")?;
        if duration == 0 || duration > 10000 || duration + 1000 > skill.timeout_ms {
            bail!("duration needs at least 1000 ms of stop/IO budget");
        }
        for (name, limit) in [("vx", 0.3), ("vy", 0.2), ("yaw_rate", 1.0)] {
            let value = skill.parameters[name]
                .as_f64()
                .context("twist parameter missing")?;
            if !value.is_finite() || value.abs() > limit {
                bail!("twist outside adapter limits");
            }
        }
        Ok(())
    }
    async fn start(&mut self, skill: &PreparedSkill) -> Result<()> {
        self.validate(skill)?;
        // Wait for a measured upright standing state; no pose teleport or implicit reset.
        let obs = self
            .exchange(json!({"op":"observe"}), Duration::from_millis(700))
            .await?;
        if obs["fault"] != Value::Null
            || obs["tilt_deg"].as_f64().unwrap_or(180.0) > 15.0
            || obs["xyz"][2].as_f64().unwrap_or(0.0) < 0.1
        {
            bail!("entry condition: robot must be upright");
        }
        self.exchange(json!({"op":"command","twist":[skill.parameters["vx"],skill.parameters["vy"],skill.parameters["yaw_rate"]],"duration_ms":skill.parameters["duration_ms"],"lease_ms":600}),Duration::from_millis(700)).await?;
        Ok(())
    }
    async fn poll(&mut self) -> Result<RunnerProgress> {
        let observation = self
            .exchange(json!({"op":"poll"}), Duration::from_millis(700))
            .await?;
        if observation["fault"] != Value::Null {
            bail!("worker fault: {}", observation["fault"]);
        }
        let finished = observation["state"] == "idle";
        if finished && observation["reason"] != "duration_complete" {
            bail!("motion ended unexpectedly: {}", observation["reason"]);
        }
        Ok(RunnerProgress {
            finished,
            observation,
        })
    }
    async fn stop(&mut self) -> Result<Value> {
        self.exchange(json!({"op":"stop"}), Duration::from_millis(700))
            .await?;
        // Require measured settling rather than only acknowledging a zero command.
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        let mut stable = 0;
        loop {
            let mut observation = self
                .exchange(json!({"op":"observe"}), Duration::from_millis(700))
                .await?;
            if observation["fault"] != Value::Null {
                observation["stop_confirmed"] = json!(false);
                return Ok(observation);
            }
            let vx = observation["body_velocity"][0]
                .as_f64()
                .unwrap_or(f64::INFINITY);
            let vy = observation["body_velocity"][1]
                .as_f64()
                .unwrap_or(f64::INFINITY);
            let yaw = observation["yaw_rate"].as_f64().unwrap_or(f64::INFINITY);
            if vx.hypot(vy) < 0.015 && yaw.abs() < 0.08 {
                stable += 1;
            } else {
                stable = 0;
            }
            if stable >= 3 {
                observation["stop_confirmed"] = json!(true);
                return Ok(observation);
            }
            if std::time::Instant::now() >= deadline {
                bail!("robot did not settle after zero twist");
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}
