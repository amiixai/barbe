"""
Discretizers classes, to be used in lime_tabular
"""
import numpy as np
import sklearn
import sklearn.tree
import scipy
from sklearn.utils import check_random_state
from abc import ABCMeta, abstractmethod


class BaseDiscretizer():
    """
    Abstract class - Build a class that inherits from this class to implement
    a custom discretizer.
    Method bins() is to be redefined in the child class, as it is the actual
    custom part of the discretizer.
    """

    __metaclass__ = ABCMeta  # abstract class

    def __init__(self, data, categorical_features, feature_names, labels=None, random_state=None,
                 data_stats=None):
        """Initializer
        Args:
            data: numpy 2d array
            categorical_features: list of indices (ints) corresponding to the
                categorical columns. These features will not be discretized.
                Everything else will be considered continuous, and will be
                discretized.
            categorical_names: map from int to list of names, where
                categorical_names[x][y] represents the name of the yth value of
                column x.
            feature_names: list of names (strings) corresponding to the columns
                in the training data.
            data_stats: must have 'means', 'stds', 'mins' and 'maxs', use this
                if you don't want these values to be computed from data
        """
        self.to_discretize = ([x for x in range(data.shape[1])
                               if x not in categorical_features])
        print("DI", self.to_discretize)
        
        self.data_stats = data_stats
        self.names = {}
        self.lambdas = {}
        self.means = {}
        self.stds = {}
        self.mins = {}
        self.maxs = {}
        self.precompute_size = 10000
        self.undiscretize_idxs = {}
        self.undiscretize_precomputed = {}
        self.random_state = check_random_state(random_state)

        # To override when implementing a custom binning
        bins = self.bins(data, labels)
        bins = [np.unique(x) for x in bins]
        print('In BaseDiscretizer: self.to_discretize = ', self.to_discretize, 'bins = ', bins)

        # Read the stats from data_stats if exists
        if data_stats:
            self.means = self.data_stats.get("means")
            self.stds = self.data_stats.get("stds")
            self.mins = self.data_stats.get("mins")
            self.maxs = self.data_stats.get("maxs")

        for feature, qts in zip(self.to_discretize, bins):
            if qts is not None:
                print(str(qts))
                n_bins = qts.shape[0]  # Actually number of borders (= #bins-1)
                boundaries = np.min(data[:, feature]), np.max(data[:, feature])
                name = feature_names[feature]

                self.names[feature] = ['%s <= %.2f' % (name, qts[0])]
                self.undiscretize_idxs[feature] = (
                    [self.precompute_size] * (n_bins + 1))
                self.undiscretize_precomputed[feature] = [[]] * (n_bins + 1)
                for i in range(n_bins - 1):
                    self.names[feature].append('%.2f < %s <= %.2f' %
                                               (qts[i], name, qts[i + 1]))
                self.names[feature].append('%s > %.2f' % (name, qts[n_bins - 1]))

                self.lambdas[feature] = lambda x, qts=qts: np.searchsorted(qts, x)
                discretized = self.lambdas[feature](data[:, feature])

                # If data stats are provided no need to compute the below set of details
                if data_stats:
                    [self.get_undiscretize_value(feature, i)
                     for i in range(n_bins + 1)]
                    continue

                self.means[feature] = []
                self.stds[feature] = []
                for x in range(n_bins + 1):
                    selection = data[discretized == x, feature]
                    mean = 0 if len(selection) == 0 else np.mean(selection)
                    self.means[feature].append(mean)
                    std = 0 if len(selection) == 0 else np.std(selection)
                    std += 0.00000000001
                    self.stds[feature].append(std)

                self.mins[feature] = [boundaries[0]] + qts.tolist()
                self.maxs[feature] = qts.tolist() + [boundaries[1]]
                [self.get_undiscretize_value(feature, i)
                 for i in range(n_bins + 1)]

    @abstractmethod
    def bins(self, data, labels):
        """
        To be overridden
        Returns for each feature to discretize the boundaries
        that form each bin of the discretizer
        """
        raise NotImplementedError("Must override bins() method")

    def discretize(self, data):
        """Discretizes the data.
        Args:
            data: numpy 2d or 1d array
        Returns:
            numpy array of same dimension, discretized.
        """
        # they are off by one!
        ret = data.copy()
        for feature in self.lambdas:
            if len(data.shape) == 1:
                ret[feature] = int(self.lambdas[feature](ret[feature]))
            else:
                ret[:, feature] = self.lambdas[feature](
                    ret[:, feature]).astype(int)
        return ret

    def get_undiscretize_value(self, feature, val):
        if self.undiscretize_idxs[feature][val] == self.precompute_size:
            self.undiscretize_idxs[feature][val] = 0
            mins = self.mins[feature]
            maxs = self.maxs[feature]
            means = self.means[feature]
            stds = self.stds[feature]
            minz = (mins[val] - means[val]) / stds[val]
            maxz = (maxs[val] - means[val]) / stds[val]
            try:
                self.undiscretize_precomputed[feature][val] = (
                    scipy.stats.truncnorm.rvs(
                        minz, maxz, loc=means[val], scale=stds[val],
                        random_state=self.random_state,
                        size=self.precompute_size))
            except Exception as e:
                self.undiscretize_precomputed[feature][val] = (
                    np.ones(self.precompute_size) * minz)
        idx = self.undiscretize_idxs[feature][val]
        ret = self.undiscretize_precomputed[feature][val][idx]
        ret =  1000000. if np.isinf(ret) else ret
        self.undiscretize_idxs[feature][val] += 1
        return ret

    def undiscretize(self, data):
        ret = data.copy()
        for feature in self.means:
            if len(data.shape) == 1:
                q = int(ret[feature])
                ret[feature] = self.get_undiscretize_value(feature, q)
            else:
                ret[:, feature] = (
                    [self.get_undiscretize_value(feature, int(x))
                     for x in ret[:, feature]])
        return ret


class StatsDiscretizer(BaseDiscretizer):
    """
        Class to be used to supply the data stats info when discretize_continuous is true
    """

    def __init__(self, data, categorical_features, feature_names, labels=None, random_state=None,
                 data_stats=None):

        BaseDiscretizer.__init__(self, data, categorical_features,
                                 feature_names, labels=labels,
                                 random_state=random_state,
                                 data_stats=data_stats)

    def bins(self, data, labels):
        bins_from_stats = self.data_stats.get("bins")
        bins = []
        if bins_from_stats is not None:
            for feature in self.to_discretize:
                bins_from_stats_feature = bins_from_stats.get(feature)
                if bins_from_stats_feature is not None:
                    qts = np.array(bins_from_stats_feature)
                    bins.append(qts)
        return bins


class QuartileDiscretizer(BaseDiscretizer):
    def __init__(self, data, categorical_features, feature_names, labels=None, random_state=None):

        BaseDiscretizer.__init__(self, data, categorical_features,
                                 feature_names, labels=labels,
                                 random_state=random_state)

    def bins(self, data, labels):
        bins = []
        for feature in self.to_discretize:
            qts = np.array(np.percentile(data[:, feature], [25, 50, 75]))
            bins.append(qts)
        return bins


class DecileDiscretizer(BaseDiscretizer):
    def __init__(self, data, categorical_features, feature_names, labels=None, random_state=None):
        BaseDiscretizer.__init__(self, data, categorical_features,
                                 feature_names, labels=labels,
                                 random_state=random_state)

    def bins(self, data, labels):
        bins = []
        for feature in self.to_discretize:
            for i in range(10,2,-1):
                try:
                    qts = np.array(np.percentile(data[:, feature], np.arange(100/i, 100, 100/i, dtype=int)))
                    break
                except Exception as e:
                    print(str(e))
                    pass
            bins.append(qts)
#         print('###### bins ########')
#         print(bins)
        return bins


class EntropyDiscretizer(BaseDiscretizer):
    def __init__(self, data, categorical_features, feature_names, labels=None, random_state=None):
        if(labels is None):
            raise ValueError('Labels must be not None when using \
                             EntropyDiscretizer')
        BaseDiscretizer.__init__(self, data, categorical_features,
                                 feature_names, labels=labels,
                                 random_state=random_state)

    def bins(self, data, labels):
        bins = []
        for feature in self.to_discretize:
            # Entropy splitting / at most 8 bins so max_depth=3
            dt = sklearn.tree.DecisionTreeClassifier(criterion='entropy',
                                                     max_depth=3,
                                                     random_state=self.random_state)
            x = np.reshape(data[:, feature], (-1, 1))
            dt.fit(x, labels)
            qts = dt.tree_.threshold[np.where(dt.tree_.children_left > -1)]

            if qts.shape[0] == 0:
                qts = np.array([np.median(data[:, feature])])
            else:
                qts = np.sort(qts)

            bins.append(qts)

        return bins
