"""Parse arguments, make a qemu snapshot of SWEB and run applicable tests."""

from __future__ import annotations
from pathlib import Path

import argparse
import logging
import sys

from .utils import get_logger
from .constants import SWEB_BUILD_DIR, TEST_RUN_DIR
from .test_specification import get_test_specs, filter_test_specs
from .test_runner import TestRunner
from .tortillas_config import TortillasConfig
from .progress_bar import ProgressBar


def _build_sweb(src_directory: Path, setup: bool, architecture: str):
    import os

    if setup:
        # This command is equivalent to setup_cmake.sh
        cmd = f'cmake -B"{SWEB_BUILD_DIR}" -H"{src_directory}"'

        if architecture in ("x86_32", "x86/32"):
            cmd += " -DARCH=x86/32"

        os.system(cmd)

    if os.system(f"cmake --build {SWEB_BUILD_DIR} -j4") != 0:
        sys.exit(1)
    print()


def main():
    """Tortillas main"""
    log = get_logger("global")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-S",
        "--sweb-path",
        type=Path,
        help="Path to a sweb src directory",
        default=Path.cwd(),
    )

    parser.add_argument(
        "-C",
        "--config-path",
        type=Path,
        help="Path to a tortillas config file",
        default=None,
    )

    parser.add_argument(
        "--arch",
        type=str,
        help="Set sweb architecture target. " "Supported: x86_64, x86_32",
        default="x86_64",
    )

    parser.add_argument(
        "-a", "--skip-setup", action="store_true", help="If set, skip the build setup"
    )

    parser.add_argument(
        "-s", "--skip-build", action="store_true", help="If set, skip building sweb"
    )

    parser.add_argument(
        "--no-progress", action="store_true", help="Turn of the progress bar"
    )

    parser.add_argument(
        "-g",
        "--glob",
        help="Glob of tests in the testfolder\n"
        "e.g. -g test_pthread* "
        "(tests userspace/tests/test_pthread*.c)\n"
        "Note: zsh users must escape: test_pthread\\*",
        default="*",
    )

    parser.add_argument(
        "-c",
        "--category",
        type=str,
        nargs="*",
        help="Category or a list of categories to test",
    )

    parser.add_argument(
        "-t", "--tag", type=str, nargs="*", help="Tag or list of tags to test"
    )

    parser.add_argument(
        "-r",
        "--repeat",
        type=int,
        help="Run the specified tests multiple times. "
        "-r 2 will run all tests 2 times",
        default=1,
    )
    parser.add_argument("-o", "--output",type=str, help="if set outputs a junit xml file with the specified name")


    args = parser.parse_args()

    sweb_src_folder = args.sweb_path
    if not args.config_path:
        args.config_path = sweb_src_folder / "tortillas_config.yml"

    progress_bar = ProgressBar(args.no_progress)
    log.info("Starting tortillas© test system\n")

    config = TortillasConfig(args.config_path)

    all_specs = get_test_specs(sweb_src_folder, args.glob)
    selected_specs = filter_test_specs(all_specs, args.category, args.tag)
    if len(selected_specs) == 0:
        log.error("No test specs were found")
        sys.exit(1)

    test_runner = TestRunner(
        selected_specs, args.repeat, args.arch, config, progress_bar
    )

    progress_bar.update_main_status("Building SWEB")
    if not args.skip_build:
        _build_sweb(sweb_src_folder, not args.skip_setup, args.arch)

    if not TEST_RUN_DIR.is_dir():
        TEST_RUN_DIR.mkdir()

    progress_bar.update_main_status("Creating snapshot")
    test_runner.create_snapshot()

    progress_bar.update_main_status("Running tests")
    test_runner.start()

    if not test_runner.success:
        log.error("Tortillas has failed!")

    log.info("Completed tortillas© test system\n")

    summary = test_runner.get_markdown_test_summary()
    log.info("")
    log.info(summary)

    logging.shutdown()
    if(args.output):
        test_runner.write_junit_xml(sweb_src_folder / args.output)

    sys.exit(not test_runner.success)


if __name__ == "__main__":
    main()
