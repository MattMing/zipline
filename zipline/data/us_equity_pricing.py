# Copyright 2015 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from abc import ABCMeta, abstractmethod, abstractproperty
from errno import ENOENT
from functools import partial
from os import remove
import sqlite3
import warnings

from bcolz import (
    carray,
    ctable,
)
from collections import namedtuple
import logbook
import numpy as np
from numpy import (
    array,
    int64,
    float64,
    full,
    iinfo,
    integer,
    issubdtype,
    nan,
    uint32,
    zeros,
)
from pandas import (
    DataFrame,
    DatetimeIndex,
    read_csv,
    Timestamp,
    NaT,
    isnull,
)
from pandas.tslib import iNaT
from six import (
    iteritems,
    with_metaclass,
    viewkeys,
)

from zipline.utils.functional import apply
from zipline.utils.preprocess import call
from zipline.utils.input_validation import (
    coerce_string,
    preprocess,
    expect_element,
    verify_indices_all_unique,
)
from zipline.utils.sqlite_utils import group_into_chunks
from zipline.utils.memoize import lazyval
from zipline.utils.cli import maybe_show_progress
from ._equities import _compute_row_slices, _read_bcolz_data
from ._adjustments import load_adjustments_from_sqlite


logger = logbook.Logger('UsEquityPricing')

OHLC = frozenset(['open', 'high', 'low', 'close'])
US_EQUITY_PRICING_BCOLZ_COLUMNS = (
    'open', 'high', 'low', 'close', 'volume', 'day', 'id'
)
SQLITE_ADJUSTMENT_COLUMN_DTYPES = {
    'effective_date': integer,
    'ratio': float,
    'sid': integer,
}
SQLITE_ADJUSTMENT_TABLENAMES = frozenset(['splits', 'dividends', 'mergers'])

SQLITE_DIVIDEND_PAYOUT_COLUMN_DTYPES = {
    'sid': integer,
    'ex_date': integer,
    'declared_date': integer,
    'record_date': integer,
    'pay_date': integer,
    'amount': float,
}

SQLITE_STOCK_DIVIDEND_PAYOUT_COLUMN_DTYPES = {
    'sid': integer,
    'ex_date': integer,
    'declared_date': integer,
    'record_date': integer,
    'pay_date': integer,
    'payment_sid': integer,
    'ratio': float,
}
UINT32_MAX = iinfo(uint32).max


class NoDataOnDate(Exception):
    """
    Raised when a spot price can be found for the sid and date.
    """
    pass


def check_uint32_safe(value, colname):
    if value >= UINT32_MAX:
        raise ValueError(
            "Value %s from column '%s' is too large" % (value, colname)
        )


@expect_element(invalid_data_behavior={'warn', 'raise', 'ignore'})
def winsorise_uint32(df, invalid_data_behavior, column, *columns):
    """Drops any record where a value would not fit into a uint32.

    Parameters
    ----------
    df : pd.DataFrame
        The dataframe to winsorise.
    invalid_data_behavior : {'warn', 'raise', 'ignore'}
        What to do when data is outside the bounds of a uint32.
    *columns : iterable[str]
        The names of the columns to check.

    Returns
    -------
    truncated : pd.DataFrame
        ``df`` with values that do not fit into a uint32 zeroed out.
    """
    columns = list((column,) + columns)
    mask = df[columns] > UINT32_MAX

    if invalid_data_behavior != 'ignore':
        mask |= df[columns].isnull()
    else:
        # we are not going to generate a warning or error for this so just use
        # nan_to_num
        df[columns] = np.nan_to_num(df[columns])

    mv = mask.values
    if mv.any():
        if invalid_data_behavior == 'raise':
            raise ValueError(
                '%d values out of bounds for uint32: %r' % (
                    mv.sum(), df[mask.any(axis=1)],
                ),
            )
        if invalid_data_behavior == 'warn':
            warnings.warn(
                'Ignoring %d values because they are out of bounds for'
                ' uint32: %r' % (
                    mv.sum(), df[mask.any(axis=1)],
                ),
                stacklevel=3,  # one extra frame for `expect_element`
            )

    df[mask] = 0
    return df


@expect_element(invalid_data_behavior={'warn', 'raise', 'ignore'})
def to_ctable(raw_data, invalid_data_behavior):
    if isinstance(raw_data, ctable):
        # we already have a ctable so do nothing
        return raw_data

    winsorise_uint32(raw_data, invalid_data_behavior, 'volume', *OHLC)
    processed = (raw_data[list(OHLC)] * 1000).astype('uint32')
    dates = raw_data.index.values.astype('datetime64[s]')
    check_uint32_safe(dates.max().view(np.int64), 'day')
    processed['day'] = dates.astype('uint32')
    processed['volume'] = raw_data.volume.astype('uint32')
    return ctable.fromdataframe(processed)


class BcolzDailyBarWriter(object):
    """
    Class capable of writing daily OHLCV data to disk in a format that can be
    read efficiently by BcolzDailyOHLCVReader.

    Parameters
    ----------
    filename : str
        The location at which we should write our output.
    calendar : pandas.DatetimeIndex
        Calendar to use to compute asset calendar offsets.

    See Also
    --------
    zipline.data.us_equity_pricing.BcolzDailyBarReader
    """
    _csv_dtypes = {
        'open': float64,
        'high': float64,
        'low': float64,
        'close': float64,
        'volume': float64,
    }

    def __init__(self, filename, calendar):
        self._filename = filename
        self._calendar = calendar

    @property
    def progress_bar_message(self):
        return "Merging daily equity files:"

    def progress_bar_item_show_func(self, value):
        return value if value is None else str(value[0])

    def write(self,
              data,
              assets=None,
              show_progress=False,
              invalid_data_behavior='warn'):
        """
        Parameters
        ----------
        data : iterable[tuple[int, pandas.DataFrame or bcolz.ctable]]
            The data chunks to write. Each chunk should be a tuple of sid
            and the data for that asset.
        assets : set[int], optional
            The assets that should be in ``data``. If this is provided
            we will check ``data`` against the assets and provide better
            progress information.
        show_progress : bool, optional
            Whether or not to show a progress bar while writing.
        invalid_data_behavior : {'warn', 'raise', 'ignore'}, optional
            What to do when data is encountered that is outside the range of
            a uint32.

        Returns
        -------
        table : bcolz.ctable
            The newly-written table.
        """
        ctx = maybe_show_progress(
            ((sid, to_ctable(df, invalid_data_behavior)) for sid, df in data),
            show_progress=show_progress,
            item_show_func=self.progress_bar_item_show_func,
            label=self.progress_bar_message,
            length=len(assets) if assets is not None else None,
        )
        with ctx as it:
            return self._write_internal(it, assets)

    def write_csvs(self,
                   asset_map,
                   show_progress=False,
                   invalid_data_behavior='warn'):
        """Read CSVs as DataFrames from our asset map.

        Parameters
        ----------
        asset_map : dict[int -> str]
            A mapping from asset id to file path with the CSV data for that
            asset
        show_progress : bool
            Whether or not to show a progress bar while writing.
        invalid_data_behavior : {'warn', 'raise', 'ignore'}
            What to do when data is encountered that is outside the range of
            a uint32.
        """
        read = partial(
            read_csv,
            parse_dates=['day'],
            index_col='day',
            dtype=self._csv_dtypes,
        )
        return self.write(
            ((asset, read(path)) for asset, path in iteritems(asset_map)),
            assets=viewkeys(asset_map),
            show_progress=show_progress,
            invalid_data_behavior=invalid_data_behavior,
        )

    def _write_internal(self, iterator, assets):
        """
        Internal implementation of write.

        `iterator` should be an iterator yielding pairs of (asset, ctable).
        """
        total_rows = 0
        first_row = {}
        last_row = {}
        calendar_offset = {}

        # Maps column name -> output carray.
        columns = {
            k: carray(array([], dtype=uint32))
            for k in US_EQUITY_PRICING_BCOLZ_COLUMNS
        }

        earliest_date = None
        calendar = self._calendar

        if assets is not None:
            @apply
            def iterator(iterator=iterator, assets=set(assets)):
                for asset_id, table in iterator:
                    if asset_id not in assets:
                        raise ValueError('unknown asset id %r' % asset_id)
                    yield asset_id, table

        for asset_id, table in iterator:
            nrows = len(table)
            for column_name in columns:
                if column_name == 'id':
                    # We know what the content of this column is, so don't
                    # bother reading it.
                    columns['id'].append(
                        full((nrows,), asset_id, dtype='uint32'),
                    )
                    continue

                columns[column_name].append(table[column_name])

            if earliest_date is None:
                earliest_date = table["day"][0]
            else:
                earliest_date = min(earliest_date, table["day"][0])

            # Bcolz doesn't support ints as keys in `attrs`, so convert
            # assets to strings for use as attr keys.
            asset_key = str(asset_id)

            # Calculate the index into the array of the first and last row
            # for this asset. This allows us to efficiently load single
            # assets when querying the data back out of the table.
            first_row[asset_key] = total_rows
            last_row[asset_key] = total_rows + nrows - 1
            total_rows += nrows

            # Calculate the number of trading days between the first date
            # in the stored data and the first date of **this** asset. This
            # offset used for output alignment by the reader.
            asset_first_day = table['day'][0]
            calendar_offset[asset_key] = calendar.get_loc(
                Timestamp(asset_first_day, unit='s', tz='UTC'),
            )

        # This writes the table to disk.
        full_table = ctable(
            columns=[
                columns[colname]
                for colname in US_EQUITY_PRICING_BCOLZ_COLUMNS
            ],
            names=US_EQUITY_PRICING_BCOLZ_COLUMNS,
            rootdir=self._filename,
            mode='w',
        )

        full_table.attrs['first_trading_day'] = (
            earliest_date if earliest_date is not None else iNaT
        )
        full_table.attrs['first_row'] = first_row
        full_table.attrs['last_row'] = last_row
        full_table.attrs['calendar_offset'] = calendar_offset
        full_table.attrs['calendar'] = calendar.asi8.tolist()
        full_table.flush()
        return full_table


class DailyBarReader(with_metaclass(ABCMeta)):
    """
    Reader for OHCLV pricing data at a daily frequency.
    """
    @abstractmethod
    def load_raw_arrays(self, columns, start_date, end_date, assets):
        pass

    @abstractmethod
    def spot_price(self, sid, day, colname):
        pass

    @abstractproperty
    def last_available_dt(self):
        pass


class BcolzDailyBarReader(DailyBarReader):
    """
    Reader for raw pricing data written by BcolzDailyOHLCVWriter.

    Parameters
    ----------
    table : bcolz.ctable
        The ctable contaning the pricing data, with attrs corresponding to the
        Attributes list below.
    read_all_threshold : int
        The number of equities at which; below, the data is read by reading a
        slice from the carray per asset.  above, the data is read by pulling
        all of the data for all assets into memory and then indexing into that
        array for each day and asset pair.  Used to tune performance of reads
        when using a small or large number of equities.

    Attributes
    ----------
    The table with which this loader interacts contains the following
    attributes:

    first_row : dict
        Map from asset_id -> index of first row in the dataset with that id.
    last_row : dict
        Map from asset_id -> index of last row in the dataset with that id.
    calendar_offset : dict
        Map from asset_id -> calendar index of first row.
    calendar : list[int64]
        Calendar used to compute offsets, in asi8 format (ns since EPOCH).

    We use first_row and last_row together to quickly find ranges of rows to
    load when reading an asset's data into memory.

    We use calendar_offset and calendar to orient loaded blocks within a
    range of queried dates.

    Notes
    ------
    A Bcolz CTable is comprised of Columns and Attributes.
    The table with which this loader interacts contains the following columns:

    ['open', 'high', 'low', 'close', 'volume', 'day', 'id'].

    The data in these columns is interpreted as follows:

    - Price columns ('open', 'high', 'low', 'close') are interpreted as 1000 *
      as-traded dollar value.
    - Volume is interpreted as as-traded volume.
    - Day is interpreted as seconds since midnight UTC, Jan 1, 1970.
    - Id is the asset id of the row.

    The data in each column is grouped by asset and then sorted by day within
    each asset block.

    The table is built to represent a long time range of data, e.g. ten years
    of equity data, so the lengths of each asset block is not equal to each
    other. The blocks are clipped to the known start and end date of each asset
    to cut down on the number of empty values that would need to be included to
    make a regular/cubic dataset.

    When read across the open, high, low, close, and volume with the same
    index should represent the same asset and day.

    See Also
    --------
    zipline.data.us_equity_pricing.BcolzDailyBarWriter
    """
    def __init__(self, table, read_all_threshold=3000):
        self._maybe_table_rootdir = table
        # Cache of fully read np.array for the carrays in the daily bar table.
        # raw_array does not use the same cache, but it could.
        # Need to test keeping the entire array in memory for the course of a
        # process first.
        self._spot_cols = {}
        self.PRICE_ADJUSTMENT_FACTOR = 0.001
        self._read_all_threshold = read_all_threshold

    @lazyval
    def _table(self):
        maybe_table_rootdir = self._maybe_table_rootdir
        if isinstance(maybe_table_rootdir, ctable):
            return maybe_table_rootdir
        return ctable(rootdir=maybe_table_rootdir, mode='r')

    @lazyval
    def _calendar(self):
        return DatetimeIndex(self._table.attrs['calendar'], tz='UTC')

    @lazyval
    def _first_rows(self):
        return {
            int(asset_id): start_index
            for asset_id, start_index in iteritems(
                self._table.attrs['first_row'],
            )
        }

    @lazyval
    def _last_rows(self):
        return {
            int(asset_id): end_index
            for asset_id, end_index in iteritems(
                self._table.attrs['last_row'],
            )
        }

    @lazyval
    def _calendar_offsets(self):
        return {
            int(id_): offset
            for id_, offset in iteritems(
                self._table.attrs['calendar_offset'],
            )
        }

    @lazyval
    def first_trading_day(self):
        try:
            return Timestamp(
                self._table.attrs['first_trading_day'],
                unit='s',
                tz='UTC'
            )
        except KeyError:
            return None

    @property
    def last_available_dt(self):
        return self._calendar[-1]

    def _compute_slices(self, start_idx, end_idx, assets):
        """
        Compute the raw row indices to load for each asset on a query for the
        given dates after applying a shift.

        Parameters
        ----------
        start_idx : int
            Index of first date for which we want data.
        end_idx : int
            Index of last date for which we want data.
        assets : pandas.Int64Index
            Assets for which we want to compute row indices

        Returns
        -------
        A 3-tuple of (first_rows, last_rows, offsets):
        first_rows : np.array[intp]
            Array with length == len(assets) containing the index of the first
            row to load for each asset in `assets`.
        last_rows : np.array[intp]
            Array with length == len(assets) containing the index of the last
            row to load for each asset in `assets`.
        offset : np.array[intp]
            Array with length == (len(asset) containing the index in a buffer
            of length `dates` corresponding to the first row of each asset.

            The value of offset[i] will be 0 if asset[i] existed at the start
            of a query.  Otherwise, offset[i] will be equal to the number of
            entries in `dates` for which the asset did not yet exist.
        """
        # The core implementation of the logic here is implemented in Cython
        # for efficiency.
        return _compute_row_slices(
            self._first_rows,
            self._last_rows,
            self._calendar_offsets,
            start_idx,
            end_idx,
            assets,
        )

    def load_raw_arrays(self, columns, start_date, end_date, assets):
        # Assumes that the given dates are actually in calendar.
        start_idx = self._calendar.get_loc(start_date)
        end_idx = self._calendar.get_loc(end_date)
        first_rows, last_rows, offsets = self._compute_slices(
            start_idx,
            end_idx,
            assets,
        )
        read_all = len(assets) > self._read_all_threshold
        return _read_bcolz_data(
            self._table,
            (end_idx - start_idx + 1, len(assets)),
            list(columns),
            first_rows,
            last_rows,
            offsets,
            read_all,
        )

    def _spot_col(self, colname):
        """
        Get the colname from daily_bar_table and read all of it into memory,
        caching the result.

        Parameters
        ----------
        colname : string
            A name of a OHLCV carray in the daily_bar_table

        Returns
        -------
        array (uint32)
            Full read array of the carray in the daily_bar_table with the
            given colname.
        """
        try:
            col = self._spot_cols[colname]
        except KeyError:
            col = self._spot_cols[colname] = self._table[colname]
        return col

    def get_last_traded_dt(self, asset, day):
        volumes = self._spot_col('volume')

        if day >= asset.end_date:
            # go back to one day before the asset ended
            search_day = self._calendar[
                self._calendar.searchsorted(asset.end_date) - 1
            ]
        else:
            search_day = day

        while True:
            try:
                ix = self.sid_day_index(asset, search_day)
            except NoDataOnDate:
                return None
            if volumes[ix] != 0:
                return search_day
            prev_day_ix = self._calendar.get_loc(search_day) - 1
            if prev_day_ix > -1:
                search_day = self._calendar[prev_day_ix]
            else:
                return None

    def sid_day_index(self, sid, day):
        """
        Parameters
        ----------
        sid : int
            The asset identifier.
        day : datetime64-like
            Midnight of the day for which data is requested.

        Returns
        -------
        int
            Index into the data tape for the given sid and day.
            Raises a NoDataOnDate exception if the given day and sid is before
            or after the date range of the equity.
        """
        try:
            day_loc = self._calendar.get_loc(day)
        except:
            raise NoDataOnDate("day={0} is outside of calendar={1}".format(
                day, self._calendar))
        offset = day_loc - self._calendar_offsets[sid]
        if offset < 0:
            raise NoDataOnDate(
                "No data on or before day={0} for sid={1}".format(
                    day, sid))
        ix = self._first_rows[sid] + offset
        if ix > self._last_rows[sid]:
            raise NoDataOnDate(
                "No data on or after day={0} for sid={1}".format(
                    day, sid))
        return ix

    def spot_price(self, sid, day, colname):
        """
        Parameters
        ----------
        sid : int
            The asset identifier.
        day : datetime64-like
            Midnight of the day for which data is requested.
        colname : string
            The price field. e.g. ('open', 'high', 'low', 'close', 'volume')

        Returns
        -------
        float
            The spot price for colname of the given sid on the given day.
            Raises a NoDataOnDate exception if the given day and sid is before
            or after the date range of the equity.
            Returns -1 if the day is within the date range, but the price is
            0.
        """
        ix = self.sid_day_index(sid, day)
        price = self._spot_col(colname)[ix]
        if price == 0:
            return -1
        if colname != 'volume':
            return price * 0.001
        else:
            return price


class PanelDailyBarReader(DailyBarReader):
    """
    Reader for data passed as Panel.

    DataPanel Structure
    -------
    items : Int64Index
        Asset identifiers.  Must be unique.
    major_axis : DatetimeIndex
       Dates for data provided provided by the Panel.  Must be unique.
    minor_axis : ['open', 'high', 'low', 'close', 'volume']
       Price attributes.  Must be unique.

    Attributes
    ----------
    The table with which this loader interacts contains the following
    attributes:

    panel : pd.Panel
        The panel from which to read OHLCV data.
    first_trading_day : pd.Timestamp
        The first trading day in the dataset.
    """
    @preprocess(panel=call(verify_indices_all_unique))
    def __init__(self, calendar, panel):

        panel = panel.copy()
        if 'volume' not in panel.minor_axis:
            # Fake volume if it does not exist.
            panel.loc[:, :, 'volume'] = int(1e9)

        self.first_trading_day = panel.major_axis[0]
        self._calendar = calendar

        self.panel = panel

    @property
    def last_available_dt(self):
        return self._calendar[-1]

    def load_raw_arrays(self, columns, start_date, end_date, assets):
        columns = list(columns)
        cal = self._calendar
        index = cal[cal.slice_indexer(start_date, end_date)]
        shape = (len(index), len(assets))
        results = []
        for col in columns:
            outbuf = zeros(shape=shape)
            for i, asset in enumerate(assets):
                data = self.panel.loc[asset, start_date:end_date, col]
                data = data.reindex_axis(index).values
                outbuf[:, i] = data
            results.append(outbuf)
        return results

    def spot_price(self, sid, day, colname):
        """
        Parameters
        ----------
        sid : int
            The asset identifier.
        day : datetime64-like
            Midnight of the day for which data is requested.
        colname : string
            The price field. e.g. ('open', 'high', 'low', 'close', 'volume')

        Returns
        -------
        float
            The spot price for colname of the given sid on the given day.
            Raises a NoDataOnDate exception if the given day and sid is before
            or after the date range of the equity.
            Returns -1 if the day is within the date range, but the price is
            0.
        """
        return self.panel.loc[sid, day, colname]

    def get_last_traded_dt(self, sid, dt):
        """
        Parameters
        ----------
        sid : int
            The asset identifier.
        dt : datetime64-like
            Midnight of the day for which data is requested.

        Returns
        -------
        pd.Timestamp : The last know dt for the asset and dt;
                       NaT if no trade is found before the given dt.
        """
        while dt in self.panel.major_axis:
            freq = self.panel.major_axis.freq
            if not isnull(self.panel.loc[sid, dt, 'close']):
                return dt
            dt -= freq
        else:
            return NaT


class SQLiteAdjustmentWriter(object):
    """
    Writer for data to be read by SQLiteAdjustmentReader

    Parameters
    ----------
    conn_or_path : str or sqlite3.Connection
        A handle to the target sqlite database.
    daily_bar_reader : BcolzDailyBarReader
        Daily bar reader to use for dividend writes.
    overwrite : bool, optional, default=False
        If True and conn_or_path is a string, remove any existing files at the
        given path before connecting.

    See Also
    --------
    zipline.data.us_equity_pricing.SQLiteAdjustmentReader
    """

    def __init__(self,
                 conn_or_path,
                 daily_bar_reader,
                 calendar,
                 overwrite=False):
        if isinstance(conn_or_path, sqlite3.Connection):
            self.conn = conn_or_path
        elif isinstance(conn_or_path, str):
            if overwrite:
                try:
                    remove(conn_or_path)
                except OSError as e:
                    if e.errno != ENOENT:
                        raise
            self.conn = sqlite3.connect(conn_or_path)
            self.uri = conn_or_path
        else:
            raise TypeError("Unknown connection type %s" % type(conn_or_path))

        self._daily_bar_reader = daily_bar_reader
        self._calendar = calendar

    def _write(self, tablename, expected_dtypes, frame):
        if frame is None or frame.empty:
            # keeping the dtypes correct for empty frames is not easy
            frame = DataFrame(
                np.array([], dtype=list(expected_dtypes.items())),
            )
        else:
            if frozenset(frame.columns) != viewkeys(expected_dtypes):
                raise ValueError(
                    "Unexpected frame columns:\n"
                    "Expected Columns: %s\n"
                    "Received Columns: %s" % (
                        set(expected_dtypes),
                        frame.columns.tolist(),
                    )
                )

            actual_dtypes = frame.dtypes
            for colname, expected in iteritems(expected_dtypes):
                actual = actual_dtypes[colname]
                if not issubdtype(actual, expected):
                    raise TypeError(
                        "Expected data of type {expected} for column"
                        " '{colname}', but got '{actual}'.".format(
                            expected=expected,
                            colname=colname,
                            actual=actual,
                        ),
                    )

        frame.to_sql(
            tablename,
            self.conn,
            if_exists='append',
            chunksize=50000,
        )

    def write_frame(self, tablename, frame):
        if tablename not in SQLITE_ADJUSTMENT_TABLENAMES:
            raise ValueError(
                "Adjustment table %s not in %s" % (
                    tablename,
                    SQLITE_ADJUSTMENT_TABLENAMES,
                )
            )
        if not (frame is None or frame.empty):
            frame = frame.copy()
            frame['effective_date'] = frame['effective_date'].values.astype(
                'datetime64[s]',
            ).astype('int64')
        return self._write(
            tablename,
            SQLITE_ADJUSTMENT_COLUMN_DTYPES,
            frame,
        )

    def write_dividend_payouts(self, frame):
        """
        Write dividend payout data to SQLite table `dividend_payouts`.
        """
        return self._write(
            'dividend_payouts',
            SQLITE_DIVIDEND_PAYOUT_COLUMN_DTYPES,
            frame,
        )

    def write_stock_dividend_payouts(self, frame):
        return self._write(
            'stock_dividend_payouts',
            SQLITE_STOCK_DIVIDEND_PAYOUT_COLUMN_DTYPES,
            frame,
        )

    def calc_dividend_ratios(self, dividends):
        """
        Calculate the ratios to apply to equities when looking back at pricing
        history so that the price is smoothed over the ex_date, when the market
        adjusts to the change in equity value due to upcoming dividend.

        Returns
        -------
        DataFrame
            A frame in the same format as splits and mergers, with keys
            - sid, the id of the equity
            - effective_date, the date in seconds on which to apply the ratio.
            - ratio, the ratio to apply to backwards looking pricing data.
        """
        if dividends is None:
            return DataFrame(np.array(
                [],
                dtype=[
                    ('sid', uint32),
                    ('effective_date', uint32),
                    ('ratio',  float64),
                ],
            ))
        ex_dates = dividends.ex_date.values

        sids = dividends.sid.values
        amounts = dividends.amount.values

        ratios = full(len(amounts), nan)

        daily_bar_reader = self._daily_bar_reader

        effective_dates = full(len(amounts), -1, dtype=int64)
        calendar = self._calendar
        for i, amount in enumerate(amounts):
            sid = sids[i]
            ex_date = ex_dates[i]
            day_loc = calendar.get_loc(ex_date, method='bfill')
            prev_close_date = calendar[day_loc - 1]
            try:
                prev_close = daily_bar_reader.spot_price(
                    sid, prev_close_date, 'close')
                if prev_close != 0.0:
                    ratio = 1.0 - amount / prev_close
                    ratios[i] = ratio
                    # only assign effective_date when data is found
                    effective_dates[i] = ex_date
            except NoDataOnDate:
                logger.warn("Couldn't compute ratio for dividend %s" % {
                    'sid': sid,
                    'ex_date': ex_date,
                    'amount': amount,
                })
                continue

        # Create a mask to filter out indices in the effective_date, sid, and
        # ratio vectors for which a ratio was not calculable.
        effective_mask = effective_dates != -1
        effective_dates = effective_dates[effective_mask]
        effective_dates = effective_dates.astype('datetime64[ns]').\
            astype('datetime64[s]').astype(uint32)
        sids = sids[effective_mask]
        ratios = ratios[effective_mask]

        return DataFrame({
            'sid': sids,
            'effective_date': effective_dates,
            'ratio': ratios,
        })

    def _write_dividends(self, dividends):
        if dividends is None:
            dividend_payouts = None
        else:
            dividend_payouts = dividends.copy()
            dividend_payouts['ex_date'] = dividend_payouts['ex_date'].values.\
                astype('datetime64[s]').astype(integer)
            dividend_payouts['record_date'] = \
                dividend_payouts['record_date'].values.astype('datetime64[s]').\
                astype(integer)
            dividend_payouts['declared_date'] = \
                dividend_payouts['declared_date'].values.astype('datetime64[s]').\
                astype(integer)
            dividend_payouts['pay_date'] = \
                dividend_payouts['pay_date'].values.astype('datetime64[s]').\
                astype(integer)

        self.write_dividend_payouts(dividend_payouts)

    def _write_stock_dividends(self, stock_dividends):
        if stock_dividends is None:
            stock_dividend_payouts = None
        else:
            stock_dividend_payouts = stock_dividends.copy()
            stock_dividend_payouts['ex_date'] = \
                stock_dividend_payouts['ex_date'].values.\
                astype('datetime64[s]').astype(integer)
            stock_dividend_payouts['record_date'] = \
                stock_dividend_payouts['record_date'].values.\
                astype('datetime64[s]').astype(integer)
            stock_dividend_payouts['declared_date'] = \
                stock_dividend_payouts['declared_date'].\
                values.astype('datetime64[s]').astype(integer)
            stock_dividend_payouts['pay_date'] = \
                stock_dividend_payouts['pay_date'].\
                values.astype('datetime64[s]').astype(integer)
        self.write_stock_dividend_payouts(stock_dividend_payouts)

    def write_dividend_data(self, dividends, stock_dividends=None):
        """
        Write both dividend payouts and the derived price adjustment ratios.
        """

        # First write the dividend payouts.
        self._write_dividends(dividends)
        self._write_stock_dividends(stock_dividends)

        # Second from the dividend payouts, calculate ratios.
        dividend_ratios = self.calc_dividend_ratios(dividends)
        self.write_frame('dividends', dividend_ratios)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def write(self,
              splits=None,
              mergers=None,
              dividends=None,
              stock_dividends=None):
        """
        Writes data to a SQLite file to be read by SQLiteAdjustmentReader.

        Parameters
        ----------
        splits : pandas.DataFrame, optional
            Dataframe containing split data. The format of this dataframe is:
              effective_date : int
                  The date, represented as seconds since Unix epoch, on which
                  the adjustment should be applied.
              ratio : float
                  A value to apply to all data earlier than the effective date.
                  For open, high, low, and close those values are multiplied by
                  the ratio. Volume is divided by this value.
              sid : int
                  The asset id associated with this adjustment.
        mergers : pandas.DataFrame, optional
            DataFrame containing merger data. The format of this dataframe is:
              effective_date : int
                  The date, represented as seconds since Unix epoch, on which
                  the adjustment should be applied.
              ratio : float
                  A value to apply to all data earlier than the effective date.
                  For open, high, low, and close those values are multiplied by
                  the ratio. Volume is unaffected.
              sid : int
                  The asset id associated with this adjustment.
        dividends : pandas.DataFrame, optional
            DataFrame containing dividend data. The format of the dataframe is:
              sid : int
                  The asset id associated with this adjustment.
              ex_date : datetime64
                  The date on which an equity must be held to be eligible to
                  receive payment.
              declared_date : datetime64
                  The date on which the dividend is announced to the public.
              pay_date : datetime64
                  The date on which the dividend is distributed.
              record_date : datetime64
                  The date on which the stock ownership is checked to determine
                  distribution of dividends.
              amount : float
                  The cash amount paid for each share.

            Dividend ratios are calculated as:
            ``1.0 - (dividend_value / "close on day prior to ex_date")``
        stock_dividends : pandas.DataFrame, optional
            DataFrame containing stock dividend data. The format of the
            dataframe is:
              sid : int
                  The asset id associated with this adjustment.
              ex_date : datetime64
                  The date on which an equity must be held to be eligible to
                  receive payment.
              declared_date : datetime64
                  The date on which the dividend is announced to the public.
              pay_date : datetime64
                  The date on which the dividend is distributed.
              record_date : datetime64
                  The date on which the stock ownership is checked to determine
                  distribution of dividends.
              payment_sid : int
                  The asset id of the shares that should be paid instead of
                  cash.
              ratio : float
                  The ratio of currently held shares in the held sid that
                  should be paid with new shares of the payment_sid.

        See Also
        --------
        zipline.data.us_equity_pricing.SQLiteAdjustmentReader
        """
        self.write_frame('splits', splits)
        self.write_frame('mergers', mergers)
        self.write_dividend_data(dividends, stock_dividends)
        self.conn.execute(
            "CREATE INDEX splits_sids "
            "ON splits(sid)"
        )
        self.conn.execute(
            "CREATE INDEX splits_effective_date "
            "ON splits(effective_date)"
        )
        self.conn.execute(
            "CREATE INDEX mergers_sids "
            "ON mergers(sid)"
        )
        self.conn.execute(
            "CREATE INDEX mergers_effective_date "
            "ON mergers(effective_date)"
        )
        self.conn.execute(
            "CREATE INDEX dividends_sid "
            "ON dividends(sid)"
        )
        self.conn.execute(
            "CREATE INDEX dividends_effective_date "
            "ON dividends(effective_date)"
        )
        self.conn.execute(
            "CREATE INDEX dividend_payouts_sid "
            "ON dividend_payouts(sid)"
        )
        self.conn.execute(
            "CREATE INDEX dividends_payouts_ex_date "
            "ON dividend_payouts(ex_date)"
        )
        self.conn.execute(
            "CREATE INDEX stock_dividend_payouts_sid "
            "ON stock_dividend_payouts(sid)"
        )
        self.conn.execute(
            "CREATE INDEX stock_dividends_payouts_ex_date "
            "ON stock_dividend_payouts(ex_date)"
        )

    def close(self):
        self.conn.close()


UNPAID_QUERY_TEMPLATE = """
SELECT sid, amount, pay_date from dividend_payouts
WHERE ex_date=? AND sid IN ({0})
"""

Dividend = namedtuple('Dividend', ['asset', 'amount', 'pay_date'])

UNPAID_STOCK_DIVIDEND_QUERY_TEMPLATE = """
SELECT sid, payment_sid, ratio, pay_date from stock_dividend_payouts
WHERE ex_date=? AND sid IN ({0})
"""

StockDividend = namedtuple(
    'StockDividend',
    ['asset', 'payment_asset', 'ratio', 'pay_date'])


class SQLiteAdjustmentReader(object):
    """
    Loads adjustments based on corporate actions from a SQLite database.

    Expects data written in the format output by `SQLiteAdjustmentWriter`.

    Parameters
    ----------
    conn : str or sqlite3.Connection
        Connection from which to load data.

    See Also
    --------
    :class:`zipline.data.us_equity_pricing.SQLiteAdjustmentWriter`
    """

    @preprocess(conn=coerce_string(sqlite3.connect))
    def __init__(self, conn):
        self.conn = conn

    def load_adjustments(self, columns, dates, assets):
        return load_adjustments_from_sqlite(
            self.conn,
            list(columns),
            dates,
            assets,
        )

    def get_adjustments_for_sid(self, table_name, sid):
        t = (sid,)
        c = self.conn.cursor()
        adjustments_for_sid = c.execute(
            "SELECT effective_date, ratio FROM %s WHERE sid = ?" %
            table_name, t).fetchall()
        c.close()

        return [[Timestamp(adjustment[0], unit='s', tz='UTC'), adjustment[1]]
                for adjustment in
                adjustments_for_sid]

    def get_dividends_with_ex_date(self, assets, date, asset_finder):
        seconds = date.value / int(1e9)
        c = self.conn.cursor()

        divs = []
        for chunk in group_into_chunks(assets):
            query = UNPAID_QUERY_TEMPLATE.format(
                ",".join(['?' for _ in chunk]))
            t = (seconds,) + tuple(map(lambda x: int(x), chunk))

            c.execute(query, t)

            rows = c.fetchall()
            for row in rows:
                div = Dividend(
                    asset_finder.retrieve_asset(row[0]),
                    row[1], Timestamp(row[2], unit='s', tz='UTC'))
                divs.append(div)
        c.close()

        return divs

    def get_stock_dividends_with_ex_date(self, assets, date, asset_finder):
        seconds = date.value / int(1e9)
        c = self.conn.cursor()

        stock_divs = []
        for chunk in group_into_chunks(assets):
            query = UNPAID_STOCK_DIVIDEND_QUERY_TEMPLATE.format(
                ",".join(['?' for _ in chunk]))
            t = (seconds,) + tuple(map(lambda x: int(x), chunk))

            c.execute(query, t)

            rows = c.fetchall()

            for row in rows:
                stock_div = StockDividend(
                    asset_finder.retrieve_asset(row[0]),    # asset
                    asset_finder.retrieve_asset(row[1]),    # payment_asset
                    row[2],
                    Timestamp(row[3], unit='s', tz='UTC'))
                stock_divs.append(stock_div)
        c.close()

        return stock_divs
