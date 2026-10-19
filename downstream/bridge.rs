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
#[allow(unused_imports)]
pub(crate) mod supervise {
    pub(crate) use crate::commands::supervise::{
        local_cancel_requested, status_of, watch_ssh_job, SshOwner, POLL_INTERVAL,
    };
    pub(crate) use crate::error::{anyhow, Result};
    pub(crate) use crate::jobs::{openresearch, ssh, BackendDescriptor};
    pub(crate) use crate::store::{now_ms, RunStatus, Store};
    pub(crate) use std::time::Duration;
}
pub(crate) mod api {
    pub(crate) use crate::commands::up::{bad_request, ApiResult};
    pub(crate) use axum::Json;
    pub(crate) use serde_json::{json, Value};
}
pub(crate) mod harness {
    pub(crate) use crate::local::chat::{
        harness_log, prepare_env, set_chat_session_env, ContextUsage, DeliveryState,
        PermissionDecision, PromptAnswer, ResumeCtx, TurnCtx, WirePart, WirePrompt,
    };
    pub(crate) use crate::local::harness::{
        CompactCtx, CompactOutcome, Harness, HarnessAuthState, HarnessInfo, HarnessOptions,
        ModelInfo, OptionChoice, PermissionMode, ResumeAction, TurnFailure, TurnOutcome,
        TurnResult, TURN_WATCHDOG,
    };
    pub(crate) use crate::local::opencode::{ensure_playbook, PLAYBOOK_REL};
}
pub(crate) fn spawn_supervisor(id: &str) -> crate::error::Result<()> {
    crate::commands::exp::spawn_detached_supervise(id)
}
