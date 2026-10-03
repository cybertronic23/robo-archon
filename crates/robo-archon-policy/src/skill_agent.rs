//! LLM proposes one capability call; host validation and Executive retain authority.
use crate::chat::ChatClient;
use anyhow::{bail, Context, Result};
use robo_archon_skills::{SkillCall, SkillManifest};
use serde_json::{json, Value};

pub struct SkillDecision {
    pub assistant: Value,
    pub call: Option<SkillCall>,
    pub refusal: Option<String>,
    pub sequence: Option<robo_archon_skills::SkillSequence>,
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
            sequence: None,
        });
    }
    let calls = calls.unwrap();
    if calls.len() != 1 {
        bail!("exactly one tool call is allowed; use archon_sequence for ordered composition");
    }
    let call = &calls[0];
    if call["type"] != "function" || call["id"].as_str().is_none_or(str::is_empty) {
        bail!("invalid function call");
    }
    let name = call["function"]["name"]
        .as_str()
        .context("function name missing")?;
    if name == "archon_sequence" && skills.len() > 1 {
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Step {
            tool_name: String,
            parameters: Value,
        }
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Arguments {
            steps: Vec<Step>,
        }
        let args: Arguments = serde_json::from_str(
            call["function"]["arguments"]
                .as_str()
                .context("arguments required")?,
        )?;
        if args.steps.is_empty() || args.steps.len() > 16 {
            bail!("sequence needs 1..16 steps");
        }
        let mut steps = Vec::new();
        for step in args.steps {
            let skill = skills
                .iter()
                .find(|s| s.tool_name == step.tool_name)
                .context("sequence selected unavailable tool")?;
            if !step.parameters.is_object() {
                bail!("step parameters must be object");
            }
            steps.push(SkillCall {
                skill_id: skill.id.clone(),
                parameters: step.parameters,
                timeout_ms: skill.max_duration_ms,
            });
        }
        let budget = steps
            .iter()
            .try_fold(0u64, |sum, s| sum.checked_add(s.timeout_ms))
            .context("budget overflow")?;
        if budget > 120000 {
            bail!("sequence exceeds 120 second budget");
        }
        return Ok(SkillDecision {
            assistant,
            call: None,
            refusal: None,
            sequence: Some(robo_archon_skills::SkillSequence {
                schema_version: 1,
                timeout_ms: budget,
                steps,
            }),
        });
    }
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
        sequence: None,
    })
}

pub fn planning_tools(skills: &[SkillManifest]) -> Value {
    let mut value = tools(skills);
    if skills.len() > 1 {
        value.as_array_mut().unwrap().push(json!({"type":"function","function":{"name":"archon_sequence","description":"Execute an ordered sequence of registered skills in the SAME simulation. Every step is validated before any motion; stop on failure. Use for instructions with multiple ordered actions. Use only names and parameters from the other tool definitions.","parameters":{"type":"object","additionalProperties":false,"required":["steps"],"properties":{"steps":{"type":"array","minItems":1,"maxItems":16,"items":{"type":"object","additionalProperties":false,"required":["tool_name","parameters"],"properties":{"tool_name":{"type":"string","enum":skills.iter().map(|s| &s.tool_name).collect::<Vec<_>>()},"parameters":{"type":"object"}}}}}}}}));
    }
    value
}

pub fn system_prompt() -> &'static str {
    "Select one available robot skill or archon_sequence for ordered multi-step instructions. Never use parallel tool calls. If it is unsupported, explain why without calling a tool. Do not invent capabilities, goal completion, or exact speed tracking. Tools describe bounded input commands; duration completion may occur with little motion. Only use the supplied tool descriptions and schemas. You cannot alter policy, runner, contracts or time budget."
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
            Some(planning_tools(skills)),
        )
        .await?;
    parse_decision(assistant, skills)
}

pub async fn propose_with_context(
    client: &ChatClient,
    instruction: &str,
    skills: &[SkillManifest],
    measured_history: &Value,
) -> Result<SkillDecision> {
    if skills.is_empty() {
        bail!("no executable skills available");
    }
    let assistant = client.messages(vec![json!({"role":"system","content":system_prompt()}), json!({"role":"user","content":json!({"instruction":instruction,"previous_measured_results":measured_history}).to_string()})], Some(planning_tools(skills))).await?;
    parse_decision(assistant, skills)
}

pub async fn feedback(
    client: &ChatClient,
    instruction: &str,
    decision: &SkillDecision,
    result: &Value,
) -> Result<String> {
    let evidence = feedback_evidence(client, instruction, decision, result).await?;
    Ok(evidence["text"]
        .as_str()
        .context("feedback text missing")?
        .to_string())
}

pub async fn feedback_evidence(
    client: &ChatClient,
    instruction: &str,
    decision: &SkillDecision,
    result: &Value,
) -> Result<Value> {
    let id = decision.assistant["tool_calls"][0]["id"]
        .as_str()
        .context("call ID missing")?;
    let mut planning_message = decision.assistant.clone();
    planning_message
        .as_object_mut()
        .context("assistant message")?
        .remove("_archon_trace");
    let assistant=client.messages(vec![json!({"role":"system","content":"Explain the measured skill result briefly in the user's language. Use status, fault, measured pose/velocity, motion samples and stop confirmation. Do not claim requested speed/distance was achieved without evidence. Do not request another action."}),json!({"role":"user","content":instruction}),planning_message,json!({"role":"tool","tool_call_id":id,"content":result.to_string()})],None).await?;
    if assistant
        .get("tool_calls")
        .and_then(Value::as_array)
        .is_some_and(|c| !c.is_empty())
    {
        bail!("feedback must not propose another call");
    }
    let text = assistant["content"]
        .as_str()
        .context("feedback text missing")?;
    Ok(json!({"text":text,"trace":assistant["_archon_trace"]}))
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

#[cfg(test)]
mod sequence_tests {
    use super::*;
    #[test]
    fn sequential_tool_uses_registry_names_and_host_budgets() {
        let mut first: SkillManifest =
            serde_json::from_str(include_str!("../../../skills/microduck-walk/skill.json"))
                .unwrap();
        let second = first.clone();
        first.id = "user.other".into();
        first.tool_name = "user_other".into();
        let skills = vec![first, second];
        let response = json!({"role":"assistant","tool_calls":[{"id":"p","type":"function","function":{"name":"archon_sequence","arguments":json!({"steps":[{"tool_name":"user_other","parameters":{"vx":0.4}},{"tool_name":"microduck_walk","parameters":{"vx":0.3,"yaw_rate":1.0}}]}).to_string()}}]});
        let plan = parse_decision(response.clone(), &skills)
            .unwrap()
            .sequence
            .unwrap();
        assert_eq!(plan.steps[0].skill_id, "user.other");
        assert_eq!(plan.timeout_ms, 20000);
        let mut bad = response;
        bad["tool_calls"][0]["function"]["arguments"] =
            json!(json!({"steps":[{"tool_name":"shell","parameters":{}}]}).to_string());
        assert!(parse_decision(bad, &skills).is_err());
    }
}
