# Local browser AES-GCM fallback

`noble-gcm.js` is a 9.2 KiB standalone bundle containing only the required AES-GCM code from `@noble/ciphers` **2.4.0**. It is loaded from the Gravelle server, never a CDN. The complete upstream MIT license is in `noble-ciphers-LICENSE.txt`.

Official upstream: <https://github.com/paulmillr/noble-ciphers>.
Official package: <https://registry.npmjs.org/@noble%2fciphers/2.4.0>.

The NPM tarball's SHA-512 integrity was checked before copying the unmodified `aes.js`, `_polyval.js`, and `utils.js` modules to `packaging/vendor/noble-ciphers-2.4.0/`. Their hashes, tarball integrity, build flags, and bundle hash are recorded in `noble-gcm-provenance.json`.

Rebuild with Python and the official **esbuild 0.28.2** executable:

```text
python packaging/build_share_crypto.py --esbuild /path/to/esbuild
```

The build checks the input hashes and exact output hash. Source modules remain in the source export; Docker and desktop apps only need the committed bundle and its notice. The esbuild tool is build-only and is not redistributed with Gravelle.

`share-crypto.js` uses browser WebCrypto when available. On HTTP LAN addresses where WebCrypto is unavailable it uses this local library. That supports the same authenticated envelope; it does not make HTTP resistant to interception or modification. Noble has a public audit history, which does not mean the complete 2.4.0 release or this application integration was audited.
