# ─── DESCRIPTION ──────────────────────────────────────────────────────────────

# This script does the following:
#  - Segments raw recordings into songs Using segmentation information in the
#    Sonic Visualiser format
#  - These are saved as new .wav files + .json files with metadata

# TODO:
# Convert 2020 metadata to same format as 2021 so that the below works for any
# year.


# %%──── LIBRARIES ─────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import os
import pickle
import shutil
import warnings
from pathlib import Path
from tabnanny import verbose
from typing import Any, Dict, List
from xml.etree import ElementTree

import git
from importlib_resources import files
import librosa
import matplotlib.pyplot as plt
import numpy as np
import pkg_resources
import pykanto.plot as kplot
import pytest
import ray
import seaborn as sns
import soundfile as sf
from bokeh.palettes import Set3_12
from pykanto.dataset import KantoData
from pykanto.parameters import Parameters
from pykanto.signal.segment import (
    get_segment_info,
    segment_files,
    segment_files_parallel,
)
from pykanto.utils.compute import flatten_list, to_iterator, tqdmm
from pykanto.utils.custom import (
    chipper_units_to_json,
    parse_sonic_visualiser_xml,
)
from pykanto.utils.paths import (
    ProjDirs,
    get_file_paths,
    get_wavs_w_annotation,
    pykanto_data,
)
from pykanto.utils.write import makedir

warnings.simplefilter("always", ImportWarning)
os.environ["RAY_DISABLE_IMPORT_WARNING"] = "1"


# %%──── SETTINGS ──────────────────────────────────────────────────────────────

# Import package data

# DATASETS_ID = ["STORM-PETREL", "BENGALESE_FINCH", "GREAT_TIT"]
# for dataset in DATASETS_ID:
#     DIRS = pykanto_data(dataset=dataset)
#     print(DIRS)


DATASET_ID = "GREAT_TIT"
DIRS = pykanto_data(dataset=DATASET_ID)
print(DIRS)

# %%
params = Parameters(dereverb=True, verbose=False)
dataset = KantoData(
    DATASET_ID,
    DIRS,
    parameters=params,
    overwrite_dataset=True,
    overwrite_data=True,
)

dataset.vocs.head()
# %%

# storm petrel
DATASET_ID = "STORM-PETREL"
DATA_PATH = Path(pkg_resources.resource_filename("pykanto", "data"))
PROJECT = Path(DATA_PATH).parent
RAW_DATA = DATA_PATH / "raw" / DATASET_ID

DIRS = ProjDirs(PROJECT, RAW_DATA, mkdir=True)

wav_filepaths, xml_filepaths = [
    get_file_paths(DIRS.RAW_DATA, [ext]) for ext in [".wav", ".xml"]
]
files_to_segment = get_wavs_w_annotation(wav_filepaths, xml_filepaths)


segment_files_parallel(
    files_to_segment,
    DIRS,
    resample=22050,
    parser_func=parse_sonic_visualiser_xml,
    min_duration=0.1,
    min_freqrange=100,
    labels_to_ignore=["NOISE"],
)

outfiles = [get_file_paths(DIRS.SEGMENTED, [ext]) for ext in [".wav", ".JSON"]]


# 2. Chipper outputs segmentation information to a gzip file. Let's add this to
# the JSON metadata created above.

# # These files can be anywhere, but here the external segmentation files
# are in a subdirectory within `data/segmented``

chipper_units_to_json(DIRS.SEGMENTED)

# %%


# Define parameters
params = Parameters(
    # Spectrogramming
    window_length=512,
    hop_length=32,
    n_fft=2048,
    num_mel_bins=240,
    sr=22050,
    top_dB=65,  # top dB to keep
    lowcut=300,
    highcut=10000,
    dereverb=True,
    # general settings
    song_level=True,
    subset=(0, -1),
    verbose=False,
)

# np.random.seed(123)
# random.seed(123)
dataset = KantoData(
    DATASET_ID,
    DIRS,
    parameters=params,
    overwrite_dataset=True,
    overwrite_data=False,
)

# Segmente into individual units using information from chipper,
# then check a few.
dataset.segment_into_units()

for voc in dataset.vocs.index:
    dataset.plot_segments(voc)


# %%

to_rm = [
    dataset.DIRS.DATASET.parent,
    dataset.DIRS.SEGMENTED / "WAV",
    dataset.DIRS.SEGMENTED / "JSON",
]
for path in to_rm:
    if path.exists():
        shutil.rmtree(str(path))
assert all([f.exists() for f in to_rm]) == False


# ──── INTERACTIVE APP DEMO ────────────────────────────────────────────────────

DATASET_ID = "LABEL_APP_DEMO"
PROJECT_ROOT = Path("/home/nilomr/projects/great-tit-song")
RAW_DATA = PROJECT_ROOT / "data" / "raw" / "LABEL_APP_DEMO_DATA"
DIRS = ProjDirs(PROJECT_ROOT, RAW_DATA, mkdir=True)
print(DIRS)


wav_filepaths, xml_filepaths = [
    get_file_paths(DIRS.RAW_DATA, [ext]) for ext in [".WAV", ".xml"]
]
files_to_segment = get_wavs_w_annotation(wav_filepaths, xml_filepaths)


segment_files_parallel(
    files_to_segment,
    DIRS,
    resample=22050,
    parser_func=parse_sonic_visualiser_xml,
    min_duration=0.1,
    min_freqrange=100,
    labels_to_ignore=["NOISE"],
)


# Define parameters
params = Parameters(
    # Spectrogramming
    window_length=1024,
    hop_length=128,
    n_fft=1024,
    num_mel_bins=224,
    sr=22050,
    top_dB=65,  # top dB to keep
    lowcut=2000,
    highcut=10000,
    # Segmentation,
    max_dB=-30,  # Max threshold for segmentation
    dB_delta=5,  # n thresholding steps, in dB
    silence_threshold=0.1,  # Between 0.1 and 0.3 tends to work
    max_unit_length=0.4,  # Maximum unit length allowed
    min_unit_length=0.02,  # Minimum unit length allowed
    min_silence_length=0.001,  # Minimum silence length allowed
    gauss_sigma=3,  # Sigma for gaussian kernel
    # general settings
    song_level=True,
    subset=None,
    verbose=False,
    num_cpus=None,
)

# np.random.seed(123)
# random.seed(123)
dataset = KantoData(
    DATASET_ID,
    DIRS,
    parameters=params,
    overwrite_dataset=True,
    random_subset=None,
    overwrite_data=False,
)


dataset.segment_into_units()
dataset.vocs["ID"] = "TR43633"

dataset.get_units()
dataset.cluster_ids(min_sample=20)
dataset.prepare_interactive_data()


out_dir = DIRS.DATA / "datasets" / DATASET_ID / f"{DATASET_ID}.db"
dataset = pickle.load(open(out_dir, "rb"))

dataset.open_label_app()

for song_level in [True, False]:
    dataset.parameters.update(song_level=song_level)
    dataset.get_units()

dataset.reload()
for song_level in [True, False]:
    dataset.parameters.update(song_level=song_level)
    dataset.cluster_ids(min_sample=5)

for song_level in [True, False]:
    dataset.parameters.update(song_level=song_level)
    dataset.prepare_interactive_data()

dataset.parameters.update(song_level=True)

pal = list(Set3_12)
pal.append("#ffed6f")

dataset.open_label_app()


# greti test


import json
from typing import List
from pykanto.utils.compute import tqdmm

sr = 48000


def build_sc_xml(segs: List, sr, include_label="GRETI_HQ") -> None | str:

    points = []
    sep = "\n      "
    for seg in segs:

        if seg[4][0]["species"] != include_label:
            print(seg[4][0]["species"])
            continue
        else:
            s = int(seg[0] * sr)
            e = int(seg[1] * sr)
            points.append(
                f'<point frame="{s}" value="{seg[2]}" duration="{e-s}" '
                f'extent="{seg[3]- seg[2]}" label="{seg[4][0]["species"]}" />'
            )

    if not len(points):
        return None

    xml_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE sonic-visualiser>
<sv>
    <data>
        <model id="1" name="" sampleRate="{sr}" type="sparse" dimensions="2" resolution="1" notifyOnAdd="true" dataset="0" subtype="box" minimum="83.5534" maximum="9420.05" units="Hz" />
        <dataset id="0" dimensions="2">
            {sep.join(points)}
        </dataset>
    </data>
    <display>
        <layer id="2" type="boxes" name="Boxes" model="1"  verticalScale="0"  colourName="White" colour="#ffffff" darkBackground="true" />
    </display>
</sv>"""

    return xml_str


def convert_data_to_xml(datfile: Path) -> None:
    # Open .data file
    with open(datfile) as dat:
        segs = json.load(dat)[1:]
    if not len(segs):
        return
    # Build xml
    xml_str = build_sc_xml(segs, sr)
    if xml_str is None:
        return
    outfile = (
        datfile.parent
        / f"{datfile.name[: -len(''.join(datfile.suffixes))]}.xml"
    )
    # Save .xml file in the same location
    with open(outfile.as_posix(), "w") as f:
        f.write(xml_str)


rootdir = Path("/media/nilomr/SONGDATA/raw/2020")
to_convert = list(rootdir.rglob("*.data"))
for file in tqdmm(to_convert):
    convert_data_to_xml(file)

#%%
# -------------------

DATASET_ID = "TEST"
PROJECT_ROOT = Path("/home/nilomr/projects/great-tit-song")
RAW_DATA = PROJECT_ROOT / "data" / "raw" / "2020"
DIRS = ProjDirs(PROJECT_ROOT, RAW_DATA, mkdir=True)


wav_filepaths, xml_filepaths = [
    get_file_paths(DIRS.RAW_DATA, [ext]) for ext in [".WAV", ".xml"]
]
files_to_segment = get_wavs_w_annotation(wav_filepaths, xml_filepaths)


#%%

wav_outdir, json_outdir = [
    makedir(DIRS.SEGMENTED / ext) for ext in ["WAV", "JSON"]
]

segment_files(
    files_to_segment[:2],
    wav_outdir,
    json_outdir,
    resample=22050,
    parser_func=parse_sonic_visualiser_xml,
    min_duration=1.5,
    min_freqrange=100,
    min_amplitude=6000,
    labels_to_ignore=["NOISE"],
)

segment_files_parallel(
    files_to_segment[:20],
    DIRS,
    resample=22050,
    parser_func=parse_sonic_visualiser_xml,
    min_duration=1.5,
    min_freqrange=100,
    min_amplitude=5000,
    labels_to_ignore=["NOISE"],
)
#%%


DATASET_ID = "GREAT_TIT"
DIRS = pykanto_data(dataset=DATASET_ID)


DIRS.WAV_LIST = sorted(list((DIRS.SEGMENTED / "WAV").glob("*.wav")))
if not len(DIRS.WAV_LIST):
    raise FileNotFoundError(f"There are no .wav files in {DIRS.WAV_LIST}")
DIRS.JSON_LIST = sorted(list((DIRS.SEGMENTED / "JSON").glob("*.JSON")))


#%%


# Define parameters
params = Parameters(
    # Spectrogramming
    window_length=1024,
    hop_length=128,
    n_fft=1024,
    num_mel_bins=224,
    sr=22050,
    top_dB=65,  # top dB to keep
    lowcut=2000,
    highcut=10000,
    # Segmentation,
    max_dB=-30,  # Max threshold for segmentation
    dB_delta=5,  # n thresholding steps, in dB
    silence_threshold=0.1,  # Between 0.1 and 0.3 tends to work
    max_unit_length=0.4,  # Maximum unit length allowed
    min_unit_length=0.02,  # Minimum unit length allowed
    min_silence_length=0.001,  # Minimum silence length allowed
    gauss_sigma=3,  # Sigma for gaussian kernel
    # general settings
    song_level=True,
    subset=None,
    verbose=False,
    num_cpus=None,
)
# np.random.seed(123)
# random.seed(123)
dataset = KantoData(
    DATASET_ID,
    DIRS,
    parameters=params,
    overwrite_dataset=True,
    random_subset=None,
    overwrite_data=True,
)

#%%
dataset.segment_into_units()


def plot()

dataset.vocs.at[key, "spectrogram_loc"]

        for key in testkeys:
            kplot.melspectrogram(
                dataset.vocs.at[key, "spectrogram_loc"],
                parameters=self.parameters,
                title=Path(key).stem,
                **kwargs,
            )



for voc in dataset.vocs.index:
    print(voc)
    dataset.plot_segments(voc)


# %%
dataset.get_units()
dataset.cluster_ids(min_sample=2)
dataset.prepare_interactive_data()


from pykanto.utils.read import load_dataset

out_dir = DIRS.DATA / "datasets" / DATASET_ID / f"{DATASET_ID}.db"
dataset = load_dataset(out_dir)


dataset.open_label_app()

dataset.vocs.head()
dataset.to_csv(dataset.DIRS.DATASET.parent)

recover_csv = pd.read_csv(
    dataset.DIRS.DATASET.parent / "TEST_VOCS.csv", index_col=0
)
np_str = recover_csv["silence_durations"]
dataset.vocs["unit_durations"][0]

import ast
import numpy as np


def from_np_array(array_string):
    array_string = ",".join(array_string.replace("[ ", "[").split())
    return np.array(ast.literal_eval(array_string))


df2 = pd.read_csv(
    dataset.DIRS.DATASET.parent / "TEST_VOCS.csv",
    index_col=0,
    converters={"images": from_np_array},
)
dataset.vocs.info()

for col in dataset.vocs.columns:
    if isinstance(dataset.vocs[col][0], list):
        print(col)

VIstring = ",".join(["%.5f" % num for num in np_str])

np.fromstring(np_str, sep=" ")
