import sys
import pickle
from collections import namedtuple
from itertools import count
import random
import logging
import os
import argparse
import shutil

import git
import gym
import gym.spaces
from gym import wrappers
import numpy as np
import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from crowd_sim.envs.utils.action import ActionXY
from crowd_sim.envs.policy.orca import ORCA
from visual_nav.utils.replay_buffer import ReplayBuffer
from visual_nav.utils.gym import get_wrapper_by_name
from visual_nav.utils.schedule import LinearSchedule
from visual_sim.envs.visual_sim import VisualSim


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
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.fc4 = nn.Linear(7 * 7 * 64, 512)
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


class Trainer(object):
    def __init__(self,
                 env,
                 q_func,
                 replay_buffer_size=1000000,
                 batch_size=128,
                 gamma=0.99,
                 frame_history_len=4,
                 target_update_freq=10000
                 ):
        self.env = env
        self.batch_size = batch_size
        self.gamma = gamma
        self.frame_history_len = frame_history_len
        self.target_update_freq = target_update_freq

        img_h, img_w, img_c = env.observation_space.shape
        input_arg = frame_history_len * img_c
        self.num_actions = env.action_space.n

        self.Q = q_func(input_arg, self.num_actions).type(dtype)
        self.target_Q = q_func(input_arg, self.num_actions).type(dtype)
        self.replay_buffer = ReplayBuffer(replay_buffer_size, frame_history_len)

        self.log_every_n_steps = 10000
        self.num_param_updates = 0
        self.actions = None

    def imitation_learning(self, optimizer_spec, weights_file, demonstrate_steps=50000, update_steps=10000):
        # TODO: how to optimize q-value function
        if os.path.exists(weights_file):
            self.Q.load_state_dict(torch.load(weights_file))
            self.target_Q.load_state_dict(torch.load(weights_file))
            logging.info('Imitation learning trained weight loaded')
            self.test()
            return

        optimizer = optimizer_spec.constructor(self.Q.parameters(), **optimizer_spec.kwargs)

        logging.info('Start imitation learning')
        # TODO: use 2D RL as demonstrator
        demonstrator = ORCA()
        demonstrator.set_device(torch.device('cpu'))
        demonstrator.set_phase('test')
        demonstrator.time_step = self.env.unwrapped.time_step

        obs = self.env.reset()
        joint_state = self.env.unwrapped.compute_coordinate_observation()
        for step in count():
            last_idx = self.replay_buffer.store_observation(obs)
            action_rot = demonstrator.predict(joint_state)
            action_xy, index = self.translate_action(action_rot)
            obs, reward, done, info = self.env.step(action_rot)
            self.replay_buffer.store_effect(last_idx, torch.IntTensor([[index]]), reward, done)

            if info:
                logging.debug('Episode finished with signal: {} in {}s'.format(info, self.env.unwrapped.time))
            if done:
                if step > demonstrate_steps:
                    break
                else:
                   obs = self.env.reset()

            joint_state = self.env.unwrapped.compute_coordinate_observation()

        # finish collecting experience and update the model
        for _ in range(update_steps):
            self.td_update(optimizer)
        torch.save(self.Q.state_dict(), weights_file)
        logging.info('Save imitation learning trained weights to {}'.format(weights_file))

        self.test()

    def translate_action(self, demonstration):
        """ Translate demonstration action into target action category"""
        assert isinstance(demonstration, ActionXY)
        if self.actions is None:
            actions = self.env.unwrapped.actions
            self.actions = [ActionXY(action.v * np.cos(action.r), action.v * np.sin(action.r)) for action in actions]

        min_diff = float('inf')
        index = -1
        for i, action in enumerate(self.actions):
            diff = np.linalg.norm(np.array(action) - np.array(demonstration))
            if diff < min_diff:
                min_diff = diff
                index = i

        return self.actions[index], index

    def test(self):
        logging.info('Start testing model')
        replay_buffer = ReplayBuffer(100000, self.frame_history_len)

        test_case_num = self.env.unwrapped.test_case_num
        success = 0
        collision = 0
        overtime = 0
        time = []
        for i in range(test_case_num):
            obs = self.env.reset()
            done = False
            while not done:
                last_idx = replay_buffer.store_observation(obs)
                recent_observations = replay_buffer.encode_recent_observation()
                action = self.select_greedy_action(recent_observations)
                ob, reward, done, info = self.env.step(action.item())
                replay_buffer.store_effect(last_idx, action, reward, done)
                if info == 'Accomplishment':
                    success += 1
                    time.append(self.env.time)
                elif info == 'Collision':
                    collision += 1
                elif info == 'Overtime':
                    overtime += 1

            logging.info('Episode ends with signal: {} in {}s'.format(info, self.env.unwrapped.time))

        avg_time = sum(time) / len(time) if time else 0
        logging.info('Success: {:.2f}, collision: {:.2f}, overtime: {:.2f}, average time: {:.2f}s'.format(
            success / test_case_num, collision / test_case_num, overtime / test_case_num, avg_time))

    def reinforcement_learning(self, optimizer_spec, exploration, weights_file, statistics_file,
                               stopping_criterion=None, learning_starts=50000, learning_freq=4):
        if os.path.exists(weights_file):
            self.Q.load_state_dict(torch.load(weights_file))
            self.target_Q.load_state_dict(torch.load(weights_file))
            logging.info('Reinforcement learning trained weight loaded')

        logging.info('Start reinforcement learning')
        mean_episode_reward = -float('nan')
        best_mean_episode_reward = -float('inf')
        last_obs = self.env.reset()

        optimizer = optimizer_spec.constructor(self.Q.parameters(), **optimizer_spec.kwargs)

        for t in count():
            # Check stopping criterion
            if stopping_criterion is not None and stopping_criterion(self.env):
                break

            # Step the env and store the transition
            # Store last observation in replay memory and last_idx can be used to store action, reward, done
            last_idx = self.replay_buffer.store_observation(last_obs)
            # encode_recent_observation will take the latest observation
            # that you pushed into the buffer and compute the corresponding
            # input that should be given to a Q network by appending some
            # previous frames.
            recent_observations = self.replay_buffer.encode_recent_observation()

            # Choose random action if not yet start learning
            if t > learning_starts:
                eps_threshold = exploration.value(t)
                action = self.select_epsilon_greedy_action(self.Q, recent_observations, eps_threshold)[0]
            else:
                action = torch.IntTensor([[random.randrange(self.num_actions)]])
            # Advance one step
            obs, reward, done, info = self.env.step(action.item())
            if info:
                logging.debug('Episode finished with signal: {} in {}s'.format(info, self.env.unwrapped.time))
            # clip rewards between -1 and 1
            reward = max(-1.0, min(reward, 1.0))
            # Store other info in replay memory
            self.replay_buffer.store_effect(last_idx, action, reward, done)
            # Resets the environment when reaching an episode boundary.
            if done:
                obs = self.env.reset()
            last_obs = obs

            # Perform experience replay and train the network.
            # Note that this is only done if the replay buffer contains enough samples
            # for us to learn something useful -- until then, the model will not be
            # initialized and random actions should be taken
            if (t > learning_starts and t % learning_freq == 0 and
                    self.replay_buffer.can_sample(self.batch_size)):
                self.td_update(optimizer)

            # Log progress and keep track of statistics
            episode_rewards = get_wrapper_by_name(self.env, "Monitor").get_episode_rewards()
            if len(episode_rewards) > 0:
                mean_episode_reward = np.mean(episode_rewards[-100:])
            if len(episode_rewards) > 100:
                best_mean_episode_reward = max(best_mean_episode_reward, mean_episode_reward)

            Statistic["mean_episode_rewards"].append(mean_episode_reward)
            Statistic["best_mean_episode_rewards"].append(best_mean_episode_reward)

            if t % self.log_every_n_steps == 0 and t > learning_starts:
                logging.info("Timestep %d" % (t,))
                logging.info("mean reward (100 episodes) %f" % mean_episode_reward)
                logging.info("best mean reward %f" % best_mean_episode_reward)
                logging.info("episodes %d" % len(episode_rewards))
                logging.info("exploration %f" % exploration.value(t))
                sys.stdout.flush()

                # Dump statistics to pickle
                with open(statistics_file, 'wb') as f:
                    pickle.dump(Statistic, f)
                    logging.info("Saved to %s" % statistics_file)

                torch.save(self.Q.state_dict(), weights_file)

    def select_epsilon_greedy_action(self, model, obs, eps_threshold):
        sample = random.random()
        if sample > eps_threshold:
            frames = torch.from_numpy(obs[0]).type(dtype).unsqueeze(0) / 255.0
            goals = torch.from_numpy(obs[1]).type(dtype).unsqueeze(0)
            # Use volatile = True if variable is only used in inference mode, i.e. don’t save the history
            return model(Variable(frames), Variable(goals)).data.max(1)[1].cpu()
        else:
            return torch.IntTensor([random.randrange(self.num_actions)])

    def select_greedy_action(self, obs):
        frames = torch.from_numpy(obs[0]).type(dtype).unsqueeze(0) / 255.0
        goals = torch.from_numpy(np.array(obs[1])).type(dtype).unsqueeze(0)
        return self.Q(Variable(frames), Variable(goals)).data.max(1)[1].cpu()

    def td_update(self, optimizer):
        # Use the replay buffer to sample a batch of transitions
        # Note: done_mask[i] is 1 if the next state corresponds to the end of an episode,
        # in which case there is no Q-value at the next state; at the end of an
        # episode, only the current state reward contributes to the target
        frames_batch, goals_batch, action_batch, reward_batch, next_frames_batch, next_goals_batch, done_mask = \
            self.replay_buffer.sample(self.batch_size)
        # Convert numpy nd_array to torch variables for calculation
        frames_batch = Variable(torch.from_numpy(frames_batch).type(dtype) / 255.0)
        goals_batch = Variable(torch.from_numpy(goals_batch).type(dtype))
        action_batch = Variable(torch.from_numpy(action_batch).long())
        reward_batch = Variable(torch.from_numpy(reward_batch))
        next_frames_batch = Variable(torch.from_numpy(next_frames_batch).type(dtype) / 255.0)
        next_goals_batch = Variable(torch.from_numpy(next_goals_batch).type(dtype))
        not_done_mask = Variable(torch.from_numpy(1 - done_mask)).type(dtype)

        if USE_CUDA:
            action_batch = action_batch.cuda()
            reward_batch = reward_batch.cuda()

        # Compute current Q value, q_func takes only state and output value for every state-action pair
        # We choose Q based on action taken, action is used to index the value in the dqn output
        # current_q_values[i][j] = Q_outputs[i][action_batch[i][j]], where j=0
        current_q_values = self.Q(frames_batch, goals_batch).gather(1, action_batch.unsqueeze(1)).squeeze(1)
        # Compute next Q value based on which action gives max Q values
        # Detach variable from the current graph since we don't want gradients for next Q to propagated
        next_max_q = self.target_Q(next_frames_batch, next_goals_batch).detach().max(1)[0]
        next_q_values = not_done_mask * next_max_q
        # Compute the target of the current Q values
        target_q_values = reward_batch + (self.gamma * next_q_values)

        # Compute Bellman error
        td_error = target_q_values - current_q_values
        # clip the bellman error between [-1 , 1]
        clipped_bellman_error = td_error.clamp(-1, 1)
        # Note: clipped_bellman_delta * -1 will be right gradient w.r.t current_q_values
        # Cuz in the td_error, there is a negative sing before current_q_values
        d_error = clipped_bellman_error * -1.0
        # Clear previous gradients before backward pass
        optimizer.zero_grad()
        # run backward pass and back prop through Q network, d_error is the gradient of final loss w.r.t. Q
        current_q_values.backward(d_error.data)

        # # equivalent gradient computation, TODO: test
        # loss = (target_q_values - current_q_values).pow(2).mean()
        # self.optimizer.zero_grad()
        # loss.backward()

        # Perform the update
        optimizer.step()
        self.num_param_updates += 1

        # Periodically update the target network by Q network to target Q network
        if self.num_param_updates % self.target_update_freq == 0:
            self.target_Q.load_state_dict(self.Q.state_dict())


def main():
    parser = argparse.ArgumentParser('Parse configuration file')
    parser.add_argument('--output_dir', type=str, default='data/output')
    parser.add_argument('--debug', default=False, action='store_true')
    parser.add_argument('--without_il', default=False, action='store_true')
    args = parser.parse_args()

    # configure paths
    make_new_dir = True
    if os.path.exists(args.output_dir):
        key = input('Output directory already exists! Overwrite the folder? (y/n)')
        if key == 'y':
            shutil.rmtree(args.output_dir)
        else:
            make_new_dir = False
    if make_new_dir:
        os.makedirs(args.output_dir)
    log_file = os.path.join(args.output_dir, 'output.log')
    il_weights_file = os.path.join(args.output_dir, 'il_model.pth')
    rl_weights_file = os.path.join(args.output_dir, 'rl_model.pth')
    statistics_file = os.path.join(args.output_dir, 'statistics.pkl')
    monitor_dir = os.path.join(args.output_dir, 'monitor-outputs')

    # configure logging
    file_handler = logging.FileHandler(log_file, mode='a')
    stdout_handler = logging.StreamHandler(sys.stdout)
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, handlers=[stdout_handler, file_handler],
                        format='%(asctime)s, %(levelname)s: %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    repo = git.Repo(search_parent_directories=True)
    logging.info('Current git head hash code: {}'.format(repo.head.object.hexsha))
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    logging.info('Using device: %s', device)

    env = VisualSim(step_penalty=-0.01)
    env = wrappers.Monitor(env, monitor_dir, force=True)
    num_timesteps = 1000000

    assert type(env.observation_space) == gym.spaces.Box
    assert type(env.action_space) == gym.spaces.Discrete

    trainer = Trainer(
        env=env,
        q_func=DQN,
        # replay_buffer_size=1000000
        replay_buffer_size=100000,
        batch_size=32,
        gamma=0.99,
        frame_history_len=4,
        target_update_freq=10000
    )

    # imitation learning
    il_optimizer_spec = OptimizerSpec(
        constructor=optim.RMSprop,
        kwargs=dict(lr=0.01, alpha=0.95, eps=0.01),
    )
    if not args.without_il:
        trainer.imitation_learning(
            optimizer_spec=il_optimizer_spec,
            weights_file=il_weights_file,
            demonstrate_steps=50000,
            update_steps=100000
        )

    # reinforcement learning
    def stopping_criterion(env):
        # notice that here t is the number of steps of the wrapped env,
        # which is different from the number of steps in the underlying env
        return get_wrapper_by_name(env, "Monitor").get_total_steps() >= num_timesteps

    rl_optimizer_spec = OptimizerSpec(
        constructor=optim.RMSprop,
        kwargs=dict(lr=0.00025, alpha=0.95, eps=0.01),
    )
    exploration_schedule = LinearSchedule(1000000, 0.1)
    trainer.reinforcement_learning(
        optimizer_spec=rl_optimizer_spec,
        exploration=exploration_schedule,
        stopping_criterion=stopping_criterion,
        weights_file=rl_weights_file,
        statistics_file=statistics_file,
        # learning_starts=50000,
        learning_starts=50000,
        learning_freq=4
    )


if __name__ == '__main__':
    main()