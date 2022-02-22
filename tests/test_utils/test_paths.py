# ─── DEPENDENCIES ─────────────────────────────────────────────────────────────

from re import I
import shutil
import pytest
from pykanto.utils.paths import ProjDirs, get_wavs_w_annotation
from pathlib import Path

# ──── FIXTURES ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def test_dir():
    return Path(__file__)


@pytest.fixture()
def wav_filepaths():
    wl = [(Path('a') / f'{i}.wav') for i in (1, 2)]
    return wl


@pytest.fixture()
def annotation_paths():
    al = [(Path('a') / f'{i}.annotation') for i in (1, 2)]
    return al

# ──── TESTS ────────────────────────────────────────────────────────────────────


def test_ProjDirs(test_dir):
    DIRS = ProjDirs(test_dir.parent, test_dir.parent, mkdir=True)
    assert DIRS.FIGURES.is_dir()
    for path in [DIRS.DATA, DIRS.REPORTS, DIRS.RESOURCES]:
        shutil.rmtree(str(path))


def test_ProjDirs_append(test_dir):
    DIRS = ProjDirs(test_dir.parent, test_dir.parent)
    DIRS.append('TEST', test_dir)
    assert hasattr(DIRS, 'TEST')


def test_get_wavs_w_annotation(wav_filepaths, annotation_paths):
    r = get_wavs_w_annotation(wav_filepaths, annotation_paths)
    assert len(r) == 2
    assert r[0][0].suffix == '.wav'