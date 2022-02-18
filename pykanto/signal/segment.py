# ─── DESCRIPTION ──────────────────────────────────────────────────────────────

"""Segment audio files and find vocalisation units in spectrograms."""

# ─── DEPENDENCIES ─────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import (TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Tuple,
                    TypedDict)
from xml.etree import ElementTree

import audio_metadata as audiometa
import librosa
import librosa.display
import numpy as np
import ray
import soundfile as sf
from pykanto.signal.filter import (dereverberate, dereverberate_jit,
                                   gaussian_blur, kernels, norm, normalise)
from pykanto.signal.spectrogram import retrieve_spectrogram
from pykanto.utils.compute import (calc_chunks, flatten_list, get_chunks,
                                   print_parallel_info, to_iterator, tqdmm)
from pykanto.utils.custom import parse_sonic_visualiser_xml
from scipy import ndimage
from skimage.exposure import equalize_hist
from skimage.filters.rank import median
from skimage.morphology import dilation, disk, erosion
from skimage.util import img_as_ubyte

from pykanto.utils.types import AnnotationDict, AudioMetadataDict, MetadataDict

if TYPE_CHECKING:
    from pykanto.dataset import SongDataset

from pykanto.utils.paths import get_xml_filepaths


# ──── DIVIDING RAW FILES INTO SEGMENTS ─────────────────────────────────────────


# ──── CLASSES AND CLASS METHODS ────

class ReadWav:
    """
    Reads a wav file and its metadata.

    Note:
        You can extend this class to read in metadata from the wav file that is
        specific to your research, e.g. the recorder device ID, or time
        information.

    Examples:

        >>> class CustomReadWav(ReadWav):
        ... def get_metadata(self) -> Dict[str, Any]:
        ...     add_to_dict = {
        ...         'recorder': str(self.all_metadata['recorder'])
        ...     }
        ...     return {**self.metadata, **add_to_dict}

        >>> ReadWav = CustomReadWav
    """

    def __init__(self, wav_dir: Path) -> None:
        self.wav_dir = wav_dir
        """Location of wav file."""
        self._load_wav()
        self._load_metadata()

    def _load_wav(self) -> None:
        """
        Opens a wav sound file.

        Raises:
            ValueError: The file is not  'seekable'.
        """
        # Warning: keeps file open!!
        wavfile = sf.SoundFile(self.wav_dir)

        if not wavfile.seekable():
            raise ValueError("Cannot seek through this file", self.wav_dir)

        self.wavfile = wavfile

    def _load_metadata(self) -> None:
        """
        Loads available metadata from wavfile; builds a dictionary w/
        keys: ['sample_rate', 'bit_depth', ''bitrate', 'source_file'].
        """

        all_metadata = audiometa.load(self.wav_dir)
        self.all_metadata = all_metadata
        """All available metadata for this audio clip."""

        self.metadata: AudioMetadataDict = {
            'sample_rate': self.wavfile.samplerate,
            'bit_rate': self.all_metadata["streaminfo"].bitrate,
            'source_file': self.wav_dir,
        }
        """Selected metadata to be used later"""

    def get_wav(self) -> sf.SoundFile:
        """
        Returns the wavfile.

        Returns:
            sf.SoundFile: Seekable wavfile.
        """
        return self.wavfile

    def get_metadata(self) -> AudioMetadataDict:
        """
        Returns metadata attached to wavfile as a dictionary.

        Returns:
            Dict[str, Any]: Wavfile metadata.
        """
        return self.metadata


class SegmentMetadata:
    """
    Consolidates segment metadata in a single dictionary,
    which can then be saved as a standard .JSON file.

    You can extend this class to incorporate other metadata fields
    specific to your research (see Example).

    Examples:

        >>> class CustomSegmentMetadata(SegmentMetadata):
        ...     def get(self) -> Dict[str, Any]:
        ...         new_dict = {
        ...             'tags': self.all_metadata['tags']
        ...         }
        ...         return {**self.metadata, **new_dict}

        >>> SegmentMetadata = CustomSegmentMetadata

    """

    def __init__(
            self, name: str, metadata: AnnotationDict,
            audio_section: np.ndarray, i: int, sr: int, wav_out: Path) -> None:
        """
        Consolidates segment metadata in a single dictionary,
        which can then be saved as a standard .JSON file.

        Args:
            name (str): Segment identifier.
            metadata (AnnotationDict): A dictionary containing pertinent metadata.
            audio_section (np.ndarray): Array containing segment audio data
                (to extract min/max amplitude).
            i (int): Segment index.
            sr (int): Sample rate.
            wav_out (Path): Path to segment wav file.

        Returns: None

        Note:
            Call SegmentMetadata(args).get() to return dictionary.
        """

        self.all_metadata: AnnotationDict = metadata
        """Attribute containing all available metadata"""

        self.index: int = i
        """Index of 'focal' segment"""

        self._make_metadata_dict(name, metadata,
                                 audio_section, i, sr, wav_out)

    def _make_metadata_dict(
            self, name: str, metadata: AnnotationDict,
            audio_section: np.ndarray, i: int, sr: int, wav_out: Path) -> None:
        """
        Consolidates segment metadata in a single dictionary,
        which can then be saved as a standard .JSON file.

        Args:
            name (str): Segment identifier.
            metadata (Dict[str, Any]): A dictionary containing pertinent metadata.
            audio_section (np.ndarray): Array containing segment audio data
                (to extract min/max amplitude).
            i (int): Segment index.
            sr (int): Sample rate.
            wav_out (Path): Path to segment wav file.

        Returns: None

        Note:
            Call SegmentMetadata(args).get() to return dictionary.
        """

        self.metadata: MetadataDict = {
            "ID": name,
            "label": metadata['label'][i],
            "sample_rate": sr,
            "length_s": len(audio_section) / sr,
            "lower_freq": metadata['lower_freq'][i],
            "upper_freq": metadata['upper_freq'][i],
            "max_amplitude": float(max(audio_section)),
            "min_amplitude": float(min(audio_section)),
            "bit_rate": metadata['bit_rate'],
            "source_file": metadata['source_file'].as_posix(),
            "wav_file": wav_out.as_posix()}

    def get(self) -> MetadataDict:
        """
        Consolidates segment metadata in a single dictionary,
        which can then be saved as a standard .JSON file.

        Returns:
            Dict[str, Any]: Metadata dictionary (self.metadata)
        """
        return self.metadata


# ──── FUNCTIONS ────


def segment_file(
        wav_dir: Path,
        metadata_dir: Path,
        wav_outdir: Path,
        json_outdir: Path,
        resample: int | None = 22050,
        parser_func: Callable[[Path], AnnotationDict] = parse_sonic_visualiser_xml,
        **kwargs):
    """
    Segments and saves audio segmets and their metadata from a single audio 
    file, based on annotations provided in a separate 'metadata' file.

    Args:
        wav_dir (Path): Where are the wav files to be segmented?
        metadata_dir (Path): Where are the files containing segmentation 
            metadata?
        wav_outdir (Path): Where to save the resulting segmented wav files.
        json_outdir (Path): Where to save the resulting json metadata files.
        resample (int | None, optional): Whether to resample audio, and to what 
            sample ratio. Defaults to 22050.
        parser_func (Callable[[Path], dict[str, Any]], optional): 
            Function to parse your metadata format. Defaults to parse_sonic_visualiser_xml.
        **kwargs: Keyword arguments passed to 
            :func:`~pykanto.signal.segment.segment_is_valid`.
    """

    # Read audio and metadata
    wav_object = ReadWav(wav_dir)
    wavfile, audio_metadata = wav_object.get_wav(), wav_object.get_metadata()
    metadata: AnnotationDict = {**parser_func(metadata_dir), **audio_metadata}
    # Then save segments
    save_segments(metadata, wavfile, wav_outdir,
                  json_outdir, resample=resample, **kwargs)


def save_segments(
        metadata: AnnotationDict,
        wavfile: sf.SoundFile,
        wav_outdir: Path,
        json_outdir: Path,
        resample: int | None = 22050, **kwargs) -> None:
    """
    Save segments present in a single wav file to new separate files along with
    their metadata.

    Args:
        metadata (AnnotationDict): Annotation and file metadata for this wav file.
        wavfile (SoundFile): Seekable wav file.
        wav_outdir (Path): Where to save the resulting segmented wav files.
        json_outdir (Path): Where to save the resulting json metadata files.
        resample (int | None, optional): Whether to resample audio, and to what 
            sample ratio. Defaults to 22050.
        **kwargs: Keyword arguments passed to 
            :func:`~pykanto.signal.segment.segment_is_valid`.
    """

    n_segments = len(metadata['start_times'])
    for i in range(n_segments):
        if not segment_is_valid(metadata, i, **kwargs):
            return

        # Get segment frames
        wavfile.seek(metadata['start_times'][i])
        audio_section: np.ndarray = wavfile.read(metadata['durations'][i])

        # Collapse to mono if not already the case
        if len(audio_section.shape) == 2:
            audio_section: np.ndarray = librosa.to_mono(
                np.swapaxes(audio_section, 0, 1))

        # Resample if necessary
        sr = metadata['sample_rate']

        if resample:
            audio_section: np.ndarray = librosa.resample(
                audio_section, sr, resample)
            sr = resample

        # Both to disk under name:
        name: str = metadata['source_file'].stem

        # Save .wav
        wav_out = (wav_outdir / f'{name}_{str(i)}.wav')
        sf.write(wav_out.as_posix(), audio_section, sr)

        # Save metadata .json
        segment_metadata = SegmentMetadata(
            name, metadata, audio_section, i, sr, wav_out).get()
        json_out = (json_outdir / f'{name}_{str(i)}.JSON')
        with open(json_out.as_posix(), "w") as f:
            print(json.dumps(segment_metadata, indent=2), file=f)


def segment_is_valid(
        segment_metadata: Dict[str, Any],
        i: int,
        min_duration: float = .5,
        min_freqrange: int = 200,
        labels_to_ignore: List[str] = ["NO", "NOISE"]) -> bool:
    """
    Checks whether a segment of index i within a dictionary is a valid segment.

    Args:
        segment_metadata (Dict[str, Any]): Dictionary with segment inf
        i (int): _description_
        min_duration (float, optional): _description_. Defaults to .5.
        min_freqrange (int, optional): _description_. Defaults to 200.
        labels_to_ignore (List[str], optional): _description_. Defaults to ["NO", "NOISE"].

    Returns:
        bool: _description_
    """

    min_frames = min_duration * segment_metadata['sample_rate']

    if ((segment_metadata['durations'][i] < min_frames) or
            (segment_metadata['freq_extent'][i] < min_freqrange) or
            (segment_metadata['label'][i] in labels_to_ignore)):
        return False
    else:
        return True


def segment_files(
        datapaths: List[Tuple[Path, Path]],
        wav_outdir: Path,
        json_outdir: Path,
        resample: int | None = 22050,
        parser_func: Callable[[Path], AnnotationDict] = parse_sonic_visualiser_xml,
        pbar: bool = True,
        **kwargs) -> None:
    """
    Finds and saves audio segments and their metadata.
    Parallel version in :func:`~pykanto.signal.segment.segment_files_parallel`.
    Works well with large files (only reads one chunk at a time).

    Args:
        datapaths (List[Tuple[Path, Path]]): List of tuples with pairs of paths
            to raw data files and their annotation metadata files.
        wav_outdir (Path): Location where to save generated wav files.
        json_outdir (Path): Location where to save generated json metadata files.
        resample (int | None, optional): Whether to resample audio.
            Defaults to 22050.
        parser_func (Callable[[Path], dict[str, Any]], optional): 
            Function to parse your metadata format. 
            Defaults to parse_sonic_visualiser_xml.
        pbar (bool, optional): Wheter to print progress bar. Defaults to True.
        **kwargs: Keyword arguments passed to 
            :func:`~pykanto.signal.segment.segment_is_valid`
    """
    if len(datapaths) == 0:
        raise IndexError('List must contain at least one tuple.', datapaths)
    elif isinstance(datapaths, tuple):
        datapaths = [datapaths]

    for wav_dir, metadata_dir in tqdmm(
            datapaths,
            desc="Finding and savings audio segments and their metadata",
            disable=False if pbar else True):
        try:
            segment_file(wav_dir, metadata_dir, wav_outdir, json_outdir,
                         resample=resample, parser_func=parser_func, **kwargs)
        except RuntimeError as e:
            print(f'Failed to export {wav_dir}: ', e)


segment_files_r = ray.remote(segment_files)
"""Remote'd version of :func:`~pykanto.signal.segment.segment_files"""


def segment_files_parallel(
        datapaths: List[Tuple[Path, Path]],
        wav_outdir: Path,
        json_outdir: Path,
        resample: int | None = 22050,
        parser_func: Callable[[Path], AnnotationDict] = parse_sonic_visualiser_xml,
        **kwargs):
    """
    Finds and saves audio segments and their metadata.
    Parallel version of :func:`~pykanto.signal.segment.segment_files`.
    Works well with large files (only reads one chunk at a time).

    Args:
        datapaths (List[Tuple[Path, Path]]): List of tuples with pairs of paths
            to raw data files and their annotation metadata files.
        wav_outdir (Path): Location where to save generated wav files.
        json_outdir (Path): Location where to save generated json metadata files.
        resample (int | None, optional): Whether to resample audio.
            Defaults to 22050.
        parser_func (Callable[[Path], dict[str, Any]], optional): 
            Function to parse your metadata format. 
            Defaults to parse_sonic_visualiser_xml.
        **kwargs: Keyword arguments passed to 
            :func:`~pykanto.signal.segment.segment_is_valid`
    """

    # Calculate and make chunks
    n = len(datapaths)
    if not n:
        raise KeyError('No file keys were passed to '
                       'segment_song_into_units.')
    chunk_length, n_chunks = map(calc_chunks(
        n, verbose=True).__getitem__, [3, 2])
    chunks = get_chunks(datapaths, chunk_length)
    print_parallel_info(n, 'files', n_chunks, chunk_length)

    # Distribute with ray
    obj_ids = [
        segment_files_r.remote(
            paths, wav_outdir, json_outdir,
            resample=resample,
            parser_func=parser_func, pbar=False, **kwargs)
        for paths in chunks]
    pbar = {
        'desc': "Finding and savings audio segments and their metadata",
        'total': n_chunks}
    [obj_id for obj_id in tqdmm(to_iterator(obj_ids), **pbar)]


def get_segment_info(
    RAW_DATA_DIR: Path,
    min_duration: float,
    min_freqrange: int,
    ignore_labels: List[str] = ["FIRST", "first"]
) -> Dict[str, List[float]]:
    """
    Get a summary of all segments present in a directory. Works for .xml files 
        output by Sonic Visualiser.

    Args:
        RAW_DATA_DIR (Path): Folder to check, normally DATA_DIR / "raw" / YEAR
        min_duration (float): Minimum duration for a segment to be
            considered (in seconds)
        min_freqrange (int): Minimum frequency range for a segment to be
            considered (in hertz)
        ignore_labels (List[str], optional): Ignore segments with these labels. 
            Defaults to ["FIRST", "first"].
    Returns:
        Dict[str, List[float]]: Lists of segment durations, in seconds
    """

    # TODO: Make it work with any file type (by passing a custom parser
    # function)

    XML_LIST = get_xml_filepaths(RAW_DATA_DIR)
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
        f'There are {cnt} segments in {RAW_DATA_DIR}, of which {signal_cnt} are '
        f'songs and {noise_cnt} are noise samples. Returning a dictionary '
        'containing lists of segment durations.')

    return {'signal_lengths': signal_lengths, 'noise_lengths': noise_lengths}


# ──── SEGMENTING UNITS PRESENT IN A SEGMENT ────────────────────────────────────


def find_units(
    dataset: SongDataset,
    spectrogram: np.ndarray
) -> Tuple[np.ndarray, np.ndarray] | tuple[None, None]:
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


def onsets_offsets(signal: np.ndarray) -> np.ndarray:
    """
    Labels features in array as insets and offsets.
    Based on Tim Sainburg's 
    `vocalseg <https://github.com/timsainb/vocalization-segmentation/>`_ code.

    Args:
        signal (np.ndarray): _description_

    Returns:
        np.ndarray: _description_
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
) -> Tuple[str, np.ndarray, np.ndarray] | None:

    mel_spectrogram = retrieve_spectrogram(
        dataset.vocalisations.at[key, 'spectrogram_loc'])

    # NOTE: numba version (dereverberate_jit) more likely to crash when running
    # this in parallel in low memory situations for obvious reasons.
    mel_spectrogram_d = dereverberate_jit(
        mel_spectrogram, echo_range=100, echo_reduction=3,
        hop_length=dataset.parameters.hop_length, sr=dataset.parameters.sr)
    mel_spectrogram_d = img_as_ubyte(norm(mel_spectrogram_d))

    img_eq = equalize_hist(mel_spectrogram)
    img_med = median(img_as_ubyte(img_eq), disk(2))
    img_eroded = erosion(img_med, kernels.erosion_kern)
    img_dilated = dilation(img_eroded, kernels.dilation_kern)
    img_dilated = dilation(img_dilated, kernels.erosion_kern)

    img_norm = equalize_hist(img_dilated)

    img_inv = np.interp(
        img_norm, (img_norm.min(),
                   img_norm.max()),
        (-dataset.parameters.top_dB, 0))
    img_gauss = gaussian_blur(img_inv.astype(float), 3)

    img_gauss_d = dereverberate_jit(
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
