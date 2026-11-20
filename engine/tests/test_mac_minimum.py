"""package.py's check that every compiled file in the Mac app runs on the macOS the app
says it needs (MAC_MINIMUM) - built on GitHub's macOS 26 machines, a file compiled there
would otherwise silently need macOS 26 (user-asked)."""

import importlib.util
import struct
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("package", Path(__file__).resolve().parents[2] / "package.py")
package = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(package)


def _thin(major: int, minor: int, old_style: bool = False) -> bytes:
    """A 64-bit Mach-O image with one version load command."""
    packed = (major << 16) | (minor << 8)
    if old_style:  # LC_VERSION_MIN_MACOSX {cmd, size, version, sdk}
        command = struct.pack("<IIII", 0x24, 16, packed, packed)
    else:  # LC_BUILD_VERSION {cmd, size, platform, minos, sdk, ntools}
        command = struct.pack("<IIIIII", 0x32, 24, 1, packed, packed, 0)
    other = struct.pack("<II", 0x19, 8)  # an unrelated command before it
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 6, 2, len(other) + len(command), 0, 0)
    return header + other + command


def _fat(*images: bytes) -> bytes:
    head = struct.pack(">II", 0xCAFEBABE, len(images))
    offset = 4096
    table, body = b"", b""
    for image in images:
        table += struct.pack(">IIIII", 0, 0, offset + len(body), len(image), 12)
        body += image
    return (head + table).ljust(offset, b"\0") + body


def test_reads_the_stamp_from_each_kind_of_file(tmp_path):
    cases = {
        "new": (_thin(11, 0), (11, 0)),
        "old_style": (_thin(10, 13, old_style=True), (10, 13)),
        "universal": (_fat(_thin(10, 15), _thin(11, 0)), (11, 0)),
        "text.py": (b"print('hello')\n", None),
    }
    for name, (data, expected) in cases.items():
        (tmp_path / name).write_bytes(data)
        assert package.mach_o_minimum(tmp_path / name) == expected, name


def test_an_app_with_a_file_for_a_newer_macos_stops_the_build(tmp_path):
    app = tmp_path / "Classark Exam Builder.app"
    (app / "Contents" / "Frameworks").mkdir(parents=True)
    (app / "Contents" / "MacOS").mkdir()
    (app / "Contents" / "MacOS" / "Classark Exam Builder").write_bytes(_thin(11, 0))
    (app / "Contents" / "Frameworks" / "fine.so").write_bytes(_fat(_thin(10, 15), _thin(11, 0)))
    package.check_mac_minimum(app)  # all macOS 11 or older: carries on

    (app / "Contents" / "Frameworks" / "compiled_here.so").write_bytes(_thin(26, 0))
    with pytest.raises(SystemExit, match=r"compiled_here\.so: macOS 26\.0"):
        package.check_mac_minimum(app)
