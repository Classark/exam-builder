import os

# the app's papers-folder watcher (main._start_watching) never runs under the tests
os.environ.setdefault("QBX_WATCH_PAPERS", "0")
# and nothing asks Classark's config for the minimum version (qbx/app_version.py)
os.environ.setdefault("QBX_CONFIG_URL", "")
