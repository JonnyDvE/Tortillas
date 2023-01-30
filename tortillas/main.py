#!/usr/bin/env python3

# SPDX-FileCopyrightText: © 2022 Leo Moser, Maximilian Seidler
import argparse
import logging
import os
import sys
import pathlib
import threading

from utils import get_logger
from constants import TestStatus, TEST_FOLDER_PATH, QEMU_VMSTATE_TAG
from test_runner import Test, create_snapshot, run_tests
from tortillas_config import TortillasConfig
from progress_bar import ProgressBar
from test_specification import NoTestSpecFound


# On exception, exit the program
def exception_hook(args):
    log = get_logger('global')
    log.error("XO", exc_info=args)
    sys.exit(-1)


# Set the exception hook for all threads
threading.excepthook = exception_hook


def get_tests_to_run(tortillas_config: TortillasConfig, sweb_src_folder: str,
                     test_glob: str, repeat: int, category: list[str],
                     tag: list[str], **kwargs) -> tuple[list[Test], list[str]]:
    file_paths = list(pathlib.Path(
        f'{sweb_src_folder}/{TEST_FOLDER_PATH}').glob(f'{test_glob}*.c'))

    tests: list[Test] = []
    for file_path in file_paths:
        for num in range(repeat):
            try:
                tests.append(Test(file_path.stem, num, sweb_src_folder,
                                  tortillas_config))
            except NoTestSpecFound:
                continue

    if category:
        tests = [test for test in tests
                 if test.spec.category in category]

    if tag:
        tests = [test for test in tests
                 if any(tag in test.spec.tags for tag in tag)]

    disabled_tests = [test.name for test in tests if test.spec.disabled]
    tests = [test for test in tests if not test.spec.disabled]

    tests.sort(key=(lambda test: test.name), reverse=True)
    if repeat > 1:
        tests.sort(key=(lambda test: test.run_number))

    return tests, disabled_tests


def get_markdown_test_summary(tests: list[Test],
                              disabled_tests: list[Test],
                              success: bool) -> str:

    def markdown_table_row(cols: list[str],
                           widths: list[int] = [40, 20]) -> str:
        assert (len(widths) == len(cols))
        res = '|'
        for cell, width in zip(cols, widths):
            padding = width - len(cell) - 2
            if padding < 0:
                raise ValueError(f'\"{cell}\" is to long '
                                 'for the table width')
            res += f" {cell}{' '*padding}|"
        return res + '\n'

    def markdown_table_delim(widths: list[int] = [40, 20]):
        res = '|'
        for width in widths:
            res += f" {'-'*(width-3)} |"
        return res + '\n'

    tests.sort(key=(lambda test: test.result.status.name))

    summary = ''
    summary += markdown_table_row(['Test run', 'Result'])
    summary += markdown_table_delim()

    for test in disabled_tests:
        summary += markdown_table_row([test, TestStatus.DISABLED.name])

    for test in tests:
        summary += markdown_table_row([repr(test),
                                      test.result.status.name])

    if not success:
        failed_tests = (test for test in tests
                        if test.result.status in
                        [TestStatus.FAILED, TestStatus.PANIC])

        summary += '\n\n'
        summary += '## Errors\n\n'

        for test in failed_tests:
            summary += f'### {repr(test)} - {test.get_tmp_dir()}/out.log\n\n'
            for error in test.result.errors:
                if error[-1] not in ['\n', '\r']:
                    error = f'{error}\n'

                if error[-2:] == '\n\n':
                    error = error[:-1]

                if '=== Begin of backtrace' in error:
                    summary += f'```\n{error}```'
                    continue

                summary += f'- {error}'
            summary += '\n'

    with open('tortillas_summary.md', 'w') as summary_file:
        summary_file.write(summary)

    return summary


def main():
    log = get_logger('global')

    parser = argparse.ArgumentParser()
    parser.add_argument('--arch',
                        help='Set the architecture to build for e.g. x86_64',
                        default='x86_64', type=str)

    parser.add_argument('-g', '--test-glob',
                        help='Identifier of testcases in the test source dir,'
                             ' e.g. -b test_pthread (tests test_pthread*.c)',
                        default='')

    parser.add_argument('-c', '--category', type=str, nargs='*',
                        help='Category or a list of categories to test')

    parser.add_argument('-t', '--tag', type=str, nargs='*',
                        help='tag or list of tags to test')

    parser.add_argument('-r', '--repeat',
                        help='Run the specified tests mutiple times.'
                             'e.g. -r 2 will run all tests 2 times',
                        default=1, type=int)

    parser.add_argument('-a', '--skip-arch',
                        action='store_true',
                        help='If set, skip architecture build')

    parser.add_argument('-s', '--skip',
                        action='store_true',
                        help='If set, skip build')

    parser.add_argument('--no-progress',
                        action='store_true',
                        help='Turn of the progress bar')

    args = parser.parse_args()

    sweb_src_folder = os.getcwd()

    progress_bar = ProgressBar(args.no_progress)
    log.info('Starting tortillas© test system\n')

    config = TortillasConfig()
    tests, disabled_tests = get_tests_to_run(config, sweb_src_folder,
                                             **vars(args))

    if len(tests) == 0:
        log.error('No tests were found')
        sys.exit(-1)

    log.info('Registered tests:')
    for test in tests:
        log.info(f'- {repr(test)}')
    log.info('')

    # Build
    if not args.skip_arch and not args.skip:
        progress_bar.update_main_status('Setting up SWEB build')
        # This command is equivalent to setup_cmake.sh
        os.system(f'cmake -B\"{config.build_directory}\" -H.')
        os.chdir(config.build_directory)
        os.system(f'echo yes | make {args.arch}')
    else:
        os.chdir(config.build_directory)

    if not args.skip:
        progress_bar.update_main_status('Building SWEB')
        if os.system('make') != 0:
            return -1
        log.info('')

    if not os.path.exists(config.test_run_directory):
        os.mkdir(config.test_run_directory)

    progress_bar.update_main_status('Creating snapshot')
    create_snapshot(args.arch, QEMU_VMSTATE_TAG, config)

    progress_bar.update_main_status('Running tests')
    run_tests(tests, args.arch, progress_bar, config)

    success = not any(
            test.result.status in (TestStatus.FAILED, TestStatus.PANIC)
            for test in tests)

    if not success:
        log.error('Tortillas has failed!')

    log.info('Completed tortillas© test system\n')

    summary = get_markdown_test_summary(tests, disabled_tests, success)

    log.info('')
    log.info(summary)

    logging.shutdown()
    sys.exit(not success)


if __name__ == "__main__":
    main()
