import sys
from collections import namedtuple
from itertools import count
import random
import logging
import os
import argparse
import shutil
import pprint
import configparser

import git
import gym
import gym.spaces
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tensorboardX import SummaryWriter

from crowd_sim.envs.utils.action import ActionXY, ActionRot
from crowd_sim.envs.policy.orca import ORCA
from crowd_nav.policy.sarl import SARL
from visual_nav.utils.replay_buffer import ReplayBuffer
from visual_nav.utils.my_monitor import MyMonitor
from visual_nav.utils.schedule import LinearSchedule, ConstantSchedule
from visual_sim.envs.visual_sim import VisualSim


"""
    OptimizerSpec containing following attributes
        constructor: The optimizer constructor ex: RMSprop
        kwargs: {Dict} arguments for constructing optimizer
"""
OptimizerSpec = namedtuple("OptimizerSpec", ["constructor", "kwargs"])


class DQN(nn.Module):
    def __init__(self, in_channels=4, num_actions=18, with_attention=False):
        """
        Initialize a deep Q-learning network as described in
        https://storage.googleapis.com/deepmind-data/assets/papers/DeepMindNature14236Paper.pdf
        Arguments:
            in_channels: number of channel of input.
                i.e The number of most recent frames stacked together as describe in the paper
            num_actions: number of action-value to output, one-to-one correspondence to action in game.
        """
        super(DQN, self).__init__()
        self.with_attention = with_attention
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


class Trainer(object):
    def __init__(self,
                 env,
                 q_func,
                 device,
                 output_dir,
                 replay_buffer_size=1000000,
                 batch_size=128,
                 gamma=0.9,
                 frame_history_len=4,
                 target_update_freq=10000,
                 num_test_case=100,
                 ):
        self.env = env
        self.device = device
        self.batch_size = batch_size
        self.gamma = gamma
        self.frame_history_len = frame_history_len
        self.target_update_freq = target_update_freq
        self.output_dir = output_dir
        self.num_test_case = num_test_case

        img_h, img_w, img_c = env.observation_space.shape
        input_arg = frame_history_len * img_c
        self.num_actions = env.action_space.n
        self.image_size = (img_h, img_w, img_c)
        self.time_step = env.unwrapped.time_step

        self.Q = q_func(input_arg, self.num_actions).to(device)
        self.target_Q = q_func(input_arg, self.num_actions).to(device)
        self.replay_buffer = ReplayBuffer(replay_buffer_size, frame_history_len, self.image_size)

        self.log_every_n_steps = 10000
        self.num_param_updates = 0
        # map action_rot to its index and action_xy
        self.action_dict = {action: (i, ActionXY(action.v * np.cos(action.r), action.v * np.sin(action.r)))
                            for i, action in enumerate(self.env.unwrapped.actions)}

    def imitation_learning(self, optimizer_spec, update='td'):
        """
        Imitation learning and reinforcement learning share the same environment, replay buffer and Q function

        """
        if self.load_il_weights():
            return

        logging.info('Start imitation learning')
        model_dir = 'data/sarl'
        assert os.path.exists(model_dir)
        policy = SARL()
        policy.epsilon = 0
        policy_config = configparser.RawConfigParser()
        policy_config.read(os.path.join(model_dir, 'policy.config'))
        policy.configure(policy_config)
        policy.model.load_state_dict(torch.load(os.path.join(model_dir, 'rl_model.pth')))

        policy.set_device(torch.device('cpu'))
        policy.set_phase('test')
        policy.time_step = self.time_step

        if update == 'td':
            demonstrate_steps = 50000
            num_train_batch = 1000000
            optimizer = optimizer_spec.constructor(self.Q.parameters(), **optimizer_spec.kwargs)

            obs = self.env.reset()
            joint_state = self.env.unwrapped.compute_coordinate_observation()
            for step in count():
                last_idx = self.replay_buffer.store_observation(obs)
                demonstration = policy.predict(joint_state)
                action_rot, index = self._approximate_action(demonstration)
                obs, reward, done, info = self.env.step(action_rot)
                self.replay_buffer.store_effect(last_idx, torch.IntTensor([[index]]), reward, done)

                if done:
                    if step > demonstrate_steps:
                        break
                    else:
                        logging.info(self.env.get_episode_summary())
                        obs = self.env.reset()

                joint_state = self.env.unwrapped.compute_coordinate_observation()

            # finish collecting experience and update the model
            for _ in range(num_train_batch):
                self._td_update(optimizer)
        elif update == 'mc':
            num_episodes = 1000
            num_train_batch = num_episodes * 100
            criterion = nn.MSELoss().to(self.device)
            optimizer = optim.Adam(self.Q.parameters(), lr=0.001)

            for episodes in range(num_episodes):
                obs = self.env.reset()
                joint_state = self.env.unwrapped.compute_coordinate_observation()
                done = False

                indices = []
                rewards = []
                while not done:
                    last_idx = self.replay_buffer.store_observation(obs)
                    action_xy = policy.predict(joint_state)
                    action_rot, index = self._approximate_action(action_xy)
                    obs, reward, done, info = self.env.step(action_rot)
                    self.replay_buffer.store_effect(last_idx, torch.IntTensor([[index]]), reward, done)
                    indices.append(last_idx)
                    rewards.append(reward)

                    if done:
                        logging.info(self.env.get_episode_summary())
                        obs = self.env.reset()

                    joint_state = self.env.unwrapped.compute_coordinate_observation()

                for i, index in enumerate(indices):
                    value = sum([pow(self.gamma, max(t - i, 0) * self.time_step) * reward
                                 for t, reward in enumerate(rewards)])
                    self.replay_buffer.store_value(index, value)

            # finish collecting experience and update the model
            for _ in range(num_train_batch):
                self._mc_update(optimizer, criterion)
        else:
            raise NotImplementedError

        torch.save(self.Q.state_dict(), weights_file)
        logging.info('Save imitation learning trained weights to {}'.format(weights_file))

        self.test()

    def _approximate_action(self, demonstration):
        """ Approximate demonstration action with closest target action"""
        min_diff = float('inf')
        target_action = None
        index = -1
        for action_rot, value in self.action_dict.items():
            i, action_xy = value
            if isinstance(demonstration, ActionXY):
                diff = np.linalg.norm(np.array(action_xy) - np.array(demonstration))
            else:
                diff = np.linalg.norm(np.array(action_rot) - np.array(demonstration))
            if diff < min_diff:
                min_diff = diff
                target_action = action_rot
                index = i

        return target_action, index

    def test(self):
        logging.info('Start testing model')
        replay_buffer = ReplayBuffer(100000, self.frame_history_len, self.image_size)

        for i in range(self.num_test_case):
            obs = self.env.reset()
            done = False
            while not done:
                last_idx = replay_buffer.store_observation(obs)
                recent_observations = replay_buffer.encode_recent_observation()
                action = self.act(recent_observations)
                obs, reward, done, info = self.env.step(action.item())
                replay_buffer.store_effect(last_idx, action, reward, done)

                action_rot = self.env.unwrapped.actions[action.item()]
                logging.debug('Action velocity: {:.2f}, rotation: {:.2f}'.format(action_rot[0], np.rad2deg(action_rot[1])))

            logging.info(self.env.get_episode_summary())

        logging.info(self.env.get_episodes_summary(num_last_episodes=self.num_test_case))

    def reinforcement_learning(self, optimizer_spec, exploration, learning_starts=50000,
                               learning_freq=4, num_timesteps=2000000, episode_update=False):
        statistics_file = os.path.join(self.output_dir, 'statistics.json')
        self.load_rl_weights()
        logging.info('Start reinforcement learning')
        writer = SummaryWriter()
        episode_starts = len(self.env.get_episode_rewards())
        avg_reward = -float('nan')
        success_rate = -float('nan')
        collision_rate = -float('nan')
        overtime_rate = -float('nan')
        avg_time = -float('nan')
        best_avg_episode_reward = -float('inf')
        last_obs = self.env.reset()
        optimizer = optimizer_spec.constructor(self.Q.parameters(), **optimizer_spec.kwargs)

        t = 0
        while True:
            # Check stopping criterion
            if self.env.get_total_steps() > num_timesteps:
                break

            if not episode_update:
                last_idx = self.replay_buffer.store_observation(last_obs)
                recent_observations = self.replay_buffer.encode_recent_observation()

                # Choose random action if not yet start learning
                if t > learning_starts:
                    eps_threshold = exploration.value(t)
                    action = self._select_epsilon_greedy_action(self.Q, recent_observations, eps_threshold)[0]
                else:
                    action = torch.IntTensor([[random.randrange(self.num_actions)]])
                # Advance one step
                obs, reward, done, info = self.env.step(action.item())
                # Store other info in replay memory
                self.replay_buffer.store_effect(last_idx, action, reward, done)
                # Resets the environment when reaching an episode boundary.
                if done:
                    logging.info(self.env.get_episode_summary() + ' in step {}'.format(t))
                    obs = self.env.reset()
                last_obs = obs

                if (t > learning_starts and t % learning_freq == 0 and
                        self.replay_buffer.can_sample(self.batch_size)):
                    self._td_update(optimizer)
                t += 1
            else:
                done = False
                observations = []
                frames = []
                goals = []
                effects = []
                while not done:
                    frame, goal = last_obs
                    frame = np.array(frame).astype(np.float32)
                    goal = np.array(goal).astype(np.float32)

                    # transpose image frame into (img_c, img_h, img_w)
                    frame = frame.transpose(2, 0, 1)
                    frames.append(frame)
                    goals.append(goal)

                    frame_concat = []
                    goal_concat = []
                    if len(frames) < self.frame_history_len:
                        for _ in range(self.frame_history_len - len(frames)):
                            frame_concat.append(np.zeros_like(frame))
                            goal_concat.append(np.zeros_like(goal))
                        frame_concat += frames
                        goal_concat += goals
                    else:
                        frame_concat = frames[-4:]
                        goal_concat = goals[-4:]
                    frame_concat = np.concatenate(frame_concat, 0)
                    goal_concat = np.concatenate(goal_concat, 0)
                    recent_observations = frame_concat, goal_concat

                    if t > learning_starts:
                        eps_threshold = exploration.value(t)
                        action = self._select_epsilon_greedy_action(self.Q, recent_observations, eps_threshold)[0]
                    else:
                        action = torch.IntTensor([[random.randrange(self.num_actions)]])

                    obs, reward, done, info = self.env.step(action.item())

                    observations.append(last_obs)
                    effects.append((action, reward, done))
                    last_obs = obs
                    t += 1

                if info in ['Success', 'Collision']:
                    # only update the replay buffer if the robot has positive reward
                    for obs, effect in zip(observations, effects):
                        last_idx = self.replay_buffer.store_observation(obs)
                        self.replay_buffer.store_effect(last_idx, *effect)
                        if (t > learning_starts and t % learning_freq == 0 and
                                self.replay_buffer.can_sample(self.batch_size)):
                            self._td_update(optimizer)

                last_obs = self.env.reset()
                logging.info(self.env.get_episode_summary() + ' in step {}'.format(t))

            # Log progress and keep track of statistics
            num_last_episodes = 100
            episode_rewards = self.env.get_episode_rewards()[episode_starts:]
            num_episodes = len(episode_rewards)
            if num_episodes > 0:
                avg_reward = self.env.get_average_reward(num_last_episodes, episode_starts)
                success_rate = self.env.get_success_rate(num_last_episodes, episode_starts)
                collision_rate = self.env.get_collision_rate(num_last_episodes, episode_starts)
                overtime_rate = self.env.get_overtime_rate(num_last_episodes, episode_starts)
                avg_time = self.env.get_average_time(num_last_episodes, episode_starts)
            if num_episodes > num_last_episodes:
                best_avg_episode_reward = max(best_avg_episode_reward, avg_reward)

            writer.add_scalar('data/mean_episode_rewards', avg_reward, t)
            writer.add_scalar('data/best_mean_episode_rewards', best_avg_episode_reward, t)
            writer.add_scalar('data/success_rate', success_rate, t)
            writer.add_scalar('data/collision_rate', collision_rate, t)
            writer.add_scalar('data/overtime_rate', overtime_rate, t)
            writer.add_scalar('data/mean_episode_time', avg_time, t)

            if t % self.log_every_n_steps == 0 and t > learning_starts:
                logging.info("Timestep %d" % (t,))
                logging.info("mean reward (100 episodes) %f" % avg_reward)
                logging.info("best mean reward %f" % best_avg_episode_reward)
                logging.info("episodes %d" % num_episodes)
                logging.info("exploration %f" % exploration.value(t))
                sys.stdout.flush()

                # Dump statistics to json file
                writer.export_scalars_to_json(statistics_file)
                logging.info("Saved to %s" % statistics_file)

                torch.save(self.Q.state_dict(), weights_file)

        writer.close()

    def _select_epsilon_greedy_action(self, model, obs, eps_threshold):
        sample = random.random()
        if sample > eps_threshold:
            frames = torch.from_numpy(obs[0]).unsqueeze(0).to(self.device) / 255.0
            goals = torch.from_numpy(obs[1]).unsqueeze(0).to(self.device)
            # Use volatile = True if variable is only used in inference mode, i.e. don’t save the history
            return model(frames, goals).data.max(1)[1].cpu()
        else:
            return torch.IntTensor([random.randrange(self.num_actions)])

    def act(self, obs):
        frames = torch.from_numpy(obs[0]).unsqueeze(0).to(self.device) / 255.0
        goals = torch.from_numpy(np.array(obs[1])).unsqueeze(0).to(self.device)
        return self.Q(frames, goals).data.max(1)[1].cpu()

    def _td_update(self, optimizer):
        # Use the replay buffer to sample a batch of transitions
        # Note: done_mask[i] is 1 if the next state corresponds to the end of an episode,
        # in which case there is no Q-value at the next state; at the end of an
        # episode, only the current state reward contributes to the target
        frames_batch, goals_batch, action_batch, reward_batch, next_frames_batch, next_goals_batch, done_mask = \
            self.replay_buffer.sample(self.batch_size)
        # Convert numpy nd_array to torch variables for calculation
        frames_batch = torch.from_numpy(frames_batch).to(self.device) / 255.0
        goals_batch = torch.from_numpy(goals_batch).to(self.device)
        action_batch = torch.from_numpy(action_batch).long().to(self.device)
        reward_batch = torch.from_numpy(reward_batch).to(self.device)
        next_frames_batch = torch.from_numpy(next_frames_batch).to(self.device) / 255.0
        next_goals_batch = torch.from_numpy(next_goals_batch).to(self.device)
        not_done_mask = torch.from_numpy(1 - done_mask).to(self.device)

        # Compute current Q value, q_func takes only state and output value for every state-action pair
        # We choose Q based on action taken, action is used to index the value in the dqn output
        # current_q_values[i][j] = Q_outputs[i][action_batch[i][j]], where j=0
        current_q_values = self.Q(frames_batch, goals_batch).gather(1, action_batch.unsqueeze(1)).squeeze(1)
        # Compute next Q value based on which action gives max Q values
        # Detach variable from the current graph since we don't want gradients for next Q to propagated
        next_max_q = self.target_Q(next_frames_batch, next_goals_batch).detach().max(1)[0]
        next_q_values = not_done_mask * next_max_q
        # Compute the target of the current Q values
        target_q_values = reward_batch + (pow(self.gamma, self.time_step) * next_q_values)

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

    def _mc_update(self, optimizer, criterion):
        frames_batch, goals_batch, action_batch, _, _, _, _, value_batch = \
            self.replay_buffer.sample(self.batch_size, with_value=True)
        # Convert numpy nd_array to torch variables for calculation
        frames_batch = torch.from_numpy(frames_batch).to(self.device) / 255.0
        goals_batch = torch.from_numpy(goals_batch).to(self.device)
        action_batch = torch.from_numpy(action_batch).long().to(self.device)
        value_batch = torch.from_numpy(value_batch).to(self.device)

        current_q_values = self.Q(frames_batch, goals_batch).gather(1, action_batch.unsqueeze(1)).squeeze(1)
        loss = criterion(current_q_values, value_batch)
        optimizer.zero_grad()
        loss.backward()
        logging.info('Batch loss: {:.4f}'.format(loss.item()))

        # Perform the update
        optimizer.step()
        self.num_param_updates += 1

        # Periodically update the target network by Q network to target Q network
        if self.num_param_updates % self.target_update_freq == 0:
            self.target_Q.load_state_dict(self.Q.state_dict())

    def load_il_weights(self):
        weights_file = os.path.join(self.output_dir, 'il_model.pth')
        if os.path.exists(weights_file):
            self.Q.load_state_dict(torch.load(weights_file))
            self.target_Q.load_state_dict(torch.load(weights_file))
            logging.info('Imitation learning trained weight loaded')
            return True
        else:
            return False

    def load_rl_weights(self):
        weights_file = os.path.join(self.output_dir, 'rl_model.pth')
        if os.path.exists(weights_file):
            self.Q.load_state_dict(torch.load(weights_file))
            self.target_Q.load_state_dict(torch.load(weights_file))
            logging.info('Reinforcement learning trained weight loaded')
            return True
        else:
            return False


def main():
    parser = argparse.ArgumentParser('Parse configuration file')
    parser.add_argument('--output_dir', type=str, default='data/output')
    parser.add_argument('--debug', default=False, action='store_true')
    parser.add_argument('--with_il', default=False, action='store_true')
    parser.add_argument('--il_update', type=str, default='td')
    parser.add_argument('--eps_start', type=float, default=1)
    parser.add_argument('--eps_end', type=float, default=0.1)
    parser.add_argument('--eps_decay_steps', type=int, default=1000000)
    parser.add_argument('--gamma', type=float, default=0.9)
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--num_timesteps', type=int, default=2000000)
    parser.add_argument('--learning_starts', type=int, default=50000)
    parser.add_argument('--reward_shaping', default=False, action='store_true')
    parser.add_argument('--curriculum_learning', default=False, action='store_true')
    parser.add_argument('--test_il', default=False, action='store_true')
    parser.add_argument('--test_rl', default=False, action='store_true')
    parser.add_argument('--num_test_case', type=int, default=50)
    parser.add_argument('--show_image', default=False, action='store_true')
    parser.add_argument('--episode_update', default=False, action='store_true')
    args = parser.parse_args()

    if args.test_il or args.test_rl:
        if not os.path.exists(args.output_dir):
            raise ValueError('Model dir does not exist')
    else:
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
    monitor_output_dir = os.path.join(args.output_dir, 'monitor-outputs')

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

    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(vars(args))

    # configure environment
    env = VisualSim(reward_shaping=args.reward_shaping, curriculum_learning=args.curriculum_learning)
    env = MyMonitor(env, monitor_output_dir, args.show_image)
    assert type(env.observation_space) == gym.spaces.Box
    assert type(env.action_space) == gym.spaces.Discrete

    trainer = Trainer(
        env=env,
        q_func=DQN,
        device=device,
        output_dir=args.output_dir,
        replay_buffer_size=100000,
        batch_size=args.batch_size,
        gamma=args.gamma,
        frame_history_len=4,
        target_update_freq=10000,
        num_test_case=args.num_test_case
    )

    if args.test_il:
        trainer.load_il_weights()
        trainer.test()
    elif args.test_rl:
        trainer.load_rl_weights()
        trainer.test()
    else:
        # imitation learning
        il_optimizer_spec = OptimizerSpec(
            constructor=optim.RMSprop,
            kwargs=dict(lr=0.00025, alpha=0.95, eps=0.01),
        )
        if args.with_il:
            trainer.imitation_learning(
                optimizer_spec=il_optimizer_spec,
                update=args.il_update
            )

        # reinforcement learning
        rl_optimizer_spec = OptimizerSpec(
            constructor=optim.RMSprop,
            kwargs=dict(lr=0.00025, alpha=0.95, eps=0.01),
        )
        if args.eps_decay_steps == 0:
            exploration_schedule = ConstantSchedule(args.eps_end)
            logging.info('Use constant exploration rate: {}'.format(args.eps_end))
        else:
            exploration_schedule = LinearSchedule(args.eps_decay_steps, args.eps_end, args.eps_start)
        trainer.reinforcement_learning(
            optimizer_spec=rl_optimizer_spec,
            exploration=exploration_schedule,
            learning_starts=args.learning_starts,
            learning_freq=4,
            num_timesteps=args.num_timesteps,
            episode_update=args.episode_update
        )


if __name__ == '__main__':
    main()
