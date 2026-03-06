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
                            let html = format!(
                                r#"<html><body style="background:#08090E;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
                                <div style="text-align:center">
                                    <h1 style="color:#F97316">Ashlr AO</h1>
                                    <p>Failed to start the Python server.</p>
                                    <p style="color:#888;font-size:14px">{}</p>
                                    <p style="margin-top:20px;font-size:13px;color:#666">
                                        Make sure <code>ashlr-ao</code> is installed:<br>
                                        <code style="color:#706CF0">pip install ashlr-ao</code>
                                    </p>
                                </div></body></html>"#,
                                e
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
