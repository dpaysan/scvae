import tensorflow as tf

from models.auxiliary import dense_layer, dense_layers, log_reduce_exp, reduce_logmeanexp

from tensorflow.python.ops.nn import relu, softmax, softplus
from tensorflow import sigmoid, identity

from tensorflow.contrib.distributions import Normal, Bernoulli, kl, Categorical
from distributions import distributions, latent_distributions, Categorized

import numpy
from numpy import inf

import copy
import os, shutil
from time import time
from auxiliary import formatDuration, normaliseString

from data import DataSet

class GaussianMixtureVariationalAutoEncoder_M2(object):
    def __init__(self, feature_size, latent_size, hidden_sizes,
        number_of_monte_carlo_samples, number_of_importance_samples,
        analytical_kl_term = False,
        latent_distribution = "gaussian", number_of_latent_clusters = 10,
        reconstruction_distribution = None,
        number_of_reconstruction_classes = None,
        batch_normalisation = True, count_sum = True,
        number_of_warm_up_epochs = 0, epsilon = 1e-6,
        log_directory = "log"):
        
        # Class setup
        super(GaussianMixtureVariationalAutoEncoder_M2, self).__init__()
        
        self.type = "GMVAE_M2"
        
        self.Dim_x = feature_size
        self.Dim_z = latent_size
        self.latent_size = latent_size
        self.hidden_sizes = hidden_sizes
        
        self.latent_distribution_name = latent_distribution
        self.latent_distribution = copy.deepcopy(
            latent_distributions[latent_distribution]
        )
        self.Dim_y = number_of_latent_clusters
        self.number_of_latent_clusters = number_of_latent_clusters

        self.analytical_kl_term = analytical_kl_term
        
        # Dictionary holding number of samples needed for the "monte carlo" 
        # estimator and "importance weighting" during both "train" and "test" time.  
        self.number_of_importance_samples = number_of_importance_samples
        self.number_of_monte_carlo_samples = number_of_monte_carlo_samples

        self.reconstruction_distribution_name = reconstruction_distribution
        self.reconstruction_distribution = distributions\
            [reconstruction_distribution]
        
        self.k_max = number_of_reconstruction_classes
        
        self.batch_normalisation = batch_normalisation

        self.count_sum_feature = count_sum
        self.count_sum = self.count_sum_feature or "constrained" in \
            self.reconstruction_distribution_name or "multinomial" in \
            self.reconstruction_distribution_name

        self.number_of_warm_up_epochs = number_of_warm_up_epochs

        self.epsilon = epsilon
        self.log_sigma_support = lambda x: tf.clip_by_value(x, -3 + self.epsilon, 3 - self.epsilon)
        
        self.main_log_directory = log_directory
        
        # Graph setup
        
        self.graph = tf.Graph()
        
        self.parameter_summary_list = []
        
        with self.graph.as_default():
            
            self.x = tf.placeholder(tf.float32, [None, self.Dim_x], 'X')
            self.t = tf.placeholder(tf.float32, [None, self.Dim_x], 'T')
            
            if self.count_sum:
                self.n = tf.placeholder(tf.float32, [None, 1], 'N')
            
            self.learning_rate = tf.placeholder(tf.float32, [], 'learning_rate')
            
            self.warm_up_weight = tf.placeholder(tf.float32, [], 'warm_up_weight')
            parameter_summary = tf.summary.scalar('warm_up_weight',
                self.warm_up_weight)
            self.parameter_summary_list.append(parameter_summary)
            
            self.is_training = tf.placeholder(tf.bool, [], 'is_training')
            
            self.S_iw = tf.placeholder(
                tf.int32,
                [],
                'number_of_iw_samples'
            )
            self.S_mc = tf.placeholder(
                tf.int32,
                [],
                'number_of_mc_samples'
            )

            self.inference()
            self.loss()
            self.training()
            
            self.saver = tf.train.Saver(max_to_keep = 1)
    
    @property
    def name(self):
        
        latent_part = normaliseString(self.latent_distribution_name)
        
        if "mixture" in self.latent_distribution_name:
            latent_part += "_c_" + str(self.Dim_y)
        
        reconstruction_part = normaliseString(
            self.reconstruction_distribution_name)
        
        if self.k_max:
            reconstruction_part += "_c_" + str(self.k_max)
        
        if self.count_sum_feature:
            reconstruction_part += "_sum"
        
        reconstruction_part += "_l_" + str(self.Dim_z) \
            + "_h_" + "_".join(map(str, self.hidden_sizes))
        
        mc_train = self.number_of_monte_carlo_samples["training"]
        mc_eval = self.number_of_monte_carlo_samples["evaluation"]
        
        if mc_train > 1 or mc_eval > 1:
            reconstruction_part += "_mc_" + str(mc_train)
            if mc_eval != mc_train:
                reconstruction_part += "_" + str(mc_eval)
        
        iw_train = self.number_of_importance_samples["training"]
        iw_eval = self.number_of_importance_samples["evaluation"]
        
        if iw_train > 1 or iw_eval > 1:
            reconstruction_part += "_iw_" + str(iw_train)
            if iw_eval != iw_train:
                reconstruction_part += "_" + str(iw_eval)
        
        if self.analytical_kl_term:
            reconstruction_part += "_kl"
        
        if self.batch_normalisation:
            reconstruction_part += "_bn"
        
        if self.number_of_warm_up_epochs:
            reconstruction_part += "_wu_" + str(self.number_of_warm_up_epochs)
        
        model_name = os.path.join(self.type, latent_part, reconstruction_part)
        
        return model_name
    
    @property
    def log_directory(self):
        return os.path.join(self.main_log_directory, self.name)
    
    @property
    def title(self):
        
        title = model.type
        
        configuration = [
            self.reconstruction_distribution_name.capitalize(),
            "$l = {}$".format(self.Dim_z),
            "$h = \\{{{}\\}}$".format(", ".join(map(str, self.hidden_sizes)))
        ]
        
        if self.k_max:
            configuration.append("$k_{{\\mathrm{{max}}}} = {}$".format(self.k_max))
        
        if self.count_sum_feature:
            configuration.append("CS")
        
        if self.batch_normalisation:
            configuration.append("BN")
        
        if self.number_of_warm_up_epochs:
            configuration.append("$W = {}$".format(
                self.number_of_warm_up_epochs))
        
        title += " (" + ", ".join(configuration) + ")"
        
        return title
    
    @property
    def description(self):
        
        description_parts = ["Model setup:"]
        
        description_parts.append("type: {}".format(self.type))
        description_parts.append("feature size: {}".format(self.Dim_x))
        description_parts.append("latent size: {}".format(self.Dim_z))
        description_parts.append("hidden sizes: {}".format(", ".join(
            map(str, self.hidden_sizes))))
        
        description_parts.append("latent distribution: " +
            self.latent_distribution_name)
        if "mixture" in self.latent_distribution_name:
            description_parts.append("latent clusters: {}".format(
                self.Dim_y))
        
        description_parts.append("reconstruction distribution: " +
            self.reconstruction_distribution_name)
        if self.k_max > 0:
            description_parts.append(
                "reconstruction classes: {}".format(self.k_max) +
                " (including 0s)"
            )
        
        mc_train = self.number_of_monte_carlo_samples["training"]
        mc_eval = self.number_of_monte_carlo_samples["evaluation"]
        
        if mc_train > 1 or mc_eval > 1:
            mc = "Monte Carlo samples: {}".format(mc_train)
            if mc_eval != mc_train:
                mc += " (training), {} (evaluation)".format(mc_eval)
            description_parts.append(mc)
        
        iw_train = self.number_of_importance_samples["training"]
        iw_eval = self.number_of_importance_samples["evaluation"]
        
        if iw_train > 1 or iw_eval > 1:
            iw = "importance samples: {}".format(iw_train)
            if iw_eval != iw_train:
                iw += " (training), {} (evaluation)".format(iw_eval)
            description_parts.append(iw)
        
        if self.analytical_kl_term:
            description_parts.append("using analytical KL term")
        
        if self.batch_normalisation:
            description_parts.append("using batch normalisation")
        if self.count_sum_feature:
            description_parts.append("using count sums")
        
        description = "\n    ".join(description_parts)
        
        return description
    
    @property
    def parameters(self, trainable = True):
        
        with self.graph.as_default():
            all_parameters = tf.global_variables()
            trainable_parameters = tf.trainable_variables()
        
        if trainable:
            parameters_string_parts = ["Trainable parameters"]
            parameters = trainable_parameters
        elif not trainable:
            parameters_string_parts = ["Non-trainable parameters"]
            parameters = [p for p in all_parameters
                if p not in trainable_parameters]
        
        width = max(map(len, [p.name for p in parameters]))
        
        for parameter in parameters:
            parameters_string_parts.append("{:{}}  {}".format(
                parameter.name, width, parameter.get_shape()))
        
        parameters_string = "\n    ".join(parameters_string_parts)
        
        return parameters_string
    
    def inference(self):
        # Total number of samples drawn from latent distributions, z1 and z2.
        self.S_iw_mc = self.S_iw * self.S_mc
        '''
        ########## ENCODER ###########
        Inference model for:
            q(y,z|x)    = q(y|x) q(z|x, y)
            q(z|x, y)   = N(z; mu_{q(z)}(x, y), sigma^2_{q(z)}(x, y)I)
            q(y|x)      = Cat(y; pi(x))
        '''
        
        # q(y|x)
        with tf.variable_scope("q_y"):
            ## (B, H)
            q_y_NN = dense_layers(
                inputs = self.x,
                num_outputs = self.hidden_sizes,
                activation_fn = relu,
                batch_normalisation = self.batch_normalisation,
                is_training = self.is_training,
                scope="NN"
            )

            ## (B, K)
            q_y_logits = tf.reshape(dense_layer(q_y_NN, self.Dim_y, activation_fn=None, scope="logits"), [1, -1, self.Dim_y])

            ## (B, K)
            self.q_y_given_x = Categorical(
                logits = q_y_logits,
                validate_args=True
            )

            ## (B, K) <-> (K, B)
            self.q_y_given_x_probs = 
                tf.transpose(
                    self.q_y_given_x.probs, 
                    [1, 0]
                ),

            self.q_y_mean = self.q_y_given_x.probs
        
        # q(z| x, y) #
        with tf.variable_scope("q_z"):
            ## (K, K)
            y_onehot = tf.diag(tf.ones(self.Dim_y))

            ## (K, K) -->
            ## (K, B, K)
            self.y_tile = tf.tile(
                tf.reshape(
                    y_onehot, 
                    [self.Dim_y, 1, self.Dim_y]
                ), 
                [1, tf.shape(self.x)[0], 1]
            )

            ## (B, F) -->
            ## (K, B, F)
            self.x_tile_q_z = tf.tile(tf.expand_dims(self.x, 0), [self.Dim_y, 1, 1])

            ## (K, B, F + K)
            self.x_y = tf.concat((self.x_tile_q_z, self.y_tile), axis = -1)

            ## (K * B, H)
            q_z_NN = dense_layers(
                inputs = self.x_y,
                num_outputs = self.hidden_sizes,
                activation_fn = relu,
                batch_normalisation = self.batch_normalisation,
                is_training = self.is_training,
                scope="NN"
            )

            ## (K * B, H) ==> (1, K, B, L)
            q_z_mu = tf.reshape(
                dense_layer(
                    q_z_NN,
                    self.Dim_z,
                    activation_fn=None,
                    scope="mu"
                    ), 
                [1, self.Dim_y, -1, self.Dim_z]
            )
            q_z_log_sigma = tf.reshape(
                dense_layer(
                    q_z_NN, 
                    self.Dim_z, 
                    activation_fn=self.log_sigma_support,
                    scope="log_sigma"
                    ), 
                [1, self.Dim_y, -1, self.Dim_z]
            )

            ## (1, K, B, L)
            self.q_z_given_x_y = Normal(
                loc=q_z_mu,
                scale=tf.exp(q_z_log_sigma),
                validate_args=True
            )

            ## (1, K, B, L) -> sum((K, B, 1) * (K, B, L), 0) -->
            ## (B, L)
            self.z_mean = tf.expand_dims(self.q_y_given_x_probs, -1) *\
                tf.reshape(
                    self.q_z_given_x_y.mean(), [self.Dim_y, -1, self.Dim_z]
                )

            ## (S_iw * S_mc, K, B, L)
            self.z = tf.reshape(
                self.q_z_given_x_y.sample(self.S_iw_mc), 
                [self.S_iw_mc, self.Dim_y, -1, self.Dim_z]
            )


        '''
        ##########DECODER ###########
        Generative model for:
            p(x,y,z) = p(x|z) p(z|y) p(y) where:
                p(z|y)   = N(z; mu(y), sigma^2(y)I)
                p(y)        = Cat(y; pi=1/K)
                p(x|z)     = f(x; gamma(y, z_1)) (some count distribution) 
        '''
        
        # Add a feature which is the total number of counts in an example. 
        with tf.variable_scope("count_sum"):
            if self.count_sum or self.count_sum_feature:
                ## (B, 1) -->
                ## (S_iw * S_mc, K, B, 1) 
                n_tile = tf.tile(
                    tf.reshape(self.n, [1, 1, -1, 1]
                    ), 
                    [self.S_iw_mc self.Dim_y, 1, 1]
                )
        
        # p(z|y) = N(z; mu(y), sigma(y))
        with tf.variable_scope("p_z"):
            ## (K, K) <:> (K, L) --> (1, K, 1, L)
            p_z_mu = tf.reshape(
                dense_layer(
                    inputs = y_onehot,
                    num_outputs = self.Dim_z,
                    activation_fn = None,
                    is_training = self.is_training,
                    scope = 'mu'
                ),
                [1, self.Dim_y, 1, self.Dim_z]
            )
            p_z_log_sigma = tf.reshape(
                dense_layer(
                    inputs = y_onehot,
                    num_outputs = self.Dim_z,
                    activation_fn = self.log_sigma_support,
                    is_training = self.is_training,
                    scope = 'log_sigma'
                ), 
                [1, self.Dim_y, 1, self.Dim_z]
            )

            ## (1, K, 1, L)
            self.p_z_given_y = Normal(
                loc=p_z_mu,
                scale=tf.exp(p_z_log_sigma),
                validate_args=True
            )

        # p(y)
        with tf.variable_scope("p_y"):
            p_y_logits = tf.ones((1, self.Dim_y))
            ## (1, K)
            self.p_y = Categorical(
                logits = p_y_logits,
                validate_args=True
            )



# Reconstruction distribution parameterisation
        
        with tf.variable_scope("p_x_given_z"):
            
            if self.count_sum_feature:
                ## (S_iw * S_mc, K, B, L+1) <:> (S_iw * S_mc * K * B, H)
                p_x_NN = dense_layers(
                    inputs = tf.concat((self.z, n_tile), -1),
                    num_outputs = self.hidden_sizes,
                    reverse_order = True,
                    activation_fn = relu,
                    batch_normalisation = self.batch_normalisation,
                    is_training = self.is_training,
                    scope="NN"
                )
            else:
                ## (S_iw * S_mc, K, B, L) <:> (S_iw * S_mc * K * B, H)
                p_x_NN = dense_layers(
                    inputs = self.z,
                    num_outputs = self.hidden_sizes,
                    reverse_order = True,
                    activation_fn = relu,
                    batch_normalisation = self.batch_normalisation,
                    is_training = self.is_training,
                    scope="NN"
                )

            x_theta = {}
            ## (S_iw * S_mc, K, B, F)
            for parameter in self.reconstruction_distribution["parameters"]:
                
                parameter_activation_function = \
                    self.reconstruction_distribution["parameters"]\
                    [parameter]["activation function"]
                p_min, p_max = \
                    self.reconstruction_distribution["parameters"]\
                    [parameter]["support"]
                
                ## (S_iw * S_mc * K * B, H) <:>
                ## (S_iw * S_mc * K * B, F) -->
                ## (S_iw * S_mc, K, B, F)
                x_theta[parameter] = tf.reshape(
                    dense_layer(
                        inputs = p_x_NN,
                        num_outputs = self.Dim_x,
                        activation_fn = lambda x: tf.clip_by_value(
                            parameter_activation_function(x),
                            p_min + self.epsilon,
                            p_max - self.epsilon
                        ),
                        is_training = self.is_training,
                        scope = parameter.upper()
                    ),
                    [self.S_iw_mc, self.Dim_y, -1, self.Dim_x]
                )
            
            if "constrained" in self.reconstruction_distribution_name or \
                "multinomial" in self.reconstruction_distribution_name:
                self.p_x_given_z = self.reconstruction_distribution["class"](
                    x_theta,
                    n_tile
                )
            elif "multinomial" in self.reconstruction_distribution_name:
                self.p_x_given_z = self.reconstruction_distribution["class"](
                    x_theta,
                    n_tile
                )
            else:
                self.p_x_given_z = self.reconstruction_distribution["class"](
                    x_theta
                )
            
            if self.k_max:
                x_logits = dense_layer(
                    inputs = p_x_NN,
                    num_outputs = self.Dim_x * self.k_max,
                    activation_fn = None,
                    is_training = self.is_training,
                    scope = "P_K"
                )
                
                x_logits = tf.reshape(x_logits,
                    [self.S_iw_mc, self.Dim_y, -1, self.Dim_x, self.k_max])
                
                self.p_x_given_z = Categorized(
                    dist = self.p_x_given_z,
                    cat = Categorical(logits = x_logits, validate_args=True)
                )
            
            ## (S_iw * S_mc, K, B, F) -->
            ## (S_iw * S_mc, B, F) -->
            ## (B, F)
            self.x_mean = tf.reduce_mean(
                tf.reduce_sum(
                    self.p_x_given_z.mean() *\
                        tf.expand_dims(
                            self.q_y_given_x_probs,
                            -1
                        ),
                    axis = 1
                ),
                0
            )
        
        # Add histogram summaries for the trainable parameters
        for parameter in tf.trainable_variables():
            parameter_summary = tf.summary.histogram(parameter.name, parameter)
            self.parameter_summary_list.append(parameter_summary)
        self.parameter_summary = tf.summary.merge(self.parameter_summary_list)
    
    def loss(self):
        # Loss
        # Initialise reshaped data
        ## (S_iw * S_mc, K, B, F)
        t_tiled = tf.tile(
            tf.reshape(
                self.t, 
                [1, 1, -1, self.Dim_x]
            ),
            [self.S_iw_mc, self.Dim_y, 1, 1]
        )

        # log(p(x|z))
        ## (S_iw * S_mc, K, B, F) -->
        ## (S_iw * S_mc, K, B) -->
        p_x_given_z_log_prob = tf.reduce_sum(
            self.p_x_given_z_y.log_prob(t_tiled), -1
        )
        ## (S_iw * S_mc, K, B) -->
        ## (K, B) -->
        ## (B)
        log_p_x_given_z = tf.reduce_sum(
            self.q_y_given_x_probs * tf.reduce_mean(
                p_x_given_z_log_prob, 
                0
            ),
            0
        )

        # log(p(z|y))
        ## (1, K, 1, L) --> 
        ## (S_iw * S_mc, K, B)
        p_z_given_y_log_prob = tf.reduce_sum(
            self.p_z_given_y.log_prob(self.z),
            -1
        )
        ## (S_iw * S_mc, K, B) -->
        ## (K, B) -->
        ## (B)
        log_p_z_given_y = tf.reduce_sum(
            self.q_y_given_x_probs * tf.reduce_mean(
                p_z_given_y_log_prob, 
                0
            ), 
            0
        )

        # log(q(z|x))
        ## (S_iw * S_mc, K, B, L) -->
        ## (S_iw * S_mc, K, B)
        q_z_given_x_y_log_prob = tf.reduce_sum(
           self.q_z_given_x_y.log_prob(self.z),
            -1
        )

        ## (S_iw * S_mc, K, B) -->
        ## (K, B) -->
        ## (B)
        log_q_z_given_x_y = tf.reduce_sum(
            self.q_y_given_x_probs * tf.reduce_mean(
                q_z_given_x_y_log_prob, 
                0
            ),
            0
        )

        # Importance weighted log likelihood
        # Put all log_prob tensors together in one.
        ## (S_iw * S_mc, S_iw * S_mc, K, B) -->
        ## (S_iw, S_mc, S_iw, S_mc, K, B)
        # all_log_prob_iw = tf.reshape(
        #     tf.expand_dims(p_x_given_z_y_log_prob, 0)\
        #     - self.warm_up_weight * (q_z2_given_y_z_log_prob \
        #         + tf.expand_dims(q_y_given_x_z_log_prob, 0) \
        #         + tf.expand_dims(tf.expand_dims(q_z_given_x_log_prob, 1), 0) \
        #         - p_z2_log_prob \
        #         - p_z_given_y_z2_log_prob \
        #         - p_y_given_z2_log_prob
        #         ), 
        #     [self.S_iw, self.S_mc, self.S_iw, self.S_mc, self.Dim_y, -1]
        # )

        # # log-mean-exp trick for stable marginalisation of importance weights.
        # ## (S_iw, S_mc, S_iw, S_mc, K, B) -->
        # ## (S_mc, S_mc, K, B)
        # log_mean_exp_iw = log_reduce_exp(
        #     all_log_prob_iw, 
        #     reduction_function=tf.reduce_mean, axis = (0, 2)
        # )

        # # Marginalise all Monte Carlo samples, classes and examples into total
        # # importance weighted loss
        # # (S_iw * S_mc, K, B) --> (S_iw, S_mc, K, B) --> (S_mc, K, B)
        # q_y_given_x_z_probs_mc = tf.reshape(
        #     self.q_y_given_x_probs, 
        #     [self.S_iw, self.S_mc, self.Dim_y, -1]
        # )[0]

        # ## (S_mc, S_mc, K, B) -->
        # ## (S_mc, K, B) -->
        # ## (S_mc, B) -->
        # ## ()
        # self.ELBO = tf.reduce_mean(
        #     tf.reduce_sum(
        #         q_y_given_x_z_probs_mc * tf.reduce_mean(
        #             log_mean_exp_iw, 
        #             0
        #         ), 
        #         1
        #     ),
        # )
        # self.loss = self.ELBO
        # tf.add_to_collection('losses', self.ELBO)

        # KL_z
        self.KL_z = tf.reduce_mean(log_q_z_given_x_y - log_p_z_given_y)


        # KL_y (B)
        self.KL_y = tf.reduce_mean(kl(self.q_y_given_x, self.p_y))

        self.KL = tf.add_n([self.KL_z, self.KL_y], name = 'KL')
        tf.add_to_collection('losses', self.KL)

        self.KL_all = tf.expand_dims(self.KL, -1, name = "KL_all")

        # ENRE
        self.ENRE = tf.reduce_mean(log_p_x_given_z, name = "ENRE")
        tf.add_to_collection('losses', self.ENRE)

        # ELBO
        self.ELBO = tf.subtract(self.ENRE, self.KL, name = "ELBO")
        tf.add_to_collection('losses', self.ELBO)

        # loss objective with Warm-up and term-specific KL weighting 
        self.loss = self.ENRE - self.warm_up_weight * self.KL
    
    def training(self):
        
        # Create the gradient descent optimiser with the given learning rate.
        def setupTraining():
            
            # Optimizer and training objective of negative loss
            optimiser = tf.train.AdamOptimizer(self.learning_rate)
            # clipped_optimiser = tf.contrib.opt.VariableClippingOptimizer(optimiser, ) 
            # Create a variable to track the global step.
            self.global_step = tf.Variable(0, name = 'global_step',
                trainable = False)
            
            # Use the optimiser to apply the gradients that minimize the loss
            # (and also increment the global step counter) as a single training
            # step.
            # self.train_op = optimiser.minimize(
            #     -self.loss,
            #     global_step = self.global_step
            # )
        
            gradients = optimiser.compute_gradients(-self.loss)
            # for gradient, variable in gradients:
            #     if not gradient.:
            #         print(variable)
            clipped_gradients = []
            for gradient, variable in gradients:
                if gradient is not None:
                    clipped_gradients.append((tf.clip_by_value(gradient, -1., 1.), variable))
                else:
                    clipped_gradients.append((gradient, variable))
            # clipped_gradients = [(tf.clip_by_value(gradient, -1., 1.), variable) for gradient, variable in gradients if gradient is not None else (gradient, variable)]
            self.train_op = optimiser.apply_gradients(clipped_gradients, global_step = self.global_step)
        # Make sure that the updates of the moving_averages in batch_norm
        # layers are performed before the train_step.
        
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        
        if update_ops:
            updates = tf.group(*update_ops)
            with tf.control_dependencies([updates]):
                setupTraining()
        else:
            setupTraining()

    def train(self, training_set, validation_set,
        number_of_epochs = 100, batch_size = 100, learning_rate = 1e-3,
        reset_training = False):
        
        # Logging
        
        status = {
            "completed": False,
            "message": None
        }
        
        # parameter_values = "lr_{:.1g}".format(learning_rate)
        # parameter_values += "_b_" + str(batch_size)
        
        # self.log_directory = os.path.join(self.log_directory, parameter_values)
        
        if reset_training and os.path.exists(self.log_directory):
            shutil.rmtree(self.log_directory)
        
        checkpoint_file = os.path.join(self.log_directory, 'model.ckpt')
        
        # Setup
        
        if self.count_sum:
            n_train = training_set.count_sum
            n_valid = validation_set.count_sum
        
        M_train = training_set.number_of_examples
        M_valid = validation_set.number_of_examples
        
        noisy_preprocess = training_set.noisy_preprocess
        
        if not noisy_preprocess:
            
            x_train = training_set.preprocessed_values
            x_valid = validation_set.preprocessed_values
        
            if self.reconstruction_distribution_name == "bernoulli":
                t_train = training_set.binarised_values
                t_valid = validation_set.binarised_values
            else:
                t_train = training_set.values
                t_valid = validation_set.values
        
        steps_per_epoch = numpy.ceil(M_train / batch_size)
        output_at_step = numpy.round(numpy.linspace(0, steps_per_epoch, 11))
        
        with tf.Session(graph = self.graph) as session:
            
            parameter_summary_writer = tf.summary.FileWriter(
                self.log_directory)
            training_summary_writer = tf.summary.FileWriter(
                os.path.join(self.log_directory, "training"))
            validation_summary_writer = tf.summary.FileWriter(
                os.path.join(self.log_directory, "validation"))
            
            # Initialisation
            
            checkpoint = tf.train.get_checkpoint_state(self.log_directory)
            
            if checkpoint:
                self.saver.restore(session, checkpoint.model_checkpoint_path)
                epoch_start = int(os.path.split(
                    checkpoint.model_checkpoint_path)[-1].split('-')[-1])
            else:
                session.run(tf.global_variables_initializer())
                epoch_start = 0
                parameter_summary_writer.add_graph(session.graph)
            
            # Training loop
            
            if epoch_start == number_of_epochs:
                print("Model has already been trained for {} epochs.".format(
                    number_of_epochs))
            
            for epoch in range(epoch_start, number_of_epochs):
                
                epoch_time_start = time()
                
                if noisy_preprocess:
                    x_train = noisy_preprocess(training_set.values)
                    t_train = x_train
                    x_valid = noisy_preprocess(validation_set.values)
                    t_valid = x_valid

                if self.number_of_warm_up_epochs:
                    warm_up_weight = float(min(
                        epoch / (self.number_of_warm_up_epochs), 1.0))
                else:
                    warm_up_weight = 1.0
                
                shuffled_indices = numpy.random.permutation(M_train)
                
                for i in range(0, M_train, batch_size):
                    
                    # Internal setup
                    
                    step_time_start = time()
                    
                    step = session.run(self.global_step)
                    
                    # Prepare batch
                    
                    batch_indices = shuffled_indices[i:(i + batch_size)]
                    
                    feed_dict_batch = {
                        self.x: x_train[batch_indices],
                        self.t: t_train[batch_indices],
                        self.is_training: True,
                        self.learning_rate: learning_rate, 
                        self.warm_up_weight: warm_up_weight,
                        self.S_iw: self.number_of_importance_samples["training"],
                        self.S_mc: self.number_of_monte_carlo_samples["training"]
                    }
                    
                    if self.count_sum:
                        feed_dict_batch[self.n] = n_train[batch_indices]
                    
                    # Run the stochastic batch training operation
                    _, batch_loss = session.run(
                        [self.train_op, self.ELBO],
                        feed_dict = feed_dict_batch
                    )
                    
                    # Compute step duration
                    step_duration = time() - step_time_start
                    
                    # Print evaluation and output summaries
                    if (step + 1 - steps_per_epoch * epoch) in output_at_step:
                        
                        print('Step {:d} ({}): {:.5g}.'.format(
                            int(step + 1), formatDuration(step_duration),
                            batch_loss))
                        
                        if numpy.isnan(batch_loss):
                            status["completed"] = False
                            status["message"] = "loss became nan"
                            return status
                
                print()
                
                epoch_duration = time() - epoch_time_start
                
                print("Epoch {} ({}):".format(epoch + 1,
                    formatDuration(epoch_duration)))

                # With warmup or not
                if warm_up_weight < 1:
                    print('    Warm-up weight: {:.2g}'.format(warm_up_weight))

                # Saving model parameters
                print('    Saving model.')
                saving_time_start = time()
                self.saver.save(session, checkpoint_file,
                    global_step = epoch + 1)
                saving_duration = time() - saving_time_start
                print('    Model saved ({}).'.format(
                    formatDuration(saving_duration)))
                
                # Export parameter summaries
                parameter_summary_string = session.run(
                    self.parameter_summary,
                    feed_dict = {self.warm_up_weight: warm_up_weight}
                )
                parameter_summary_writer.add_summary(
                    parameter_summary_string, global_step = epoch + 1)
                parameter_summary_writer.flush()
                
                # Evaluation
                print('    Evaluating model.')
                
                ## Training
                
                evaluating_time_start = time()
                
                ELBO_train = 0
                ENRE_train = 0
                KL_z_train = 0
                KL_y_train = 0
                
                
                for i in range(0, M_train, batch_size):
                    subset = slice(i, (i + batch_size))
                    x_batch = x_train[subset]
                    t_batch = t_train[subset]
                    feed_dict_batch = {
                        self.x: x_batch,
                        self.t: t_batch,
                        self.is_training: False,
                        self.warm_up_weight: 1.0,
                        self.S_iw: self.number_of_importance_samples["training"],
                        self.S_mc: self.number_of_monte_carlo_samples["training"]
                    }
                    if self.count_sum:
                        feed_dict_batch[self.n] = n_train[subset]
                    
                    ELBO_i, ENRE_i, KL_z_i, KL_y_i = session.run(
                        [self.ELBO, self.ENRE, self.KL_z, self.KL_y],
                        feed_dict = feed_dict_batch
                    )
                    
                    ELBO_train += ELBO_i
                    ENRE_train += ENRE_i
                    KL_z_train += KL_z_i
                    KL_y_train += KL_y_i
                                    
                ELBO_train /= M_train / batch_size
                ENRE_train /= M_train / batch_size
                KL_z_train /= M_train / batch_size
                KL_y_train /= M_train / batch_size
                                
                evaluating_duration = time() - evaluating_time_start
                
                summary = tf.Summary()
                summary.value.add(tag="losses/lower_bound",
                    simple_value = ELBO_train)
                summary.value.add(tag="losses/reconstruction_error",
                    simple_value = ENRE_train)
                summary.value.add(tag="losses/kl_divergence_z",
                    simple_value = KL_z_train)                
                summary.value.add(tag="losses/kl_divergence_z2",
                    simple_value = KL_z2_train)
                summary.value.add(tag="losses/kl_divergence_y",
                    simple_value = KL_y_train)
                
                training_summary_writer.add_summary(summary,
                    global_step = epoch + 1)
                training_summary_writer.flush()
                
                print("    Training set ({}): ".format(
                    formatDuration(evaluating_duration)) + \
                    "ELBO: {:.5g}, ENRE: {:.5g}, KL_z: {:.5g}, KL_y: {:.5g}.".format(
                    ELBO_train, ENRE_train, KL_z_train, KL_y_train))
                
                ## Validation
                
                evaluating_time_start = time()
                
                ELBO_valid = 0
                ENRE_valid = 0
                KL_z_valid = 0
                KL_y_valid = 0
                
                for i in range(0, M_valid, batch_size):
                    subset = slice(i, (i + batch_size))
                    x_batch = x_valid[subset]
                    t_batch = t_valid[subset]
                    feed_dict_batch = {
                        self.x: x_batch,
                        self.t: t_batch,
                        self.is_training: False,
                        self.warm_up_weight: 1.0,
                        self.S_iw:
                            self.number_of_importance_samples["training"],
                        self.S_mc:
                            self.number_of_monte_carlo_samples["training"]
                    }
                    if self.count_sum:
                        feed_dict_batch[self.n] = n_valid[subset]
                    
                    ELBO_i, ENRE_i, KL_z_i, KL_y_i = session.run(
                        [self.ELBO, self.ENRE, self.KL_z, self.KL_y],
                        feed_dict = feed_dict_batch
                    )
                    
                    ELBO_valid += ELBO_i
                    ENRE_valid += ENRE_i
                    KL_z_valid += KL_z_i
                    KL_y_valid += KL_y_i
                                    
                ELBO_valid /= M_valid / batch_size
                ENRE_valid /= M_valid / batch_size
                KL_z_valid /= M_valid / batch_size
                KL_y_valid /= M_valid / batch_size
                                
                evaluating_duration = time() - evaluating_time_start
                
                summary = tf.Summary()
                summary.value.add(tag="losses/lower_bound",
                    simple_value = ELBO_valid)
                summary.value.add(tag="losses/reconstruction_error",
                    simple_value = ENRE_valid)
                summary.value.add(tag="losses/kl_divergence_z",
                    simple_value = KL_z_valid)                
                summary.value.add(tag="losses/kl_divergence_z2",
                    simple_value = KL_z2_valid)
                summary.value.add(tag="losses/kl_divergence_y",
                    simple_value = KL_y_valid)
                
                validation_summary_writer.add_summary(summary,
                    global_step = epoch + 1)
                validation_summary_writer.flush()
                
                print("    Validation set ({}): ".format(
                    formatDuration(evaluating_duration)) + \
                    "ELBO: {:.5g}, ENRE: {:.5g}, KL_z: {:.5g}, KL_y: {:.5g}.".format(
                    ELBO_valid, ENRE_valid, KL_z_valid, KL_y_valid))
                
                print()
            
            # Clean up
            
            checkpoint = tf.train.get_checkpoint_state(self.log_directory)
            
            if checkpoint:
                for f in os.listdir(self.log_directory):
                    file_path = os.path.join(self.log_directory, f)
                    is_old_checkpoint_file = os.path.isfile(file_path) \
                        and "model" in f \
                        and not checkpoint.model_checkpoint_path in file_path
                    if is_old_checkpoint_file:
                        os.remove(file_path)
            
            status["completed"] = True
            
            return status
    
    def evaluate(self, test_set, batch_size = 100):
        
        if self.count_sum:
            n_test = test_set.count_sum
        
        M_test = test_set.number_of_examples
        F_test = test_set.number_of_features
        
        noisy_preprocess = test_set.noisy_preprocess
        
        if not noisy_preprocess:
            
            x_test = test_set.preprocessed_values
        
            if self.reconstruction_distribution_name == "bernoulli":
                t_test = test_set.binarised_values
            else:
                t_test = test_set.values
            
        else:
            x_test = noisy_preprocess(test_set.values)
            t_test = x_test
        
        checkpoint = tf.train.get_checkpoint_state(self.log_directory)
        
        test_summary_directory = os.path.join(self.log_directory, "test")
        if os.path.exists(test_summary_directory):
            shutil.rmtree(test_summary_directory)
        
        with tf.Session(graph = self.graph) as session:
            
            test_summary_writer = tf.summary.FileWriter(
                test_summary_directory)
            
            if checkpoint:
                self.saver.restore(session, checkpoint.model_checkpoint_path)
                epoch = int(os.path.split(
                    checkpoint.model_checkpoint_path)[-1].split('-')[-1])
            else:
                raise Exception(
                    "Cannot evaluate model when it has not been trained.")
            
            evaluating_time_start = time()
            
            ELBO_test = 0
            ENRE_test = 0
            KL_z_test = 0
            KL_y_test = 0
            
            x_mean_test = numpy.empty([M_test, F_test])
            z_mean_test = numpy.empty([M_test, self.Dim_z])
            y_mean_test = numpy.empty([M_test, self.Dim_y])

            for i in range(0, M_test, batch_size):
                subset = slice(i, (i + batch_size))
                x_batch = x_test[subset]
                t_batch = t_test[subset]
                feed_dict_batch = {
                    self.x: x_batch,
                    self.t: t_batch,
                    self.is_training: False,
                    self.warm_up_weight: 1.0,
                    self.S_iw: self.number_of_importance_samples["evaluation"],
                    self.S_mc: self.number_of_monte_carlo_samples["evaluation"]
                }
                if self.count_sum:
                    feed_dict_batch[self.n] = n_test[subset]
                
                ELBO_i, ENRE_i, KL_z_i, KL_y_i, \
                    x_mean_i, z_mean_i, y_mean_i = session.run(
                    [self.ELBO, self.ENRE, self.KL_z, self.KL_y,
                        self.x_mean, self.z_mean, self.q_y_mean],
                    feed_dict = feed_dict_batch
                )
                
                ELBO_test += ELBO_i
                ENRE_test += ENRE_i
                KL_z_test += KL_z_i
                KL_y_test += KL_y_i
                
                x_mean_test[subset] = x_mean_i
                y_mean_test[subset] = y_mean_i
                z_mean_test[subset] = z_mean_i
            
            ELBO_test /= M_test / batch_size
            ENRE_test /= M_test / batch_size
            KL_z_test /= M_test / batch_size
            KL_y_test /= M_test / batch_size
            
            summary = tf.Summary()
            summary.value.add(tag="losses/lower_bound",
                simple_value = ELBO_test)
            summary.value.add(tag="losses/reconstruction_error",
                simple_value = ENRE_test)
            summary.value.add(tag="losses/kl_divergence_z",
                simple_value = KL_z_test)                
            summary.value.add(tag="losses/kl_divergence_z2",
                simple_value = KL_z2_test)
            summary.value.add(tag="losses/kl_divergence_y",
                simple_value = KL_y_test)
            
            test_summary_writer.add_summary(summary,
                global_step = epoch + 1)
            test_summary_writer.flush()
            
            evaluating_duration = time() - evaluating_time_start
            print("Test set ({}): ".format(
                    formatDuration(evaluating_duration)) + \
                    "ELBO: {:.5g}, ENRE: {:.5g}, KL_z: {:.5g}, KL_y: {:.5g}.".format(
                    ELBO_test, ENRE_test, KL_z_test, KL_y_test))
            
            if self.reconstruction_distribution_name == "bernoulli":
                transformed_test_set = DataSet(
                    name = test_set.name,
                    values = t_test,
                    preprocessed_values = None,
                    labels = test_set.labels,
                    example_names = test_set.example_names,
                    feature_names = test_set.feature_names,
                    feature_selection = test_set.feature_selection,
                    preprocessing_methods = test_set.preprocessing_methods,
                    kind = "test",
                    version = "binarised"
                )
            else:
                transformed_test_set = test_set
            
            reconstructed_test_set = DataSet(
                name = test_set.name,
                values = x_mean_test,
                preprocessed_values = None,
                labels = test_set.labels,
                example_names = test_set.example_names,
                feature_names = test_set.feature_names,
                feature_selection = test_set.feature_selection,
                preprocessing_methods = test_set.preprocessing_methods,
                kind = "test",
                version = "reconstructed"
            )
            
            z_test_set = DataSet(
                name = test_set.name,
                values = z1_mean_test,
                preprocessed_values = None,
                labels = test_set.labels,
                example_names = test_set.example_names,
                feature_names = numpy.array(["z variable {}".format(
                    i + 1) for i in range(self.Dim_z)]),
                feature_selection = test_set.feature_selection,
                preprocessing_methods = test_set.preprocessing_methods,
                kind = "test",
                version = "z"
            )
            
            y_test_set = DataSet(
                name = test_set.name,
                values = y_mean_test,
                preprocessed_values = None,
                labels = test_set.labels,
                example_names = test_set.example_names,
                feature_names = numpy.array(["y variable {}".format(
                    i + 1) for i in range(self.Dim_y)]),
                feature_selection = test_set.feature_selection,
                preprocessing_methods = test_set.preprocessing_methods,
                kind = "test",
                version = "y"
            )
            latent_test_sets = (z_test_set, y_test_set)

            return transformed_test_set, reconstructed_test_set, latent_test_sets