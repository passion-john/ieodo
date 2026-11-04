//! Ship the capture installer with the CLI; no source checkout is needed.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

const HOOKS: &str = include_str!("../integrations/continuity/ieodo_hooks.py");
const SETUP: &str = include_str!("../integrations/continuity/ieodo_setup.py");

/// Writes `contents` at `path` unless it is already there, replacing an older
/// version atomically so a hook that runs meanwhile reads one whole file.
fn write_runtime(path: &Path, contents: &str) -> Result<(), String> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(format!(
                "runtime file must not be a symlink: {}",
                path.display()
            ));
        }
        Ok(_) if fs::read(path).map_err(|e| e.to_string())? == contents.as_bytes() => return Ok(()),
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.to_string()),
    }
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_nanos();
    let temporary = path.with_extension(format!("{}.{stamp}.tmp", std::process::id()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let result = (|| {
        let mut file = options.open(&temporary).map_err(|e| e.to_string())?;
        file.write_all(contents.as_bytes())
            .map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
        fs::rename(&temporary, path).map_err(|e| e.to_string())
    })();
    let _ = fs::remove_file(temporary);
    result
}

/// The bundled capture adapter lives at one fixed path,
/// `IEODO_HOME/continuity/runtime/current/`, so the hook commands written
/// into projects never change and Codex's hook trust survives upgrades. The
/// files are refreshed by `setup` and whenever the server starts.
pub fn refresh_runtime(home: &Path) -> Result<PathBuf, String> {
    let dir = home.join("continuity/runtime/current");
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(&dir).map_err(|e| e.to_string())?;
    if fs::symlink_metadata(&dir)
        .map_err(|e| e.to_string())?
        .file_type()
        .is_symlink()
    {
        return Err("bundled runtime directory must not be a symlink".into());
    }
    write_runtime(&dir.join("ieodo_hooks.py"), HOOKS)?;
    write_runtime(&dir.join("ieodo_setup.py"), SETUP)?;
    dir.canonicalize().map_err(|e| e.to_string())
}

pub fn run(args: &[String]) -> Result<(), String> {
    let dir = refresh_runtime(&crate::data_dir())?;
    let status = Command::new("python3")
        .arg(dir.join("ieodo_setup.py"))
        .args(args)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .status()
        .map_err(|e| format!("setup requires Python 3: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "setup did not complete (exit {:?}); see its result above",
            status.code()
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_runtime_path_is_fixed_and_upgrades_replace_its_files() {
        let home = tempfile::tempdir().unwrap();
        let first = refresh_runtime(home.path()).unwrap();
        assert!(first.ends_with("continuity/runtime/current"));
        let hook = first.join("ieodo_hooks.py");
        let modified = fs::metadata(&hook).unwrap().modified().unwrap();
        // Unchanged contents are left alone.
        assert_eq!(refresh_runtime(home.path()).unwrap(), first);
        assert_eq!(fs::metadata(&hook).unwrap().modified().unwrap(), modified);
        // An older version at the same path is replaced by the bundled one.
        fs::write(&hook, "old version").unwrap();
        assert_eq!(refresh_runtime(home.path()).unwrap(), first);
        assert_eq!(fs::read_to_string(&hook).unwrap(), HOOKS);
    }
}
