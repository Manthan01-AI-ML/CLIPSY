#!/usr/bin/env python3
"""
patch_render_for_reframe.py — One-shot patcher.

Adds support for `user_crop` (from the Reframe modal) to backend/pipeline/render.py:

  1. Adds `user_crop: dict | None = None` parameter to render_one_clip()
  2. Passes user_crop through to build_smart_crop_filter()
  3. Adds `user_crop` extraction from clip_row.meta in rerender_clip_with_edits()

Run from the ClipWise repo root:

    python patch_render_for_reframe.py

Idempotent — safe to re-run. Backs up render.py.bak. If the script can't
find the patterns it expects, it exits with a clear error and changes
nothing.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path


def find_repo_root() -> Path:
    """Walk up looking for backend/pipeline/render.py."""
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "backend" / "pipeline" / "render.py").exists():
            return candidate
    print("ERROR: Could not find backend/pipeline/render.py.")
    print(f"Current dir: {cwd}")
    print("Run this script from inside your ClipWise repo.")
    sys.exit(1)


def main():
    print("=" * 60)
    print("ClipWise render.py reframe patcher")
    print("=" * 60)

    repo = find_repo_root()
    print(f"Repo root: {repo}")
    render_py = repo / "backend" / "pipeline" / "render.py"
    print(f"Patching: {render_py.relative_to(repo)}")
    print()

    src = render_py.read_text()
    original = src
    applied = []
    skipped = []

    # ---------------------------------------------------------------------
    # Patch 1: Add user_crop param to render_one_clip signature
    # ---------------------------------------------------------------------
    # Look for: def render_one_clip(  ...  )
    # Insert user_crop kwarg before the closing paren of params.
    sig_pattern = re.compile(
        r'(def\s+render_one_clip\s*\(\s*[^)]*?detected_language\s*:\s*str\s*=\s*"en"\s*,?)',
        re.MULTILINE | re.DOTALL,
    )
    if "user_crop" in src and "render_one_clip" in src:
        skipped.append("render_one_clip already has user_crop")
    else:
        m = sig_pattern.search(src)
        if not m:
            print("⚠ Could not find render_one_clip signature with detected_language param.")
            print("  This means render.py has a different shape than expected.")
            print("  Aborting without changes.")
            sys.exit(1)
        # Insert after detected_language line
        original_sig = m.group(1)
        # Append user_crop param. Preserve trailing comma if present.
        new_sig = original_sig.rstrip(",") + ',\n    user_crop: dict | None = None,'
        src = src[:m.start()] + new_sig + src[m.end():]
        applied.append("added user_crop param to render_one_clip()")

    # ---------------------------------------------------------------------
    # Patch 2: Pass user_crop to build_smart_crop_filter at call site
    # ---------------------------------------------------------------------
    # First check if already patched — look for our specific addition
    if "user_crop=user_crop," in src and "build_smart_crop_filter" in src:
        skipped.append("call site already passes user_crop")
    else:
        call_pattern = re.compile(
            r'(scale_crop\s*=\s*build_smart_crop_filter\s*\(\s*'
            r'source_video\s*=\s*source\s*,\s*'
            r'clip_start\s*=\s*start\s*,\s*'
            r'clip_duration\s*=\s*duration\s*,\s*'
            r'source_width\s*=\s*sw\s*,\s*'
            r'source_height\s*=\s*sh\s*,\s*'
            r'out_w\s*=\s*out_w\s*,\s*'
            r'out_h\s*=\s*out_h\s*,\s*'
            r'fallback_position\s*=\s*crop_position\s*,?\s*\))',
            re.MULTILINE | re.DOTALL,
        )
        m = call_pattern.search(src)
        if not m:
            print("⚠ Could not find build_smart_crop_filter() call site.")
            print("  render.py may have been modified or have unusual formatting.")
            print("  Aborting without changes.")
            sys.exit(1)
        original_call = m.group(0)
        stripped = original_call.rstrip()
        if not stripped.endswith(")"):
            print("⚠ build_smart_crop_filter call doesn't end with ')'; aborting.")
            sys.exit(1)
        stripped = stripped[:-1].rstrip()
        stripped = stripped.rstrip(",").rstrip()
        new_call = stripped + ",\n                user_crop=user_crop,\n            )"
        src = src[:m.start()] + new_call + src[m.end():]
        applied.append("added user_crop=user_crop to build_smart_crop_filter() call")

    # ---------------------------------------------------------------------
    # Patch 3: Add user_crop extraction in rerender_clip_with_edits
    # ---------------------------------------------------------------------
    if "user_crop_from_meta" in src:
        skipped.append("rerender_clip_with_edits already passes user_crop")
    else:
        rerender_call_pattern = re.compile(
            r'(return\s+render_one_clip\s*\(\s*'
            r'source\s*=\s*source\s*,[^)]+?'
            r'detected_language\s*=\s*detected_language\s*,?\s*\))',
            re.MULTILINE | re.DOTALL,
        )
        m = rerender_call_pattern.search(src)
        if not m:
            print("⚠ Could not find rerender_clip_with_edits → render_one_clip call.")
            print("  Skipping patch 3 (auto-pickup of user_crop from clip.meta will not work).")
            print("  Manual reframe via /clips/{id}/reframe endpoint will still work since")
            print("  it sets user_crop directly when calling render_one_clip.")
        else:
            original_call = m.group(0)
            stripped = original_call.rstrip()
            if not stripped.endswith(")"):
                print("⚠ render_one_clip return doesn't end with ')'; aborting.")
                sys.exit(1)
            stripped = stripped[:-1].rstrip()
            stripped = stripped.rstrip(",").rstrip()
            new_call = stripped + ",\n        user_crop=user_crop_from_meta,\n    )"
            src = src[:m.start()] + new_call + src[m.end():]

            # Inject the extraction line just before the return statement
            return_idx = src.find("return render_one_clip(")
            if return_idx >= 0:
                line_start = src.rfind("\n", 0, return_idx) + 1
                indent = ""
                for ch in src[line_start:return_idx]:
                    if ch == " ":
                        indent += " "
                    else:
                        break
                extraction = (
                    f"{indent}# Reframe support: pull user_crop override from clip meta if present\n"
                    f"{indent}user_crop_from_meta = (getattr(clip_row, 'meta', None) or {{}}).get('user_crop')\n"
                )
                src = src[:line_start] + extraction + src[line_start:]
                applied.append("added user_crop extraction in rerender_clip_with_edits()")

    # ---------------------------------------------------------------------
    # Patch 4: Also patch render_all_clips → render_one_clip if it exists.
    # This makes user_crop work for INITIAL job processing too (future-proof).
    # ---------------------------------------------------------------------
    # Skipped — initial job processing doesn't have user_crop yet (it's set
    # after clips are generated via the Reframe modal). Patches 1+2+3 are
    # sufficient for the reframe flow.

    # ---------------------------------------------------------------------
    # Save
    # ---------------------------------------------------------------------
    if src == original:
        print("✓ No changes needed (already patched)")
        for s in skipped:
            print(f"  • skip: {s}")
        return

    # Backup
    bak = render_py.with_suffix(".py.bak")
    if not bak.exists():
        shutil.copy2(str(render_py), str(bak))
        print(f"✓ backed up to {bak.name}")

    render_py.write_text(src)
    print()
    for a in applied:
        print(f"  ✓ {a}")
    for s in skipped:
        print(f"  • skip: {s}")
    print()
    print(f"Patched render.py written ({len(src)} chars).")
    print()
    print("=" * 60)
    print("✓ DONE")
    print("=" * 60)
    print()
    print("Next step: restart the app:")
    print("  docker-compose down")
    print("  docker-compose up")
    print()
    print("Rollback if needed:")
    print(f"  cp {bak.relative_to(repo)} {render_py.relative_to(repo)}")


if __name__ == "__main__":
    main()