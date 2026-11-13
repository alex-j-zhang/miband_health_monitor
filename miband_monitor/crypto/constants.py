"""Static values shared by both ECDH backends.

The Mi Band 6 authentication handshake is ECDH over the NIST B-163
(sect163r2) curve.  The wire format is fixed by the band firmware:

* a 24-byte little-endian private key,
* a 48-byte little-endian public key (x || y, 24 bytes each),
* a 48-byte little-endian shared point (x || y, 24 bytes each).

Paths are read from miband_monitor.config.paths and the environment variable
names from miband_monitor.config.defaults, so a relocated checkout, a
read-only install and the legacy .cache/ecdh build directory all keep working
exactly as before.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Tuple

from miband_monitor.config.defaults import ENV_ECDH_BACKEND, ENV_ECDH_LIBRARY
from miband_monitor.config.paths import PROJECT_ROOT, ecdh_build_dir, ecdh_c_source

logger = logging.getLogger("MiBandECDH")

#: Repository / checkout root; native libraries may be dropped here by users.
PROJECT_ROOT: Path = PROJECT_ROOT
#: Vendored C implementation of the tiny-ECDH curve arithmetic.
C_SOURCE_PATH: Path = ecdh_c_source()
#: Where a locally compiled native library is written (git-ignored, .cache/).
BUILD_DIR: Path = ecdh_build_dir()

PRIVATE_KEY_SIZE = 24
PUBLIC_KEY_SIZE = 48
SHARED_SECRET_SIZE = 48
KEY_GENERATION_ATTEMPTS = 16

#: Native library file names tried per platform, in order.  Anything not listed
#: (Linux, and anything unusual) falls back to the ELF-style libecdh.so.
NATIVE_LIBRARY_NAMES: Dict[str, Tuple[str, ...]] = {
    "win32": ("libecdh.dll", "ecdh.dll"),
    "darwin": ("libecdh.dylib", "libecdh.so"),
}

#: Private alias kept because the historical module exposed this name to tests.
_NATIVE_LIBRARY_NAMES = NATIVE_LIBRARY_NAMES

#: auto (default), native or python; see miband_monitor.crypto.facade.select_backend.
ENV_BACKEND = ENV_ECDH_BACKEND
#: Explicit native library path, handy on Windows where users build ecdh.dll.
ENV_LIBRARY = ENV_ECDH_LIBRARY
