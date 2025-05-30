"""
The SHAP experiment MAIN for Communities and Crime.
 * Run the file and the CC experiments will complete
 * See compas experiment file for more details on how to read results.
"""
import warnings
warnings.filterwarnings('ignore') 

from adversarial_models import * 
from utils import *
from get_data import *

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_samples, silhouette_score

import matplotlib.pyplot as plt
import matplotlib.cm as cm

import numpy as np
import pandas as pd

import lime
import lime.lime_tabular
import shap

from copy import deepcopy

import csv

''' Function that helps to determine the number of clusters with silhouette score
X -> numpy array, data
n_clusters_list -> list of candidates for number of clusters
'''
def s_score(X, n_clusters_list):
	for n_clusters in n_clusters_list:
		# Create a subplot with 1 row and 2 columns
		fig, ax = plt.subplots(1, 1)
		fig.set_size_inches(18, 7)

		# The 1st subplot is the silhouette plot
		# The silhouette coefficient can range from -1, 1 but in this example all
		# lie within [-0.1, 1]
		ax.set_xlim([-0.1, 1])
		# The (n_clusters+1)*10 is for inserting blank space between silhouette
		# plots of individual clusters, to demarcate them clearly.
		ax.set_ylim([0, len(X) + (n_clusters + 1) * 10])

		# Clustering
		k_means = KMeans(n_clusters=n_clusters)
		clusters = k_means.fit_predict(X)

		# Average silhouette score
		silhouette_avg = silhouette_score(X, clusters)
		# Silhouette score for every instance
		silhouette_values = silhouette_samples(X, clusters)

		# Start with y=10, so there are spaces between clusters on graph
		y_lower = 10
		for i in range(n_clusters):
			cluster_silhouette_values = silhouette_values[clusters == i]
			cluster_silhouette_values.sort()
			
			# Upper bound for y
			y_upper = y_lower + cluster_silhouette_values.shape[0]
			# Color of the cluster
			color = cm.nipy_spectral(float(i) / n_clusters)
			# Fill the figure
			ax.fill_betweenx(np.arange(y_lower, y_upper),
							0, cluster_silhouette_values,
							facecolor=color, edgecolor=color, alpha=0.7)
			
			# Label the silhouette plots with their cluster numbers at the middle
			ax.text(-0.05, y_lower + 0.5 * cluster_silhouette_values.shape[0], str(i))

			# Increase lower bound for y so next cluster is 10 above this one
			y_lower = y_upper + 10

		ax.set_title(f"Display of silhouette scores for {n_clusters} clusters")
		ax.set_xlabel("Silhouette score")
		ax.set_ylabel("Cluster label")

		# The vertical line for average silhouette score of all the values
		ax.axvline(x=silhouette_avg, color="red", linestyle="--")

		ax.set_yticks([])  # Clear the yaxis labels / ticks
		ax.set_xticks([-0.1, 0, 0.2, 0.4, 0.6, 0.8, 1])

	plt.show()

# Set up experiment parameters
params = Params("model_configurations/experiment_params.json")
np.random.seed(params.seed)

# Prepare data (use this option if you are not using treeEnsemble)
'''
X, y, cols = get_and_preprocess_cc(params)

# add unrelated columns, setup
X['unrelated_column_one'] = np.random.choice([0,1],size=X.shape[0])
X['unrelated_column_two'] = np.random.choice([0,1],size=X.shape[0])

# Split the data into train and test set
data_train, data_test, ytrain, ytest = train_test_split(X, y, test_size=0.1)'''

# If data was split before the experiment (required for treeEnsemble)
data_train = pd.read_csv("..\Data\cc_forest_train.csv")
data_test = pd.read_csv("..\Data\cc_forest_test.csv")
ytrain = data_train.pop("response")
ytest = data_test.pop("response")

# Save the data, so we can generate new samples in R
data_train["response"] = ytrain
data_train.to_csv("..\Data\cc_RBF_train.csv", index = False)

# Stops the execution of experiment so generators have time to generate data in R
input("Press enter, when rbfDataGen and treeEnsemble generated all the data.")

data_train = data_train.drop("response", axis = 1)
features = [c for c in data_train]

# Only categorical features are unrelated columns
categorical_features = ["unrelated_column_one", "unrelated_column_two"]
categorical_idcs = [features.index("unrelated_column_one"), features.index("unrelated_column_two")]
dummy_idcs = [[categorical_idcs[0]], [categorical_idcs[1]]]

# Find integer features
integer_attributes = [i for i, feature in enumerate(data_test.columns)
					if (data_test[feature].dtype in ["int64", "int32", "int8", "uint64", "uint32", "uint8"] and i not in categorical_idcs)]

# Original names are the same as features as there are no one-hot encoded features
original_names = features

# Parameters for MCD-VAE
latent_dim = data_train.shape[1] // 2
original_dim = data_train.shape[1]

# Indices of sensitive and unrelated features
race_indc = features.index('racePctWhite numeric')
unrelated_indcs = features.index('unrelated_column_one')
unrelated_indcs1 = features.index('unrelated_column_two')

xtrain = data_train.values
xtest = data_test.values

# for racist model
mean_race = np.mean(xtrain[:,race_indc])

###
## The models f and psi for CC.  We discriminate based on race for f and concider two RANDOMLY DRAWN features to display in psi
#

# the biased model 
class racist_model_f:
    # Decision rule: classify negatively if race is black
    def predict(self,X):
        return np.array([params.negative_outcome if x[race_indc] > mean_race else params.positive_outcome for x in X])

    def predict_proba(self, X): 
        return one_hot_encode(self.predict(X))

    def score(self, X,y):
        return np.sum(self.predict(X)==y) / len(X)
    
# the display model with one unrelated feature
class innocuous_model_psi:
    # Decision rule: classify according to randomly drawn column 'unrelated column'
    def predict_proba(self, X): 
        return one_hot_encode(np.array([params.negative_outcome if x[unrelated_indcs] > 0 else params.positive_outcome for x in X]))

# the display model with two unrelated features
class innocuous_model_psi_two:
	def predict_proba(self, X):
		# Using 0.5 to make it easier to detect decision boundary on perturbation
		A = np.where(X[:,unrelated_indcs] > 0.5, params.positive_outcome, params.negative_outcome)
		B = np.where(X[:,unrelated_indcs1] < 0.5, params.positive_outcome, params.negative_outcome)
		preds = np.logical_xor(A, B).astype(int)
		return one_hot_encode(preds)
#
##
###

def experiment_main():
	"""
	Run through experiments for SHAP on CC using both one and two unrelated features.
	* This may take some time given that we iterate through every point in the test set
	* We print out the rate at which features occur in the top three features
	"""

	# Setup SHAP

	# Choose the optimal number of clusters
	candidates = [2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 20, 32, 64, 100]
	s_score(xtrain, candidates)

	n_clusters = int(input("Please enter the optimal number of clusters: "))

	##############################################
	# One unrelated (innocuous_model_psi is used)
	##############################################
	background_distribution = shap.kmeans(xtrain, n_clusters)
	generator_specs = {"original_dim": original_dim, "intermediate_dim": 8, "latent_dim": latent_dim, "epochs": 100, "dropout": 0.2,\
						"experiment": "CC", "feature_names": features}

	# Adversarial models
	adv_models = dict()
	adv_models["Perturbation"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi()).train(xtrain, ytrain, feature_names=features)
	adv_models["DropoutVAE"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi(), generator = "DropoutVAE", generator_specs = generator_specs).\
            				train(xtrain, ytrain, feature_names=features, dummy_idcs=dummy_idcs, integer_idcs=integer_attributes, n_samples=10*xtrain.shape[0])
	adv_models["RBF"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi(), generator = "RBF", generator_specs = generator_specs).\
            				train(xtrain, ytrain, feature_names=features, dummy_idcs=dummy_idcs, integer_idcs=integer_attributes)
	adv_models["Forest"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi(), generator = "Forest", generator_specs = generator_specs).\
            				train(xtrain, ytrain, feature_names=features, dummy_idcs=dummy_idcs, integer_idcs=integer_attributes)

	for adversarial in ["Perturbation", "DropoutVAE", "RBF", "Forest"]:
		adv_shap = adv_models[adversarial]

		# Explainers
		adv_kernel_explainers = dict()
		adv_kernel_explainers["Perturbation"] = shap.KernelExplainer(adv_shap.predict, background_distribution)
		adv_kernel_explainers["DropoutVAE"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="DropoutVAE", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs, integer_idcs=integer_attributes, instance_multiplier=100)
		adv_kernel_explainers["RBF"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="RBF", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs)
		adv_kernel_explainers["Forest"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="Forest", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs)
		adv_kernel_explainers["ForestFill"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="Forest", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs)

		for explainer in ["Perturbation", "DropoutVAE", "RBF", "Forest", "ForestFill"]:
			adv_kernel_explainer = adv_kernel_explainers[explainer]

			if explainer == "ForestFill":
				explanations = adv_kernel_explainer.shap_values(xtest, fill_data=True, data_location="..\Data/cc_forest_shap.csv")
			
			else:
				explanations = adv_kernel_explainer.shap_values(xtest)

			# format for display
			formatted_explanations = []
			for exp in explanations:
				if explainer == "Perturbation":
					formatted_explanations.append([(features[i], exp[i]) for i in range(len(exp))])
				else:
					formatted_explanations.append([(original_names[i], exp[i]) for i in range(len(exp))])

			print (f"SHAP Ranks and Pct Occurances one unrelated feature, adversarial: {adversarial}, explainer: {explainer}:")
			if explainer == "Perturbation":
				summary = experiment_summary(formatted_explanations, features)
			else:
				summary = experiment_summary(formatted_explanations, original_names)
			print (summary)
			print ("Fidelity:",round(adv_shap.fidelity(xtest),2))

			file_name = f"../Results/CCShap/ccShapSummary_adversarial_{adversarial}_explainer_{explainer}.csv"
			with open(file_name, "w") as output:
				w = csv.writer(output)
				for key, val in summary.items():
					w.writerow([key] + [pair for pair in val])

	##################################################
	# Two unrelated (innocuous_model_psi_two is used)
	##################################################
	background_distribution = shap.kmeans(xtrain, n_clusters)
	generator_specs = {"original_dim": original_dim, "intermediate_dim": 8, "latent_dim": latent_dim, "epochs": 100, "dropout": 0.2,\
						"experiment": "CC", "feature_names": features}

	# Adversarial models
	adv_models = dict()
	adv_models["Perturbation"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi_two()).train(xtrain, ytrain, feature_names=features)
	adv_models["DropoutVAE"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi_two(), generator = "DropoutVAE", generator_specs = generator_specs).\
            				train(xtrain, ytrain, feature_names=features, dummy_idcs=dummy_idcs, integer_idcs=integer_attributes, n_samples=10*xtrain.shape[0])
	adv_models["RBF"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi_two(), generator = "RBF", generator_specs = generator_specs).\
            				train(xtrain, ytrain, feature_names=features, dummy_idcs=dummy_idcs, integer_idcs=integer_attributes)
	adv_models["Forest"] = Adversarial_Kernel_SHAP_Model(racist_model_f(), innocuous_model_psi_two(), generator = "Forest", generator_specs = generator_specs).\
            				train(xtrain, ytrain, feature_names=features, dummy_idcs=dummy_idcs, integer_idcs=integer_attributes)

	for adversarial in ["Perturbation", "DropoutVAE", "RBF", "Forest"]:
		adv_shap = adv_models[adversarial]

		# Explainers
		adv_kernel_explainers = dict()
		adv_kernel_explainers["Perturbation"] = shap.KernelExplainer(adv_shap.predict, background_distribution)
		adv_kernel_explainers["DropoutVAE"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="DropoutVAE", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs, integer_idcs=integer_attributes, instance_multiplier=100)
		adv_kernel_explainers["RBF"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="RBF", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs)
		adv_kernel_explainers["Forest"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="Forest", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs)
		adv_kernel_explainers["ForestFill"] = shap.KernelExplainer(adv_shap.predict, xtrain, generator="Forest", generator_specs=generator_specs,\
								dummy_idcs=dummy_idcs)
		for explainer in ["Perturbation", "DropoutVAE", "RBF", "Forest", "ForestFill"]:
			adv_kernel_explainer = adv_kernel_explainers[explainer]

			if explainer == "ForestFill":
				explanations = adv_kernel_explainer.shap_values(xtest, fill_data=True, data_location="..\Data/cc_forest_shap.csv")
			
			else:
				explanations = adv_kernel_explainer.shap_values(xtest)

			# format for display
			formatted_explanations = []
			for exp in explanations:
				if explainer == "Perturbation":
					formatted_explanations.append([(features[i], exp[i]) for i in range(len(exp))])
				else:
					formatted_explanations.append([(original_names[i], exp[i]) for i in range(len(exp))])

			print (f"SHAP Ranks and Pct Occurances two unrelated features, adversarial: {adversarial}, explainer: {explainer}:")
			if explainer == "Perturbation":
				summary = experiment_summary(formatted_explanations, features)
			else:
				summary = experiment_summary(formatted_explanations, original_names)
			print (summary)
			print ("Fidelity:",round(adv_shap.fidelity(xtest),2))

			file_name = f"../Results/CCShap/ccShapSummary2_adversarial_{adversarial}_explainer_{explainer}.csv"
			with open(file_name, "w") as output:
				w = csv.writer(output)
				for key, val in summary.items():
					w.writerow([key] + [pair for pair in val])
	print ('---------------------')

if __name__ == "__main__":
	experiment_main()
