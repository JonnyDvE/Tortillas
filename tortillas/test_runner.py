from __future__ import annotations
from typing import Callable

import sys
import time
import pathlib
import threading
import subprocess

from utils import get_logger
from constants import TestStatus, QEMU_VMSTATE_TAG
from tortillas_config import TortillasConfig
from test import Test, TestResult
from log_parser import LogParser
from progress_bar import ProgressBar
from qemu_interface import QemuInterface, InterruptWatchdog


def run_tests(tests: list[Test], architecture: str, progress_bar: ProgressBar,
              config: TortillasConfig):
    test_queue = [test for test in tests]
    running_tests: dict[str, threading.Thread] = {}

    progress_bar.create_run_tests_counters(len(tests))
    lock = threading.Lock()

    def thread_callback(test: Test):
        with lock:
            running_tests.pop(repr(test))

            if test.result.retry:
                test_logger = get_logger(repr(test), prefix=True)
                if test.result.panic:
                    panic = ''.join(test.result.errors)
                    test_logger.info(f'Restarting test, because of {panic}')

                test.result = TestResult(test.name)
                progress_bar.update_counter(
                        progress_bar.Counter.RUNNING, incr=-1)
                test_queue.append(test)

                return

            counter_type = progress_bar.Counter.FAIL
            if (test.result.status == TestStatus.SUCCESS):
                counter_type = progress_bar.Counter.SUCCESS

            progress_bar.update_counter(counter_type,
                                        progress_bar.Counter.RUNNING)

    def run_test(test_queue: list[Test]):
        with lock:
            test = test_queue.pop()
            progress_bar.update_counter(progress_bar.Counter.RUNNING)

            thread = threading.Thread(target=_run, args=[
                                      test, architecture, config,
                                      thread_callback])

            running_tests[repr(test)] = thread
            thread.start()

    # Run all the tests
    for _ in range(config.threads):
        if test_queue:
            run_test(test_queue)

    while test_queue or running_tests:
        progress_bar.refresh()

        if not test_queue:
            # Probably waiting for a test timeout -> wait longer.
            time.sleep(1)
            continue

        if len(running_tests) < config.threads:
            run_test(test_queue)

        time.sleep(0.0001)  # Basically yield
    # Testing finished


def create_snapshot(architecture: str, label: str, config: TortillasConfig):
    log = get_logger('Create snapshot', prefix=True)

    log.info('Booting SWEB')
    return_reg = 'RAX'
    if (architecture == 'x86_32'):
        return_reg = 'EAX'

    tmp_dir = f'{config.test_run_directory}/snapshot'
    _clean_tmp_dir(tmp_dir)

    snapshot_qcow2_path = f'{tmp_dir}/SWEB.qcow2'

    subprocess.run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b',
                    f'{config.build_directory}/SWEB.qcow2',
                    snapshot_qcow2_path],
                   check=True,
                   stdout=subprocess.DEVNULL)

    bootup_error = False
    with QemuInterface(
            tmp_dir=tmp_dir,
            qcow2_path=snapshot_qcow2_path,
            arch=architecture,
            logger=log
            ) as qemu:

        if not qemu.is_alive():
            sys.exit(-1)

        log.debug('Waiting for bootup...')

        # Wait for the interrupt, that singals bootup completion
        res = qemu.interrupt_watchdog.wait_until(
                int_num='80',
                int_regs={
                    return_reg: config.sc_tortillas_bootup
                },
                timeout=config.bootup_timeout_secs
                )

        if (res == InterruptWatchdog.Status.TIMEOUT or
           res == InterruptWatchdog.Status.STOPPED):
            log.info('Boot attempt failed, dumping logfile!')
            bootup_error = True

        else:
            log.info('Successful bootup!')
            time.sleep(0.1)
            qemu.monitor_command(f'savevm {label}\n')

    if bootup_error:
        with open(f'{tmp_dir}/out.log', 'r') as log_file:
            log.info(log_file.read())
        sys.exit(-1)

    subprocess.run(['cp', snapshot_qcow2_path,
                    f'{config.test_run_directory}/SWEB-snapshot.qcow2'],
                   check=True)


def _clean_tmp_dir(tmp_dir):
    if pathlib.Path(tmp_dir).is_dir():
        subprocess.run(f'rm {tmp_dir}/*', shell=True)
    else:
        subprocess.run(['mkdir', tmp_dir], check=True)


def _run(test: Test, architecture: str, config: TortillasConfig,
         callback: Callable[['Test'], None] | None = None):
    log = test.logger

    return_reg = 'RAX'
    if (architecture == 'x86_32'):
        return_reg = 'EAX'

    tmp_dir = test.get_tmp_dir()
    _clean_tmp_dir(tmp_dir)

    log.debug(f'Copying SWEB-snapshot.qcow2 to {tmp_dir}')

    snapshot_path = f'{tmp_dir}/SWEB-snapshot.qcow2'
    subprocess.run(['cp', f'{config.test_run_directory}/SWEB-snapshot.qcow2',
                    snapshot_path], check=True)

    log.debug(
        f'Starting qemu snapshot {QEMU_VMSTATE_TAG} (arch={architecture})')

    with QemuInterface(
            tmp_dir=tmp_dir,
            qcow2_path=snapshot_path,
            arch=architecture,
            logger=log,
            vmstate=QEMU_VMSTATE_TAG
            ) as qemu:

        if not qemu.is_alive():
            test.result.retry = True
            if callback:
                callback(test)
            return

        log.info('Starting test execution')
        qemu.sweb_input(f'{test.name}.sweb\n')

        timeout = config.default_test_timeout_secs
        # Overwrite timeout if in test config
        if test.config.timeout:
            timeout = test.config.timeout

        # Wait for the interrupt, that signals program completion
        res = qemu.interrupt_watchdog.wait_until(
                int_num='80',
                int_regs={
                    return_reg: config.sc_tortillas_finished
                },
                timeout=timeout
                )

        if (res == InterruptWatchdog.Status.TIMEOUT
           and not test.config.expect_timeout):
            test.result.add_execution_error('Test execution timeout')

        if res == InterruptWatchdog.Status.STOPPED:
            test.result.add_execution_error('Test killed, because no more '
                                            'interrupts were comming')

        # Wait a bit for cleanup and debug output to be flushed
        time.sleep(1)

    parser = LogParser(test, config)
    parser.parse()

    test.result.analyze(parser.log_data)

    log.info('Done!')
    if callback:
        callback(test)
