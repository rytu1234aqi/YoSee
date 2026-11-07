// Release builds use the Windows GUI subsystem so launching the app opens no console.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

fn main() {
    youdekan_lib::run();
}
