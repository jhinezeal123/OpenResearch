//! Kaggle settings are local; no platform account/API or Workbench server.
use crate::downstream::bridge::api::*;
use crate::downstream::kaggle::runtime::{self as kaggle, Settings};

async fn payload(settings: Settings) -> Value {
    let catalog = if !settings.python.is_empty() && !settings.donor_root.is_empty() {
        kaggle::call(&settings, json!({"action":"accounts"}), 20).await
    } else {
        Ok(json!({"accounts":[], "accelerators":kaggle::ACCELERATORS}))
    };
    match catalog {
        Ok(catalog) => {
            json!({"settings":settings, "accounts":catalog["accounts"], "accelerators":catalog["accelerators"], "configured":settings.validate().is_ok(), "error":null})
        }
        Err(error) => {
            json!({"settings":settings, "accounts":[], "accelerators":kaggle::ACCELERATORS, "configured":false, "error":error.to_string()})
        }
    }
}

pub(crate) async fn get_settings() -> ApiResult {
    let settings = kaggle::load_settings()?.unwrap_or_else(|| Settings {
        accelerator: "cpu".into(),
        ttl_minutes: 10,
        ..Default::default()
    });
    Ok(Json(payload(settings).await))
}

pub(crate) async fn discover(Json(settings): Json<Settings>) -> ApiResult {
    Ok(Json(payload(settings).await))
}

pub(crate) async fn save_settings(Json(settings): Json<Settings>) -> ApiResult {
    settings.validate().map_err(bad_request)?;
    let catalog = kaggle::call(&settings, json!({"action":"accounts"}), 20)
        .await
        .map_err(bad_request)?;
    if !catalog["accounts"].as_array().is_some_and(|rows| {
        rows.iter()
            .any(|row| row["id"] == settings.account && row["configured"] == true)
    }) {
        return Err(bad_request(
            "Select a configured Kaggle account from the imported profiles.",
        ));
    }
    kaggle::save_settings(&settings)?;
    Ok(Json(payload(settings).await))
}

pub(crate) async fn preflight(Json(settings): Json<Settings>) -> ApiResult {
    settings.validate().map_err(bad_request)?;
    let result = kaggle::call(
        &settings,
        json!({"action":"readiness", "account":settings.account}),
        55,
    )
    .await
    .map_err(bad_request)?;
    Ok(Json(
        json!({"ready":result["readiness"] == "verified_idle", "readiness":result["readiness"], "activeSessions":result["active_session_count"], "observedAt":result["observed_at"]}),
    ))
}

pub(crate) fn summary() -> Value {
    json!({
        "id": "kaggle",
        "configured": crate::downstream::kaggle::runtime::configured(),
        "summary": crate::downstream::kaggle::runtime::load_settings().ok().flatten()
            .filter(|s| s.validate().is_ok())
            .map(|s| format!("{} · {} · {} min", s.account, s.accelerator, s.ttl_minutes))
            .unwrap_or_else(|| "Connect the existing AI Scientist Kaggle runtime".into()),
    })
}
