#!/usr/bin/env python3
"""catalog_fetch.py - list, download and convert plugins from the community OpenMPC catalog.

Usage:
    catalog_fetch.py list    [--gen2-verified] [--arch aarch64|armv7] [--md FILE]
    catalog_fetch.py convert ID [ID ...]        [--arch aarch64|armv7] [--out DIR] [--zip] [--force]
    catalog_fetch.py convert --gen2-verified    [--arch aarch64]       [--out DIR] [--zip] [--force]

The OpenMPC catalog (https://open-mpc.github.io/) lists VST2 plugins packaged for a *modified* MPC OS: each
release zip holds an install.sh that needs root, stops the MPC and edits MPC.settings. This tool never runs
those scripts. It only

  1. reads the catalog (catalog.json),
  2. downloads the release zip for your MPC's CPU from the URL in the catalog,
  3. checks its size and SHA-256 against the catalog, and
  4. hands it to legacy_convert.py, which rewrites it into an ordinary plugin folder (plugin.json, version.xml,
     the plugin's own .so left unchanged, Plugin Skins/, data) for a "VST2 Plugins" folder.

Then copy the folders in --out into "VST2 Plugins" on a drive, or install them with
    python tools/mpc_remote.py <MPC address> add <out folder>

"--gen2-verified" picks the instruments and effects that the catalog records as tested on a Gen2 (64-bit) MPC
such as the Live III. Those tests were done with the community installer, not the VST2 Plugins folder, so
test each converted plugin yourself. Addins (code preloaded into the MPC app) are never converted.

Arch: aarch64 = MPC Live III, XL and other Gen2 models; armv7 = MPC X, Live, Live II, One, One+, Key 61/37, Force.

Standard library only. Python 3.9+.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import legacy_convert  # noqa: E402

CATALOG_URLS = (
    "https://open-mpc.github.io/catalog.json",
    "https://sd88me.github.io/mpc-vst-plugins/catalog.json",  # the catalog's old address, kept as a copy
    "https://raw.githubusercontent.com/open-mpc/open-mpc.github.io/main/catalog.json",  # the site's source
)
USER_AGENT = "mpc-plugin-sdk-catalog-fetch/1.0"
MAX_ZIP = 512 * 1024 * 1024


class FetchError(Exception):
    pass


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def load_catalog(source=None):
    """catalog.json from a local file or URL (source), else the first of CATALOG_URLS that answers."""
    if source and os.path.isfile(source):
        with open(source, encoding="utf-8") as f:
            return json.load(f), source
    errors = []
    for url in ([source] if source else CATALOG_URLS):
        try:
            with http_get(url) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("plugins"), list):
                return data, url
            errors.append("%s: not a plugin catalog" % url)
        except Exception as e:  # network errors differ by platform; report them all the same way
            errors.append("%s: %s" % (url, e))
    raise FetchError("cannot read the catalog:\n  " + "\n  ".join(errors))


def current_version(entry):
    """The catalog's newest stable, non-yanked release of an entry, or None."""
    versions = [v for v in entry.get("versions", []) if not v.get("yanked") and v.get("assets")]
    latest = entry.get("latest")
    for v in versions:
        if v.get("version") == latest:
            return v
    stable = [v for v in versions if v.get("channel", "stable") == "stable"]
    return (stable or versions or [None])[0]


def gen2_tests(version):
    return [t for t in version.get("tested") or [] if "gen2" in json.dumps(t).lower()]


def select(catalog, ids=None, gen2_verified=False, arch="aarch64"):
    """Returns (rows, skipped). Each row: entry, version, asset. skipped: (id, reason)."""
    by_id = {e.get("id"): e for e in catalog["plugins"]}
    if ids:
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise FetchError("not in the catalog: %s (see the list command)" % ", ".join(missing))
        entries = [by_id[i] for i in ids]
    else:
        entries = sorted(catalog["plugins"], key=lambda e: e.get("name", "").lower())
    rows, skipped = [], []
    for e in entries:
        if gen2_verified and "gen2" not in (e.get("verified_on") or []):
            continue
        if e.get("kind") not in ("instrument", "effect"):
            skipped.append((e["id"], "%s: code loaded into the MPC app, not a plugin; cannot be converted" % e.get("kind")))
            continue
        if e.get("distribution") != "release":
            skipped.append((e["id"], "no download in the catalog (build-it-yourself)"))
            continue
        v = current_version(e)
        asset = (v or {}).get("assets", {}).get(arch)
        if not asset or not asset.get("url") or not asset.get("sha256"):
            skipped.append((e["id"], "no %s build" % arch))
            continue
        rows.append({"entry": e, "version": v, "asset": asset})
    return rows, skipped


def needs_user_files(row):
    """What the user must add (ROMs, OS files), from the catalog's 'requires' text or the package's user_files list."""
    req = (row["version"].get("requires") or "").strip()
    if req:
        return req
    files = (row["version"].get("manifest") or {}).get("user_files") or []
    labels = ["%s (in the plugin's %s/ folder)" % (u.get("label", "files"), u.get("path", ""))
              for u in files if isinstance(u, dict) and "optional" not in (u.get("label", "") + u.get("description", "")).lower()]
    return "; ".join(labels)


def download(url, dest, sha256, size=None):
    h = hashlib.sha256()
    got = 0
    with http_get(url, timeout=120) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            got += len(chunk)
            if got > MAX_ZIP:
                raise FetchError("%s is larger than %d MB; refusing it" % (url, MAX_ZIP >> 20))
            h.update(chunk)
            f.write(chunk)
    if size and got != size:
        raise FetchError("%s: downloaded %d bytes, the catalog says %d" % (url, got, size))
    if h.hexdigest().lower() != sha256.lower():
        raise FetchError("%s: SHA-256 does not match the catalog (got %s)" % (url, h.hexdigest()))


def table_md(rows, arch):
    lines = ["| Plugin | Kind | Author | Version | Licence | Gen2 test (catalog) | Needs |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        e, v = r["entry"], r["version"]
        tests = "; ".join("%s, %s" % (t.get("device", "?"), t.get("firmware") or "?") for t in gen2_tests(v)) or "-"
        req = needs_user_files(r).replace("|", "/") or "-"
        lines.append("| %s (`%s`) | %s | %s | %s | %s | %s | %s |" % (
            e["name"], e["id"], e["kind"], e.get("author", ""), v["version"], e.get("license", "") or "?", tests, req))
    return "\n".join(lines)


def cmd_list(args):
    catalog, src = load_catalog(args.catalog)
    rows, skipped = select(catalog, gen2_verified=args.gen2_verified, arch=args.arch)
    print("Catalog: %s (%d entries)" % (src, len(catalog["plugins"])))
    print("%d plugin(s) with a %s build%s:\n" % (len(rows), args.arch, ", verified on Gen2" if args.gen2_verified else ""))
    for r in rows:
        e, v = r["entry"], r["version"]
        flag = "  [needs extra files or setup]" if needs_user_files(r) else ""
        print("  %-30s %-10s %-9s %s%s" % (e["id"], e["kind"], v["version"], e["name"], flag))
    if skipped:
        print("\nNot convertible:")
        for i, why in skipped:
            print("  %-30s %s" % (i, why))
    if args.md:
        with open(args.md, "w", encoding="utf-8", newline="\n") as f:
            f.write("# OpenMPC catalog plugins%s (%s)\n\nCatalog: %s, read by tools/catalog_fetch.py.\n\n%s\n" % (
                " verified on Gen2" if args.gen2_verified else "", args.arch, src, table_md(rows, args.arch)))
            if skipped:
                f.write("\nNot convertible:\n\n" + "".join("- `%s`: %s\n" % s for s in skipped))
        print("\nWrote %s" % args.md)
    return 0


def cmd_convert(args):
    if not args.ids and not args.gen2_verified:
        raise FetchError("name plugin ids, or use --gen2-verified")
    catalog, src = load_catalog(args.catalog)
    rows, skipped = select(catalog, ids=args.ids or None, gen2_verified=args.gen2_verified, arch=args.arch)
    for i, why in skipped:
        print("SKIP %s: %s" % (i, why))
    os.makedirs(args.out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="catalog-")
    done, failed = [], []
    try:
        for r in rows:
            e, v, a = r["entry"], r["version"], r["asset"]
            print("%s %s (%s)" % (e["name"], v["version"], args.arch))
            zpath = os.path.join(work, os.path.basename(a["url"].split("?")[0]) or (e["id"] + ".zip"))
            try:
                if args.cache and os.path.isfile(os.path.join(args.cache, os.path.basename(zpath))):
                    shutil.copy(os.path.join(args.cache, os.path.basename(zpath)), zpath)
                    print("  using cached %s" % os.path.basename(zpath))
                    download_ok = hashlib.sha256(open(zpath, "rb").read()).hexdigest() == a["sha256"].lower()
                    if not download_ok:
                        raise FetchError("cached zip does not match the catalog's SHA-256; delete it")
                else:
                    download(a["url"], zpath, a["sha256"], a.get("size"))
                    print("  downloaded and checked (SHA-256 matches the catalog)")
                    if args.cache:
                        os.makedirs(args.cache, exist_ok=True)
                        shutil.copy(zpath, os.path.join(args.cache, os.path.basename(zpath)))
                results = legacy_convert.convert([zpath], args.out, args.zip, args.force)
            except (FetchError, legacy_convert.ConvertError, OSError) as ex:
                print("  FAILED: %s" % ex)
                failed.append((e["id"], str(ex)))
                continue
            for res in results:
                print("  -> %s" % res["folder"])
                for w in res["warnings"]:
                    if "no 32-bit build" in w and args.arch == "aarch64":
                        continue  # expected: only the 64-bit build was asked for
                    print("     WARNING: %s" % w)
                req = needs_user_files(r)
                if req:
                    print("     NEEDS: %s" % req)
                done.append((r, res))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if done:
        write_notes(args.out, done, src, args.arch)
    print("\nConverted %d, failed %d, skipped %d. Output: %s" % (len(done), len(failed), len(skipped), os.path.abspath(args.out)))
    for i, why in failed:
        print("  failed %s: %s" % (i, why))
    if done:
        print("Install: copy the folders into 'VST2 Plugins' on a drive, or run\n"
              "  python tools/mpc_remote.py <MPC address> add %s" % args.out)
    return 1 if failed else 0


def write_notes(out, done, src, arch):
    path = os.path.join(out, "CONVERTED.md")
    lines = ["# Converted from the OpenMPC catalog", "",
             "Source: %s. Build: %s. Converted by tools/catalog_fetch.py + tools/legacy_convert.py; the plugin binaries are" % (src, arch),
             "unchanged. These plugins were made for the community installer on a modified MPC OS; through the VST2 Plugins",
             "folder they are untested until you try them. Check each licence before sharing your converted copies.", "",
             "| Folder | Version | Licence | Source | Needs |", "|---|---|---|---|---|"]
    for r, res in done:
        e, v = r["entry"], r["version"]
        lines.append("| %s | %s | %s | %s | %s |" % (os.path.basename(res["folder"]), v["version"], e.get("license") or "?",
                                                    "https://github.com/" + e["repo"] if e.get("repo") else "-",
                                                    needs_user_files(r).replace("|", "/") or "-"))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="List, download and convert plugins from the OpenMPC catalog.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--arch", choices=("aarch64", "armv7"), default="aarch64",
                        help="aarch64: Live III, XL and other Gen2; armv7: X, Live, Live II, One, Key, Force (default aarch64)")
    common.add_argument("--gen2-verified", action="store_true", help="only plugins the catalog records as tested on a Gen2 MPC")
    common.add_argument("--catalog", help="catalog.json file or URL to use instead of the default addresses")
    sub = ap.add_subparsers(dest="command", required=True)
    lp = sub.add_parser("list", parents=[common], help="show the plugins that can be converted")
    lp.add_argument("--md", help="also write the list as a Markdown table to this file")
    cp = sub.add_parser("convert", parents=[common], help="download, verify and convert plugins")
    cp.add_argument("ids", nargs="*", help="catalog ids (from the list command)")
    cp.add_argument("--out", default="converted", help="where to write the plugin folders (default ./converted)")
    cp.add_argument("--zip", action="store_true", help="also write a zip with INSTALL.txt per plugin")
    cp.add_argument("--force", action="store_true", help="replace converted folders that already exist")
    cp.add_argument("--cache", help="keep downloaded zips in this folder and reuse them")
    args = ap.parse_args(argv)
    try:
        return cmd_list(args) if args.command == "list" else cmd_convert(args)
    except FetchError as e:
        print("ERROR: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
