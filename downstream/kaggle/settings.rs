//! Settings and session controls belong to the Kaggle plugin.
use crate::downstream::bridge::api::*;
use crate::downstream::kaggle::runtime::{self as kaggle, Settings};

async fn payload(settings: Settings) -> Value {
    let catalog = kaggle::call(&settings, json!({"action":"accounts"}), 20).await;
    match catalog {
        Ok(catalog) => {
            json!({"settings":settings,"accounts":catalog["accounts"],"accelerators":kaggle::ACCELERATORS,"configured":settings.validate().is_ok(),"runtimeReady":kaggle::python().is_file(),"error":null})
        }
        Err(error) => {
            json!({"settings":settings,"accounts":[],"accelerators":kaggle::ACCELERATORS,"configured":false,"runtimeReady":kaggle::python().is_file(),"error":error.to_string()})
        }
    }
}
fn defaults() -> Settings {
    Settings {
        accelerator: "cpu".into(),
        ttl_minutes: 10,
        ..Default::default()
    }
}
pub(crate) async fn get_settings() -> ApiResult {
    Ok(Json(
        payload(kaggle::load_settings()?.unwrap_or_else(defaults)).await,
    ))
}
pub(crate) async fn discover(Json(settings): Json<Settings>) -> ApiResult {
    Ok(Json(payload(settings).await))
}
pub(crate) async fn save_settings(Json(settings): Json<Settings>) -> ApiResult {
    settings.validate().map_err(bad_request)?;
    let catalog = kaggle::call(&settings, json!({"action":"accounts"}), 20)
        .await
        .map_err(bad_request)?;
    if !settings.accounts.iter().all(|id| {
        catalog["accounts"].as_array().is_some_and(|rows| {
            rows.iter()
                .any(|r| r["id"] == *id && r["configured"] == true)
        })
    }) {
        return Err(bad_request(
            "Enable configured accounts from the imported profiles.",
        ));
    }
    kaggle::save_settings(&settings)?;
    Ok(Json(payload(settings).await))
}
pub(crate) async fn preflight(Json(settings): Json<Settings>) -> ApiResult {
    settings.validate().map_err(bad_request)?;
    let mut rows = Vec::new();
    for account in &settings.accounts {
        match kaggle::call(
            &settings,
            json!({"action":"readiness","account":account}),
            55,
        )
        .await
        {
            Ok(value) => rows.push(value),
            Err(error) => rows.push(
                json!({"account":account,"readiness":"unavailable","error":error.to_string()}),
            ),
        }
    }
    Ok(Json(json!({"accounts":rows})))
}
pub(crate) async fn sessions() -> ApiResult {
    let settings = kaggle::load_settings()?.unwrap_or_else(defaults);
    Ok(Json(
        kaggle::call(&settings, json!({"action":"sessions"}), 30)
            .await
            .map_err(bad_request)?,
    ))
}
pub(crate) async fn close_session(Json(value): Json<Value>) -> ApiResult {
    let settings = kaggle::load_settings()?.unwrap_or_else(defaults);
    Ok(Json(
        kaggle::call(
            &settings,
            json!({"action":"close-session","sessionId":value["sessionId"]}),
            30,
        )
        .await
        .map_err(bad_request)?,
    ))
}
pub(crate) async fn recover_cookie(Json(value): Json<Value>) -> ApiResult {
    let settings = kaggle::load_settings()?.unwrap_or_else(defaults);
    Ok(Json(
        kaggle::call(
            &settings,
            json!({"action":"recover-cookie","account":value["account"]}),
            240,
        )
        .await
        .map_err(bad_request)?,
    ))
}
pub(crate) fn summary() -> Value {
    json!({"id":"kaggle","configured":kaggle::configured(),"summary":kaggle::load_settings().ok().flatten().filter(|s|s.validate().is_ok()).map(|s|format!("{} accounts · {} · {} min",s.accounts.len(),s.accelerator,s.ttl_minutes)).unwrap_or_else(||"Configure the packaged Kaggle account pool".into())})
}
