"""Validate Pipecat definitions and product selection without credentials or network access."""

import argparse
from pathlib import Path

import yaml
from k8s_stack_tooling.addons import ContractError, load_manifest, load_selection
from k8s_stack_tooling.gateway_catalog.metadata import MetadataError
from k8s_stack_tooling.gateway_catalog.values import read_values

from pipecat_tooling.selection import SelectionError, select_pipecat, validate_selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Pipecat checkout")
    parser.add_argument("--list-packages", action="store_true")
    parser.add_argument("--values", type=Path, action="append", default=[])
    parser.add_argument("--selection", type=Path, help="Complete client config/addons.yaml")
    args = parser.parse_args(argv)
    if args.selection and not args.values:
        parser.error("--selection requires ordered --values inputs")
    try:
        addon = load_manifest(args.root / "release/manifest.yaml")
        if addon.metadata.name != "pipecat":
            raise ContractError("expected the Pipecat add-on")
        if args.values:
            product = read_values(args.values).get("pipecat")
            select_pipecat(product)
            if args.selection:
                validate_selection(addon, load_selection(args.selection), product)
    except (ContractError, SelectionError, MetadataError, OSError, yaml.YAMLError):
        parser.exit(1, "Pipecat definition or selection is invalid; supplied values are hidden\n")
    if args.list_packages:
        for package in addon.spec.packages:
            print(package.path)
    else:
        print("Pipecat definition and supplied product settings are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
