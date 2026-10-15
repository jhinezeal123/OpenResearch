//! DeepSeek Harness through ACP. Catalog and effort choices are owned by DSH.
//! Configure its local launch descriptor with downstream/dsh/setup.mjs.
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};

use crate::downstream::bridge::error::{anyhow, Result};
use crate::downstream::bridge::harness::{ensure_playbook, PLAYBOOK_REL};
use crate::downstream::bridge::harness::{
    harness_log, prepare_env, set_chat_session_env, DeliveryState, PermissionDecision,
    PromptAnswer, ResumeCtx, TurnCtx, WirePart, WirePrompt,
};
use crate::downstream::bridge::harness::{
    Harness, HarnessAuthState, HarnessInfo, HarnessOptions, ModelInfo, OptionChoice,
    PermissionMode, ResumeAction, TurnFailure, TurnOutcome, TurnResult, TURN_WATCHDOG,
};

pub struct Dsh;

#[derive(Deserialize)]
struct Launch {
    executable: PathBuf,
    args: Vec<String>,
    home: PathBuf,
    version: Option<String>,
    default_model: Option<String>,
    default_effort: Option<String>,
}

fn descriptor_path() -> PathBuf {
    crate::downstream::bridge::store::data_dir().join("dsh.json")
}

fn launch() -> Result<Launch> {
    let config: Launch = serde_json::from_slice(&std::fs::read(descriptor_path())?)?;
    if !config.executable.is_file()
        || !config.home.is_dir()
        || config.args.is_empty()
        || matches!(
            config.executable.extension().and_then(|s| s.to_str()),
            Some("cmd" | "bat" | "ps1")
        )
    {
        return Err(anyhow!(
            "DSH requires a native executable/Node entrypoint and a configured ACP home"
        ));
    }
    Ok(config)
}

#[async_trait]
impl Harness for Dsh {
    fn id(&self) -> &'static str {
        "dsh"
    }
    fn name(&self) -> &'static str {
        "DSH"
    }
    fn supports_chat(&self) -> bool {
        true
    }
    fn session_skills_dir(&self) -> Option<&'static str> {
        Some(".agents/skills")
    }
    fn options(&self) -> HarnessOptions {
        let mut options = HarnessOptions::none();
        options.permission_modes = vec![
            OptionChoice::new("ask", "Ask for approval"),
            OptionChoice::new("bypass", "Bypass permissions"),
        ];
        options.default_permission_mode = Some("ask");
        options
    }
    async fn detect_snapshot(&self) -> Option<HarnessInfo> {
        let mut info = HarnessInfo::new(self.id(), self.name());
        match launch() {
            Ok(config) => {
                info.installed = true;
                info.bin_path = Some(config.executable.to_string_lossy().into_owned());
                info.version = config.version;
            }
            Err(_) => info.agent_note = Some("Configure DSH with downstream/dsh/setup.mjs, then Refresh. No automatic package downloads.".into()),
        }
        Some(info)
    }
    async fn detect(&self) -> Option<HarnessInfo> {
        let mut info = self.detect_snapshot().await?;
        if !info.installed {
            return Some(info);
        }
        match tokio::time::timeout(Duration::from_secs(180), discover()).await {
            Ok(Ok(models)) if !models.is_empty() => {
                info.models = models;
                // ACP catalog resolution checks configuration, not inference entitlement.
                info.authenticated = true;
                info.auth_state = HarnessAuthState::Ready;
                info.auth_method = Some("thirdParty");
                info.agent_ready = true;
                info.agent_note = Some(
                    "ACP configuration ready; model access is verified by an inference turn."
                        .into(),
                );
            }
            Ok(Err(error)) => {
                info.agent_note = Some(format!("DSH ACP catalog unavailable: {error}"))
            }
            _ => info.agent_note = Some(
                "DSH ACP catalog timed out or returned no models. Check the configured provider."
                    .into(),
            ),
        }
        Some(info)
    }
    async fn run_turn(&self, ctx: &mut TurnCtx) -> TurnResult {
        run_turn(ctx)
            .await
            .map(|()| TurnOutcome::Completed)
            .map_err(|error| TurnFailure::adapter(error, ctx.delivery_state()))
    }
    async fn resume_from_prompt(
        &self,
        ctx: &ResumeCtx,
        prompt: &WirePrompt,
        answer: &PromptAnswer,
    ) -> Result<ResumeAction> {
        if prompt.kind == "permission" {
            if let Some(id) = &prompt.native_id {
                if !ctx.is_busy().await {
                    return Err(anyhow!("DSH approval is no longer pending"));
                }
                let decision = if answer.approve {
                    PermissionDecision::Allow {
                        updated_input: prompt.tool_input.clone(),
                    }
                } else {
                    PermissionDecision::Deny {
                        message: "The user denied this action".into(),
                    }
                };
                ctx.host.settle_permission(id, decision)?;
                return Ok(ResumeAction::Handled { plan_mode: None });
            }
        }
        Ok(ResumeAction::Nothing)
    }
}

struct Acp {
    child: Child,
    stdin: ChildStdin,
    lines: Lines<BufReader<ChildStdout>>,
    counter: u64,
    closed: bool,
}

impl Acp {
    async fn start(cwd: &Path, session: Option<&TurnCtx>) -> Result<Self> {
        let config = launch()?;
        let mut cmd = Command::new(&config.executable);
        cmd.args(&config.args)
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(harness_log("dsh")?))
            .kill_on_drop(true);
        prepare_env(&mut cmd);
        cmd.env("DSH_HOME", &config.home).env("NO_COLOR", "1");
        if let Some(ctx) = session {
            set_chat_session_env(&mut cmd, &ctx.session_id, "dsh", ctx.host.up_port());
        }
        #[cfg(windows)]
        cmd.creation_flags(0x08000000);
        #[cfg(unix)]
        cmd.process_group(0);
        let mut child = cmd.spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("DSH stdin unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("DSH stdout unavailable"))?;
        let mut acp = Self {
            child,
            stdin,
            lines: BufReader::new(stdout).lines(),
            counter: 0,
            closed: false,
        };
        acp.call("initialize", json!({"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"openresearch","version":env!("CARGO_PKG_VERSION")}}), None).await?;
        Ok(acp)
    }
    async fn send(&mut self, value: Value) -> Result<()> {
        let data = serde_json::to_vec(&value)?;
        tokio::time::timeout(Duration::from_secs(15), async {
            self.stdin.write_all(&data).await?;
            self.stdin.write_all(b"\n").await?;
            self.stdin.flush().await
        })
        .await
        .map_err(|_| anyhow!("DSH stopped reading ACP input"))??;
        Ok(())
    }
    async fn call(
        &mut self,
        method: &str,
        params: Value,
        mut ctx: Option<&mut TurnCtx>,
    ) -> Result<Value> {
        self.counter += 1;
        let id = self.counter;
        self.send(json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))
            .await?;
        loop {
            let timeout = match method {
                "session/prompt" => TURN_WATCHDOG,
                "initialize" => Duration::from_secs(90),
                _ => Duration::from_secs(30),
            };
            let line = tokio::time::timeout(timeout, self.lines.next_line())
                .await
                .map_err(|_| anyhow!("DSH ACP timed out during {method}"))??
                .ok_or_else(|| anyhow!("DSH closed ACP during {method}; see agent-dsh.log"))?;
            if line.len() > 2_000_000 {
                return Err(anyhow!("DSH ACP message exceeded 2 MB"));
            }
            let message: Value = serde_json::from_str(&line)
                .map_err(|_| anyhow!("DSH returned invalid ACP JSON"))?;
            if let Some(request_id) = message
                .get("id")
                .filter(|_| message.get("method").is_some())
            {
                let reply = if message["method"] == "session/request_permission" {
                    permission(ctx.as_deref_mut(), &message["params"]).await?
                } else {
                    self.send(json!({"jsonrpc":"2.0","id":request_id,"error":{"code":-32601,"message":"Client capability not advertised"}})).await?;
                    continue;
                };
                self.send(json!({"jsonrpc":"2.0","id":request_id,"result":reply}))
                    .await?;
            } else if message["method"] == "session/update" {
                if let Some(ctx) = ctx.as_deref_mut() {
                    if method == "session/prompt" {
                        ctx.mark_delivery(DeliveryState::Accepted);
                    }
                    apply_update(ctx, &message["params"]["update"]);
                }
            } else if message.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(error) = message.get("error") {
                    return Err(anyhow!(
                        "DSH {method}: {}",
                        error["message"].as_str().unwrap_or("protocol error")
                    ));
                }
                return message
                    .get("result")
                    .cloned()
                    .ok_or_else(|| anyhow!("DSH ACP response lacks result"));
            }
        }
    }
    async fn close(&mut self, session_id: &str) -> Result<()> {
        self.call("session/close", json!({"sessionId":session_id}), None)
            .await?;
        self.closed = true;
        Ok(())
    }
}

impl Drop for Acp {
    fn drop(&mut self) {
        // Successful session/close already disposed tools. An aborted turn must
        // also stop owned shell descendants, not merely the JSON-RPC transport.
        if !self.closed {
            if let Some(pid) = self.child.id() {
                stop_process_tree(pid);
            }
        }
        let _ = self.child.start_kill();
    }
}

#[cfg(unix)]
fn stop_process_tree(pid: u32) {
    if let Ok(pid) = libc::pid_t::try_from(pid) {
        // SAFETY: start() created this child in its own process group.
        unsafe {
            libc::killpg(pid, libc::SIGKILL);
        }
    }
}

#[cfg(windows)]
fn stop_process_tree(pid: u32) {
    use std::os::windows::process::CommandExt;
    // As with the upstream Antigravity harness, experiment supervisors survive
    // stopping chat; cancelling a compute run is a separate OpenResearch action.
    let script = r#"
$processes = @(Get-CimInstance Win32_Process)
function Stop-DshProcess([uint32] $processId) {
    $children = @($processes | Where-Object { $_.ParentProcessId -eq $processId })
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        if ($child.Name -eq 'orx.exe' -and $child.CommandLine -match '^(?:"[^"\r\n]+"|\S+)\s+supervise\s') { continue }
        Stop-DshProcess $child.ProcessId
    }
}
Stop-DshProcess ([uint32] $env:ORX_DSH_STOP_PID)
"#;
    let _ = std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .env("ORX_DSH_STOP_PID", pid.to_string())
        .creation_flags(0x08000000)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn choices(option: &Value) -> Vec<(String, String)> {
    fn collect(options: &Value, result: &mut Vec<(String, String)>) {
        if let Some(rows) = options.as_array() {
            for row in rows {
                if let Some(value) = row["value"].as_str() {
                    result.push((value.into(), row["name"].as_str().unwrap_or(value).into()));
                }
                collect(&row["options"], result);
            }
        }
    }
    let mut result = Vec::new();
    collect(&option["options"], &mut result);
    result
}

fn option<'a>(session: &'a Value, category: &str) -> Option<&'a Value> {
    session["configOptions"]
        .as_array()?
        .iter()
        .find(|o| o["category"] == category || o["id"] == category)
}

fn model_info(id: &str, name: &str, session: &Value) -> ModelInfo {
    let mut model = ModelInfo::new(id).with_label(Some(name), None);
    model.description = Some(format!("DSH route: {id}"));
    // Empty explicitly suppresses the picker on models without advertised effort.
    let mut levels = vec![OptionChoice::new("default", "Agent default")];
    if let Some(effort) =
        option(session, "thought_level").or_else(|| option(session, "reasoning_effort"))
    {
        for (id, name) in choices(effort).into_iter().filter(|(id, _)| !id.is_empty()) {
            levels.push(OptionChoice::new(id, name));
        }
    }
    model.reasoning_levels = Some(if levels.len() > 1 { levels } else { Vec::new() });
    model
}

async fn discover() -> Result<Vec<ModelInfo>> {
    let cwd = crate::downstream::bridge::store::data_dir().join("agents/dsh/catalog");
    std::fs::create_dir_all(&cwd)?;
    let mut acp = Acp::start(&cwd, None).await?;
    let session = acp
        .call("session/new", json!({"cwd":cwd,"mcpServers":[]}), None)
        .await?;
    let sid = session["sessionId"]
        .as_str()
        .ok_or_else(|| anyhow!("DSH returned no session id"))?;
    let model = option(&session, "model")
        .ok_or_else(|| anyhow!("DSH did not advertise model configuration"))?;
    let mut models = Vec::new();
    for (id, name) in choices(model) {
        let selected = acp
            .call(
                "session/set_config_option",
                json!({"sessionId":sid,"configId":model["id"],"value":id}),
                None,
            )
            .await?;
        models.push(model_info(&id, &name, &selected));
    }
    acp.close(sid).await?;
    let current = model["currentValue"].as_str();
    models.sort_by_key(|m| current != Some(m.id.as_str()));
    Ok(models)
}

async fn run_turn(ctx: &mut TurnCtx) -> Result<()> {
    if ctx.plan_mode {
        return Err(anyhow!("DSH does not advertise an OpenResearch Plan mode"));
    }
    let project = ctx.project.clone();
    let session = ctx.session_id.clone();
    let (cwd, _) = tokio::task::spawn_blocking(move || {
        ensure_playbook(&project, &session, Some(".agents/skills"))
    })
    .await
    .map_err(|error| anyhow!("DSH playbook task: {error}"))??;
    let mut acp = Acp::start(&cwd, Some(ctx)).await?;
    let resumed = ctx.native_session_id.is_some();
    let mut session = if let Some(sid) = ctx.native_session_id.clone() {
        acp.call(
            "session/resume",
            json!({"sessionId":sid,"cwd":cwd,"mcpServers":[]}),
            None,
        )
        .await?
    } else {
        acp.call("session/new", json!({"cwd":cwd,"mcpServers":[]}), None)
            .await?
    };
    let sid = ctx
        .native_session_id
        .clone()
        .or_else(|| session["sessionId"].as_str().map(str::to_owned))
        .ok_or_else(|| anyhow!("DSH returned no session id"))?;
    ctx.set_native_session_id(&sid);
    if let Some(id) = ctx.model.clone() {
        let config =
            option(&session, "model").ok_or_else(|| anyhow!("DSH has no model selector"))?;
        if !choices(config).iter().any(|(value, _)| value == &id) {
            return Err(anyhow!("Model is not advertised by DSH"));
        }
        session = acp
            .call(
                "session/set_config_option",
                json!({"sessionId":sid,"configId":config["id"],"value":id}),
                None,
            )
            .await?;
    }
    let configured = launch()?;
    let selected_model = option(&session, "model").and_then(|o| o["currentValue"].as_str());
    let level = ctx
        .reasoning_level
        .as_deref()
        .filter(|s| *s != "default")
        .or_else(|| {
            (selected_model == configured.default_model.as_deref())
                .then_some(configured.default_effort.as_deref())
                .flatten()
        });
    if let Some(level) = level {
        let config = option(&session, "thought_level")
            .or_else(|| option(&session, "reasoning_effort"))
            .ok_or_else(|| anyhow!("This DSH model does not advertise reasoning effort"))?;
        if !choices(config).iter().any(|(id, _)| id == level) {
            return Err(anyhow!("DSH model does not support effort {level}"));
        }
        acp.call(
            "session/set_config_option",
            json!({"sessionId":sid,"configId":config["id"],"value":level}),
            None,
        )
        .await?;
    }
    let prompt = if resumed {
        ctx.text.clone()
    } else {
        format!(
            "Read and follow `{PLAYBOOK_REL}` before acting.\n\n{}",
            ctx.text
        )
    };
    ctx.persist_delivery(DeliveryState::Unknown)?;
    let result = acp
        .call(
            "session/prompt",
            json!({"sessionId":sid,"prompt":[{"type":"text","text":prompt}]}),
            Some(ctx),
        )
        .await?;
    if result["stopReason"] != "end_turn" {
        return Err(anyhow!(
            "DSH turn did not complete: {}",
            result["stopReason"]
        ));
    }
    acp.close(&sid).await?;
    ctx.mark_final_text_tail();
    ctx.flush()?;
    Ok(())
}

async fn permission(ctx: Option<&mut TurnCtx>, params: &Value) -> Result<Value> {
    let Some(ctx) = ctx else {
        return Ok(json!({"outcome":{"outcome":"cancelled"}}));
    };
    let call_id = params["toolCall"]["toolCallId"].as_str().unwrap_or("");
    let part = ctx
        .assistant
        .parts
        .iter()
        .find(|p| p.id == format!("dsh-tool-{call_id}"));
    let tool = part
        .and_then(|p| p.tool.as_deref())
        .unwrap_or("DSH tool")
        .to_owned();
    let input = part
        .and_then(|p| p.state.as_ref())
        .and_then(|s| s.input.clone())
        .unwrap_or(Value::Null);
    let token = ctx.host.mint_gate_token(
        &ctx.session_id,
        false,
        ctx.permission_mode == Some(PermissionMode::Bypass),
    );
    ctx.flush()?;
    let decision = ctx
        .host
        .request_permission(&ctx.session_id, &token, &tool, input)
        .await?;
    let kind = match decision {
        PermissionDecision::Allow { .. } => "allow_once",
        PermissionDecision::Deny { .. } => "reject_once",
    };
    let selected = params["options"]
        .as_array()
        .and_then(|rows| rows.iter().find(|o| o["kind"] == kind))
        .and_then(|o| o.get("optionId"));
    Ok(match selected {
        Some(id) => json!({"outcome":{"outcome":"selected","optionId":id}}),
        None => json!({"outcome":{"outcome":"cancelled"}}),
    })
}

fn apply_update(ctx: &mut TurnCtx, update: &Value) {
    match update["sessionUpdate"].as_str().unwrap_or("") {
        "agent_message_chunk" | "agent_thought_chunk" => {
            if let Some(text) = update["content"]["text"].as_str() {
                let thought = update["sessionUpdate"] == "agent_thought_chunk";
                let id = if thought { "dsh-thought" } else { "dsh-text" };
                if ctx.assistant.parts.iter().any(|p| p.id == id) {
                    ctx.append_part_text(id, text);
                } else {
                    ctx.upsert_part(if thought {
                        WirePart::reasoning(id, text)
                    } else {
                        WirePart::text(id, text)
                    });
                }
            }
        }
        "tool_call" | "tool_call_update" => {
            if let Some(call) = update["toolCallId"].as_str() {
                let id = format!("dsh-tool-{call}");
                let mut part = ctx
                    .assistant
                    .parts
                    .iter()
                    .find(|p| p.id == id)
                    .cloned()
                    .unwrap_or_else(|| {
                        WirePart::tool(
                            &id,
                            update["title"].as_str().unwrap_or("DSH tool"),
                            "running",
                            None,
                        )
                    });
                if let Some(state) = part.state.as_mut() {
                    if let Some(status) = update["status"].as_str() {
                        state.status = match status {
                            "completed" => "completed",
                            "failed" => "error",
                            _ => "running",
                        }
                        .into();
                    }
                    if let Some(input) = update.get("rawInput") {
                        state.input = Some(input.clone());
                    }
                    if let Some(output) = update.get("rawOutput").or_else(|| update.get("content"))
                    {
                        state.output = Some(
                            output
                                .as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| output.to_string()),
                        );
                    }
                    if let Some(title) = update["title"].as_str() {
                        state.title = Some(title.into());
                    }
                }
                ctx.upsert_part(part);
            }
        }
        _ => {}
    }
    ctx.maybe_flush();
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn grouped_catalog_and_exact_model_efforts() {
        let session = json!({"configOptions":[{"id":"model","options":[{"group":"Provider","options":[{"value":"[\"p\",\"m\"]","name":"Model"}]}]},{"id":"reasoning_effort","options":[{"value":"high","name":"High"}]}]});
        assert_eq!(
            choices(option(&session, "model").unwrap()),
            vec![("[\"p\",\"m\"]".into(), "Model".into())]
        );
        let model = model_info("m", "Model", &session);
        let levels = model.reasoning_levels.unwrap();
        assert_eq!(
            levels.iter().map(|x| x.id.as_str()).collect::<Vec<_>>(),
            ["default", "high"]
        );
        assert!(model_info("plain", "Plain", &json!({"configOptions":[]}))
            .reasoning_levels
            .unwrap()
            .is_empty());
    }
    #[tokio::test]
    async fn catalog_cannot_approve_tools() {
        assert_eq!(
            permission(None, &json!({})).await.unwrap()["outcome"]["outcome"],
            "cancelled"
        );
    }
}
