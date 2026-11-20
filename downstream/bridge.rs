//! Compatibility boundary: adapters access OpenResearch through this module.
#[allow(unused_imports)]
pub(crate) use crate::{config, error, jobs, local, store, ExpRunArgs};
pub(crate) mod compute {
    pub(crate) use crate::compute::*;
    pub(crate) use crate::error::{anyhow, Result};
    pub(crate) use crate::local::model::{LocalExperiment, LocalProject};
}
pub(crate) mod engine {
    pub(crate) use crate::compute::SourceSnapshot;
    pub(crate) use crate::engine::adapters::api::{self, Api};
    pub(crate) use crate::engine::handle::Handle;
    pub(crate) use crate::engine::{
        Fence, LaunchError, Observation, Recovery, RunIntent, WatchedRun,
    };
    pub(crate) use crate::error::{anyhow, Context, Result};
    pub(crate) use crate::jobs::BackendDescriptor;
    pub(crate) use crate::store::Store;
}
pub(crate) mod api {
    pub(crate) use crate::commands::up::{bad_request, ApiResult};
    pub(crate) use axum::Json;
    pub(crate) use serde_json::{json, Value};
}
