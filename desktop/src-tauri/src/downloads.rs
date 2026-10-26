//! User-selected text exports. No destination path can be supplied by JavaScript.
use serde::Serialize;
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, AtomicU64, Ordering},
};
use tauri::{Url, WebviewWindow};

const MAX_EXPORT_BYTES: usize = 8 * 1024 * 1024;
static SAVING: AtomicBool = AtomicBool::new(false);
static SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct SaveGuard;
impl SaveGuard {
    fn acquire() -> Result<Self, &'static str> {
        SAVING
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| Self)
            .map_err(|_| "export_busy")
    }
}
impl Drop for SaveGuard {
    fn drop(&mut self) {
        SAVING.store(false, Ordering::Release);
    }
}

#[derive(Serialize)]
pub(crate) struct SaveResult {
    state: &'static str,
    path: Option<PathBuf>,
    bytes: usize,
}

fn allowed_page(page: &Url) -> bool {
    page.scheme() == "http"
        && page.host_str() == Some("127.0.0.1")
        && page.port().is_some()
        && (page.path() == "/app" || page.path().starts_with("/app/"))
}

fn valid_export(filename: &str, content: &str) -> bool {
    !filename.is_empty()
        && filename.encode_utf16().count() <= 235
        && !filename
            .chars()
            .any(|c| c.is_control() || "<>:\"/\\|?*".contains(c))
        && matches!(
            Path::new(filename).extension().and_then(|s| s.to_str()),
            Some("md" | "json")
        )
        && content.len() <= MAX_EXPORT_BYTES
}

struct StagingFile(PathBuf);
impl Drop for StagingFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn write_export(destination: &Path, content: &[u8]) -> io::Result<()> {
    if !destination.is_absolute() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "absolute destination required",
        ));
    }
    let parent = destination
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing parent"))?;
    let (staging, mut file) = (0..32)
        .find_map(|_| {
            let path = parent.join(format!(
                ".xrml-export-{}-{}.tmp",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            match OpenOptions::new().create_new(true).write(true).open(&path) {
                Ok(file) => Some(Ok((StagingFile(path), file))),
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => None,
                Err(e) => Some(Err(e)),
            }
        })
        .unwrap_or_else(|| {
            Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "staging paths occupied",
            ))
        })?;
    file.write_all(content)?;
    file.sync_all()?;
    drop(file);
    // Same-directory rename leaves an existing destination intact if writing fails.
    // On Windows std::fs::rename uses MoveFileExW with REPLACE_EXISTING.
    fs::rename(&staging.0, destination)
}

#[tauri::command]
pub(crate) async fn save_text_export(
    window: WebviewWindow,
    filename: String,
    content: String,
) -> Result<SaveResult, String> {
    eprintln!("XRML text export: native command received");
    if window.label() != "main"
        || !window.url().is_ok_and(|p| allowed_page(&p))
        || !valid_export(&filename, &content)
    {
        return Err("export_not_allowed".into());
    }
    let guard = SaveGuard::acquire().map_err(str::to_owned)?;
    #[cfg(windows)]
    let owner = window
        .hwnd()
        .map_err(|_| "export_window_unavailable".to_owned())?
        .0 as usize;
    #[cfg(not(windows))]
    let owner = 0;
    // Never open a modal dialog inside a synchronous WebView2 event callback.
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        let path = choose_destination(owner, Path::new(&filename))
            .map_err(|_| "export_dialog_failed".to_owned())?;
        let Some(path) = path else {
            return Ok(SaveResult {
                state: "cancelled",
                path: None,
                bytes: 0,
            });
        };
        write_export(&path, content.as_bytes()).map_err(|_| "export_write_failed".to_owned())?;
        Ok(SaveResult {
            state: "saved",
            path: Some(path),
            bytes: content.len(),
        })
    })
    .await
    .map_err(|_| "export_task_failed".to_owned())?
}

#[cfg(windows)]
fn choose_destination(owner: usize, filename: &Path) -> Result<Option<PathBuf>, ()> {
    use std::{
        ffi::OsString,
        os::windows::ffi::{OsStrExt, OsStringExt},
    };
    use windows_sys::Win32::UI::Controls::Dialogs::{
        CommDlgExtendedError, GetSaveFileNameW, OFN_DONTADDTORECENT, OFN_EXPLORER, OFN_NOCHANGEDIR,
        OFN_OVERWRITEPROMPT, OFN_PATHMUSTEXIST, OPENFILENAMEW,
    };
    let mut buffer = vec![0u16; 32768];
    let suggested: Vec<u16> = filename.as_os_str().encode_wide().collect();
    if suggested.len() >= buffer.len() || suggested.contains(&0) {
        return Err(());
    }
    buffer[..suggested.len()].copy_from_slice(&suggested);
    let title: Vec<u16> = "XRML 내보내기 저장\0".encode_utf16().collect();
    let filter: Vec<u16> = "모든 파일\0*.*\0\0".encode_utf16().collect();
    let extension: Vec<u16> = filename
        .extension()
        .unwrap_or_default()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let initial = std::env::var_os("XRML_DATA_DIR")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute() && p.is_dir())
        .map(|p| {
            p.as_os_str()
                .encode_wide()
                .chain(Some(0))
                .collect::<Vec<_>>()
        });
    let mut dialog = OPENFILENAMEW {
        lStructSize: std::mem::size_of::<OPENFILENAMEW>() as u32,
        hwndOwner: owner as _,
        lpstrFile: buffer.as_mut_ptr(),
        nMaxFile: buffer.len() as u32,
        lpstrTitle: title.as_ptr(),
        lpstrFilter: filter.as_ptr(),
        lpstrDefExt: extension.as_ptr(),
        lpstrInitialDir: initial.as_ref().map_or(std::ptr::null(), |s| s.as_ptr()),
        Flags: OFN_EXPLORER
            | OFN_OVERWRITEPROMPT
            | OFN_PATHMUSTEXIST
            | OFN_NOCHANGEDIR
            | OFN_DONTADDTORECENT,
        ..Default::default()
    };
    // Owned UTF-16 buffers outlive this blocking call. Only the native picker supplies the destination.
    if unsafe { GetSaveFileNameW(&mut dialog) } == 0 {
        let error = unsafe { CommDlgExtendedError() };
        eprintln!("XRML text export: dialog closed, extended_error={error}");
        return if error == 0 { Ok(None) } else { Err(()) };
    }
    let end = buffer.iter().position(|c| *c == 0).ok_or(())?;
    Ok(Some(PathBuf::from(OsString::from_wide(&buffer[..end]))))
}
#[cfg(not(windows))]
fn choose_destination(_owner: usize, _filename: &Path) -> Result<Option<PathBuf>, ()> {
    Err(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn export_capability_matches_origin_only_ipc_without_granting_other_commands() {
        use tauri::utils::acl::RemoteUrlPattern;
        let origin = Url::parse("http://127.0.0.1:61204").unwrap();
        // The original path-scoped capability rejected Windows IPC's Origin.
        let previous: RemoteUrlPattern = "http://127.0.0.1:*/app*".parse().unwrap();
        assert!(!previous.test(&origin));
        let capability: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/text-export.json")).unwrap();
        assert_eq!(
            capability["permissions"],
            serde_json::json!(["allow-save-text-export"])
        );
        assert_eq!(capability["windows"], serde_json::json!(["main"]));
        assert_eq!(capability["local"], false);
        let pattern: RemoteUrlPattern = capability["remote"]["urls"][0]
            .as_str()
            .unwrap()
            .parse()
            .unwrap();
        assert!(pattern.test(&origin));
        assert!(!pattern.test(&Url::parse("https://example.com/app").unwrap()));
        // Origin-level transport permission is insufficient at the command boundary.
        assert!(!allowed_page(&origin));
        assert!(allowed_page(&origin.join("/app/topics/x/reports").unwrap()));
    }
    #[test]
    fn restricts_origin_and_export_payload() {
        assert!(allowed_page(
            &Url::parse("http://127.0.0.1:4000/app/topics/x").unwrap()
        ));
        for url in [
            "https://example.com/app",
            "http://127.0.0.1:4000/application",
            "file:///D:/app",
            "http://tauri.localhost/app",
        ] {
            assert!(!allowed_page(&Url::parse(url).unwrap()));
        }
        assert!(valid_export("한글 검토.md", "한글\r\n😀"));
        for filename in [
            "../x.md",
            "D:\\x.md",
            "x:stream.json",
            "x.exe",
            "x\0.json",
            "x.json ",
        ] {
            assert!(!valid_export(filename, "text"));
        }
        assert!(!valid_export("x.json", &"x".repeat(MAX_EXPORT_BYTES + 1)));
    }
    #[test]
    fn exact_bytes_replace_and_failed_replace_preserves_existing_data() {
        let root = std::env::temp_dir().join(format!(
            "xrml-save-test-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let target = root.join("한글 검토.md");
        fs::write(&target, b"old").unwrap();
        let content = "원문\r\n😀\n".as_bytes();
        write_export(&target, content).unwrap();
        assert_eq!(fs::read(&target).unwrap(), content);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            let locked = OpenOptions::new()
                .read(true)
                .share_mode(0)
                .open(&target)
                .unwrap();
            assert!(write_export(&target, b"must not replace a locked original").is_err());
            drop(locked);
            assert_eq!(fs::read(&target).unwrap(), content);
            assert_eq!(fs::read_dir(&root).unwrap().count(), 1);
        }
        let blocked = root.join("directory.json");
        fs::create_dir(&blocked).unwrap();
        fs::write(blocked.join("keep"), b"untouched").unwrap();
        assert!(write_export(&blocked, b"replacement").is_err());
        assert_eq!(fs::read(blocked.join("keep")).unwrap(), b"untouched");
        assert_eq!(
            fs::read_dir(&root).unwrap().count(),
            2,
            "no staging file after failure"
        );
        assert!(write_export(&root.join("absent/file.json"), b"x").is_err());
        assert!(write_export(Path::new("relative.json"), b"x").is_err());
        fs::remove_file(target).unwrap();
        fs::remove_file(blocked.join("keep")).unwrap();
        fs::remove_dir(blocked).unwrap();
        fs::remove_dir(root).unwrap();
    }
    #[test]
    fn concurrent_request_is_rejected_until_guard_drops() {
        let first = SaveGuard::acquire().unwrap();
        assert!(SaveGuard::acquire().is_err());
        drop(first);
        assert!(SaveGuard::acquire().is_ok());
    }
}
