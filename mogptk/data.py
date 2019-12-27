import csv
import copy
import inspect
import numpy as np
from .bnse import *
from scipy import signal
import dateutil, datetime
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import re

class FormatNumber:
    """
    FormatNumber is the default formatter and takes regular floating point values as input.
    """
    def _parse(self, val):
        return float(val)

    def _parse_duration(self, val):
        return self._parse(val)

    def _format(self, val):
        return '%.6g' % (val,)

    def _scale(self, maxfreq=None):
        return 1, None

class FormatDate:
    """
    FormatDate is a formatter that takes date values as input, such as '2019-03-01', and stores values internally as days since 1970-01-01.
    """
    def _parse(self, val):
        return (dateutil.parser.parse(val) - datetime.datetime(1970,1,1)).total_seconds()/3600/24

    def _parse_duration(self, val):
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            return _parse_duration_to_sec(val)/24/3600
        raise ValueError("could not convert input to duration")
    
    def _format(self, val):
        return datetime.datetime.utcfromtimestamp(val*3600*24).strftime('%Y-%m-%d')

    def _scale(self, maxfreq=None):
        if maxfreq == 'year':
            return 356.2425, 'year'
        if maxfreq == 'month':
            return 30.4369, 'month'
        if maxfreq == None or maxfreq == 'day':
            return 1, 'day'
        if maxfreq == 'hour':
            return 1/24, 'hour'
        if maxfreq == 'minute':
            return 1/24/60, 'minute'
        if maxfreq == 'second':
            return 1/24/3600, 'second'

class FormatDateTime:
    """
    FormatDateTime is a formatter that takes date and time values as input, such as '2019-03-01 12:30', and stores values internally as seconds since 1970-01-01.
    """
    def _parse(self, val):
        return (dateutil.parser.parse(val) - datetime.datetime(1970,1,1)).total_seconds()

    def _parse_duration(self, val):
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            return _parse_duration_to_sec(val)
        raise ValueError("could not convert input to duration")

    def _format(self, val):
        return datetime.datetime.utcfromtimestamp(val).strftime('%Y-%m-%d %H:%M')

    def _scale(self, maxfreq=None):
        if maxfreq == 'year':
            return 3600*24*356.2425, 'year'
        if maxfreq == 'month':
            return 3600*24*30.4369, 'month'
        if maxfreq == 'day':
            return 3600*24, 'day'
        if maxfreq == 'hour':
            return 3600, 'hour'
        if maxfreq == 'minute':
            return 60, 'minute'
        if maxfreq == None or maxfreq == 'second':
            return 1, 'second'

################################################################
################################################################
################################################################

class TransformDetrend:
    """
    TransformDetrend is a transformer that detrends the data. It uses NumPy `polyfit` to find an `n` degree polynomial that removes the trend.

    Args:
        degree (int): Polynomial degree that will be fit, i.e. `2` will find a quadratic trend and remove it from the data.
    """
    # TODO: add regression?
    def __init__(self, degree=1):
        self.degree = degree

    def _data(self, data):
        if data.get_input_dims() != 1:
            raise Exception("can only remove ranges on one dimensional input data")

        self.coef = np.polyfit(data.X[:,0], data.Y, self.degree)
        # reg = Ridge(alpha=0.1, fit_intercept=True)
        # reg.fit(data.X, data.Y)
        # self.trend = reg

    def _forward(self, x, y):
        return y - np.polyval(self.coef, x[:, 0])
        # return y - self.trend.predict(x)
    
    def _backward(self, x, y):
        return y + np.polyval(self.coef, x[:, 0])
        # return y + self.trend.predict(x)

class TransformNormalize:
    """
    TransformNormalize is a transformer that normalizes the data so that the y-axis is between 0 and 1.
    """
    def __init__(self):
        pass

    def _data(self, data):
        self.ymin = np.amin(data.Y)
        self.ymax = np.amax(data.Y)

    def _forward(self, x, y):
        return (y-self.ymin)/(self.ymax-self.ymin)
    
    def _backward(self, x, y):
        return y*(self.ymax-self.ymin)+self.ymin

class TransformLog:
    """
    TransformLog is a transformer that takes the log of the data. Data is automatically shifted in the y-axis so that all values are greater than or equal to 1.
    """
    def __init__(self):
        pass

    def _data(self, data):
        self.shift = 1 - np.amin(data.Y)
        self.mean = np.log(data.Y + self.shift).mean()

    def _forward(self, x, y):
        return np.log(y + self.shift) - self.mean
    
    def _backward(self, x, y):
        return np.exp(y + self.mean) - self.shift

################################################################
################################################################
################################################################

def LoadFunction(f, start, end, n, var=0.0, name=None, random=False):
    """
    LoadFunction loads a dataset from a given function y = f(x) + N(0,var). It will pick n data points between start and end for x, for which f is being evaluated. By default the n points are spread equally over the interval, with random=True they will be picked randomly.

    The function should take one argument x with shape (n,input_dims) and return y with shape (n). If your data has only one input dimension, you can use x[:,0] to select only the first (and only) input dimension.

    Args:
        f (function): Function taking x with shape (n,input_dims) and returning shape (n) as y.
        n (int): Number of data points to pick between start and end.
        start (float, list): Define start of interval.
        end (float, list): Define end of interval.
        var (float, optional): Variance added to the output.
        name (str, optional): Name of data.
        random (boolean): Select points randomly between start and end (defaults to False).

    Returns:
        mogptk.data.Data

    Examples:
        >>> LoadFunction(lambda x: np.sin(3*x[:,0]), 0, 10, n=200, var=0.1, name='Sine wave')
        <mogptk.data.Data at ...>
    """
    # TODO: make work for multiple input dimensions, take n as a list

    start = _normalize_input_dims(start, None)
    input_dims = len(start)
    if input_dims != 1:
        raise ValueError("can only load function with one dimensional input data")
    
    end = _normalize_input_dims(end, input_dims)
    _check_function(f, input_dims)

    x = np.empty((n, input_dims))
    for i in range(input_dims):
        if start[i] >= end[i]:
            if input_dims == 1:
                raise ValueError("start must be lower than end")
            else:
                raise ValueError("start must be lower than end for input dimension %d" % (i))

        if random:
            x[:,i] = np.random.uniform(start[i], end[i], n)
        else:
            x[:,i] = np.linspace(start[i], end[i], n)

    y = f(x)
    if y.ndim == 2 and y.shape[1] == 1:
        y = y[:,0]
    y += np.random.normal(0.0, var, n)

    data = Data(x, y, name=name)
    data.set_function(f)
    return data

def LoadCSV(filename, x_cols, y_col, name=None, format={}, filter=None, **kwargs):
    """
    LoadCSV loads a dataset from a given CSV file. It loads in x_cols as the names of the input dimension columns, and y_col the name of the output column. Setting a formatter for a column will enable parsing for example date fields such as '2019-03-01'. A filter can be set to filter out data from the CSV, such as ensuring that another column has a certain value.

    Args:
        filename (str): CSV filename.
        x_cols (str, list): Name or names of X column(s) in CSV.
        y_col (str): Name of Y column in CSV.
        name (str, optional): Name of data.
        format (dict, optional): Dictionary with x_cols values as keys containing FormatNumber (default), FormatDate, FormetDateTime, ...
        filter (function, optional): Function that takes row as argument, and returns True to keep the record.
        **kwargs: Additional keyword arguments for csv.DictReader.

    Returns:
        mogptk.data.Data

    Examples:
        >>> LoadCSV('gold.csv', 'Date', 'Price', name='Gold', format={'Date': FormatDate}, filter=lambda row: row['Region'] == 'Europe')
        <mogptk.data.Data at ...>

        >>> LoadCSV('gold.csv', 'Date', 'Price', delimiter=' ', quotechar='|')
        <mogptk.data.Data at ...>
    """

    if (not isinstance(x_cols, list) or not all(isinstance(item, str) for item in x_cols)) and not isinstance(x_cols, str):
        raise ValueError("x_cols must be string or list of strings")
    if not isinstance(y_col, str):
        raise ValueError("y_col must be string")

    # if isinstance(x_cols, str):
    #     x_cols = [x_cols]

    # with open(filename, mode='r') as csv_file:
    #     rows = list(csv.DictReader(csv_file, **kwargs))
        
    #     X = []
    #     Y = []
    #     for j, row in enumerate(rows):
    #         if filter != None and not filter(row):
    #             continue

    #         xs = []
    #         for i, x_col in enumerate(x_cols):
    #             xs.append(row[x_col])
    #         X.append(xs)
    #         Y.append(row[y_col])

    #     return Data(X, Y, name=name, formats=format, x_labels=x_cols, y_label=y_col)

    df = pd.read_csv(filename, **kwargs)
    df.dropna(inplace=True)

    return LoadDataFrame(df=df, x_cols=x_cols, y_col=y_col, name=name, format=format, filter=filter)


# TODO: filter not implemented
def LoadDataFrame(df, x_cols, y_col, name=None, format={}, filter=None):
    """
    LoadDataFrame loads a DataFrame from Pandas. It loads in x_cols as the names of the input dimension columns, and y_col the name of the output column. Setting a formatter for a column will enable parsing for example date fields such as '2019-03-01'. A filter can be set to filter out data from the CSV, such as ensuring that another column has a certain value.

    Args:
        df (pandas.DataFrame): The Pandas DataFrame.
        x_cols (str, list): Name or names of X column(s) in DataFrame.
        y_col (str): Name of Y column in DataFrame.
        name (str, optional): Name of data.
        format (dict, optional): Dictionary with x_cols values as keys containing FormatNumber (default), FormatDate, FormetDateTime, ...
        filter (function, optional): Function that takes row as argument, and returns True to keep the record.
        **kwargs: Additional keyword arguments for csv.DictReader.

    Returns:
        mogptk.data.Data

    Examples:
        >>> df = pd.DataFrame(...)
        >>> LoadDataFrame(df, 'Date', 'Price', name='Gold', format={'Date': FormatDate}, filter=lambda row: row['Region'] == 'Europe')
        <mogptk.data.Data at ...>

        >>> df = pd.DataFrame(...)
        >>> LoadDataFrame(df, 'Date', 'Price', delimiter=' ', quotechar='|')
        <mogptk.data.Data at ...>
    """
    input_dims = 1
    if isinstance(x_cols, list):
        input_dims = len(x_cols)
    else:
        x_cols = [x_cols]

    x_data = df[x_cols]
    y_data = df[y_col]

    return Data(x_data.values, y_data.values, name=name, formats=format, x_labels=x_data.columns.values.tolist(), y_label=y_data.name)

################################################################
################################################################
################################################################

class Data:
    def __init__(self, X, Y, name="", formats=None, x_labels=None, y_label=None):
        """
        Data class holds all the observations, latent functions and prediction data.

        This class takes the data raw, but you can load data also conveniently using
        LoadFunction, LoadCSV, LoadDataFrame, etc. This class allows to modify the data before being passed into the model.
        Examples are transforming data, such as detrending or taking the log, removing data range to simulate sensor failure,
        and aggregating data for given spans on X, such as aggregating daily data into
        weekly data. Additionally, we also use this class to set the range we want to predict.

        Args:
            X (list, numpy.ndarray, dict): Independent variable data of shape (n) or (n,input_dims).
            Y (list, numpy.ndarray): Dependent variable data of shape (n).
            name (str, optional): Name of data.
            formats (list, dict, optional): List or dict of formatters (such as FormatNumber (default), FormatDate,
                FormatDateTime, ...) for each input dimension.
            x_labels (str, list of str, optional): Name or names of input dimensions.
            y_label (str, optional): Name of output dimension.

        Examples:
            >>> channel = mogptk.Data([0, 1, 2, 3], [4, 3, 5, 6])
        """
        
        # find out number of data rows (n) and number of input dimensions (input_dims)
        n = 0
        input_dims = 0
        x_nested_lists = False
        if isinstance(X, (list, np.ndarray, dict)) and 0 < len(X):
            n = len(X)
            input_dims = 1

            if isinstance(X, dict):
                it1 = iter(X.values())
                it2 = iter(X.values())
            else:
                it1 = iter(X)
                it2 = iter(X)

            if all(isinstance(val, (list, np.ndarray)) for val in it1):
                first = len(next(it2))
                if all(len(val) == first for val in it2):
                    x_nested_lists = True
                    input_dims = first

        # convert dicts to lists
        if x_labels != None:
            if n != 0:
                if isinstance(x_labels, str) and input_dims == 1:
                    x_labels = [x_labels] 
                if not isinstance(x_labels, list) or not all(isinstance(label, str) for label in x_labels):
                    raise ValueError("x_labels must be a string or list of strings for each input dimension")

                if isinstance(X, dict):
                    it = iter(X.values())
                    first = len(next(it))
                    if not all(isinstance(x, (list, np.ndarray)) for x in X.values()) or not all(len(x) == first for x in it):
                        raise ValueError("X dict should contain all lists or np.ndarrays where each has the same length")
                    if not all(key in X for key in x_labels):
                        raise ValueError("X dict must contain all keys listed in x_labels")
                    X = list(map(list, zip(*[X[key] for key in x_labels])))

                if formats != None and isinstance(formats, dict):
                    # it = iter(formats.values())
                    # first = len(next(it))
                    # if not all(isinstance(fmt, (list, np.ndarray)) for fmt in formats.values()) or not all(len(fmt) == first for fmt in it):
                    #     raise ValueError("formats dict should contain all lists or np.ndarrays where each has the same length")
                    # if not all(key in formats for key in x_labels):
                    #     raise ValueError("formats dict must contain all keys listed in x_labels")
                    # formats = list(map(list, zip(*[formats[key] for key in x_labels])))
                    formats = list(formats.values())

        # format X columns
        if formats != None:
            if not isinstance(formats, list):
                raise ValueError("formats should be list or dict for each input dimension, when a dict is passed than x_labels must also be set")

            if n != 0:
                if not isinstance(formats, list):
                    formats = [formats]
                for col in range(input_dims):
                    if len(formats) <= col:
                        formats.append(FormatNumber())
                    elif isinstance(formats[col], type):
                        formats[col] = formats[col]()
                
                X_raw = X
                X = np.empty((n,input_dims))
                for row, val in enumerate(X_raw):
                    if x_nested_lists:
                        for col in range(input_dims):
                            try:
                                X[row,col] = formats[col]._parse(val[col])
                            except ValueError:
                                print("Warning: could not parse X format at row %d column %s" % (row+1, col))
                    else:
                        try:
                            X[row,0] = formats[col]._parse(val)
                        except ValueError:
                            print("Warning: could not parse X format at row %d" % (row+1,))
            else:
                # error handled below
                pass

        # check if X and Y are correct inputs
        if isinstance(X, list):
            if all(isinstance(x, list) for x in X):
                m = len(X[0])
                if not all(len(x) == m for x in X[1:]):
                    raise ValueError("X list items must all be lists of the same length")
                if not all(all(isinstance(val, (int, float)) for val in x) for x in X):
                    raise ValueError("X list items must all be lists of numbers")
            elif all(isinstance(x, np.ndarray) for x in X):
                m = len(X[0])
                if not all(len(x) == m for x in X[1:]):
                    raise ValueError("X list items must all be numpy.ndarrays of the same length")
            elif not all(isinstance(x, (int, float)) for x in X):
                raise ValueError("X list items must be all lists, all numpy.ndarrays, or all numbers")
            X = np.array(X)
        if isinstance(Y, list):
            if not all(isinstance(y, (int, float)) for y in Y):
                raise ValueError("Y list items must all be numbers")
            Y = np.array(Y)
        if not isinstance(X, np.ndarray) or not isinstance(Y, np.ndarray):
            raise ValueError("X and Y must be lists or numpy arrays, if dicts are passed then x_labels and/or y_label must also be set")
        X = X.astype(float)
        Y = Y.astype(float)

        if X.ndim == 1:
            X = X.reshape(-1, 1)
        if X.ndim != 2:
            raise ValueError("X must be either a one or two dimensional array of data")
        if Y.ndim != 1:
            raise ValueError("Y must be a one dimensional array of data")
        if X.shape[0] != Y.shape[0]:
            raise ValueError("X and Y must be of the same length")
        
        # sort on X for single input dimensions
        if input_dims == 1:
            ind = np.argsort(X, axis=0)
            X = np.take_along_axis(X, ind, axis=0)
            Y = np.take_along_axis(Y, ind[:,0], axis=0)

        for dim in range(X.shape[1]):
            xran = np.max(X[:,dim]) - np.min(X[:,dim])
            if xran < 1e-3:
                print("Warning: very small X range may give problems, it is suggested to scale up your X-axis")
            elif 1e4 < xran:
                print("Warning: very large X range may give problems, it is suggested to scale down your X-axis")
        
        self.name = name
        self.X = X # shape (n, input_dims)
        self.Y = Y # shape (n)
        self.mask = np.array([True] * n)
        self.F = None
        self.X_pred = np.array([])
        self.Y_mu_pred = {}
        self.Y_var_pred = {}

        if x_labels != None and isinstance(x_labels, list) and all(isinstance(item, str) for item in x_labels):
            self.x_labels = x_labels
        else:
            self.x_labels = [''] * input_dims

        if y_label != None and isinstance(y_label, str):
            self.y_label = y_label
        else:
            self.y_label = ''

        if formats != None and isinstance(formats, list):
            self.formatters = formats
        else:
            self.formatters = [FormatNumber()] * input_dims
        self.transformations = []

    def __str__(self):
        return "x=%s\ny=%s" % (self.X.tolist(), self.Y.tolist())

    def set_name(self, name):
        """
        Set name for data.

        Args:
            name (str): Name of data.

        Examples:
            >>> data.set_name('Channel A')
        """
        self.name = name

    def set_labels(self, x_labels, y_label):
        """
        Set axes labels for plots.

        Args:
            x_labels (str, list of str): X data names for each input dimension.
            y_label (str): Y data name for output dimension.

        Examples:
            >>> data.set_labels(['X', 'Y'], 'Cd')
        """
        if isinstance(x_labels, str):
            x_labels = [x_labels]
        elif not isinstance(x_labels, list) or not all(isinstance(item, str) for item in x_labels):
            raise ValueError("x_labels must be list of strings")
        if not isinstance(y_label, str):
            raise ValueError("y_label must be string")
        if len(x_labels) != self.get_input_dims():
            raise ValueError("x_labels must have the same input dimensions as the data")

        self.x_labels = x_labels
        self.y_label = y_label

    def set_function(self, f):
        """
        Set a (latent) function for the data, ie. the theoretical or true signal. This is used for plotting purposes and is optional.
    
        The function should take one argument x with shape (n,input_dims) and return y with shape (n). If your data has only one input dimension, you can use x[:,0] to select only the first (and only) input dimension.

        Args:
            f (function): Function taking x with shape (n,input_dims) and returning shape (n) as y.

        Examples:
            >>> data.set_function(lambda x: np.sin(3*x[:,0])
        """
        _check_function(f, self.get_input_dims())
        self.F = f

    def copy(self):
        """
        Make a deep copy of Data.

        Returns:
            mogptk.data.Data

        Examples:
            >>> other = data.copy()
        """
        return copy.deepcopy(self)

    def transform(self, transformer):
        """
        Transform the data by using one of the provided transformers, such as TransformDetrend, TransformNormalize, TransformLog, ...

        Args:
            transformer (obj): Transformer object with _forward(x, y) and _backward(x, y) methods.

        Examples:
            >>> data.transform(mogptk.TransformDetrend)
        """
        t = transformer
        if isinstance(t, type):
            t = transformer()
        t._data(self)

        self.Y = t._forward(self.X, self.Y)
        if self.F != None:
            f = self.F
            self.F = lambda x: t._forward(x, f(x))
        self.transformations.append(t)
    
    def filter(self, start, end):
        """
        Filter the data range to be between start and end. Start and end can be strings if a proper formatter is set for the independent variable.

        Args:
            start (float, str): Start of interval.
            end (float, str): End of interval.

        Examples:
            >>> data = mogptk.LoadFunction(lambda x: np.sin(3*x[:,0]), 0, 10, n=200, var=0.1, name='Sine wave')
            >>> data.filter(3, 8)
        
            >>> data = mogptk.LoadCSV('gold.csv', 'Date', 'Price', format={'Date': mogptk.FormatDate})
            >>> data.filter('2016-01-15', '2016-06-15')
        """
        if self.get_input_dims() != 1:
            raise ValueError("can only filter on one dimensional input data")
        
        start = self.formatters[0]._parse(start)
        end = self.formatters[0]._parse(end)
        ind = (self.X[:,0] >= start) & (self.X[:,0] < end)

        self.X = np.expand_dims(self.X[ind,0], 1)
        self.Y = self.Y[ind]
        self.mask = self.mask[ind]

    def aggregate(self, duration, f=np.mean):
        """
        Aggregate the data by duration and apply a function to obtain a reduced dataset.

        For example, group daily data by week and take the mean.
        The duration can be set as a number which defined the intervals on the X axis,
        or by a string written in the duration format with:
        y=year, M=month, w=week, d=day, h=hour, m=minute, and s=second.
        For example, 3w1d means three weeks and one day, ie. 22 days, or 6M to mean six months.
        If using a number, be aware that when using FormatDate your X data is denoted per day,
        while with FormatDateTime it is per second.

        Args:
            duration (float, str): Duration along the X axis or as a string in the duration format.
            f (function, optional): Function to use to reduce data, by default uses np.mean.

        Examples:
            >>> data.aggregate(5)

            >>> data.aggregate('2w', f=np.sum)
        """
        if self.get_input_dims() != 1:
            raise ValueError("can only aggregate on one dimensional input data")
        
        start = self.X[0,0]
        end = self.X[-1,0]
        step = self.formatters[0]._parse_duration(duration)

        X = np.arange(start+step/2, end+step/2, step)
        Y = np.empty((len(X)))
        for i in range(len(X)):
            ind = (self.X[:,0] >= X[i]-step/2) & (self.X[:,0] < X[i]+step/2)
            Y[i] = f(self.Y[ind])

        self.X = np.expand_dims(X, 1)
        self.Y = Y
        self.mask = np.array([True] * len(self.X))

    ################################################################

    def get_name(self):
        """
        Return the name.

        Returns:
            str.

        Examples:
            >>> data.get_name()
            'A'
        """
        return self.name

    def has_removed_obs(self):
        """
        Returns True if observations have been removed using the remove_* methods.

        Returns:
            boolean

        Examples:
            >>> data.has_removed_obs()
            True
        """
        return False in self.mask

    def get_input_dims(self):
        """
        Returns the number of input dimensions.

        Returns:
            int: Input dimensions.

        Examples:
            >>> data.get_input_dims()
            2
        """
        return self.X.shape[1]

    def get_data(self):
        """
        Returns the observations.

        Returns:
            numpy.ndarray: X data of shape (n,input_dims).
            numpy.ndarray: Y data of shape (n).

        Examples:
            >>> x, y = data.get_data()
        """
        x = self.X[self.mask,:]
        y = self.Y[self.mask]
        return x, self._detransform(x, y)
    
    def get_all(self):
        """
        Returns all observations (including removed observations).

        Returns:
            numpy.ndarray: X data of shape (n,input_dims).
            numpy.ndarray: Y data of shape (n).

        Examples:
            >>> x, y = data.get_all()
        """
        x = self.X
        y = self.Y
        return x, self._detransform(x, y)

    def get_removed(self):
        """
        Returns the removed observations.

        Returns:
            numpy.ndarray: X data of shape (n,input_dims).
            numpy.ndarray: Y data of shape (n).

        Examples:
            >>> x, y = data.get_removed()
        """
        x = self.X[~self.mask,:]
        y = self.Y[~self.mask]
        return x, self._detransform(x, y)

    ################################################################
    
    def remove_randomly(self, n=None, pct=None):
        """
        Removes observations randomly on the whole range. Either 'n' observations are removed, or a percentage of the observations.

        Args:
            n (int, optional): Number of observations to remove randomly.
            pct (float, optional): Percentage in interval [0,1] of observations to remove randomly.

        Examples:
            >>> data.remove_randomly(50) # remove 50 observations

            >>> data.remove_randomly(pct=0.9) # remove 90% of the observations
        """
        if n == None:
            if pct == None:
                n = 0
            else:
                n = int(pct * self.X.shape[0])

        idx = np.random.choice(self.X.shape[0], n, replace=False)
        self.mask[idx] = False
    
    def remove_range(self, start=None, end=None):
        """
        Removes observations in the interval [start,end]. Start and end can be strings if a proper formatter is set for the independent variable.
        
        Args:
            start (float, str, optional): Start of interval. Defaults to first value in observations.
            end (float, str, optional): End of interval. Defaults to last value in observations.

        Examples:
            >>> data = mogptk.LoadFunction(lambda x: np.sin(3*x[:,0]), 0, 10, n=200, var=0.1, name='Sine wave')
            >>> data.remove_range(3, 8)
        
            >>> data = mogptk.LoadCSV('gold.csv', 'Date', 'Price', format={'Date': mogptk.FormatDate})
            >>> data.remove_range('2016-01-15', '2016-06-15')
        """
        if self.get_input_dims() != 1:
            raise Exception("can only remove ranges on one dimensional input data")

        if start == None:
            start = np.min(self.X[:,0])
        else:
            start = self.formatters[0]._parse(start)
        if end == None:
            end = np.max(self.X[:,0])
        else:
            end = self.formatters[0]._parse(end)

        idx = np.where(np.logical_and(self.X[:,0] >= start, self.X[:,0] <= end))
        self.mask[idx] = False
    
    def remove_rel_range(self, start, end):
        """
        Removes observations between start and end as a percentage of the number of observations. So '0' is the first observation, '0.5' is the middle observation, and '1' is the last observation.

        Args:
            start (float): Start percentage in interval [0,1].
            end (float): End percentage in interval [0,1].
        """
        if self.get_input_dims() != 1:
            raise Exception("can only remove ranges on one dimensional input data")

        x_min = np.min(self.X[:,0])
        x_max = np.max(self.X[:,0])
        start = x_min + max(0.0, min(1.0, start)) * (x_max-x_min)
        end = x_min + max(0.0, min(1.0, end)) * (x_max-x_min)

        idx = np.where(np.logical_and(self.X[:,0] >= start, self.X[:,0] <= end))
        self.mask[idx] = False

    def remove_random_ranges(self, n, duration):
        """
        Removes a number of ranges to simulate sensor failure.

        Args:
            n (int): Number of ranges to remove.
            duration (float, str): Width of ranges to remove, can use a number or the duration format syntax (see aggregate()).

        Examples:
            >>> data.remove_random_ranges(2, 5) # remove two ranges that are 5 wide in input space

            >>> data.remove_random_ranges(3, '1d') # remove three ranges that are 1 day wide
        """
        if self.get_input_dims() != 1:
            raise Exception("can only remove ranges on one dimensional input data")

        duration = self.formatters[0]._parse_duration(duration)
        if n < 1:
            return

        m = (self.X[-1]-self.X[0]) - n*duration
        if m <= 0:
            raise Exception("no data left after removing ranges")

        locs = self.X[:,0] <= self.X[-1,0]-duration
        locs[sum(locs)] = True # make sure the last data point can be deleted
        for i in range(n):
            x = self.X[locs][np.random.randint(len(self.X[locs]))]
            locs[(self.X[:,0] > x-duration) & (self.X[:,0] < x+duration)] = False
            self.mask[(self.X[:,0] >= x) & (self.X[:,0] < x+duration)] = False
    
    ################################################################
    
    def get_pred(self, name, sigma=2):
        """
        Returns the prediction of a given name with a normal variance of sigma.

        Args:
            name (str): Name of the prediction, equals the name of the model that made the prediction.
            sigma (float): The uncertainty interval calculated at mean-sigma*var and mean+sigma*var. Defaults to 2,

        Returns:
            numpy.ndarray: X prediction of shape (n,input_dims).
            numpy.ndarray: Y mean prediction of shape (n,).
            numpy.ndarray: Y lower prediction of uncertainty interval of shape (n,).
            numpy.ndarray: Y upper prediction of uncertainty interval of shape (n,).

        Examples:
            >>> x, y_mean, y_var_lower, y_var_upper = data.get_pred('MOSM', sigma=1)
        """
        if name not in self.Y_mu_pred:
            raise Exception("prediction name '%s' does not exist" % (name))

        mu = self.Y_mu_pred[name]
        lower = mu - sigma * np.sqrt(self.Y_var_pred[name])
        upper = mu + sigma * np.sqrt(self.Y_var_pred[name])

        mu = self._detransform(self.X_pred, mu)
        lower = self._detransform(self.X_pred, lower)
        upper = self._detransform(self.X_pred, upper)
        return self.X_pred, mu, lower, upper

    def set_pred_range(self, start=None, end=None, n=None, step=None):
        """
        Sets the prediction range.

        The interval is set with [start,end], with either 'n' points or a
        given 'step' between the points. Start and end can be set as strings and
        step in the duration string format if the proper formatter is set.

        Args:
            start (float, str, optional): Start of interval, defaults to the first observation.
            end (float, str, optional): End of interval, defaults to the last observation.
            n (int, optional): Number of points to generate in the interval.
            step (float, str, optional): Spacing between points in the interval.

            If neither 'step' or 'n' is passed, default number of points is 100.

        Examples:
            >>> data = mogptk.LoadFunction(lambda x: np.sin(3*x[:,0]), 0, 10, n=200, var=0.1, name='Sine wave')
            >>> data.set_pred_range(3, 8, 200)
        
            >>> data = mogptk.LoadCSV('gold.csv', 'Date', 'Price', format={'Date': mogptk.FormatDate})
            >>> data.set_pred_range('2016-01-15', '2016-06-15', step='1d')
        """
        if self.get_input_dims() != 1:
            raise Exception("can only set prediction range on one dimensional input data")

        if start == None:
            start = self.X[0,:]
        elif isinstance(start, list):
            for i in range(self.get_input_dims()):
                start[i] = self.formatters[i]._parse(start[i])
        else:
            start = self.formatters[0]._parse(start)

        if end == None:
            end = self.X[-1,:]
        elif isinstance(end, list):
            for i in range(self.get_input_dims()):
                end[i] = self.formatters[i]._parse(end[i])
        else:
            end = self.formatters[0]._parse(end)
        
        start = _normalize_input_dims(start, self.get_input_dims())
        end = _normalize_input_dims(end, self.get_input_dims())

        # TODO: works for multi input dims?
        if end <= start:
            raise ValueError("start must be lower than end")

        # TODO: prediction range for multi input dimension; fix other axes to zero so we can plot?
        self.X_pred = np.array([])
        if step == None and n != None:
            self.X_pred = np.empty((n, self.get_input_dims()))
            for i in range(self.get_input_dims()):
                self.X_pred[:,i] = np.linspace(start[i], end[i], n)
        else:
            if self.get_input_dims() != 1:
                raise ValueError("cannot use step for multi dimensional input, use n")
            if step == None:
                step = (end[0]-start[0])/100
            else:
                step = self.formatters[0]._parse_duration(step)
            self.X_pred = np.arange(start[0], end[0]+step, step).reshape(-1, 1)
    
    def set_pred(self, x):
        """
        Set the prediction range directly.

        Args:
            x (list, numpy.ndarray): Array of shape (n) or (n,input_dims) with input values to predict at.

        Examples:
            >>> data.set_pred([5.0, 5.5, 6.0, 6.5, 7.0])
        """
        if isinstance(x, list):
            x = np.array(x)
        elif not isinstance(x, np.ndarray):
            raise ValueError("x expected to be a list or numpy.ndarray")

        x = x.astype(float)

        if x.ndim == 1:
            x = x.reshape(-1, 1)
        if x.ndim != 2 or x.shape[1] != self.get_input_dims():
            raise ValueError("x shape must be (n,input_dims)")

        self.X_pred = x

        # clear old prediction data now that X_pred has been updated
        self.Y_mu_pred = {}
        self.Y_var_pred = {}

    ################################################################

    def get_nyquist_estimation(self):
        """
        Estimate nyquist frequency by taking 0.5/(minimum distance of points).

        Returns:
            numpy.ndarray: Nyquist frequency array of shape (input_dims,).

        Examples:
            >>> freqs = data.get_nyquist_estimation()
        """
        input_dims = self.get_input_dims()

        nyquist = np.empty((input_dims))
        for i in range(self.get_input_dims()):
            x = np.sort(self.X[:,i])
            dist = np.abs(x[1:]-x[:-1]) # TODO: assumes X is sorted, use average distance instead of minimal distance?
            dist = np.min(dist[np.nonzero(dist)])
            nyquist[i] = 0.5/dist
        return nyquist

    def get_bnse_estimation(self, Q=1, n=5000):
        """
        Peaks estimation using BNSE (Bayesian Non-parametric Spectral Estimation).

        Args:
            Q (int): Number of peaks to find, defaults to 1.
            n (int): Number of points of the grid to evaluate frequencies, defaults to 5000.

        Returns:
            numpy.ndarray: Amplitude array of shape (input_dims,Q).
            numpy.ndarray: Frequency array of shape (input_dims,Q).
            numpy.ndarray: Variance array of shape (input_dims,Q).

        Examples:
            >>> amplitudes, means, variances = data.get_bnse_estimation()
        """
        input_dims = self.get_input_dims()

        # Gaussian: f(x) = A * exp((x-B)^2 / (2C^2))
        # Ie. A is the amplitude or peak height, B the mean or peak position, and C the variance or peak width
        A = np.zeros((input_dims, Q))
        B = np.zeros((input_dims, Q))
        C = np.zeros((input_dims, Q))

        nyquist = self.get_nyquist_estimation()
        for i in range(input_dims):
            x = self.X[:,i]
            y = self.Y
            bnse = bse(x, y)
            bnse.set_freqspace(nyquist[i], dimension=n)
            bnse.train()
            bnse.compute_moments()

            amplitudes, positions, variances = bnse.get_freq_peaks()
            if len(positions) == 0:
                continue

            n = len(positions)
            if n < Q and n != 0:
                # if there not enough peaks, we will repeat them
                j = 0
                while len(positions) < Q:
                    amplitudes = np.append(amplitudes, amplitudes[j])
                    positions = np.append(positions, positions[j])
                    variances = np.append(variances, variances[j])
                    j = (j+1) % n

            A[i,:] = amplitudes[:Q]
            B[i,:] = positions[:Q]
            C[i,:] = variances[:Q]
        return A, B, C

    def get_ls_estimation(self, Q=1, n=50000):
        """
        Peak estimation using Lomb Scargle.

        Args:
            Q (int): Number of peaks to find, defaults to 1.
            n (int): Number of points to use for Lomb Scargle, defaults to 50000.

        Returns:
            numpy.ndarray: Amplitude array of shape (input_dims,Q).
            numpy.ndarray: Frequency array of shape (input_dims,Q).
            numpy.ndarray: Variance array of shape (input_dims,Q).

        Examples:
            >>> amplitudes, means, variances = data.get_ls_estimation()
        """
        input_dims = self.get_input_dims()

        # Gaussian: f(x) = A * exp((x-B)^2 / (2C^2))
        # Ie. A is the amplitude or peak height, B the mean or peak position, and C the variance or peak width
        A = np.zeros((input_dims, Q))
        B = np.zeros((input_dims, Q))
        C = np.zeros((input_dims, Q))

        nyquist = self.get_nyquist_estimation() * 2 * np.pi
        for i in range(input_dims):
            x = np.linspace(0, nyquist[i], n+1)[1:]
            dx = x[1]-x[0]

            y = signal.lombscargle(self.X[:,i], self.Y, x)
            ind, _ = signal.find_peaks(y)
            ind = ind[np.argsort(y[ind])[::-1]] # sort by biggest peak first

            widths, width_heights, _, _ = signal.peak_widths(y, ind, rel_height=0.5)
            widths *= dx / np.pi / 2.0

            positions = x[ind] / np.pi / 2.0
            amplitudes = y[ind]
            variances = widths / np.sqrt(8 * np.log(amplitudes / width_heights)) # from full-width half-maximum to Gaussian sigma

            n = len(positions)
            if n < Q and n != 0:
                # if there not enough peaks, we will repeat them
                j = 0
                while len(positions) < Q:
                    amplitudes = np.append(amplitudes, amplitudes[j])
                    positions = np.append(positions, positions[j])
                    variances = np.append(variances, variances[j])
                    j = (j+1) % n

            A[i,:] = amplitudes[:Q]
            B[i,:] = positions[:Q]
            C[i,:] = variances[:Q]
        return A, B, C
    
    #def get_gm_estimation(self):
    #    # TODO: use sklearn.mixture.GaussianMixture to retrieve fitted gaussian mixtures to spectral data
    #    pass

    def plot(self, ax=None):
        """
        Plot the data including removed observations, latent function, and predictions.

        Args:
            ax (matplotlib.axes.Axes, optional): Draw to this axes, otherwise draw to the current axes.

        Returns:
            matplotlib.axes.Axes
        """
        # TODO: ability to plot conditional or marginal distribution to reduce input dims
        if self.get_input_dims() > 2:
            raise Exception("cannot plot more than two input dimensions")
        if self.get_input_dims() == 2:
            raise Exception("two dimensional input data not yet implemented") # TODO

        if ax == None:
            ax = plt.gca()
        
        ax.set_title(self.name, fontsize=36)
        
        legend = []
        colors = list(matplotlib.colors.TABLEAU_COLORS)
        for i, name in enumerate(self.Y_mu_pred):
            if self.Y_mu_pred[name].size != 0:
                lower = self.Y_mu_pred[name] - self.Y_var_pred[name]
                upper = self.Y_mu_pred[name] + self.Y_var_pred[name]
                ax.plot(self.X_pred[:,0], self.Y_mu_pred[name], ls='-', color=colors[i], lw=2)
                ax.fill_between(self.X_pred[:,0], lower, upper, color=colors[i], alpha=0.1)
                ax.plot(self.X_pred[:,0], lower, ls='-', color=colors[i], lw=1, alpha=0.5)
                ax.plot(self.X_pred[:,0], upper, ls='-', color=colors[i], lw=1, alpha=0.5)
                legend.append(plt.Line2D([0], [0], ls='-', color=colors[i], lw=2, label='Prediction '+name))

        if self.F != None:
            n = len(self.X[:,0])*10
            x_min = np.min(self.X[:,0])
            x_max = np.max(self.X[:,0])
            if len(self.X_pred) != 0:
                x_min = min(x_min, np.min(self.X_pred))
                x_max = max(x_max, np.max(self.X_pred))

            x = np.empty((n, 1))
            x[:,0] = np.linspace(x_min, x_max, n)
            y = self.F(x)

            ax.plot(x[:,0], y, 'r--', lw=1)
            legend.append(plt.Line2D([0], [0], ls='--', color='r', label='Latent function'))

        ax.plot(self.X[:,0], self.Y, 'k-')
        legend.append(plt.Line2D([0], [0], ls='-', color='k', label='Data'))

        if self.has_removed_obs():
            X, Y = self.X[self.mask,:], self.Y[self.mask]
            ax.plot(X[:,0], Y, 'k.', mew=2, ms=5)
            legend.append(plt.Line2D([0], [0], ls='', marker='.', color='k', mew=2, ms=8, label='Training'))

        ax.set_xlabel(self.x_labels[0])
        ax.set_ylabel(self.y_label)
        ax.set_title(self.name, fontsize=30)
        formatter = matplotlib.ticker.FuncFormatter(lambda x,pos: self.formatters[0]._format(x))
        ax.xaxis.set_major_formatter(formatter)

        if 0 < len(legend):
            plt.legend(handles=legend, loc='best')
        return ax

    def plot_spectrum(self, method='lombscargle', ax=None, per=None, maxfreq=None):
        """
        Plot the spectrum of the data.

        Args:
            method (str, optional): Set the method to get the spectrum such as 'lombscargle'.
            ax (matplotlib.axes.Axes, optional): Draw to this axes, otherwise draw to the current axes.
            per (float, str): Set the scale of the X axis depending on the formatter used, eg. per=5 or per='3d' for three days.
            maxfreq (float, optional): Maximum frequency to plot, otherwise the Nyquist frequency is used.

        Returns:
            matplotlib.axes.Axes
        """
        # TODO: ability to plot conditional or marginal distribution to reduce input dims
        if self.get_input_dims() > 2:
            raise Exception("cannot plot more than two input dimensions")
        if self.get_input_dims() == 2:
            raise Exception("two dimensional input data not yet implemented") # TODO

        if ax == None:
            ax = plt.gca()
        
        ax.set_title(self.name, fontsize=36)

        formatter = self.formatters[0]
        factor, name = formatter._scale(per)
        if name != None:
            ax.set_xlabel('Frequency (1/'+name+')')
        else:
            ax.set_xlabel('Frequency')

        X_space = self.X[:,0].copy() / factor
        freq = maxfreq
        if freq == None:
            dist = np.abs(X_space[1:]-X_space[:-1])
            freq = 1/np.average(dist)

        X = np.linspace(0.0, freq, 10001)[1:]
        Y_err = []
        if method == 'lombscargle':
            Y = signal.lombscargle(X_space, self.Y, X)
        elif method == 'bnse':
            # TODO: check if outcome is correct
            nyquist = self.get_nyquist_estimation()
            bnse = bse(X_space, self.Y)
            bnse.set_freqspace(freq/2.0/np.pi, 10001)
            bnse.train()
            bnse.compute_moments()
            Y = bnse.post_mean_r**2 + bnse.post_mean_i**2
            Y_err = 2 * np.sqrt(np.diag(bnse.post_cov_r**2 + bnse.post_cov_i**2))
            Y = Y[1:]
            Y_err = Y_err[1:]
        else:
            raise ValueError('periodogram method "%s" does not exist' % (method))

        ax.plot(X, Y, 'k-')
        if len(Y_err) != 0:
            ax.fill_between(X, Y-Y_err, Y+Y_err, alpha=0.1)
        ax.set_title(self.name + ' spectrum', fontsize=30)
        ax.set_yticks([])
        ax.set_ylim(0, None)

        return ax

    def _transform(self, x, y):
        for t in self.transformations:
            y = t._forward(x, y)
        return y

    def _detransform(self, x, y):
        for t in self.transformations[::-1]:
            y = t._backward(x, y)
        return y

def _check_function(f, input_dims):
    if not inspect.isfunction(f):
        raise ValueError("function must take X as a parameter")

    sig = inspect.signature(f)
    if not len(sig.parameters) == 1:
        raise ValueError("function must take X as a parameter")

    x = np.ones((1, input_dims))
    y = f(x)
    if len(y.shape) != 1 or y.shape[0] != 1:
        raise ValueError("function must return Y with shape (n), note that X has shape (n,input_dims)")

def _normalize_input_dims(x, input_dims):
    if x == None:
        return x
    if isinstance(x, float):
        x = [x]
    elif isinstance(x, int):
        x = [float(x)]
    elif isinstance(x, str):
        x = [x]
    elif isinstance(x, np.ndarray):
        x = list(x)
    elif not isinstance(x, list):
        raise ValueError("input should be a floating point, list or ndarray")
    if input_dims != None and len(x) != input_dims:
        raise ValueError("input must be a scalar for single-dimension input or a list of values for each input dimension")
    return x
    
duration_regex = re.compile(
    r'^((?P<years>[\.\d]+?)y)?'
    r'((?P<months>[\.\d]+?)M)?'
    r'((?P<weeks>[\.\d]+?)w)?'
    r'((?P<days>[\.\d]+?)d)?'
    r'((?P<hours>[\.\d]+?)h)?'
    r'((?P<minutes>[\.\d]+?)m)?'
    r'((?P<seconds>[\.\d]+?)s)?$')

def _parse_duration_to_sec(s):
    x = duration_regex.match(s)
    if x == None:
        raise ValueError('duration string must be of the form 2h45m, allowed characters: (y)ear, (M)onth, (w)eek, (d)ay, (h)our, (m)inute, (s)econd')

    sec = 0
    matches = x.groups()[1::2]
    if matches[0]:
        sec += float(matches[0])*356.2425*24*3600
    if matches[1]:
        sec += float(matches[1])*30.4369*24*3600
    if matches[2]:
        sec += float(matches[2])*7*24*3600
    if matches[3]:
        sec += float(matches[3])*24*3600
    if matches[4]:
        sec += float(matches[4])*3600
    if matches[5]:
        sec += float(matches[5])*60
    if matches[6]:
        sec += float(matches[6])
    return sec
