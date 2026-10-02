//! Skill discovery and trusted continuous execution; never run commands embedded in manifests.
use crate::Args;
use anyhow::{bail, Context, Result};
use robo_archon_kinetic::Chronos;
use robo_archon_runtime::{Executive, ExecutiveConfig};
use robo_archon_sim_bridge::continuous::ContinuousRunner;
use robo_archon_skills::{RunnerRegistry, SkillCall, SkillRegistry, SkillStatus};
use serde_json::json;
use std::path::PathBuf;

pub fn handle(args: &Args) -> Result<bool> {
    if args.list_skills || args.inspect_skill.is_some() || args.validate_skill_call.is_some() {
        let registry = SkillRegistry::load_dir(&args.skills_dir)?;
        if let Some(path) = &args.validate_skill_call {
            let catalog = crate::skill_readiness::metadata_catalog(
                robo_archon_embodied::body::BodyCatalog::load(&args.body_catalog)?,
                &args.policy_packages,
            )?;
            let call: SkillCall = serde_json::from_slice(&std::fs::read(path)?)?;
            let prepared = registry.prepare(
                &catalog,
                args.robot.as_deref().context("--robot required")?,
                &args.backend,
                &call,
            )?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({"validation_only":true,"prepared":prepared}))?
            );
        } else if let Some(id) = &args.inspect_skill {
            let skill = registry.get(id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &json!({"manifest":skill,"tool_definition":skill.tool_definition(),"runner_readiness_checked":false})
                )?
            );
        } else {
            for skill in registry.list() {
                println!(
                    "{}@{} — {} [dependency readiness checked at execution]",
                    skill.id, skill.version, skill.description
                );
            }
        }
        return Ok(true);
    }
    Ok(false)
}

pub async fn execute(args: &Args) -> Result<bool> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut python = std::env::var("ROBO_ARCHON_PYTHON").unwrap_or_else(|_| {
        root.join(".venv-microduck/bin/python")
            .to_string_lossy()
            .into_owned()
    });
    if let Some(source) = &args.install_policy {
        let status = tokio::process::Command::new(&python)
            .arg(root.join("scripts/install_policy.py"))
            .arg(source)
            .arg("--destination-root")
            .arg(&args.policy_packages)
            .status()
            .await?;
        if !status.success() {
            bail!("policy installation failed");
        }
        return Ok(true);
    }
    if args.run_skill.is_none()
        && !args.skill_keyboard
        && args.skill_instruction.is_none()
        && !args.list_skill_tools
    {
        return Ok(false);
    }
    let body = args.robot.as_deref().context("--robot required")?;
    if body != "microduck" || args.backend != "mujoco" {
        bail!("installed runner supports microduck/mujoco only");
    }
    let registry = SkillRegistry::load_dir(&args.skills_dir)?;
    let catalog = robo_archon_embodied::body::BodyCatalog::load(&args.body_catalog)?;
    let ready = crate::skill_readiness::inspect(
        &python,
        &root,
        &args.skill_assets,
        &args.policy_packages,
        &registry,
        catalog,
        body,
        &args.backend,
    )
    .await?;
    let catalog = &ready.catalog;
    if args.list_skill_tools {
        println!(
            "{}",
            serde_json::to_string_pretty(
                &json!({"tools":robo_archon_policy::skill_agent::tools(&ready.skills),"rejected":ready.rejected,"readiness":"structural, not behavioral certification"})
            )?
        );
        return Ok(true);
    }
    let mut client = None;
    let mut decision = None;
    let mut pending = if let Some(instruction) = &args.skill_instruction {
        let key = args
            .llm_api_key
            .clone()
            .or_else(|| std::env::var("OPENAI_API_KEY").ok())
            .context("skill instruction requires an LLM API key")?;
        let connection = robo_archon_policy::chat::ChatClient::new(
            key,
            args.llm_base_url
                .clone()
                .unwrap_or_else(|| "https://api.deepseek.com".into()),
            args.llm_model.clone(),
        );
        let proposal =
            robo_archon_policy::skill_agent::propose(&connection, instruction, &ready.skills)
                .await?;
        if proposal.call.is_none() {
            let report = json!({"executed":false,"refusal":proposal.refusal});
            save_report(args, &report)?;
            println!("{}", report);
            return Ok(true);
        }
        let call = proposal.call.clone();
        client = Some(connection);
        decision = Some(proposal);
        call
    } else if let Some(path) = &args.run_skill {
        Some(serde_json::from_slice::<SkillCall>(&std::fs::read(path)?)?)
    } else {
        None
    };
    let mut selected_policy = "velstand".to_string();
    // No worker or motion exists until the proposed call passes host validation.
    if let Some(call) = &pending {
        if !ready.skills.iter().any(|skill| skill.id == call.skill_id) {
            bail!("skill is not executable: {}", call.skill_id);
        }
        let prepared = registry.prepare(catalog, body, &args.backend, call)?;
        selected_policy = prepared
            .binding
            .policy
            .clone()
            .context("policy reference required")?;
        robo_archon_sim_bridge::continuous::validate_call(&prepared, &selected_policy)?;
    }
    let selected_package = ready
        .packages
        .get(&selected_policy)
        .context("policy is not ready")?
        .as_deref();
    if args.viewer && cfg!(target_os = "macos") {
        let path = std::path::Path::new(&python)
            .parent()
            .context("viewer needs a venv Python path")?
            .join("mjpython");
        python = path.to_string_lossy().into_owned();
    }
    let runner = ContinuousRunner::launch(
        &python,
        &root.join("python/robo_archon_sim_workers/microduck_worker.py"),
        &args.skill_assets,
        args.viewer,
        args.skill_record_dir.as_deref(),
        &selected_policy,
        selected_package,
    )
    .await?;
    let mut runners = RunnerRegistry::default();
    runners.register(Box::new(runner))?;
    let mut executive = Executive::new(ExecutiveConfig::default(), Chronos::desktop_arm_6dof());
    if args.auto_stop_ms > 0 {
        let cancel = executive.cancel.clone();
        let delay = args.auto_stop_ms;
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
            cancel.cancel();
        });
    }
    let (sender, mut keys) = tokio::sync::mpsc::unbounded_channel();
    if args.skill_keyboard {
        let cancel = executive.cancel.clone();
        std::thread::spawn(move || {
            use crossterm::event::{read, Event, KeyCode, KeyEventKind, KeyModifiers};
            while let Ok(event) = read() {
                if let Event::Key(key) = event {
                    if key.kind != KeyEventKind::Press {
                        continue;
                    }
                    if let KeyCode::Char(mut c) = key.code {
                        if c == 'c' && key.modifiers.contains(KeyModifiers::CONTROL) {
                            c = 'q';
                        }
                        if c == 'x' || c == 'q' {
                            cancel.cancel();
                        }
                        if sender.send(c).is_err() || c == 'q' {
                            break;
                        }
                    }
                }
            }
        });
        crossterm::terminal::enable_raw_mode()?;
        eprintln!("w: forward (vx=0.4), a/d: turn (vx=0.3, yaw=±1), x: stop, q: quit. Commands expire after 2 seconds.");
    }
    let result: Result<()> = async {
        loop {
            if args.skill_keyboard {
                let mut key = keys.recv().await.context("keyboard closed")?;
                // Do not replay stale movement keys after an already queued stop/quit.
                while let Ok(next) = keys.try_recv() {
                    if key != 'q' && (key != 'x' || next == 'q') {
                        key = next;
                    }
                }
                if key == 'q' {
                    break;
                }
                let (vx, yaw) = match key {
                    'w' => (0.4, 0.0),
                    'a' => (0.3, 1.0),
                    'd' => (0.3, -1.0),
                    'x' => (0.0, 0.0),
                    _ => continue,
                };
                executive.cancel.reset();
                pending = Some(SkillCall {
                    skill_id: if key == 'x' {
                        "microduck.stop"
                    } else {
                        "microduck.walk"
                    }
                    .into(),
                    parameters: json!({"vx":vx,"yaw_rate":yaw,"duration_ms":2000}),
                    timeout_ms: 4000,
                });
            }
            let call = pending.take().context("skill call missing")?;
            let prepared = registry.prepare(catalog, body, &args.backend, &call)?;
            let outcome = executive
                .run_skill(&prepared, runners.get_mut(&prepared.binding.runner)?)
                .await?;
            let raw = serde_json::to_value(&outcome)?;
            if let (Some(client), Some(decision), Some(instruction)) =
                (&client, &decision, &args.skill_instruction)
            {
                // Preserve physical evidence before asking the model to explain it.
                let mut report =
                    json!({"executed":true,"call":decision.call,"result":raw,"feedback":null});
                save_report(args, &report)?;
                report["feedback"] = match robo_archon_policy::skill_agent::feedback(
                    client,
                    instruction,
                    decision,
                    &raw,
                )
                .await
                {
                    Ok(text) => json!({"text":text}),
                    Err(error) => json!({"error":error.to_string()}),
                };
                save_report(args, &report)?;
                println!("{}", report);
            } else {
                save_report(args, &raw)?;
                println!("{}", raw);
            }
            if !args.skill_keyboard {
                if outcome.status != SkillStatus::Succeeded {
                    bail!("skill ended: {:?}: {}", outcome.status, outcome.reason);
                }
                break;
            }
            if outcome.status == SkillStatus::Failed {
                bail!("skill failed; restart explicitly: {}", outcome.reason);
            }
        }
        Ok(())
    }
    .await;
    if args.skill_keyboard {
        let _ = crossterm::terminal::disable_raw_mode();
    }
    let shutdown = runners.get_mut("onnx_policy.v1")?.shutdown().await;
    result?;
    shutdown?;
    Ok(true)
}

fn save_report(args: &Args, value: &serde_json::Value) -> Result<()> {
    if let Some(path) = &args.skill_report {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, serde_json::to_string_pretty(value)?)?;
    }
    Ok(())
}
