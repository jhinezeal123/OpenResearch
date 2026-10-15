//! Kaggle notebook ownership; source upload, execution and monitoring use SSH.
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::AsyncWriteExt;

use crate::downstream::bridge::error::{anyhow, Result};

pub const ACCELERATORS: &[&str] = &["cpu", "NvidiaTeslaT4", "TpuV5E8", "TpuV6E8"];

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Settings {
    pub accounts: Vec<String>,
    pub accelerator: String,
    pub ttl_minutes: u64,
    pub competition_sources: Vec<String>,
    pub dataset_sources: Vec<String>,
}

impl Settings {
    pub fn validate(&self) -> Result<()> {
        if !python().is_file() {
            return Err(anyhow!(
                "Install the packaged Kaggle runtime with downstream/kaggle/setup.ps1."
            ));
        }
        if self.accounts.is_empty() || !ACCELERATORS.contains(&self.accelerator.as_str()) {
            return Err(anyhow!(
                "Enable at least one Kaggle account and select an accelerator."
            ));
        }
        if !(1..=720).contains(&self.ttl_minutes) {
            return Err(anyhow!("Kaggle notebook lifetime must be 1–720 minutes."));
        }
        for source in self.competition_sources.iter().chain(&self.dataset_sources) {
            if source.is_empty() || source.len() > 500 || source.contains(['\r', '\n']) {
                return Err(anyhow!(
                    "Kaggle sources must be nonempty references, one per line."
                ));
            }
        }
        Ok(())
    }
}

fn settings_path() -> PathBuf {
    crate::downstream::bridge::store::data_dir().join("kaggle.json")
}
pub fn root() -> PathBuf {
    crate::downstream::bridge::store::data_dir().join("kaggle")
}

pub fn load_settings() -> Result<Option<Settings>> {
    match std::fs::read(settings_path()) {
        Ok(bytes) => {
            let old: Value = serde_json::from_slice(&bytes)?;
            let mut settings: Settings = serde_json::from_value(old.clone())?;
            if settings.accounts.is_empty() {
                if let Some(account) = old["account"].as_str().filter(|s| !s.is_empty()) {
                    settings.accounts.push(account.into());
                }
            }
            Ok(Some(settings))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

pub fn configured() -> bool {
    load_settings()
        .ok()
        .flatten()
        .is_some_and(|s| s.validate().is_ok())
}

pub fn save_settings(settings: &Settings) -> Result<()> {
    settings.validate()?;
    save_json(&settings_path(), settings)
}

fn save_json(path: &Path, value: &impl Serialize) -> Result<()> {
    std::fs::create_dir_all(
        path.parent()
            .ok_or_else(|| anyhow!("Missing parent directory"))?,
    )?;
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    std::fs::write(&temporary, serde_json::to_vec_pretty(value)?)?;
    std::fs::rename(temporary, path)?;
    Ok(())
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Launch {
    pub runtime: Settings,
    pub run_id: String,
    pub account: String,
    pub accelerator: String,
    pub ttl_seconds: u64,
    pub competition_sources: Vec<String>,
    pub dataset_sources: Vec<String>,
    pub command: String,
    #[serde(default)]
    pub pooled: bool,
}

impl Launch {
    pub fn resolve(
        args: &crate::downstream::bridge::ExpRunArgs,
        run_id: &str,
        command: &str,
    ) -> Result<Self> {
        let runtime = load_settings()?
            .ok_or_else(|| anyhow!("Configure Kaggle in Compute settings first."))?;
        runtime.validate()?;
        let accelerator = args
            .flavor
            .clone()
            .unwrap_or_else(|| runtime.accelerator.clone());
        if !ACCELERATORS.contains(&accelerator.as_str()) {
            return Err(anyhow!(
                "Choose a Kaggle accelerator: {}.",
                ACCELERATORS.join(", ")
            ));
        }
        let ttl_seconds = args
            .timeout
            .as_deref()
            .map(crate::downstream::bridge::jobs::huggingface::parse_timeout)
            .transpose()?
            .unwrap_or(runtime.ttl_minutes * 60);
        if !(60..=43_200).contains(&ttl_seconds) {
            return Err(anyhow!("Kaggle lifetime must be 60–43200 seconds."));
        }
        Ok(Self {
            run_id: run_id.into(),
            account: String::new(),
            accelerator,
            ttl_seconds,
            competition_sources: runtime.competition_sources.clone(),
            dataset_sources: runtime.dataset_sources.clone(),
            command: command.into(),
            runtime,
            pooled: true,
        })
    }
    fn path(run_id: &str) -> Result<PathBuf> {
        let id = uuid::Uuid::parse_str(run_id)?;
        Ok(root().join("runs").join(format!("{id}.json")))
    }
    pub fn save(&self) -> Result<()> {
        save_json(&Self::path(&self.run_id)?, self)
    }
    pub fn load(run_id: &str) -> Result<Self> {
        let value: Self = serde_json::from_slice(&std::fs::read(Self::path(run_id)?)?)?;
        if value.run_id != run_id {
            return Err(anyhow!("Kaggle launch identity mismatch"));
        }
        Ok(value)
    }
    pub async fn call(&self, action: &str) -> Result<Value> {
        let mut value = serde_json::to_value(self)?;
        value["action"] = json!(action);
        value["command"] = json!(&self.command);
        call(
            &self.runtime,
            value,
            if action == "start" { 150 } else { 55 },
        )
        .await
    }

    pub fn stopped(&self) -> bool {
        let Ok(id) = uuid::Uuid::parse_str(&self.run_id) else {
            return false;
        };
        let receipt = root()
            .join("sessions")
            .join(id.simple().to_string())
            .join("stop-confirmed.json");
        std::fs::read(receipt)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .is_some_and(|v| {
                v["session_id"] == id.simple().to_string()
                    && v["account"] == self.account
                    && v["stopped"] == true
            })
    }
}

pub fn python() -> PathBuf {
    root().join(if cfg!(windows) {
        "runtime/Scripts/python.exe"
    } else {
        "runtime/bin/python"
    })
}

pub async fn call(settings: &Settings, mut request: Value, seconds: u64) -> Result<Value> {
    request["runtime"] = serde_json::to_value(settings)?;
    request["stateRoot"] = json!(root().join("sessions"));
    request["home"] = json!(root());
    let package = super::package::extract()?;
    let action = request["action"].as_str().unwrap_or("operation").to_owned();
    std::fs::create_dir_all(root().join("tmp"))?;
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(root().join("adapter.log"))?;
    let mut command = tokio::process::Command::new(python());
    command
        .arg("-u")
        .arg(package.join("bridge.py"))
        .current_dir(&package)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log))
        .kill_on_drop(true);
    // The account profile owns Kaggle credentials. Agent/provider tokens do
    // not belong in the bootstrap child or in the remote notebook.
    command.env_clear();
    for (key, value) in std::env::vars_os() {
        if [
            "ALLUSERSPROFILE",
            "APPDATA",
            "COMSPEC",
            "HOMEDRIVE",
            "HOMEPATH",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "PROGRAMDATA",
            "PROGRAMFILES",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERDOMAIN",
            "USERNAME",
            "USERPROFILE",
            "WINDIR",
            "HOME",
            "LANG",
            "SHELL",
            "TMPDIR",
            "USER",
            "AI_SCIENTIST_KAGGLE_PROXY_PORT",
        ]
        .iter()
        .any(|allowed| key.to_string_lossy().eq_ignore_ascii_case(allowed))
        {
            command.env(key, value);
        }
    }
    command
        .env("TEMP", root().join("tmp"))
        .env("TMP", root().join("tmp"))
        .env("TMPDIR", root().join("tmp"))
        .env("PLAYWRIGHT_BROWSERS_PATH", root().join("browsers"))
        .env("ORX_KAGGLE_HOME", root())
        .env("PYTHONUTF8", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1");
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    let mut child = command.spawn()?;
    let mut input = child
        .stdin
        .take()
        .ok_or_else(|| anyhow!("Kaggle stdin unavailable"))?;
    input.write_all(&serde_json::to_vec(&request)?).await?;
    drop(input);
    let output = tokio::time::timeout(Duration::from_secs(seconds), child.wait_with_output())
        .await
        .map_err(|_| {
            anyhow!("Kaggle {action} timed out; inspect the saved session before retrying.")
        })??;
    let envelope: Value = serde_json::from_slice(&output.stdout).map_err(|_| {
        anyhow!("Kaggle {action} returned no valid receipt; see the local adapter log.")
    })?;
    if !output.status.success() || envelope["ok"] != true {
        return Err(anyhow!("Kaggle {action} failed ({}); see Kaggle adapter.log. No replacement notebook was submitted.", envelope["errorType"].as_str().unwrap_or("unknown")));
    }
    Ok(envelope["result"].clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn saved_launch_ids_cannot_escape_the_runtime() {
        assert!(Launch::path("../../other").is_err());
        assert!(Launch::path("9ef38277-9183-47b0-b869-62fe43701b61").is_ok());
    }
}
