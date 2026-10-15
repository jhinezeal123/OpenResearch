//! Notebook provisioning/teardown around OpenResearch's shared SSH job loop.
use super::*;
use crate::jobs::kaggle::Launch;

struct KaggleOwner<'a>(&'a Launch);

#[async_trait::async_trait]
impl SshOwner for KaggleOwner<'_> {
    async fn disappeared(&self) -> Option<String> {
        if Store::open()
            .ok()
            .is_some_and(|s| local_cancel_requested(&s, &self.0.run_id))
        {
            // SSH may be unavailable; the native receipt keeps cancellation
            // intent even when only the notebook's bounded TTL can stop it.
            let _ = self.0.call("stop").await;
        }
        self.0
            .call("status")
            .await
            .ok()
            .filter(|v| v["stopped"] == true)
            .map(|_| "Kaggle confirmed the notebook stopped while SSH was unavailable.".into())
    }
}

pub(super) async fn run(
    store: Store,
    stored: crate::store::StoredRun,
    mut descriptor: BackendDescriptor,
    run_id: String,
) -> Result<()> {
    let launch = Launch::load(&run_id)?;
    let result = execute(&store, &stored, &mut descriptor, &launch).await;
    // Teardown is required on success, cancellation and failed provisioning.
    // A failed response does not mean the provider stopped; preserve that fact.
    let cleanup = shutdown(&launch).await;
    if let Err(error) = &cleanup {
        descriptor.monitoring_error = Some(format!("Kaggle shutdown unconfirmed: {error}. The saved notebook has a bounded lifetime; inspect before starting another run."));
        store.set_backend_json(&run_id, &descriptor.to_json())?;
    } else if descriptor.monitoring_error.take().is_some() {
        store.set_backend_json(&run_id, &descriptor.to_json())?;
    }
    if let Err(error) = &result {
        let status = if local_cancel_requested(&store, &run_id) && cleanup.is_ok() {
            RunStatus::Cancelled
        } else {
            RunStatus::Failed
        };
        if store.update_status(&run_id, status, Some(now_ms()), None)? {
            store.set_result_markdown(&run_id, &format!("Kaggle run failed: {error}"))?;
        }
    }
    result?;
    cleanup
}

async fn execute(
    store: &Store,
    stored: &crate::store::StoredRun,
    descriptor: &mut BackendDescriptor,
    launch: &Launch,
) -> Result<()> {
    let run_id = &launch.run_id;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(8 * 60);
    let mut cancellation_sent = false;
    let receipt = loop {
        if local_cancel_requested(store, run_id) {
            return Err(anyhow!("Cancelled before the experiment was started"));
        }
        // A repeated start uses the SAME request ID. The donor journal never
        // retries an uncertain notebook submission under a replacement ID.
        let start = launch.call("start");
        tokio::pin!(start);
        let observed = loop {
            tokio::select! {
                result = &mut start => break result?,
                _ = tokio::time::sleep(Duration::from_secs(2)) => {
                    if !cancellation_sent && local_cancel_requested(store, run_id) {
                        let _ = launch.call("stop").await;
                        cancellation_sent = true;
                    }
                }
            }
        };
        if cancellation_sent || local_cancel_requested(store, run_id) {
            return Err(anyhow!("Cancelled during Kaggle provisioning"));
        }
        match observed["ssh_status"].as_str() {
            Some("READY") => break observed,
            Some("BLOCKED") => {
                return Err(anyhow!(
                    "{}",
                    observed["required_action"]
                        .as_str()
                        .unwrap_or("Kaggle account is not ready")
                ))
            }
            _ if observed["submission_status"] == "NOT_SUBMITTED" => {
                return Err(anyhow!(
                    "The notebook was not submitted; inspect the saved Kaggle request."
                ))
            }
            _ => {}
        }
        if launch
            .call("status")
            .await
            .is_ok_and(|s| s["stopped"] == true)
        {
            return Err(anyhow!(
                "Kaggle confirmed this notebook already stopped before SSH became ready."
            ));
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(anyhow!(
                "Kaggle SSH did not become ready. The saved request was not replaced."
            ));
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    };
    let target = crate::jobs::kaggle::ssh_target(&receipt)?;
    descriptor.url = receipt["notebook_ref"]
        .as_str()
        .map(|r| format!("https://www.kaggle.com/code/{r}"));
    store.set_backend_json(run_id, &descriptor.to_json())?;
    let dir = format!(".orx/runs/{run_id}");

    // Do not replay a possibly launched experiment after losing its ACK.
    // The local intent is durable before sending run_job; reattach needs a
    // remote PID/exit receipt, otherwise the result remains an explicit error.
    let intent = crate::jobs::kaggle::root()
        .join("runs")
        .join(format!("{run_id}.launch-intent"));
    if !intent.exists() {
        let source = crate::compute::SourceSnapshot::from_run(stored, descriptor)?;
        ssh::stage_source(&target, run_id, &source.path, &source.digest, None).await?;
        if local_cancel_requested(store, run_id) {
            return Err(anyhow!("Cancelled before launching the experiment"));
        }
        let payload = receipt["wrapped_command"]
            .as_str()
            .ok_or_else(|| anyhow!("Kaggle omitted the notebook environment wrapper"))?;
        let script = openresearch::wrap_with_timeout(payload, launch.ttl_seconds);
        let env = crate::config::list_synced_env().into_iter().collect();
        std::fs::write(&intent, b"submitted-once\n")?;
        if let Err(error) = ssh::run_job(&ssh::SshJobSpec {
            target: target.clone(),
            run_id: run_id.clone(),
            script,
            env,
            container: None,
        })
        .await
        {
            // Inspect without repeating the command. Reconciliation failure
            // still tears down the uniquely owned notebook.
            let state = ssh::inspect_job(&target, &dir, None).await?;
            if state.message.as_deref() == Some(ssh::RUN_DIR_MISSING) {
                return Err(error);
            }
        }
    }
    watch_ssh_job(
        store,
        status_of(stored)?,
        target,
        dir,
        run_id,
        descriptor,
        Some(&KaggleOwner(launch)),
    )
    .await?;
    Ok(())
}

async fn shutdown(launch: &Launch) -> Result<()> {
    if launch.stopped() {
        return Ok(());
    }
    let mut last_error = None;
    let mut stop_sent = false;
    for attempt in 0..8 {
        if launch
            .call("status")
            .await
            .is_ok_and(|s| s["stopped"] == true)
        {
            return Ok(());
        }
        if !stop_sent {
            match launch.call("stop").await {
                Ok(receipt) if receipt["stopped"] == true => return Ok(()),
                Ok(_) => stop_sent = true,
                Err(error) => last_error = Some(error),
            }
        }
        if attempt < 7 {
            tokio::time::sleep(Duration::from_secs(3)).await;
        }
    }
    Err(last_error.unwrap_or_else(|| anyhow!("Kaggle has not confirmed the stop request")))
}
