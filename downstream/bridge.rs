//! Compatibility boundary: adapters access OpenResearch through this module.
#[allow(unused_imports)]
pub(crate) use crate::{config, error, jobs, local, store, ExpRunArgs};
pub(crate) mod compute {
    pub(crate) use crate::compute::*;
    pub(crate) use crate::error::{anyhow, Result};
    pub(crate) use crate::jobs::BackendDescriptor;
    pub(crate) use crate::local::model::{LocalExperiment, LocalProject};
    pub(crate) use crate::store::{Store, StoredRun};
}
pub(crate) mod supervise {
    pub(crate) use crate::commands::supervise::{local_cancel_requested, POLL_INTERVAL};
    pub(crate) use crate::error::{anyhow, Result};
    pub(crate) use crate::jobs::BackendDescriptor;
    pub(crate) use crate::store::{now_ms, RunStatus, Store};
    pub(crate) use std::time::Duration;
}
pub(crate) mod api {
    pub(crate) use crate::commands::up::{bad_request, ApiResult};
    pub(crate) use axum::Json;
    pub(crate) use serde_json::{json, Value};
}
pub(crate) fn spawn_supervisor(id: &str) -> crate::error::Result<()> {
    crate::commands::exp::spawn_detached_supervise(id)
}
