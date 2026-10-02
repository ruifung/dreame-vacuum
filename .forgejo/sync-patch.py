#!/usr/bin/env python3
"""Applies this fork's deactivation patches on top of a fresh upstream checkout.

Run from the repository root after `git checkout upstream/master -- .`.
Idempotent: safe to run repeatedly.

What it does (only step 1 is mandatory; the rest are best-effort and allowed
to fail if upstream changes so a pattern is no longer found):
  1. Delete the obfuscated proprietary license frontend (frontend.py).
  2. Strip the obfuscated JS bundle + banner image constants (FRONTEND, DVC)
     from const.py.
  3. Drop the now-undefined `DVC` import from config_flow.py / coordinator.py.
  4. Switch hacs.json to "install from repo" (no zip release).
  5. Strip the DVC paywall marketing / notification banners (best-effort).

Only depends on Python 3 stdlib.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENT = os.path.join(ROOT, "custom_components", "dreame_vacuum")


def log(msg: str) -> None:
    print(f"==> {msg}")


def remove_frontend() -> None:
    log("Removing obfuscated license frontend")
    path = os.path.join(COMPONENT, "frontend.py")
    if os.path.exists(path):
        os.remove(path)
        print(f"  deleted {os.path.relpath(path, ROOT)}")
    else:
        print("  already absent")


def strip_constants() -> None:
    log("Stripping FRONTEND / DVC constants from const.py")
    path = os.path.join(COMPONENT, "const.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for name in ("FRONTEND", "DVC"):
        pat = re.compile(
            r"^" + re.escape(name) + r":\s*Final\s*=\s*\(\n.*?\n\)\n?",
            re.MULTILINE | re.DOTALL,
        )
        m = pat.search(src)
        if not m:
            print(f"  WARNING: {name} block not found (skipped)")
            continue
        # const blocks are tiny (a few lines); keep a tight ceiling.
        if not _span_ok(src, m.start(), m.end(), f"{name} block", 25):
            continue
        src = src[:m.start()] + src[m.end():]
    src = src.rstrip() + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("  done")


def strip_dvc_imports() -> None:
    log("Removing DVC import from config_flow.py / coordinator.py")
    for name in ("config_flow.py", "coordinator.py"):
        path = os.path.join(COMPONENT, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        new = src.replace("    DVC,\n", "")
        if new == src:
            print(f"  WARNING: no '    DVC,' line found in {name}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"  patched {name}")


def _remove_import(path: str, name: str, label: str) -> None:
    """Remove a single `    <name>,` line from an import block (best-effort)."""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    line = f"    {name},\n"
    new = src.replace(line, "")
    if new == src:
        print(f"  WARNING: no '{line.strip()}' line found in {label} (skipped)")
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"  removed {name} import from {label}")


def strip_dvc_key_option() -> None:
    """Remove the DVC license-key config option everywhere (CONF_DVC_KEY).

    Removes the constant from const.py, the import + options-schema field in
    config_flow.py, and the import in coordinator.py. Best-effort; each edit is
    guarded and aborts independently if the pattern is not found.
    """
    log("Removing CONF_DVC_KEY option (const.py, config_flow.py, coordinator.py)")

    const_path = os.path.join(COMPONENT, "const.py")
    with open(const_path, encoding="utf-8") as f:
        const = f.read()
    pat = re.compile(r'^CONF_DVC_KEY: Final = "dvc_key"\n?', re.MULTILINE)
    m = pat.search(const)
    if not m:
        print("  WARNING: CONF_DVC_KEY constant not found in const.py (skipped)")
    else:
        const = const[:m.start()] + const[m.end():]
        with open(const_path, "w", encoding="utf-8") as f:
            f.write(const)
        print("  removed CONF_DVC_KEY constant from const.py")

    _remove_import(os.path.join(COMPONENT, "coordinator.py"), "CONF_DVC_KEY", "coordinator.py")
    _remove_import(os.path.join(COMPONENT, "config_flow.py"), "CONF_DVC_KEY", "config_flow.py")

    # config_flow.py: drop the CONF_DVC_KEY input-handling block in async_step_init
    cfg_path = os.path.join(COMPONENT, "config_flow.py")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = f.read()

    key_block_start = '            key = (user_input.get(CONF_DVC_KEY) or "").strip().lower()\n'
    key_block_end = '                errors["base"] = "invalid_key"\n\n'
    s = cfg.find(key_block_start)
    e = cfg.find(key_block_end, s + len(key_block_start)) if s != -1 else -1
    if s != -1 and e != -1:
        end = e + len(key_block_end)
        if not _span_ok(cfg, s, end, "CONF_DVC_KEY input block", 12):
            return
        cfg = cfg[:s] + cfg[end:]

    # config_flow.py: drop the vol.Optional(CONF_DVC_KEY, ...) schema field
    schema_start = "                vol.Optional(\n"
    schema_end = "                ): str,\n"
    s2 = cfg.find(schema_start)
    e2 = cfg.find(schema_end, s2 + len(schema_start)) if s2 != -1 else -1
    if s2 != -1 and e2 != -1:
        end2 = e2 + len(schema_end)
        if not _span_ok(cfg, s2, end2, "CONF_DVC_KEY schema field", 15):
            return
        cfg = cfg[:s2] + cfg[end2:]

    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(cfg)
    print("  removed CONF_DVC_KEY handling from config_flow.py")


def patch_hacs() -> None:
    log("Patching hacs.json (install from repo, no zip release)")
    path = os.path.join(ROOT, "hacs.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.pop("zip_release", None)
    data.pop("filename", None)
    data["render_readme"] = False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print("  done")


def _lines_between(src: str, start: int, end: int) -> int:
    """Count the number of newlines in src[start:end] (i.e. span in lines)."""
    return src[start:end].count("\n")


def _span_ok(src: str, start: int, end: int, label: str, max_lines: int) -> bool:
    """Return True if src[start:end] spans <= max_lines lines.

    Otherwise print a warning and return False so the caller aborts that patch.
    Tuning: each operation passes its own max_lines ceiling.
    """
    span = _lines_between(src, start, end)
    if span > max_lines:
        print(f"  WARNING: {label} span too large ({span} lines > {max_lines}); "
              f"aborting this patch")
        return False
    return True


def _strip_block(src: str, label: str, start_marker: str, end_marker: str,
                 max_lines: int = 25):
    """Remove a contiguous block between start_marker and end_marker.

    max_lines is the per-operation sanity ceiling: if the matched span exceeds
    it, the block is left unchanged and "aborted" is returned.
    Returns (new_src, status) where status is one of:
      "stripped"  - block was removed
      "notfound"  - markers not present (nothing to do)
      "aborted"   - span exceeded the sanity guard; left unchanged
    """
    s = src.find(start_marker)
    e = src.find(end_marker, s + len(start_marker)) if s != -1 else -1
    if s == -1 or e == -1:
        return src, "notfound"
    # include the trailing newline after end_marker if present
    end = e + len(end_marker)
    if src[end:end + 1] == "\n":
        end += 1
    if not _span_ok(src, s, end, label, max_lines):
        return src, "aborted"
    return src[:s] + src[end:], "stripped"


def strip_config_flow_banner() -> None:
    log("Neutralizing DVC step in config_flow.py (no UI form)")
    path = os.path.join(COMPONENT, "config_flow.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # Replace the whole async_step_dvc method with one that completes the flow
    # immediately instead of showing the DVC license-key form in the UI. We anchor
    # on the method definition and extend to the next top-level `def `.
    start_marker = "    async def async_step_dvc("
    s = src.find(start_marker)
    if s == -1:
        print("  WARNING: async_step_dvc not found in config_flow.py (skipped)")
        return
    nxt = src.find("\n    def ", s)
    if nxt == -1:
        print("  WARNING: could not locate end of async_step_dvc (skipped)")
        return
    replacement = (
        "    async def async_step_dvc("
        "self, user_input: dict[str, Any] | None = None) -> FlowResult:\n"
        "        return self.async_create_entry(\n"
        "            title=self.name,\n"
        "            data={\n"
        "                CONF_NAME: self.name,\n"
        "                CONF_HOST: self.host,\n"
        "                CONF_TOKEN: self.token,\n"
        "                CONF_USERNAME: self.username,\n"
        "                CONF_PASSWORD: self.password,\n"
        "                CONF_COUNTRY: self.country,\n"
        "                CONF_MAC: self.mac,\n"
        "                CONF_DID: self.device_id,\n"
        "                CONF_AUTH_KEY: self.protocol.cloud.auth_key if self.protocol and self.protocol.cloud else None,\n"
        "                CONF_ACCOUNT_TYPE: self.account_type,\n"
        "            },\n"
        "            options=self.options,\n"
        "        )\n\n"
    )
    # Whole-method replacement: allow a larger per-operation ceiling (a real
    # method can be several dozen lines) while still catching runaway matches.
    if not _span_ok(src, s, nxt, "async_step_dvc", 80):
        return
    new = src[:s] + replacement + src[nxt + 1:]  # +1 to keep the leading \n of the next def
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print("  done")


def strip_coordinator_banner() -> None:
    log("Stripping DVC paywall notification banner from coordinator.py")
    path = os.path.join(COMPONENT, "coordinator.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    start = "            # Version check to ensure each user only sees dvc notification once"
    end = '                notification_id=f"{DOMAIN}_dvc",\n                )'
    new, status = _strip_block(src, "coordinator banner", start, end)
    if status == "notfound":
        print("  WARNING: DVC banner pattern not found in coordinator.py (skipped)")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"  {status}")


def _optional(label: str, fn) -> None:
    """Run an optional patch step, warning (never failing) on any error.

    Only the frontend deletion is mandatory; every other patch is best-effort.
    If a pattern is missing or an unexpected error occurs, we log a warning and
    continue so the daily sync never fails because of a changed upstream.
    """
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - all optional steps are best-effort
        print(f"  WARNING: {label} failed ({type(exc).__name__}): {exc} (skipped)")


def main() -> int:
    # Mandatory: remove the obfuscated license frontend.
    remove_frontend()

    # Everything below is optional / best-effort.
    _optional("strip_constants", strip_constants)
    _optional("strip_dvc_imports", strip_dvc_imports)
    _optional("patch_hacs", patch_hacs)
    _optional("strip_config_flow_banner", strip_config_flow_banner)
    _optional("strip_coordinator_banner", strip_coordinator_banner)
    # Must run after the coordinator banner strip, which references CONF_DVC_KEY.
    _optional("strip_dvc_key_option", strip_dvc_key_option)

    log("Sync patch complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
