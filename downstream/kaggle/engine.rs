//! The run engine's Kaggle adapter.
//!
//! It drives the same acquire → launch → inspect sequence as the 0.2.x supervisor, one bridge
//! operation at a time. Every operation is keyed by the run ID on both sides (the pool's
//! assignment and the remote run directory), so a step lost to a restart is simply repeated:
//! the remote launch intent keeps a command from ever running twice.
//!
//! A bridge operation can outlast the engine's observation budget (account probes, a source
//! upload), so each step runs in the background and `inspect` reports the latest result.
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use async_trait::async_trait;
use base64::Engine as _;
use serde_json::{json, Value};

use crate::downstream::bridge::engine::*;
use crate::downstream::kaggle::runtime::{self, Launch};

pub(crate) const KIND: &str = "kaggle_job";
/// How long `inspect` waits on a step before reporting the previous one; under the engine's
/// ten-second observation budget.
const STEP_WAIT: Duration = Duration::from_secs(7);
const LOG_POLL: Duration = Duration::from_secs(5);
/// An `inspect` offset past any log, for a status check that transfers no log bytes. Kept
/// well under Linux's maximum file size: notebooks predating the remote's bounds check seek
/// to it, and a larger offset fails there with EINVAL.
const NO_LOG: u64 = 1 << 40;

#[derive(Default)]
pub(crate) struct KaggleAccount {
    drivers: Mutex<HashMap<String, Driver>>,
}

/// What this process learned about a run. Lost on restart, which only repeats steps.
#[derive(Default)]
struct Driver {
    acquired: bool,
    launched: bool,
    last: Option<Observation>,
    step: Option<tokio::task::JoinHandle<Step>>,
}

struct Step {
    acquired: bool,
    launched: bool,
    observation: Observation,
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Every Kaggle operation names the run, not a job ID: the session it leases changes, and a
/// 0.2.x descriptor's `jobId` holds that session.
fn check(run: &WatchedRun) -> Result<()> {
    let ours = match &run.handle {
        Handle::Api { provider, .. } => provider == KIND,
        Handle::Legacy { descriptor } => descriptor.kind == KIND,
        _ => false,
    };
    if ours {
        Ok(())
    } else {
        Err(anyhow!("Run {} is not a Kaggle run", run.id))
    }
}

/// Written before a cancel is sent, so no later step acquires a notebook for the run again.
fn cancel_marker(run_id: &str) -> Result<PathBuf> {
    let id = uuid::Uuid::parse_str(run_id)?;
    Ok(runtime::root().join("runs").join(format!("{id}.cancel")))
}

/// The bridge request for a run: its saved launch plus where its source snapshot lives. The
/// environment, which may carry synced secrets, is added only for the launch itself.
fn request(run_id: &str, with_env: bool) -> Result<(Launch, Value)> {
    let launch = Launch::load(run_id)
        .with_context(|| format!("Kaggle launch for run {run_id} is missing"))?;
    if !launch.pooled {
        return Err(anyhow!(
            "Run {run_id} uses the old one-run Kaggle notebook; inspect its saved status before restarting."
        ));
    }
    let run = Store::open()?
        .get_run(run_id)?
        .ok_or_else(|| anyhow!("Run {run_id} not found in the local store."))?;
    let descriptor = BackendDescriptor::parse(&run.backend_json)?;
    let source = SourceSnapshot::from_run(&run, &descriptor)?;
    let mut request = serde_json::to_value(&launch)?;
    request["projectId"] = json!(run.project_id);
    request["experimentId"] = json!(run.experiment_id);
    request["sourcePath"] = json!(source.path);
    request["digest"] = json!(source.digest);
    request["offset"] = json!(NO_LOG);
    if with_env {
        request["env"] = json!(crate::downstream::bridge::config::run_env(run_id));
    }
    Ok((launch, request))
}

async fn operation(launch: &Launch, request: &Value, action: &str) -> Result<Value> {
    let mut request = request.clone();
    request["action"] = json!(action);
    let seconds = if action == "launch" { 300 } else { 90 };
    runtime::call(&launch.runtime, request, seconds).await
}

fn note(reply: &Value, fallback: &str) -> String {
    reply["error"]
        .as_str()
        .or(reply["detail"].as_str())
        .unwrap_or(fallback)
        .to_owned()
}

fn waiting(reply: &Value) -> Observation {
    let detail = match (reply["detail"].as_str(), reply["error"].as_str()) {
        (Some(detail), Some(error)) => format!("{detail} {error}"),
        (Some(detail), None) => detail.to_owned(),
        (None, Some(error)) => format!("Kaggle notebook not ready: {error}"),
        (None, None) => "Waiting for the Kaggle notebook".to_owned(),
    };
    Observation::Queued { detail }
}

/// Reads a remote run's state from a launch, inspect or cancel reply.
fn observe(reply: &Value, step: &mut Step) -> Observation {
    if reply["stopped"] == true {
        // The notebook is gone, and the run's processes with it.
        return Observation::Gone {
            reason: format!(
                "The Kaggle session ended before this run completed; its command was not replayed. {}",
                note(reply, "")
            )
            .trim_end()
            .to_owned(),
        };
    }
    if reply["pending"] == true || reply["queued"] == true {
        return waiting(reply);
    }
    match reply["stage"].as_str().unwrap_or("RUNNING") {
        "COMPLETE" | "ERROR" => Observation::Exited {
            finished_at: None,
            code: reply["exitCode"].as_i64().map(|code| code as i32),
            signal: None,
        },
        "NOT_STARTED" => {
            step.launched = false;
            Observation::Queued {
                detail: "The Kaggle notebook is ready; the run has not started yet.".into(),
            }
        }
        "UNKNOWN" => Observation::Unreconciled {
            detail: note(
                reply,
                "Kaggle has no acknowledgement of the launch; the command will not be replayed.",
            ),
        },
        _ => Observation::Running { started_at: None },
    }
}

/// Why a step could not finish: local state waiting cannot repair, or Kaggle out of reach.
enum Failure {
    Local(crate::downstream::bridge::error::Error),
    Remote(crate::downstream::bridge::error::Error),
}

fn local(run_id: &str, with_env: bool) -> Result<(Launch, Value), Failure> {
    request(run_id, with_env).map_err(Failure::Local)
}

async fn remote(launch: &Launch, request: &Value, action: &str) -> Result<Value, Failure> {
    operation(launch, request, action)
        .await
        .map_err(Failure::Remote)
}

async fn drive(run_id: &str, cancelled: bool, step: &mut Step) -> Result<Observation, Failure> {
    if cancelled {
        // Idempotent: it re-reads the remote run, or ends a run still waiting for a notebook.
        let (launch, request) = local(run_id, false)?;
        return Ok(observe(&remote(&launch, &request, "cancel").await?, step));
    }
    if !step.acquired {
        let (launch, request) = local(run_id, false)?;
        let reply = remote(&launch, &request, "acquire").await?;
        if reply["stopped"] == true {
            // Before any launch: every account is out of quota, or the session already ended.
            return Ok(Observation::Failed {
                finished_at: None,
                reason: note(&reply, "The Kaggle session ended before this run started."),
            });
        }
        if reply["ready"] != true {
            return Ok(waiting(&reply));
        }
        step.acquired = true;
    }
    if !step.launched {
        let (launch, request) = local(run_id, true)?;
        let reply = remote(&launch, &request, "launch").await?;
        step.launched = reply["pending"] != true && reply["stopped"] != true;
        return Ok(observe(&reply, step));
    }
    let (launch, request) = local(run_id, false)?;
    Ok(observe(&remote(&launch, &request, "inspect").await?, step))
}

async fn run_step(run_id: String, cancelled: bool, acquired: bool, launched: bool) -> Step {
    let mut step = Step {
        acquired,
        launched,
        observation: Observation::Queued {
            detail: "Contacting Kaggle".into(),
        },
    };
    step.observation = match drive(&run_id, cancelled, &mut step).await {
        Ok(observation) => observation,
        Err(Failure::Local(error)) => Observation::Unreconciled {
            detail: error.to_string(),
        },
        Err(Failure::Remote(error)) => Observation::Unreachable {
            cause: format!("Kaggle monitoring will retry the same session: {error}"),
        },
    };
    step
}

impl KaggleAccount {
    fn forget(&self, run_id: &str) {
        if let Some(driver) = lock(&self.drivers).remove(run_id) {
            if let Some(step) = driver.step {
                step.abort();
            }
        }
    }
}

#[async_trait]
impl Api for KaggleAccount {
    type Cursor = u64;

    /// Records the launch locally; the next observation leases a notebook and starts it.
    async fn launch(&self, intent: &RunIntent, fence: &Fence<'_>) -> Result<Handle, LaunchError> {
        let launch: Launch = serde_json::from_value(intent.launch.options["launch"].clone())
            .map_err(api::rejected)?;
        if launch.run_id != intent.run.id {
            return Err(api::rejected("Kaggle launch belongs to another run"));
        }
        if fence.cancelled() {
            return Err(api::rejected(
                "The run was cancelled before Kaggle received it.",
            ));
        }
        launch.save().map_err(api::rejected)?;
        Ok(api::handle(intent, vec![intent.run.id.clone()]))
    }

    async fn recover(&self, intent: &RunIntent) -> Recovery {
        match Launch::saved(&intent.run.id) {
            Ok(true) => Recovery::Found(api::handle(intent, vec![intent.run.id.clone()])),
            Ok(false) => Recovery::Absent,
            Err(error) => api::unknown(error),
        }
    }

    async fn inspect(&self, run: &WatchedRun) -> Result<Observation> {
        check(run)?;
        let cancelled = cancel_marker(&run.id)?.exists();
        let mut task = {
            let mut drivers = lock(&self.drivers);
            let driver = drivers.entry(run.id.clone()).or_default();
            match driver.step.take() {
                Some(task) => task,
                None => tokio::spawn(run_step(
                    run.id.clone(),
                    cancelled,
                    driver.acquired,
                    driver.launched,
                )),
            }
        };
        let finished = tokio::time::timeout(STEP_WAIT, &mut task).await;
        let observation = {
            let mut drivers = lock(&self.drivers);
            let driver = drivers.entry(run.id.clone()).or_default();
            match finished {
                Ok(Ok(step)) => {
                    driver.acquired = step.acquired;
                    driver.launched = step.launched;
                    driver.last = Some(step.observation.clone());
                    step.observation
                }
                Ok(Err(error)) => return Err(anyhow!("Kaggle monitoring stopped: {error}")),
                Err(_) => {
                    driver.step = Some(task);
                    driver.last.clone().unwrap_or(Observation::Queued {
                        detail: "Contacting Kaggle".into(),
                    })
                }
            }
        };
        if observation.ends_run() {
            self.forget(&run.id);
        }
        Ok(observation)
    }

    async fn cancel(&self, run: &WatchedRun) -> Result<()> {
        check(run)?;
        let marker = cancel_marker(&run.id)?;
        if let Some(runs) = marker.parent() {
            std::fs::create_dir_all(runs)?;
        }
        std::fs::write(
            &marker,
            crate::downstream::bridge::store::now_ms().to_string(),
        )?;
        let (launch, request) = request(&run.id, false)?;
        operation(&launch, &request, "cancel").await?;
        Ok(())
    }

    /// Frees the notebook for the next run only once Kaggle reports this one finished.
    async fn finish(&self, run: &WatchedRun) -> Result<()> {
        check(run)?;
        self.forget(&run.id);
        let (launch, request) = request(&run.id, false)?;
        let reply = operation(&launch, &request, "release").await?;
        if reply["released"] == true || reply["stopped"] == true {
            Ok(())
        } else {
            Err(anyhow!(
                "Kaggle has not confirmed that run {} finished; its notebook stays reserved until it does.",
                run.id
            ))
        }
    }

    async fn logs(
        &self,
        run: &WatchedRun,
        cursor: &mut u64,
        window: Duration,
        sink: &mut (dyn for<'b> FnMut(&'b [u8]) + Send),
    ) -> Result<bool> {
        check(run)?;
        let deadline = tokio::time::Instant::now() + window;
        let (launch, mut request) = request(&run.id, false)?;
        let mut received = false;
        loop {
            request["offset"] = json!(*cursor);
            let reply = match operation(&launch, &request, "inspect").await {
                Ok(reply) => reply,
                Err(error) if !received => return Err(error),
                Err(_) => return Ok(false),
            };
            if reply["stopped"] == true {
                return Ok(true);
            }
            if reply["unassigned"] == true && reply["waiting"] != true {
                // No notebook was ever assigned and none will be: there is no log.
                return Ok(true);
            }
            let bytes = match reply["log"].as_str() {
                Some(encoded) => base64::engine::general_purpose::STANDARD.decode(encoded)?,
                None => Vec::new(),
            };
            if !bytes.is_empty() {
                sink(&bytes);
                received = true;
                *cursor += bytes.len() as u64;
            }
            let terminal = matches!(reply["stage"].as_str(), Some("COMPLETE" | "ERROR"));
            if terminal && *cursor >= reply["logSize"].as_u64().unwrap_or(0) {
                return Ok(true);
            }
            if bytes.is_empty() {
                if tokio::time::Instant::now() + LOG_POLL >= deadline {
                    return Ok(false);
                }
                tokio::time::sleep(LOG_POLL).await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn step() -> Step {
        Step {
            acquired: true,
            launched: true,
            observation: Observation::Queued {
                detail: String::new(),
            },
        }
    }

    #[test]
    fn remote_stages_map_to_engine_observations() {
        let mut s = step();
        assert_eq!(
            observe(&json!({"stage":"COMPLETE","exitCode":0}), &mut s),
            Observation::Exited {
                finished_at: None,
                code: Some(0),
                signal: None
            }
        );
        assert_eq!(
            observe(&json!({"stage":"ERROR","exitCode":143}), &mut s),
            Observation::Exited {
                finished_at: None,
                code: Some(143),
                signal: None
            }
        );
        assert_eq!(
            observe(&json!({"stage":"RUNNING"}), &mut s),
            Observation::Running { started_at: None }
        );
        assert!(matches!(
            observe(&json!({"stage":"UNKNOWN","message":"x"}), &mut s),
            Observation::Unreconciled { .. }
        ));
        assert!(matches!(
            observe(&json!({"stopped":true,"error":"Notebook lifetime expired"}), &mut s),
            Observation::Gone { reason } if reason.ends_with("Notebook lifetime expired")
        ));
        assert!(matches!(
            observe(&json!({"pending":true}), &mut s),
            Observation::Queued { .. }
        ));
        assert!(s.launched);
    }

    #[test]
    fn a_run_the_notebook_never_started_is_launched_again() {
        let mut s = step();
        assert!(matches!(
            observe(&json!({"stage":"NOT_STARTED"}), &mut s),
            Observation::Queued { .. }
        ));
        assert!(!s.launched);
    }

    #[test]
    fn queued_runs_say_why_they_wait() {
        let Observation::Queued { detail } = waiting(&json!({
            "queued": true,
            "detail": "Waiting for a compatible idle notebook.",
            "error": "No Kaggle account is ready. a: busy"
        })) else {
            panic!("not queued");
        };
        assert!(detail.contains("idle notebook") && detail.contains("a: busy"));
    }

    #[test]
    fn handles_from_other_backends_are_refused() {
        let run = WatchedRun {
            id: "run".into(),
            handle: Handle::Api {
                provider: "modal_job".into(),
                account: crate::engine::handle::ApiAccount {
                    target: None,
                    context: None,
                },
                ids: vec!["run".into()],
            },
        };
        assert!(check(&run).is_err());
    }
}
