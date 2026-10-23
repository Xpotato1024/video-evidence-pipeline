fn main() {
    println!("cargo:rerun-if-changed=gui.manifest");
    if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        println!("cargo:rustc-link-arg-bin=vep-setup-gui=/MANIFEST:EMBED");
        println!(
            "cargo:rustc-link-arg-bin=vep-setup-gui=/MANIFESTINPUT:{}/gui.manifest",
            std::env::var("CARGO_MANIFEST_DIR").unwrap()
        );
    }
}
