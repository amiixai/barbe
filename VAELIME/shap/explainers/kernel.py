from ..common import convert_to_instance, convert_to_model, match_instance_to_data, match_model_to_data, convert_to_instance_with_index, convert_to_link, IdentityLink, convert_to_data, DenseData, SparseData
from scipy.special import binom
import numpy as np
import pandas as pd
import scipy as sp
import logging
import copy
import itertools
import warnings
from sklearn.linear_model import LassoLarsIC, Lasso, lars_path
from sklearn.cluster import KMeans
from tqdm.auto import tqdm
from .explainer import Explainer

from Generators.DropoutVAE import DropoutVAE
from sklearn.preprocessing import MinMaxScaler

log = logging.getLogger('shap')


def kmeans(X, k, round_values=True):
    """ Summarize a dataset with k mean samples weighted by the number of data points they
    each represent.

    Parameters
    ----------
    X : numpy.array or pandas.DataFrame
        Matrix of data samples to summarize (# samples x # features)

    k : int
        Number of means to use for approximation.

    round_values : bool
        For all i, round the ith dimension of each mean sample to match the nearest value
        from X[:,i]. This ensures discrete features always get a valid value.

    Returns
    -------
    DenseData object.
    """

    group_names = [str(i) for i in range(X.shape[1])]
    if str(type(X)).endswith("'pandas.core.frame.DataFrame'>"):
        group_names = X.columns
        X = X.values
    kmeans = KMeans(n_clusters=k, random_state=0).fit(X)

    if round_values:
        for i in range(k):
            for j in range(X.shape[1]):
                ind = np.argmin(np.abs(X[:,j] - kmeans.cluster_centers_[i,j]))
                kmeans.cluster_centers_[i,j] = X[ind,j]
    return DenseData(kmeans.cluster_centers_, group_names, None, 1.0*np.bincount(kmeans.labels_))


class KernelExplainer(Explainer):
    """Uses the Kernel SHAP method to explain the output of any function.

    Kernel SHAP is a method that uses a special weighted linear regression
    to compute the importance of each feature. The computed importance values
    are Shapley values from game theory and also coefficents from a local linear
    regression.


    Parameters
    ----------
    model : function or iml.Model
        User supplied function that takes a matrix of samples (# samples x # features) and
        computes a the output of the model for those samples. The output can be a vector
        (# samples) or a matrix (# samples x # model outputs).

    data : numpy.array or pandas.DataFrame or shap.common.DenseData or any scipy.sparse matrix
        The background dataset to use for integrating out features. To determine the impact
        of a feature, that feature is set to "missing" and the change in the model output
        is observed. Since most models aren't designed to handle arbitrary missing data at test
        time, we simulate "missing" by replacing the feature with the values it takes in the
        background dataset. So if the background dataset is a simple sample of all zeros, then
        we would approximate a feature being missing by setting it to zero. For small problems
        this background dataset can be the whole training set, but for larger problems consider
        using a single reference value or using the kmeans function to summarize the dataset.
        Note: for sparse case we accept any sparse matrix but convert to lil format for
        performance. 

    link : "identity" or "logit"
        A generalized linear model link to connect the feature importance values to the model
        output. Since the feature importance values, phi, sum up to the model output, it often makes
        sense to connect them to the ouput with a link function where link(outout) = sum(phi).
        If the model output is a probability then the LogitLink link function makes the feature
        importance values have log-odds units.

    Additional arguments required for use of the generators (all are located in kwargs):
        generator: "DropoutVAE", "RBF" or "Forest". If this argument is not given, basic perturbation sampling
            is used
        generator_specs: Dictionary with values, required by the generator (required if generator is given)
        dummy_idcs: List of lists of indeces that represent one-hot encoding of the same categorical feature
        integer_idcs: List of indeces of integer features
        instance_multiplier: Integer, size of the generated distribution set (set to 100 if not given)
    """

    def __init__(self, model, data, link=IdentityLink(), **kwargs):
        # Check if we have a generator
        self.generator = kwargs.get("generator", None)
        # Check for generator_specs
        if self.generator is not None:
            if kwargs.get("generator_specs") is None:
                raise ValueError("Argument generator_specs required")
            self.generator_specs = kwargs.get("generator_specs")

        # Needed if we use generators
        self.dummy_idcs = kwargs.get("dummy_idcs", [])

        # Train the generator and Scaler if needed
        if self.generator == "DropoutVAE":
            if self.generator_specs.get("original_dim") is None or self.generator_specs.get("latent_dim") is None:
                raise ValueError("original_dim and latent_dim should not be None")
            self.generator = DropoutVAE(original_dim = self.generator_specs["original_dim"],
                                        input_shape = (self.generator_specs["original_dim"],),
                                        intermediate_dim = self.generator_specs.get("intermediate_dim", 128),
                                        dropout = self.generator_specs.get("dropout", 0.3),
                                        latent_dim = self.generator_specs["latent_dim"])
            
            self.scaler = MinMaxScaler()
            data_train = self.scaler.fit_transform(data)
            self.generator.fit_unsplit(data_train, epochs = self.generator_specs.get("epochs", 100))

            self.integer_idcs = kwargs.get("integer_idcs", [])
            self.instance_multiplier = kwargs.get("instance_multiplier", 100)

        # convert incoming inputs to standardized iml objects
        self.link = convert_to_link(link)
        self.model = convert_to_model(model)
        self.keep_index = kwargs.get("keep_index", False)
        self.keep_index_ordered = kwargs.get("keep_index_ordered", False)
        if self.generator in ["RBF", "Forest"]:
            if self.generator_specs.get("experiment") is None or self.generator_specs.get("feature_names") is None:
                raise ValueError("feature_names and experiment should not be None")
            if self.generator == "RBF":
                if self.generator_specs["experiment"] == "Compas":
                    df = pd.read_csv("..\Data\compas_RBF.csv")
                elif self.generator_specs["experiment"] == "German":
                    df = pd.read_csv("..\Data\german_RBF.csv")
                else:
                    df = pd.read_csv("..\Data\cc_RBF.csv")
                if self.generator_specs["experiment"] != "CC":
                    df = pd.get_dummies(df)
                    df = df[self.generator_specs["feature_names"]]
            else:
                if self.generator_specs["experiment"] == "Compas":
                    df = pd.read_csv("..\Data\compas_forest.csv")
                elif self.generator_specs["experiment"] == "German":
                    df = pd.read_csv("..\Data\german_forest.csv")
                else:
                    df = pd.read_csv("..\Data\cc_forest.csv")
                # pri CC presledke spremeni v pike
                if self.generator_specs["experiment"] != "CC":
                    df = pd.get_dummies(df)
                    df = df[self.generator_specs["feature_names"]]

            # Set the groups, so dummies for the same feature are recognized as one feature
            groups = list(range(df.shape[1]))
            for dummy in self.dummy_idcs:
                for idx in dummy:
                    groups.remove(idx)
                groups.append(np.array(dummy))
            # List that can be sorted
            groups = [np.array([el]) if not isinstance(el, np.ndarray) else el for el in groups]
            sortable = [el[0] for el in groups]
            groups = np.array(groups)
            # Put the groups indeces in correct order
            groups = groups[np.argsort(sortable)]
            # Typecast back to list
            groups = groups.tolist()
        
            # Names of the groups are just indeces
            group_names = [str(i) for i in range(len(groups))]

            # Set the distribution set
            self.data = DenseData(df.values, group_names, groups)
        
        # Basic option (distribution set obtained by k means)
        else:
            self.data = convert_to_data(data, keep_index=self.keep_index)
        model_null = match_model_to_data(self.model, self.data)

        # enforce our current input type limitations
        assert isinstance(self.data, DenseData) or isinstance(self.data, SparseData), \
               "Shap explainer only supports the DenseData and SparseData input currently."
        assert not self.data.transposed, "Shap explainer does not support transposed DenseData or SparseData currently."

        # warn users about large background data sets
        # (ignore the warning if you are using data generators, true distribution set is set in shap_values)
        if len(self.data.weights) > 100:
            log.warning("Using " + str(len(self.data.weights)) + " background data samples could cause " +
                        "slower run times. Consider using shap.sample(data, K) or shap.kmeans(data, K) to " +
                        "summarize the background as K samples.")

        # init our parameters
        self.N = self.data.data.shape[0]
        self.P = self.data.data.shape[1]
        self.linkfv = np.vectorize(self.link.f)
        self.nsamplesAdded = 0
        self.nsamplesRun = 0

        # find E_x[f(x)]
        if isinstance(model_null, (pd.DataFrame, pd.Series)):
            model_null = np.squeeze(model_null.values)
        self.fnull = np.sum((model_null.T * self.data.weights).T, 0)
        self.expected_value = self.linkfv(self.fnull)
        
        # see if we have a vector output
        self.vector_out = True
        if len(self.fnull.shape) == 0:
            self.vector_out = False
            self.fnull = np.array([self.fnull])
            self.D = 1
            self.expected_value = float(self.expected_value)
        else:
            self.D = self.fnull.shape[0]
        

    def shap_values(self, X, **kwargs):
        """ Estimate the SHAP values for a set of samples.

        Parameters
        ----------
        X : numpy.array or pandas.DataFrame or any scipy.sparse matrix
            A matrix of samples (# samples x # features) on which to explain the model's output.

        nsamples : "auto" or int
            Number of times to re-evaluate the model when explaining each prediction. More samples
            lead to lower variance estimates of the SHAP values. The "auto" setting uses
            `nsamples = 2 * X.shape[1] + 2048`.

        l1_reg : "num_features(int)", "auto" (default for now, but deprecated), "aic", "bic", or float
            The l1 regularization to use for feature selection (the estimation procedure is based on
            a debiased lasso). The auto option currently uses "aic" when less that 20% of the possible sample
            space is enumerated, otherwise it uses no regularization. THE BEHAVIOR OF "auto" WILL CHANGE
            in a future version to be based on num_features instead of AIC.
            The "aic" and "bic" options use the AIC and BIC rules for regularization.
            Using "num_features(int)" selects a fix number of top features. Passing a float directly sets the
            "alpha" parameter of the sklearn.linear_model.Lasso model used for feature selection.

        Additional arguments if Forest is used as generator (located in kwargs):
            fill_data: Boolean, if True the data fill option is used (different distribution set for every instance).
                False by default.
            data_location: The path to a file, where distribution sets are stored (This is only required because
                treeEnsemble is not yet implemented in Python, so distribution sets have to be pregenerated in R).

        Returns
        -------
        For models with a single output this returns a matrix of SHAP values
        (# samples x # features). Each row sums to the difference between the model output for that
        sample and the expected value of the model output (which is stored as expected_value
        attribute of the explainer). For models with vector outputs this returns a list
        of such matrices, one for each output.
        """

        # convert dataframes
        if str(type(X)).endswith("pandas.core.series.Series'>"):
            X = X.values
        elif str(type(X)).endswith("'pandas.core.frame.DataFrame'>"):
            if self.keep_index:
                index_value = X.index.values
                index_name = X.index.name
                column_name = list(X.columns)
            X = X.values
        
        x_type = str(type(X))
        arr_type = "'numpy.ndarray'>"
        # if sparse, convert to lil for performance
        if sp.sparse.issparse(X) and not sp.sparse.isspmatrix_lil(X):
            X = X.tolil()
        assert x_type.endswith(arr_type) or sp.sparse.isspmatrix_lil(X), "Unknown instance type: " + x_type
        assert len(X.shape) == 1 or len(X.shape) == 2, "Instance must have 1 or 2 dimensions!"

        # single instance
        if len(X.shape) == 1:
            data = X.reshape((1, X.shape[0]))
            if self.keep_index:
                data = convert_to_instance_with_index(data, column_name, index_name, index_value)
            explanation = self.explain(data, **kwargs)

            # vector-output
            s = explanation.shape
            if len(s) == 2:
                outs = [np.zeros(s[0]) for j in range(s[1])]
                for j in range(s[1]):
                    outs[j] = explanation[:, j]
                return outs

            # single-output
            else:
                out = np.zeros(s[0])
                out[:] = explanation
                return out

        # explain the whole dataset
        elif len(X.shape) == 2:
            if self.generator == "Forest":
                # We check if Forest is set to fill data (generate around point)
                self.fill_data = kwargs.get("fill_data", False)
                if self.fill_data:
                    # Beginning of the distribution set for current instance
                    self.forest_index = 0
                    self.distribution_size = kwargs.get("distribution_size", 100)
                    # Location of the data on the hard drive
                    path = kwargs.get("data_location", None)
                    if path == None:
                        raise ValueError("Given location of the generated data is not valid.")
                    self.forest_data = pd.read_csv(path)

                    if self.generator_specs["experiment"] != "CC":
                        self.forest_data = pd.get_dummies(self.forest_data)
                        self.forest_data = self.forest_data[self.generator_specs["feature_names"]]

                    self.forest_data = self.forest_data.values

            explanations = []
            for i in tqdm(range(X.shape[0]), disable=kwargs.get("silent", False)):
                data = X[i:i + 1, :]
                if self.keep_index:
                    data = convert_to_instance_with_index(data, column_name, index_value[i:i + 1], index_name)
                explanations.append(self.explain(data, **kwargs))

            # vector-output
            s = explanations[0].shape
            if len(s) == 2:
                outs = [np.zeros((X.shape[0], s[0])) for j in range(s[1])]
                for i in range(X.shape[0]):
                    for j in range(s[1]):
                        outs[j][i] = explanations[i][:, j]
                return outs

            # single-output
            else:
                out = np.zeros((X.shape[0], s[0]))
                for i in range(X.shape[0]):
                    out[i] = explanations[i]
                return out

    def explain(self, incoming_instance, **kwargs):
        # if generator is DropoutVAE we generate new background distribution
        if (isinstance(self.generator, DropoutVAE)): self.generate_data(incoming_instance)

        # if generator is Forest and we are generating around the instance, we load the next distribution set
        if (self.generator == "Forest" and self.fill_data): self.load_distribution()

        # convert incoming input to a standardized iml object
        instance = convert_to_instance(incoming_instance)
        match_instance_to_data(instance, self.data)

        # find the feature groups we will test. If a feature does not change from its
        # current value then we know it doesn't impact the model
        self.varyingInds = self.varying_groups(instance.x)
        if self.data.groups is None:
            self.varyingFeatureGroups = np.array([i for i in self.varyingInds])
            self.M = self.varyingFeatureGroups.shape[0]
        else:
            self.varyingFeatureGroups = [self.data.groups[i] for i in self.varyingInds]
            self.M = len(self.varyingFeatureGroups)
            groups = self.data.groups
            # convert to numpy array as it is much faster if not jagged array (all groups of same length)
            if self.varyingFeatureGroups and all(len(groups[i]) == len(groups[0]) for i in self.varyingInds):
                self.varyingFeatureGroups = np.array(self.varyingFeatureGroups)
                # further performance optimization in case each group has a single value
                if self.varyingFeatureGroups.shape[1] == 1:
                    self.varyingFeatureGroups = self.varyingFeatureGroups.flatten()

        # find f(x)
        if self.keep_index:
            model_out = self.model.f(instance.convert_to_df())
        else:
            model_out = self.model.f(instance.x)
        if isinstance(model_out, (pd.DataFrame, pd.Series)):
            model_out = model_out.values
        self.fx = model_out[0]

        if not self.vector_out:
            self.fx = np.array([self.fx])

        # if no features vary then no feature has an effect
        if self.M == 0:
            phi = np.zeros((self.data.groups_size, self.D))
            phi_var = np.zeros((self.data.groups_size, self.D))

        # if only one feature varies then it has all the effect
        elif self.M == 1:
            phi = np.zeros((self.data.groups_size, self.D))
            phi_var = np.zeros((self.data.groups_size, self.D))
            diff = self.link.f(self.fx) - self.link.f(self.fnull)
            for d in range(self.D):
                phi[self.varyingInds[0],d] = diff[d]

        # if more than one feature varies then we have to do real work
        else:
            self.l1_reg = kwargs.get("l1_reg", "auto")

            # pick a reasonable number of samples if the user didn't specify how many they wanted
            self.nsamples = kwargs.get("nsamples", "auto")
            if self.nsamples == "auto":
                self.nsamples = 2 * self.M + 2**11

            # if we have enough samples to enumerate all subsets then ignore the unneeded samples
            self.max_samples = 2 ** 30
            if self.M <= 30:
                self.max_samples = 2 ** self.M - 2
                if self.nsamples > self.max_samples:
                    self.nsamples = self.max_samples

            # reserve space for some of our computations
            self.allocate()

            # weight the different subset sizes
            num_subset_sizes = np.int(np.ceil((self.M - 1) / 2.0))
            num_paired_subset_sizes = np.int(np.floor((self.M - 1) / 2.0))
            weight_vector = np.array([(self.M - 1.0) / (i * (self.M - i)) for i in range(1, num_subset_sizes + 1)])
            weight_vector[:num_paired_subset_sizes] *= 2
            weight_vector /= np.sum(weight_vector)
            log.debug("weight_vector = {0}".format(weight_vector))
            log.debug("num_subset_sizes = {0}".format(num_subset_sizes))
            log.debug("num_paired_subset_sizes = {0}".format(num_paired_subset_sizes))
            log.debug("M = {0}".format(self.M))

            # fill out all the subset sizes we can completely enumerate
            # given nsamples*remaining_weight_vector[subset_size]
            num_full_subsets = 0
            num_samples_left = self.nsamples
            group_inds = np.arange(self.M, dtype='int64') 
            mask = np.zeros(self.M)
            remaining_weight_vector = copy.copy(weight_vector)
            for subset_size in range(1, num_subset_sizes + 1):

                # determine how many subsets (and their complements) are of the current size
                nsubsets = binom(self.M, subset_size)
                if subset_size <= num_paired_subset_sizes: nsubsets *= 2
                log.debug("subset_size = {0}".format(subset_size))
                log.debug("nsubsets = {0}".format(nsubsets))
                log.debug("self.nsamples*weight_vector[subset_size-1] = {0}".format(
                    num_samples_left * remaining_weight_vector[subset_size - 1]))
                log.debug("self.nsamples*weight_vector[subset_size-1]/nsubsets = {0}".format(
                    num_samples_left * remaining_weight_vector[subset_size - 1] / nsubsets))

                # see if we have enough samples to enumerate all subsets of this size
                if num_samples_left * remaining_weight_vector[subset_size - 1] / nsubsets >= 1.0 - 1e-8:
                    num_full_subsets += 1
                    num_samples_left -= nsubsets

                    # rescale what's left of the remaining weight vector to sum to 1
                    if remaining_weight_vector[subset_size - 1] < 1.0:
                        remaining_weight_vector /= (1 - remaining_weight_vector[subset_size - 1])

                    # add all the samples of the current subset size
                    w = weight_vector[subset_size - 1] / binom(self.M, subset_size)
                    if subset_size <= num_paired_subset_sizes: w /= 2.0
                    for inds in itertools.combinations(group_inds, subset_size):
                        mask[:] = 0.0
                        mask[np.array(inds, dtype='int64')] = 1.0
                        self.addsample(instance.x, mask, w)
                        if subset_size <= num_paired_subset_sizes:
                            mask[:] = np.abs(mask - 1)
                            self.addsample(instance.x, mask, w)
                else:
                    break
            log.info("num_full_subsets = {0}".format(num_full_subsets))

            # add random samples from what is left of the subset space
            nfixed_samples = self.nsamplesAdded
            samples_left = self.nsamples - self.nsamplesAdded
            log.debug("samples_left = {0}".format(samples_left))
            if num_full_subsets != num_subset_sizes:
                remaining_weight_vector = copy.copy(weight_vector)
                remaining_weight_vector[:num_paired_subset_sizes] /= 2 # because we draw two samples each below
                remaining_weight_vector = remaining_weight_vector[num_full_subsets:]
                remaining_weight_vector /= np.sum(remaining_weight_vector)
                log.info("remaining_weight_vector = {0}".format(remaining_weight_vector))
                log.info("num_paired_subset_sizes = {0}".format(num_paired_subset_sizes))
                ind_set = np.random.choice(len(remaining_weight_vector), 4 * samples_left, p=remaining_weight_vector)
                ind_set_pos = 0
                used_masks = {}
                while samples_left > 0 and ind_set_pos < len(ind_set):
                    mask.fill(0.0)
                    ind = ind_set[ind_set_pos] # we call np.random.choice once to save time and then just read it here
                    ind_set_pos += 1
                    subset_size = ind + num_full_subsets + 1
                    mask[np.random.permutation(self.M)[:subset_size]] = 1.0

                    # only add the sample if we have not seen it before, otherwise just
                    # increment a previous sample's weight
                    mask_tuple = tuple(mask)
                    new_sample = False
                    if mask_tuple not in used_masks:
                        new_sample = True
                        used_masks[mask_tuple] = self.nsamplesAdded
                        samples_left -= 1
                        self.addsample(instance.x, mask, 1.0)
                    else:
                        self.kernelWeights[used_masks[mask_tuple]] += 1.0

                    # add the compliment sample
                    if samples_left > 0 and subset_size <= num_paired_subset_sizes:
                        mask[:] = np.abs(mask - 1)

                        # only add the sample if we have not seen it before, otherwise just
                        # increment a previous sample's weight
                        if new_sample:
                            samples_left -= 1
                            self.addsample(instance.x, mask, 1.0)
                        else:
                            # we know the compliment sample is the next one after the original sample, so + 1
                            self.kernelWeights[used_masks[mask_tuple] + 1] += 1.0

                # normalize the kernel weights for the random samples to equal the weight left after
                # the fixed enumerated samples have been already counted
                weight_left = np.sum(weight_vector[num_full_subsets:])
                log.info("weight_left = {0}".format(weight_left))
                self.kernelWeights[nfixed_samples:] *= weight_left / self.kernelWeights[nfixed_samples:].sum()

            # execute the model on the synthetic samples we have created
            self.run()

            # solve then expand the feature importance (Shapley value) vector to contain the non-varying features
            phi = np.zeros((self.data.groups_size, self.D))
            phi_var = np.zeros((self.data.groups_size, self.D))
            for d in range(self.D):
                vphi, vphi_var = self.solve(self.nsamples / self.max_samples, d)
                phi[self.varyingInds, d] = vphi
                phi_var[self.varyingInds, d] = vphi_var

        if not self.vector_out:
            phi = np.squeeze(phi, axis=1)
            phi_var = np.squeeze(phi_var, axis=1)

        return phi

    def varying_groups(self, x):
        if not sp.sparse.issparse(x):
            varying = np.zeros(self.data.groups_size)
            for i in range(0, self.data.groups_size):
                inds = self.data.groups[i]
                x_group = x[0, inds]
                if sp.sparse.issparse(x_group):
                    if all(j not in x.nonzero()[1] for j in inds):
                        varying[i] = False
                        continue
                    x_group = x_group.todense()
                num_mismatches = np.sum(np.invert(np.isclose(x_group, self.data.data[:, inds], equal_nan=True)))
                varying[i] = num_mismatches > 0
            varying_indices = np.nonzero(varying)[0]
            return varying_indices
        else:
            varying_indices = []
            # go over all nonzero columns in background and evaluation data
            # if both background and evaluation are zero, the column does not vary
            varying_indices = np.unique(np.union1d(self.data.data.nonzero()[1], x.nonzero()[1]))
            remove_unvarying_indices = []
            for i in range(0, len(varying_indices)):
                varying_index = varying_indices[i]
                # now verify the nonzero values do vary
                data_rows = self.data.data[:, [varying_index]]
                nonzero_rows = data_rows.nonzero()[0]

                if nonzero_rows.size > 0:
                    background_data_rows = data_rows[nonzero_rows]
                    if sp.sparse.issparse(background_data_rows):
                        background_data_rows = background_data_rows.toarray()
                    num_mismatches = np.sum(np.abs(background_data_rows - x[0, varying_index]) > 1e-7)
                    # Note: If feature column non-zero but some background zero, can't remove index
                    if num_mismatches == 0 and not \
                        (np.abs(x[0, [varying_index]][0, 0]) > 1e-7 and len(nonzero_rows) < data_rows.shape[0]):
                        remove_unvarying_indices.append(i)
            mask = np.ones(len(varying_indices), dtype=bool)
            mask[remove_unvarying_indices] = False
            varying_indices = varying_indices[mask]
            return varying_indices

    def allocate(self):
        if sp.sparse.issparse(self.data.data):
            # We tile the sparse matrix in csr format but convert it to lil
            # for performance when adding samples
            shape = self.data.data.shape
            nnz = self.data.data.nnz
            data_rows, data_cols = shape
            rows = data_rows * self.nsamples
            shape = rows, data_cols
            if nnz == 0:
                self.synth_data = sp.sparse.csr_matrix(shape, dtype=self.data.data.dtype).tolil()
            else:
                data = self.data.data.data
                indices = self.data.data.indices
                indptr = self.data.data.indptr
                last_indptr_idx = indptr[len(indptr) - 1]
                indptr_wo_last = indptr[:-1]
                new_indptrs = []
                for i in range(0, self.nsamples - 1):
                    new_indptrs.append(indptr_wo_last + (i * last_indptr_idx))
                new_indptrs.append(indptr + ((self.nsamples - 1) * last_indptr_idx))
                new_indptr = np.concatenate(new_indptrs)
                new_data = np.tile(data, self.nsamples)
                new_indices = np.tile(indices, self.nsamples)
                self.synth_data = sp.sparse.csr_matrix((new_data, new_indices, new_indptr), shape=shape).tolil()
        else:
            self.synth_data = np.tile(self.data.data, (self.nsamples, 1))

        self.maskMatrix = np.zeros((self.nsamples, self.M))
        self.kernelWeights = np.zeros(self.nsamples)
        self.y = np.zeros((self.nsamples * self.N, self.D))
        self.ey = np.zeros((self.nsamples, self.D))
        self.lastMask = np.zeros(self.nsamples)
        self.nsamplesAdded = 0
        self.nsamplesRun = 0
        if self.keep_index:
            self.synth_data_index = np.tile(self.data.index_value, self.nsamples)

    def addsample(self, x, m, w):
        offset = self.nsamplesAdded * self.N
        if isinstance(self.varyingFeatureGroups, (list,)):
            for j in range(self.M):
                for k in self.varyingFeatureGroups[j]:
                    if m[j] == 1.0:
                        self.synth_data[offset:offset+self.N, k] = x[0, k]
        else:
            # for non-jagged numpy array we can significantly boost performance
            mask = m == 1.0
            groups = self.varyingFeatureGroups[mask]
            if len(groups.shape) == 2:
                for group in groups:
                    self.synth_data[offset:offset+self.N, group] = x[0, group]
            else:
                # further performance optimization in case each group has a single feature
                evaluation_data = x[0, groups]
                # In edge case where background is all dense but evaluation data
                # is all sparse, make evaluation data dense
                if sp.sparse.issparse(x) and not sp.sparse.issparse(self.synth_data):
                    evaluation_data = evaluation_data.toarray()
                self.synth_data[offset:offset+self.N, groups] = evaluation_data
        self.maskMatrix[self.nsamplesAdded, :] = m
        self.kernelWeights[self.nsamplesAdded] = w
        self.nsamplesAdded += 1

    def run(self):
        num_to_run = self.nsamplesAdded * self.N - self.nsamplesRun * self.N
        data = self.synth_data[self.nsamplesRun*self.N:self.nsamplesAdded*self.N,:]
        if self.keep_index:
            index = self.synth_data_index[self.nsamplesRun*self.N:self.nsamplesAdded*self.N]
            index = pd.DataFrame(index, columns=[self.data.index_name])
            data = pd.DataFrame(data, columns=self.data.group_names)
            data = pd.concat([index, data], axis=1).set_index(self.data.index_name)
            if self.keep_index_ordered:
                data = data.sort_index()
        modelOut = self.model.f(data)
        if isinstance(modelOut, (pd.DataFrame, pd.Series)):
            modelOut = modelOut.values
        self.y[self.nsamplesRun * self.N:self.nsamplesAdded * self.N, :] = np.reshape(modelOut, (num_to_run, self.D))

        # find the expected value of each output
        for i in range(self.nsamplesRun, self.nsamplesAdded):
            eyVal = np.zeros(self.D)
            for j in range(0, self.N):
                eyVal += self.y[i * self.N + j, :] * self.data.weights[j]

            self.ey[i, :] = eyVal
            self.nsamplesRun += 1

    def solve(self, fraction_evaluated, dim):
        eyAdj = self.linkfv(self.ey[:, dim]) - self.link.f(self.fnull[dim])
        s = np.sum(self.maskMatrix, 1)

        # do feature selection if we have not well enumerated the space
        nonzero_inds = np.arange(self.M)
        log.debug("fraction_evaluated = {0}".format(fraction_evaluated))
        if self.l1_reg == "auto":
            warnings.warn(
                "l1_reg=\"auto\" is deprecated and in the next version (v0.29) the behavior will change from a " \
                "conditional use of AIC to simply \"num_features(10)\"!"
            )
        if (self.l1_reg not in ["auto", False, 0]) or (fraction_evaluated < 0.2 and self.l1_reg == "auto"):
            w_aug = np.hstack((self.kernelWeights * (self.M - s), self.kernelWeights * s))
            log.info("np.sum(w_aug) = {0}".format(np.sum(w_aug)))
            log.info("np.sum(self.kernelWeights) = {0}".format(np.sum(self.kernelWeights)))
            w_sqrt_aug = np.sqrt(w_aug)
            eyAdj_aug = np.hstack((eyAdj, eyAdj - (self.link.f(self.fx[dim]) - self.link.f(self.fnull[dim]))))
            eyAdj_aug *= w_sqrt_aug
            mask_aug = np.transpose(w_sqrt_aug * np.transpose(np.vstack((self.maskMatrix, self.maskMatrix - 1))))
            #var_norms = np.array([np.linalg.norm(mask_aug[:, i]) for i in range(mask_aug.shape[1])])

            # select a fixed number of top features
            if isinstance(self.l1_reg, str) and self.l1_reg.startswith("num_features("):
                r = int(self.l1_reg[len("num_features("):-1])
                nonzero_inds = lars_path(mask_aug, eyAdj_aug, max_iter=r)[1]
            
            # use an adaptive regularization method
            elif self.l1_reg == "auto" or self.l1_reg == "bic" or self.l1_reg == "aic":
                c = "aic" if self.l1_reg == "auto" else self.l1_reg
                nonzero_inds = np.nonzero(LassoLarsIC(criterion=c).fit(mask_aug, eyAdj_aug).coef_)[0]
            
            # use a fixed regularization coeffcient
            else:
                nonzero_inds = np.nonzero(Lasso(alpha=self.l1_reg).fit(mask_aug, eyAdj_aug).coef_)[0]

        if len(nonzero_inds) == 0:
            return np.zeros(self.M), np.ones(self.M)

        # eliminate one variable with the constraint that all features sum to the output
        eyAdj2 = eyAdj - self.maskMatrix[:, nonzero_inds[-1]] * (
                    self.link.f(self.fx[dim]) - self.link.f(self.fnull[dim]))
        etmp = np.transpose(np.transpose(self.maskMatrix[:, nonzero_inds[:-1]]) - self.maskMatrix[:, nonzero_inds[-1]])
        log.debug("etmp[:4,:] {0}".format(etmp[:4, :]))

        # solve a weighted least squares equation to estimate phi
        tmp = np.transpose(np.transpose(etmp) * np.transpose(self.kernelWeights))
        tmp2 = np.linalg.inv(np.dot(np.transpose(tmp), etmp))
        w = np.dot(tmp2, np.dot(np.transpose(tmp), eyAdj2))
        log.debug("np.sum(w) = {0}".format(np.sum(w)))
        log.debug("self.link(self.fx) - self.link(self.fnull) = {0}".format(
            self.link.f(self.fx[dim]) - self.link.f(self.fnull[dim])))
        log.debug("self.fx = {0}".format(self.fx[dim]))
        log.debug("self.link(self.fx) = {0}".format(self.link.f(self.fx[dim])))
        log.debug("self.fnull = {0}".format(self.fnull[dim]))
        log.debug("self.link(self.fnull) = {0}".format(self.link.f(self.fnull[dim])))
        phi = np.zeros(self.M)
        phi[nonzero_inds[:-1]] = w
        phi[nonzero_inds[-1]] = (self.link.f(self.fx[dim]) - self.link.f(self.fnull[dim])) - sum(w)
        log.info("phi = {0}".format(phi))

        # clean up any rounding errors
        for i in range(self.M):
            if np.abs(phi[i]) < 1e-10:
                phi[i] = 0

        return phi, np.ones(len(phi))

    '''
    Function that sets the distribution set so it consists out of instances that are generated by MCD-VAE in
    the vicinity of data_row.

    Arguments:
    data_row: Numpy array, instance that is being explained.
    '''
    def generate_data(self, data_row):
        # Generate new data with MCD-VAE
        reshaped = data_row.reshape(1, -1)
        scaled = self.scaler.transform(reshaped)
        scaled = np.reshape(scaled, (-1, data_row.shape[1]))
        encoded = self.generator.mean_predict(scaled, nums = self.instance_multiplier)

        generated_data = encoded.reshape(self.instance_multiplier*encoded.shape[2], data_row.shape[1])
        generated_data = self.scaler.inverse_transform(generated_data)

        # Round up the integer features
        generated_data[:, self.integer_idcs] = (np.around(generated_data[:, self.integer_idcs])).astype(int)

        # Set categorical features to one-hot encoded (1 is given to the dummy with highest value inside the dummies
        # that represent the same categorical feature)
        for feature in self.dummy_idcs:
            column = generated_data[:, feature]
            binary = np.zeros(column.shape)
            # Check if categorical feature has only 2 possible values, otherwise there is more than one dummy
            if len(feature) == 1:
                binary = (column > 0.5).astype(int)
            else:
                # Dummy with the highest value is set to 1
                ones = column.argmax(axis = 1)
                for i, idx in enumerate(ones):
                    binary[i, idx] = 1
            generated_data[:, feature] = binary

        # Set the groups (so the dummies representing the same feature are in one group)
        groups = list(range(reshaped.shape[1]))
        for dummy in self.dummy_idcs:
            for idx in dummy:
                groups.remove(idx)
            groups.append(np.array(dummy))
        # List that can be sorted
        groups = [np.array([el]) if not isinstance(el, np.ndarray) else el for el in groups]
        sortable = [el[0] for el in groups]
        groups = np.array(groups)
        # Sort the groups
        groups = groups[np.argsort(sortable)]
        # Typecast back to list
        groups = groups.tolist()
        
        # Group names are their indices
        group_names = [str(i) for i in range(len(groups))]

        # Set the distribution set
        self.data = DenseData(generated_data, group_names, groups)

        # Code from constructor that calculates the expected values and sets the required class attributes
        model_null = match_model_to_data(self.model, self.data)
        self.N = self.data.data.shape[0]

        # find E_x[f(x)]
        if isinstance(model_null, (pd.DataFrame, pd.Series)):
            model_null = np.squeeze(model_null.values)
        self.fnull = np.sum((model_null.T * self.data.weights).T, 0)
        self.expected_value = self.linkfv(self.fnull)

        # see if we have a vector output
        self.vector_out = True
        if len(self.fnull.shape) == 0:
            self.vector_out = False
            self.fnull = np.array([self.fnull])
            self.D = 1
            self.expected_value = float(self.expected_value)
        else:
            self.D = self.fnull.shape[0]

    '''
    Function that sets the distribution set to the correct subset of data that was loaded and pregenerated
    in R with treeEnsemble with data fill enabled.
    '''
    def load_distribution(self):
        # Next distribution set
        distribution_set = self.forest_data[self.forest_index:self.forest_index + self.distribution_size, :]
        # Update the index
        self.forest_index += self.distribution_size

        # Group the dummies
        groups = list(range(distribution_set.shape[1]))
        for dummy in self.dummy_idcs:
            for idx in dummy:
                groups.remove(idx)
            groups.append(np.array(dummy))
        # List that can be sorted
        groups = [np.array([el]) if not isinstance(el, np.ndarray) else el for el in groups]
        sortable = [el[0] for el in groups]
        groups = np.array(groups)
        # Sort the groups
        groups = groups[np.argsort(sortable)]
        # Typecast back to list
        groups = groups.tolist()

        # Group names are their indices
        group_names = [str(i) for i in range(len(groups))]

        # Set the distribution set
        self.data = DenseData(distribution_set, group_names, groups)

        # Code from constructor that calculates the expected values and sets the required class attributes
        model_null = match_model_to_data(self.model, self.data)
        self.N = self.data.data.shape[0]

        # find E_x[f(x)]
        if isinstance(model_null, (pd.DataFrame, pd.Series)):
            model_null = np.squeeze(model_null.values)
        self.fnull = np.sum((model_null.T * self.data.weights).T, 0)
        self.expected_value = self.linkfv(self.fnull)

        # see if we have a vector output
        self.vector_out = True
        if len(self.fnull.shape) == 0:
            self.vector_out = False
            self.fnull = np.array([self.fnull])
            self.D = 1
            self.expected_value = float(self.expected_value)
        else:
            self.D = self.fnull.shape[0]