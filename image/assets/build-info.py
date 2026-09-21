#!/usr/bin/env python3
"""Generate /usr/share/prusa-buddy3d-camera/build-info.json (AC-14).

Standard library only, as required for image assets. Records the source commit,
the pinned image-builder revision, the OS suite, the kernel package, and (when
WP-2b supplies it) the installed-package manifest. Output is deterministic:
keys are sorted and no wall-clock time is embedded; SOURCE_DATE_EPOCH is copied
through when present.

WP-2b extension point: pass --package-manifest with the generated package
manifest path to have it recorded here.
"""

import argparse
import json
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--builder-revision", required=True)
    parser.add_argument("--os-suite", required=True)
    parser.add_argument("--kernel-package", required=True)
    parser.add_argument("--package-manifest", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    doc = {
        "schema_version": 1,
        "source_commit": args.source_commit,
        "builder_revision": args.builder_revision,
        "os_suite": args.os_suite,
        "kernel_package": args.kernel_package,
        # WP-2b fills this; null until then.
        "package_manifest": args.package_manifest or None,
    }
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch:
        doc["source_date_epoch"] = source_date_epoch

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
