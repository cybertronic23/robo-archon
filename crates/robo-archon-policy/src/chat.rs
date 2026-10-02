//! Lightweight OpenAI-compatible Chat Completions via `curl`
//! (avoids pulling reqwest into the offline-vendored workspace).

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone)]
pub struct ChatClient {
    api_key: String,
    base_url: String,
    model: String,
}

impl ChatClient {
    pub fn new(
        api_key: impl Into<String>,
        base_url: impl Into<String>,
        model: impl Into<String>,
    ) -> Self {
        Self {
            api_key: api_key.into(),
            base_url: base_url.into().trim_end_matches('/').to_string(),
            model: model.into(),
        }
    }

    pub fn deepseek(api_key: impl Into<String>) -> Self {
        Self::new(api_key, "https://api.deepseek.com", "deepseek-chat")
    }

    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model = model.into();
        self
    }

    pub async fn chat(&self, system: &str, user: &str) -> Result<String> {
        let url = format!("{}/v1/chat/completions", self.base_url);
        let body = ChatRequest {
            model: self.model.clone(),
            temperature: 0.2,
            messages: vec![
                ChatMessage {
                    role: "system".into(),
                    content: system.into(),
                },
                ChatMessage {
                    role: "user".into(),
                    content: user.into(),
                },
            ],
        };
        let body_json = serde_json::to_string(&body).context("serialize chat request")?;
        let auth = format!("Authorization: Bearer {}", self.api_key);

        let output = tokio::process::Command::new("curl")
            .args([
                "-fsSL",
                "-X",
                "POST",
                &url,
                "-H",
                &auth,
                "-H",
                "Content-Type: application/json",
                "-d",
                &body_json,
            ])
            .output()
            .await
            .context("spawn curl for LLM request")?;

        if !output.status.success() {
            let err = String::from_utf8_lossy(&output.stderr);
            bail!("curl LLM request failed: {err}");
        }

        let text = String::from_utf8(output.stdout).context("LLM response utf8")?;
        let parsed: ChatResponse =
            serde_json::from_str(&text).with_context(|| format!("parse LLM JSON: {text}"))?;
        let content = parsed
            .choices
            .first()
            .and_then(|c| c.message.content.clone())
            .context("LLM returned empty choices")?;
        Ok(content)
    }
}

#[derive(Serialize)]
struct ChatRequest {
    model: String,
    temperature: f64,
    messages: Vec<ChatMessage>,
}

#[derive(Serialize)]
struct ChatMessage {
    role: String,
    content: String,
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

#[derive(Deserialize)]
struct ChatChoice {
    message: ChatMessageDe,
}

#[derive(Deserialize)]
struct ChatMessageDe {
    content: Option<String>,
}

/// Strip optional ```json fences.
pub fn extract_json_object(raw: &str) -> Result<String> {
    let t = raw.trim();
    if let Some(start) = t.find('{') {
        if let Some(end) = t.rfind('}') {
            return Ok(t[start..=end].to_string());
        }
    }
    bail!("no JSON object found in LLM output: {raw}");
}

impl ChatClient {
    /// Bounded structured tool transport. Credentials stay off process arguments and reports.
    pub async fn messages(
        &self,
        messages: Vec<serde_json::Value>,
        tools: Option<serde_json::Value>,
    ) -> Result<serde_json::Value> {
        use std::process::Stdio;
        use tokio::io::AsyncWriteExt;
        if self.api_key.is_empty() || self.api_key.contains(['\r', '\n']) {
            bail!("invalid API key");
        }
        if !self.base_url.starts_with("https://") && !self.base_url.starts_with("http://") {
            bail!("LLM base URL requires HTTP(S)");
        }
        let url = format!("{}/chat/completions", self.base_url);
        let mut body =
            serde_json::json!({"model":self.model,"temperature":0.2,"messages":messages});
        if let Some(tools) = tools {
            body["tools"] = tools;
            body["tool_choice"] = serde_json::json!("auto");
        }
        let quoted = |value: &str| serde_json::to_string(value);
        let config = format!(
            "url = {}\nheader = {}\nheader = \"Content-Type: application/json\"\ndata = {}\n",
            quoted(&url)?,
            quoted(&format!("Authorization: Bearer {}", self.api_key))?,
            quoted(&body.to_string())?
        );
        let mut child = tokio::process::Command::new("curl")
            .args([
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "10",
                "--max-time",
                "30",
                "--config",
                "-",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()?;
        let mut input = child.stdin.take().context("curl stdin missing")?;
        input.write_all(config.as_bytes()).await?;
        drop(input);
        let output =
            tokio::time::timeout(std::time::Duration::from_secs(35), child.wait_with_output())
                .await
                .context("LLM request timed out")??;
        if !output.status.success() {
            bail!("LLM HTTP transport failed ({})", output.status);
        }
        let response: serde_json::Value =
            serde_json::from_slice(&output.stdout).context("invalid LLM response JSON")?;
        let message = response["choices"][0]["message"].clone();
        if message["role"] != "assistant" {
            bail!("LLM response has no assistant message");
        }
        Ok(message)
    }
}
