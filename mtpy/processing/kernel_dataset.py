"""

This module contains a class for representing a dataset that can be processed.

Development Notes:
The KernelDataset could potentially be moved into mth5 or mtpy and used
as the dataset description for other processing flows.

Players on the stage:  One or more mth5s.

Each mth5 has a mth5_obj.channel_summary dataframe which tells what data are available.
Use a compressed view of this df with one line per acquisition run -- a "run_summary".

Run_summary provides options for the local and possibly remote reference stations.
Candidates for local station are the unique values in the station column.

For any candidate station, there are some integer n runs available.
This yields 2^n - 1 possible combinations that can be processed, neglecting any
flagging of time intervals within any run, or any joining of runs.
(There are actually 2**n, but we ignore the empty set, so -1)

Intuition suggests default ought to be to process n runs in n+1 configurations:
{all runs} + each run individually.  This will give a bulk answer, and bad runs can
be flagged by comparing them.  After an initial processing, the tfs can be reviewed
and the problematic runs can be addressed.

The user can interact with the run_summary_df, selecting sub dataframes via querying,
and in future maybe via some GUI (or a spreadsheet).

The intended usage process is as follows:
 0. Start with a list of mth5s
 1. Extract channel_summaries from each mth5 and join them vertically
 2. Compress to a run_summary
 3. Stare at the run_summary_df & Select a station "S" to process
 4. Select a non-empty set of runs for station "S"
 5. Select a remote reference "RR", (this is allowed to be None)
 6. Extract the sub-dataframe corresponding to acquisition_runs from "S" and "RR"
 7. If the remote is not None:
  - Drop the runs (rows) associated with RR that do not intersect with S
  - Restrict start/end times of RR runs that intersect with S so overlap is complete.
  - Restrict start/end times of S runs so that they intersect with remote
 8. This is now a TFKernel Dataset Definition (ish). Initialize a default processing
 object and pass it this df.
 ```
  >>> cc = ConfigCreator()
  >>> p = cc.create_from_kernel_dataset(kernel_dataset)
  - Optionally pass emtf_band_file=emtf_band_setup_file
 9. Edit the Processing Config appropriately,

TODO: Consider supporting a default value for 'channel_scale_factors' that is None,

TODO: As of March 2023 a RunSummary is available at the station level in mth5, but
 the aurora version is still being used.  This should be merged if possible so that
 aurora uses the built-in mth5 method. -- Run Summary exists atstation level in mth5

TODO: Might need to groupby survey & station, for now consider station_id  unique.

"""

# =============================================================================
# Imports
# =============================================================================
from pathlib import Path
import copy
from typing import Optional, Union

import pandas as pd
from loguru import logger

import mt_metadata.timeseries
from mt_metadata.utils.list_dict import ListDict

import mth5.timeseries.run_ts
from mth5.utils.helpers import initialize_mth5

from mtpy.processing.run_summary import RunSummary
from mtpy.processing import (
    KERNEL_DATASET_DTYPE,
    MINI_SUMMARY_COLUMNS,
)

# =============================================================================


class KernelDataset:
    """This class is intended to work with mth5-derived channel_summary or run_summary
    dataframes, that specify time series intervals.

    Development Notes:
    This class is closely related to (may actually be an extension of) RunSummary

    The main idea is to specify one or two stations, and a list of acquisition "runs"
    that can be merged into a "processing run". Each acquisition run can be further
    divided into non-overlapping chunks by specifying time-intervals associated with
    that acquisition run.  An empty iterable of time-intervals associated with a run
    is interpreted as the interval corresponding to the entire run.

    The time intervals can be used for several purposes but primarily:
    To specify contiguous chunks of data for:
    1. STFT, that will be made into merged FC data structures
    2. binding together into xarray time series, for eventual gap fill (and then STFT)
    3. managing and analyse the availability of reference time series

    The basic data structure can be represented as a table or as a tree:
    Station <-- run <-- [Intervals],

    This is described in issue #118 https://github.com/simpeg/aurora/issues/118

    Desired Properties
    a) This should be able to take a dictionary (tree) and return the tabular (
    DataFrame) representation and vice versa.
    b) When there are two stations, can apply interval intersection rules, so that
    only time intervals when both stations are acquiring data are kept

    From (a) above we can see that a simple table per station can
    represent the available data.  That table can be generated by default from
    the mth5, and intervals to exclude some data can be added as needed.

    (b) is really just the case of considering pairs of tables like (a)

    Question: To return a copy or modify in-place when querying.  Need to decide on
    standards and syntax.  Handling this in general is messy because every function
    needs to be modified.  Maybe better to use a decorator that allows for df kwarg
    to be passed, and if it is not passed the modification is done in place.
    The user who doesn't want to modify in place can work with a clone.
    """

    def __init__(
        self,
        df: Optional[Union[pd.DataFrame, None]] = None,
        local_station_id: Optional[str] = "",
        remote_station_id: Optional[Union[str, None]] = None,
        **kwargs,
    ):
        """Constructor.
        :param **kwargs:
        :param df: Option to pass an already formed dataframe.  Normally the df if built from a run_summary, defaults to None.
        :type df: Optional[Union[pd.DataFrame, None]], optional
        :param local_station_id: The local station for the dataset.  Normally this is passed via from_run_summary method, defaults to "".
        :type local_station_id: Optional[str], optional
        :param remote_station_id: The remote station for the dataset.  Normally this is passed via from_run_summary method, defaults to None.
        :type remote_station_id: Optional[Union[str, None]], optional
        """
        self.df = df
        self.local_station_id = local_station_id
        self.remote_station_id = remote_station_id
        self._mini_summary_columns = MINI_SUMMARY_COLUMNS
        self.survey_metadata = {}
        self.initialized = False
        self.local_mth5_obj = None
        self.remote_mth5_obj = None
        self._local_mth5_path = None
        self._remote_mth5_path = None

        for key, value in kwargs.items():
            setattr(self, key, value)

    def __str__(self):
        """Str function."""
        return str(self.mini_summary.head(None))

    def __repr__(self):
        """Repr function."""
        return self.__str__()

    @property
    def df(self):
        """Df function."""
        return self._df

    @df.setter
    def df(self, value: pd.DataFrame) -> None:
        """Make sure the data frame is set properly with proper column names.
        :param value: DESCRIPTION.
        :type value: pd.DataFrame
        :return: DESCRIPTION.
        :rtype: TYPE
        """
        if value is None:
            self._df = None
            return

        if not isinstance(value, pd.DataFrame):
            msg = f"Need to set df with a Pandas.DataFrame not type({type(value)})"
            logger.error(msg)

            raise TypeError(msg)

        self._df = self._add_duration_column(
            self._set_datetime_columns(self._add_columns(value)), inplace=False
        )

    def _has_df(self) -> bool:
        """Check to see if dataframe is set."""
        if self._df is not None:
            if not self._df.empty:
                return True
            return False
        return False

    def _df_has_local_station_id(self) -> bool:
        """Check to make sure the dataframe has the local station id.
        :return: DESCRIPTION.
        :rtype: bool
        """
        if self._has_df():
            return (self._df.station == self.local_station_id).any()

    def _df_has_remote_station_id(self) -> bool:
        """Check to make sure the dataframe has the local station id.
        :return: DESCRIPTION.
        :rtype: bool
        """
        if self._has_df():
            return (self._df.station == self.remote_station_id).any()

    def _set_datetime_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Be sure to set start and end to be date time objects."""

        try:
            df.start = pd.to_datetime(df.start, format="mixed")
            df.end = pd.to_datetime(df.end, format="mixed")
        except ValueError:
            df.start = pd.to_datetime(df.start)
            df.end = pd.to_datetime(df.end)

        return df

    def clone(self):
        """Return a deep copy."""
        return copy.deepcopy(self)

    def clone_dataframe(self) -> pd.DataFrame:
        """Return a deep copy of dataframe."""
        return copy.deepcopy(self.df)

    def _add_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add columns with appropriate dtypes."""

        for col, dtype in KERNEL_DATASET_DTYPE:
            if not col in df.columns:
                if col in ["survey", "station", "run", "start", "end"]:
                    raise ValueError(
                        f"{col} must be a filled column in the dataframe"
                    )
                try:
                    df[col] = dtype(0)
                except TypeError:
                    df[col] = None
                logger.info(
                    f"KernelDataset DataFrame needs column {col}, adding "
                    f"and setting dtype to {dtype}."
                )
        return df

    @property
    def local_station_id(self) -> str:
        """Local station id."""
        return self._local_station_id

    @local_station_id.setter
    def local_station_id(self, value: str) -> None:
        """Local station id."""
        if value is None:
            self._local_station_id = None
        else:
            try:
                self._local_station_id = str(value)
            except ValueError:
                raise ValueError(
                    f"Bad type {type(value)}. "
                    "Cannot convert local_station_id value to string."
                )
            if self._has_df():
                if not self._df_has_local_station_id():
                    raise NameError(
                        f"Could not find {self._local_station_id} in dataframe"
                    )

    @property
    def local_mth5_path(self) -> Path:
        """Local mth5 path.
        :return: Local station MTH5 path, a property extracted from the dataframe.
        :rtype: Path
        """
        if self._has_df():
            return Path(
                self._df.loc[
                    self._df.station == self.local_station_id, "mth5_path"
                ].unique()[0]
            )
        else:
            return self._local_mth5_path

    @local_mth5_path.setter
    def local_mth5_path(self, value: Union[str, Path]):
        """Local mth5 path."""
        self._local_mth5_path = self.set_path(value)

    def has_local_mth5(self) -> bool:
        """Test if local mth5 exists."""
        if self.local_mth5_path is None:
            return False
        else:
            return self.local_mth5_path.exists()

    @property
    def remote_station_id(self) -> str:
        """Remote station id."""
        return self._remote_station_id

    @remote_station_id.setter
    def remote_station_id(self, value: Union[str, Path]):
        """Remote station id."""
        if value is None:
            self._remote_station_id = None
        else:
            try:
                self._remote_station_id = str(value)
            except ValueError:
                raise ValueError(
                    f"Bad type {type(value)}. "
                    "Cannot convert remote_station_id value to string."
                )
            if self._has_df():
                if not self._df_has_remote_station_id():
                    raise NameError(
                        f"Could not find {self._remote_station_id} in dataframe"
                    )

    @property
    def remote_mth5_path(self) -> Path:
        """Remote mth5 path.
        :return: Remote station MTH5 path, a property extracted from the dataframe.
        :rtype: Path
        """
        if self._has_df() and self.remote_station_id is not None:
            return Path(
                self._df.loc[
                    self._df.station == self.remote_station_id, "mth5_path"
                ].unique()[0]
            )
        else:
            return self._remote_mth5_path

    @remote_mth5_path.setter
    def remote_mth5_path(self, value: Union[str, Path]):
        """Remote mth5 path."""
        self._remote_mth5_path = self.set_path(value)

    def has_remote_mth5(self) -> bool:
        """Test if remote mth5 exists."""
        if self.remote_mth5_path is None:
            return False
        else:
            return self.remote_mth5_path.exists()

    @property
    def processing_id(self) -> str:
        """Its difficult to come put with unique ids without crazy long names
        so this is a generic id of local-remote, the station metadata
        will have run information and the config parameters.
        """
        if self.remote_station_id is not None:
            return (
                f"{self.local_station_id}-rr_{self.remote_station_id}_"
                f"sr{int(self.sample_rate)}"
            )
        else:
            return f"{self.local_station_id}_sr{int(self.sample_rate)}"

    @property
    def input_channels(self) -> list:
        """Get input channels from data frame.
        :return: Input channels (sources).
        :rtype: list of strings
        """

        if self._has_df():
            return self.local_df.input_channels[0]

    @property
    def output_channels(self) -> list:
        """Get input channels from data frame.
        :return: Input channels (sources).
        :rtype: list of strings
        """

        if self._has_df():
            return self.local_df.output_channels[0]

    @property
    def local_df(self) -> pd.DataFrame:
        """Split data frame to just the local station runs.
        :return: Local station runs.
        :rtype: pd.DataFrame
        """

        if self._has_df():
            return self.df[self.df.station == self.local_station_id]

    @property
    def remote_df(self) -> pd.DataFrame:
        """Split data frame to just the local station runs.
        :return: Local station runs.
        :rtype: pd.DataFrame
        """

        if self._has_df() and self.remote_station_id is not None:
            return self.df[self.df.station == self.remote_station_id]

    @classmethod
    def set_path(self, value: Union[str, Path]) -> Path:
        """Set path."""
        return_path = None
        if value is not None:
            if isinstance(value, (str, Path)):
                return_path = Path(value)
                if not return_path.exists():
                    raise IOError(f"Cannot find file: {return_path}")
            else:
                raise ValueError(f"Cannot convert type{type(value)} to Path")

        return return_path

    def from_run_summary(
        self,
        run_summary: RunSummary,
        local_station_id: Optional[Union[str, None]] = None,
        remote_station_id: Optional[Union[str, None]] = None,
        sample_rate: Optional[Union[float, int, None]] = None,
    ) -> None:
        """Initialize the dataframe from a run summary.
        :param sample_rate:
            Defaults to None.
        :type sample_rate: Optional[Union[float, int, None]], optional
        :param run_summary: Summary of available data for processing from one or more stations.
        :type run_summary: RunSummary
        :param local_station_id: Label of the station for which an estimate will be computed, defaults to None.
        :type local_station_id: Optional[Union[str, None]], optional
        :param remote_station_id: Label of the remote reference station, defaults to None.
        :type remote_station_id: Optional[Union[str, None]], optional
        """
        if local_station_id is not None:
            self.local_station_id = local_station_id
        if remote_station_id is not None:
            self.remote_station_id = remote_station_id

        if sample_rate is not None:
            run_summary = run_summary.set_sample_rate(sample_rate)

        station_ids = [self.local_station_id]
        if self.remote_station_id:
            station_ids.append(self.remote_station_id)
        df = restrict_to_station_list(
            run_summary.df, station_ids, inplace=False
        )

        # Check df is non-empty
        if len(df) == 0:
            msg = f"Restricting run_summary df to {station_ids} yields an empty set"
            logger.critical(msg)
            raise ValueError(msg)

        # add columns column
        df = self._add_columns(df)

        # set remote reference
        if self.remote_station_id:
            cond = df.station == self.remote_station_id
            df.remote = cond

        # be sure to set date time columns and restrict to simultaneous runs
        df = self._set_datetime_columns(df)
        if self.remote_station_id:
            df = self.restrict_run_intervals_to_simultaneous(df)

        # Again check df is non-empty
        if len(df) == 0:
            msg = (
                f"Local: {self.local_station_id} and remote: "
                f"{self.remote_station_id} do not overlap. Remote reference "
                "processing not a valid option."
            )
            logger.error(msg)
            raise ValueError(msg)

        self.df = df

    @property
    def mini_summary(self) -> pd.DataFrame:
        """Return a dataframe that fits in terminal."""
        return self.df[self._mini_summary_columns]

    @property
    def local_survey_id(self) -> str:
        """Return string label for local survey id."""
        survey_id = self.df.loc[~self.df.remote].survey.unique()[0]
        if survey_id in ["none"]:
            survey_id = "0"
        return survey_id

    @property
    def local_survey_metadata(self) -> mt_metadata.timeseries.Survey:
        """Return survey metadata for local station."""
        try:
            return self.survey_metadata[self.local_survey_id]
        except KeyError:
            msg = f"Unexpected key {self.local_survey_id} not found in survey_metadata"
            msg += f"{msg} WARNING -- Maybe old MTH5 -- trying to use key '0'"
            logger.warning(msg)
            return self.survey_metadata["0"]

    def _add_duration_column(self, df, inplace=True) -> None:
        """Adds a column to self.df with times end-start (in seconds)."""

        timedeltas = df.end - df.start
        durations = [x.total_seconds() for x in timedeltas]
        if inplace:
            df["duration"] = durations
            return df
        else:
            new_df = df.copy()
            new_df["duration"] = durations
            return new_df

    def _update_duration_column(self, inplace=True) -> None:
        """Calls add_duration_column (after possible manual manipulation of start/end."""

        if inplace:
            self._df = self._add_duration_column(self._df, inplace)
        else:
            return self._add_duration_column(self._df, inplace)

    def drop_runs_shorter_than(
        self,
        minimum_duration: float,
        units: Optional[str] = "s",
        inplace: Optional[bool] = True,
    ) -> pd.DataFrame:
        """Drop runs from df that are inconsequentially short

        Development Notes:
        This needs to have duration refreshed before hand.
        :param inplace:
            Defaults to True.
        :type inplace: Optional[bool], optional
        :param minimum_duration: The minimum allowed duration for a run (in units of units).
        :type minimum_duration: float
        :param units: Placeholder to support units that are not seconds, defaults to "s".
        :type units: Optional[str], optional
        """
        if units != "s":
            msg = "Expected units are seconds : units='s'"
            raise NotImplementedError(msg)

        drop_cond = self.df.duration < minimum_duration
        if inplace:
            self._update_duration_column(inplace)
            self.df.drop(self.df[drop_cond].index, inplace=inplace)
            self.df.reset_index(drop=True, inplace=True)
            return
        else:
            new_df = self._update_duration_column(inplace)
            new_df = self.df.drop(self.df[drop_cond].index)
            new_df.reset_index(drop=True, inplace=True)
            return new_df

    def select_station_runs(
        self,
        station_runs_dict: dict,
        keep_or_drop: bool,
        inplace: Optional[bool] = True,
    ) -> pd.DataFrame:
        """Partition the rows of df based on the contents of station_runs_dict and return
        one of the two partitions (based on value of keep_or_drop).

        dict -> {station: [{run, start, end}]}

        For example {"mt01": ["0001", "0003"]}
        :param inplace:
            Defaults to True.
        :type inplace: Optional[bool], optional
        :param station_runs_dict: Keys are string ids of the stations to keep
            Values are lists of string labels for run_ids to keep.
        :type station_runs_dict: dict
        :param keep_or_drop: If "keep": returns df with only the station-runs specified in station_runs_dict
            If "drop": returns df with station_runs_dict excised.
        :type keep_or_drop: bool
        :param overwrite: If True, self.df is overwritten with the reduced dataframe.
        :type overwrite: bool
        :rtype: pd.DataFrame
        """

        for station_id, run_ids in station_runs_dict.items():
            if isinstance(run_ids, str):
                run_ids = [
                    run_ids,
                ]
            cond1 = self.df["station"] == station_id
            cond2 = self.df["run"].isin(run_ids)
            if keep_or_drop == "keep":
                drop_df = self.df[cond1 & ~cond2]
            else:
                drop_df = self.df[cond1 & cond2]

        if inplace:
            self.df.drop(drop_df.index, inplace=True)
            self.df.reset_index(drop=True, inplace=True)
        else:
            df = self.df.drop(drop_df.index, inplace=False)
            df = df.reset_index(drop=True, inplace=True)
            return df

    def set_run_times(
        self, run_time_dict: dict, inplace: Optional[bool] = True
    ):
        """Set run times from a dictionary formatted as {run_id: {start, end}}.
        :param run_time_dict: DESCRIPTION.
        :type run_time_dict: dict
        :param inplace: DESCRIPTION, defaults to True.
        :type inplace: Optional[bool], optional
        :return: DESCRIPTION.
        :rtype: TYPE
        """

        msg = "Need to set run time with a dictionary in the form of {run_id: {start, end}}"
        if not isinstance(run_time_dict, dict):
            raise TypeError(msg)

        for key, times in run_time_dict.items():
            if not isinstance(times, dict):
                raise TypeError(msg)
            if not "start" in times.keys() or "end" not in times.keys():
                raise KeyError(msg)

            cond1 = self.df.run == key
            cond2 = self.df.start <= times["start"]
            cond3 = self.df.end >= times["end"]
            self.df.loc[cond1 & cond2 & cond3, "start"] = times["start"]
            self.df.loc[cond1 & cond2 & cond3, "end"] = times["end"]
        self._update_duration_column()
        self.df = self.restrict_run_intervals_to_simultaneous(self.df)

    @property
    def is_single_station(self) -> bool:
        """Returns True if no RR station."""
        if self.local_station_id:
            if self.remote_station_id:
                return False
            else:
                return True
        else:
            return False

    def restrict_run_intervals_to_simultaneous(self, df: pd.DataFrame) -> None:
        """For each run in local_station_id check if it has overlap with other runs

        There is room for optimization here

        Note that you can wind up splitting runs here.  For example, in that case where
        local is running continuously, but remote is intermittent.  Then the local
        run may break into several chunks..
        :rtype: None
        """
        local_df = df[df.station == self.local_station_id]
        remote_df = df[df.station == self.remote_station_id]
        output_sub_runs = []
        for i_local, local_row in local_df.iterrows():
            for i_remote, remote_row in remote_df.iterrows():
                if intervals_overlap(
                    local_row.start,
                    local_row.end,
                    remote_row.start,
                    remote_row.end,
                ):
                    # print(f"OVERLAP {i_local}, {i_remote}")
                    olap_start, olap_end = overlap(
                        local_row.start,
                        local_row.end,
                        remote_row.start,
                        remote_row.end,
                    )

                    local_sub_run = local_row.copy(deep=True)
                    remote_sub_run = remote_row.copy(deep=True)
                    local_sub_run.start = olap_start
                    local_sub_run.end = olap_end
                    remote_sub_run.start = olap_start
                    remote_sub_run.end = olap_end
                    output_sub_runs.append(local_sub_run)
                    output_sub_runs.append(remote_sub_run)
                else:
                    pass
                    # print(f"NOVERLAP {i_local}, {i_remote}")
        new_df = pd.DataFrame(output_sub_runs)
        new_df = new_df.reset_index(drop=True)

        if new_df.empty:
            msg = (
                f"Local: {self.local_station_id} and "
                f"remote: {self.remote_station_id} do "
                f"not overlap, Remote reference processing not a valid option."
            )
            logger.error(msg)
            raise ValueError(msg)

        return new_df

    def get_station_metadata(
        self, local_station_id: str
    ) -> mt_metadata.timeseries.Station:
        """Returns the station metadata.

        Development Notes:
        TODO: This appears to be unused.  Was probably a precursor to the
          update_survey_metadata() method. Delete if unused. If used fill out doc:
        "Helper function for archiving the TF -- returns an object we can use to populate
        station metadata in the _____"
        :param local_station_id: The name of the local station.
        :type local_station_id: str
        :rtype: mt_metadata.timeseries.Station
        """
        # get a list of local runs:
        cond = self.df["station"] == local_station_id
        sub_df = self.df[cond]
        sub_df.drop_duplicates(subset="run", inplace=True)

        # sanity check:
        run_ids = sub_df.run.unique()
        assert len(run_ids) == len(sub_df)

        station_metadata = sub_df.mth5_obj[0].from_reference(
            sub_df.station_hdf5_reference[0]
        )
        station_metadata.runs = ListDict()
        for i, row in sub_df.iterrows():
            local_run_obj = self.get_run_object(row)
            station_metadata.add_run(local_run_obj.metadata)
        return station_metadata

    def get_run_object(
        self, index_or_row: Union[int, pd.Series]
    ) -> mt_metadata.timeseries.Run:
        """Gets the run object associated with a row of the df

        Development Notes:
        TODO: This appears to be unused except by get_station_metadata.
         Delete or integrate if desired.
         - This has likely been deprecated by direct calls to
         run_obj = row.mth5_obj.from_reference(row.run_hdf5_reference) in pipelines..
        :param index_or_row:
        :type index_or_row: Union[int, pd.Series]
        :return run_obj: The run associated with the row of the df.
        :rtype run_obj: mt_metadata.timeseries.Run
        """
        if isinstance(index_or_row, int):
            row = self.df.loc[index_or_row]
        else:
            row = index_or_row
        run_obj = row.mth5_obj.from_reference(row.run_hdf5_reference)
        return run_obj

    @property
    def num_sample_rates(self) -> int:
        """Returns the number of unique sample rates in the dataframe."""
        return len(self.df.sample_rate.unique())

    @property
    def sample_rate(self) -> float:
        r"""Returns the sample rate that of the data in the dataframe."""
        if self.num_sample_rates != 1:
            msg = "Aurora does not yet process data from mixed sample rates"
            logger.error(f"{msg}")
            raise NotImplementedError(msg)
        sample_rate = self.df.sample_rate.unique()[0]
        return sample_rate

    def update_survey_metadata(
        self, i: int, row: pd.Series, run_ts: mth5.timeseries.run_ts.RunTS
    ) -> None:
        """Wrangle survey_metadata into kernel_dataset.

        Development Notes:
        - The survey metadata needs to be passed to TF before exporting data.
        - This was factored out of initialize_dataframe_for_processing
        - TODO: It looks like we don't need to pass the whole run_ts, just its metadata
           There may be some performance implications to passing the whole object.
           Consider passing run_ts.survey_metadata, run_ts.run_metadata,
           run_ts.station_metadata only
        :param i: This would be the index of row, if we were sure that the dataframe was cleanly indexed.
        :type i: int
        :param row:
        :type row: pd.Series
        :param run_ts: Mth5 object having the survey_metadata.
        :type run_ts: mth5.timeseries.run_ts.RunTS
        :rtype: None
        """
        survey_id = run_ts.survey_metadata.id
        if i == 0:
            self.survey_metadata[survey_id] = run_ts.survey_metadata
        elif i > 0:
            if row.station in self.survey_metadata[survey_id].stations.keys():
                self.survey_metadata[survey_id].stations[row.station].add_run(
                    run_ts.run_metadata
                )
            else:
                self.survey_metadata[survey_id].add_station(
                    run_ts.station_metadata
                )
        if len(self.survey_metadata.keys()) > 1:
            raise NotImplementedError

    @property
    def mth5_objs(self):
        """Mth5 objs.
        :return: Dictionary [station_id: mth5_obj].
        :rtype: dict
        """
        mth5_obj_dict = {}
        mth5_obj_dict[self.local_station_id] = self.local_mth5_obj
        if self.remote_station_id is not None:
            mth5_obj_dict[self.remote_station_id] = self.remote_mth5_obj
        return mth5_obj_dict

    def initialize_mth5s(self, mode: Optional[str] = "r"):
        """Returns a dict of open mth5 objects, keyed by station_id

        A future version of this for multiple station processing may need
        nested dict with [survey_id][station].
        :return mth5_objs: Keyed by stations.
            local station id : mth5.mth5.MTH5
            remote station id: mth5.mth5.MTH5.
        :rtype mth5_objs: dict
        """
        self.local_mth5_obj = initialize_mth5(self.local_mth5_path, mode=mode)
        if self.remote_station_id:
            self.remote_mth5_obj = initialize_mth5(
                self.remote_mth5_path, mode="r"
            )

        self.initialized = True

        return self.mth5_objs

    def initialize_dataframe_for_processing(self) -> None:
        """Adds extra columns needed for processing to the dataframe.

        Populates them with mth5 objects, run_hdf5_reference, and xr.Datasets.

        Development Notes:
        Note #1: When assigning xarrays to dataframe cells, df dislikes xr.Dataset,
        so we convert to xr.DataArray before packing df

        Note #2: [OPTIMIZATION] By accessing the run_ts and packing the "run_dataarray" column of the df, we
         perform a non-lazy operation, and essentially forcing the entire decimation_level=0 dataset to be
         loaded into memory.  Seeking a lazy method to handle this maybe worthwhile.  For example, using
         a df.apply() approach to initialize only one row at a time would allow us to generate the FCs one
         row at a time and never ingest more than one run of data at a time ...

        Note #3: Uncommenting the continue statement here is desireable, will speed things up, but
         is not yet tested.  A nice test would be to have two stations, some runs having FCs built
         and others not having FCs built.  What goes wrong is in update_survey_metadata.
         Need a way to get the survey metadata from a run, not a run_ts if possible
        """

        self.add_columns_for_processing()

        for i, row in self.df.iterrows():
            run_obj = row.mth5_obj.get_run(
                row.station, row.run, survey=row.survey
            )
            self.df["run_hdf5_reference"].at[i] = run_obj.hdf5_group.ref

            if row.fc:
                msg = (
                    f"row {row} already has fcs prescribed by processing config"
                )
                msg += "-- skipping time series initialisation"
                logger.info(msg)
                # see Note #3
                # continue
            # the line below is not lazy, See Note #2
            run_ts = run_obj.to_runts(start=row.start, end=row.end)
            self.df["run_dataarray"].at[i] = run_ts.dataset.to_array("channel")

            self.update_survey_metadata(i, row, run_ts)

        logger.info("Dataset dataframe initialized successfully")

    def add_columns_for_processing(self) -> None:
        """Add columns to the dataframe used during processing.

        Development Notes:
        - This was originally in pipelines.
        - Q: Should mth5_objs be keyed by survey-station?
        - A: Yes, and ...
        since the KernelDataset dataframe will be iterated over, should probably
        write an iterator method.  This can iterate over survey-station tuples
        for multiple station processing.
        - Currently the model of keeping all these data objects "live" in the df
        seems to work OK, but is not well suited to HPC or lazy processing.
        :param mth5_objs: Keys are station_id, values are MTH5 objects.
        :type mth5_objs: dict,
        """
        if not self.initialized:
            raise ValueError("mth5 objects have not been initialized yet.")

        if self._has_df():
            self._df.loc[
                self._df.station == self.local_station_id, "mth5_obj"
            ] = self.local_mth5_obj
            if self.remote_station_id is not None:
                self._df.loc[
                    self._df.station == self.remote_station_id, "mth5_obj"
                ] = self.remote_mth5_obj

    def close_mth5s(self) -> None:
        """Loop over all unique mth5_objs in dataset df and make sure they are closed.+."""
        mth5_objs = self.df["mth5_obj"].unique()
        for mth5_obj in mth5_objs:
            mth5_obj.close_mth5()
        return


def restrict_to_station_list(
    df: pd.DataFrame,
    station_ids: Union[str, list],
    inplace: Optional[bool] = True,
) -> pd.DataFrame:
    """Drops all rows of run_summary dataframe where station_ids are NOT in
    the provided list of station_ids.  Operates on a deepcopy of self.df if a df
    isn't provided
    :param df: A run summary dataframer.
    :type df: pd.DataFrame
    :param station_ids: These are the station ids to keep, normally local and remote.
    :type station_ids: Union[str, list]
    :param inplace: If True, self.df is overwritten with the reduced dataframe, defaults to True.
    :type inplace: Optional[bool], optional
    :rtype: pd.DataFrame
    """
    if isinstance(station_ids, str):
        station_ids = [
            station_ids,
        ]
    if not inplace:
        df = copy.deepcopy(df)
    cond1 = ~df["station"].isin(station_ids)
    df.drop(df[cond1].index, inplace=True)
    df = df.reset_index(drop=True)
    return df


def _select_station_runs(
    df: pd.DataFrame,
    station_runs_dict: dict,
    keep_or_drop: bool,
    overwrite: Optional[bool] = True,
):
    """Partition the rows of df based on the contents of station_runs_dict and return
    one of the two partitions (based on value of keep_or_drop).
    :param df:
    :type df: pd.DataFrame
    :param station_runs_dict: Keys are string ids of the stations to keep
        Values are lists of string labels for run_ids to keep.
    :type station_runs_dict: dict
    :param keep_or_drop: If "keep": returns df with only the station-runs specified in station_runs_dict
        If "drop": returns df with station_runs_dict excised.
    :type keep_or_drop: bool
    :param overwrite: If True, self.df is overwritten with the reduced dataframe, defaults to True.
    :type overwrite: Optional[bool], optional
    """

    if not overwrite:
        df = copy.deepcopy(df)
    for station_id, run_ids in station_runs_dict.items():
        if isinstance(run_ids, str):
            run_ids = [
                run_ids,
            ]
        cond1 = df["station"] == station_id
        cond2 = df["run"].isin(run_ids)
        if keep_or_drop == "keep":
            drop_df = df[cond1 & ~cond2]
        else:
            drop_df = df[cond1 & cond2]

        df.drop(drop_df.index, inplace=True)
        df.reset_index(drop=True, inplace=True)
    return df


def intervals_overlap(
    start1: pd.Timestamp,
    end1: pd.Timestamp,
    start2: pd.Timestamp,
    end2: pd.Timestamp,
) -> bool:
    """Checks if intervals 1, and 2 overlap.

    Interval 1 is (start1, end1), Interval 2 is (start2, end2),

    Development Notes:
    This may work vectorized out of the box but has not been tested.
    Also, it is intended to work with pd.Timestamp objects, but should work
    for many objects that have an ordering associated.
    This website was used as a reference when writing the method:
    https://stackoverflow.com/questions/3721249/python-date-interval-intersection
    :param start1: Start of interval 1.
    :type start1: pd.Timestamp
    :param end1: End of interval 1.
    :type end1: pd.Timestamp
    :param start2: Start of interval 2.
    :type start2: pd.Timestamp
    :param end2: End of interval 2.
    :type end2: pd.Timestamp
    :return cond: True of the intervals overlap, False if they do now.
    :rtype cond: bool
    """
    cond = (start1 <= start2 <= end1) or (start2 <= start1 <= end2)
    return cond


def overlap(
    t1_start: pd.Timestamp,
    t1_end: pd.Timestamp,
    t2_start: pd.Timestamp,
    t2_end: pd.Timestamp,
) -> tuple:
    """Get the start and end times of the overlap between two intervals.

    Interval 1 is (start1, end1), Interval 2 is (start2, end2),

    Development Notes:
     Possibly some nicer syntax in this discussion:
     https://stackoverflow.com/questions/3721249/python-date-interval-intersection
     - Intended to work with pd.Timestamp objects, but should work for many objects
      that have an ordering associated.
    :param t1_start: The start of interval 1.
    :type t1_start: pd.Timestamp
    :param t1_end: The end of interval 1.
    :type t1_end: pd.Timestamp
    :param t2_start: The start of interval 2.
    :type t2_start: pd.Timestamp
    :param t2_end: The end of interval 2.
    :type t2_end: pd.Timestamp
    :return start, end: Start, end are either same type as input, or they are None,None.
    :rtype start, end: tuple
    """
    if t1_start <= t2_start <= t2_end <= t1_end:
        return t2_start, t2_end
    elif t1_start <= t2_start <= t1_end:
        return t2_start, t1_end
    elif t1_start <= t2_end <= t1_end:
        return t1_start, t2_end
    elif t2_start <= t1_start <= t1_end <= t2_end:
        return t1_start, t1_end
    else:
        return None, None