use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent};

struct Backend(Child);

impl Drop for Backend {
    fn drop(&mut self) {
        // Closing stdin asks Python to shut down Uvicorn and its scheduler gracefully.
        drop(self.0.stdin.take());
        let deadline = Instant::now() + Duration::from_secs(7);
        while Instant::now() < deadline {
            if !matches!(self.0.try_wait(), Ok(None)) {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[derive(Default)]
struct BackendState(Mutex<Option<Backend>>);

fn healthy(address: SocketAddr) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(300)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    if stream
        .write_all(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok()
        && response.starts_with("HTTP/1.1 200")
        && response.contains("\"status\":\"ok\"")
}

fn start_backend(resource_dir: &Path, data_dir: &Path) -> Result<(Backend, String), String> {
    fs::create_dir_all(data_dir).map_err(|e| e.to_string())?;
    let log_path = data_dir.join("backend.log");
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| e.to_string())?;
    let mut stdout_log = log.try_clone().map_err(|e| e.to_string())?;
    let binary_name = if cfg!(windows) {
        "youdekan-backend.exe"
    } else {
        "youdekan-backend"
    };
    let binary = resource_dir.join("backend").join(binary_name);
    if !binary.is_file() {
        return Err(format!("安装包缺少内置服务：{}", binary.display()));
    }
    let mut command = Command::new(binary);
    command
        .arg("--data-dir")
        .arg(data_dir)
        .arg("--watch-parent")
        .current_dir(data_dir)
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut backend = Backend(
        command
            .spawn()
            .map_err(|e| format!("无法启动内置服务：{e}"))?,
    );
    let stdout = backend.0.stdout.take().ok_or("无法读取服务启动状态")?;
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let _ = writeln!(stdout_log, "{line}");
            if let Some(url) = line.strip_prefix("YOUDEKAN_READY=") {
                let _ = sender.send(url.to_string());
            }
        }
    });
    let url = receiver
        .recv_timeout(Duration::from_secs(60))
        .map_err(|_| format!("内置服务未能完成启动。日志：{}", log_path.display()))?;
    let address: SocketAddr = url
        .strip_prefix("http://")
        .ok_or("无效的服务地址")?
        .parse()
        .map_err(|_| "无效的服务端口")?;
    if !address.ip().is_loopback() {
        return Err("内置服务必须监听本机地址".into());
    }
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if backend.0.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err(format!("内置服务意外退出。日志：{}", log_path.display()));
        }
        if healthy(address) {
            return Ok((backend, url));
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err(format!("内置服务连接超时。日志：{}", log_path.display()))
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(BackendState::default())
        .setup(|app| {
            let resources = app.path().resource_dir()?;
            let data_dir = app.path().app_data_dir()?;
            let window = app.get_webview_window("main").ok_or("缺少主窗口")?;
            let handle = app.handle().clone();
            thread::spawn(move || {
                let result = start_backend(&resources, &data_dir).and_then(|(backend, url)| {
                    *handle.state::<BackendState>().0.lock().unwrap() = Some(backend);
                    window
                        .navigate(url.parse().map_err(|e| format!("服务地址错误：{e}"))?)
                        .map_err(|e| e.to_string())
                });
                if let Err(error) = result {
                    let message =
                        serde_json::to_string(&format!("启动失败：{error}\n请退出后重试。"))
                            .unwrap();
                    let _ = window.eval(&format!(
                        "document.getElementById('status').textContent = {message};"
                    ));
                    eprintln!("{error}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("无法创建有得看窗口")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                drop(app.state::<BackendState>().0.lock().unwrap().take());
            }
        });
}
