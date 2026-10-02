//! LLM proposes one capability call; host validation and Executive retain authority.
use crate::chat::ChatClient;
use anyhow::{bail, Context, Result};
use robo_archon_skills::{SkillCall, SkillManifest};
use serde_json::{json, Value};

pub struct SkillDecision {
    pub assistant: Value,
    pub call: Option<SkillCall>,
    pub refusal: Option<String>,
}

pub fn tools(skills: &[SkillManifest]) -> Value {
    json!(skills.iter().map(|s| {let t=s.tool_definition();json!({"type":"function","function":{"name":t.name,"description":t.description,"parameters":t.input_schema}})}).collect::<Vec<_>>())
}

pub fn parse_decision(assistant: Value, skills: &[SkillManifest]) -> Result<SkillDecision> {
    let calls = assistant.get("tool_calls").and_then(Value::as_array);
    if calls.is_none_or(|c| c.is_empty()) {
        let refusal = assistant["content"]
            .as_str()
            .filter(|s| !s.trim().is_empty())
            .context("empty model decision")?
            .to_string();
        return Ok(SkillDecision {
            assistant,
            call: None,
            refusal: Some(refusal),
        });
    }
    let calls = calls.unwrap();
    if calls.len() != 1 {
        bail!("exactly one skill call is allowed; composition is a later milestone");
    }
    let call = &calls[0];
    if call["type"] != "function" || call["id"].as_str().is_none_or(str::is_empty) {
        bail!("invalid function call");
    }
    let name = call["function"]["name"]
        .as_str()
        .context("function name missing")?;
    let skill = skills
        .iter()
        .find(|s| s.tool_name == name)
        .context("model selected an unavailable tool")?;
    let parameters: Value = serde_json::from_str(
        call["function"]["arguments"]
            .as_str()
            .context("arguments must be JSON text")?,
    )?;
    if !parameters.is_object() {
        bail!("skill arguments must be an object");
    }
    let proposed = SkillCall {
        skill_id: skill.id.clone(),
        parameters,
        timeout_ms: skill.max_duration_ms,
    };
    Ok(SkillDecision {
        assistant,
        call: Some(proposed),
        refusal: None,
    })
}

pub fn system_prompt() -> &'static str {
    "Select at most one available robot skill for this instruction. If it is unsupported, explain why without calling a tool. Do not invent capabilities, goal completion, or exact speed tracking. Tools describe bounded input commands; duration completion may occur with little motion. Only use the supplied tool descriptions and schemas. You cannot alter policy, runner, contracts or time budget."
}

pub async fn propose(
    client: &ChatClient,
    instruction: &str,
    skills: &[SkillManifest],
) -> Result<SkillDecision> {
    if skills.is_empty() {
        bail!("no executable skills available");
    }
    let assistant = client
        .messages(
            vec![
                json!({"role":"system","content":system_prompt()}),
                json!({"role":"user","content":instruction}),
            ],
            Some(tools(skills)),
        )
        .await?;
    parse_decision(assistant, skills)
}

pub async fn feedback(
    client: &ChatClient,
    instruction: &str,
    decision: &SkillDecision,
    result: &Value,
) -> Result<String> {
    let id = decision.assistant["tool_calls"][0]["id"]
        .as_str()
        .context("call ID missing")?;
    let assistant=client.messages(vec![json!({"role":"system","content":"Explain the measured skill result briefly. Use status, fault, measured pose/velocity, motion samples and stop confirmation. Do not claim requested speed/distance was achieved without evidence. Do not request another action."}),json!({"role":"user","content":instruction}),decision.assistant.clone(),json!({"role":"tool","tool_call_id":id,"content":result.to_string()})],None).await?;
    if assistant
        .get("tool_calls")
        .and_then(Value::as_array)
        .is_some_and(|c| !c.is_empty())
    {
        bail!("feedback must not propose another call");
    }
    Ok(assistant["content"]
        .as_str()
        .context("feedback text missing")?
        .to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn skills() -> Vec<SkillManifest> {
        vec![
            serde_json::from_str(include_str!("../../../skills/microduck-walk/skill.json"))
                .unwrap(),
        ]
    }
    fn response(name: &str, args: &str) -> Value {
        json!({"role":"assistant","tool_calls":[{"id":"call1","type":"function","function":{"name":name,"arguments":args}}]})
    }
    #[test]
    fn refuses_unknown_malformed_or_parallel_calls() {
        let s = skills();
        assert!(parse_decision(response("shell", "{}"), &s).is_err());
        assert!(parse_decision(response("microduck_walk", "[]"), &s).is_err());
        let mut r = response("microduck_walk", "{}");
        let duplicate = r["tool_calls"][0].clone();
        r["tool_calls"].as_array_mut().unwrap().push(duplicate);
        assert!(parse_decision(r, &s).is_err());
        assert!(
            parse_decision(json!({"role":"assistant","content":"Unsupported"}), &s)
                .unwrap()
                .call
                .is_none()
        );
    }
    #[test]
    fn uses_registry_names_and_host_budget() {
        let mut s = skills();
        s[0].id = "user.demo".into();
        s[0].tool_name = "user_demo".into();
        let d = parse_decision(response("user_demo", r#"{"vx":0.4}"#), &s).unwrap();
        assert_eq!(d.call.unwrap().skill_id, "user.demo");
        assert_eq!(tools(&s)[0]["function"]["name"], "user_demo");
    }
}
