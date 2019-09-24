from collections import OrderedDict
import numpy as np
from gym.spaces import Box, Dict
import mujoco_py

from multiworld.core.serializable import Serializable
from multiworld.envs.env_util import (
    get_stat_in_paths,
    create_stats_ordered_dict,
    get_asset_full_path,
)

from multiworld.envs.mujoco.mujoco_env import MujocoEnv
import copy

from multiworld.core.multitask_env import MultitaskEnv
from scipy.spatial.transform import Rotation as R


def theta_to_quat(theta):
    r = R.from_euler('zxy', [theta, 0., 0.])
    x, y, z, w = r.as_quat()
    return w, x, y, z


def quat_to_theta(x, y, z, w):
    rotation = R.from_quat([x, y, z, w])
    euler = rotation.as_euler('zyx')
    theta = euler[0]
    return theta


class SawyerPushAndReachTXYEnv(MujocoEnv, Serializable, MultitaskEnv):
    INIT_HAND_POS = np.array([0, 0.4, 0.02])

    def __init__(
      self,
      reward_info=None,
      frame_skip=50,
      pos_action_scale=2. / 100,
      randomize_goals=True,
      hide_goal=False,
      init_block_low=(-0.05, 0.55, 0.),
      init_block_high=(0.05, 0.65, 0.),
      puck_goal_low=(-0.05, 0.55, -0.2),
      puck_goal_high=(0.05, 0.65, 0.2),
      hand_goal_low=(-0.05, 0.55),
      hand_goal_high=(0.05, 0.65),
      fixed_puck_goal=(0.05, 0.6, 0.),
      fixed_hand_goal=(-0.05, 0.6, 0.),
      mocap_low=(-0.1, 0.5, 0.0),
      mocap_high=(0.1, 0.7, 0.5),
      force_puck_in_goal_space=False,
    ):
        self.quick_init(locals())
        self.reward_info = reward_info
        self.randomize_goals = randomize_goals
        self._pos_action_scale = pos_action_scale
        self.hide_goal = hide_goal

        self.init_block_low = np.array(init_block_low)
        self.init_block_high = np.array(init_block_high)
        self.puck_goal_low = np.array(puck_goal_low)
        self.puck_goal_high = np.array(puck_goal_high)
        self.hand_goal_low = np.array(hand_goal_low)
        self.hand_goal_high = np.array(hand_goal_high)
        self.fixed_puck_goal = np.array(fixed_puck_goal)
        self.fixed_hand_goal = np.array(fixed_hand_goal)
        self.mocap_low = np.array(mocap_low)
        self.mocap_high = np.array(mocap_high)
        self.force_puck_in_goal_space = force_puck_in_goal_space

        self._goal_xyxytheta = self.sample_goal_xyxytheta()
        # MultitaskEnv.__init__(self, distance_metric_order=2)
        MujocoEnv.__init__(self, self.model_name, frame_skip=frame_skip)

        self.action_space = Box(
            np.array([-1, -1]),
            np.array([1, 1]),
        )
        self.obs_box = Box(
            np.array([-0.2, 0.5, -0.2, 0.5, -np.pi]),
            np.array([0.2, 0.7, 0.2, 0.7, np.pi]),
        )
        goal_low = np.concatenate((self.hand_goal_low, self.puck_goal_low))
        goal_high = np.concatenate((self.hand_goal_high, self.puck_goal_high))
        self.goal_box = Box(
            goal_low,
            goal_high,
        )
        self.observation_space = Dict([
            ('observation', self.obs_box),
            ('state_observation', self.obs_box),
            ('desired_goal', self.goal_box),
            ('state_desired_goal', self.goal_box),
            ('achieved_goal', self.goal_box),
            ('state_achieved_goal', self.goal_box),
        ])
        # hack for state-based experiments for other envs
        # self.observation_space = Box(
        #     np.array([-0.2, 0.5, -0.2, 0.5, -0.2, 0.5]),
        #     np.array([0.2, 0.7, 0.2, 0.7, 0.2, 0.7]),
        # )
        # self.goal_space = Box(
        #     np.array([-0.2, 0.5, -0.2, 0.5, -0.2, 0.5]),
        #     np.array([0.2, 0.7, 0.2, 0.7, 0.2, 0.7]),
        # )

        self.reset()
        self.reset_mocap_welds()

    @property
    def model_name(self):
        return get_asset_full_path(
            'sawyer_xyz/sawyer_push_and_reach_mocap_goal_hidden_T.xml'
        )

    def viewer_setup(self):
        self.viewer.cam.trackbodyid = 0
        self.viewer.cam.distance = 1.0

        # robot view
        # rotation_angle = 90
        # cam_dist = 1
        # cam_pos = np.array([0, 0.5, 0.2, cam_dist, -45, rotation_angle])

        # 3rd person view
        cam_dist = 0.3
        rotation_angle = 270
        cam_pos = np.array([0, 1.0, 0.5, cam_dist, -45, rotation_angle])

        # top down view
        # cam_dist = 0.2
        # rotation_angle = 0
        # cam_pos = np.array([0, 0, 1.5, cam_dist, -90, rotation_angle])

        for i in range(3):
            self.viewer.cam.lookat[i] = cam_pos[i]
        self.viewer.cam.distance = cam_pos[3]
        self.viewer.cam.elevation = cam_pos[4]
        self.viewer.cam.azimuth = cam_pos[5]
        self.viewer.cam.trackbodyid = -1

    def step(self, a):
        a = np.clip(a, -1, 1)
        mocap_delta_z = 0.06 - self.data.mocap_pos[0, 2]
        new_mocap_action = np.hstack((
            a,
            np.array([mocap_delta_z])
        ))
        self.mocap_set_action(new_mocap_action[:3] * self._pos_action_scale)
        if self.force_puck_in_goal_space:
            puck_pos = self.get_puck_xytheta()
            clipped = np.clip(
                puck_pos,
                self.puck_goal_low,
                self.puck_goal_high
            )
            if not (clipped == puck_pos).all():
                self.set_puck_xytheta(clipped)
        u = np.zeros(7)
        self.do_simulation(u, self.frame_skip)
        obs = self._get_obs()
        # reward = self.compute_reward(obs, u, obs, self._goal_xyxy)
        reward = self.compute_reward(a, obs)
        done = False

        hand_distance = np.linalg.norm(
            self.get_hand_goal_pos() - self.get_endeff_pos()
        )
        puck_distance = np.linalg.norm(
            self.get_puck_goal_xytheta() - self.get_puck_xytheta())
        puck_xy_distance = np.linalg.norm(
            self.get_puck_goal_xytheta()[:2] - self.get_puck_xytheta()[:2])
        puck_theta_distance = np.linalg.norm(
            self.get_puck_goal_xytheta()[2] - self.get_puck_xytheta()[2])
        touch_distance = np.linalg.norm(
            self.get_endeff_pos() - self.get_puck_xytheta())
        info = dict(
            hand_distance=hand_distance,
            puck_distance=puck_distance,
            puck_xy_distance=puck_xy_distance,
            puck_theta_distance=puck_theta_distance,
            touch_distance=touch_distance,
            success=float(hand_distance + puck_distance < 0.06),
        )
        return obs, reward, done, info

    def mocap_set_action(self, action):
        pos_delta = action[None]
        new_mocap_pos = self.data.mocap_pos + pos_delta
        new_mocap_pos[0, :] = np.clip(
            new_mocap_pos[0, :],
            self.mocap_low,
            self.mocap_high
        )
        # new_mocap_pos[0, 0] = np.clip(
        #     new_mocap_pos[0, 0],
        #     -0.1,
        #     0.1,
        # )
        # new_mocap_pos[0, 1] = np.clip(
        #     new_mocap_pos[0, 1],
        #     -0.1 + 0.6,
        #     0.1 + 0.6,
        #     )
        # new_mocap_pos[0, 2] = np.clip(
        #     new_mocap_pos[0, 2],
        #     0,
        #     0.5,
        # )
        self.data.set_mocap_pos('mocap', new_mocap_pos)
        self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))

    def _get_obs(self):
        e = self.get_endeff_pos()[:2]
        b = self.get_puck_xytheta()[:3]
        x = np.concatenate((e, b))
        g = self._goal_xyxytheta

        new_obs = dict(
            observation=x,
            state_observation=x,
            desired_goal=g,
            state_desired_goal=g,
            achieved_goal=x,
            state_achieved_goal=x,
        )

        return new_obs

    def get_puck_xytheta(self):
        xy = self.data.body_xpos[self.puck_id].copy()
        w, x, y, z = self.data.body_xquat[self.puck_id].copy()
        theta = quat_to_theta(x=x, y=y, z=z, w=w)
        return np.array([xy[0], xy[1], theta])

    def get_endeff_pos(self):
        return self.data.body_xpos[self.endeff_id].copy()

    def get_hand_goal_pos(self):
        return self.data.body_xpos[self.hand_goal_id].copy()

    def get_puck_goal_xytheta(self):
        x, y, _ = self.data.body_xpos[self.puck_goal_id].copy()
        qw, qx, qy, qz = self.data.body_xquat[self.puck_goal_id].copy()
        theta = quat_to_theta(x=qx, y=qy, z=qz, w=qw)
        return np.array([x, y, theta])

    @property
    def endeff_id(self):
        return self.model.body_names.index('leftclaw')

    @property
    def puck_id(self):
        return self.model.body_names.index('puck')

    @property
    def puck_goal_id(self):
        return self.model.body_names.index('puck-goal')

    @property
    def hand_goal_id(self):
        return self.model.body_names.index('hand-goal')

    def sample_goal_xyxytheta(self):
        if self.randomize_goals:
            hand = np.random.uniform(self.hand_goal_low, self.hand_goal_high)
            puck = np.random.uniform(self.puck_goal_low, self.puck_goal_high)
        else:
            hand = self.fixed_hand_goal.copy()
            puck = self.fixed_puck_goal.copy()
        return np.hstack((hand, puck))

    # def sample_puck_xy(self):
    #     raise NotImplementedError("Shouldn't you use "
    #                               "SawyerPushAndReachXYEasyEnv? Ask Vitchyr")
    #     pos = np.random.uniform(self.init_block_low, self.init_block_high)
    #     while np.linalg.norm(self.get_endeff_pos()[:2] - pos) < 0.035:
    #         pos = np.random.uniform(self.init_block_low, self.init_block_high)
    #     return pos

    def set_puck_xytheta(self, xytheta):
        qpos = self.data.qpos.flat.copy()
        qvel = self.data.qvel.flat.copy()
        x, y, theta = xytheta
        qw, qx, qy, qz = theta_to_quat(theta)
        qpos[7:10] = np.hstack((x, y, np.array([0.02])))
        qpos[10:14] = qw, qx, qy, qz
        qvel[7:10] = [0, 0, 0]
        qvel[10:14] = [0, 0, 0, 0]
        self.set_state(qpos, qvel)

    def set_goal_xyxytheta(self, xyxytheta):
        self._goal_xyxytheta = xyxytheta
        hand_goal = xyxytheta[:2]
        puck_xy_goal = xyxytheta[2:4]
        puck_theta_goal = xyxytheta[-1]
        puck_quat_goal = theta_to_quat(puck_theta_goal)
        qpos = self.data.qpos.flat.copy()
        qvel = self.data.qvel.flat.copy()
        qpos[14:17] = np.hstack((hand_goal.copy(), np.array([0.02])))
        qvel[14:17] = [0, 0, 0]
        qpos[21:24] = np.hstack((puck_xy_goal.copy(), np.array([0.02])))
        qvel[21:24] = [0, 0, 0]
        qpos[24:28] = np.array(puck_quat_goal).copy()
        self.set_state(qpos, qvel)

    def reset_mocap_welds(self):
        """Resets the mocap welds that we use for actuation."""
        sim = self.sim
        if sim.model.nmocap > 0 and sim.model.eq_data is not None:
            for i in range(sim.model.eq_data.shape[0]):
                if sim.model.eq_type[i] == mujoco_py.const.EQ_WELD:
                    sim.model.eq_data[i, :] = np.array(
                        [0., 0., 0., 1., 0., 0., 0.])
        sim.forward()

    def reset_mocap2body_xpos(self):
        # move mocap to weld joint
        self.data.set_mocap_pos(
            'mocap',
            np.array([self.data.body_xpos[self.endeff_id]]),
        )
        self.data.set_mocap_quat(
            'mocap',
            np.array([self.data.body_xquat[self.endeff_id]]),
        )

    def reset(self):
        velocities = self.data.qvel.copy()
        angles = np.array(self.init_angles)
        self.set_state(angles.flatten(), velocities.flatten())
        for _ in range(10):
            self.data.set_mocap_pos('mocap', self.INIT_HAND_POS)
            self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))
        # set_state resets the goal xy, so we need to explicit set it again
        self._goal_xyxytheta = self.sample_goal_for_rollout()
        self.set_goal_xyxytheta(self._goal_xyxytheta)
        self.set_puck_xytheta(self.sample_puck_xytheta())
        self.reset_mocap_welds()
        return self._get_obs()

    def compute_rewards(self, action, obs, info=None):
        r = -np.linalg.norm(
            obs['state_achieved_goal'] - obs['state_desired_goal'], axis=1)
        return r

    def compute_reward(self, action, obs, info=None):
        r = -np.linalg.norm(
            obs['state_achieved_goal'] - obs['state_desired_goal'])
        return r

    # REPLACING REWARD FN
    # def compute_reward(self, ob, action, next_ob, goal, env_info=None):
    #     hand_xy = next_ob[:2]
    #     puck_xy = next_ob[-2:]
    #     hand_goal_xy = goal[:2]
    #     puck_goal_xy = goal[-2:]
    #     hand_dist = np.linalg.norm(hand_xy - hand_goal_xy)
    #     puck_dist = np.linalg.norm(puck_xy - puck_goal_xy)
    #     if not self.reward_info or self.reward_info["type"] == "euclidean":
    #         r = - hand_dist - puck_dist
    #     elif self.reward_info["type"] == "state_distance":
    #         r = -np.linalg.norm(next_ob - goal)
    #     elif self.reward_info["type"] == "hand_only":
    #         r = - hand_dist
    #     elif self.reward_info["type"] == "puck_only":
    #         r = - puck_dist
    #     elif self.reward_info["type"] == "sparse":
    #         t = self.reward_info["threshold"]
    #         r = float(
    #             hand_dist + puck_dist < t
    #         ) - 1
    #     else:
    #         raise NotImplementedError("Invalid/no reward type.")
    #     return r

    def compute_her_reward_np(self, ob, action, next_ob, goal, env_info=None):
        return self.compute_reward(ob, action, next_ob, goal, env_info=env_info)

    # @property
    # def init_angles(self):
    #     return [
    #         1.06139477e+00, -6.93988797e-01, 3.76729934e-01, 1.78410587e+00,
    #         - 5.36763074e-01, 5.88122189e-01, 3.51531533e+00,
    #         0.05, 0.55, 0.02,
    #         1, 0, 0, 0,
    #         0, 0.6, 0.02,
    #         1, 0, 1, 0,
    #         0, 0.6, 0.02,
    #         1, 0, 1, 0,
    #     ]
    @property
    def init_angles(self):
        return [1.78026069e+00, - 6.84415781e-01, - 1.54549231e-01,
                2.30672090e+00, 1.93111471e+00, 1.27854012e-01,
                1.49353907e+00, 1.80196716e-03, 7.40415706e-01,
                2.09895360e-02, 1, 0,
                0, 0, - 3.62518873e-02,
                6.13435141e-01, 2.09686080e-02, 7.07106781e-01,
                1.48979724e-14, 7.07106781e-01, - 1.48999170e-14,
                0, 0.6, 0.02,
                1, 0, 1, 0,
                ]

    def get_diagnostics(self, paths, prefix=""):
        statistics = OrderedDict()
        for stat_name in [
            'hand_distance',
            'puck_distance',
            'puck_xy_distance',
            'puck_theta_distance',
            'touch_distance',
            'success',
        ]:
            stat_name = stat_name
            stat = get_stat_in_paths(paths, 'env_infos', stat_name)
            statistics.update(create_stats_ordered_dict(
                '%s%s' % (prefix, stat_name),
                stat,
                always_show_all_stats=True,
            ))
            statistics.update(create_stats_ordered_dict(
                'Final %s%s' % (prefix, stat_name),
                [s[-1] for s in stat],
                always_show_all_stats=True,
            ))
        return statistics

    """
    Multitask functions
    """

    @property
    def goal_dim(self) -> int:
        return 4

    def sample_goals(self, batch_size):
        # goals = np.zeros((batch_size, self.goal_box.low.size))
        # for b in range(batch_size):
        #     goals[b, :] = self.sample_goal_xyxy()
        goals = np.random.uniform(
            self.goal_box.low,
            self.goal_box.high,
            size=(batch_size, self.goal_box.low.size),
        )
        return {
            'desired_goal': goals,
            'state_desired_goal': goals,
        }

    def sample_goal_for_rollout(self):
        g = self.sample_goal_xyxytheta()
        return g

    # OLD SET GOAL
    # def set_goal(self, goal):
    #     MultitaskEnv.set_goal(self, goal)
    #     self.set_goal_xyxy(goal)
    #     # hack for VAE
    #     self.set_to_goal(goal)

    def get_goal(self):
        return {
            'desired_goal': self._goal_xyxytheta,
            'state_desired_goal': self._goal_xyxytheta,
        }

    def set_goal(self, goal):
        state_goal = goal['state_desired_goal']
        self.set_goal_xyxytheta(state_goal)

    def set_to_goal(self, goal):
        state_goal = goal['state_desired_goal']
        self.set_hand_xy(state_goal[:2])
        self.set_puck_xytheta(state_goal[-3:])

    def convert_obs_to_goals(self, obs):
        return obs

    def set_hand_xy(self, xy):
        for _ in range(10):
            self.data.set_mocap_pos('mocap', np.array([xy[0], xy[1], 0.02]))
            self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))
            u = np.zeros(7)
            self.do_simulation(u, self.frame_skip)

    def get_env_state(self):
        joint_state = self.sim.get_state()
        mocap_state = self.data.mocap_pos, self.data.mocap_quat
        state = joint_state, mocap_state
        return copy.deepcopy(state)

    def set_env_state(self, state):
        joint_state, mocap_state = state
        self.sim.set_state(joint_state)
        mocap_pos, mocap_quat = mocap_state
        self.data.set_mocap_pos('mocap', mocap_pos)
        self.data.set_mocap_quat('mocap', mocap_quat)
        self.sim.forward()


class SawyerPushAndReachTXYEasyEnv(SawyerPushAndReachTXYEnv):
    """
    Always start the block in the same position, and use a 40x20 puck space
    """

    def __init__(
      self,
      **kwargs
    ):
        self.quick_init(locals())
        default_kwargs = dict(
            puck_goal_low=(-0.2, 0.5),
            puck_goal_high=(0.2, 0.7),
        )
        actual_kwargs = {
            **default_kwargs,
            **kwargs
        }
        SawyerPushAndReachTXYEnv.__init__(
            self,
            **actual_kwargs
        )

    def sample_puck_xytheta(self):
        theta = (np.random.random() - 0.5) * np.pi
        return np.array([0, 0.6, theta])

# class SawyerPushAndReachTXYHarderEnv(SawyerPushAndReachTXYEnv):
#     """
#     Fixed initial position, all spaces are 40cm x 20cm
#     """
#
#     def __init__(
#       self,
#       **kwargs
#     ):
#         self.quick_init(locals())
#         SawyerPushAndReachTXYEnv.__init__(
#             self,
#             hand_goal_low=(-0.2, 0.5),
#             hand_goal_high=(0.2, 0.7),
#             puck_goal_low=(-0.2, 0.5),
#             puck_goal_high=(0.2, 0.7),
#             mocap_low=(-0.2, 0.5, 0.0),
#             mocap_high=(0.2, 0.7, 0.5),
#             **kwargs
#         )
#
#     def sample_puck_xy(self):
#         return np.array([0, 0.6])
