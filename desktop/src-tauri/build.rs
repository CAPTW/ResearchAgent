fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["restart_sidecar", "save_text_export"]),
    ))
    .expect("failed to build XRML desktop permissions");
}
