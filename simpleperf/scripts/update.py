#!/usr/bin/env python3
#
# Copyright (C) 2016 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Downloads simpleperf prebuilts from the build server."""
import argparse
import logging
import os
import shutil
import stat
import textwrap

THIS_DIR = os.path.realpath(os.path.dirname(__file__))


class InstallEntry(object):
    def __init__(self, target, name, install_path, need_strip=False):
        self.target = target
        self.name = name
        self.install_path = install_path
        self.need_strip = need_strip


INSTALL_LIST = [
    # simpleperf on device.
    InstallEntry('MODULES-IN-system-extras-simpleperf',
                 'simpleperf/android/arm64/simpleperf_ndk',
                 'android/arm64/simpleperf'),
    InstallEntry('MODULES-IN-system-extras-simpleperf_arm',
                 'simpleperf/android/arm/simpleperf_ndk32',
                 'android/arm/simpleperf'),
    InstallEntry('MODULES-IN-system-extras-simpleperf_x86',
                 'simpleperf/android/x86_64/simpleperf_ndk',
                 'android/x86_64/simpleperf'),
    InstallEntry('MODULES-IN-system-extras-simpleperf_x86',
                 'simpleperf/android/x86/simpleperf_ndk32',
                 'android/x86/simpleperf'),

    # simpleperf on host.
    InstallEntry('MODULES-IN-system-extras-simpleperf',
                 'simpleperf/linux/x86_64/simpleperf',
                 'linux/x86_64/simpleperf', True),
    InstallEntry('MODULES-IN-system-extras-simpleperf_mac',
                 'simpleperf/darwin/x86_64/simpleperf',
                 'darwin/x86_64/simpleperf'),

    # libsimpleperf_report.so on host
    InstallEntry('MODULES-IN-system-extras-simpleperf',
                 'simpleperf/linux/x86_64/libsimpleperf_report.so',
                 'linux/x86_64/libsimpleperf_report.so', True),
    InstallEntry('MODULES-IN-system-extras-simpleperf_mac',
                 'simpleperf/darwin/x86_64/libsimpleperf_report.dylib',
                 'darwin/x86_64/libsimpleperf_report.dylib'),
]


def logger():
    """Returns the main logger for this module."""
    return logging.getLogger(__name__)


def check_call(cmd):
    """Proxy for subprocess.check_call with logging."""
    import subprocess
    logger().debug('check_call `%s`', ' '.join(cmd))
    subprocess.check_call(cmd)


def fetch_artifact(branch, build, target, name):
    """Fetches and artifact from the build server."""
    if target.startswith('local:'):
        shutil.copyfile(target[6:], name)
        return
    logger().info('Fetching %s from %s %s (artifacts matching %s)', build,
                  target, branch, name)
    fetch_artifact_path = '/google/data/ro/projects/android/fetch_artifact'
    cmd = [fetch_artifact_path, '--branch', branch, '--target', target,
           '--bid', build, name]
    check_call(cmd)


def start_branch(build):
    """Creates a new branch in the project."""
    branch_name = 'update-' + (build or 'latest')
    logger().info('Creating branch %s', branch_name)
    check_call(['repo', 'start', branch_name, '.'])


def commit(branch, build, add_paths):
    """Commits the new prebuilts."""
    logger().info('Making commit')
    check_call(['git', 'add'] + add_paths)
    message = textwrap.dedent("""\
        simpleperf: update simpleperf prebuilts to build {build}.

        Taken from branch {branch}.""").format(branch=branch, build=build)
    check_call(['git', 'commit', '-m', message])


def remove_old_release(install_dir):
    """Removes the old prebuilts."""
    if os.path.exists(install_dir):
        logger().info('Removing old install directory "%s"', install_dir)
        check_call(['git', 'rm', '-rf', '--ignore-unmatch', install_dir])

    # Need to check again because git won't remove directories if they have
    # non-git files in them.
    if os.path.exists(install_dir):
        shutil.rmtree(install_dir)


def install_new_release(branch, build, install_dir):
    """Installs the new release."""
    for entry in INSTALL_LIST:
        install_entry(branch, build, install_dir, entry)


def install_entry(branch, build, install_dir, entry):
    """Installs the device specific components of the release."""
    target = entry.target
    name = entry.name
    install_path = os.path.join(install_dir, entry.install_path)
    need_strip = entry.need_strip

    fetch_artifact(branch, build, target, name)
    name = os.path.basename(name)
    exe_stat = os.stat(name)
    os.chmod(name, exe_stat.st_mode | stat.S_IEXEC)
    if need_strip:
        check_call(['strip', name])
    dirname = os.path.dirname(install_path)
    if not os.path.isdir(dirname):
        os.makedirs(dirname)
    shutil.move(name, install_path)


def get_args():
    """Parses and returns command line arguments."""
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-b', '--branch', default='aosp-simpleperf-release',
        help='Branch to pull build from.')
    parser.add_argument('--build', required=True, help='Build number to pull.')
    parser.add_argument(
        '--use-current-branch', action='store_true',
        help='Perform the update in the current branch. Do not repo start.')
    parser.add_argument(
        '-v', '--verbose', action='count', default=0,
        help='Increase output verbosity.')

    return parser.parse_args()


def main():
    """Program entry point."""
    os.chdir(THIS_DIR)

    args = get_args()
    verbose_map = (logging.WARNING, logging.INFO, logging.DEBUG)
    verbosity = args.verbose
    if verbosity > 2:
        verbosity = 2
    logging.basicConfig(level=verbose_map[verbosity])

    install_dir = 'bin'

    if not args.use_current_branch:
        start_branch(args.build)
    remove_old_release(install_dir)
    install_new_release(args.branch, args.build, install_dir)
    artifacts = [install_dir]
    commit(args.branch, args.build, artifacts)


if __name__ == '__main__':
    main()
