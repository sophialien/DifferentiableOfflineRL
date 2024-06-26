import os, sys
sys.path.append(os.getcwd())
os.environ["TF_CUDNN_DETERMINISTIC"] = "1"
#os.environ["JAX_DEBUG_NANS"] = "True"

import wandb
import uuid
import pyrallis

import jax
import numpy as np
import optax
import flax
import random

import jax.numpy as jnp
from jax import grad, vmap
#from jax.scipy.spatial.distance import cosine
from jax import jacfwd

from functools import partial
from dataclasses import dataclass, asdict
from flax.core import FrozenDict
from typing import Dict, Tuple, Any, Callable
from tqdm.auto import trange

from flax.training.train_state import TrainState

from src.networks import EnsembleCritic, DetActor, EnsembleCritic_swish
from src.utils.buffer import ReplayBuffer
from src.utils.common import Metrics, make_env, evaluate, wrap_env


@dataclass
class Config:
    # wandb params
    project: str = "ReBRAC"
    group: str = "rebrac"
    name: str = "rebrac"
    # model params
    actor_learning_rate: float = 1e-3
    critic_learning_rate: float = 1e-3
    hidden_dim: int = 256
    actor_n_hiddens: int = 3
    critic_n_hiddens: int = 3
    gamma: float = 0.99
    tau: float = 5e-3
    actor_bc_coef: float = 1.0
    critic_bc_coef: float = 1.0
    actor_ln: bool = False
    critic_ln: bool = True
    policy_noise: float = 0.2
    noise_clip: float = 0.5
    policy_freq: int = 2
    normalize_q: bool = True
    # training params
    dataset_name: str = "halfcheetah-medium-v2"
    batch_size: int = 1024
    num_epochs: int = 1000
    num_updates_on_epoch: int = 1000
    normalize_reward: bool = False
    normalize_states: bool = False
    # evaluation params
    eval_episodes: int = 10
    eval_every: int = 5
    # general params
    train_seed: int = 0
    eval_seed: int = 42
    robust_eps: float = 0.01  # default value
    robust_alpha: float = 0.5  # default value
    hjb_discount: float = 0.01  # default value
    sim_w: float = 0.1  # default value
    strict_dim: Any = None
    strict_min: float = None
    strict_max: float = None
    strict_r: Any = None
    subsample: Any = None
    averageR: float = 0.01

    def __post_init__(self):
        self.name = f"first-order-{self.name}-{self.dataset_name}-seed_{self.train_seed}"


class CriticTrainState(TrainState):
    target_params: FrozenDict


class ActorTrainState(TrainState):
    target_params: FrozenDict
"""
def check_for_nans(tensor, name="Tensor"):
    if jnp.any(jnp.isnan(tensor)):
        raise ValueError(f"NaN detected in {name}")
"""
def update_actor(
        key: jax.random.PRNGKey,
        actor: TrainState,
        critic: TrainState,
        batch: Dict[str, jax.Array],
        beta: float,
        tau: float,
        normalize_q: bool,
        metrics: Metrics,
) -> Tuple[jax.random.PRNGKey, TrainState, jax.Array, Metrics]:
    key, random_action_key = jax.random.split(key, 2)
    #print("training set",  batch["states"], batch["actions"])
    def actor_loss_fn(params):
        actions = actor.apply_fn(params, batch["states"])
        #check_for_nans(actions, "actions")

        bc_penalty = ((actions - batch["actions"]) ** 2).sum(-1)
        #check_for_nans(bc_penalty, "bc_penalty")
        
        q_values = critic.apply_fn(critic.params, batch["states"], actions).min(0)
        #check_for_nans(q_values, "q_values")

        lmbda = 1
        if normalize_q:
            lmbda = jax.lax.stop_gradient(1 / jax.numpy.abs(q_values).mean())
            #check_for_nans(lmbda, "lmbda")
        loss = (beta * bc_penalty * (jax.lax.stop_gradient(jax.numpy.abs(q_values).mean())) - lmbda * q_values).mean()
        
        #loss = (- q_values).mean()
        
        # logging stuff
        random_actions = jax.random.uniform(random_action_key, shape=batch["actions"].shape, minval=-1.0, maxval=1.0)
        new_metrics = metrics.update({
            "actor_loss": loss,
            "bc_mse_policy": bc_penalty.mean(),
            "bc_mse_random": ((random_actions - batch["actions"]) ** 2).sum(-1).mean(),
            "action_mse": ((actions - batch["actions"]) ** 2).mean()
        })
        return loss, new_metrics

    grads, new_metrics = jax.grad(actor_loss_fn, has_aux=True)(actor.params)
    
    
    new_actor = actor.apply_gradients(grads=grads)

    new_actor = new_actor.replace(
        target_params=optax.incremental_update(actor.params, actor.target_params, tau)
    )
    new_critic = critic.replace(
        target_params=optax.incremental_update(critic.params, critic.target_params, tau)
    )

    return key, new_actor, new_critic, new_metrics


def update_critic(
        key: jax.random.PRNGKey,
        actor: TrainState,
        critic: CriticTrainState,
        batch: Dict[str, jax.Array],
        gamma: float,
        beta: float,
        tau: float,
        policy_noise: float,
        noise_clip: float,
        metrics: Metrics,
        hjb_discount: float,
        sim_w: float,
        strict_dim = None,
        strict_min: float = None,
        strict_max: float = None,
        averageR: float = 1.0,
) -> Tuple[jax.random.PRNGKey, TrainState, Metrics]:
    key, actions_key = jax.random.split(key)

    next_actions = actor.apply_fn(actor.target_params, batch["next_states"])
    noise = jax.numpy.clip(
        (jax.random.normal(actions_key, next_actions.shape) * policy_noise),
        -noise_clip,
        noise_clip,
    )
    next_actions = jax.numpy.clip(next_actions + noise, -1, 1)
    #print("training", next_actions.min(), next_actions.max())
    bc_penalty = ((next_actions - batch["next_actions"]) ** 2).sum(-1)
    next_q = critic.apply_fn(critic.target_params, batch["next_states"], next_actions).min(0)
    next_q = next_q - beta * bc_penalty

    target_q = batch["rewards"] + (1 - batch["dones"]) * gamma * next_q



    def critic_loss_fn(critic_params):
        # [N, batch_size] - [1, batch_size]
        q = critic.apply_fn(critic_params, batch["states"], batch["actions"])
        q_min = q.min(0).mean()



        def value_fn(states, actions):
            return jnp.mean(critic.apply_fn(critic_params, states, actions))

        value, state_grad = jax.value_and_grad(value_fn)(batch["states"], batch["actions"])
 
        # Compute gradient with respect to actions
        action_grad = jax.grad(value_fn, argnums=1)(batch["states"], batch["actions"])

        # Calculate the trust using the cosine similarity
        def cosine_similarity_jax(a, b):
            """Compute the cosine similarity between rows of matrices a and b using JAX."""
            dot_product = jnp.sum(a * b, axis=-1)  # Element-wise multiplication and sum over the last axis
            norm_a = jnp.linalg.norm(a, axis=-1)
            norm_b = jnp.linalg.norm(b, axis=-1)
            return dot_product / (norm_a * norm_b)
        trust = cosine_similarity_jax(state_grad, (batch["next_states"] - batch["states"]))
        
        e = random.uniform(0, 1)
        random_state = batch["states"] - (batch["next_states"] - batch["states"]) * e
        andom_value, random_state_grad = jax.value_and_grad(value_fn)(random_state, batch["actions"])
        random_trust = cosine_similarity_jax(random_state_grad, ((batch["next_states"] - batch["states"]) ))

        tolerance = 1  # set your tolerance threshold
        action_grad_squared = (action_grad**2).sum(-1)

        action_grad_squared_with_tolerance = jnp.where(action_grad_squared < tolerance, jnp.zeros_like(action_grad_squared), action_grad_squared)

        first_order_loss_1 = jnp.exp(batch["rewards"] - averageR) * (-trust - random_trust + action_grad_squared_with_tolerance)
       
        log_info = ((q - jax.lax.stop_gradient(hjb_discount*(batch["rewards"] + (1 - batch["dones"]) *jnp.sum(state_grad * (batch["next_states"] - batch["states"]), axis=-1))))**2)
        
        loss = (((q - target_q[None, ...]) ** 2).sum(0)).mean() + sim_w*addition_loss_1.mean(0)
        return loss, (q_min, (log_info).mean(1).sum(0), first_order_loss_1.mean(0), ((q - target_q[None, ...]) ** 2).sum(0).mean())

    (loss, (q_min, hjb_loss, sim_loss, td)), grads = jax.value_and_grad(critic_loss_fn, has_aux=True)(critic.params)
    new_critic = critic.apply_gradients(grads=grads)
    new_metrics = metrics.update({
        "critic_loss": loss,
        "q_min": q_min,
        "sim_loss": sim_loss, 
        "hjb_loss": hjb_loss,
        "td_loss": td,
    })
    return key, new_critic, new_metrics


def update_td3(
        key: jax.random.PRNGKey,
        actor: TrainState,
        critic: CriticTrainState,
        batch: Dict[str, Any],
        metrics: Metrics,
        gamma: float,
        actor_bc_coef: float,
        critic_bc_coef: float,
        tau: float,
        policy_noise: float,
        noise_clip: float,
        normalize_q: bool,
        hjb_discount: float,
        sim_w: float,
        strict_dim: Any = None,
        strict_min: float = None,
        strict_max: float = None,
        averageR: float = 1.0,
):
    key, new_critic, new_metrics = update_critic(
        key, actor, critic, batch, gamma, critic_bc_coef, tau, policy_noise, noise_clip, metrics, hjb_discount, sim_w, strict_dim, strict_min, strict_max, averageR,
    )
    key, new_actor, new_critic, new_metrics = update_actor(key, actor,
                                                           new_critic, batch, actor_bc_coef, tau, normalize_q,
                                                           new_metrics)
    return key, new_actor, new_critic, new_metrics


def update_td3_no_targets(
        key: jax.random.PRNGKey,
        actor: TrainState,
        critic: CriticTrainState,
        batch: Dict[str, Any],
        gamma: float,
        metrics: Metrics,
        actor_bc_coef: float,
        critic_bc_coef: float,
        tau: float,
        policy_noise: float,
        noise_clip: float,
        hjb_discount: float,
        sim_w: float,
        strict_dim: Any = None,
        strict_min: float = None,
        strict_max: float = None,
        averageR: float = 1.0,
):
    key, new_critic, new_metrics = update_critic(
        key, actor, critic, batch, gamma, critic_bc_coef, tau, policy_noise, noise_clip, metrics, hjb_discount, sim_w, strict_dim, strict_min, strict_max, averageR,
    )
    return key, actor, new_critic, new_metrics


def action_fn(actor: TrainState) -> Callable:
    @jax.jit
    def _action_fn(obs: jax.Array) -> jax.Array:
        action = actor.apply_fn(actor.params, obs)
        return action

    return _action_fn


@pyrallis.wrap()
def main(config: Config):
    dict_config = asdict(config)
    dict_config["mlc_job_name"] = os.environ.get("PLATFORM_JOB_NAME")

    wandb.init(
        config=dict_config,
        project=config.project,
        group=config.group,
        name=config.name,
        id=str(uuid.uuid4()),
    )
    wandb.mark_preempting()
    buffer = ReplayBuffer()
    buffer.create_from_d4rl(config.dataset_name, config.normalize_reward, config.normalize_states, strict_dim=config.strict_dim, strict_min=config.strict_min, strict_max=config.strict_max, strict_r=config.strict_r, subsample=config.subsample)

    key = jax.random.PRNGKey(seed=config.train_seed)
    key, actor_key, critic_key = jax.random.split(key, 3)

    eval_env = make_env(config.dataset_name, seed=config.eval_seed)
    eval_env = wrap_env(eval_env, buffer.mean, buffer.std)
    init_state = buffer.data["states"][0][None, ...]
    init_action = buffer.data["actions"][0][None, ...]

    actor_module = DetActor(action_dim=init_action.shape[-1], hidden_dim=config.hidden_dim, layernorm=config.actor_ln,
                            n_hiddens=config.actor_n_hiddens)
    actor = ActorTrainState.create(
        apply_fn=actor_module.apply,
        params=actor_module.init(actor_key, init_state),
        target_params=actor_module.init(actor_key, init_state),
        tx=optax.adam(learning_rate=config.actor_learning_rate),
    )

    critic_module = EnsembleCritic(hidden_dim=config.hidden_dim, num_critics=2, layernorm=config.critic_ln,
                                   n_hiddens=config.critic_n_hiddens)
    critic = CriticTrainState.create(
        apply_fn=critic_module.apply,
        params=critic_module.init(critic_key, init_state, init_action),
        target_params=critic_module.init(critic_key, init_state, init_action),
        tx=optax.adam(learning_rate=config.critic_learning_rate),
    )

    update_td3_partial = partial(
        update_td3, gamma=config.gamma,
        actor_bc_coef=config.actor_bc_coef, critic_bc_coef=config.critic_bc_coef, tau=config.tau,
        policy_noise=config.policy_noise,
        noise_clip=config.noise_clip,
        normalize_q=config.normalize_q,
        hjb_discount=config.hjb_discount,
        sim_w=config.sim_w,
        strict_dim=config.strict_dim,
        strict_min=config.strict_min,
        strict_max=config.strict_max,
        averageR=config.averageR,
    )

    update_td3_no_targets_partial = partial(
        update_td3_no_targets, gamma=config.gamma,
        actor_bc_coef=config.actor_bc_coef, critic_bc_coef=config.critic_bc_coef, tau=config.tau,
        policy_noise=config.policy_noise,
        noise_clip=config.noise_clip,
        hjb_discount=config.hjb_discount,
        sim_w=config.sim_w,
        strict_dim=config.strict_dim,
        strict_min=config.strict_min,
        strict_max=config.strict_max,
        averageR=config.averageR,
    )

    def td3_loop_update_step(i, carry):
        key, batch_key = jax.random.split(carry["key"])
        batch = carry["buffer"].sample_batch(batch_key, batch_size=config.batch_size)

        full_update = partial(update_td3_partial,
                              key=key,
                              actor=carry["actor"],
                              critic=carry["critic"],
                              batch=batch,
                              metrics=carry["metrics"])

        update = partial(update_td3_no_targets_partial,
                         key=key,
                         actor=carry["actor"],
                         critic=carry["critic"],
                         batch=batch,
                         metrics=carry["metrics"])

        key, new_actor, new_critic, new_metrics = jax.lax.cond(update_carry["delayed_updates"][i], full_update, update)

        carry.update(
            key=key, actor=new_actor, critic=new_critic, metrics=new_metrics
        )
        return carry

    # metrics
    bc_metrics_to_log = [
        "critic_loss", "q_min", "actor_loss", "batch_entropy",
        "bc_mse_policy", "bc_mse_random", "action_mse", "sim_loss", "hjb_loss", "td_loss"
    ]
    # shared carry for update loops
    update_carry = {
        "key": key,
        "actor": actor,
        "critic": critic,
        "buffer": buffer,
        "delayed_updates": jax.numpy.equal(
            jax.numpy.arange(config.num_updates_on_epoch) % config.policy_freq, 0
        ).astype(int)
    }

    @jax.jit
    def actor_action_fn(params, obs):
        return actor.apply_fn(params, obs)

    dir_path = 'models/{}'.format(config.name)
    # Check if directory exists, if not, create it
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    for epoch in trange(config.num_epochs, desc="ReBRAC Epochs"):
        # for epoch in range(config.num_epochs):
        # metrics for accumulation during epoch and logging to wandb, we need to reset them every epoch
        update_carry["metrics"] = Metrics.create(bc_metrics_to_log)

        update_carry = jax.lax.fori_loop(
            lower=0,
            upper=config.num_updates_on_epoch,
            body_fun=td3_loop_update_step,
            init_val=update_carry
        )
        # log mean over epoch for each metric
        mean_metrics = update_carry["metrics"].compute()
        wandb.log({"epoch": epoch, **{f"ReBRAC/{k}": v for k, v in mean_metrics.items()}})

        if epoch % config.eval_every == 0 or epoch == config.num_epochs - 1:
            eval_returns = evaluate(eval_env, update_carry["actor"].params, actor_action_fn, config.eval_episodes,
                                    seed=config.eval_seed)
            normalized_score = eval_env.get_normalized_score(eval_returns) * 100.0
            wandb.log({
                "epoch": epoch,
                "eval/return_mean": np.mean(eval_returns),
                "eval/return_std": np.std(eval_returns),
                "eval/normalized_score_mean": np.mean(normalized_score),
                "eval/normalized_score_std": np.std(normalized_score)
            })

    serialized_params = flax.serialization.to_bytes(update_carry["actor"].params)
    with open(os.path.join(dir_path, 'actor_checkpoint.pkl'), 'wb') as f:
        f.write(serialized_params)

    serialized_params = flax.serialization.to_bytes(update_carry["critic"].params)
    with open(os.path.join(dir_path, 'critic_checkpoint.pkl'), 'wb') as f:
        f.write(serialized_params)

if __name__ == "__main__":
    main()
