# Gravelle Windows runtime

This distribution includes Python, Tcl/Tk and Pillow so users do not need to install them separately. Their license notices are included in the `licenses` directory and bundled runtime directories. PyInstaller is used to package this application; its bootloader distribution exception is included with its license notice.

Local screenshot and crop matching uses NumPy (BSD-3-Clause), OpenCV (Apache-2.0) and the opencv-python-headless packaging scripts (MIT). Package license files, including notices for components shipped by these wheels, are included in `licenses` and the bundled runtime directories. Matching runs locally on the CPU; no model download or GPU runtime is included.

The Libre Caslon Text font includes its SIL Open Font License at `_internal/static/fonts/OFL.txt`. Gravelle's own license is in `LICENSE`.

The installer and uninstaller use Inno Setup. Its license is included at `licenses/Inno-Setup-LICENSE.txt`. The Simplified Chinese translation is maintained by Zhenghan Yang (Kira) and comes from the Inno Setup 7.1.0 source release; attribution and provenance are preserved in `desktop/languages` in the source distribution.

Build tooling uses [resvg-py 0.5.0](https://github.com/baseplate-admin/resvg-py) (MIT, Copyright (C) 2024 baseplate-admin), based on [resvg](https://github.com/linebender/resvg) (MIT or Apache-2.0). It renders Gravelle's supplied SVG wordmark to a transparent PNG during packaging. These renderer libraries are not included in the Windows runtime.

Source and releases: https://github.com/pumpkissster/Gravelle
