# uv binary in the Windows x64 bundle

The release build includes `uv.exe` from Astral's uv 0.13.5 Windows x64 archive:
https://github.com/astral-sh/uv/releases/download/0.13.5/uv-x86_64-pc-windows-msvc.zip

Archive SHA-256: `a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a`.
`tools/build_bundle.py` verifies this digest before packaging the binary.

uv is dual-licensed under Apache-2.0 or MIT. The corresponding license texts are in this directory.
