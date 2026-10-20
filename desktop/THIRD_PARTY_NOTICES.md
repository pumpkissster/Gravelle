# Gravelle Windows runtime

This distribution includes Python, Tcl/Tk and Pillow so users do not need to install them separately. Their license notices are included in the `licenses` directory and bundled runtime directories. PyInstaller is used to package this application; its bootloader distribution exception is included with its license notice.

Local screenshot and crop matching uses NumPy (BSD-3-Clause), OpenCV (Apache-2.0) and the opencv-python-headless packaging scripts (MIT). Package license files, including notices for components shipped by these wheels, are included in `licenses` and the bundled runtime directories. Matching runs locally on the CPU; no model download or GPU runtime is included.

The Libre Caslon Text font includes its SIL Open Font License at `_internal/static/fonts/OFL.txt`. Gravelle's own license is in `LICENSE`.

PDF reading uses pypdfium2 5.14.0 (Apache-2.0 or BSD-3-Clause) and its bundled PDFium renderer. The complete PDFium license and the notices of all components shipped with that wheel, including the libjpeg-turbo IJG license, are preserved under `licenses/pypdfium2/`. Rendering runs locally without downloading a model or executing PDF JavaScript.

Shared photo pages use cryptography (Apache-2.0 or BSD-3-Clause), CFFI/pycparser (MIT), and the cryptography wheel's statically linked OpenSSL. Complete notices, including both cryptography license alternatives, are kept in `licenses/cryptography/`, `licenses/cffi/`, and `licenses/pycparser/`. Windows, Linux, and Apple-chip macOS use cryptography 50.0.2 / OpenSSL 4.0.2; Intel macOS uses the final official universal2 wheel, cryptography 48.0.1 / OpenSSL 4.0.1. Gravelle uses only AES-GCM from this package, not certificate or PKCS#7 parsing.

The browser decryptor's local AES-GCM fallback is built from unmodified `@noble/ciphers` 2.4.0 modules (MIT, Paul Miller and Thomas Pornin). The complete notice is in `static/vendor/noble-ciphers-LICENSE.txt`; pinned source files and reproducible build details are included in the source package. The project has a public audit history; that does not imply every later change or Gravelle integration has been independently audited.

The installer and uninstaller use Inno Setup. Its license is included at `licenses/Inno-Setup-LICENSE.txt`. The Simplified Chinese translation is maintained by Zhenghan Yang (Kira) and comes from the Inno Setup 7.1.0 source release; attribution and provenance are preserved in `desktop/languages` in the source distribution.

Build tooling uses [resvg-py 0.5.0](https://github.com/baseplate-admin/resvg-py) (MIT, Copyright (C) 2024 baseplate-admin), based on [resvg](https://github.com/linebender/resvg) (MIT or Apache-2.0). It renders Gravelle's supplied SVG wordmark to a transparent PNG during packaging. These renderer libraries are not included in the Windows runtime.

Source and releases: https://github.com/pumpkissster/Gravelle
