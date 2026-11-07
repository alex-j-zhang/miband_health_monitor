# Vendored third-party code

This directory contains small, permissively licensed third-party components that
are copied into the repository instead of being installed from PyPI, so that the
project keeps working offline and on platforms where a dependency release is
not available for the current Python version.

## `tiny_ecdh/` (version 0.1.0)

- Upstream: <https://github.com/danielsousaoliveira/tiny-ECDH-python>
- PyPI: `tiny-ecdh-python` (wheel `tiny_ecdh_python-0.1.0-py3-none-any.whl`)
- License: MIT (see [`tiny_ecdh/LICENSE`](tiny_ecdh/LICENSE))
- Purpose: pure-Python implementation of the NIST B-163 (sect163r2) curve,
  used by `ecdh_wrapper.py` as a **fallback** when the native `libecdh`
  (`ecdh.c`, tiny-ECDH-c) cannot be loaded on the current platform. The Mi Band
  6 authentication protocol uses this exact curve; the native library is still
  used preferentially on Linux for speed.
- Local modifications: none to the module sources. Kept byte-for-byte from the
  0.1.0 wheel; only `LICENSE` was added next to the package.
- Cross-implementation check: `tests/test_ecdh_backends.py` compares the pure
  Python backend against the native C library byte-for-byte (shared secret and
  public key layout) whenever the native library is available.

> Note: the upstream package describes itself as "educational" because the
> B-163 curve is an 80-bit curve. This project only uses it to reproduce the
> Mi Band 6 authentication protocol, which itself mandates B-163; it is not used
> for any other secret material and is not a security downgrade relative to the
> C implementation it replaces.
