#!/usr/bin/env python3
"""Fork-local (openweb) build hook -- not an upstream file.

Pins the Windows/macOS client to the self-hosted RustDesk server and public key
before the Rust build, by rewriting the two constants that carry them.

Why this exists
---------------
Up to 1.3.7, upstream's .github/workflows/flutter-build.yml exported the repo
secrets RENDEZVOUS_SERVER / RS_PUB_KEY and libs/hbb_common/src/config.rs read
them at compile time:

    PROD_RENDEZVOUS_SERVER = RwLock::new(match option_env!("RENDEZVOUS_SERVER") { .. })
    RS_PUB_KEY             = match option_env!("RS_PUB_KEY") { .. => PUBLIC_RS_PUB_KEY }

1.4.9 deleted that hook; config.rs now hardcodes

    PROD_RENDEZVOUS_SERVER = RwLock::new("".to_owned())
    RS_PUB_KEY             = "<RustDesk's own public key>"

so a 1.4.9 build of this fork silently produces a *stock* client aimed at
rs-ny.rustdesk.com -- confirmed by string-probing the built librustdesk.dll
(0 hits for our hostname/key against 2/1 in the 1.3.7 build). The one custom
client mechanism 1.4.9 still honours natively, custom.txt, is signed with
RustDesk's private key, so a self-hosted client now needs a source patch.

What it does
------------
Rewrites exactly those two constants from the RENDEZVOUS_SERVER / RS_PUB_KEY
environment variables (fed by the workflow's `secrets:`), in the throwaway CI
checkout only. The libs/hbb_common submodule pointer stays byte-identical to
upstream, so `git submodule update` and future upstream merges cannot conflict
with this; same shape as the existing apply_flutter_3.44_source_patches.sh.
Two constants are enough: the API server is derived from the rendezvous server
by src/common.rs::get_api_server_ (1.4.9) and using_public_server() keys off the
same value.

Behaviour
---------
  either value empty -> warn and exit 0, leaving the checkout exactly as
                        upstream, so callers that do not hand the secrets to
                        flutter-build.yml (e.g. pull-request builds) still build
                        a stock client instead of failing
  both values set    -> rewrite, re-read the file, verify, and only then exit 0.
                        Anchor drift or an unusable value aborts the build, so a
                        pinned build can never half-apply or silently no-op.

Usage (cwd-independent; paths resolve from this file):
    RENDEZVOUS_SERVER=host RS_PUB_KEY=key \\
        python3 .github/patches/apply_selfhosted_pinning.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "libs" / "hbb_common" / "src" / "config.rs"

# Anchored on the 1.4.9 definitions. The value part is [^"\r\n]* so re-running
# over an already-pinned file (or a LF/CRLF checkout) behaves identically.
PROD_RE = re.compile(
    r'(?m)^(\s*pub static ref PROD_RENDEZVOUS_SERVER: RwLock<String> = RwLock::new\(")'
    r'[^"\r\n]*'
    r'("\.to_owned\(\)\);)'
)
KEY_RE = re.compile(
    r'(?m)^(pub const RS_PUB_KEY: &str = ")[^"\r\n]*(";)'
)


def fail(msg: str) -> None:
    print(f"::error::apply_selfhosted_pinning: {msg}", file=sys.stderr)
    raise SystemExit(1)


def warn(msg: str) -> None:
    print(f"::warning::apply_selfhosted_pinning: {msg}")


def clean(name: str, raw: str) -> str:
    """Trim a value from the environment and reject anything that could escape
    the Rust string literal it is about to be written into."""
    value = raw.strip()
    unsafe = sorted({c for c in value if c == '"' or c == "\\" or not ("\x20" <= c <= "\x7e")})
    if unsafe:
        fail(f"{name} contains characters that are unsafe in a Rust string literal: {unsafe!r}")
    return value


def mask(value: str) -> str:
    # Keep every log line pure ASCII: the Windows runner console mangles non-ASCII
    # (and an unencodable char must never be able to fail the build from a print).
    return value if len(value) <= 8 else value[:8] + "..."


def apply(text: str, rendezvous: str, key: str) -> str:
    """Return `text` with both constants rewritten, or fail loudly."""
    for label, regex, value in (
        ("PROD_RENDEZVOUS_SERVER", PROD_RE, rendezvous),
        ("RS_PUB_KEY", KEY_RE, key),
    ):
        text, n = regex.subn(lambda m, v=value: m.group(1) + v + m.group(2), text)
        if n != 1:
            fail(
                f"expected exactly 1 {label} definition in {CONFIG.relative_to(ROOT)}, "
                f"found {n} -- upstream changed the file, re-anchor this patch"
            )
    return text


def main() -> int:
    rendezvous = clean("RENDEZVOUS_SERVER", os.environ.get("RENDEZVOUS_SERVER", ""))
    key = clean("RS_PUB_KEY", os.environ.get("RS_PUB_KEY", ""))

    if not rendezvous or not key:
        missing = [
            name
            for name, value in (("RENDEZVOUS_SERVER", rendezvous), ("RS_PUB_KEY", key))
            if not value
        ]
        warn(
            f"{' and '.join(missing)} not set; building an UNPINNED stock client "
            "(it will use RustDesk's public servers)"
        )
        return 0

    if not CONFIG.is_file():
        fail(f"{CONFIG} not found -- is the libs/hbb_common submodule checked out?")

    original = CONFIG.read_bytes().decode("utf-8")
    patched = apply(original, rendezvous, key)

    # Read-back assertion before anything is written, so a failure cannot leave
    # a half-patched tree behind for cargo to compile.
    for label, needle in (
        ("PROD_RENDEZVOUS_SERVER", f'RwLock::new("{rendezvous}".to_owned());'),
        ("RS_PUB_KEY", f'pub const RS_PUB_KEY: &str = "{key}";'),
    ):
        if needle not in patched:
            fail(f"verification failed: {label} is not {needle!r} after patching")

    # newline="" keeps the checkout's own CRLF/LF endings byte-for-byte.
    with open(CONFIG, "w", encoding="utf-8", newline="") as fh:
        fh.write(patched)

    written = CONFIG.read_bytes().decode("utf-8")
    if written != patched:
        fail("file did not round-trip through write")

    print("::notice::apply_selfhosted_pinning: pinning applied")
    print(f"  file: {CONFIG.relative_to(ROOT)} ({len(original)} -> {len(patched)} bytes)")
    print(f"  PROD_RENDEZVOUS_SERVER = {rendezvous!r}")
    print(f"  RS_PUB_KEY             = {mask(key)!r} ({len(key)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
