"""Cross-implementation tests for the native and pure-Python ECDH backends.

The Mi Band 6 authentication relies on the exact byte layout produced by the
native tiny-ECDH C library.  These tests make sure the pure-Python fallback
(used on Windows/macOS/ARM where no prebuilt ``libecdh`` exists) is byte-for-byte
compatible, and that backend selection/compilation helpers behave per platform.
"""

import pytest

import ecdh_wrapper
from ecdh_wrapper import (
    PRIVATE_KEY_SIZE,
    PUBLIC_KEY_SIZE,
    SHARED_SECRET_SIZE,
    NativeEcdhBackend,
    PurePythonEcdhBackend,
    backend_name,
    compute_shared_secret,
    generate_ecdh_keys,
    native_library_candidates,
    select_backend,
)


def _native_or_skip() -> NativeEcdhBackend:
    try:
        return NativeEcdhBackend()
    except RuntimeError:
        pytest.skip("native libecdh is not available on this platform")


def test_public_api_key_agreement():
    prv_a, pub_a = generate_ecdh_keys()
    prv_b, pub_b = generate_ecdh_keys()

    assert len(prv_a) == PRIVATE_KEY_SIZE
    assert len(pub_a) == PUBLIC_KEY_SIZE
    assert len(prv_b) == PRIVATE_KEY_SIZE
    assert len(pub_b) == PUBLIC_KEY_SIZE

    secret_a = compute_shared_secret(prv_a, pub_b)
    secret_b = compute_shared_secret(prv_b, pub_a)

    assert len(secret_a) == SHARED_SECRET_SIZE
    assert secret_a == secret_b
    assert backend_name() in ("native", "python")


def test_pure_python_backend_key_agreement():
    backend = PurePythonEcdhBackend()
    prv_a, pub_a = backend.generate_keys()
    prv_b, pub_b = backend.generate_keys()

    assert backend.compute_shared_secret(prv_a, pub_b) == backend.compute_shared_secret(
        prv_b, pub_a
    )


def test_pure_python_backend_rejects_invalid_public_point():
    backend = PurePythonEcdhBackend()
    private_key, public_key = backend.generate_keys()
    corrupted = bytearray(public_key)
    corrupted[0] ^= 0xFF

    with pytest.raises(ValueError, match="valid point"):
        backend.compute_shared_secret(private_key, bytes(corrupted))


def test_pure_python_backend_validates_lengths():
    backend = PurePythonEcdhBackend()
    private_key, public_key = backend.generate_keys()

    with pytest.raises(ValueError, match="Private key"):
        backend.compute_shared_secret(b"short", public_key)
    with pytest.raises(ValueError, match="Peer public key"):
        backend.compute_shared_secret(private_key, b"short")


def test_native_and_pure_backends_are_byte_compatible():
    """The fallback must produce the same wire bytes as the C library."""
    native = _native_or_skip()
    pure = PurePythonEcdhBackend()

    for _ in range(3):
        pure_prv, pure_pub = pure.generate_keys()
        native_prv, native_pub = native.generate_keys()

        # Both directions of a mixed handshake must agree exactly, including
        # the y coordinate of the returned shared point.
        assert native.compute_shared_secret(
            pure_prv, native_pub
        ) == pure.compute_shared_secret(native_prv, pure_pub)
        assert native.compute_shared_secret(
            native_prv, pure_pub
        ) == pure.compute_shared_secret(pure_prv, native_pub)


def test_native_and_pure_backends_reject_corrupted_points():
    native = _native_or_skip()
    pure = PurePythonEcdhBackend()
    native_prv, native_pub = native.generate_keys()
    pure_prv, _pure_pub = pure.generate_keys()
    corrupted = bytearray(native_pub)
    corrupted[0] ^= 0xFF

    for backend, private_key in (
        (native, pure_prv),
        (pure, pure_prv),
    ):
        with pytest.raises(ValueError):
            backend.compute_shared_secret(private_key, bytes(corrupted))
    assert native_prv  # keep the generated value referenced


def test_native_library_candidates_follow_platform(monkeypatch):
    expected = {
        "win32": ["libecdh.dll", "ecdh.dll"],
        "darwin": ["libecdh.dylib", "libecdh.so"],
        "linux": ["libecdh.so"],
    }
    for platform, names in expected.items():
        monkeypatch.setattr(ecdh_wrapper.sys, "platform", platform)
        candidates = ecdh_wrapper.native_library_candidates()
        root_names = [
            path.name for path in candidates if path.parent == ecdh_wrapper.PROJECT_ROOT
        ]
        build_names = [path.name for path in candidates if path.parent == ecdh_wrapper.BUILD_DIR]
        assert root_names == names
        assert build_names == names


def test_compiled_library_path_is_git_ignored_cache(monkeypatch):
    monkeypatch.setattr(ecdh_wrapper.sys, "platform", "linux")
    path = ecdh_wrapper.compiled_library_path()
    assert path.parent == ecdh_wrapper.BUILD_DIR
    assert path.name == "libecdh.so"
    assert ".cache" in path.parts


def test_explicit_library_path_is_preferred(monkeypatch):
    monkeypatch.setenv(ecdh_wrapper.ENV_LIBRARY, "/custom/path/libecdh_custom.so")
    candidates = native_library_candidates()
    assert candidates[0].name == "libecdh_custom.so"


def test_select_backend_python_is_always_available():
    assert select_backend("python").name == "python"
    assert select_backend("pure").name == "python"


def test_select_backend_auto_prefers_native_when_available():
    try:
        NativeEcdhBackend()
    except RuntimeError:
        pytest.skip("native libecdh is not available on this platform")
    assert select_backend("auto").name == "native"


def test_select_backend_native_requires_library():
    try:
        NativeEcdhBackend()
    except RuntimeError:
        with pytest.raises(RuntimeError):
            select_backend("native")
    else:
        assert select_backend("native").name == "native"


def test_auto_falls_back_when_native_library_and_compiler_are_missing(monkeypatch):
    """Windows-like environment: no DLL and no compiler -> pure Python."""

    def _raise_cdll(*_args, **_kwargs):
        raise OSError("not a valid Win32 application")

    def _raise_compiler(*_args, **_kwargs):
        raise FileNotFoundError("gcc/clang not installed")

    monkeypatch.setattr(ecdh_wrapper.ctypes, "CDLL", _raise_cdll)
    monkeypatch.setattr(ecdh_wrapper.subprocess, "run", _raise_compiler)

    assert ecdh_wrapper.load_native_library() is None
    assert ecdh_wrapper.select_backend("auto").name == "python"


def test_select_backend_survives_unknown_value(monkeypatch, caplog):
    monkeypatch.setattr(ecdh_wrapper.ctypes, "CDLL", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(
        ecdh_wrapper.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("x"))
    )
    with caplog.at_level("WARNING", logger="MiBandECDH"):
        backend = ecdh_wrapper.select_backend("bogus")
    assert backend.name == "python"
    assert any("Unknown MIBAND_ECDH_BACKEND" in record.message for record in caplog.records)
