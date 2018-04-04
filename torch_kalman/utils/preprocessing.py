from collections import OrderedDict
import numpy as np
import pandas as pd

from torch_kalman.utils.utils import product


class ForecastPreprocessor(object):
    def __init__(self, measurements, group_col, measurement_col, value_col, date_col, freq):
        """
        Create a preprocessor for multivariate time-series. This converts pandas dataframes into numpy arrays and
        vice-versa.

        :param measurements: A list naming the "measurements" (i.e., the dimensions in the multivariate time-series).
        :param group_col: The column in the pandas dataframe containing the group. Each group is a separate multivariate
        time-series
        :param measurement_col: The column in the pandas dataframe containing labels corresponding to `measurements`.
        :param value_col: The column in the pandas dataframe containing the actual values of the time-series.
        :param date_col: The column in the pandas dataframe containing the date.
        :param freq: The frequency for the date. Currently pandas.Timedeltas (but not TimeOffsets) are supported. Can pass a
        string that can be interpreted by pandas.to_timedelta.
        """
        if not isinstance(freq, pd.Timedelta):
            freq = pd.to_timedelta(freq)
        self.freq = freq
        self.measurements = sorted(measurements)
        self.group_col = group_col
        self.measurement_col = measurement_col
        self.value_col = value_col
        self.date_col = date_col

    # Pandas to Numpy ------------------------------------------------------
    def pd_to_array(self, dataframe, min_len_prop):
        """
        Convert a dataframe into an array (that's suitable for passing to KalmanFilters), additionally returning
        information about that array.

        :param dataframe: A pandas dataframe. The dataframe should be in a "long" format: a single 'value' column
        contains the actual values of the time-series, a 'measurement' column indicates which values belong to which
        measurement within a group, and a 'group' column indicates which values belong to which group.
        :param min_len_prop: The longest group in the dataset will dictate the size of the array. If there are groups
        that have very little data and therefore will be mostly missing values in the array, they can be excluded. For
        example, if the longest group is 365, and you want to exclude any groups that are less than half of this length,
        Then set `min_len_prop = .50`.
        :return: A tuple with three elements:
            * A 3D numpy array. The first dimension is the group, the second is the timepoint, and the third is the
            measurement. All groups will have their first element corresponding to their first date. If one or more of the
            measurements for a group starts later than the first measurement in that group, they will be nan-padded.
            * Information about the elements of each dimension: an ordered dictionary with (1) the group-column-name :
            the group of each slice along this dimension, (2) 'timesteps' : a generator for the timesteps, and (2) the
            measurement-column name : the measurement of each slice along this dimension.
            * The original start-date for each group.
        """

        # check date-col:
        date_cols = dataframe.select_dtypes(include=[np.datetime64]).columns.tolist()
        if self.date_col not in date_cols:
            raise Exception("The date column ('{}') is not of type np.datetime64".format(self.date_col))

        # subsequent methods will assume data are sorted:
        dataframe = dataframe.sort_values(by=[self.group_col, self.measurement_col, self.date_col])

        # get info per group:
        info_per_group = {g: self.get_group_info(df_g) for g, df_g in dataframe.groupby(self.group_col)}

        # filter based on min_len_prop:
        longest_group_length = max(info['length'] for info in info_per_group.values())
        group_length_min = round(longest_group_length * min_len_prop)
        info_per_group = {g: info for g, info in info_per_group.items() if info['length'] >= group_length_min}

        # create the 'dim_info' dict with information about each dimension:
        dim_info = OrderedDict()
        dim_info[self.group_col] = sorted(info_per_group.keys())  # sorted so we always know order later
        dim_info['timesteps'] = [self.freq * i for i in range(longest_group_length + 1)]
        dim_info[self.measurement_col] = self.measurements  # sorted in __init__ so we always know order later

        # preallocate numpy array:
        x = np.empty(shape=[len(x) for x in dim_info.values()])
        x[:, :, :] = np.nan

        # fill array:
        start_dates = {}
        for g_idx, group in enumerate(dim_info[self.group_col]):
            # for each group...
            start_dates[group] = info_per_group[group]['start_date']
            for m_idx, measure in enumerate(self.measurements):
                this_var_info = info_per_group[group]['measurement_info'][measure]
                # this_var_info['idx'] accounts for implicit missings due to date-gaps:
                x[g_idx, this_var_info['idx'], m_idx] = this_var_info['values']

        return x, dim_info, start_dates

    def timedelta_int(self, t1, t2):
        """
        Subtract t1 from t2, to get difference in integers where the units are self.freq.
        :param t1: A datetime (or DateTimeIndex)
        :param t2: A datetime (or DateTimeIndex)
        :return: An integer.
        """
        diff = (t1 - t2) / self.freq
        if isinstance(diff, float):
            out = int(diff)
        else:
            out = diff.astype(int)
        if not np.isclose(diff, out).all():
            raise ValueError("Timedelta did not divide evenly into self.freq.")
        return out

    def get_group_info(self, dataframe):
        """
        Helper function for `pd_to_array`. Given a dataframe with only a single group, get information about that groups
        start-date, length, and measurements.
        :param dataframe: A slice of the original dataframe passed to `pd_to_array` corresponding to one of its groups.
        :return: A dictionary with start-date, length, and measurements.
        """
        measurement_info = {var: self.get_measurement_info(df_gd) for var, df_gd in dataframe.groupby(self.measurement_col)}
        if not all([var in self.measurements for var in measurement_info.keys()]):
            raise Exception("Some measurements in this dataframe are not in self.measurements.")

        # offset from groups start-date so all measurements have sycn'd seasonality,
        # also add nans for missing-measurements
        start_date = min(vi['start_date'] for vi in measurement_info.values())
        end_date = start_date # will find in loop:
        for measure in self.measurements:
            this_measurement = measurement_info.get(measure, None)
            if this_measurement is None:
                measurement_info[measure] = {'values': np.array([np.nan]), 'idx': np.zeros(1)}
            else:
                # already obtained idx relative to measure start, offset that based on group start:
                offset = self.timedelta_int(this_measurement['start_date'], start_date)
                measurement_info[measure]['idx'] += offset
                # keep track of end date:
                if this_measurement['end_date'] > end_date:
                    end_date = this_measurement['end_date']

        group_info = {'start_date': start_date,
                      'end_date': end_date,
                      'length': self.timedelta_int(end_date, start_date),
                      'measurement_info': measurement_info}

        return group_info

    def get_measurement_info(self, dataframe):
        """
        Helper function for `pd_to_array`. Given a dataframe with only a single measurement within a single group, get
        the start-date and actual values.

        :param dataframe: A slice of the original dataframe passed to `pd_to_array` corresponding to one of the
        measurements within one of the groups.
        :return: A dctionary with start-date and actual values.
        """
        min_difftime = dataframe[self.date_col].diff().min()
        if min_difftime == pd.Timedelta(days=0):
            raise ValueError("One or more consecutive rows in the dataframe have the same date/datetime. This could be "
                             "caused by mis-specification of groups/measures.")
        if min_difftime < self.freq:
            raise ValueError("One (or more) consecutive rows in the dataframe has a timedelta of {}, which is less than the"
                             "`freq` passed at init ({}).".format(min_difftime, self.freq))

        measurement_info = dict(start_date=dataframe[self.date_col].values.min(),
                                end_date=dataframe[self.date_col].values.max(),
                                values=dataframe[self.value_col].values)
        measurement_info['idx'] = self.timedelta_int(dataframe[self.date_col].values,
                                                     measurement_info['start_date'])

        return measurement_info

    # Numpy to Pandas ------------------------------------------------------
    def array_to_pd(self, array, dim_info, start_dates, value_col=None):
        """
        Convert the output of `pd_to_array` (or an array with the same shape as it) into a pandas dataframe. Typically
        this will be used on predictions that were generated from passing that original `pd_to_array` output to a model.

        :param array: The array output from `pd_to_array` (or an array with the same shape as it, e.g., predictions).
        :param dim_info: The dim_info dictionary output from `pd_to_array`.
        :param start_dates: A list of start_dates output from `pd_to_array`.
        :param value_col: What should the column containing the actual values be named in the output pandas dataframe?
        By default this will be the original value-column, name, but you could rename it (e.g., to 'prediction').
        :return: A pandas dataframe. The dataframe will be in a "long" format: a single value column contains the
        reshaped contents of `array`, a 'measurement' column indicates which values belong to which measurement within a
        group, and a 'group' column indicates which values belong to which group.
        """
        if value_col is None:
            value_col = self.value_col

        # make sure correct dtypes so joining to original can work:
        num_rows = product(len(val) for val in dim_info.values())
        group_dtype = np.array(dim_info[self.group_col]).dtype
        var_dtype = np.array(dim_info[self.measurement_col]).dtype
        out = {self.group_col: np.empty(shape=(num_rows,), dtype=group_dtype),
               self.measurement_col: np.empty(shape=(num_rows,), dtype=var_dtype),
               self.date_col: np.empty(shape=(num_rows,), dtype='datetime64[ns]'),
               value_col: np.empty(shape=(num_rows,)) * np.nan}

        row = 0
        for g_idx, group_name in enumerate(dim_info[self.group_col]):
            for v_idx, measure in enumerate(dim_info[self.measurement_col]):
                values = array[g_idx, :, v_idx]
                row2 = row + len(values)
                out[self.group_col][row:row2] = group_name
                out[self.measurement_col][row:row2] = measure
                out[self.date_col][row:row2] = pd.date_range(start_dates[group_name],
                                                             periods=len(values), freq=self.freq)
                out[value_col][row:row2] = values
                row = row2

        return pd.DataFrame(out)
