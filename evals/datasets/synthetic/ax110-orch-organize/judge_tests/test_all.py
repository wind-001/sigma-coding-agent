import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'dump'
EXPECTED_SUBDIR = {"note-a.txt": "txt", "note-b.txt": "txt", "readme.txt": "txt", "table-1.csv": "csv", "table-2.csv": "csv", "empty.csv": "csv", "run-1.log": "log", "run-2.log": "log", "trace.log": "log", "page-a.txt": "txt", "table-3.csv": "csv", "run-3.log": "log"}


def test_all_files_moved() -> None:
    for name, sub in EXPECTED_SUBDIR.items():
        assert (ROOT / sub / name).is_file(), f'{sub}/{name} 缺失'


def test_manifest() -> None:
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest == EXPECTED_SUBDIR


def test_root_has_no_files() -> None:
    leftovers = [p.name for p in ROOT.iterdir() if p.is_file() and p.name != 'manifest.json']
    assert leftovers == [], f'根目录残留文件: {leftovers}'


def test_content_preserved() -> None:
    EXPECTED_HASHES = {"note-a.txt": "c3edc681784096a743f52d254a9edf44c69a38fcc9b2d6c00e042bef87ae8f84", "note-b.txt": "9bb7b4aa4e0d5fbcdc1964b9db66ab4bf9eddb99c75be7ad22b8ec57219d0db7", "readme.txt": "93117c503d4362341c8aba6466398401c30ed4962f2cdeabe33b29d5234b4e1b", "table-1.csv": "007de5b231eb394c0bbcc5d8032adb639d0c1d248415f56ee595f674a1d07764", "table-2.csv": "19beb51bb1f5b909cfdd381ff927ab6d4cde02fa80d8fcd87d93226e7b17ebf4", "empty.csv": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "run-1.log": "17b5a6c0bc419a71e727ec52af68096944d7baabf42e779ce51be31c02df4bfe", "run-2.log": "25b418ea5624e816123252ced55c97b3430a498563bc1882edd09699cefe31e9", "trace.log": "9706b025a3d70e644e71dd7f449ddcbc41c08ebdeb52f1771f0357f3172c25c7", "page-a.txt": "3ec688f54dadef8ea64f1ece64eeb9befe77f4841ed641276f749a1b8f7d7f14", "table-3.csv": "acee714c5fd0e79b59e87adf6429eb07231b85b46c88e2886154ff89de8e46b7", "run-3.log": "c7495439aade58b2c7fd7cf8041fe0b2a5e38ed7aff04fdc165e9c4365058db4"}
    for name, sub in EXPECTED_SUBDIR.items():
        data = (ROOT / sub / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == EXPECTED_HASHES[name], name
