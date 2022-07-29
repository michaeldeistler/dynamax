from functools import partial

import chex
import jax.numpy as jnp
import jax.random as jr
import tensorflow_probability.substrates.jax.bijectors as tfb
import tensorflow_probability.substrates.jax.distributions as tfd
from jax import tree_map
from jax import vmap
from jax.nn import one_hot
from jax.tree_util import register_pytree_node_class
from ssm_jax.abstractions import Parameter
from ssm_jax.hmm.inference import compute_transition_probs
from ssm_jax.hmm.inference import hmm_smoother
from ssm_jax.hmm.models.base import BaseHMM

@chex.dataclass
class CategoricalHMMSuffStats:
    # Wrapper for sufficient statistics of a BernoulliHMM
    marginal_loglik: chex.Scalar
    initial_probs: chex.Array
    trans_probs: chex.Array
    sum_x: chex.Array
    
@register_pytree_node_class
class CategoricalHMM(BaseHMM):

    def __init__(self, initial_probabilities, transition_matrix, emission_probs):
        """_summary_

        Args:
            initial_probabilities (_type_): _description_
            transition_matrix (_type_): _description_
            emission_probs (_type_): _description_
        """
        super().__init__(initial_probabilities, transition_matrix)

        # Check shapes
        assert emission_probs.ndim == 3, \
            "emission_probs must be (num_states x num_emissions x num_classes)"
        self._emission_probs = Parameter(emission_probs, bijector=tfb.Invert(tfb.SoftmaxCentered()))

    @classmethod
    def random_initialization(cls, key, num_states, num_emissions, num_classes):
        key1, key2, key3 = jr.split(key, 3)
        initial_probs = jr.dirichlet(key1, jnp.ones(num_states))
        transition_matrix = jr.dirichlet(key2, jnp.ones(num_states), (num_states,))
        emission_probs = jr.dirichlet(key3, jnp.ones(num_classes), (num_states, num_emissions))
        return cls(initial_probs, transition_matrix, emission_probs)

    @property
    def emission_probs(self):
        return self._emission_probs

    @property
    def num_emissions(self):
        return self.emission_probs.value.shape[1]

    @property
    def num_classes(self):
        return self.emission_probs.value.shape[2]

    def emission_distribution(self, state):
        return tfd.Independent(
            tfd.Categorical(probs=self.emission_probs.value[state]),
            reinterpreted_batch_ndims=1)

    @property
    def suff_stats_event_shape(self):
        """Return dataclass containing 'event_shape' of each sufficient statistic."""
        return CategoricalHMMSuffStats(
            marginal_loglik = (),
            initial_probs   = (self.num_states,),
            trans_probs     = (self.num_states, self.num_states),
            sum_x           = (self.num_states, self.num_obs, self.num_classes),
        )

    def e_step(self, batch_emissions):
        """The E-step computes expected sufficient statistics under the
        posterior. In the Gaussian case, this these are the first two
        moments of the data
        """

        def _single_e_step(emissions):
            # Run the smoother
            posterior = hmm_smoother(self.initial_probs.value,
                                     self.transition_matrix.value,
                                     self._conditional_logliks(emissions))

            # Compute the initial state and transition probabilities
            initial_probs = posterior.smoothed_probs[0]
            trans_probs = compute_transition_probs(self.transition_matrix.value, posterior)

            # Compute the expected sufficient statistics
            sum_x = jnp.einsum("tk, tdi->kdi", posterior.smoothed_probs, one_hot(emissions, self.num_classes))

            # Pack into a dataclass
            stats = CategoricalHMMSuffStats(
                marginal_loglik=posterior.marginal_loglik,
                initial_probs=initial_probs,
                trans_probs=trans_probs,
                sum_x=sum_x,
            )
            return stats

        # Map the E step calculations over batches
        return vmap(_single_e_step)(batch_emissions)

    def m_step(self, batch_emissions, batch_posteriors, **kwargs):
        # Sum the statistics across all batches
        stats = tree_map(partial(jnp.sum, axis=0), batch_posteriors)
        # Then maximize the expected log probability as a fn of model parameters
        self._initial_probs.value = tfd.Dirichlet(1.0001 + stats.initial_probs).mode()
        self._transition_matrix.value = tfd.Dirichlet(1.0001 + stats.trans_probs).mode()
        self._emission_probs.value = tfd.Dirichlet(1.1 + stats.sum_x).mode()
