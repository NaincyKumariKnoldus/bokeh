#-----------------------------------------------------------------------------
# Copyright (c) 2012 - 2022, Anaconda, Inc., and Bokeh Contributors.
# All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
#-----------------------------------------------------------------------------
'''
Functions for helping with serialization and deserialization of
Bokeh objects.

Certain NumPy array dtypes can be serialized to a binary format for
performance and efficiency. The list of supported dtypes is:

{binary_array_types}

'''

#-----------------------------------------------------------------------------
# Boilerplate
#-----------------------------------------------------------------------------
from __future__ import annotations

import logging # isort:skip
log = logging.getLogger(__name__)

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

# Standard library imports
import base64
import datetime as dt
import uuid
from functools import lru_cache
from threading import Lock
from typing import (
    TYPE_CHECKING,
    Any,
    List,
    Literal,
    Set,
    Tuple,
    TypedDict,
)

# External imports
import numpy as np
from typing_extensions import TypeGuard

if TYPE_CHECKING:
    import numpy.typing as npt
    import pandas as pd

# Bokeh imports
from ..core.types import ID, Ref
from ..settings import settings
from .dependencies import import_optional
from .string import format_docstring

#-----------------------------------------------------------------------------
# Globals and constants
#-----------------------------------------------------------------------------

@lru_cache(None)
def _compute_datetime_types() -> Set[type]:
    result = {dt.time, dt.datetime, np.datetime64}
    pd = import_optional('pandas')
    if pd:
        result.add(pd.Timestamp)
        result.add(pd.Timedelta)
        result.add(pd.Period)
        result.add(type(pd.NaT))
    return result

def __getattr__(name: str) -> Any:
    if name == "DATETIME_TYPES":
        return _compute_datetime_types()
    raise AttributeError

BINARY_ARRAY_TYPES = {
    np.dtype(np.float32),
    np.dtype(np.float64),
    np.dtype(np.uint8),
    np.dtype(np.int8),
    np.dtype(np.uint16),
    np.dtype(np.int16),
    np.dtype(np.uint32),
    np.dtype(np.int32),
}

NP_EPOCH = np.datetime64(0, 'ms')
NP_MS_DELTA = np.timedelta64(1, 'ms')

DT_EPOCH = dt.datetime.utcfromtimestamp(0)

__doc__ = format_docstring(__doc__, binary_array_types="\n".join(f"* ``np.{x}``" for x in BINARY_ARRAY_TYPES))

__all__ = (
    'array_encoding_disabled',
    'convert_date_to_datetime',
    'convert_datetime_array',
    'convert_datetime_type',
    'convert_timedelta_type',
    'decode_base64_dict',
    'is_datetime_type',
    'is_timedelta_type',
    'make_globally_unique_id',
    'make_id',
    'transform_array',
    'transform_array_to_list',
    'transform_series',
)

#-----------------------------------------------------------------------------
# General API
#-----------------------------------------------------------------------------

ByteOrder = Literal["little", "big"]

class BufferJson(TypedDict):
    array: Ref | str
    shape: Tuple[int, ...]
    dtype: str
    order: ByteOrder

def is_datetime_type(obj: Any) -> TypeGuard[dt.time | dt.datetime | np.datetime64]:
    ''' Whether an object is any date, time, or datetime type recognized by
    Bokeh.

    Arg:
        obj (object) : the object to test

    Returns:
        bool : True if ``obj`` is a datetime type

    '''
    _dt_tuple = tuple(_compute_datetime_types())

    return isinstance(obj, _dt_tuple)

def is_timedelta_type(obj: Any) -> TypeGuard[dt.timedelta | np.timedelta64]:
    ''' Whether an object is any timedelta type recognized by Bokeh.

    Arg:
        obj (object) : the object to test

    Returns:
        bool : True if ``obj`` is a timedelta type

    '''
    return isinstance(obj, (dt.timedelta, np.timedelta64))

def convert_date_to_datetime(obj: dt.date) -> float:
    ''' Convert a date object to a datetime

    Args:
        obj (date) : the object to convert

    Returns:
        datetime

    '''
    return (dt.datetime(*obj.timetuple()[:6], tzinfo=None) - DT_EPOCH).total_seconds() * 1000

def convert_timedelta_type(obj: dt.timedelta | np.timedelta64) -> float:
    ''' Convert any recognized timedelta value to floating point absolute
    milliseconds.

    Arg:
        obj (object) : the object to convert

    Returns:
        float : milliseconds

    '''
    if isinstance(obj, dt.timedelta):
        return obj.total_seconds() * 1000.
    elif isinstance(obj, np.timedelta64):
        return float(obj / NP_MS_DELTA)

    raise ValueError(f"unknonw timedelta object: {obj!r}")

# The Any here should be pd.NaT | pd.Period but mypy chokes on that for some reason
def convert_datetime_type(obj: Any | pd.Timestamp | pd.Timedelta | dt.datetime | dt.date | dt.time | np.datetime64) -> float:
    ''' Convert any recognized date, time, or datetime value to floating point
    milliseconds since epoch.

    Arg:
        obj (object) : the object to convert

    Returns:
        float : milliseconds

    '''
    pd = import_optional('pandas')

    # Pandas NaT
    if pd and obj is pd.NaT:
        return np.nan

    # Pandas Period
    if pd and isinstance(obj, pd.Period):
        return obj.to_timestamp().value / 10**6.0

    # Pandas Timestamp
    if pd and isinstance(obj, pd.Timestamp):
        return obj.value / 10**6.0

    # Pandas Timedelta
    elif pd and isinstance(obj, pd.Timedelta):
        return obj.value / 10**6.0

    # Datetime (datetime is a subclass of date)
    elif isinstance(obj, dt.datetime):
        diff = obj.replace(tzinfo=None) - DT_EPOCH
        return diff.total_seconds() * 1000

    # XXX (bev) ideally this would not be here "dates are not datetimes"
    # Date
    elif isinstance(obj, dt.date):
        return convert_date_to_datetime(obj)

    # NumPy datetime64
    elif isinstance(obj, np.datetime64):
        epoch_delta = obj - NP_EPOCH
        return float(epoch_delta / NP_MS_DELTA)

    # Time
    elif isinstance(obj, dt.time):
        return (obj.hour * 3600 + obj.minute * 60 + obj.second) * 1000 + obj.microsecond / 1000.

    raise ValueError(f"unknown datetime object: {obj!r}")


def convert_datetime_array(array: npt.NDArray[Any]) -> npt.NDArray[np.floating[Any]]:
    ''' Convert NumPy datetime arrays to arrays to milliseconds since epoch.

    Args:
        array : (obj)
            A NumPy array of datetime to convert

            If the value passed in is not a NumPy array, it will be returned as-is.

    Returns:
        array

    '''

    if not isinstance(array, np.ndarray):
        return array

    # not quite correct, truncates to ms..
    if array.dtype.kind == 'M':
        return array.astype('datetime64[us]').astype('int64') / 1000.0

    elif array.dtype.kind == 'm':
        return array.astype('timedelta64[us]').astype('int64') / 1000.0

    # XXX (bev) special case dates, not great
    elif array.dtype.kind == 'O' and len(array) > 0 and isinstance(array[0], dt.date):
        try:
            return array.astype('datetime64[us]').astype('int64') / 1000.0
        except Exception:
            pass

    return array

def make_id() -> ID:
    ''' Return a new unique ID for a Bokeh object.

    Normally this function will return simple monotonically increasing integer
    IDs (as strings) for identifying Bokeh objects within a Document. However,
    if it is desirable to have globally unique for every object, this behavior
    can be overridden by setting the environment variable ``BOKEH_SIMPLE_IDS=no``.

    Returns:
        str

    '''
    global _simple_id

    if settings.simple_ids():
        with _simple_id_lock:
            _simple_id += 1
            return ID(str(_simple_id))
    else:
        return make_globally_unique_id()

def make_globally_unique_id() -> ID:
    ''' Return a globally unique UUID.

    Some situations, e.g. id'ing dynamically created Divs in HTML documents,
    always require globally unique IDs.

    Returns:
        str

    '''
    return ID(str(uuid.uuid4()))

def array_encoding_disabled(array: npt.NDArray[Any]) -> bool:
    ''' Determine whether an array may be binary encoded.

    The NumPy array dtypes that can be encoded are:

    {binary_array_types}

    Args:
        array (np.ndarray) : the array to check

    Returns:
        bool

    '''

    # disable binary encoding for non-supported dtypes
    return array.dtype not in BINARY_ARRAY_TYPES

array_encoding_disabled.__doc__ = format_docstring(
    array_encoding_disabled.__doc__,
    binary_array_types="\n    ".join(f"* ``np.{x}``" for x in BINARY_ARRAY_TYPES),
)

def transform_array(array: npt.NDArray[Any]) -> npt.NDArray[Any]:
    ''' Transform a ndarray into a serializable ndarray.

    Converts un-serializable dtypes and returns JSON serializable
    format

    Args:
        array (np.ndarray) : a NumPy array to be transformed

    Returns:
        ndarray

    '''
    array = convert_datetime_array(array)

    if isinstance(array, np.ma.MaskedArray):
        # Set masked values to nan
        array = array.filled(np.nan)  # type: ignore # filled is untyped
    if not array.flags["C_CONTIGUOUS"]:
        array = np.ascontiguousarray(array)
    return array

def transform_array_to_list(array: npt.NDArray[Any]) -> List[Any]:
    ''' Transforms a NumPy array into a list of values

    Args:
        array (np.nadarray) : the NumPy array series to transform

    Returns:
        list or dict

    '''
    pd = import_optional('pandas')

    if (array.dtype.kind in ('u', 'i', 'f') and (~np.isfinite(array)).any()):
        transformed = array.astype('object')
        transformed[np.isnan(array)] = 'NaN'
        transformed[np.isposinf(array)] = 'Infinity'
        transformed[np.isneginf(array)] = '-Infinity'
        return transformed.tolist()
    elif (array.dtype.kind == 'O' and pd and pd.isnull(array).any()):
        transformed = array.astype('object')
        transformed[pd.isnull(array)] = 'NaN'
        return transformed.tolist()
    return array.tolist()

def transform_series(series: pd.Series | pd.Index) -> npt.NDArray[Any]:
    ''' Transforms a Pandas series into serialized form

    Args:
        series (pd.Series) : the Pandas series to transform

    Returns:
        ndarray

    '''
    pd = import_optional('pandas')

    # not checking for pd here, this function should only be called if it
    # is already known that series is a Pandas Series type
    if isinstance(series, pd.PeriodIndex):
        vals = series.to_timestamp().values  # type: ignore # pandas PeriodIndex type is misunderstood somehow
    else:
        vals = series.values
    return vals

def decode_base64_dict(buffer: BufferJson) -> npt.NDArray[Any]:
    ''' Decode a base64 encoded array into a NumPy array.

    Args:
        data (dict) : encoded array data to decode

    Data should have the format encoded by :func:`encode_base64_dict`.

    Returns:
        np.ndarray

    '''
    data = buffer["array"]
    dtype = buffer["dtype"]
    shape = buffer["shape"]

    if isinstance(data, str):
        bytes = base64.b64decode(data)
    else:
        raise NotImplementedError("TODO")

    array = np.copy(np.frombuffer(bytes, dtype=dtype))  # type: ignore # from and frombuffer are untyped
    if len(shape) > 1:
        array = array.reshape(shape)
    return array

#-----------------------------------------------------------------------------
# Dev API
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Private API
#-----------------------------------------------------------------------------

_simple_id = 999
_simple_id_lock = Lock()

#-----------------------------------------------------------------------------
# Code
#-----------------------------------------------------------------------------
