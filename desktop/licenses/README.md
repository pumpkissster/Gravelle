# Pinned PyObjC core license notice

`pyobjc-core-12.2.2-LICENSE.txt` is the unmodified `pyobjc_core-12.2.2/License.txt` from the official PyPI source distribution. The CPython 3.12 universal2 wheel for this exact version omits that file; its metadata contains only the short `License: MIT` identifier. The macOS build copies the full notice only when the installed distribution has no complete license file, its version is exactly `12.2.2`, and the vendored bytes pass SHA-256 verification. Other missing license notices still stop the build.

Provenance:

- Official source file: https://github.com/ronaldoussoren/pyobjc/blob/v12.2.2/pyobjc-core/License.txt
- Official PyPI source distribution: https://files.pythonhosted.org/packages/a5/78/abc4ce5920305780aeb36b4067a86253378b36e29ba96673a3deb02eb03a/pyobjc_core-12.2.2.tar.gz
- Source archive SHA-256: `3906452339cd06a3bb07df103c2511d4cb0f7a22d8771c0b802eba15d9a642b6`
- License file SHA-256: `0ca04b07928d4872b9d9bb22187ca0426dd8bfab08f26eada0999a71dc81aaff`
- License file size: 1,249 bytes, UTF-8, original LF line endings

The macOS build collects Cocoa and Quartz notices from their installed wheels. This pinned fallback is specific to `pyobjc-core`; it is not a replacement license for unrelated packages or future releases.
