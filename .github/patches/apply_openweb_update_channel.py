#!/usr/bin/env python3
"""Fork-local (openweb) build hook -- not an upstream file.

Points the client's update check at the Openweb release channel and makes the
"new version" banner open that release page instead of RustDesk's in-app
updater.

Why this exists
---------------
1.5.3 still asks api.rustdesk.com for the newest version:

    https://api.rustdesk.com/version/latest   (libs/hbb_common/src/lib.rs,
                                               version_check_request)

So a self-hosted client is told that 1.5.x exists and its "Download" button
fetches the *official* installer, which is a stock client aimed at RustDesk's
public servers -- clicking it silently moves the user off our hbbs.

1.5.3 also added signed update metadata (src/update_metadata.rs,
TRUSTED_UPDATE_KEYS): the in-app updater only accepts artifacts described in a
manifest signed by RustDesk's ed25519 key, so our own builds can never be
installed by it. Hence the banner now opens the release page in the browser.

What it does
------------
  * rewrites the single `const URL` inside version_check_request() so the check
    asks https://openweb.com.br/_files/rustdesk/version.php (a tiny endpoint
    that returns {"url": ".../releases/tag/<version>"} for the newest published
    build, or {"url": ""} when there is nothing newer);
  * applies .github/patches/openweb-update-button.patch, which turns the banner
    button into a plain browser hand-off to that release page.

Both edits happen in the throwaway CI checkout only; the submodule pointer and
the tracked sources stay byte-identical to upstream, so future merges cannot
conflict with this.

Behaviour
---------
Anchor drift in lib.rs or a patch that no longer applies aborts the build, so a
build can never silently keep pointing at api.rustdesk.com.

Usage (cwd-independent; paths resolve from this file):
    python3 .github/patches/apply_openweb_update_channel.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "libs" / "hbb_common" / "src" / "lib.rs"
DART_PATCH = ROOT / ".github" / "patches" / "openweb-update-button.patch"

UPDATE_URL = "https://openweb.com.br/_files/rustdesk/version.php"

FUNCTION_ANCHOR = "pub fn version_check_request("
URL_RE = re.compile(r'const URL: &str = "[^"\r\n]*";')


def fail(msg: str) -> None:
    print(f"::error::apply_openweb_update_channel: {msg}", file=sys.stderr)
    raise SystemExit(1)


def patch_version_url(text: str) -> str:
    """Rewrite the `const URL` that belongs to version_check_request()."""
    start = text.find(FUNCTION_ANCHOR)
    if start < 0:
        fail(f"anchor {FUNCTION_ANCHOR!r} not found in {LIB.relative_to(ROOT)}")

    match = URL_RE.search(text, start)
    if not match:
        # Do not cross into unrelated code: the const must live in this function.
        fail(
            f"no `const URL` inside version_check_request() in "
            f"{LIB.relative_to(ROOT)} -- upstream changed the file, re-anchor this patch"
        )

    end_of_function = text.find("\n}", start)
    if end_of_function >= 0 and match.start() > end_of_function:
        fail("`const URL` moved out of version_check_request(), re-anchor this patch")

    return text[: match.start()] + f'const URL: &str = "{UPDATE_URL}";' + text[match.end() :]


def main() -> int:
    if not LIB.is_file():
        fail(f"{LIB} not found -- is the libs/hbb_common submodule checked out?")
    if not DART_PATCH.is_file():
        fail(f"{DART_PATCH} not found")

    original = LIB.read_bytes().decode("utf-8")
    patched = patch_version_url(original)
    if f'const URL: &str = "{UPDATE_URL}";' not in patched:
        fail("verification failed: version check URL was not rewritten")

    # newline="" keeps the checkout's own CRLF/LF endings byte-for-byte.
    with open(LIB, "w", encoding="utf-8", newline="") as fh:
        fh.write(patched)
    if LIB.read_bytes().decode("utf-8") != patched:
        fail("file did not round-trip through write")

    for args in (["--check"], []):
        result = subprocess.run(
            ["git", "apply", *args, str(DART_PATCH)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            fail(
                "openweb-update-button.patch does not apply "
                f"({'check' if args else 'apply'}): {result.stderr.strip()}"
            )

    print("::notice::apply_openweb_update_channel: update channel applied")
    print(f"  file: {LIB.relative_to(ROOT)} ({len(original)} -> {len(patched)} bytes)")
    print(f"  version check URL = {UPDATE_URL!r}")
    print(f"  dart patch        = {DART_PATCH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
