# Code to:
#
# - Read song segmentation information from AviaNZ
#   and save .wav files of individual songs.
#
# - Save JSON dictionaries including all pertinent
#   information for each song.


import gzip
import pickle
import numpy as np
import wavio
import os
import json
import wave
import contextlib
import sys
from tqdm.auto import tqdm
import pathlib2
from pathlib2 import Path
import time
import datetime as dt
from src.read.paths import safe_makedir
import audio_metadata
import re
from multiprocess import Pool, cpu_count


# ---------------------------------------------------------
#   Read song segmentation information from AviaNZ
#   + save .wav files of individual songs
#   + save .jason files for each song
# ---------------------------------------------------------


def segment_songs(wavfile, DATA_PATH, DT_ID, subset="GRETI_HQ"):
    """Takes onset/offset times for a .wav files and saves 
    individual .wav files for each segment, as well as a .json file with the relevant metadata.
    Reads .data files generated by AviaNZ, which are assumed to be in the same folder as the 'wavfile'.

    Args:
        wavfile (PosixPath): path to the source.wav file
        DATA_PATH (Posixpath): Path to higher-level data folder
        DT_ID (str): Identifier for dataset ("%Y-%m-%d_%H-%M-%S")
        subset (str, optional): Label to export. Defaults to "GRETI_HQ".
    """
    DATASET_ID = subset
    wavfile_str = str(wavfile)
    datfile = wavfile_str + ".data"
    datetime = dt.datetime.strptime(wavfile.stem, "%Y%m%d_%H%M%S")

    if Path(datfile).is_file():

        with open(datfile) as dat, wave.open(wavfile_str) as wav:
            segments = json.load(dat)[1:]
            frames = wav.getnframes()
            sampleRate = wav.getframerate()
            data = np.frombuffer(wav.readframes(frames), dtype=np.int16)

        cnt = 1

        for seg in segments:
            species = seg[4][0]["species"]
            cnt += 1

            # select segment with this label
            if subset == species:

                s = int(seg[0] * sampleRate)
                e = int(seg[1] * sampleRate)
                temp = data[s:e]

                # Save .wav
                wav_out = (
                    DATA_PATH
                    / "processed"
                    / DATASET_ID
                    / DT_ID
                    / "WAV"
                    / (
                        str(
                            wavfile.parts[-2]
                            + "-"
                            + species
                            + "-"
                            + wavfile.stem
                            + "-"
                            + str(cnt)
                            + ".wav"
                        )
                    )
                )
                safe_makedir(wav_out)
                wavio.write(
                    str(wav_out),
                    temp.astype("int16"),
                    sampleRate,
                    scale="dtype-limits",
                    sampwidth=2,
                )

                # make a JSON dictionary to go with the .wav file
                seg_datetime = datetime + dt.timedelta(seconds=seg[0])
                meta = audio_metadata.load(wavfile)
                tags = audio_metadata.load(wavfile)["tags"].comment[0]
                audiomoth = re.search(r"AudioMoth.(.*?) at gain", tags).group(1)

                json_dict = {}
                json_dict["species"] = species
                json_dict["nestbox"] = wavfile.parts[-2]
                json_dict["recorder"] = audiomoth
                json_dict["recordist"] = "Nilo Merino Recalde"
                json_dict["source_datetime"] = str(datetime)
                json_dict["datetime"] = str(seg_datetime)
                json_dict["date"] = str(seg_datetime.date())
                json_dict["time"] = str(seg_datetime.time())
                json_dict["timezone"] = "UTC"
                json_dict["samplerate_hz"] = sampleRate
                json_dict["length_s"] = len(temp) / sampleRate
                json_dict["lower_freq"] = seg[2]
                json_dict["upper_freq"] = seg[3]
                json_dict["max_amplitude"] = float(max(temp))
                json_dict["min_amplitude"] = float(min(temp))
                json_dict["bit_depth"] = meta["streaminfo"].bit_depth
                json_dict["tech_comment"] = tags
                json_dict["source_location"] = wavfile.as_posix()
                json_dict["wav_location"] = wav_out.as_posix()

                from avgn.utils.json import NoIndent, NoIndentEncoder

                # Dump json
                json_txt = json.dumps(json_dict, cls=NoIndentEncoder, indent=2)
                json_out = (
                    DATA_PATH
                    / "processed"
                    / DATASET_ID
                    / DT_ID
                    / "JSON"
                    / (wav_out.name + ".JSON")
                )

                # Save .json
                safe_makedir(json_out.as_posix())
                print(json_txt, file=open(json_out.as_posix(), "w"))

    else:
        print(
            """No .data file exists for this .wav
        There might be files with no segmentation information or
        you might have included an unwanted directory"""
        )


def batch_segment_songs_single(origin, DATA_PATH, DT_ID, subset="GRETI_HQ"):
    """Extracts all sound segments found in a folder/subfolders.
    Based on code by Stephen Marsland, Nirosha Priyadarshani & Julius Juodakis.

    Args:

        origin (PosixPath): folder with raw data to be segmented
        DATA_PATH (Posixpath): Path to higher-level data folder
        DT_ID (str): Identifier for dataset ("%Y-%m-%d_%H-%M-%S")
        subset (str, optional): Label to export. Defaults to "GRETI_HQ"
    """
    for root, dirs, files in os.walk(str(origin)):

        for wav in tqdm(
            files, desc="{Reading, trimming and saving songs}", position=0, leave=True,
        ):

            if wav.endswith(".wav") or wav.endswith(".WAV") and wav + ".data" in files:
                wavfile = Path(root) / wav
                segment_songs(wavfile, DATA_PATH, DT_ID, subset=subset)


def batch_segment_songs(origin, DATA_PATH, DT_ID, subset="GRETI_HQ"):
    """Extracts all sound segments found in a folder/subfolders. Uses multiprocessing.

    Args:

        origin (PosixPath): folder with raw data to be segmented
        DATA_PATH (Posixpath): Path to higher-level data folder
        DT_ID (str): Identifier for dataset ("%Y-%m-%d_%H-%M-%S")
        subset (str, optional): Label to export. Defaults to "GRETI_HQ"
    """
    for root, dirs, files in os.walk(str(origin)):

        p = Pool(processes=cpu_count() - 2)
        start = time.time()

        for wav in tqdm(
            files, desc="{Reading, trimming and saving songs}", position=0, leave=True,
        ):
            wavfile = Path(root) / wav

            if wav.endswith(".wav") or wav.endswith(".WAV") and wav + ".data" in files:
                p.apply_async(
                    segment_songs,
                    args=(wavfile, DATA_PATH, DT_ID),
                    kwds={"subset": subset},
                )

        p.close()
        p.join()
        print("Complete")
        end = time.time()
        print("total time (s)= " + str(end - start))


####################################

# 1 - Make function to split wavs to processed data folder
# (you need to create a directory in paths.py for this purpose)
# update: now can subset, ***needs better filenames AND platform-independent path creation

# 2 - get data from the .data file, add coordinates and other information
# and make a nice, tidy .jason file following avgn format


# * parse chipper gzips (see /utils.py)


# ---------------------------------------------------------
#   Read syllable segmentation information from Chipper
# ---------------------------------------------------------


def open_gzip(file):
    """Reads syllable segmentation generated with chipper
    
    Args:
        file (path): path to the .gzip file
    
    Returns:
        list: params, onsets, offsets
    """
    with gzip.open(file, "rb") as f:
        data = f.read()

    song_data = pickle.loads(data, encoding="utf-8")

    return song_data[0], song_data[1]


# ---------------------------------------------------------
#   Save JSON dictionaries including all pertinent
#   information for each song.
# ---------------------------------------------------------

# file = "/media/nilomr/SONGDATA/interim/2020/W100/SegSyllsOutput_20200407_T191217/SegSyllsOutput_W100-BLUETI-20200327_040000-27.gzip"
# open_gzip(file)[0]['BoutRange']

# json_dict = {}
# json_dict["species"] = "European starling"
# json_dict["common_name"] = "Sturnus vulgaris"

