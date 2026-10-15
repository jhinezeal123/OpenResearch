//! Compile-time assets, including in debug builds. No external checkout at runtime.
use crate::downstream::bridge::error::Result;
use sha2::{Digest, Sha256};
use std::path::PathBuf;
const FILES: &[(&str, &str)] = &[
    ("bridge.py", include_str!("python/bridge.py")),
    (
        "native/account_admin.py",
        include_str!("python/native/account_admin.py"),
    ),
    (
        "native/account_runtime.py",
        include_str!("python/native/account_runtime.py"),
    ),
    (
        "native/account_store.py",
        include_str!("python/native/account_store.py"),
    ),
    (
        "native/auto_login/__init__.py",
        include_str!("python/native/auto_login/__init__.py"),
    ),
    (
        "native/auto_login/recovery.py",
        include_str!("python/native/auto_login/recovery.py"),
    ),
    (
        "native/auto_login/web_session.py",
        include_str!("python/native/auto_login/web_session.py"),
    ),
    (
        "native/cookie_client.py",
        include_str!("python/native/cookie_client.py"),
    ),
    (
        "native/interface_ai_scientist/__init__.py",
        include_str!("python/native/interface_ai_scientist/__init__.py"),
    ),
    (
        "native/interface_ai_scientist/bootstrap.py",
        include_str!("python/native/interface_ai_scientist/bootstrap.py"),
    ),
    (
        "native/interface_ai_scientist/bootstrap_service.py",
        include_str!("python/native/interface_ai_scientist/bootstrap_service.py"),
    ),
    (
        "native/interface_ai_scientist/session.py",
        include_str!("python/native/interface_ai_scientist/session.py"),
    ),
    (
        "native/interface_ai_scientist/terminal_remote.py",
        include_str!("python/native/interface_ai_scientist/terminal_remote.py"),
    ),
    (
        "native/provider_metrics.py",
        include_str!("python/native/provider_metrics.py"),
    ),
    ("native/proxy.py", include_str!("python/native/proxy.py")),
    (
        "native/proxy_control.py",
        include_str!("python/native/proxy_control.py"),
    ),
    (
        "native/sdk_version.py",
        include_str!("python/native/sdk_version.py"),
    ),
    (
        "native/web_session.py",
        include_str!("python/native/web_session.py"),
    ),
    ("pool.py", include_str!("python/pool.py")),
    ("remote.py", include_str!("python/remote.py")),
    ("worker.py", include_str!("python/worker.py")),
];
pub fn extract() -> Result<PathBuf> {
    let mut hash = Sha256::new();
    for (name, body) in FILES {
        hash.update(name.as_bytes());
        hash.update(body.as_bytes());
    }
    let version = format!("{:x}", hash.finalize());
    let root = super::runtime::root().join("package").join(version);
    for (name, body) in FILES {
        let path = root.join(name);
        if !path.is_file() {
            std::fs::create_dir_all(path.parent().expect("package parent"))?;
            let tmp = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
            std::fs::write(&tmp, body)?;
            if let Err(error) = std::fs::rename(&tmp, &path) {
                if !path.is_file() {
                    return Err(error.into());
                }
                let _ = std::fs::remove_file(tmp);
            }
        }
    }
    Ok(root)
}
