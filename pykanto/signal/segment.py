
from __future__ import annotations
import librosa
import ray
import soundfile as sf

import datetime as dt
import json
import os
import re
import time
import warnings
import wave
from pathlib import Path
from typing import Any, Dict, Iterable, List, TYPE_CHECKING, Tuple
from xml.etree import ElementTree

import audio_metadata
import numpy as np
from scipy import ndimage
from skimage.exposure import equalize_hist
from skimage.filters import threshold_yen
from skimage.filters.rank import median
from skimage.filters.rank.generic import equalize
from skimage.morphology import dilation, disk, erosion
from skimage.util import img_as_ubyte
from pykanto.signal.filter import (
    dereverberate_jit, dereverberate, gaussian_blur, kernels, norm, normalise)
from pykanto.signal.spectrogram import retrieve_spectrogram
from pykanto.utils.compute import calc_chunks, flatten_list, get_chunks, print_parallel_info, to_iterator, tqdmm
from pykanto.utils.write import NoIndentEncoder

if TYPE_CHECKING:
    from pykanto.dataset import SongDataset

from pykanto.utils.paths import get_xml_filepaths
from pykanto.utils.write import makedir
from tqdm.auto import tqdm

# ────────────────────────────────────────────────────────────────────────────────
#   Read song segmentation information from AviaNZ
#   + save .wav files of individual songs
#   + save .jason files for each song
# ────────────────────────────────────────────────────────────────────────────────


def segment_songs(
        wavfile: Path, DATA_DIR: Path,
        DT_ID: str, RAW_DATA_ID: str,
        subset: str = "GRETI_HQ", threshold: int = 0) -> None:
    """
    Takes onset/offset times for a .wav files and saves
    individual .wav files for each segment, as well as a .json file 
    with the relevant metadata. Reads .data files generated by AviaNZ, 
    which are assumed to be in the same folder as the 'wavfile'.

    Args:
        wavfile (Path): path to the source.wav file
        DATA_DIR (Path): Path to higher-level data folder
        DT_ID (str): Identifier for dataset ("%Y-%m-%d_%H-%M-%S")
        RAW_DATA_ID (str): Label to export. Defaults to "GRETI_HQ".
        subset (str, optional): Label of desired subset. Defaults to "GRETI_HQ".
        threshold (int, optional): Minimum amplitude of segment. Defaults to 0.
    """
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

                if float(max(temp)) > threshold:

                    # Save .wav
                    wav_out = (
                        DATA_DIR
                        / "processed"
                        / RAW_DATA_ID
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
                    makedir(wav_out)
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
                    tags: str = audio_metadata.load(wavfile)["tags"].comment[0]
                    audiomoth = re.search(
                        r"AudioMoth.(.*?) at gain", tags).group(1)

                    json_dict = {}
                    json_dict["species"] = species
                    json_dict["nestbox"] = wavfile.parts[-2]
                    json_dict["indvs"] = {
                        wavfile.parts[-2]: {"species": species}}
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
                    json_dict["source_loc"] = wavfile.as_posix()
                    json_dict["wav_loc"] = wav_out.as_posix()

                    # Dump json
                    json_txt = json.dumps(
                        json_dict, cls=NoIndentEncoder, indent=2)
                    json_out = (
                        DATA_DIR
                        / "processed"
                        / RAW_DATA_ID
                        / DT_ID
                        / "JSON"
                        / (wav_out.name + ".JSON")
                    )

                    # Save .json
                    makedir(json_out.as_posix())
                    f = open(json_out.as_posix(), "w")
                    print(json_txt, file=f)
                    f.close()

    else:
        print(
            """No .data file exists for this .wav
        There might be files with no segmentation information or
        you might have included an unwanted directory"""
        )


def batch_segment_songs_single(
        origin: Path, DATA_DIR: Path,
        DT_ID: str, RAW_DATA_ID: str,
        subset: str = "GRETI_HQ", threshold: int = 0) -> None:
    """
    Extracts all sound segments found in a folder/subfolders.
    Based on code by Stephen Marsland, Nirosha Priyadarshani & Julius Juodakis.

    Args:

        origin (Path): folder with raw data to be segmented
        DATA_DIR (Path): Path to higher-level data folder
        DT_ID (str): Identifier for dataset ("%Y-%m-%d_%H-%M-%S")
        subset (str, optional): Label to export. Defaults to "GRETI_HQ"
    """
    for root, _, files in os.walk(str(origin)):
        wav: str
        for wav in tqdm(
                files, desc="{Reading, trimming and saving songs}", position=0,
                leave=True,):

            if wav.endswith(".wav") or wav.endswith(".WAV") and wav + ".data" in files:
                wavfile = Path(root) / wav
                segment_songs(
                    wavfile,
                    DATA_DIR,
                    DT_ID,
                    RAW_DATA_ID,
                    subset=subset,
                    threshold=threshold,
                )


def batch_segment_songs(
        origin: Path, DATA_DIR: Path, DT_ID: str,
        RAW_DATA_ID: str, subset: str = "GRETI_HQ",
        threshold: int = 0) -> None:
    """
    Extracts all sound segments found in a folder/subfolders. Uses multiprocessing.

    Args:

        origin (Path): folder with raw data to be segmented
        DATA_DIR (Path): Path to higher-level data folder
        DT_ID (str): Identifier for dataset ("%Y-%m-%d_%H-%M-%S")
        subset (str, optional): Label to export. Defaults to "GRETI_HQ"
    """
    for root, _, files in os.walk(str(origin)):

        p = Pool(processes=cpu_count() - 2)
        start = time.time()

        for wav in tqdm(
                files, desc="{Reading, trimming and saving songs}", position=0,
                leave=True,):
            wavfile = Path(root) / wav

            if wav.endswith(".wav") or wav.endswith(".WAV") and wav + ".data" in files:
                p.apply_async(
                    segment_songs,
                    args=(wavfile, DATA_DIR, DT_ID, RAW_DATA_ID),
                    kwds={"subset": subset, "threshold": threshold},
                )

        p.close()
        p.join()
        print("Complete")
        end = time.time()
        print("total time (s)= " + str(end - start))


# ────────────────────────────────────────────────────────────────────────────────
#   Read song segmentation information from Sonic Visualiser
#   + save .wav files of individual songs
#   + save .jason files for each song
# ────────────────────────────────────────────────────────────────────────────────

def get_segment_info(
    ORIGIN_DIR: Path,
    min_duration: float,
    min_freqrange: int,
    ignore_labels: List[str] = ["FIRST", "first"]
) -> Dict[str, List[float]]:
    """
    Get a summary of all segments present in a directory. Works for .xml files 
        output by Sonic Visualiser.

    Args:
        ORIGIN_DIR (Path): Folder to check, normally DATA_DIR / "raw" / YEAR
        min_duration (float): Minimum duration for a segment to be
            considered (in seconds)
        min_freqrange (int): Minimum frequency range for a segment to be
            considered (in hertz)
        ignore_labels (List[str], optional): Ignore segments with these labels. 
            Defaults to ["FIRST", "first"].
    Returns:
        Dict[str, List[float]]: Lists of segment durations, in seconds
    """

    XML_LIST = get_xml_filepaths(ORIGIN_DIR)
    cnt = 0
    noise_cnt = 0
    signal_cnt = 0
    noise_lengths: List[float] = []
    signal_lengths: List[float] = []

    for XML_FILEDIR in XML_LIST:
        root = ElementTree.parse(XML_FILEDIR).getroot()
        sr = int(root.findall('data/model')[0].get('sampleRate'))
        min_frames = min_duration * sr

    # iterate over segments and save them (+ metadata)
        for segment in root.findall('data/dataset/point'):
            seg_nframes = float(segment.get('duration'))
            # Ignore very short segments
            if seg_nframes < min_frames:
                continue
        # Also ignore segments that have very narroy bandwidth
            if float(segment.get('extent')) < min_freqrange:
                continue
        # Ignore first segments
            if segment.get('label') in ignore_labels:
                continue
            else:
                cnt += 1
                if segment.get('label') in ["NOISE", "noise"]:
                    noise_cnt += 1
                    noise_lengths.append(seg_nframes / sr)
                else:
                    signal_cnt += 1
                    signal_lengths.append(seg_nframes / sr)

    print(
        f'There are {cnt} segments in {ORIGIN_DIR}, of which {signal_cnt} are '
        f'songs and {noise_cnt} are noise samples. Returning a dictionary '
        'containing lists of segment durations.')

    return {'signal_lengths': signal_lengths, 'noise_lengths': noise_lengths}


def segment_into_songs(
    DATA_DIR: Path,
    WAV_FILEDIR: Path,
    RAW_DATA_ID: str,
    min_duration: float = .5,
    min_freqrange: int = 200,
    species: str = "Big Bird",
    ignore_labels: List[str] = ["FIRST", "first"],
    resample: int | None = 22050
) -> None:
    """
    Segments long .wav files recorderd with AudioMoth units into shorter 
    segments, using segmentation metadata from .xml files output by 
    Sonic Visualiser. Works well with large files (only reads one chunk at a
    time).

    Note:
        .xml files should be in the same folder as the .wav files. 

    Args:
        DATA_DIR (Path): Directory where to output files (will create subfolders).
        WAV_FILEDIR (Path): Path to wav file.
        RAW_DATA_ID (str): Name of dataset.
        min_duration (float, optional): Minimum duration for a segment to be
            considered (in seconds). Defaults to .5.
        min_freqrange (int, optional): Minimum frequency range for a segment to be
            considered (in hertz). Defaults to 200.
        species (str, optional): Species name. Defaults to "Big Bird".
        ignore_labels (List[str], optional): Ignore segments with these labels. 
            Defaults to ["FIRST", "first"].

    Raises:
        Exception: Will raise an exception if sample rates in the file and the
        metadata do not coincide. You should check why.
    """
    wavfile_str = str(WAV_FILEDIR)
    XML_FILEDIR = WAV_FILEDIR.parents[0] / str(WAV_FILEDIR.stem + ".xml")

    if Path(XML_FILEDIR).is_file():
        wavfile = sf.SoundFile(wavfile_str)
        if not wavfile.seekable():
            raise ValueError(f"Cannot seek through this file ({wavfile_str}).")
        sr = wavfile.samplerate

        # Get xml metadata for this file.
        # This xml file structure is particular to Sonic Visualiser
        root = ElementTree.parse(XML_FILEDIR).getroot()

        # Get sample rate of file
        xml_sr = int(root.findall('data/model')[0].get('sampleRate'))
        species = species  # Here you would obtain the species label.

        # Check that sample rates coincide
        if not sr == xml_sr:
            warnings.warn(
                f"Sample rates do not coincide for {WAV_FILEDIR.name}. "
                f"(XML: {xml_sr}, WAV: {sr}.)")

        # Get minimum n of frames for a segment to be kept
        min_frames = min_duration * sr

        # Prepare audio metadata
        mtdt = audio_metadata.load(WAV_FILEDIR)
        metadata = {
            'bit_depth': mtdt["streaminfo"].bit_depth,
            'tags': mtdt['tags'].comment[0],
            'audiomoth': re.search(
                r"AudioMoth.(.*?) at gain", mtdt['tags'].comment[0]).group(1),
            'datetime': dt.datetime.strptime(
                WAV_FILEDIR.stem, "%Y%m%d_%H%M%S")
        }

        # Where to save output?
        OUT_DIR = DATA_DIR / "segmented" / RAW_DATA_ID

        # Iterate over segments and save them (+ metadata)
        for cnt, segment in enumerate(root.findall('data/dataset/point')):
            # Ignore very short segments, segments that have very narrow
            # bandwidth, and anything passed to `ignore_labels`.
            if (int(
                    float(segment.get('duration'))) < min_frames or int(
                    float(segment.get('extent'))) < min_freqrange or segment.get(
                    'label') in ignore_labels):
                continue
            else:
                save_segment(segment, WAV_FILEDIR, OUT_DIR, species,
                             sr, wavfile, metadata, cnt, resample=resample)


def save_segment(
    segment: Dict[str, object],
    WAV_FILEDIR: Path,
    OUT_DIR: Path,
    species: str, sr: int,
    wavfile: sf.SoundFile,
    metadata: Dict[str, Any],
    cnt: int,
    resample: int | None = 22050
) -> None:
    """
    Save wav and json files for a single song segment present in WAV_FILEDIR

    Args:
        segment (Dict[str, object]): [description]
        WAV_FILEDIR (Path): [description]
        OUT_DIR (Path): [description]
        species (str): [description]
        sr (int): [description]
        wavfile (sf.SoundFile): [description]
        metadata (Dict[str, Any]): [description]
        cnt (int): [description]
        resample (int, optional): [description]. Defaults to 22050.
    """
    # Extract relevant information from xml file
    start, duration, lowfreq, freq_extent = [
        int(float(segment.get(value)))
        for value in ['frame', 'duration', 'value', 'extent']]
    label = segment.get('label')

    # Get segment frames
    wavfile.seek(start)
    audio_section = wavfile.read(duration)

    # Save .wav
    wav_out = (OUT_DIR
               / "WAV"
               / (f'{WAV_FILEDIR.stem[:4]}-'
                  f'{WAV_FILEDIR.parts[-2]}-'
                  f'{WAV_FILEDIR.stem[4:-4]}-'
                  f'{str(cnt)}.wav')
               )
    makedir(wav_out)

    if resample:
        audio_section = librosa.resample(audio_section, sr, resample)
        sf.write(wav_out, audio_section, resample)
    else:
        sf.write(wav_out, audio_section, sr)

    # Make a JSON dictionary to go with the .wav file
    seg_datetime = metadata['datetime'] + dt.timedelta(seconds=start/sr)
    json_dict = {"species": species,
                 "ID": WAV_FILEDIR.parts[-2],
                 "label": label,
                 "recorder": metadata['audiomoth'],
                 "recordist": "Nilo Merino Recalde",
                 "source_datetime": str(metadata['datetime']),
                 "datetime": str(seg_datetime),
                 "date": str(seg_datetime.date()),
                 "time": str(seg_datetime.time()),
                 "timezone": "UTC",
                 "samplerate_hz": resample if resample else sr,
                 "length_s": len(audio_section) / resample if resample else sr,
                 "lower_freq": lowfreq,
                 "upper_freq": lowfreq + freq_extent,
                 "max_amplitude": float(max(audio_section)),
                 "min_amplitude": float(min(audio_section)),
                 "bit_depth": metadata['bit_depth'],
                 "tech_comment": metadata['tags'],
                 "source_loc": WAV_FILEDIR.as_posix(),
                 "wav_loc": wav_out.as_posix()}

    # Dump json
    json_out = (OUT_DIR / "JSON" / (wav_out.name + ".JSON"))
    makedir(json_out)
    f = open(json_out.as_posix(), "w")
    print(json.dumps(json_dict, indent=2), file=f)
    f.close()


def find_units(
    dataset: SongDataset,
    spectrogram: np.ndarray
) -> Tuple[np.ndarray, np.ndarray] | None:
    """
    Segment a given spectrogram array into its units. For convenience,
    parameters are defined in a SongDataset class instance (class Parameters).
    Based on Tim Sainburg's 
    `vocalseg <https://github.com/timsainb/vocalization-segmentation/>`_ code.


    Returns:
        Tuple[np.ndarray, np.ndarray]: Tuple(onsets, offsets)
        None: Did not find any units matching given criteria.
    """

    params = dataset.parameters
    envelope_is_good = False
    params.hop_length_ms = params.sr / params.hop_length

    # Loop through thresholds, lowest first
    for min_level_dB in np.arange(-params.top_dB,
                                  params.max_dB,
                                  params.dB_delta):
        # Threshold spectrogram
        spec = norm(normalise(spectrogram, min_level_db=min_level_dB))
        spec = spec - np.median(spec, axis=1).reshape((len(spec), 1))
        spec[spec < 0] = 0

        # Calculate and normalise the amplitude envelope
        envelope = np.max(spec, axis=0) * np.sqrt(np.mean(spec, axis=0))
        envelope = envelope / np.max(envelope)

        # Get onsets and offsets (sound and silence)
        onsets, offsets = onsets_offsets(
            envelope > params.silence_threshold) / params.hop_length_ms
        onsets_sil, offsets_sil = (
            onsets_offsets(
                envelope <= params.silence_threshold) /
            params.hop_length_ms)

        # Check results and return or continue
        if len(onsets_sil) > 0:
            # Get longest silences and vocalizations
            max_silence_len = np.max(offsets_sil - onsets_sil)
            max_unit_len = np.max(offsets - onsets)
            # Can this be considered a bout?
            if (max_silence_len > params.min_silence_length and
                    max_unit_len < params.max_unit_length):
                envelope_is_good = True
                break

    if not envelope_is_good:
        return None, None  # REVIEW

    # threshold out short syllables
    length_mask = (offsets - onsets) >= params.min_unit_length

    return onsets[length_mask], offsets[length_mask]


def onsets_offsets(signal):
    """
    [summary]

    Arguments:
        signal {[type]} -- [description]

    Returns:
        [type] -- [description]
    """
    units, nunits = ndimage.label(signal)
    if nunits == 0:
        return np.array([[0], [0]])
    onsets, offsets = np.array(
        [
            np.where(units == unit)[
                0][np.array([0, -1])] + np.array([0, 1])
            for unit in np.unique(units)
            if unit != 0
        ]
    ).T
    return np.array([onsets, offsets])


def segment_song_into_units(
    dataset: SongDataset,
    key: str
) -> Tuple[str, np.ndarray, np.ndarray]:

    mel_spectrogram = retrieve_spectrogram(
        dataset.vocalisations.at[key, 'spectrogram_loc'])

    # NOTE: numba version (dereverberate_jit) more likely to crash when running
    # this in parallel in low memory situations for obvious reasons.
    mel_spectrogram_d = dereverberate(
        mel_spectrogram, echo_range=100, echo_reduction=3,
        hop_length=dataset.parameters.hop_length, sr=dataset.parameters.sr)
    mel_spectrogram_d = img_as_ubyte(norm(mel_spectrogram_d))

    img_eq = equalize_hist(mel_spectrogram)
    img_med = median(img_as_ubyte(img_eq), disk(2))
    img_eroded = erosion(img_med, kernels.erosion_kern)
    img_dilated = dilation(img_eroded, kernels.dilation_kern)
    img_dilated = dilation(img_dilated, kernels.erosion_kern)

    img_norm = equalize_hist(img_dilated, disk(2))

    img_inv = np.interp(
        img_norm, (img_norm.min(),
                   img_norm.max()),
        (-dataset.parameters.top_dB, 0))
    img_gauss = gaussian_blur(img_inv.astype(float), 3)

    img_gauss_d = dereverberate(
        img_gauss, echo_range=100, echo_reduction=1,
        hop_length=dataset.parameters.hop_length, sr=dataset.parameters.sr)

    onsets, offsets = find_units(dataset, img_gauss_d)
    if onsets is None or offsets is None:
        warnings.warn(f'No units found in {key}. '
                      'This segment will be dropped from the dataset.')
        return None
    return key, onsets, offsets


@ray.remote
def _segment_song_into_units_r(
        dataset: SongDataset,
        keys: Iterable[str],
        **kwargs
) -> List[Tuple[str, np.ndarray, np.ndarray]]:
    return [segment_song_into_units(dataset, key, **kwargs) for key in keys]


def segment_song_into_units_parallel(
    dataset: SongDataset,
    keys: Iterable[str],
    **kwargs
) -> List[Tuple[str, np.ndarray, np.ndarray]]:
    """See save_melspectrogram"""

    # Calculate and make chunks
    n = len(keys)
    if not n:
        raise KeyError('No file keys were passed to '
                       'segment_song_into_units.')
    chunk_info = calc_chunks(n, verbose=True)
    chunk_length, n_chunks = chunk_info[3], chunk_info[2]
    chunks = get_chunks(keys, chunk_length)
    print_parallel_info(n, 'vocalisations', n_chunks, chunk_length)

    # Copy dataset to local object store
    dataset_ref = ray.put(dataset)

    # Distribute with ray
    obj_ids = [_segment_song_into_units_r.remote(
        dataset_ref, i, **kwargs) for i in chunks]
    pbar = {'desc': "Finding units in vocalisations", 'total': n_chunks}
    units = [obj_id for obj_id in tqdmm(to_iterator(obj_ids), **pbar)]

    # Flatten and return
    return flatten_list(units)
