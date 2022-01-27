from datetime import datetime, timedelta
import os
import sys
from typing import List, Union
import numpy as np
import pandas as pd
from pathlib import Path
from tqdm import tqdm
import glob


def get_boxes_data(YEAR: str, ORIGIN_DIR: Union[str, Path],
                   RESOURCES_DIR: Union[str, Path],
                   days_before_laydate: Union[int, None] = None):
    """Returns a dataframe with brood information for each nestbox that was
    recorded within a specified time window. Prints some basic information about
    sample sizes.

    Args:
        YEAR (str): Year of data to extract
        ORIGIN_DIR (PosixPath): Location of sound files to use
        RESOURCES_DIR (PosixPath): Path to 'resources' folder in the project; 
            assumes `RESOURCES_DIR / "brood_data" / YEAR / *.csv` exists.
        days_before_laydate (int, optional): Maximum difference allowed between
        first day of recording and first egg allowed. In days, defaults to None.

    Returns:
        final_df (pd.DataFrame): Those that laid eggs and were recorded a
        maximum of 10 days before the first egg was laid
        full_df (pd.DataFrame): Every box that was recorded
    """

    filelist: np.ndarray = np.sort(list(ORIGIN_DIR.glob("**/*.WAV")))
    recorded_nestboxes = set([file.parent.name for file in filelist])
    ebmp_df, breeding_attempts = get_ebmp_data(YEAR, RESOURCES_DIR)

    # Get only those boxes that were recorded
    ebmp_df = ebmp_df[ebmp_df['Nestbox'].isin(recorded_nestboxes)]
    dates_df = get_recorded_dates_df(filelist)

    # Join both dataframes
    full_df = pd.merge(ebmp_df, dates_df, left_on='Nestbox', right_on='index')

    variables = [  # Variables to keep in dataframe
        'Nestbox', 'first_date', 'Lay date',
        'last_date', 'Clear date', 'Species',
        'April lay date', 'Incubation started',
        'Hatch date', 'Clutch size', 'Num chicks',
        'Num fledglings', 'Mean chick weight', 'Father',
        'Mother'
    ]

    if days_before_laydate is not None:

        # NOTE: This is not a particularly good way of doing this. So,
        # TODO: Make get_recorded_dates_df() get all dates and decide based on this;
        # the first/last recording dates are not representative when a nestbox
        # was recorded more than once

        # Make a boolean mask
        mask = (
            full_df['Lay date'] + timedelta(days=days_before_laydate) >
            full_df['first_date']) & (
            full_df['last_date'] < full_df['Clear date'])

        # Select the sub-DataFrame:
        final_df = full_df.loc[mask][variables]
        recorded_within_window = len(final_df)

    # Print info
    print(f"\nYou recorded a total of {(len(filelist))} hours of audio.\n"
          f"You recorded {len(ebmp_df)} out of a total of {breeding_attempts} "
          "breeding attempts this year")
    if days_before_laydate is not None:
        print(f"Of those, {recorded_within_window} laid eggs and were recorded "
              "a maximum of 10 days before the first egg was laid")
        return final_df  # Those that laid eggs and were recorded a maximum of n days before the first egg was laid

    return full_df[variables]  # Every box that was recorded


def get_recorded_dates_df(filelist):
    # Get first and last recorded files per nestbox
    recorded_nestboxes = set([file.parent.name for file in filelist])
    times_dict = {}
    for box in tqdm(recorded_nestboxes,
                    desc="Getting recording dates",
                    leave=True, position=0,
                    file=sys.stdout):
        dt_list = [datetime.strptime(
            file.stem, "%Y%m%d_%H%M%S") for file in filelist
            if file.parents[0].name == box]
        times_dict[box] = [dt_list[0], dt_list[-1]]

    dates_df = pd.DataFrame.from_dict(times_dict, orient='index', columns=[
        "first_date", "last_date"]).reset_index(level=0)

    return dates_df


def get_ebmp_data(YEAR, RESOURCES_DIR):
    # Import the latest brood data downloaded from https://ebmp.zoo.ox.ac.uk/broods
    brood_data_path = RESOURCES_DIR / "brood_data" / YEAR
    list_of_files = glob.glob(os.fspath(brood_data_path) + "/*.csv")
    latest_file = max(list_of_files, key=os.path.getctime)

    # Read csv
    ebmp_df = pd.read_csv(latest_file).query('Species == "g"')
    breeding_attempts = len(ebmp_df)
    ebmp_df.insert(1, "Nestbox", ebmp_df["Pnum"].str[5:])
    ebmp_df.drop(list(ebmp_df.filter(regex='Legacy')), axis=1, inplace=True)

    # Ensure dates are dtype datetime64[ns]:
    ebmp_df[["Clear date", "Lay date"]] = ebmp_df[[
        "Clear date", "Lay date"]].apply(pd.to_datetime, format="%d-%m-%Y")

    return ebmp_df, breeding_attempts