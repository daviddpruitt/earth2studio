# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-FileCopyrightText: All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import pathlib
import shutil
from datetime import datetime, timedelta
from typing import Union

import fsspec
import gcsfs
import numpy as np
import xarray as xr
import zarr
from loguru import logger
from modulus.distributed.manager import DistributedManager
from tqdm import tqdm

from earth2studio.data.utils import prep_data_inputs
from earth2studio.lexicon import ARCOLexicon
from earth2studio.utils.type import TimeArray, VariableArray

LOCAL_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "earth2studio")


class ARCO:
    """Analysis-Ready, Cloud Optimized (ARCO) is a data store of ERA5 re-analysis data
    currated by Google. This data is stored in Zarr format and contains 31 surface and
    pressure level variables (for 37 pressure levels)  on a 0.25 degree lat lon grid.
    Temporal resolution is 1 hour.

    Parameters
    ----------
    cache : bool, optional
        Cache data source on local memory, by default True
    verbose : bool, optional
        Print download progress, by default True

    Warning
    -------
    This is a remote data source and can potentially download a large amount of data
    to your local machine for large requests.

    Note
    ----
    Additional information on the data repository can be referenced here:

    - https://cloud.google.com/storage/docs/public-datasets/era5
    """

    ARCO_LAT = np.linspace(90, -90, 721)
    ARCO_LON = np.linspace(0, 359.75, 1440)

    def __init__(self, cache: bool = True, verbose: bool = True):
        self._cache = cache
        self._verbose = verbose

        if self._cache:
            gcstore = fsspec.get_mapper(
                "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3",
                target_protocol="gs",
                cache_storage=self.cache,
                target_options={"anon": True, "default_block_size": 2**20},
            )
        else:
            gcs = gcsfs.GCSFileSystem(cache_timeout=-1)
            gcstore = gcsfs.GCSMap(
                "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3",
                gcs=gcs,
            )
        self.zarr_group = zarr.open(gcstore, mode="r")

    def initialize_zarr_cache(
        self,
        file_path: Union[str, pathlib.Path, fsspec.spec.AbstractFileSystem],
    ):
        """Initialize zarr cache for ARCO data source"""

        # Initialize zarr cache
        # Open in append mode to not overwrite existing data
        zarr_cache = zarr.open(file_path, mode="a")

        # Create time coordinate system
        if "time" not in list(zarr_cache.keys()):
            logger.debug(
                f"Initalizing time coordinate system for ARCO data source at {file_path}"
            )
            zarr_cache.create_dataset("time", data=self.zarr_group["time"]) # TODO: Maybe exand the time coordinate
            zarr_cache["time"].attrs["_ARRAY_DIMENSIONS"] = ["time"]
            zarr_cache["time"].attrs["calendar"] = "proleptic_gregorian"
            zarr_cache["time"].attrs["units"] = "hours since 1900-01-01 00:00:00"

        # Create longitude and latitude coordinate system
        if "latitude" not in list(zarr_cache.keys()):
            logger.debug(
                f"Initalizing latitude coordinate system for ARCO data source at {file_path}"
            )
            zarr_cache.create_dataset("latitude", data=self.zarr_group["latitude"])
            for key, value in self.zarr_group["latitude"].attrs.items():
                zarr_cache["latitude"].attrs[key] = value
        if "longitude" not in list(zarr_cache.keys()):
            logger.debug(
                f"Initalizing longitude coordinate system for ARCO data source at {file_path}"
            )
            zarr_cache.create_dataset("longitude", data=self.zarr_group["longitude"])
            for key, value in self.zarr_group["longitude"].attrs.items():
                zarr_cache["longitude"].attrs[key] = value

        # Consulidate all variables
        zarr.consolidate_metadata(file_path)

        return zarr_cache

    def fetch_array(
        self,
        time: datetime,
        variable: str,
    ) -> np.ndarray:
        """Function to get data.
        """

        # Determine arco variable and modifier
        try:
            arco_name, modifier = ARCOLexicon[variable]
        except KeyError as e:
            logger.error(f"variable id {variable} not found in ARCO lexicon")
            raise e

        # Get level coordinate from arco naming convention
        arco_variable, level = arco_name.split("::")

        # Get time index
        time_index = self._get_time_index(time)

        # special variables
        if variable == "tp06":
            return modifier(self._fetch_tp06(time))

        shape = self.zarr_group[arco_variable].shape
        # Static variables
        if len(shape) == 2:
            return modifier(self.zarr_group[arco_variable][:])
        # Surface variable
        elif len(shape) == 3:
            return modifier(self.zarr_group[arco_variable][time_index])
        # Atmospheric variable
        else:
            level_index = np.where(level_coords == int(level))[0][0]
            return modifier(
                self.zarr_group[arco_variable][time_index, level_index]
            )

    def fetch_cached_array(
        self,
        time: datetime,
        variable: str,
        zarr_cache: zarr.hierarchy.Group,
    ) -> np.ndarray:
        """ Reads data from zarr cache """

        # Get time index
        time_index = self._get_time_index(time)

        # Check if array is already in cache
        if (variable not in list(zarr_cache.keys())) or not zarr_cache[self._get_download_name(variable)][time_index]:
            array = self.fetch_array(time, variable)
            self.cache_array(array, time, variable, zarr_cache)
        else:
            logger.debug(
                f"Reading {variable} from cache at time {time.isoformat()}"
            ) # TODO: Remove this
            array = zarr_cache[variable][time_index]

        return array

    def cache_array(
        self,
        array: np.ndarray,
        time: datetime,
        variable: str,
        zarr_cache: zarr.hierarchy.Group = None,
    ) -> None:
        """
        Caches array in zarr cache
        """

        # Get time index
        time_index = self._get_time_index(time)

        # Check if variable exists in cache
        if variable not in list(zarr_cache.keys()):

            # Create dataset
            ds = zarr_cache.create_dataset(
                variable,
                shape=(len(self.zarr_group["time"]), *array.shape),
                chunks=(1, *array.shape),
                compressor=zarr.Blosc(cname="lz4", clevel=5, shuffle=zarr.Blosc.SHUFFLE, blocksize=0),
                dtype=array.dtype,
            )
            ds.attrs["_ARRAY_DIMENSIONS"] = ["time", "latitude", "longitude"]

            # Create download marker
            logger.debug(
                f"len(self.zarr_group['time']): {len(self.zarr_group['time'])}"
            )
            ds = zarr_cache.create_dataset(
                self._get_download_name(variable),
                data=np.zeros(len(self.zarr_group["time"]), dtype=bool),
                chunks=(1024,), # TODO: This might break things
                dtype=bool,
            )
            ds.attrs["_ARRAY_DIMENSIONS"] = ["time"]

        # Cache array
        zarr_cache[variable][time_index] = array

        # Mark as downloaded
        zarr_cache[self._get_download_name(variable)][time_index] = True

    def __call__(
        self,
        time: datetime | list[datetime] | TimeArray,
        variable: str | list[str] | VariableArray,
        zarr_cache: zarr.hierarchy.Group = None,
    ) -> xr.DataArray:
        """Function to get data.

        Parameters
        ----------
        time : datetime | list[datetime] | TimeArray
            Timestamps to return data for (UTC).
        variable : str | list[str] | VariableArray
            String, list of strings or array of strings that refer to variables to
            return. Must be in the ARCO lexicon.

        Returns
        -------
        xr.DataArray
            ERA5 weather data array from ARCO
        """

        # Prepare time and variable inputs
        time, variable = prep_data_inputs(time, variable)

        # Create cache dir if doesnt exist
        pathlib.Path(self.cache).mkdir(parents=True, exist_ok=True)

        # Make sure input time is valid
        self._validate_time(time)

        # Fetch index file for requested time
        data_arrays = []
        for t0 in time:
            data_array = self.fetch_arco_dataarray(t0, variable, zarr_cache)
            data_arrays.append(data_array)

        # Delete cache if needed
        if not self._cache:
            shutil.rmtree(self.cache)

        return xr.concat(data_arrays, dim="time")

    def fetch_arco_dataarray(
        self,
        time: datetime,
        variables: list[str],
        zarr_cache: zarr.hierarchy.Group = None,
    ) -> xr.DataArray:
        """Retrives ARCO data array for given date time by downloading a lat lon array
        from the Zarr store

        Parameters
        ----------
        time : datetime
            Date time to fetch
        variables : list[str]
            list of atmosphric variables to fetch. Must be supported in ARCO lexicon

        Returns
        -------
        xr.DataArray
            ARCO data array for given date time
        """
        arcoda = xr.DataArray(
            data=np.empty((1, len(variables), len(self.ARCO_LAT), len(self.ARCO_LON))),
            dims=["time", "variable", "lat", "lon"],
            coords={
                "time": [time],
                "variable": variables,
                "lat": self.ARCO_LAT,
                "lon": self.ARCO_LON,
            },
        )

        # Load levels coordinate system from Zarr store and check
        level_coords = self.zarr_group["level"][:]

        # TODO: Add MP here
        for i, variable in enumerate(
            tqdm(
                variables, desc=f"Fetching ARCO for {time}", disable=(not self._verbose)
            )
        ):
            logger.debug(
                f"Fetching ARCO zarr array for variable: {variable} at {time.isoformat()}"
            )

            # Fetch variable from ARCO
            if zarr_cache is None:
                arcoda[0, i] = self.fetch_array(time, variable)
            else:
                arcoda[0, i] = self.fetch_cached_array(time, variable, zarr_cache)

        return arcoda

    def _fetch_tp06(self, time: datetime) -> np.array:
        """Handle special tp06 variable"""
        tp06_array = np.zeros((self.ARCO_LAT.shape[0], self.ARCO_LON.shape[0]))
        # Accumulate over past 6 hours
        for i in range(6):
            time_index = self._get_time_index(time - timedelta(hours=i))
            tp06_array += self.zarr_group["total_precipitation"][time_index]

        return tp06_array

    @property
    def cache(self) -> str:
        """Get the appropriate cache location."""
        cache_location = os.path.join(LOCAL_CACHE, "arco")
        if not self._cache:
            cache_location = os.path.join(
                cache_location, f"tmp_{DistributedManager().rank}"
            )
        return cache_location

    @classmethod
    def _validate_time(cls, times: list[datetime]) -> None:
        """Verify if date time is valid for ARCO

        Parameters
        ----------
        times : list[datetime]
            list of date times to fetch data
        """
        for time in times:
            if not (time - datetime(1900, 1, 1)).total_seconds() % 3600 == 0:
                raise ValueError(
                    f"Requested date time {time} needs to be 1 hour interval for ARCO"
                )

            if time < datetime(year=1940, month=1, day=1):
                raise ValueError(
                    f"Requested date time {time} needs to be after January 1st, 1940 for ARCO"
                )

            if time >= datetime(year=2023, month=11, day=10):
                raise ValueError(
                    f"Requested date time {time} needs to be before November 10th, 2023 for ARCO"
                )

    @classmethod
    def _get_time_index(cls, time: datetime) -> int:
        """Small little index converter to go from datetime to integer index.
        We don't need to do with with xarray, but since we are vanilla zaar for speed
        this conversion must be manual.

        Parameters
        ----------
        time : datetime
            Input date time

        Returns
        -------
        int
            Time coordinate index of ARCO data
        """
        start_date = datetime(year=1900, month=1, day=1)
        duration = time - start_date
        return int(divmod(duration.total_seconds(), 3600)[0])

    @classmethod
    def available(cls, time: datetime | np.datetime64) -> bool:
        """Checks if given date time is avaliable in the ARCO data source

        Parameters
        ----------
        time : datetime | np.datetime64
            Date time to access

        Returns
        -------
        bool
            If date time is avaiable
        """
        if isinstance(time, np.datetime64):  # np.datetime64 -> datetime
            _unix = np.datetime64(0, "s")
            _ds = np.timedelta64(1, "s")
            time = datetime.utcfromtimestamp((time - _unix) / _ds)

        # Offline checks
        try:
            cls._validate_time([time])
        except ValueError:
            return False

        gcs = gcsfs.GCSFileSystem(cache_timeout=-1)
        gcstore = gcsfs.GCSMap(
            "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3",
            gcs=gcs,
        )
        zarr_group = zarr.open(gcstore, mode="r")
        # Load time coordinate system from Zarr store and check
        time_index = cls._get_time_index(time)
        max_index = zarr_group["time"][-1]
        return time_index >= 0 and time_index <= max_index

    @classmethod
    def _get_download_name(cls, variable: str) -> str:
        """Get the download name for a given time, this is used to cache data"""
        return f"DOWNLOADED_{variable}"
