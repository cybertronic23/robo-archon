//! UI-independent Skill chat lifecycle shared by line CLI and TUI.
use crate::{skill_commands as commands, skill_readiness::Readiness, Args};
use anyhow::{Context, Result};
use robo_archon_policy::{chat::ChatClient, skill_agent};
use robo_archon_runtime::Executive;
use robo_archon_sim_bridge::continuous::ContinuousRunner;
use robo_archon_skills::{SkillRegistry, SkillRunner};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};

pub type Input = (String, u64);

pub fn client(args: &Args) -> Result<ChatClient> {
    let key = args
        .llm_api_key
        .clone()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| {
            std::env::var("OPENAI_API_KEY")
                .ok()
                .filter(|s| !s.trim().is_empty())
        })
        .context("请在本地 .env 配置 DEEPSEEK_API_KEY（或 OPENAI_API_KEY），保存后重启")?;
    Ok(ChatClient::new(
        key,
        args.llm_base_url
            .clone()
            .unwrap_or_else(|| "https://api.deepseek.com".into()),
        args.llm_model.clone(),
    ))
}

fn emit(events: &Option<UnboundedSender<Value>>, event: Value) {
    if let Some(tx) = events {
        let _ = tx.send(event);
    } else if event["kind"] == "report" {
        println!("{}", event["report"]);
    }
}

pub async fn run_chat(
    args: &Args,
    registry: &SkillRegistry,
    ready: &Readiness,
    client: ChatClient,
    mut executive: Executive,
    mut runner: ContinuousRunner,
    mut lines: UnboundedReceiver<Input>,
    interruption: Arc<AtomicU64>,
    events: Option<UnboundedSender<Value>>,
) -> Result<()> {
    let mut reports = Vec::new();
    let metadata = json!({"body":args.robot,"backend":args.backend,
        "connection":client.connection_metadata(),
        "loaded_skills":ready.skills.iter().map(|s| &s.id).collect::<Vec<_>>()});
    let execution: Result<()> = async {
        loop {
            let message = tokio::select! {
                message=lines.recv()=>message,
                _=tokio::signal::ctrl_c()=>{executive.cancel.cancel();break;}
            };
            let Some((instruction,generation))=message else { break; };
            let instruction = instruction.trim();
            if instruction == "/quit" { break; }
            if instruction == "/stop" {
                runner.stop().await?;
                emit(&events,json!({"kind":"report","report":{"stopped":true}}));
                continue;
            }
            let report = if instruction == "/reset" {
                let observation=runner.reset_simulation().await?;
                executive.cancel.reset();
                json!({"executed":false,"explicit_simulation_reset":true,"observation":observation})
            } else {
                if instruction.is_empty() || generation != interruption.load(Ordering::SeqCst) { continue; }
                executive.cancel.reset();
                if generation != interruption.load(Ordering::SeqCst) { executive.cancel.cancel(); continue; }
                emit(&events,json!({"kind":"planning","connection":client.connection_metadata()}));
                let history = json!(reports.iter().rev().take(8).collect::<Vec<_>>());
                let proposal = tokio::select! {
                    p=skill_agent::propose_with_context(&client,instruction,&ready.skills,&history)=>p,
                    _=async { while !executive.cancel.is_cancelled() {
                        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                    }} => Err(anyhow::anyhow!("cancelled during model planning; no motion started")),
                };
                match proposal {
                    Err(e)=>json!({"instruction":instruction,"executed":false,"error":e.to_string()}),
                    Ok(proposal)=> {
                        let evidence=json!({"trace":proposal.assistant["_archon_trace"],
                            "tool_calls":proposal.assistant["tool_calls"]});
                        emit(&events,json!({"kind":"plan","evidence":evidence}));
                        if executive.cancel.is_cancelled() || generation != interruption.load(Ordering::SeqCst) {
                            json!({"instruction":instruction,"executed":false,"planning":evidence,"error":"cancelled before execution"})
                        } else if proposal.call.is_none() && proposal.sequence.is_none() {
                            json!({"instruction":instruction,"executed":false,"planning":evidence,"refusal":proposal.refusal})
                        } else {
                            match commands::decision_sequence(&proposal).and_then(|s| commands::prepare_plan(args,registry,ready,&s)) {
                                Err(e)=>json!({"instruction":instruction,"executed":false,"planning":evidence,"error":e.to_string()}),
                                Ok(plan)=> {
                                    emit(&events,json!({"kind":"executing","plan":plan}));
                                    let outcome = executive.run_sequence(&plan,&mut runner).await?;
                                    let mut report=json!({"instruction":instruction,"executed":true,"planning":evidence,"result":outcome,"feedback":null});
                                    reports.push(report.clone());
                                    commands::save_report(args,&json!({"session":metadata,"turns":reports}))?;
                                    emit(&events,json!({"kind":"feedback","status":outcome["status"]}));
                                    report["feedback"]=tokio::select! {
                                        f=skill_agent::feedback_evidence(&client,instruction,&proposal,&outcome)=>match f {
                                            Ok(v)=>v, Err(e)=>json!({"error":e.to_string()})
                                        },
                                        _=async { while !executive.cancel.is_cancelled() {
                                            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                                        }}=>json!({"error":"feedback cancelled; measured execution retained"}),
                                    };
                                    reports.pop();
                                    report
                                }
                            }
                        }
                    }
                }
            };
            reports.push(report.clone());
            commands::save_report(args,&json!({"session":metadata,"turns":reports}))?;
            emit(&events,json!({"kind":"report","report":report}));
        }
        Ok(())
    }.await;
    let stopped = runner.stop().await;
    let shutdown = runner.shutdown().await;
    execution?;
    stopped?;
    shutdown?;
    Ok(())
}
