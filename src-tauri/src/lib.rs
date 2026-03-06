mod sidecar;

use sidecar::ServerManager;
use std::sync::Mutex;
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, RunEvent, WindowEvent,
};

/// Holds the server manager so it lives for the app's entire lifetime.
struct ServerState(Mutex<Option<ServerManager>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .manage(ServerState(Mutex::new(None)))
        .setup(|app| {
            // ── System tray ──
            let quit = MenuItem::with_id(app, "quit", "Quit Ashlr AO", true, None::<&str>)?;
            let show = MenuItem::with_id(app, "show", "Show Window", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;

            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .tooltip("Ashlr AO")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "quit" => {
                        app.exit(0);
                    }
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    _ => {}
                })
                .build(app)?;

            // ── Launch Python server as sidecar ──
            let handle = app.handle().clone();

            tauri::async_runtime::spawn(async move {
                let server = ServerManager::new();
                match server.start(&handle).await {
                    Ok(port) => {
                        log::info!("Python server started on port {}", port);

                        // Store the server manager in app state to keep it alive
                        let state = handle.state::<ServerState>();
                        *state.0.lock().unwrap() = Some(server);

                        // Navigate WebView to the running server
                        if let Some(window) = handle.get_webview_window("main") {
                            let url = format!("http://127.0.0.1:{}", port);
                            let _ = window.navigate(url.parse().unwrap());
                        }
                    }
                    Err(e) => {
                        log::error!("Failed to start Python server: {}", e);
                        if let Some(window) = handle.get_webview_window("main") {
                            let accent = "#706CF0";
                            let html = format!(
                                r##"<html><body style="background:#08090E;color:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
                                <div style="text-align:center;max-width:400px">
                                    <svg width="48" height="48" viewBox="0 0 32 32" style="margin-bottom:20px"><rect width="32" height="32" rx="5" fill="#0D1117"/><path d="M16 5L8 26h3.2l1.8-4.2h6l1.8 4.2H24L16 5zm0 6l2.4 5.6h-4.8L16 11z" fill="#DCE1EB"/><path d="M13.2 17l5.6 0 1.2 3.8H12z" fill="#4285F4"/></svg>
                                    <h1 style="color:#F0F0F2;font-size:18px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:12px">Ashlr AO</h1>
                                    <p style="color:#8E929E;font-size:14px;margin-bottom:8px">Failed to start the server</p>
                                    <p style="color:#555968;font-size:12px;margin-bottom:20px;overflow-wrap:break-word">{err}</p>
                                    <p style="font-size:12px;color:#555968">
                                        Make sure <code style="color:{accent}">ashlr-ao</code> is installed:<br>
                                        <code style="color:{accent};font-size:13px">pip install ashlr-ao</code>
                                    </p>
                                </div></body></html>"##,
                                err = e,
                                accent = accent,
                            );
                            let _ = window.navigate(
                                format!("data:text/html,{}", urlencoding::encode(&html))
                                    .parse()
                                    .unwrap(),
                            );
                        }
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building Ashlr AO");

    app.run(|app_handle, event| match event {
        RunEvent::ExitRequested { .. } => {
            log::info!("Ashlr AO shutting down");
            // Drop the server manager to kill the sidecar
            let state = app_handle.state::<ServerState>();
            *state.0.lock().unwrap() = None;
        }
        RunEvent::WindowEvent {
            event: WindowEvent::CloseRequested { api, .. },
            label,
            ..
        } => {
            // Hide to tray instead of quitting
            if label == "main" {
                if let Some(window) = app_handle.get_webview_window("main") {
                    let _ = window.hide();
                }
                api.prevent_close();
            }
        }
        _ => {}
    });
}
