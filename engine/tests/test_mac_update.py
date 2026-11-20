"""The Mac app updating itself (qbx/mac_update.py). macOS's own tools (codesign, ditto)
are replaced here; the swap script itself runs wherever there's a /bin/sh (the iMac)."""

import hashlib
import json
import plistlib
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from qbx import mac_update


def _fake_app(folder: Path, version: str, bundle_id: str = "com.classark.exambuilder") -> Path:
    app = folder / "Classark Exam Builder.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version})
    )
    (app / "Contents" / "MacOS" / "Classark Exam Builder").write_text(f"app {version}", encoding="utf-8")
    return app


def _release(folder: Path, version: str, **feed_changes) -> tuple[str, dict]:
    """A published release in `folder`: the zipped app and its feed, as package.py writes them."""
    built = folder / "built"
    app = _fake_app(built, version)
    archive = folder / f"ClassarkExamBuilder-{version}-mac-x86_64.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in app.rglob("*"):
            z.write(path, path.relative_to(built).as_posix())
    data = archive.read_bytes()
    feed = {"version": version, "file": archive.name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    feed.update(feed_changes)
    (folder / "mac-x86_64.json").write_text(json.dumps(feed), encoding="utf-8")
    return (folder / "mac-x86_64.json").as_uri(), feed


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A release copy at 1.0.0 in a writable Applications folder, with its own data folder."""
    applications = tmp_path / "Applications"
    applications.mkdir()
    current = _fake_app(applications, "1.0.0")
    monkeypatch.setattr(mac_update, "running_app", lambda: current)
    monkeypatch.setattr(mac_update.legal, "version", lambda: "1.0.0")
    monkeypatch.setattr(mac_update, "arch", lambda: "x86_64")
    monkeypatch.setenv("QBX_UPDATES_DIR", str(tmp_path / "data" / "updates"))

    def unpack(archive, to):
        with zipfile.ZipFile(archive) as z:
            z.extractall(to)

    monkeypatch.setattr(mac_update, "_unpack", unpack)
    monkeypatch.setattr(mac_update, "verify_app", lambda new, cur, version: None)
    monkeypatch.setattr(mac_update, "_ready_app", None)
    monkeypatch.setattr(mac_update, "_state", {"status": "idle", "version": None, "done": 0, "total": 0, "error": None, "checked_at": 0.0})
    return current


def test_a_newer_version_is_downloaded_checked_and_made_ready(mac, tmp_path, monkeypatch):
    feed_url, feed = _release(tmp_path / "release", "1.0.1")
    monkeypatch.setenv("QBX_MAC_FEED", feed_url)
    mac_update.check_now()
    state = mac_update._state
    assert state["status"] == "ready" and state["version"] == "1.0.1"
    staged = tmp_path / "data" / "updates" / "1.0.1" / "Classark Exam Builder.app"
    assert (staged / "Contents" / "MacOS" / "Classark Exam Builder").read_text(encoding="utf-8") == "app 1.0.1"
    # the zip is gone once unpacked; the running copy isn't touched until the restart
    assert not list((tmp_path / "data" / "updates" / "1.0.1").glob("*.zip"))
    assert (mac / "Contents" / "MacOS" / "Classark Exam Builder").read_text(encoding="utf-8") == "app 1.0.0"


def test_the_same_version_is_nothing_to_do(mac, tmp_path, monkeypatch):
    feed_url, _ = _release(tmp_path / "release", "1.0.0")
    monkeypatch.setenv("QBX_MAC_FEED", feed_url)
    mac_update.check_now()
    assert mac_update._state["status"] == "idle"


@pytest.mark.parametrize(
    "change",
    [{"sha256": "0" * 64}, {"size": 12}],
    ids=["wrong checksum", "wrong size"],
)
def test_a_download_that_isnt_the_feeds_is_refused(mac, tmp_path, monkeypatch, change):
    feed_url, _ = _release(tmp_path / "release", "1.0.1", **change)
    monkeypatch.setenv("QBX_MAC_FEED", feed_url)
    mac_update.check_now()
    assert mac_update._state["status"] == "failed"
    assert mac_update._ready_app is None


@pytest.mark.parametrize("name", ["../evil.zip", "sub/app.zip", "app.dmg"])
def test_a_feed_naming_an_odd_file_is_refused(mac, tmp_path, monkeypatch, name):
    feed_url, _ = _release(tmp_path / "release", "1.0.1", file=name)
    monkeypatch.setenv("QBX_MAC_FEED", feed_url)
    mac_update.check_now()
    assert mac_update._state["status"] == "failed"


def test_an_app_that_fails_its_checks_is_never_made_ready(mac, tmp_path, monkeypatch):
    def refuse(new, current, version):
        raise mac_update.UpdateError("The update isn't signed by Classark.")

    monkeypatch.setattr(mac_update, "verify_app", refuse)
    feed_url, _ = _release(tmp_path / "release", "1.0.1")
    monkeypatch.setenv("QBX_MAC_FEED", feed_url)
    mac_update.check_now()
    assert mac_update._state == {**mac_update._state, "status": "failed", "error": "The update isn't signed by Classark."}
    with pytest.raises(mac_update.UpdateError):
        mac_update.install()


def test_an_app_it_cant_replace_is_left_to_the_download_link(mac, tmp_path, monkeypatch):
    monkeypatch.setattr(mac_update, "_blocked", lambda app: "This Mac doesn't let the app replace itself - download the update instead.")
    feed_url, _ = _release(tmp_path / "release", "1.0.1")
    monkeypatch.setenv("QBX_MAC_FEED", feed_url)
    mac_update.check_now()
    assert mac_update._state["status"] == "manual"
    assert not (tmp_path / "data" / "updates").exists()


def test_running_from_the_dmg_or_translocated_cant_update_itself(tmp_path):
    assert mac_update._blocked(Path("/Volumes/Classark Exam Builder/Classark Exam Builder.app"))
    assert mac_update._blocked(Path("/private/var/folders/x/AppTranslocation/ABC/d/Classark Exam Builder.app"))


def test_the_feed_is_the_rolled_out_versions_on_r2(monkeypatch):
    monkeypatch.delenv("QBX_MAC_FEED", raising=False)
    monkeypatch.delenv("QBX_MAC_RELEASES", raising=False)
    monkeypatch.setattr(mac_update.legal, "version", lambda: "1.0.0")
    monkeypatch.setattr(mac_update, "arch", lambda: "arm64")
    monkeypatch.setattr(mac_update.app_version, "status", lambda: {"mac_version": "1.0.1"})
    assert mac_update.feed_url() == "https://downloads.classark.com/exam-builder/mac/1.0.1/mac-arm64.json"
    # nothing rolled out, or this copy already has it: nothing to fetch
    for rolled in (None, "", "1.0.0", "0.9.0"):
        monkeypatch.setattr(mac_update.app_version, "status", lambda rolled=rolled: {"mac_version": rolled})
        assert mac_update.feed_url() == ""
    monkeypatch.setenv("QBX_MAC_FEED", "")
    assert mac_update.feed_url() == ""


def test_a_feed_for_another_version_than_the_one_rolled_out_is_refused(mac, tmp_path, monkeypatch):
    release = tmp_path / "release" / "1.0.1"
    _release(release, "1.0.2")
    monkeypatch.delenv("QBX_MAC_FEED", raising=False)
    monkeypatch.setenv("QBX_MAC_RELEASES", (tmp_path / "release").as_uri())
    monkeypatch.setattr(mac_update.app_version, "status", lambda: {"mac_version": "1.0.1"})
    mac_update.check_now()
    assert mac_update._state["status"] == "failed"
    assert "1.0.2" in mac_update._state["error"]


def test_nothing_rolled_out_means_no_check(mac, monkeypatch):
    monkeypatch.delenv("QBX_MAC_FEED", raising=False)
    monkeypatch.setattr(mac_update.app_version, "status", lambda: {"mac_version": None})
    mac_update.check_now()
    assert mac_update._state["status"] == "idle"


def test_only_a_release_app_bundle_updates_itself(monkeypatch):
    monkeypatch.setattr(mac_update, "running_app", lambda: None)
    assert mac_update.status() is None  # Windows, or running from source
    monkeypatch.setattr(mac_update, "running_app", lambda: Path("/Applications/Classark Exam Builder.app"))
    monkeypatch.setattr(mac_update.edition, "edition", lambda: "debug")
    assert mac_update.status() is None


# -- the signature rules ------------------------------------------------------------------


@pytest.fixture
def signed(tmp_path, monkeypatch):
    current = _fake_app(tmp_path / "current", "1.0.0")
    new = _fake_app(tmp_path / "new", "1.0.1")
    teams = {current: "D9KRDVG3LS", new: "D9KRDVG3LS"}
    monkeypatch.setattr(mac_update, "_team", lambda app: teams.get(app))
    monkeypatch.setattr(
        mac_update.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    return current, new, teams


def test_an_intact_app_from_the_same_team_passes(signed):
    current, new, _ = signed
    mac_update.verify_app(new, current, "1.0.1")


def test_another_teams_app_is_refused(signed):
    current, new, teams = signed
    teams[new] = "SOMEONEELS"
    with pytest.raises(mac_update.UpdateError, match="isn't signed by Classark"):
        mac_update.verify_app(new, current, "1.0.1")


def test_an_unsigned_copy_never_updates_itself(signed):
    current, new, teams = signed
    teams[current] = None
    with pytest.raises(mac_update.UpdateError, match="isn't signed"):
        mac_update.verify_app(new, current, "1.0.1")


def test_another_app_or_version_is_refused(signed, tmp_path):
    current, new, _ = signed
    with pytest.raises(mac_update.UpdateError, match="version"):
        mac_update.verify_app(new, current, "1.0.2")
    other = _fake_app(tmp_path / "other", "1.0.1", bundle_id="com.example.other")
    with pytest.raises(mac_update.UpdateError, match="different app"):
        mac_update.verify_app(other, current, "1.0.1")


def test_a_broken_signature_is_refused(signed, monkeypatch):
    current, new, _ = signed
    monkeypatch.setattr(mac_update.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "invalid"))
    with pytest.raises(mac_update.UpdateError, match="broken"):
        mac_update.verify_app(new, current, "1.0.1")


# -- the swap (a real shell: runs on the Mac) ---------------------------------------------


@pytest.mark.skipif(sys.platform == "win32" or not Path("/bin/sh").exists(), reason="needs /bin/sh")
def test_the_swap_replaces_only_the_app_and_never_the_teachers_data(tmp_path):
    applications = tmp_path / "Applications"
    applications.mkdir()
    app = _fake_app(applications, "1.0.0")
    data = tmp_path / "Application Support" / "ClassarkExamBuilder"
    for name in ("papers/0620_s24_qp_41.pdf", "out/0620_41_MJ_24/paper.json", "quizzes/Exam 1.json", "config/settings.json"):
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_text(name, encoding="utf-8")
    before = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    updates = data / "updates"
    new = _fake_app(updates / "1.0.1", "1.0.1")
    script = tmp_path / "install.sh"
    script.write_text(mac_update.SWAP_SCRIPT.replace('open "$app"', 'echo opened "$app"'), encoding="utf-8")
    waiting = subprocess.Popen(["sleep", "1"])  # the app, still closing
    # collected as soon as it ends, as macOS's launchd does the real app - an uncollected
    # child stays a zombie, which `kill -0` still finds
    threading.Thread(target=waiting.wait, daemon=True).start()
    subprocess.run(["/bin/sh", str(script), str(waiting.pid), str(app), str(new), str(updates / "previous.app")], check=True, timeout=30)
    waiting.wait()

    assert (app / "Contents" / "MacOS" / "Classark Exam Builder").read_text(encoding="utf-8") == "app 1.0.1"
    assert not (updates / "previous.app").exists()
    after = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file() and updates not in p.parents}
    assert after == {k: v for k, v in before.items()}


@pytest.mark.skipif(sys.platform == "win32" or not Path("/bin/sh").exists(), reason="needs /bin/sh")
def test_a_failed_swap_puts_the_old_app_back(tmp_path):
    applications = tmp_path / "Applications"
    applications.mkdir()
    app = _fake_app(applications, "1.0.0")
    script = tmp_path / "install.sh"
    script.write_text(mac_update.SWAP_SCRIPT.replace('open "$app"', 'echo opened "$app"'), encoding="utf-8")
    missing = tmp_path / "updates" / "1.0.1" / "Classark Exam Builder.app"  # nothing to move in
    subprocess.run(["/bin/sh", str(script), "999999", str(app), str(missing), str(tmp_path / "previous.app")], check=True, timeout=30)
    assert (app / "Contents" / "MacOS" / "Classark Exam Builder").read_text(encoding="utf-8") == "app 1.0.0"
