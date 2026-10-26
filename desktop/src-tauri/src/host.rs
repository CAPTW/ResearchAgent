use std::{
    env,
    ffi::OsString,
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use tauri::{
    utils::config::WebviewUrl, AppHandle, Manager, PhysicalPosition, PhysicalSize, RunEvent, Url,
    WebviewWindow, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

const SIDECAR_PROGRAM: &str = "xrml-sidecar";
const READY_TIMEOUT_SECONDS: u64 = 35;
const DESKTOP_STATE_ENV: &str = "XRML_DESKTOP_STATE_FILE";
const WEBVIEW_DATA_ENV: &str = "WEBVIEW2_USER_DATA_FOLDER";

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct DesktopWindowState {
    pub(crate) x: i32,
    pub(crate) y: i32,
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) maximized: bool,
}

pub(crate) fn custom_window_state_path(value: Option<OsString>) -> Option<PathBuf> {
    let path = PathBuf::from(value?);
    if path.as_os_str().is_empty() || !path.is_absolute() {
        return None;
    }
    Some(path)
}

pub(crate) fn custom_webview_data_path(value: Option<OsString>) -> Option<PathBuf> {
    custom_window_state_path(value)
}

fn desktop_window_state_path() -> Option<PathBuf> {
    custom_window_state_path(env::var_os(DESKTOP_STATE_ENV))
}

fn load_custom_window_state(path: &Path) -> Option<DesktopWindowState> {
    let state: DesktopWindowState = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if state.width < 1100 || state.height < 700 {
        return None;
    }
    Some(state)
}

fn restore_custom_window_state(window: &WebviewWindow) {
    let Some(path) = desktop_window_state_path() else {
        return;
    };
    let Some(state) = load_custom_window_state(&path) else {
        return;
    };
    let _ = window.set_position(PhysicalPosition::new(state.x, state.y));
    let _ = window.set_size(PhysicalSize::new(state.width, state.height));
    if state.maximized {
        let _ = window.maximize();
    }
}

fn save_custom_window_state(app: &AppHandle) {
    let Some(path) = desktop_window_state_path() else {
        return;
    };
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let (Ok(position), Ok(size), Ok(maximized)) = (
        window.outer_position(),
        // set_size restores the client area; including decorations grows every reopen.
        window.inner_size(),
        window.is_maximized(),
    ) else {
        return;
    };
    let state = DesktopWindowState {
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
        maximized,
    };
    let Some(parent) = path.parent() else {
        return;
    };
    if fs::create_dir_all(parent).is_err() {
        return;
    }
    let Ok(serialized) = serde_json::to_vec(&state) else {
        return;
    };
    let _ = fs::write(path, serialized);
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SidecarCommandSpec {
    pub program: String,
    pub args: Vec<String>,
}

impl SidecarCommandSpec {
    pub fn new(data_dir: PathBuf, startup_timeout_seconds: u64) -> Self {
        Self {
            program: SIDECAR_PROGRAM.to_string(),
            args: vec![
                "--data-dir".to_string(),
                data_dir.to_string_lossy().into_owned(),
                "--startup-timeout-seconds".to_string(),
                startup_timeout_seconds.to_string(),
            ],
        }
    }
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub(crate) struct ReadyEvent {
    event: String,
    host: String,
    pub port: u16,
    app_path: String,
    pid: u32,
}

impl ReadyEvent {
    pub fn navigation_url(&self) -> String {
        format!("http://{}:{}{}", self.host, self.port, self.app_path)
    }
}

pub(crate) fn parse_ready_event(bytes: &[u8]) -> Result<ReadyEvent, &'static str> {
    let event: ReadyEvent = serde_json::from_slice(bytes).map_err(|_| "invalid_ready_json")?;
    if event.event != "ready"
        || event.host != "127.0.0.1"
        || event.port == 0
        || event.app_path != "/app"
        || event.pid == 0
    {
        return Err("invalid_ready_contract");
    }
    Ok(event)
}

pub(crate) fn is_startup_error(event: &CommandEvent) -> bool {
    startup_error_message(event).is_some()
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct StartupPhaseEvent {
    event: String,
    schema_version: u8,
    phase: String,
    monotonic_ns: u64,
    elapsed_ms: f64,
    pid: u32,
}

pub(crate) fn parse_startup_phase(bytes: &[u8], expected_pid: u32) -> Option<StartupPhaseEvent> {
    if bytes.len() > 512 {
        return None;
    }
    let event: StartupPhaseEvent = serde_json::from_slice(bytes).ok()?;
    if event.event != "startup_phase"
        || event.schema_version != 1
        || event.pid == 0
        || event.pid != expected_pid
        || event.monotonic_ns == 0
        || !event.elapsed_ms.is_finite()
        || event.elapsed_ms < 0.0
        || !matches!(
            event.phase.as_str(),
            "python_entry"
                | "sidecar_imports_complete"
                | "configuration_started"
                | "configuration_complete"
                | "listener_bind_started"
                | "listener_bound"
                | "runtime_configured"
                | "application_import_started"
                | "application_import_complete"
                | "application_factory_started"
                | "application_factory_complete"
                | "server_import_started"
                | "server_import_complete"
                | "server_starting"
                | "lifespan_started"
                | "lifespan_enter_complete"
                | "lifespan_failed"
                | "server_started"
                | "readiness_confirmed"
        )
    {
        return None;
    }
    Some(event)
}

fn trace_host_phase(started: Option<Instant>, phase: &'static str) {
    if let Some(started) = started {
        let _ = writeln!(
            std::io::stderr().lock(),
            "{}",
            serde_json::json!({"event": "startup_host_phase", "schema_version": 1,
                "phase": phase, "elapsed_ms": started.elapsed().as_secs_f64() * 1000.0,
                "pid": std::process::id()})
        );
    }
}

pub(crate) fn startup_error_message(event: &CommandEvent) -> Option<&'static str> {
    match event {
        CommandEvent::Error(_) => Some("로컬 연구 서비스가 시작 중 오류를 보고했습니다."),
        // Application diagnostics (including successful readiness requests)
        // also use stderr. Only a fatal lifecycle event means startup failed.
        CommandEvent::Stderr(line) => serde_json::from_slice::<serde_json::Value>(line)
            .ok()
            .filter(|message| {
                message.get("event").and_then(|value| value.as_str()) == Some("fatal")
            })
            .map(
                |message| match message.get("code").and_then(|value| value.as_str()) {
                    Some("configuration_failure") => {
                        "로컬 연구 서비스의 시작 설정이 올바르지 않습니다."
                    }
                    Some("frontend_dist_missing") => {
                        "로컬 워크스페이스 화면 파일을 찾을 수 없습니다."
                    }
                    Some("port_bind_failure") => "로컬 연구 서비스의 통신 포트를 열지 못했습니다.",
                    Some("app_startup_failure") => "로컬 연구 서비스의 초기화에 실패했습니다.",
                    Some("readiness_timeout") => "로컬 연구 서비스 준비 시간이 초과되었습니다.",
                    Some("unexpected_crash") => "로컬 연구 서비스가 예기치 않게 종료되었습니다.",
                    _ => "로컬 연구 서비스가 시작 중 오류를 보고했습니다.",
                },
            ),
        _ => None,
    }
}

pub(crate) fn is_allowed_navigation(value: &str) -> bool {
    let Ok(url) = Url::parse(value) else {
        return false;
    };
    let loading_surface = (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"));
    if loading_surface {
        return true;
    }
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port().is_some()
        && (url.path() == "/app" || url.path().starts_with("/app/"))
}

struct OwnedSidecar {
    pid: u32,
    child: CommandChild,
    ready: bool,
    startup_failure: Option<&'static str>,
}

#[derive(Clone, Default)]
struct HostState {
    sidecar: Arc<Mutex<SidecarSlot<OwnedSidecar>>>,
    restart_operation: Arc<tauri::async_runtime::Mutex<()>>,
}

pub(crate) struct SidecarSlot<T> {
    pub owned: Option<T>,
    pub closing: bool,
}

impl<T> Default for SidecarSlot<T> {
    fn default() -> Self {
        Self {
            owned: None,
            closing: false,
        }
    }
}

pub(crate) fn register_sidecar<T>(
    state: &Mutex<SidecarSlot<T>>,
    child: T,
    publish: impl FnOnce(),
    dispose: impl FnOnce(T),
) -> Result<(), &'static str> {
    let reason = match state.lock() {
        Ok(mut slot) if !slot.closing && slot.owned.is_none() => {
            slot.owned = Some(child);
            publish();
            return Ok(());
        }
        Ok(_) => "Native sidecar registration was superseded or closed",
        Err(_) => "Native sidecar state is unavailable",
    };
    // A spawned child must not be dropped unowned. Cleanup may block, so it
    // runs only after releasing the state lock and cannot delay UI enqueue.
    dispose(child);
    Err(reason)
}

pub(crate) fn retire_sidecar<T>(state: &Mutex<SidecarSlot<T>>, closing: bool) -> Option<T> {
    let mut slot = state.lock().ok()?;
    slot.closing |= closing;
    slot.owned.take()
}

pub(crate) async fn with_restart_operation<T>(
    operation: &tauri::async_runtime::Mutex<()>,
    restart: impl FnOnce() -> T,
) -> T {
    let _guard = operation.lock().await;
    restart()
}

pub(crate) fn host_failure_url(message: &str) -> Url {
    // WebView2 uses Tauri's HTTP asset origin; navigate() does not translate
    // the custom scheme as WebviewUrl::App does during window creation.
    let origin = if cfg!(any(target_os = "windows", target_os = "android")) {
        "http://tauri.localhost/index.html"
    } else {
        "tauri://localhost/index.html"
    };
    let mut url = Url::parse(origin).expect("static loading URL");
    url.query_pairs_mut().append_pair("failure", message);
    url
}

fn host_failure(window: &WebviewWindow, message: &'static str) {
    // The local page renders its reason after loading, without a timer racing
    // navigation or injecting an error into a subsequently ready workspace.
    let _ = window.navigate(host_failure_url(message));
    let _ = window.show();
    let _ = window.set_focus();
}

pub(crate) fn apply_lifecycle_transition<S, T>(
    state: &Mutex<S>,
    transition: impl FnOnce(&mut S) -> Option<T>,
    publish: impl FnOnce(&T),
) -> Option<T> {
    let mut guard = state.lock().ok()?;
    let effect = transition(&mut guard)?;
    // The pinned Tauri runtime's navigate/show/set_focus setters enqueue from
    // worker threads without waiting for the main thread. Keep their enqueue
    // order atomic with replacement; do not call getters or wait/kill here.
    publish(&effect);
    Some(effect)
}

fn stop_sidecar(state: &HostState, graceful: bool) {
    stop_owned_sidecar(retire_sidecar(&state.sidecar, false), graceful);
}

fn close_sidecar(state: &HostState) {
    stop_owned_sidecar(retire_sidecar(&state.sidecar, true), true);
}

fn stop_owned_sidecar(owned: Option<OwnedSidecar>, graceful: bool) {
    let Some(mut owned) = owned else {
        return;
    };
    if graceful {
        let _ = owned.child.write(b"shutdown\n");
        std::thread::sleep(Duration::from_millis(1500));
    }
    let _ = owned.child.kill();
}

pub(crate) fn terminated_sidecar_message(
    active: Option<(u32, bool, Option<&'static str>)>,
    terminated_pid: u32,
) -> Option<&'static str> {
    let (active_pid, ready, startup_failure) = active?;
    if active_pid != terminated_pid {
        return None;
    }
    Some(startup_failure.unwrap_or(if !ready {
        "로컬 연구 서비스가 준비되기 전에 종료되었습니다."
    } else {
        "로컬 연구 서비스 연결이 종료되었습니다."
    }))
}

fn clear_sidecar_if_owned(state: &HostState, pid: u32, publish: impl FnOnce(&&'static str)) {
    apply_lifecycle_transition(
        &state.sidecar,
        |slot| {
            // A stopped/replaced process may report Terminated after its
            // successor starts. Retire only the current process.
            let message = terminated_sidecar_message(
                slot.owned
                    .as_ref()
                    .map(|owned| (owned.pid, owned.ready, owned.startup_failure)),
                pid,
            )?;
            slot.owned = None;
            Some(message)
        },
        publish,
    );
}

fn mark_ready_if_owned(state: &HostState, pid: u32, publish: impl FnOnce(&())) {
    apply_lifecycle_transition(
        &state.sidecar,
        |slot| {
            let owned = slot.owned.as_mut()?;
            if !accepts_ready_event(Some((owned.pid, owned.startup_failure)), pid) {
                return None;
            }
            owned.ready = true;
            Some(())
        },
        publish,
    );
}

pub(crate) fn accepts_ready_event(
    active: Option<(u32, Option<&'static str>)>,
    ready_pid: u32,
) -> bool {
    active.is_some_and(|(active_pid, startup_failure)| {
        active_pid == ready_pid && startup_failure.is_none()
    })
}

pub(crate) fn is_current_sidecar_waiting(active: Option<(u32, bool)>, pid: u32) -> bool {
    active.is_some_and(|(active_pid, ready)| active_pid == pid && !ready)
}

fn take_waiting_sidecar(
    state: &HostState,
    pid: u32,
    publish: impl FnOnce(&OwnedSidecar),
) -> Option<OwnedSidecar> {
    apply_lifecycle_transition(
        &state.sidecar,
        |slot| {
            if !is_current_sidecar_waiting(
                slot.owned.as_ref().map(|owned| (owned.pid, owned.ready)),
                pid,
            ) {
                return None;
            }
            slot.owned.take()
        },
        publish,
    )
}

pub(crate) fn startup_failure_for_event(
    active: Option<(u32, bool, Option<&'static str>)>,
    pid: u32,
    event: &CommandEvent,
) -> Option<&'static str> {
    let (active_pid, ready, previous_failure) = active?;
    if !is_current_sidecar_waiting(Some((active_pid, ready)), pid) {
        return None;
    }
    let message = startup_error_message(event)?;
    Some(previous_failure.unwrap_or(message))
}

fn record_startup_failure_if_owned(
    state: &HostState,
    pid: u32,
    event: &CommandEvent,
    publish: impl FnOnce(&&'static str),
) {
    apply_lifecycle_transition(
        &state.sidecar,
        |slot| {
            let owned = slot.owned.as_mut()?;
            let message = startup_failure_for_event(
                Some((owned.pid, owned.ready, owned.startup_failure)),
                pid,
                event,
            )?;
            // Keep the first cause bound to this child through exit/timeout.
            owned.startup_failure = Some(message);
            Some(message)
        },
        publish,
    );
}

pub(crate) fn readiness_timeout_message(startup_failure: Option<&'static str>) -> &'static str {
    startup_failure.unwrap_or("로컬 연구 서비스 준비 시간이 초과되었습니다.")
}

fn runtime_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    if let Some(value) = env::var_os("XRML_DATA_DIR") {
        let configured = PathBuf::from(value);
        if configured.as_os_str().is_empty() {
            return Err("XRML_DATA_DIR is blank".to_string());
        }
        return Ok(configured);
    }
    app.path()
        .app_local_data_dir()
        .map(|root| root.join("runtime-data"))
        .map_err(|_| "Native runtime data directory is unavailable".to_string())
}

fn start_sidecar(app: AppHandle) -> Result<(), String> {
    let trace_started =
        (env::var_os("XRML_STARTUP_TRACE") == Some(OsString::from("1"))).then(Instant::now);
    trace_host_phase(trace_started, "attempt_started");
    let state = app.state::<HostState>().inner().clone();
    if state
        .sidecar
        .lock()
        .map(|slot| slot.closing)
        .unwrap_or(true)
    {
        return Err("Native sidecar is shutting down".to_string());
    }
    stop_sidecar(&state, false);
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Native workspace window is unavailable".to_string())?;
    let data_dir = runtime_data_dir(&app)?;
    let spec = SidecarCommandSpec::new(data_dir.clone(), 30);
    let command = app
        .shell()
        .sidecar(&spec.program)
        .map_err(|_| "Packaged sidecar is unavailable".to_string())?
        .args(&spec.args)
        .env("XRML_DATA_DIR", data_dir);
    trace_host_phase(trace_started, "spawn_requested");
    let (mut receiver, child) = command
        .spawn()
        .map_err(|_| "Packaged sidecar could not be started".to_string())?;
    let pid = child.pid();
    trace_host_phase(trace_started, "spawn_returned");
    register_sidecar(
        &state.sidecar,
        OwnedSidecar {
            pid,
            child,
            ready: false,
            startup_failure: None,
        },
        || {
            // The previous child's queued failure must be followed by this
            // child's loading surface before it can publish readiness/failure.
            let mut loading_url = host_failure_url("");
            loading_url.set_query(None);
            let _ = window.navigate(loading_url);
        },
        |owned| {
            let _ = owned.child.kill();
        },
    )
    .map_err(str::to_string)?;

    let event_state = state.clone();
    let event_window = window.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = receiver.recv().await {
            if trace_started.is_some() {
                if let CommandEvent::Stderr(line) = &event {
                    // Never pass through raw stderr: only a bounded, typed,
                    // fixed-vocabulary event for this owned child can escape.
                    if let Some(phase) = parse_startup_phase(line, pid) {
                        if let Ok(serialized) = serde_json::to_string(&phase) {
                            let _ = writeln!(std::io::stderr().lock(), "{serialized}");
                        }
                    }
                }
            }
            match event {
                CommandEvent::Stdout(line) => {
                    if let Ok(ready) = parse_ready_event(&line) {
                        mark_ready_if_owned(&event_state, pid, |_| {
                            trace_host_phase(trace_started, "ready_received");
                            let Ok(url) = Url::parse(&ready.navigation_url()) else {
                                host_failure(
                                    &event_window,
                                    "로컬 워크스페이스 주소가 유효하지 않습니다.",
                                );
                                return;
                            };
                            if event_window.navigate(url).is_err() {
                                host_failure(
                                    &event_window,
                                    "로컬 워크스페이스 화면을 열지 못했습니다.",
                                );
                                return;
                            }
                            let _ = event_window.show();
                            let _ = event_window.set_focus();
                        });
                    }
                }
                event if is_startup_error(&event) => {
                    record_startup_failure_if_owned(&event_state, pid, &event, |message| {
                        host_failure(&event_window, message);
                    });
                }
                CommandEvent::Terminated(_) => {
                    clear_sidecar_if_owned(&event_state, pid, |message| {
                        host_failure(&event_window, message);
                    });
                    break;
                }
                _ => {}
            }
        }
    });

    let timeout_state = state;
    let timeout_window = window;
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(READY_TIMEOUT_SECONDS)).await;
        if let Some(owned) = take_waiting_sidecar(&timeout_state, pid, |owned| {
            trace_host_phase(trace_started, "readiness_deadline_missed");
            host_failure(
                &timeout_window,
                readiness_timeout_message(owned.startup_failure),
            );
        }) {
            let _ = owned.child.kill();
        }
    });
    Ok(())
}

#[tauri::command]
async fn restart_sidecar(app: AppHandle) -> Result<(), String> {
    let state = app.state::<HostState>().inner().clone();
    with_restart_operation(&state.restart_operation, || {
        stop_sidecar(&state, true);
        start_sidecar(app)
    })
    .await
}

fn build_window(app: &tauri::App) -> tauri::Result<WebviewWindow> {
    let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("XRML Research Agent")
        .inner_size(1536.0, 960.0)
        .min_inner_size(1100.0, 700.0)
        .visible(false)
        .on_navigation(|url| is_allowed_navigation(url.as_str()));
    // The async command owns native selection and writes; old hosts and browsers
    // retain their existing download behavior.
    if cfg!(windows) {
        builder = builder.initialization_script("window.__XRML_NATIVE_DOWNLOADS__=1;");
    }
    if let Some(webview_data_dir) = custom_webview_data_path(env::var_os(WEBVIEW_DATA_ENV)) {
        builder = builder.data_directory(webview_data_dir);
    }
    let window = builder.build()?;
    restore_custom_window_state(&window);
    Ok(window)
}

pub fn run() {
    let use_custom_window_state = desktop_window_state_path().is_some();
    let mut builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init());
    if !use_custom_window_state {
        builder = builder.plugin(tauri_plugin_window_state::Builder::default().build());
    }
    let application = builder
        .manage(HostState::default())
        .invoke_handler(tauri::generate_handler![
            restart_sidecar,
            crate::downloads::save_text_export
        ])
        .setup(|app| {
            build_window(app)?;
            if let Err(_error) = start_sidecar(app.handle().clone()) {
                if let Some(window) = app.get_webview_window("main") {
                    host_failure(&window, "패키지된 로컬 연구 서비스를 시작하지 못했습니다.");
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build XRML native workspace");

    application.run(|app, event| match event {
        RunEvent::WindowEvent {
            event: WindowEvent::CloseRequested { .. },
            ..
        }
        | RunEvent::ExitRequested { .. } => {
            save_custom_window_state(app);
            let state = app.state::<HostState>().inner().clone();
            close_sidecar(&state);
        }
        RunEvent::WindowEvent {
            event: WindowEvent::Destroyed,
            ..
        }
        | RunEvent::Exit => {
            let state = app.state::<HostState>().inner().clone();
            close_sidecar(&state);
        }
        _ => {}
    });
}
