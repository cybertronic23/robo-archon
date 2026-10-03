//! Local body/platform/skill selection and a responsive UI for the shared engine.
use crate::{
    skill_commands,
    skill_readiness::{self, Readiness},
    skill_session, Args,
};
use anyhow::{bail, Context, Result};
use crossterm::{
    event::{Event, EventStream, KeyCode, KeyEventKind, KeyModifiers},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use futures::StreamExt;
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout},
    style::{Color, Style},
    widgets::{Block, Borders, Paragraph, Wrap},
    Terminal,
};
use robo_archon_embodied::body::BodyCatalog;
use robo_archon_skills::{SkillCall, SkillRegistry, SkillSequence};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    io::{self, IsTerminal},
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};
use tokio::sync::mpsc;

pub fn select_skills(
    registry: &SkillRegistry,
    ready: &mut Readiness,
    body: &str,
    backend: &str,
    selected: &BTreeSet<String>,
) -> Result<()> {
    if selected.is_empty() {
        bail!("至少加载一个 Skill");
    }
    if selected
        .iter()
        .any(|id| !ready.skills.iter().any(|s| &s.id == id))
    {
        bail!("选择包含未就绪 Skill");
    }
    for skill in ready.skills.iter().filter(|s| selected.contains(&s.id)) {
        if skill.bindings.iter().any(|b| b.runner == "sequence.v1") {
            let sequence = SkillSequence {
                schema_version: 1,
                timeout_ms: skill.max_duration_ms,
                steps: vec![SkillCall {
                    skill_id: skill.id.clone(),
                    parameters: json!({}),
                    timeout_ms: skill.max_duration_ms,
                }],
            };
            let plan = registry.prepare_sequence(&ready.catalog, body, backend, &sequence)?;
            for leaf in plan.steps {
                if !selected.contains(&leaf.skill_id) {
                    bail!("{} 需要同时加载 {}", skill.id, leaf.skill_id);
                }
            }
        }
    }
    ready.skills.retain(|s| selected.contains(&s.id));
    Ok(())
}

struct Screen(Terminal<CrosstermBackend<io::Stdout>>);
impl Screen {
    fn open() -> Result<Self> {
        enable_raw_mode()?;
        if let Err(e) = execute!(io::stdout(), EnterAlternateScreen) {
            let _ = disable_raw_mode();
            return Err(e.into());
        }
        match Terminal::new(CrosstermBackend::new(io::stdout())) {
            Ok(t) => Ok(Self(t)),
            Err(e) => {
                let _ = disable_raw_mode();
                let _ = execute!(io::stdout(), LeaveAlternateScreen);
                Err(e.into())
            }
        }
    }
}
impl Drop for Screen {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(self.0.backend_mut(), LeaveAlternateScreen);
        let _ = self.0.show_cursor();
    }
}

#[derive(PartialEq)]
enum Stage {
    Body,
    Platform,
    Checking,
    Skills,
    Starting,
    Chat,
}

fn report_text(report: &Value) -> String {
    let mut lines = Vec::new();
    if let Some(s) = report["instruction"].as_str() {
        lines.push(format!("你：{s}"));
    }
    if report["stopped"] == true {
        lines.push("已停止".into());
    }
    if report["explicit_simulation_reset"] == true {
        lines.push(format!(
            "仿真已重置 · episode {}",
            report["observation"]["episode_id"]
        ));
    }
    if let Some(s) = report["error"].as_str() {
        lines.push(format!("错误：{s}"));
    }
    if let Some(s) = report["refusal"].as_str() {
        lines.push(format!("模型：{s}"));
    }
    if !report["result"].is_null() {
        lines.push(format!(
            "执行：{} · stop_confirmed={} · fault={}",
            report["result"]["status"],
            report["result"]["observation"]["stop_confirmed"],
            report["result"]["observation"]["fault"]
        ));
    }
    if let Some(s) = report["feedback"]["text"].as_str() {
        lines.push(format!("模型反馈：{s}"));
    }
    if let Some(s) = report["feedback"]["error"].as_str() {
        lines.push(format!("反馈：{s}"));
    }
    if !report["feedback"]["trace"].is_null() {
        lines.push(format!("反馈 API：{}", report["feedback"]["trace"]));
    }
    lines.join("\n")
}

pub async fn run(mut args: Args) -> Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        bail!("TUI 需要交互式终端；脚本使用 --skill-chat");
    }
    args.viewer = true;
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let catalog = BodyCatalog::load(&args.body_catalog)?;
    let registry = SkillRegistry::load_dir(&args.skills_dir)?;
    if catalog.bodies.is_empty() {
        bail!("没有本体资料包");
    }
    let python = std::env::var("ROBO_ARCHON_PYTHON").unwrap_or_else(|_| {
        root.join(".venv-microduck/bin/python")
            .to_string_lossy()
            .into_owned()
    });
    if args.skill_report.is_none() {
        args.skill_report = Some(PathBuf::from("tmp-episodes/microduck-tui-chat.json"));
    }
    let mut screen = Screen::open()?;
    let mut stage = Stage::Body;
    let mut body_index = args
        .robot
        .as_ref()
        .and_then(|id| catalog.bodies.iter().position(|b| &b.id == id))
        .or_else(|| catalog.bodies.iter().position(|b| b.id == "microduck"))
        .unwrap_or(0);
    let mut index = body_index;
    let mut platforms: Vec<String> = Vec::new();
    let mut ready: Option<Readiness> = None;
    let mut selected = BTreeSet::new();
    let mut message = String::new();
    let mut input = String::new();
    let mut history = String::new();
    let mut busy = false;
    let mut quitting = false;
    let mut status = "请选择本体资料包".to_string();
    let mut connection_display = String::new();
    let (check_tx, mut check_rx) = mpsc::unbounded_channel();
    let (evt_tx, mut evt_rx) = mpsc::unbounded_channel::<Value>();
    let (input_tx, input_rx) = mpsc::unbounded_channel::<skill_session::Input>();
    let mut input_rx = Some(input_rx);
    let interruption = Arc::new(AtomicU64::new(0));
    let mut cancel = None;
    let mut worker: Option<tokio::task::JoinHandle<Result<()>>> = None;
    let mut events = EventStream::new();
    let mut tick = tokio::time::interval(std::time::Duration::from_millis(80));
    let mut scroll = 0u16;
    let mut follow_tail = true;
    let result:Result<()>=async {
        loop {
            let content=match stage {
                Stage::Body=>catalog.bodies.iter().enumerate().map(|(i,b)|format!("{} {} ({}){}",if i==index{"›"}else{" "},b.name,b.id,
                    if b.id=="microduck"{""}else{" · 本阶段 Skill 会话 Runner 待适配"})).collect::<Vec<_>>().join("\n"),
                Stage::Platform=>platforms.iter().enumerate().map(|(i,p)|format!("{} {}{}",if i==index{"›"}else{" "},p,
                    if catalog.bodies[body_index].id=="microduck" && p=="mujoco"{""}else{" · 当前 Skill 会话 Runner 未适配"})).collect::<Vec<_>>().join("\n") + if args.viewer {"\n[v] 仿真窗口：开启（按 v 切换）"} else {"\n[v] 仿真窗口：关闭（按 v 切换）"},
                Stage::Checking=>"正在校验 Python、资产、策略包与技能绑定…".into(),
                Stage::Skills=>{
                    let r=ready.as_ref().context("readiness missing")?;
                    let mut text=r.skills.iter().enumerate().map(|(i,s)|format!("{} [{}] {} @{}",if i==index{"›"}else{" "},if selected.contains(&s.id){"x"}else{" "},s.id,s.version)).collect::<Vec<_>>().join("\n");
                    for rejected in &r.rejected {text.push_str(&format!("\n不可用：{rejected}"));}
                    text
                },
                Stage::Starting=>"启动一次仿真会话…".into(),
                Stage::Chat=>history.clone(),
            };
            let body=args.robot.as_deref().unwrap_or("未选择");
            let platform = if stage==Stage::Body || stage==Stage::Platform {"未选择"} else {&args.backend};
            let top=format!("RoboArchon · 本体 {} · 平台 {} · {}\n{}",body,platform,status,connection_display);
            let help=if stage==Stage::Chat {"Enter 发送 · /stop · /reset · /quit · Ctrl-C · PgUp/PgDn"}
                else {"↑↓ 选择 · Space 勾选 Skill · Enter 确认/加载 · Esc 返回 · Ctrl-C 退出"};
            screen.0.draw(|f|{
                let layout=Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(4),Constraint::Min(4),Constraint::Length(3),Constraint::Length(3)]).split(f.area());
                f.render_widget(Paragraph::new(top).block(Block::default().borders(Borders::ALL)),layout[0]);
                let width = layout[1].width.saturating_sub(2).max(1);
                let visible = layout[1].height.saturating_sub(2) as usize;
                let rows = Paragraph::new(content.clone()).wrap(Wrap {trim:false}).line_count(width);
                let maximum = rows.saturating_sub(visible).min(u16::MAX as usize) as u16;
                let offset = if stage==Stage::Chat {
                    scroll = if follow_tail {maximum} else {scroll.min(maximum)}; scroll
                } else {
                    let prefix = content.lines().take(index+1).collect::<Vec<_>>().join("\n");
                    Paragraph::new(prefix).wrap(Wrap {trim:false}).line_count(width)
                        .saturating_sub(visible).min(u16::MAX as usize) as u16
                };
                f.render_widget(Paragraph::new(content).wrap(Wrap{trim:false}).scroll((offset,0)).block(Block::default().borders(Borders::ALL).title(if stage==Stage::Chat{"对话与执行证据"}else{"会话配置"})),layout[1]);
                f.render_widget(Paragraph::new(if message.is_empty(){help.to_string()}else{format!("{message} · {help}")}).style(Style::default().fg(Color::Yellow)).wrap(Wrap{trim:false}),layout[2]);
                f.render_widget(Paragraph::new(input.clone()).block(Block::default().borders(Borders::ALL).title(if busy{"运行中：可输入 /stop、/reset、/quit"}else{"输入指令"})),layout[3]);
            })?;
            tokio::select! {
                _=tick.tick()=>{},
                Some(checked)=check_rx.recv()=>{
                    match checked {
                        Ok(r)=>{let r:Readiness=r;selected=r.skills.iter().map(|s|s.id.clone()).collect(); ready=Some(r);stage=Stage::Skills;index=0;message.clear();status="选择加载 Skills".into();},
                        Err(e)=>{stage=Stage::Platform;message=format!("就绪检查失败：{e}");status="检查失败".into();}
                    }
                },
                Some(event)=evt_rx.recv()=>{
                    match event["kind"].as_str().unwrap_or("") {
                        "started"=>{connection_display=format!("模型 {} · 服务 {} · 已加载 {} 个 Skills",args.llm_model,event["connection"]["service"].as_str().unwrap_or("未知"),selected.len());stage=Stage::Chat;busy=false;status="Idle".into();history.push_str(&format!("会话：{}\n已加载：{}\nWorker 日志：tmp-episodes/microduck-tui-worker.log\n",event["connection"],event["loaded_skills"]));},
                        "planning"=>{status="请求模型规划".into();history.push_str(&format!("\n规划 API：{}\n",event["connection"]));},
                        "plan"=>{history.push_str(&format!("模型响应：{}\n工具调用：{}\n",event["evidence"]["trace"],event["evidence"]["tool_calls"]));},
                        "executing"=>{status="执行 Skill".into();},
                        "feedback"=>{status=format!("执行 {} · 请求模型反馈",event["status"]);},
                        "report"=>{busy=false;status="Idle".into();history.push_str(&format!("{}\n",report_text(&event["report"])));},
                        "closed"=>{if let Some(error)=event["error"].as_str(){message=error.into();}break;},
                        _=>{},
                    }
                    follow_tail = true;
                },
                maybe=events.next()=>{
                    let Some(Ok(Event::Key(key)))=maybe else{continue};
                    if key.kind!=KeyEventKind::Press{continue;}
                    let exit=key.code==KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL);
                    if exit || (key.code==KeyCode::Esc && stage==Stage::Body) {
                        if let Some(c)=&cancel {let c:&robo_archon_embodied::CancelToken=c;c.cancel();}
                        if worker.is_some(){let generation=interruption.fetch_add(1,Ordering::SeqCst)+1;let _=input_tx.send(("/quit".into(),generation));quitting=true;status="停车并退出…".into();}
                        else{break;}
                        continue;
                    }
                    if quitting{continue;}
                    if stage==Stage::Chat {
                        match key.code {
                            KeyCode::PageUp=>{follow_tail=false;scroll=scroll.saturating_sub(5);},
                            KeyCode::PageDown=>{follow_tail=false;scroll=scroll.saturating_add(5);},
                            KeyCode::Backspace=>{input.pop();},
                            KeyCode::Char(c)=>input.push(c),
                            KeyCode::Enter=>{
                                let text=input.trim().to_string();input.clear();
                                if text.is_empty(){continue;}
                                let control=["/stop","/reset","/quit"].contains(&text.as_str());
                                if busy && !control{message="等待当前动作完成，或输入 /stop".into();continue;}
                                if control {if let Some(c)=&cancel{c.cancel();}interruption.fetch_add(1,Ordering::SeqCst);}
                                let generation=interruption.load(Ordering::SeqCst);
                                history.push_str(&format!("\n你：{text}\n"));message.clear();busy=true;
                                if text=="/quit"{quitting=true;status="停车并退出…".into();}
                                input_tx.send((text,generation))?;
                            },_=>{},
                        }
                        continue;
                    }
                    let count=match stage{Stage::Body=>catalog.bodies.len(),Stage::Platform=>platforms.len(),Stage::Skills=>ready.as_ref().map_or(0,|r|r.skills.len()),_=>0};
                    match key.code {
                        KeyCode::Up=>{index=index.saturating_sub(1);},
                        KeyCode::Down=>{if count>0{index=(index+1).min(count-1);}},
                        KeyCode::Esc=>match stage {
                            Stage::Platform=>{stage=Stage::Body;index=body_index;},
                            Stage::Skills=>{stage=Stage::Platform;index=0;ready=None;},_=>{},
                        },
                        KeyCode::Char('v') if stage==Stage::Platform=>{args.viewer=!args.viewer;},
                        KeyCode::Char(' ') if stage==Stage::Skills=>{
                            if let Some(s)=ready.as_ref().and_then(|r|r.skills.get(index)){if !selected.remove(&s.id){selected.insert(s.id.clone());}}
                        },
                        KeyCode::Enter=>match stage {
                            Stage::Body=>{body_index=index;args.robot=Some(catalog.bodies[index].id.clone());platforms=catalog.bodies[index].bindings.keys().cloned().collect();index=platforms.iter().position(|p|p=="mujoco").unwrap_or(0);stage=Stage::Platform;status="选择仿真平台".into();message.clear();},
                            Stage::Platform=>{
                                let Some(platform)=platforms.get(index) else{message="该本体没有仿真绑定".into();continue;};
                                if args.robot.as_deref()!=Some("microduck") || platform!="mujoco"{message="本阶段 Skill 会话仅支持 Microduck / MuJoCo；其他本体的原有 CLI 仍可使用".into();continue;}
                                args.backend=platform.clone();stage=Stage::Checking;status="就绪检查".into();
                                let a=args.clone();let root=root.clone();let python=python.clone();let tx=check_tx.clone();
                                tokio::spawn(async move{
                                    let checked=async{
                                        let registry=SkillRegistry::load_dir(&a.skills_dir)?;
                                        let catalog=BodyCatalog::load(&a.body_catalog)?;
                                        skill_readiness::inspect(&python,&root,&a.skill_assets,&a.policy_packages,&registry,catalog,a.robot.as_deref().context("body")?,&a.backend).await
                                    }.await;
                                    let _=tx.send(checked);
                                });
                            },
                            Stage::Skills=>{
                                let client=match skill_session::client(&args){Ok(c)=>c,Err(e)=>{message=e.to_string();continue;}};
                                let r=ready.as_mut().context("readiness")?;
                                if let Err(e)=select_skills(&registry,r,args.robot.as_deref().context("body")?,&args.backend,&selected){message=e.to_string();continue;}
                                let r=ready.take().context("readiness")?;
                                let registry=SkillRegistry::load_dir(&args.skills_dir)?;
                                let executive=skill_commands::session_executive(&args);cancel=Some(executive.cancel.clone());
                                let rx=input_rx.take().context("input receiver")?;
                                let a=args.clone();let root=root.clone();let python=python.clone();let interruption=interruption.clone();let tx=evt_tx.clone();
                                stage=Stage::Starting;busy=true;
                                worker=Some(tokio::spawn(async move{
                                    let result=async{
                                        let runner=skill_commands::launch_session(&a,&root,&python,&r).await?;
                                        let _=tx.send(json!({"kind":"started","connection":client.connection_metadata(),"loaded_skills":r.skills.iter().map(|s|&s.id).collect::<Vec<_>>()}));
                                        skill_session::run_chat(&a,&registry,&r,client,executive,runner,rx,interruption,Some(tx.clone())).await
                                    }.await;
                                    let _=tx.send(json!({"kind":"closed","error":result.as_ref().err().map(|e:&anyhow::Error|e.to_string())}));
                                    result
                                }));
                            },_=>{},
                        },_=>{},
                    }
                }
            }
        }
        Ok(())
    }.await;
    if let Some(c) = cancel {
        c.cancel();
    }
    if let Some(worker) = worker {
        let generation = interruption.fetch_add(1, Ordering::SeqCst) + 1;
        let _ = input_tx.send(("/quit".into(), generation));
        let outcome = worker.await?;
        result?;
        outcome?;
    } else {
        result?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (SkillRegistry, Readiness) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let registry = SkillRegistry::load_dir(&root.join("skills")).unwrap();
        let skills = registry
            .list()
            .filter(|s| {
                [
                    "microduck.walk",
                    "microduck.stop",
                    "microduck.stand",
                    "microduck.patrol",
                ]
                .contains(&s.id.as_str())
            })
            .cloned()
            .collect();
        let ready = Readiness {
            catalog: BodyCatalog::load(&root.join("robots/catalog.json")).unwrap(),
            packages: std::collections::BTreeMap::new(),
            skills,
            rejected: Vec::new(),
        };
        (registry, ready)
    }
    #[test]
    fn loaded_skills_are_a_whitelist_and_composites_require_loaded_leaves() {
        let (registry, mut ready) = fixture();
        let incomplete = ["microduck.patrol".to_string(), "microduck.walk".to_string()]
            .into_iter()
            .collect();
        assert!(
            select_skills(&registry, &mut ready, "microduck", "mujoco", &incomplete)
                .unwrap_err()
                .to_string()
                .contains("microduck.stop")
        );
        assert_eq!(ready.skills.len(), 4);
        let selected = ["microduck.walk".to_string()].into_iter().collect();
        select_skills(&registry, &mut ready, "microduck", "mujoco", &selected).unwrap();
        assert_eq!(ready.skills.len(), 1);
        assert_eq!(ready.skills[0].id, "microduck.walk");
    }
    #[test]
    fn empty_or_unavailable_selection_never_loads() {
        let (registry, mut ready) = fixture();
        assert!(select_skills(
            &registry,
            &mut ready,
            "microduck",
            "mujoco",
            &BTreeSet::new()
        )
        .is_err());
        let selected = ["franka.home".to_string()].into_iter().collect();
        assert!(select_skills(&registry, &mut ready, "microduck", "mujoco", &selected).is_err());
    }
}
