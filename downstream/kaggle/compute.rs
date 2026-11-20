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

    /// The engine's launch intent: the resolved Kaggle launch, which names accounts and
    /// sources but holds no credentials. The run's environment is added when it starts.
    async fn prepare(
        &self,
        args: &crate::downstream::bridge::ExpRunArgs,
        _source: &SourceSnapshot,
        run_id: &str,
        command: &str,
    ) -> Result<crate::downstream::bridge::store::LaunchSpec> {
        if command.trim().is_empty() {
            return Err(anyhow!(
                "Set the experiment run command before starting Kaggle."
            ));
        }
        let launch = crate::downstream::kaggle::runtime::Launch::resolve(args, run_id, command)?;
        Ok(crate::downstream::bridge::store::LaunchSpec {
            kind: crate::downstream::kaggle::engine::KIND.into(),
            target: None,
            // `flavor` and `timeout_seconds` are what the dashboard shows for engine runs.
            options: serde_json::json!({
                "flavor": launch.accelerator,
                "timeout_seconds": launch.ttl_seconds,
                "launch": launch,
            }),
        })
    }
}
