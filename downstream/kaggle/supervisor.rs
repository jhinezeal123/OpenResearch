//! A Run leases a session; the detached notebook owner handles TTL and shutdown.
use crate::downstream::bridge::supervise::*;
use crate::downstream::kaggle::runtime::{self, Launch};
use base64::Engine;
use serde_json::{json, Value};
use std::io::{Seek, SeekFrom, Write};

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
    request["env"] = json!(crate::downstream::bridge::config::list_synced_env()
        .into_iter()
        .collect::<std::collections::HashMap<_, _>>());
    let mut acquired = false;
    let mut launched = false;
    let mut offset = 0u64;
    let mut last_error = None;
    let mut log = std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(crate::downstream::bridge::store::log_path(&run_id))?;
    loop {
        let cancelled = local_cancel_requested(&store, &run_id);
        if cancelled && !launched {
            let _ = operation(&launch, &request, "release").await;
            store.update_status(&run_id, RunStatus::Cancelled, Some(now_ms()), None)?;
            return Ok(());
        }
        let action = if !acquired {
            "acquire"
        } else if !launched {
            "launch"
        } else if cancelled {
            "cancel"
        } else {
            "inspect"
        };
        request["offset"] = json!(offset);
        let observed = operation(&launch, &request, action).await;
        let observed = match observed {
            Ok(value) => {
                last_error = None;
                value
            }
            Err(error) => {
                let text = format!("Kaggle monitoring will retry the same session: {error}");
                if last_error.as_ref() != Some(&text) {
                    eprintln!("{text}");
                    last_error = Some(text.clone());
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
            store.set_result_markdown(
                &run_id,
                "Kaggle session ended before this run completed. Its command was not replayed.",
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
            let _ = operation(&launch, &request, "release").await;
            return Ok(());
        }
        tokio::time::sleep(POLL_INTERVAL).await;
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
