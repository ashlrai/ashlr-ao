use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};

/// Manages the Python server sidecar process.
pub struct ServerManager {
    child: Arc<Mutex<Option<Child>>>,
}

impl ServerManager {
    pub fn new() -> Self {
        Self {
            child: Arc::new(Mutex::new(None)),
        }
    }

    /// Start the Python server, returning the port it's listening on.
    pub async fn start(&self, _handle: &tauri::AppHandle) -> Result<u16, String> {
        let port = pick_port();
        log::info!("Starting ashlr server on port {}", port);

        let (program, args) = find_server_command(port)?;
        log::info!("Using command: {} {:?}", program, args);

        // Augment PATH so the child process can find pip-installed tools, tmux, etc.
        let augmented_path = augmented_path();

        let mut child = Command::new(&program)
            .args(&args)
            .env("PATH", &augmented_path)
            .env("ASHLR_PORT", port.to_string())
            .env("ASHLR_HOST", "127.0.0.1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| format!("Failed to spawn server ({}): {}", program, e))?;

        // Read stderr and log it
        if let Some(stderr) = child.stderr.take() {
            tokio::spawn(async move {
                let mut reader = BufReader::new(stderr).lines();
                while let Ok(Some(line)) = reader.next_line().await {
                    log::info!("[server] {}", line);
                }
            });
        }

        // Store the child process for cleanup
        *self.child.lock().unwrap() = Some(child);

        // Wait for the server to become responsive
        let ready = wait_for_server(port).await;
        if ready {
            log::info!("Server is ready on port {}", port);
            Ok(port)
        } else {
            Err("Server did not start within 15 seconds. Is ashlr-ao installed? (pip install ashlr-ao)".into())
        }
    }
}

impl Drop for ServerManager {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(ref mut child) = *guard {
                log::info!("Shutting down Python server");
                let _ = child.start_kill();
            }
        }
    }
}

/// Build an augmented PATH that includes common locations for Python, Homebrew, etc.
fn augmented_path() -> String {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/default".into());
    let mut paths: Vec<String> = vec![
        // User-local pip installs (where `ashlr` lives after `pip install ashlr-ao`)
        format!("{}/.local/bin", home),
        // Homebrew (Apple Silicon)
        "/opt/homebrew/bin".into(),
        "/opt/homebrew/sbin".into(),
        // Homebrew (Intel)
        "/usr/local/bin".into(),
        "/usr/local/sbin".into(),
        // Python framework (macOS installer)
        "/Library/Frameworks/Python.framework/Versions/3.13/bin".into(),
        "/Library/Frameworks/Python.framework/Versions/3.12/bin".into(),
        "/Library/Frameworks/Python.framework/Versions/3.11/bin".into(),
        // pyenv
        format!("{}/.pyenv/shims", home),
        // Cargo/Rust (for any Rust tools)
        format!("{}/.cargo/bin", home),
        // System paths
        "/usr/bin".into(),
        "/bin".into(),
        "/usr/sbin".into(),
        "/sbin".into(),
    ];

    // Also include the current PATH
    if let Ok(current) = std::env::var("PATH") {
        for p in current.split(':') {
            if !paths.contains(&p.to_string()) {
                paths.push(p.to_string());
            }
        }
    }

    paths.join(":")
}

/// Pick an available port, preferring 5111.
fn pick_port() -> u16 {
    if portpicker::is_free(5111) {
        return 5111;
    }
    portpicker::pick_unused_port().unwrap_or(5112)
}

/// Find the best way to launch the server.
fn find_server_command(port: u16) -> Result<(String, Vec<String>), String> {
    let port_str = port.to_string();
    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/default".into());

    // 1. Check for `ashlr` CLI in common locations
    let mut ashlr_candidates: Vec<String> = vec![
        format!("{}/.local/bin/ashlr", home),
        "/opt/homebrew/bin/ashlr".into(),
        "/usr/local/bin/ashlr".into(),
    ];

    // Also check for a .venv in the project directory (dev mode)
    // The app binary lives in src-tauri/target/... — walk up to find the project root
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(PathBuf::from);
        for _ in 0..8 {
            if let Some(ref d) = dir {
                let venv_ashlr = d.join(".venv/bin/ashlr");
                if venv_ashlr.exists() {
                    ashlr_candidates.insert(0, venv_ashlr.to_string_lossy().into());
                    break;
                }
                dir = d.parent().map(PathBuf::from);
            }
        }
    }

    for candidate in &ashlr_candidates {
        if PathBuf::from(candidate).exists() {
            return Ok((
                candidate.clone(),
                vec!["--port".into(), port_str, "--host".into(), "127.0.0.1".into()],
            ));
        }
    }

    // Also try which
    if which::which("ashlr").is_ok() {
        return Ok((
            "ashlr".into(),
            vec!["--port".into(), port_str, "--host".into(), "127.0.0.1".into()],
        ));
    }

    // 2. Fall back to python -m ashlr_ao
    let python = find_python()?;
    Ok((
        python,
        vec![
            "-m".into(),
            "ashlr_ao".into(),
            "--port".into(),
            port_str,
            "--host".into(),
            "127.0.0.1".into(),
        ],
    ))
}

/// Find the best Python interpreter.
fn find_python() -> Result<String, String> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/default".into());

    let candidates = [
        format!("{}/.pyenv/shims/python3", home),
        "/opt/homebrew/bin/python3".into(),
        "/usr/local/bin/python3".into(),
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3".into(),
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3".into(),
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3".into(),
        "/usr/bin/python3".into(),
    ];

    for candidate in &candidates {
        if PathBuf::from(candidate).exists() {
            return Ok(candidate.clone());
        }
    }

    if which::which("python3").is_ok() {
        return Ok("python3".into());
    }

    Err("Could not find Python 3. Install it via: brew install python3".into())
}

/// Wait for the server to become responsive by polling the health endpoint.
async fn wait_for_server(port: u16) -> bool {
    let url = format!("http://127.0.0.1:{}/api/health", port);

    for i in 0..30 {
        tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
        match reqwest::get(&url).await {
            Ok(resp) if resp.status().is_success() => {
                log::info!("Server responded on attempt {}", i + 1);
                return true;
            }
            Ok(resp) => {
                log::debug!("Server returned status {} on attempt {}", resp.status(), i + 1);
            }
            Err(_) => {
                // Server not ready yet, keep polling
            }
        }
    }

    false
}
