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

"""binary_cache_builder.py: read perf.data, collect binaries needed by
    it, and put them in binary_cache.
"""

from collections import defaultdict
import logging
import os
import os.path
from pathlib import Path
import shutil
import sys
from typing import Dict, List, Optional, Tuple, Union

from simpleperf_report_lib import ReportLib
from simpleperf_utils import (
    AdbHelper, BaseArgumentParser, extant_dir, extant_file, flatten_arg_list,
    ReadElf, str_to_bytes)


def is_jit_symfile(dso_name):
    return dso_name.split('/')[-1].startswith('TemporaryFile')


class BinaryCache:
    def __init__(self, binary_dir: Path):
        self.binary_dir = binary_dir

    def get_path_in_cache(self, path: str) -> Path:
        """ Given a binary path in perf.data, return its corresponding path in the cache. """
        if path.startswith('/'):
            path = path[1:]
        path = path.replace('/', os.sep)
        return Path(os.path.join(self.binary_dir, path))


class BinarySource:
    """ Source to find debug binaries. """

    def __init__(self, readelf: ReadElf):
        self.readelf = readelf

    def collect_binaries(self, binaries: Dict[str, str], binary_cache: BinaryCache):
        """ pull binaries needed in perf.data to binary_cache.
            binaries: maps from binary path to its build_id in perf.data.
        """
        raise Exception('not implemented')

    def read_build_id(self, path: Path):
        return self.readelf.get_build_id(path)


class BinarySourceFromDevice(BinarySource):
    """ Pull binaries from device. """

    def __init__(self, readelf: ReadElf, disable_adb_root: bool):
        super().__init__(readelf)
        self.adb = AdbHelper(enable_switch_to_root=not disable_adb_root)

    def collect_binaries(self, binaries: Dict[str, str], binary_cache: BinaryCache):
        if not self.adb.is_device_available():
            return
        for path, build_id in binaries.items():
            self.collect_binary(path, build_id, binary_cache)
        self.pull_kernel_symbols(binary_cache.binary_dir / 'kallsyms')

    def collect_binary(self, path: str, build_id: str, binary_cache: BinaryCache):
        if not path.startswith('/') or path == "//anon" or path.startswith("/dev/"):
            # [kernel.kallsyms] or unknown, or something we can't find binary.
            return
        binary_cache_file = binary_cache.get_path_in_cache(path)
        self.check_and_pull_binary(path, build_id, binary_cache_file)

    def check_and_pull_binary(self, path: str, expected_build_id: str, binary_cache_file: Path):
        """If the binary_cache_file exists and has the expected_build_id, there
           is no need to pull the binary from device. Otherwise, pull it.
        """
        if binary_cache_file.is_file() and (
                not expected_build_id or expected_build_id == self.read_build_id(binary_cache_file)
        ):
            logging.info('use current file in binary_cache: %s', binary_cache_file)
        else:
            logging.info('pull file to binary_cache: %s to %s', path, binary_cache_file)
            target_dir = binary_cache_file.parent
            if not target_dir.is_dir():
                os.makedirs(target_dir)
            if binary_cache_file.is_file():
                binary_cache_file.unlink()
            self.pull_file_from_device(path, binary_cache_file)

    def pull_file_from_device(self, device_path: str, host_path: Path):
        if self.adb.run(['pull', device_path, str(host_path)]):
            return True
        # On non-root devices, we can't pull /data/app/XXX/base.odex directly.
        # Instead, we can first copy the file to /data/local/tmp, then pull it.
        filename = device_path[device_path.rfind('/')+1:]
        if (self.adb.run(['shell', 'cp', device_path, '/data/local/tmp']) and
                self.adb.run(['pull', '/data/local/tmp/' + filename, host_path])):
            self.adb.run(['shell', 'rm', '/data/local/tmp/' + filename])
            return True
        logging.warning('failed to pull %s from device', device_path)
        return False

    def pull_kernel_symbols(self, file_path: Path):
        if file_path.is_file():
            file_path.unlink()
        if self.adb.switch_to_root():
            self.adb.run(['shell', 'echo', '0', '>/proc/sys/kernel/kptr_restrict'])
            self.adb.run(['pull', '/proc/kallsyms', file_path])


class BinarySourceFromLibDirs(BinarySource):
    """ Collect binaries from lib dirs.

        It is possible that the path of the binary in lib dir doesn't match
        the one recorded in perf.data. For example, a file in lib dir might
        be "debug/arm/obj/armeabi-v7a/libsudo-game-jni.so", but the path in
        perf.data is "/data/app/xxxx/lib/arm/libsudo-game-jni.so". So we match
        binaries if they have the same filename (like libsudo-game-jni.so)
        and same build_id.
    """

    def __init__(self, readelf: ReadElf, lib_dirs: List[Path]):
        super().__init__(readelf)
        self.lib_dirs = lib_dirs

    def collect_binaries(self, binaries: Dict[str, str], binary_cache: BinaryCache):
        filename_dict = self.build_filename_dict(binaries)

        # Search all files in lib_dirs, and copy matching files to build_cache.
        for lib_dir in self.lib_dirs:
            self.search_dir(lib_dir, filename_dict, binary_cache)

    def build_filename_dict(self, binaries: Dict[str, str]) -> Dict[str, List[Tuple[str, str]]]:
        """ Return a dict mapping from filename to binary info having the filename """
        filename_dict: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for path, build_id in binaries.items():
            index = path.rfind('/')
            filename = path[index + 1:]
            filename_dict[filename].append((path, build_id))
        return filename_dict

    def search_dir(
            self, lib_dir: Path, filename_dict: Dict[str, List[str]],
            binary_cache: BinaryCache):
        for root, _, files in os.walk(lib_dir):
            for filename in files:
                binaries = filename_dict.get(filename)
                if not binaries:
                    continue
                file_path = Path(os.path.join(root, filename))
                build_id = self.read_build_id(file_path)
                for path, expected_build_id in binaries:
                    if expected_build_id == build_id:
                        to_path = binary_cache.get_path_in_cache(path)
                        self.copy_to_binary_cache(file_path, expected_build_id, to_path)

    def copy_to_binary_cache(
            self, from_path: Path, expected_build_id: str, to_path: Path):
        if not self.need_to_copy(from_path, to_path, expected_build_id):
            # The existing file in binary_cache can provide more information, so no need to copy.
            return
        to_dir = to_path.parent
        if not to_dir.is_dir():
            os.makedirs(to_dir)
        logging.info('copy to binary_cache: %s to %s', from_path, to_path)
        shutil.copy(from_path, to_path)

    def need_to_copy(self, from_path: Path, to_path: Path, expected_build_id: str):
        if not to_path.is_file() or self.read_build_id(to_path) != expected_build_id:
            return True
        return self.get_file_stripped_level(from_path) < self.get_file_stripped_level(to_path)

    def get_file_stripped_level(self, path: Path) -> int:
        """Return stripped level of an ELF file. Larger value means more stripped."""
        sections = self.readelf.get_sections(path)
        if '.debug_line' in sections:
            return 0
        if '.symtab' in sections:
            return 1
        return 2


class BinaryCacheBuilder(object):
    """Collect all binaries needed by perf.data in binary_cache."""

    def __init__(self, ndk_path: Optional[str], disable_adb_root: bool):
        self.readelf = ReadElf(ndk_path)
        self.device_source = BinarySourceFromDevice(self.readelf, disable_adb_root)
        self.binary_cache_dir = Path('binary_cache')
        self.binary_cache = BinaryCache(self.binary_cache_dir)
        self.binaries = {}

    def build_binary_cache(self, perf_data_path: str, symfs_dirs: List[Union[Path, str]]) -> bool:
        self.collect_used_binaries(perf_data_path)
        if not self.copy_binaries_from_symfs_dirs(symfs_dirs):
            return False
        self.pull_binaries_from_device()
        self.create_build_id_list()
        return True

    def collect_used_binaries(self, perf_data_path):
        """read perf.data, collect all used binaries and their build id(if available)."""
        # A dict mapping from binary name to build_id
        binaries = {}
        lib = ReportLib()
        lib.SetRecordFile(perf_data_path)
        lib.SetLogSeverity('error')
        while True:
            sample = lib.GetNextSample()
            if sample is None:
                lib.Close()
                break
            symbols = [lib.GetSymbolOfCurrentSample()]
            callchain = lib.GetCallChainOfCurrentSample()
            for i in range(callchain.nr):
                symbols.append(callchain.entries[i].symbol)

            for symbol in symbols:
                dso_name = symbol.dso_name
                if dso_name not in binaries:
                    if is_jit_symfile(dso_name):
                        continue
                    name = 'vmlinux' if dso_name == '[kernel.kallsyms]' else dso_name
                    binaries[name] = lib.GetBuildIdForPath(dso_name)
        self.binaries = binaries

    def copy_binaries_from_symfs_dirs(self, symfs_dirs: List[Union[str, Path]]) -> bool:
        if symfs_dirs:
            lib_dirs: List[Path] = []
            for symfs_dir in symfs_dirs:
                if isinstance(symfs_dir, str):
                    symfs_dir = Path(symfs_dir)
                if not symfs_dir.is_dir():
                    logging.error("can't find dir %s", symfs_dir)
                    return False
                lib_dirs.append(symfs_dir)
            lib_dir_source = BinarySourceFromLibDirs(self.readelf, lib_dirs)
            lib_dir_source.collect_binaries(self.binaries, self.binary_cache)
        return True

    def pull_binaries_from_device(self):
        self.device_source.collect_binaries(self.binaries, self.binary_cache)

    def create_build_id_list(self):
        """ Create build_id_list. So report scripts can find a binary by its build_id instead of
            path.
        """
        build_id_list_path = self.binary_cache_dir / 'build_id_list'
        # Write in binary mode to avoid "\r\n" problem on windows, which can confuse simpleperf.
        with open(build_id_list_path, 'wb') as fh:
            for root, _, files in os.walk(self.binary_cache_dir):
                for filename in files:
                    path = Path(os.path.join(root, filename))
                    build_id = self.readelf.get_build_id(path)
                    if build_id:
                        relative_path = path.relative_to(self.binary_cache_dir)
                        line = f'{build_id}={relative_path}\n'
                        fh.write(str_to_bytes(line))


def main() -> bool:
    parser = BaseArgumentParser(description="""
        Pull binaries needed by perf.data from device to binary_cache directory.""")
    parser.add_argument('-i', '--perf_data_path', default='perf.data', type=extant_file, help="""
        The path of profiling data.""")
    parser.add_argument('-lib', '--native_lib_dir', type=extant_dir, nargs='+', help="""
        Path to find debug version of native shared libraries used in the app.""", action='append')
    parser.add_argument('--disable_adb_root', action='store_true', help="""
        Force adb to run in non root mode.""")
    parser.add_argument('--ndk_path', nargs=1, help='Find tools in the ndk path.')
    args = parser.parse_args()
    ndk_path = None if not args.ndk_path else args.ndk_path[0]
    builder = BinaryCacheBuilder(ndk_path, args.disable_adb_root)
    symfs_dirs = flatten_arg_list(args.native_lib_dir)
    return builder.build_binary_cache(args.perf_data_path, symfs_dirs)


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
