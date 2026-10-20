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
        let _launch = crate::downstream::kaggle::runtime::Launch::resolve(
            args,
            &uuid::Uuid::nil().to_string(),
            "",
        )?;
        if args.image.is_some() {
            return Err(anyhow!(
                "Kaggle uses its notebook image; --image is not supported."
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
        descriptor.namespace = None;
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
        Store::open()?
            .get_run(&handle.id)?
            .ok_or_else(|| anyhow!("Run {} not found", handle.id))
    }
}
