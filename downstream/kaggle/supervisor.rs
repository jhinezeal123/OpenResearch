//! A Run leases a session; the detached notebook owner handles TTL and shutdown.
use crate::downstream::bridge::supervise::*;
use crate::downstream::kaggle::runtime::{self, Launch};
use base64::Engine;
use serde_json::{json, Value};
use std::io::{Seek, SeekFrom, Write};

/// Continuous monitoring failure after which the Run is failed. The notebook
/// owner keeps enforcing its own deadline, so giving up never leaks compute.
const GIVE_UP_AFTER: Duration = Duration::from_secs(15 * 60);

fn next_action(acquired: bool, launched: bool, cancelled: bool) -> &'static str {
    // Local progress is lost on restart. A cancel must reconcile the remote Run
    // even if this supervisor has never acquired or launched it itself.
    if cancelled {
        "cancel"
    } else if !acquired {
        "acquire"
    } else if !launched {
        "launch"
    } else {
        "inspect"
    }
}

pub(crate) async fn run(
    store: Store,
    stored: crate::downstream::bridge::store::StoredRun,
    mut descriptor: BackendDescriptor,
    run_id: String,
) -> Result<()> {
    let launch = Launch::load(&run_id)?;
    if !launch.pooled {
        return Err(anyhow!("This legacy notebook uses the old one-run lifecycle; inspect its saved status before restarting."));
    }
    let source =
        crate::downstream::bridge::compute::SourceSnapshot::from_run(&stored, &descriptor)?;
    let mut request = serde_json::to_value(&launch)?;
    request["projectId"] = json!(stored.project_id);
    request["experimentId"] = json!(stored.experiment_id);
    request["sourcePath"] = json!(source.path);
    request["digest"] = json!(source.digest);
    request["env"] = json!(crate::downstream::bridge::config::run_env(&run_id));
    let mut acquired = false;
    let mut launched = false;
    let mut failing_since: Option<std::time::Instant> = None;
    let mut offset = 0u64;
    let mut last_error = None;
    let mut log = std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(crate::downstream::bridge::store::log_path(&run_id))?;
    loop {
        let cancelled = local_cancel_requested(&store, &run_id);
        let action = next_action(acquired, launched, cancelled);
        request["offset"] = json!(offset);
        let observed = operation(&launch, &request, action).await;
        let observed = match observed {
            Ok(value) => {
                last_error = None;
                failing_since = None;
                value
            }
            Err(error) => {
                let text = format!("Kaggle monitoring will retry the same session: {error}");
                if last_error.as_ref() != Some(&text) {
                    eprintln!("{text}");
                    last_error = Some(text.clone());
                }
                if failing_since
                    .get_or_insert_with(std::time::Instant::now)
                    .elapsed()
                    >= GIVE_UP_AFTER
                {
                    // Monitoring failure does not prove the process stopped.
                    // Its owner retains the lease until a terminal receipt or TTL.
                    store.update_status(&run_id, RunStatus::Failed, Some(now_ms()), None)?;
                    store.set_result_markdown(
                        &run_id,
                        &format!("Kaggle monitoring failed for 15 minutes and stopped: {error}. The session remains reserved until the remote Run finishes or the notebook stops at its own deadline."),
                    )?;
                    return Ok(());
                }
                descriptor.monitoring_error = Some(text);
                store.set_backend_json(&run_id, &descriptor.to_json())?;
                tokio::time::sleep(POLL_INTERVAL).await;
                // An uncertain launch is retried with the same immutable Run ID; the remote intent prevents replay.
                continue;
            }
        };
        if observed["stopped"] == true {
            let status = if cancelled {
                RunStatus::Cancelled
            } else {
                RunStatus::Failed
            };
            store.update_status(&run_id, status, Some(now_ms()), None)?;
            let reason = observed["error"]
                .as_str()
                .map(|e| format!(" Reason: {e}"))
                .unwrap_or_default();
            store.set_result_markdown(
                &run_id,
                &format!("Kaggle session ended before this run completed. Its command was not replayed.{reason}"),
            )?;
            return Ok(());
        }
        if action == "acquire" {
            if observed["ready"] == true {
                acquired = true;
                descriptor.job_id = observed["sessionId"].as_str().map(str::to_owned);
                descriptor.namespace = observed["account"].as_str().map(str::to_owned);
                descriptor.url = observed["receipt"]["notebook_ref"]
                    .as_str()
                    .map(|r| format!("https://www.kaggle.com/code/{r}"));
                descriptor.monitoring_error = None;
                store.set_backend_json(&run_id, &descriptor.to_json())?;
            } else if let Some(error) = observed["error"].as_str() {
                // Surface bootstrap trouble while the notebook owner keeps retrying.
                descriptor.monitoring_error = Some(format!("Kaggle notebook not ready: {error}"));
                store.set_backend_json(&run_id, &descriptor.to_json())?;
            }
            tokio::time::sleep(POLL_INTERVAL).await;
            continue;
        }
        if observed["pending"] == true {
            tokio::time::sleep(POLL_INTERVAL).await;
            continue;
        }
        if action == "launch" {
            launched = true;
            store.update_status(&run_id, RunStatus::Running, None, None)?;
        }
        if let Some(encoded) = observed["log"].as_str() {
            let bytes = base64::engine::general_purpose::STANDARD.decode(encoded)?;
            if !bytes.is_empty() {
                if offset == 0 {
                    log.set_len(0)?;
                    log.seek(SeekFrom::Start(0))?;
                }
                log.write_all(&bytes)?;
                log.flush()?;
            }
            offset = observed["offset"].as_u64().unwrap_or(offset);
        }
        descriptor.monitoring_error = None;
        if cancelled {
            descriptor.cancellation_accepted = true;
        }
        store.set_backend_json(&run_id, &descriptor.to_json())?;
        let stage = observed["stage"].as_str().unwrap_or("RUNNING");
        if matches!(stage, "COMPLETE" | "ERROR") {
            // Drain all log pages before recording the terminal state.
            if offset < observed["logSize"].as_u64().unwrap_or(offset) {
                continue;
            }
            let status = if cancelled {
                RunStatus::Cancelled
            } else if stage == "COMPLETE" {
                RunStatus::Done
            } else {
                RunStatus::Failed
            };
            store.update_status(
                &run_id,
                status,
                Some(now_ms()),
                observed["exitCode"].as_i64(),
            )?;
            release(&launch, &request).await;
            return Ok(());
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
}

/// Free the pool lease. The notebook owner also releases a finished Run, so a
/// failure here only delays reuse; retry briefly and log rather than fail.
async fn release(launch: &Launch, request: &Value) {
    for attempt in 1..=3 {
        match operation(launch, request, "release").await {
            Ok(value) if value["released"] == true || value["stopped"] == true => return,
            Ok(_) if attempt == 3 => eprintln!("Kaggle release awaits a remote terminal receipt"),
            Ok(_) => tokio::time::sleep(Duration::from_secs(2)).await,
            Err(error) if attempt == 3 => eprintln!("Kaggle release unconfirmed: {error}"),
            Err(_) => tokio::time::sleep(Duration::from_secs(2)).await,
        }
    }
}
async fn operation(launch: &Launch, request: &Value, action: &str) -> Result<Value> {
    let mut request = request.clone();
    request["action"] = json!(action);
    runtime::call(
        &launch.runtime,
        request,
        if action == "launch" { 300 } else { 90 },
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cancellation_reconciles_remote_state_after_restart_or_a_lost_launch_ack() {
        for (acquired, launched) in [(false, false), (true, false), (true, true)] {
            assert_eq!(next_action(acquired, launched, true), "cancel");
        }
    }

    #[test]
    fn normal_progress_acquires_then_launches_then_inspects() {
        assert_eq!(next_action(false, false, false), "acquire");
        assert_eq!(next_action(true, false, false), "launch");
        assert_eq!(next_action(true, true, false), "inspect");
    }
}
