//! Skill discovery and trusted continuous execution; never run commands embedded in manifests.
use crate::Args;
use anyhow::{bail, Context, Result};
use robo_archon_kinetic::Chronos;
use robo_archon_runtime::{Executive, ExecutiveConfig};
use robo_archon_sim_bridge::continuous::ContinuousRunner;
use robo_archon_skills::{RunnerRegistry, SkillCall, SkillRegistry, SkillRunner, SkillStatus};
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
        && args.run_skill_sequence.is_none()
        && !args.skill_chat
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
                &json!({"tools":robo_archon_policy::skill_agent::planning_tools(&ready.skills),"rejected":ready.rejected,"readiness":"structural, not behavioral certification"})
            )?
        );
        return Ok(true);
    }
    if args.run_skill_sequence.is_some() || args.skill_chat {
        return execute_session(args, &root, &python, &registry, &ready).await;
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
        if proposal.call.is_none() && proposal.sequence.is_none() {
            let report = json!({"executed":false,"refusal":proposal.refusal});
            save_report(args, &report)?;
            println!("{}", report);
            return Ok(true);
        }
        if proposal.sequence.is_some()
            || proposal.call.as_ref().is_some_and(|c| {
                registry.get(&c.skill_id).is_ok_and(|s| {
                    s.bindings.iter().any(|b| {
                        b.runner == "sequence.v1"
                            || b.config["command_adapter"] == "microduck.behavior.v1"
                    })
                })
            })
        {
            return execute_decision(
                args,
                &root,
                &python,
                &registry,
                &ready,
                &connection,
                proposal,
                instruction,
            )
            .await;
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
    if pending.as_ref().is_some_and(|c| {
        registry.get(&c.skill_id).is_ok_and(|s| {
            s.bindings.iter().any(|b| {
                b.runner == "sequence.v1" || b.config["command_adapter"] == "microduck.behavior.v1"
            })
        })
    }) {
        let call = pending.take().unwrap();
        return execute_plan_once(
            args,
            &root,
            &python,
            &registry,
            &ready,
            robo_archon_skills::SkillSequence {
                schema_version: 1,
                timeout_ms: call.timeout_ms,
                steps: vec![call],
            },
        )
        .await;
    }
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

fn prepare_plan(
    args: &Args,
    registry: &SkillRegistry,
    ready: &crate::skill_readiness::Readiness,
    sequence: &robo_archon_skills::SkillSequence,
) -> Result<robo_archon_skills::PreparedSequence> {
    let plan = registry.prepare_sequence(
        &ready.catalog,
        args.robot.as_deref().context("robot required")?,
        &args.backend,
        sequence,
    )?;
    for leaf in &plan.steps {
        if !ready.skills.iter().any(|s| s.id == leaf.skill_id) {
            bail!("sequence contains unavailable skill {}", leaf.skill_id);
        }
        let id = leaf.binding.policy.as_deref().context("policy required")?;
        robo_archon_sim_bridge::continuous::validate_call(leaf, id)?;
    }
    Ok(plan)
}

async fn launch_session(
    args: &Args,
    root: &std::path::Path,
    python: &str,
    ready: &crate::skill_readiness::Readiness,
) -> Result<ContinuousRunner> {
    let python = if args.viewer && cfg!(target_os = "macos") {
        std::path::Path::new(python)
            .parent()
            .context("venv path")?
            .join("mjpython")
            .to_string_lossy()
            .into_owned()
    } else {
        python.into()
    };
    ContinuousRunner::launch_with_policies(
        &python,
        &root.join("python/robo_archon_sim_workers/microduck_worker.py"),
        &args.skill_assets,
        args.viewer,
        args.skill_record_dir.as_deref(),
        "velstand",
        None,
        &ready.packages,
    )
    .await
}

fn session_executive(args: &Args) -> Executive {
    let executive = Executive::new(ExecutiveConfig::default(), Chronos::desktop_arm_6dof());
    if args.auto_stop_ms > 0 {
        let cancel = executive.cancel.clone();
        let delay = args.auto_stop_ms;
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
            cancel.cancel();
        });
    }
    let cancel = executive.cancel.clone();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            cancel.cancel();
        }
    });
    executive
}

async fn execute_plan_once(
    args: &Args,
    root: &std::path::Path,
    python: &str,
    registry: &SkillRegistry,
    ready: &crate::skill_readiness::Readiness,
    sequence: robo_archon_skills::SkillSequence,
) -> Result<bool> {
    let plan = prepare_plan(args, registry, ready, &sequence)?;
    let mut runner = launch_session(args, root, python, ready).await?;
    let mut executive = session_executive(args);
    let result = executive.run_sequence(&plan, &mut runner).await;
    let shutdown = runner.shutdown().await;
    let result = result?;
    save_report(args, &result)?;
    println!("{result}");
    shutdown?;
    if result["status"] != "succeeded" {
        bail!("sequence did not succeed: {}", result["status"]);
    }
    Ok(true)
}

fn decision_sequence(
    decision: &robo_archon_policy::skill_agent::SkillDecision,
) -> Result<robo_archon_skills::SkillSequence> {
    if let Some(sequence) = &decision.sequence {
        return Ok(sequence.clone());
    }
    let call = decision.call.clone().context("no proposed action")?;
    Ok(robo_archon_skills::SkillSequence {
        schema_version: 1,
        timeout_ms: call.timeout_ms,
        steps: vec![call],
    })
}

async fn execute_decision(
    args: &Args,
    root: &std::path::Path,
    python: &str,
    registry: &SkillRegistry,
    ready: &crate::skill_readiness::Readiness,
    client: &robo_archon_policy::chat::ChatClient,
    decision: robo_archon_policy::skill_agent::SkillDecision,
    instruction: &str,
) -> Result<bool> {
    let sequence = decision_sequence(&decision)?;
    let plan = prepare_plan(args, registry, ready, &sequence)?;
    let mut runner = launch_session(args, root, python, ready).await?;
    let mut executive = session_executive(args);
    let outcome = executive.run_sequence(&plan, &mut runner).await;
    let shutdown = runner.shutdown().await;
    let outcome = outcome?;
    let mut report = json!({"executed":true,"sequence":sequence,"result":outcome,"feedback":null});
    save_report(args, &report)?;
    report["feedback"] =
        match robo_archon_policy::skill_agent::feedback(client, instruction, &decision, &outcome)
            .await
        {
            Ok(text) => json!({"text":text}),
            Err(e) => json!({"error":e.to_string()}),
        };
    save_report(args, &report)?;
    println!("{report}");
    shutdown?;
    if outcome["status"] != "succeeded" {
        bail!("sequence did not succeed");
    }
    Ok(true)
}

async fn execute_session(
    args: &Args,
    root: &std::path::Path,
    python: &str,
    registry: &SkillRegistry,
    ready: &crate::skill_readiness::Readiness,
) -> Result<bool> {
    if let Some(path) = &args.run_skill_sequence {
        let sequence = serde_json::from_slice(&std::fs::read(path)?)?;
        return execute_plan_once(args, root, python, registry, ready, sequence).await;
    }
    let key = args
        .llm_api_key
        .clone()
        .or_else(|| std::env::var("OPENAI_API_KEY").ok())
        .context("skill chat requires API key in this process")?;
    let client = robo_archon_policy::chat::ChatClient::new(
        key,
        args.llm_base_url
            .clone()
            .unwrap_or_else(|| "https://api.deepseek.com".into()),
        args.llm_model.clone(),
    );
    let mut executive = session_executive(args);
    let (sender, mut lines) = tokio::sync::mpsc::unbounded_channel();
    let cancel = executive.cancel.clone();
    let interruption = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
    let input_interruption = interruption.clone();
    let signal_interruption = interruption.clone();
    let signal_sender = sender.clone();
    let signal_cancel = executive.cancel.clone();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            let generation =
                signal_interruption.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
            signal_cancel.cancel();
            let _ = signal_sender.send(("/quit".to_string(), generation));
        }
    });
    std::thread::spawn(move || {
        use std::io::BufRead;
        for line in std::io::stdin().lock().lines() {
            let Ok(line) = line else {
                break;
            };
            if ["/stop", "/quit", "/reset"].contains(&line.trim()) {
                input_interruption.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                cancel.cancel();
            }
            let generation = input_interruption.load(std::sync::atomic::Ordering::SeqCst);
            if sender.send((line, generation)).is_err() {
                break;
            }
        }
        cancel.cancel();
        let generation = input_interruption.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
        let _ = sender.send(("/quit".to_string(), generation));
    });
    let mut runner = launch_session(args, root, python, ready).await?;
    eprintln!("Persistent Microduck chat: enter an instruction, /stop to cancel, /quit to exit. Simulation stays alive between turns.");
    let mut reports = Vec::new();
    let execution: Result<()> = async {
        loop {
            let message = tokio::select! {
                message=lines.recv()=>message,
                _=tokio::signal::ctrl_c()=>{executive.cancel.cancel();break;}
            };
            let Some((instruction,generation))=message else { break; };
            let instruction = instruction.trim();
            if instruction == "/quit" { break; }
            if instruction == "/stop" { runner.stop().await?; continue; }
            if instruction == "/reset" {
                let observation=runner.reset_simulation().await?;
                executive.cancel.reset();
                let report=json!({"executed":false,"explicit_simulation_reset":true,"observation":observation});
                reports.push(report.clone());save_report(args,&json!({"turns":reports}))?;println!("{report}");continue;
            }
            if instruction.is_empty() || generation != interruption.load(std::sync::atomic::Ordering::SeqCst) { continue; }
            executive.cancel.reset();
            if generation != interruption.load(std::sync::atomic::Ordering::SeqCst) { executive.cancel.cancel(); continue; }
            let history = json!(reports.iter().rev().take(8).collect::<Vec<_>>());
            let proposal = match robo_archon_policy::skill_agent::propose_with_context(&client, instruction, &ready.skills, &history).await {
                Ok(p) => p,
                Err(error) => { let report=json!({"instruction":instruction,"executed":false,"error":error.to_string()}); reports.push(report.clone()); save_report(args, &json!({"turns":reports}))?; println!("{report}"); continue; }
            };
            if proposal.call.is_none() && proposal.sequence.is_none() {
                let report=json!({"instruction":instruction,"executed":false,"refusal":proposal.refusal});
                reports.push(report.clone()); save_report(args, &json!({"turns":reports}))?; println!("{report}"); continue;
            }
            let plan = match decision_sequence(&proposal).and_then(|s| prepare_plan(args, registry, ready, &s)) {
                Ok(p)=>p,
                Err(error)=> { let report=json!({"instruction":instruction,"executed":false,"error":error.to_string()}); reports.push(report.clone()); save_report(args,&json!({"turns":reports}))?; println!("{report}"); continue; }
            };
            let outcome = executive.run_sequence(&plan, &mut runner).await?;
            let mut report=json!({"instruction":instruction,"executed":true,"result":outcome,"feedback":null});
            reports.push(report.clone()); save_report(args, &json!({"turns":reports}))?;
            report["feedback"] = match robo_archon_policy::skill_agent::feedback(&client, instruction, &proposal, &outcome).await { Ok(text)=>json!({"text":text}), Err(e)=>json!({"error":e.to_string()}) };
            *reports.last_mut().unwrap()=report.clone(); save_report(args,&json!({"turns":reports}))?; println!("{report}");
            if outcome["status"] == "failed" { eprintln!("Task failed. Use /reset for an explicit simulation restart, or /quit. No automatic recovery."); }
        }
        Ok(())
    }.await;
    let stopped = runner.stop().await;
    let shutdown = runner.shutdown().await;
    execution?;
    stopped?;
    shutdown?;
    Ok(true)
}
