//! The run engine's Kaggle adapter.
//!
//! It drives the same acquire → launch → inspect sequence as the 0.2.x supervisor, one bridge
//! operation at a time. Every operation is keyed by the run ID on both sides (the pool's
//! assignment and the remote run directory), so a step lost to a restart is simply repeated:
//! the remote launch intent keeps a command from ever running twice.
//!
//! A bridge operation can outlast the engine's observation budget (account probes, a source
//! upload), so each step runs in the background and `inspect` reports the latest result.
//!
//! Notebook storage ends with its session, and a free notebook can be reclaimed for another
//! run moments after this one ends. So every step also copies new log bytes to this machine,
//! and the step that sees the run end copies the rest before reporting it.
use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom, Write};
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
/// How much of the local copy one `logs` pass hands over at a time.
const LOG_CHUNK: usize = 1 << 20;

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

fn run_file(run_id: &str, suffix: &str) -> Result<PathBuf> {
    let id = uuid::Uuid::parse_str(run_id)?;
    Ok(runtime::root().join("runs").join(format!("{id}{suffix}")))
}

/// Serializes appends to the local log copies; steps and log reads both write them.
static COPY: Mutex<()> = Mutex::new(());

fn decoded(reply: &Value) -> Result<Vec<u8>> {
    Ok(match reply["log"].as_str() {
        Some(encoded) => base64::engine::general_purpose::STANDARD.decode(encoded)?,
        None => Vec::new(),
    })
}

fn terminal(reply: &Value) -> bool {
    matches!(reply["stage"].as_str(), Some("COMPLETE" | "ERROR"))
}

/// Bytes of the run's log already on this machine.
fn copied(run_id: &str) -> Result<u64> {
    match std::fs::metadata(run_file(run_id, ".log")?) {
        Ok(meta) => Ok(meta.len()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(0),
        Err(error) => Err(error.into()),
    }
}

fn append(run_id: &str, bytes: &[u8]) -> Result<()> {
    let path = run_file(run_id, ".log")?;
    if let Some(runs) = path.parent() {
        std::fs::create_dir_all(runs)?;
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .with_context(|| format!("Could not open the Kaggle log copy {}", path.display()))?;
    file.write_all(bytes)?;
    Ok(())
}

fn mark_complete(run_id: &str) -> Result<()> {
    std::fs::write(run_file(run_id, ".log.complete")?, b"")?;
    Ok(())
}

fn complete(run_id: &str) -> Result<bool> {
    Ok(run_file(run_id, ".log.complete")?.is_file())
}

/// Adds a reply's log bytes to the local copy when they continue it, and marks the copy
/// complete once a finished run's log is all here. Returns whether bytes were added.
fn record(run_id: &str, reply: &Value) -> Result<bool> {
    let _guard = lock(&COPY);
    let mut len = copied(run_id)?;
    let bytes = decoded(reply)?;
    let continues = reply["offset"].as_u64() == Some(len + bytes.len() as u64);
    let added = !bytes.is_empty() && continues;
    if added {
        append(run_id, &bytes)?;
        len += bytes.len() as u64;
    }
    if terminal(reply) && len >= reply["logSize"].as_u64().unwrap_or(u64::MAX) {
        mark_complete(run_id)?;
    }
    Ok(added)
}

/// The notebook is gone: nothing past the copy can arrive, so the log says where it ends.
fn close_copy(run_id: &str) -> Result<()> {
    let _guard = lock(&COPY);
    if complete(run_id)? {
        return Ok(());
    }
    let len = copied(run_id)?;
    // A run the notebook never started wrote nothing; there is nothing to call lost.
    if len == 0 && !run_file(run_id, ".launched")?.is_file() {
        return mark_complete(run_id);
    }
    append(
        run_id,
        format!(
            "\n[orx] The Kaggle notebook closed before the rest of this log was copied; \
             output after byte {len} is lost.\n"
        )
        .as_bytes(),
    )?;
    mark_complete(run_id)
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

/// Sends `action` from where the local log copy ends and copies what comes back. Once the run
/// has ended, keeps reading until its log is all here, while each read still makes progress.
async fn exchange(
    run_id: &str,
    launch: &Launch,
    request: &mut Value,
    action: &str,
) -> Result<Value, Failure> {
    let mut action = action;
    loop {
        request["offset"] = json!(copied(run_id).map_err(Failure::Local)?);
        let reply = remote(launch, request, action).await?;
        let added = record(run_id, &reply).map_err(Failure::Local)?;
        if !(terminal(&reply) && added && !complete(run_id).map_err(Failure::Local)?) {
            return Ok(reply);
        }
        action = "inspect";
    }
}

async fn drive(run_id: &str, cancelled: bool, step: &mut Step) -> Result<Observation, Failure> {
    if cancelled {
        // Idempotent: it re-reads the remote run, or ends a run still waiting for a notebook.
        let (launch, mut request) = local(run_id, false)?;
        let reply = exchange(run_id, &launch, &mut request, "cancel").await?;
        return Ok(observe(&reply, step));
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
        let (launch, mut request) = local(run_id, true)?;
        let reply = exchange(run_id, &launch, &mut request, "launch").await?;
        step.launched = reply["pending"] != true && reply["stopped"] != true;
        if step.launched {
            std::fs::write(run_file(run_id, ".launched").map_err(Failure::Local)?, b"")
                .map_err(|error| Failure::Local(error.into()))?;
        }
        return Ok(observe(&reply, step));
    }
    let (launch, mut request) = local(run_id, false)?;
    let reply = exchange(run_id, &launch, &mut request, "inspect").await?;
    Ok(observe(&reply, step))
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
        let (launch, mut request) = request(&run.id, false)?;
        request["offset"] = json!(copied(&run.id)?);
        let reply = operation(&launch, &request, "cancel").await?;
        record(&run.id, &reply)?;
        Ok(())
    }

    /// Frees the notebook for the next run only once Kaggle reports this one finished.
    async fn finish(&self, run: &WatchedRun) -> Result<()> {
        check(run)?;
        self.forget(&run.id);
        let (launch, request) = request(&run.id, false)?;
        let reply = operation(&launch, &request, "release").await?;
        if reply["released"] == true || reply["stopped"] == true {
            // The engine keeps the log before it finishes a run; the copy has served.
            for suffix in [".log", ".log.complete", ".launched"] {
                match std::fs::remove_file(run_file(&run.id, suffix)?) {
                    Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                        return Err(error.into())
                    }
                    _ => {}
                }
            }
            Ok(())
        } else {
            Err(anyhow!(
                "Kaggle has not confirmed that run {} finished; its notebook stays reserved until it does.",
                run.id
            ))
        }
    }

    /// Serves the local copy first, then asks the notebook for what the copy lacks.
    async fn logs(
        &self,
        run: &WatchedRun,
        cursor: &mut u64,
        window: Duration,
        sink: &mut (dyn for<'b> FnMut(&'b [u8]) + Send),
    ) -> Result<bool> {
        check(run)?;
        let deadline = tokio::time::Instant::now() + window;
        let mut session: Option<(Launch, Value)> = None;
        let mut received = false;
        loop {
            if *cursor < copied(&run.id)? {
                let mut file = std::fs::File::open(run_file(&run.id, ".log")?)?;
                file.seek(SeekFrom::Start(*cursor))?;
                let mut chunk = Vec::with_capacity(LOG_CHUNK);
                file.take(LOG_CHUNK as u64).read_to_end(&mut chunk)?;
                sink(&chunk);
                *cursor += chunk.len() as u64;
                received = true;
                continue;
            }
            if complete(&run.id)? {
                return Ok(true);
            }
            if session.is_none() {
                session = Some(request(&run.id, false)?);
            }
            let (launch, request) = session.as_mut().expect("session set above");
            request["offset"] = json!(copied(&run.id)?);
            let reply = match operation(launch, request, "inspect").await {
                Ok(reply) => reply,
                Err(error) if !received => return Err(error),
                Err(_) => return Ok(false),
            };
            if reply["stopped"] == true {
                close_copy(&run.id)?;
                continue;
            }
            if reply["unassigned"] == true && reply["waiting"] != true {
                // No notebook was ever assigned and none will be: the log ends here.
                let _guard = lock(&COPY);
                mark_complete(&run.id)?;
                continue;
            }
            if record(&run.id, &reply)? || complete(&run.id)? {
                continue;
            }
            if tokio::time::Instant::now() + LOG_POLL >= deadline {
                return Ok(false);
            }
            tokio::time::sleep(LOG_POLL).await;
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

    fn log(bytes: &[u8], end: u64, stage: &str, size: u64) -> Value {
        json!({
            "log": base64::engine::general_purpose::STANDARD.encode(bytes),
            "offset": end,
            "stage": stage,
            "logSize": size,
        })
    }

    #[test]
    fn the_local_log_copy_takes_only_continuing_bytes_and_knows_when_it_is_whole() {
        let run = uuid::Uuid::new_v4().to_string();
        assert!(record(&run, &log(b"tick 1\n", 7, "RUNNING", 7)).unwrap());
        // A replayed page and a page past a gap are both refused.
        assert!(!record(&run, &log(b"tick 1\n", 7, "RUNNING", 7)).unwrap());
        assert!(!record(&run, &log(b"tick 9\n", 30, "RUNNING", 30)).unwrap());
        assert!(!complete(&run).unwrap());
        assert!(record(&run, &log(b"done\n", 12, "COMPLETE", 12)).unwrap());
        assert!(complete(&run).unwrap());
        assert_eq!(
            std::fs::read(run_file(&run, ".log").unwrap()).unwrap(),
            b"tick 1\ndone\n"
        );
    }

    #[test]
    fn a_notebook_lost_before_the_copy_finished_says_what_is_missing() {
        let run = uuid::Uuid::new_v4().to_string();
        record(&run, &log(b"tick 1\n", 7, "RUNNING", 7)).unwrap();
        close_copy(&run).unwrap();
        let text =
            String::from_utf8(std::fs::read(run_file(&run, ".log").unwrap()).unwrap()).unwrap();
        assert!(text.starts_with("tick 1\n") && text.contains("after byte 7 is lost"));
        assert!(complete(&run).unwrap());
        // A complete copy is never annotated again.
        close_copy(&run).unwrap();
        assert_eq!(text.len() as u64, copied(&run).unwrap());
    }

    #[test]
    fn a_run_that_never_started_ends_its_log_without_a_loss_note() {
        let run = uuid::Uuid::new_v4().to_string();
        close_copy(&run).unwrap();
        assert!(complete(&run).unwrap());
        assert_eq!(copied(&run).unwrap(), 0);
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
