use crate::downstream::bridge::compute::*;
use async_trait::async_trait;
pub struct KaggleCompute;

#[async_trait]
impl ComputeBackend for KaggleCompute {
    fn capabilities(&self) -> Capabilities {
        Capabilities {
            id: "kaggle",
            label: "Kaggle SSH",
            remote: true,
            flavors: true,
            requires_flavor: false,
            source_transport: "SSH tar stream",
        }
    }

    async fn stage_source(
        &self,
        project: &LocalProject,
        experiment: &LocalExperiment,
    ) -> Result<StagedSource> {
        stage_snapshot(project, experiment, false).await
    }

    async fn preflight(&self, args: &crate::downstream::bridge::ExpRunArgs) -> Result<Preflight> {
        let launch = crate::downstream::kaggle::runtime::Launch::resolve(
            args,
            &uuid::Uuid::nil().to_string(),
            "",
        )?;
        if args.image.is_some() {
            return Err(anyhow!(
                "Kaggle uses its notebook image; --image is not supported."
            ));
        }
        let check = crate::downstream::kaggle::runtime::call(
            &launch.runtime,
            serde_json::json!({"action":"readiness", "account":launch.account}),
            55,
        )
        .await?;
        if check["readiness"] != "verified_idle" {
            return Ok(not_ready(
                "Kaggle account must be signed in and idle. Check Kaggle compute settings.",
            ));
        }
        ready()
    }

    async fn submit(
        &self,
        args: &crate::downstream::bridge::ExpRunArgs,
        source: StagedSource,
        run_id: String,
    ) -> Result<StoredRun> {
        let store = Store::open()?;
        let mut run = store
            .get_run(&run_id)?
            .ok_or_else(|| anyhow!("Kaggle run was not reserved"))?;
        if run.command.trim().is_empty() {
            return Err(anyhow!(
                "Set the experiment run command before starting Kaggle."
            ));
        }
        let launch =
            crate::downstream::kaggle::runtime::Launch::resolve(args, &run_id, &run.command)?;
        launch.save()?;
        let mut descriptor = BackendDescriptor::parse(&run.backend_json)?;
        descriptor.namespace = Some(launch.account.clone());
        descriptor.job_id = Some(uuid::Uuid::parse_str(&run_id)?.simple().to_string());
        descriptor.flavor = Some(launch.accelerator.clone());
        descriptor.timeout_secs = Some(launch.ttl_seconds);
        source.0.apply_to_descriptor(&mut descriptor);
        record_submission_handle(&run_id, &descriptor)?;
        run.backend_json = descriptor.to_json();
        crate::downstream::bridge::spawn_supervisor(&run_id)?;
        Ok(run)
    }

    async fn status(&self, handle: &StoredRun) -> Result<StoredRun> {
        let store = Store::open()?;
        let mut run = store
            .get_run(&handle.id)?
            .ok_or_else(|| anyhow!("Run {} not found", handle.id))?;
        let mut descriptor = BackendDescriptor::parse(&run.backend_json)?;
        // A network failure during teardown is recoverable even after the job
        // finished. Reading that run reconciles its original notebook only.
        if crate::downstream::bridge::local::is_terminal(&run.status)
            && descriptor
                .monitoring_error
                .as_deref()
                .is_some_and(|s| s.starts_with("Kaggle shutdown unconfirmed:"))
        {
            if let Ok(launch) = crate::downstream::kaggle::runtime::Launch::load(&run.id) {
                if launch.stopped()
                    || launch
                        .call("status")
                        .await
                        .is_ok_and(|s| s["stopped"] == true)
                {
                    descriptor.monitoring_error = None;
                    run.backend_json = descriptor.to_json();
                    store.set_backend_json(&run.id, &run.backend_json)?;
                }
            }
        }
        Ok(run)
    }
}
