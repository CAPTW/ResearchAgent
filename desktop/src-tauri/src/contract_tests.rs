use std::{ffi::OsString, path::PathBuf};

use crate::host::{
    accepts_ready_event, apply_lifecycle_transition, custom_webview_data_path,
    custom_window_state_path, host_failure_url, is_allowed_navigation, is_current_sidecar_waiting,
    is_startup_error, parse_ready_event, parse_startup_phase, readiness_timeout_message,
    register_sidecar, retire_sidecar, startup_error_message, startup_failure_for_event,
    terminated_sidecar_message, with_restart_operation, DesktopWindowState, SidecarCommandSpec,
    SidecarSlot,
};
use tauri_plugin_shell::process::CommandEvent;

const HOST_SOURCE: &str = include_str!("host.rs");
const TAURI_CONFIG: &str = include_str!("../tauri.conf.json");

#[test]
fn startup_trace_accepts_only_owned_child_fixed_vocabulary_without_extra_values() {
    let valid = serde_json::json!({"event": "startup_phase", "schema_version": 1,
        "phase": "listener_bound", "monotonic_ns": 123456, "elapsed_ms": 10.5, "pid": 123});
    let bytes = serde_json::to_vec(&valid).unwrap();
    let phase = parse_startup_phase(&bytes, 123).expect("bounded diagnostic");
    assert_eq!(serde_json::to_value(phase).unwrap(), valid);
    assert!(!is_startup_error(&CommandEvent::Stderr(bytes.clone())));
    assert!(parse_startup_phase(&bytes, 456).is_none());
    for (field, value) in [
        ("event", serde_json::json!("fatal")),
        ("phase", serde_json::json!("private-path secret-value")),
        ("schema_version", serde_json::json!(2)),
        ("pid", serde_json::json!(0)),
        ("elapsed_ms", serde_json::json!(-1.0)),
        ("monotonic_ns", serde_json::json!(0)),
        (
            "detail",
            serde_json::json!("private value must never be forwarded"),
        ),
    ] {
        let mut invalid = valid.clone();
        invalid[field] = value;
        assert!(parse_startup_phase(&serde_json::to_vec(&invalid).unwrap(), 123).is_none());
    }
    assert!(parse_startup_phase(b"ordinary stderr private value", 123).is_none());
    assert!(parse_startup_phase(&vec![b' '; 513], 123).is_none());
}

#[test]
fn deadline_only_targets_its_own_process_before_readiness() {
    assert!(is_current_sidecar_waiting(Some((100, false)), 100));
    // A late deadline must leave a ready process, its replacement and an
    // intentionally stopped process alone.
    assert!(!is_current_sidecar_waiting(Some((100, true)), 100));
    assert!(!is_current_sidecar_waiting(Some((200, false)), 100));
    assert!(!is_current_sidecar_waiting(Some((200, true)), 100));
    assert!(!is_current_sidecar_waiting(None, 100));
}

#[test]
fn late_exit_from_a_replaced_sidecar_does_not_report_a_new_failure() {
    for ready in [false, true] {
        assert_eq!(
            terminated_sidecar_message(Some((200, ready, None)), 100),
            None
        );
    }
}

#[test]
fn intentional_shutdown_does_not_report_a_crash() {
    // stop_sidecar takes ownership out of the slot before sending shutdown.
    assert_eq!(terminated_sidecar_message(None, 100), None);
}

#[test]
fn current_sidecar_crashes_still_report_the_correct_phase() {
    assert_eq!(
        terminated_sidecar_message(Some((100, false, None)), 100),
        Some("로컬 연구 서비스가 준비되기 전에 종료되었습니다.")
    );
    assert_eq!(
        terminated_sidecar_message(Some((100, true, None)), 100),
        Some("로컬 연구 서비스 연결이 종료되었습니다.")
    );
}

#[test]
fn typed_startup_failures_use_safe_fixed_messages() {
    for (code, expected) in [
        (
            "configuration_failure",
            "로컬 연구 서비스의 시작 설정이 올바르지 않습니다.",
        ),
        (
            "frontend_dist_missing",
            "로컬 워크스페이스 화면 파일을 찾을 수 없습니다.",
        ),
        (
            "port_bind_failure",
            "로컬 연구 서비스의 통신 포트를 열지 못했습니다.",
        ),
        (
            "app_startup_failure",
            "로컬 연구 서비스의 초기화에 실패했습니다.",
        ),
        (
            "readiness_timeout",
            "로컬 연구 서비스 준비 시간이 초과되었습니다.",
        ),
        (
            "unexpected_crash",
            "로컬 연구 서비스가 예기치 않게 종료되었습니다.",
        ),
    ] {
        let payload = serde_json::json!({
            "event": "fatal", "code": code,
            "detail": "token=secret D:\\private\\research\\database.sqlite3", "pid": 100,
        });
        let event = CommandEvent::Stderr(serde_json::to_vec(&payload).unwrap());
        assert_eq!(startup_error_message(&event), Some(expected));
        assert!(!expected.contains("secret"));
        assert!(!expected.contains("D:"));
    }
}

#[test]
fn confirmed_startup_failure_survives_its_exit_only() {
    let failure = "로컬 연구 서비스의 초기화에 실패했습니다.";
    assert_eq!(
        terminated_sidecar_message(Some((100, false, Some(failure))), 100),
        Some(failure)
    );
    assert_eq!(
        terminated_sidecar_message(Some((200, false, Some(failure))), 100),
        None
    );
    assert_eq!(terminated_sidecar_message(None, 100), None);
    // A new child has no inherited failure; successful startup keeps normal
    // post-ready crash classification.
    assert_eq!(
        terminated_sidecar_message(Some((200, true, None)), 200),
        Some("로컬 연구 서비스 연결이 종료되었습니다.")
    );
}

#[test]
fn fatal_child_cannot_become_ready_and_restarted_child_has_no_inherited_failure() {
    let failure = "로컬 연구 서비스의 초기화에 실패했습니다.";
    assert!(!accepts_ready_event(Some((100, Some(failure))), 100));
    assert!(!accepts_ready_event(None, 100));
    // A restart supplies a fresh child's state. Neither the old ready event
    // nor its failure may change the new child's successful transition.
    let restarted = Some((200, None));
    assert!(!accepts_ready_event(restarted, 100));
    assert!(accepts_ready_event(restarted, 200));
    assert_eq!(
        terminated_sidecar_message(Some((200, false, None)), 100),
        None
    );
    assert_eq!(
        terminated_sidecar_message(Some((200, true, None)), 200),
        Some("로컬 연구 서비스 연결이 종료되었습니다.")
    );
}

#[test]
fn unknown_startup_failure_details_are_never_displayed() {
    for code in [
        serde_json::json!("token=secret"),
        serde_json::json!(null),
        serde_json::json!(123),
    ] {
        let event = CommandEvent::Stderr(
            serde_json::to_vec(&serde_json::json!({
                "event": "fatal", "code": code, "detail": "D:\\private\\secret",
            }))
            .unwrap(),
        );
        assert_eq!(
            startup_error_message(&event),
            Some("로컬 연구 서비스가 시작 중 오류를 보고했습니다.")
        );
    }
    assert_eq!(
        startup_error_message(&CommandEvent::Error("token=secret".into())),
        Some("로컬 연구 서비스가 시작 중 오류를 보고했습니다.")
    );
    assert_eq!(
        startup_error_message(&CommandEvent::Stderr(b"not-json secret".to_vec())),
        None
    );
    assert_eq!(
        startup_error_message(&CommandEvent::Stderr(
            br#"{"event":"info","code":"app_startup_failure"}"#.to_vec()
        )),
        None
    );
}

#[test]
fn first_startup_failure_is_preserved_and_scoped_to_the_waiting_child() {
    let event = CommandEvent::Stderr(
        br#"{"event":"fatal","code":"app_startup_failure","detail":"token=secret","pid":999}"#
            .to_vec(),
    );
    // The process identity comes from its receiver, not a payload's pid.
    let first = startup_failure_for_event(Some((100, false, None)), 100, &event);
    assert_eq!(first, Some("로컬 연구 서비스의 초기화에 실패했습니다."));
    let later = CommandEvent::Error("private transport detail".into());
    assert_eq!(
        startup_failure_for_event(Some((100, false, first)), 100, &later),
        first
    );
    assert_eq!(
        startup_failure_for_event(Some((200, false, None)), 100, &event),
        None
    );
    assert_eq!(
        startup_failure_for_event(Some((100, true, None)), 100, &event),
        None
    );
    assert_eq!(startup_failure_for_event(None, 100, &event), None);
    assert_eq!(
        startup_failure_for_event(
            Some((100, false, first)),
            100,
            &CommandEvent::Stderr(b"ordinary warning".to_vec())
        ),
        None
    );
    // A replacement's first failure is evaluated from its own fresh state.
    assert_eq!(
        startup_failure_for_event(Some((200, false, None)), 200, &later),
        Some("로컬 연구 서비스가 시작 중 오류를 보고했습니다.")
    );
}

#[test]
fn readiness_deadline_preserves_an_already_reported_failure() {
    let first = startup_failure_for_event(
        Some((100, false, None)),
        100,
        &CommandEvent::Stderr(br#"{"event":"fatal","code":"app_startup_failure"}"#.to_vec()),
    );
    assert_eq!(
        readiness_timeout_message(first),
        "로컬 연구 서비스의 초기화에 실패했습니다."
    );
    assert_eq!(
        readiness_timeout_message(None),
        "로컬 연구 서비스 준비 시간이 초과되었습니다."
    );
    assert!(is_current_sidecar_waiting(Some((100, false)), 100));
}

fn assert_replacement_waits_for_publication(transition: &'static str) {
    use std::{
        sync::{mpsc, Arc, Mutex, TryLockError},
        thread,
        time::Duration,
    };

    let active = Arc::new(Mutex::new(Some((100, false, None))));
    let visible = Arc::new(Mutex::new(Vec::new()));
    let (publishing_tx, publishing_rx) = mpsc::channel();
    let (attempt_tx, attempt_rx) = mpsc::channel();
    let replacement_state = active.clone();
    let replacement_visible = visible.clone();
    let replacement = thread::spawn(move || {
        publishing_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let replace = |active: &mut Option<(u32, bool, Option<&'static str>)>| {
            *active = Some((200, true, None));
            replacement_visible.lock().unwrap().push(200);
        };
        match replacement_state.try_lock() {
            Ok(mut active) => {
                replace(&mut active);
                attempt_tx.send(false).unwrap();
            }
            Err(TryLockError::WouldBlock) => {
                attempt_tx.send(true).unwrap();
                replace(&mut replacement_state.lock().unwrap());
            }
            Err(TryLockError::Poisoned(_)) => panic!("poisoned lifecycle state"),
        };
    });
    let mut replacement_was_blocked = false;
    apply_lifecycle_transition(
        &active,
        |active| {
            let current = *active;
            let accepted = match transition {
                "failure" => startup_failure_for_event(
                    current,
                    100,
                    &CommandEvent::Error("private detail".into()),
                )
                .is_some(),
                "termination" => terminated_sidecar_message(current, 100).is_some(),
                "deadline" => {
                    is_current_sidecar_waiting(current.map(|(pid, ready, _)| (pid, ready)), 100)
                }
                "ready" => {
                    accepts_ready_event(current.map(|(pid, _, failure)| (pid, failure)), 100)
                }
                _ => unreachable!(),
            };
            if accepted && matches!(transition, "termination" | "deadline") {
                *active = None;
            }
            accepted.then_some(100)
        },
        |pid| {
            // Insert the replacement precisely after the event's state
            // check and before its native UI publication, without sleeps.
            publishing_tx.send(()).unwrap();
            replacement_was_blocked = attempt_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            visible.lock().unwrap().push(*pid);
        },
    )
    .unwrap();
    replacement.join().unwrap();
    assert_eq!(*visible.lock().unwrap(), vec![100, 200], "{transition}");
    assert!(replacement_was_blocked, "{transition}");
    assert_eq!(*active.lock().unwrap(), Some((200, true, None)));
}

#[test]
fn failure_publication_is_atomic_with_child_replacement() {
    assert_replacement_waits_for_publication("failure");
}

#[test]
fn termination_publication_is_atomic_with_child_replacement() {
    assert_replacement_waits_for_publication("termination");
}

#[test]
fn deadline_publication_is_atomic_with_child_replacement() {
    assert_replacement_waits_for_publication("deadline");
}

#[test]
fn ready_publication_is_atomic_with_child_replacement() {
    assert_replacement_waits_for_publication("ready");
}

#[test]
fn simultaneous_spawn_registration_disposes_only_the_rejected_child() {
    use std::{
        sync::{Arc, Barrier, Mutex},
        thread,
    };
    let slot = Arc::new(Mutex::new(SidecarSlot::default()));
    let spawned = Arc::new(Barrier::new(2));
    let disposed = Arc::new(Mutex::new(Vec::new()));
    let published = Arc::new(Mutex::new(Vec::new()));
    let handles: Vec<_> = [100, 200]
        .into_iter()
        .map(|pid| {
            let slot = slot.clone();
            let spawned = spawned.clone();
            let disposed = disposed.clone();
            let published = published.clone();
            thread::spawn(move || {
                // Both synthetic child handles already exist before registration.
                spawned.wait();
                register_sidecar(
                    &slot,
                    pid,
                    || published.lock().unwrap().push(pid),
                    |child| {
                        assert!(
                            slot.try_lock().is_ok(),
                            "cleanup must run outside the state lock"
                        );
                        disposed.lock().unwrap().push(child);
                    },
                )
                .is_ok()
            })
        })
        .collect();
    let accepted = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .filter(|ok| *ok)
        .count();
    let owned = retire_sidecar(&slot, true).unwrap();
    disposed.lock().unwrap().push(owned);
    let mut cleanup = disposed.lock().unwrap().clone();
    cleanup.sort();
    assert_eq!(accepted, 1);
    assert_eq!(*published.lock().unwrap(), vec![owned]);
    assert_eq!(cleanup, vec![100, 200]);
}

#[test]
fn close_during_spawn_rejects_and_disposes_the_late_child() {
    use std::sync::Mutex;
    let slot = Mutex::new(SidecarSlot::<u32>::default());
    assert_eq!(retire_sidecar(&slot, true), None);
    let mut disposed = Vec::new();
    let mut published = false;
    let result = register_sidecar(
        &slot,
        100,
        || published = true,
        |child| {
            assert!(slot.try_lock().is_ok());
            disposed.push(child);
        },
    );
    assert!(result.is_err());
    assert!(!published);
    assert_eq!(disposed, vec![100]);
    assert!(slot.lock().unwrap().owned.is_none());
    assert!(slot.lock().unwrap().closing);
    // Ordinary retry cleanup never reopens a final shutdown.
    assert_eq!(retire_sidecar(&slot, false), None);
    assert!(slot.lock().unwrap().closing);
}

#[test]
fn restart_operation_holds_one_gate_across_stop_spawn_and_registration() {
    use std::{
        sync::{mpsc, Arc, Mutex},
        time::Duration,
    };
    let gate = Arc::new(tauri::async_runtime::Mutex::new(()));
    let stages = Arc::new(Mutex::new(Vec::new()));
    let (started_tx, started_rx) = mpsc::channel();
    let (continue_tx, continue_rx) = mpsc::channel();
    let first_gate = gate.clone();
    let first_stages = stages.clone();
    let first = tauri::async_runtime::spawn(async move {
        with_restart_operation(&first_gate, move || {
            first_stages.lock().unwrap().push("first_stop");
            started_tx.send(()).unwrap();
            continue_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            first_stages
                .lock()
                .unwrap()
                .extend(["first_spawn", "first_register"]);
        })
        .await;
    });
    started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let held_through_stop = gate.try_lock().is_err();
    let next_gate = gate.clone();
    let next_stages = stages.clone();
    let second = tauri::async_runtime::spawn(async move {
        with_restart_operation(&next_gate, move || {
            next_stages
                .lock()
                .unwrap()
                .extend(["second_stop", "second_spawn", "second_register"]);
        })
        .await;
    });
    continue_tx.send(()).unwrap();
    tauri::async_runtime::block_on(async {
        first.await.unwrap();
        second.await.unwrap();
    });
    assert!(held_through_stop);
    assert_eq!(
        *stages.lock().unwrap(),
        vec![
            "first_stop",
            "first_spawn",
            "first_register",
            "second_stop",
            "second_spawn",
            "second_register"
        ]
    );
}

#[test]
fn failure_navigation_uses_the_platform_asset_origin_and_carries_the_reason() {
    let reason = "서비스 종료: 한글 & <상태> ? #";
    let url = host_failure_url(reason);
    if cfg!(any(target_os = "windows", target_os = "android")) {
        assert_eq!(url.scheme(), "http");
        assert_eq!(url.host_str(), Some("tauri.localhost"));
    } else {
        assert_eq!(url.scheme(), "tauri");
        assert_eq!(url.host_str(), Some("localhost"));
    }
    assert_eq!(url.path(), "/index.html");
    assert_eq!(
        url.query_pairs()
            .find(|(key, _)| key == "failure")
            .unwrap()
            .1,
        reason
    );
    assert!(is_allowed_navigation(url.as_str()));
}

#[test]
fn sidecar_command_is_structured_and_carries_only_runtime_arguments() {
    let spec = SidecarCommandSpec::new(PathBuf::from(r"D:\XRMLData"), 30);

    assert_eq!(spec.program, "xrml-sidecar");
    assert_eq!(
        spec.args,
        vec![
            "--data-dir".to_string(),
            r"D:\XRMLData".to_string(),
            "--startup-timeout-seconds".to_string(),
            "30".to_string(),
        ]
    );
    assert!(!spec.args.join(" ").contains("cmd.exe"));
    assert!(!spec.args.join(" ").contains("powershell"));
}

#[test]
fn ready_event_accepts_only_loopback_ephemeral_origin() {
    let event = parse_ready_event(
        br#"{"event":"ready","host":"127.0.0.1","port":49152,"app_path":"/app","pid":77}"#,
    )
    .expect("valid ready event");

    assert_eq!(event.port, 49152);
    assert_eq!(event.navigation_url(), "http://127.0.0.1:49152/app");
    assert!(parse_ready_event(
        br#"{"event":"ready","host":"0.0.0.0","port":49152,"app_path":"/app","pid":77}"#,
    )
    .is_err());
    assert!(parse_ready_event(
        br#"{"event":"ready","host":"127.0.0.1","port":0,"app_path":"/app","pid":77}"#,
    )
    .is_err());
    assert!(parse_ready_event(
        br#"{"event":"ready","host":"127.0.0.1","port":49152,"app_path":"/admin","pid":77}"#,
    )
    .is_err());
    assert!(parse_ready_event(
        br#"{"event":"ready","host":"127.0.0.1","port":49152,"app_path":"/app","pid":0}"#,
    )
    .is_err());
}

#[test]
fn normal_startup_diagnostics_do_not_show_a_failure() {
    // Actual successful frozen-sidecar startup emits both messages before ready.
    for line in [
        r#"{"level":"INFO","logger":"app.run_executor","message":"run_executor_disabled"}"#,
        r#"{"level":"INFO","event":"http_request","path":"/health/ready","status_code":200}"#,
        "2026-09-26 INFO [app.run_executor] run_executor_disabled",
        "Optional diagnostic warning without a lifecycle failure",
    ] {
        assert!(!is_startup_error(&CommandEvent::Stderr(
            line.as_bytes().to_vec()
        )));
    }
}

#[test]
fn fatal_lifecycle_and_transport_errors_remain_failures() {
    assert!(is_startup_error(&CommandEvent::Stderr(
        br#"{"event":"fatal","code":"app_startup_failure","detail":"[redacted]","pid":77}"#
            .to_vec()
    )));
    assert!(is_startup_error(&CommandEvent::Error(
        "read failed".to_string()
    )));
    assert!(!is_startup_error(&CommandEvent::Stdout(
        br#"{"event":"ready","host":"127.0.0.1","port":49152,"app_path":"/app","pid":77}"#.to_vec()
    )));
}

#[test]
fn navigation_is_limited_to_loading_surface_and_ready_sidecar() {
    assert!(is_allowed_navigation("tauri://localhost/index.html"));
    assert!(is_allowed_navigation("http://tauri.localhost/index.html"));
    assert!(is_allowed_navigation("http://127.0.0.1:49152/app"));
    assert!(is_allowed_navigation(
        "http://127.0.0.1:49152/app/topics/topic-1/dashboard"
    ));
    assert!(!is_allowed_navigation("https://example.com/app"));
    assert!(!is_allowed_navigation("http://localhost:49152/app"));
    assert!(!is_allowed_navigation("http://127.0.0.1:49152/admin"));
}

#[test]
fn host_owns_sidecar_until_graceful_or_forced_termination() {
    assert!(HOST_SOURCE.contains("struct OwnedSidecar"));
    assert!(HOST_SOURCE.contains("pid: u32"));
    assert!(HOST_SOURCE.contains("child: CommandChild"));
    assert!(HOST_SOURCE.contains("owned.child.write(b\"shutdown\\n\")"));
    assert!(HOST_SOURCE.contains("Duration::from_millis(1500)"));
    assert!(HOST_SOURCE.contains("owned.child.kill()"));
    assert!(HOST_SOURCE.contains("WindowEvent::Destroyed"));
    assert!(HOST_SOURCE.contains("RunEvent::ExitRequested"));
    assert!(HOST_SOURCE.contains("stop_sidecar(&state, true)"));
}

#[test]
fn host_has_bounded_readiness_and_truthful_crash_surfaces() {
    assert!(HOST_SOURCE.contains("const READY_TIMEOUT_SECONDS: u64 = 35"));
    assert!(HOST_SOURCE.contains("take_waiting_sidecar(&timeout_state, pid,"));
    assert!(HOST_SOURCE.contains("로컬 연구 서비스 준비 시간이 초과되었습니다."));
    assert!(HOST_SOURCE.contains("로컬 연구 서비스가 준비되기 전에 종료되었습니다."));
    assert!(HOST_SOURCE.contains("로컬 연구 서비스 연결이 종료되었습니다."));
    assert!(HOST_SOURCE.contains("CommandEvent::Terminated"));
}

#[test]
fn single_instance_focus_and_window_state_restoration_are_registered() {
    let single_instance = HOST_SOURCE
        .find("tauri_plugin_single_instance::init")
        .expect("single-instance plugin");
    let shell = HOST_SOURCE
        .find("tauri_plugin_shell::init")
        .expect("shell plugin");

    assert!(
        single_instance < shell,
        "single-instance plugin must register first"
    );
    assert!(HOST_SOURCE.contains("window.unminimize()"));
    assert!(HOST_SOURCE.contains("window.show()"));
    assert!(HOST_SOURCE.contains("window.set_focus()"));
    assert!(HOST_SOURCE.contains("tauri_plugin_window_state::Builder::default().build()"));
    assert!(HOST_SOURCE.contains("XRML_DESKTOP_STATE_FILE"));
    assert!(HOST_SOURCE.contains("save_custom_window_state"));
}

#[test]
fn validation_window_state_override_is_absolute_and_round_trips() {
    assert_eq!(
        custom_window_state_path(Some(OsString::from(r"D:\XRMLValidation\window-state.json"))),
        Some(PathBuf::from(r"D:\XRMLValidation\window-state.json"))
    );
    assert_eq!(
        custom_window_state_path(Some(OsString::from("relative-state.json"))),
        None
    );
    assert_eq!(custom_window_state_path(Some(OsString::new())), None);

    let state = DesktopWindowState {
        x: 240,
        y: 160,
        width: 1480,
        height: 920,
        maximized: false,
    };
    let encoded = serde_json::to_vec(&state).expect("serialize window state");
    let decoded: DesktopWindowState =
        serde_json::from_slice(&encoded).expect("deserialize window state");
    assert_eq!(decoded, state);
}

#[test]
fn validation_webview_data_override_prevents_default_c_drive_creation() {
    assert_eq!(
        custom_webview_data_path(Some(OsString::from(r"D:\XRMLValidation\webview2"))),
        Some(PathBuf::from(r"D:\XRMLValidation\webview2"))
    );
    assert_eq!(
        custom_webview_data_path(Some(OsString::from("relative-webview2"))),
        None
    );
    assert_eq!(custom_webview_data_path(Some(OsString::new())), None);
    assert!(HOST_SOURCE.contains(".data_directory(webview_data_dir)"));
}

#[test]
fn native_window_starts_hidden_and_exposes_only_the_packaged_sidecar() {
    assert!(HOST_SOURCE.contains(".visible(false)"));
    assert!(HOST_SOURCE.contains(".min_inner_size(1100.0, 700.0)"));
    assert!(HOST_SOURCE.contains(".on_navigation(|url| is_allowed_navigation(url.as_str()))"));
    assert!(TAURI_CONFIG.contains(r#""externalBin": ["binaries/xrml-sidecar"]"#));
    assert!(TAURI_CONFIG.contains(r#""useLocalToolsDir": true"#));
    assert!(TAURI_CONFIG.contains("connect-src http://127.0.0.1:*"));
    assert!(!TAURI_CONFIG.contains("https://*"));
}
