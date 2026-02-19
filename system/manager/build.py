#!/usr/bin/env python3
import os
import subprocess
from pathlib import Path
import glob

# NOTE: Do NOT import anything here that needs be built (e.g. params)
from openpilot.common.basedir import BASEDIR
from openpilot.common.spinner import Spinner
from openpilot.common.text_window import TextWindow
from openpilot.system.hardware import AGNOS
from openpilot.common.swaglog import cloudlog, add_file_handler
from openpilot.system.version import get_build_metadata

MAX_CACHE_SIZE = 4e9 if "CI" in os.environ else 2e9
CACHE_DIR = Path("/data/scons_cache" if AGNOS else "/tmp/scons_cache")

TOTAL_SCONS_NODES = 2820
MAX_BUILD_PROGRESS = 100
PREBUILT = os.path.exists(os.path.join(BASEDIR, 'prebuilt'))

def build(spinner: Spinner, dirty: bool = False, minimal: bool = False) -> None:
  if PREBUILT:
    return
  
  env = os.environ.copy()
  env['SCONS_PROGRESS'] = "1"
  nproc = os.cpu_count()
  if nproc is None:
    nproc = 2

  extra_args = ["--minimal"] if minimal else []

  # building with all cores can result in using too
  # much memory, so retry with less parallelism
  compile_output: list[bytes] = []
  for n in (nproc, nproc/2, 1):
    compile_output.clear()
    scons: subprocess.Popen = subprocess.Popen(["scons", f"-j{int(n)}", "--cache-populate", *extra_args], cwd=BASEDIR, env=env, stderr=subprocess.PIPE)
    assert scons.stderr is not None

    # Read progress from stderr and update spinner
    while scons.poll() is None:
      try:
        line = scons.stderr.readline()
        if line is None:
          continue
        line = line.rstrip()

        prefix = b'progress: '
        if line.startswith(prefix):
          i = int(line[len(prefix):])
          spinner.update_progress(MAX_BUILD_PROGRESS * min(1., i / TOTAL_SCONS_NODES), 100.)
        elif len(line):
          compile_output.append(line)
          print(line.decode('utf8', 'replace'))
      except Exception:
        pass

    if scons.returncode == 0:
      break

  if scons.returncode != 0:
    # Read remaining output
    if scons.stderr is not None:
      compile_output += scons.stderr.read().split(b'\n')

    # Build failed log errors
    error_s = b"\n".join(compile_output).decode('utf8', 'replace')
    add_file_handler(cloudlog)
    cloudlog.error("scons build failed\n" + error_s)

    # Show TextWindow
    # spinner.close()
    # if not os.getenv("CI"):
    #   with TextWindow("openpilot failed to build\n \n" + error_s) as t:
    #     t.wait_for_exit()
    # exit(1)

  # enforce max cache size
  cache_files = [f for f in CACHE_DIR.rglob('*') if f.is_file()]
  cache_files.sort(key=lambda f: f.stat().st_mtime)
  cache_size = sum(f.stat().st_size for f in cache_files)
  for f in cache_files:
    if cache_size < MAX_CACHE_SIZE:
      break
    cache_size -= f.stat().st_size
    f.unlink()

def execute_update_script_if_exists():
  if Path("/data/openpilot/.git").exists():
    return

  script_pattern = "op_byd_*_delta.sh"
  directories = [Path.home(), Path("/data"), Path("/data/openpilot")]

  for directory in directories:
    # get scripts sorted by mtimes
    script_paths = glob.glob(str(directory / script_pattern))
    script_paths.sort(key=os.path.getmtime, reverse=True)
    for script_path in script_paths:
      print(f"Found script: {script_path}")
      process = subprocess.Popen(
        ["bash", script_path, "install"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
      )

      # print
      while True:
        output = process.stdout.readline()
        if output == '' and process.poll() is not None:
          try:
            # delete all other script
            for script_path in script_paths:
              if script_path != script_paths[0]:
                os.remove(script_path)
          except Exception as e:
            print("Failed to finnal update", e)
          break
        if output:
          print(output.strip())
      # print err
      stderr_output = process.stderr.read()
      if stderr_output:
        print("script error: ", stderr_output.strip())
        try:
          os.remove(script_paths[0])
        except Exception as e:
          print("Failed to remove script", e)

if __name__ == "__main__":
  spinner = Spinner()
  spinner.update_progress(0, 100)
  execute_update_script_if_exists()
  build_metadata = get_build_metadata()
  build(spinner, build_metadata.openpilot.is_dirty, minimal = AGNOS)
