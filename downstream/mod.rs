//! Downstream integrations. Upstream hooks register this module only.
pub(crate) mod bridge;
pub(crate) mod dsh {
    pub(crate) mod harness;
}
pub(crate) mod kaggle {
    pub(crate) mod compute;
    pub(crate) mod runtime;
    pub(crate) mod settings;
    pub(crate) mod supervisor;
}
pub(crate) fn compute_backend(id: &str) -> Option<Box<dyn crate::compute::ComputeBackend>> {
    match id {
        "kaggle" => Some(Box::new(kaggle::compute::KaggleCompute)),
        _ => None,
    }
}
pub(crate) fn harnesses() -> Vec<Box<dyn crate::local::harness::Harness>> {
    vec![Box::new(dsh::harness::Dsh)]
}
pub(crate) fn routes() -> axum::Router<crate::commands::up::AppState> {
    use axum::routing::{get, post};
    axum::Router::new()
        .route(
            "/api/settings/kaggle",
            get(kaggle::settings::get_settings).post(kaggle::settings::save_settings),
        )
        .route(
            "/api/settings/kaggle/discover",
            post(kaggle::settings::discover),
        )
        .route(
            "/api/settings/kaggle/preflight",
            post(kaggle::settings::preflight),
        )
}

pub(crate) const COMPUTE_ID: &str = "kaggle";
