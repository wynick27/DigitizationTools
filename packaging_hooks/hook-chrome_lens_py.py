from pathlib import Path

from PyInstaller.utils.hooks import collect_all, get_package_paths


datas, binaries, hiddenimports = collect_all("chrome_lens_py")

# chrome-lens-py adds this directory to sys.path because its generated protobuf
# files import one another as top-level modules. PyInstaller otherwise keeps the
# Python files only in its module archive, where those imports cannot find them.
_, package_dir = get_package_paths("chrome_lens_py")
protobuf_dir = Path(package_dir) / "utils" / "protobufs"
datas.extend(
    (str(source), "chrome_lens_py/utils/protobufs")
    for source in protobuf_dir.glob("*_pb2.py")
)
hiddenimports.append("google.protobuf.runtime_version")
