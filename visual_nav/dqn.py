import sys
import pickle
from collections import namedtuple
from itertools import count
import random
import logging

import gym
import gym.spaces
from gym import wrappers

import numpy as np
import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from visual_nav.utils.replay_buffer import ReplayBuffer
from visual_nav.utils.gym import get_wrapper_by_name
from visual_nav.utils.schedule import LinearSchedule
from visual_sim.envs.visual_sim import VisualSim, ImageInfo


USE_CUDA = torch.cuda.is_available()
dtype = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor

"""
    OptimizerSpec containing following attributes
        constructor: The optimizer constructor ex: RMSprop
        kwargs: {Dict} arguments for constructing optimizer
"""
OptimizerSpec = namedtuple("OptimizerSpec", ["constructor", "kwargs"])

Statistic = {
    "mean_episode_rewards": [],
    "best_mean_episode_rewards": []
}


class DQN(nn.Module):
    def __init__(self, in_channels=4, num_actions=18):
        """
        Initialize a deep Q-learning network as described in
        https://storage.googleapis.com/deepmind-data/assets/papers/DeepMindNature14236Paper.pdf
        Arguments:
            in_channels: number of channel of input.
                i.e The number of most recent frames stacked together as describe in the paper
            num_actions: number of action-value to output, one-to-one correspondence to action in game.
        """
        super(DQN, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, 32, kernel_size=8, stride=4)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=4, stride=2)
        self.fc4 = nn.Linear(7 * 14 * 64, 512)
        self.fc5 = nn.Linear(520, num_actions)

    def forward(self, frames, goals):
        frames = F.relu(self.conv1(frames))
        frames = F.relu(self.conv2(frames))
        frames = F.relu(self.conv3(frames))
        frames = F.relu(self.fc4(frames.view(frames.size(0), -1)))
        features = torch.cat([frames, goals.view(goals.size(0), -1)], dim=1)
        return self.fc5(features)


class Variable(autograd.Variable):
    def __init__(self, data, *args, **kwargs):
        if USE_CUDA:
            data = data.cuda()
        super(Variable, self).__init__(data, *args, **kwargs)


def train(
    env,
    q_func,
    optimizer_spec,
    exploration,
    stopping_criterion=None,
    replay_buffer_size=1000000,
    batch_size=32,
    gamma=0.99,
    learning_starts=50000,
    learning_freq=4,
    frame_history_len=4,
    target_update_freq=10000
    ):

    """Run Deep Q-learning algorithm.

    You can specify your own convnet using q_func.

    All schedules are w.r.t. total number of steps taken in the environment.

    Parameters
    ----------
    env: gym.Env
        gym environment to train on.
    q_func: function
        Model to use for computing the q function. It should accept the
        following named arguments:
            input_channel: int
                number of channel of input.
            num_actions: int
                number of actions
    optimizer_spec: OptimizerSpec
        Specifying the constructor and kwargs, as well as learning rate schedule
        for the optimizer
    exploration: Schedule (defined in utils.schedule)
        schedule for probability of chosing random action.
    stopping_criterion: (env) -> bool
        should return true when it's ok for the RL algorithm to stop.
        takes in env and the number of steps executed so far.
    replay_buffer_size: int
        How many memories to store in the replay buffer.
    batch_size: int
        How many transitions to sample each time experience is replayed.
    gamma: float
        Discount Factor
    learning_starts: int
        After how many environment steps to start replaying experiences
    learning_freq: int
        How many steps of environment to take between every experience replay
    frame_history_len: int
        How many past frames to include as input to the model.
    target_update_freq: int
        How many experience replay rounds (not steps!) to perform between
        each update to the target Q network
    """
    assert type(env.observation_space) == gym.spaces.Box
    assert type(env.action_space)      == gym.spaces.Discrete

    ###############
    # BUILD MODEL #
    ###############

    img_h, img_w, img_c = env.observation_space.shape
    input_arg = frame_history_len * img_c
    num_actions = env.action_space.n

    # Construct an epsilon greedy policy with given exploration schedule
    def select_epsilon_greedy_action(model, obs, t):
        sample = random.random()
        eps_threshold = exploration.value(t)
        if sample > eps_threshold:
            frames = torch.from_numpy(obs[0]).type(dtype).unsqueeze(0) / 255.0
            goals = torch.from_numpy(obs[1]).type(dtype).unsqueeze(0)
            # Use volatile = True if variable is only used in inference mode, i.e. don’t save the history
            return model(Variable(frames), Variable(goals)).data.max(1)[1].cpu()
        else:
            return torch.IntTensor([random.randrange(num_actions)])

    # Initialize target q function and q function
    Q = q_func(input_arg, num_actions).type(dtype)
    target_Q = q_func(input_arg, num_actions).type(dtype)

    # Construct Q network optimizer function
    optimizer = optimizer_spec.constructor(Q.parameters(), **optimizer_spec.kwargs)

    # Construct the replay buffer
    replay_buffer = ReplayBuffer(replay_buffer_size, frame_history_len)

    ###############
    #   RUN ENV   #
    ###############
    num_param_updates = 0
    mean_episode_reward = -float('nan')
    best_mean_episode_reward = -float('inf')
    last_obs = env.reset()
    LOG_EVERY_N_STEPS = 10000

    for t in count():
        # Check stopping criterion
        if stopping_criterion is not None and stopping_criterion(env):
            break

        # Step the env and store the transition
        # Store last observation in replay memory and last_idx can be used to store action, reward, done
        last_idx = replay_buffer.store_observation(last_obs)
        # encode_recent_observation will take the latest observation
        # that you pushed into the buffer and compute the corresponding
        # input that should be given to a Q network by appending some
        # previous frames.
        recent_observations = replay_buffer.encode_recent_observation()

        # Choose random action if not yet start learning
        if t > learning_starts:
            action = select_epsilon_greedy_action(Q, recent_observations, t)[0]
        else:
            action = torch.IntTensor([[random.randrange(num_actions)]])
        # Advance one step
        obs, reward, done, info = env.step(action.item())
        # clip rewards between -1 and 1
        reward = max(-1.0, min(reward, 1.0))
        # Store other info in replay memory
        replay_buffer.store_effect(last_idx, action, reward, done)
        # Resets the environment when reaching an episode boundary.
        if done:
            obs = env.reset()
        last_obs = obs

        # Perform experience replay and train the network.
        # Note that this is only done if the replay buffer contains enough samples
        # for us to learn something useful -- until then, the model will not be
        # initialized and random actions should be taken
        if (t > learning_starts and
                t % learning_freq == 0 and
                replay_buffer.can_sample(batch_size)):
            # Use the replay buffer to sample a batch of transitions
            # Note: done_mask[i] is 1 if the next state corresponds to the end of an episode,
            # in which case there is no Q-value at the next state; at the end of an
            # episode, only the current state reward contributes to the target
            frames_batch, goals_batch, act_batch, rew_batch, next_frames_batch, next_goals_batch, done_mask = \
                replay_buffer.sample(batch_size)
            # Convert numpy nd_array to torch variables for calculation
            frames_batch = Variable(torch.from_numpy(frames_batch).type(dtype) / 255.0)
            goals_batch = Variable(torch.from_numpy(goals_batch).type(dtype))
            act_batch = Variable(torch.from_numpy(act_batch).long())
            rew_batch = Variable(torch.from_numpy(rew_batch))
            next_frames_batch = Variable(torch.from_numpy(next_frames_batch).type(dtype) / 255.0)
            next_goals_batch = Variable(torch.from_numpy(next_goals_batch).type(dtype))
            not_done_mask = Variable(torch.from_numpy(1 - done_mask)).type(dtype)

            if USE_CUDA:
                act_batch = act_batch.cuda()
                rew_batch = rew_batch.cuda()

            # Compute current Q value, q_func takes only state and output value for every state-action pair
            # We choose Q based on action taken.
            current_Q_values = Q(frames_batch, goals_batch).gather(1, act_batch.unsqueeze(1)).squeeze(1)
            # Compute next Q value based on which action gives max Q values
            # Detach variable from the current graph since we don't want gradients for next Q to propagated
            next_max_q = target_Q(next_frames_batch, next_goals_batch).detach().max(1)[0]
            next_Q_values = not_done_mask * next_max_q
            # Compute the target of the current Q values
            target_Q_values = rew_batch + (gamma * next_Q_values)
            # Compute Bellman error
            bellman_error = target_Q_values - current_Q_values
            # clip the bellman error between [-1 , 1]
            clipped_bellman_error = bellman_error.clamp(-1, 1)
            # Note: clipped_bellman_delta * -1 will be right gradient w.r.t current_Q_values
            d_error = clipped_bellman_error * -1.0
            # Clear previous gradients before backward pass
            optimizer.zero_grad()
            # run backward pass and back prop through Q network, d_error is the gradient of final loss w.r.t. Q
            current_Q_values.backward(d_error.data)

            # Perform the update
            optimizer.step()
            num_param_updates += 1

            # Periodically update the target network by Q network to target Q network
            if num_param_updates % target_update_freq == 0:
                target_Q.load_state_dict(Q.state_dict())

        # 4. Log progress and keep track of statistics
        episode_rewards = get_wrapper_by_name(env, "Monitor").get_episode_rewards()
        if len(episode_rewards) > 0:
            mean_episode_reward = np.mean(episode_rewards[-100:])
        if len(episode_rewards) > 100:
            best_mean_episode_reward = max(best_mean_episode_reward, mean_episode_reward)

        Statistic["mean_episode_rewards"].append(mean_episode_reward)
        Statistic["best_mean_episode_rewards"].append(best_mean_episode_reward)

        if t % LOG_EVERY_N_STEPS == 0 and t > learning_starts:
            logging.info("Timestep %d" % (t,))
            logging.info("mean reward (100 episodes) %f" % mean_episode_reward)
            logging.info("best mean reward %f" % best_mean_episode_reward)
            logging.info("episodes %d" % len(episode_rewards))
            logging.info("exploration %f" % exploration.value(t))
            sys.stdout.flush()

            # Dump statistics to pickle
            with open('data/statistics.pkl', 'wb') as f:
                pickle.dump(Statistic, f)
                logging.info("Saved to %s" % 'data/statistics.pkl')

            torch.save(Q.state_dict(), 'data/weights.pth')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s, %(levelname)s: %(message)s',
                        datefmt="%Y-%m-%d %H:%M:%S")

    env = VisualSim()
    expt_dir = 'data/dqn-results'
    env = wrappers.Monitor(env, expt_dir, force=True)
    num_timesteps = 1000000

    def stopping_criterion(env):
        # notice that here t is the number of steps of the wrapped env,
        # which is different from the number of steps in the underlying env
        return get_wrapper_by_name(env, "Monitor").get_total_steps() >= num_timesteps

    optimizer_spec = OptimizerSpec(
        constructor=optim.RMSprop,
        kwargs=dict(lr=0.00025, alpha=0.95, eps=0.01),
    )

    exploration_schedule = LinearSchedule(1000000, 0.1)

    train(
        env=env,
        q_func=DQN,
        optimizer_spec=optimizer_spec,
        exploration=exploration_schedule,
        stopping_criterion=stopping_criterion,
        # replay_buffer_size=1000000
        replay_buffer_size=10000,
        batch_size=32,
        gamma=0.99,
        # learning_starts=50000,
        learning_starts=50,
        learning_freq=4,
        frame_history_len=4,
        target_update_freq=10000,
    )


if __name__ == '__main__':
    main()
