import logging
import subprocess
import sys

import numpy as np

import traci
import sumolib
from rllab.core.serializable import Serializable
from rllab.envs.base import Env
from rllab.envs.base import Step

from cistar.core.util import ensure_dir

"""
This file provides the interface for controlling a SUMO simulation. Using the environment class, you can
start sumo, provide a scenario to specify a configuration and controllers, perform simulation steps, and
reset the simulation to an initial configuration.

SumoEnv must be be Serializable to allow for pickling of the policy.

This class cannot be used as is: you must extend it to implement an action applicator method, and
properties to define the MDP if you choose to use it with RLLab.

"""

COLORS = [(255, 0, 0, 0), (0, 255, 0, 0), (0, 0, 255, 0), (255, 255, 0, 0), (0, 255, 255, 0), (255, 0, 255, 0),
          (255, 255, 255, 0)]


class SumoEnvironment(Env, Serializable):
    def __init__(self, env_params, sumo_binary, sumo_params, scenario):
        """[summary]
        
        [description]
        
        Arguments:
            env_params {dictionary} -- [description]
            sumo_binary {string} -- Either "sumo" or "sumo-gui"
            sumo_params {dictionary} -- {"port": {integer} connection to SUMO,
                            "timestep": {float} default=0.01s, SUMO default=1.0s}
            scenario {Scenario} -- @see Scenario; abstraction of the SUMO XML files
                                    which specify the vehicles placed on the net
        
        Raises:
            ValueError -- Raised if a SUMO port not provided
        """
        Serializable.quick_init(self, locals())

        self.env_params = env_params
        self.sumo_binary = sumo_binary
        self.scenario = scenario
        self.sumo_params = sumo_params
        self.timer = 0  # Represents number of steps taken
        self.vehicles = {}  # Vehicles: Key = Vehicle ID, Value = Dictionary describing the vehicle
        # Initial state: Key = Vehicle ID, Entry = (type_id, route_id, lane_index, lane_pos, speed, pos)
        self.initial_state = {}
        self.ids = []
        self.controlled_ids, self.rl_ids = [], []
        self.state = None
        self.obs_var_labels = []

        # SUMO Params
        if "port" not in sumo_params:
            self.port = sumolib.miscutils.getFreeSocketPort()
        else:
            self.port = sumo_params['port']

        if "time_step" in sumo_params:
            self.time_step = sumo_params["time_step"]
        else:
            self.time_step = 0.01  # 0.01 = default time step for our research

        if "emission_path" in sumo_params:
            data_folder = sumo_params['emission_path']
            ensure_dir(data_folder)
            self.emission_out = data_folder + "emission.xml"
        else:
            self.emission_out = None

        # Env Params
        if 'fail-safe' in env_params:
            self.fail_safe = env_params['fail-safe']
        else:
            self.fail_safe = 'instantaneous'

        self.start_sumo()
        self.setup_initial_state()

    def restart_sumo(self, sumo_params, sumo_binary=None):
        self.traci_connection.close(False)
        if sumo_binary:
            self.sumo_binary = sumo_binary
        if "port" in sumo_params:
            self.port = sumo_params['port']

        if "emission_path" in sumo_params:
            data_folder = sumo_params['emission_path']
            ensure_dir(data_folder)
            self.emission_out = data_folder + "emission.xml"

        self.start_sumo()
        self.setup_initial_state()

    def start_sumo(self):
        logging.info(" Starting SUMO on port " + str(self.port))
        logging.debug(" Cfg file " + str(self.scenario.cfg))
        logging.debug(" Emission file: " + str(self.emission_out))
        logging.debug(" Time step: " + str(self.time_step))

        # TODO: find a better way to do this
        # Opening the I/O thread to SUMO
        cfg_file = self.scenario.cfg
        if "mode" in self.env_params and self.env_params["mode"] == "ec2":
            cfg_file = "/root/code/rllab/" + cfg_file

        sumo_call = [self.sumo_binary,
                     "-c", cfg_file,
                     "--remote-port", str(self.port),
                     "--step-length", str(self.time_step)]
        print("Traci on port: ", self.port)
        if self.emission_out:
            sumo_call.append("--emission-output")
            sumo_call.append(self.emission_out)

        subprocess.Popen(sumo_call, stdout=sys.stdout, stderr=sys.stderr)

        logging.debug(" Initializing TraCI on port " + str(self.port) + "!")

        self.traci_connection = traci.connect(self.port)

        self.traci_connection.simulationStep()


        # Density = num vehicles / length (in meters)
        # so density in vehicles/km would be 1000 * self.density
        self.density = self.scenario.num_vehicles / self.scenario.net_params['length']

    def setup_initial_state(self):
        """
        Store initial state so that simulation can be reset at the end.
        TODO: Make traci calls as bulk as possible
        Initial state is a dictionary: key = vehicle IDs, value = state describing car
        """

        self.ids = self.traci_connection.vehicle.getIDList()
        self.controlled_ids.clear()
        self.rl_ids.clear()
        self.vehicles.clear()
        for i in self.ids:
            if self.traci_connection.vehicle.getTypeID(i) == "rl":
                self.rl_ids.append(i)
            else:
                self.controlled_ids.append(i)

        for veh_id in self.ids:
            vehicle = dict()
            vehicle["id"] = veh_id
            veh_type = self.traci_connection.vehicle.getTypeID(veh_id)
            vehicle["type"] = veh_type
            vehicle["edge"] = self.traci_connection.vehicle.getRoadID(veh_id)
            vehicle["position"] = self.traci_connection.vehicle.getLanePosition(veh_id)
            vehicle["lane"] = self.traci_connection.vehicle.getLaneIndex(veh_id)
            vehicle["speed"] = self.traci_connection.vehicle.getSpeed(veh_id)
            vehicle["length"] = self.traci_connection.vehicle.getLength(veh_id)
            vehicle["max_speed"] = self.traci_connection.vehicle.getMaxSpeed(veh_id)
            vehicle["distance"] = self.traci_connection.vehicle.getDistance(veh_id)

            # implement flexibility in controller
            controller_params = self.scenario.type_params[veh_type][1]
            vehicle['controller'] = controller_params[0](veh_id=veh_id, **controller_params[1])

            # initializes lane-changing controller
            lane_changer_params = self.scenario.type_params[veh_type][2]
            if lane_changer_params is not None:
                vehicle['lane_changer'] = lane_changer_params[0](veh_id = veh_id, **lane_changer_params[1])
            else:
                vehicle['lane_changer'] = None

            self.vehicles[veh_id] = vehicle
            self.traci_connection.vehicle.setSpeedMode(veh_id, 0)
            if veh_id in self.rl_ids:
                self.traci_connection.vehicle.setLaneChangeMode(veh_id, 768)

            # Saving initial state
            route_id = self.traci_connection.vehicle.getRouteID(veh_id)
            pos = self.traci_connection.vehicle.getPosition(veh_id)

            self.initial_state[veh_id] = (vehicle["type"], route_id, vehicle["lane"],
                                          vehicle["position"], vehicle["speed"], pos)

    def step(self, rl_actions):
        """
        Run one timestep of the environment's dynamics. "Self-driving cars" will
        step forward based on rl_actions, provided by the RL algorithm. Other cars
        will step forward based on their car following model. When end of episode
        is reached, reset() should be called to reset the environment's internal state.
        Input
        -----
        rl_actions : an action provided by the rl algorithm
        Outputs
        -------
        (observation, reward, done, info)
        observation : agent's observation of the current environment
        reward [Float] : amount of reward due to the previous action
        done : a boolean, indicating whether the episode has ended
        info : a dictionary containing other diagnostic information from the previous action
        """
        self.timer += 1

        for veh_id in self.controlled_ids:
            action = self.vehicles[veh_id]['controller'].get_action(self)
            if self.fail_safe == 'instantaneous':
                safe_action = self.vehicles[veh_id]['controller'].get_safe_action_instantaneous(self, action)
            elif self.fail_safe == 'eugene':
                safe_action = self.vehicles[veh_id]['controller'].get_safe_action(self, action)
            else:
                safe_action = action
            if safe_action is None:
                print('safe action is None')
                pass
            else:
                self.apply_action(veh_id, action=safe_action)

            if self.timer % 100 == 0:
                if self.vehicles[veh_id]['lane_changer']:
                    newlane = self.vehicles[veh_id]['lane_changer'].get_action(self)
                    self.traci_connection.vehicle.changeLane(veh_id, newlane, 10000)

        self.apply_rl_actions(rl_actions)

        self.additional_command()

        self.traci_connection.simulationStep()

        speeds = []
        for veh_id in self.ids:
            self.vehicles[veh_id]["type"] = self.traci_connection.vehicle.getTypeID(veh_id)
            this_edge = self.traci_connection.vehicle.getRoadID(veh_id)
            if this_edge is None:
                print('Null edge for vehicle:', veh_id)
            else:
                self.vehicles[veh_id]["edge"] = this_edge
            self.vehicles[veh_id]["position"] = self.traci_connection.vehicle.getLanePosition(veh_id)
            self.vehicles[veh_id]["lane"] = self.traci_connection.vehicle.getLaneIndex(veh_id)
            veh_speed = self.traci_connection.vehicle.getSpeed(veh_id)
            self.vehicles[veh_id]["speed"] = veh_speed
            speeds.append(veh_speed)
            self.vehicles[veh_id]["fuel"] = self.traci_connection.vehicle.getFuelConsumption(veh_id)
            self.vehicles[veh_id]["distance"] = self.traci_connection.vehicle.getDistance(veh_id)

        # if round(self.timer) == round(self.timer, 3):
        #     mean_speed = np.mean(speeds)
        #     print('time:', round(self.timer), 's; avg speed:', mean_speed, 'm/s; flow:', mean_speed * self.density * 3600, '(cars/km)')
        #     print('')

        # TODO: Can self._state be initialized, saved and updated so that we can exploit numpy speed
        self.state = self.getState()
        reward = self.compute_reward(self.state, rl_actions)
        # TODO: Allow for partial observability
        next_observation = np.copy(self.state)

        if self.traci_connection.simulation.getEndingTeleportNumber() != 0:
            # Crash has occurred, end rollout
            if self.fail_safe == "None":
                return Step(observation=next_observation, reward=reward, done=True)
            else:
                print("Crash has occurred! Check failsafes!")
        else:
            return Step(observation=next_observation, reward=reward, done=False)

    def reset(self):
        """
        Resets the state of the environment, returning an initial observation.
        Outputs
        -------
        observation : the initial observation of the space. (Initial reward is assumed to be 0.)
        """
        color = COLORS[np.random.choice(len(COLORS))]
        for veh_id in self.ids:
            type_id, route_id, lane_index, lane_pos, speed, pos = self.initial_state[veh_id]

            # clears controller acceleration queue
            if not self.vehicles[veh_id]['type'] == 'rl':
                self.vehicles[veh_id]['controller'].reset_delay(self)

            self.traci_connection.vehicle.remove(veh_id)
            self.traci_connection.vehicle.addFull(veh_id, route_id, typeID=str(type_id), departLane=str(lane_index),
                                  departPos=str(lane_pos), departSpeed=str(speed))
            self.traci_connection.vehicle.setColor(veh_id, color)

        self.traci_connection.simulationStep()

        # TODO: Replace these traci calls with initial_state accesses
        for veh_id in self.ids:
            self.traci_connection.vehicle.setSpeedMode(veh_id, 0)
            self.vehicles[veh_id]["type"] = self.traci_connection.vehicle.getTypeID(veh_id)
            self.vehicles[veh_id]["edge"] = self.traci_connection.vehicle.getRoadID(veh_id)
            self.vehicles[veh_id]["position"] = self.traci_connection.vehicle.getLanePosition(veh_id)
            self.vehicles[veh_id]["lane"] = self.traci_connection.vehicle.getLaneIndex(veh_id)
            self.vehicles[veh_id]["speed"] = self.traci_connection.vehicle.getSpeed(veh_id)
            self.vehicles[veh_id]["fuel"] = self.traci_connection.vehicle.getFuelConsumption(veh_id)
            self.vehicles[veh_id]["distance"] = self.traci_connection.vehicle.getDistance(veh_id)

        self.state = self.getState()
        observation = np.copy(self.state)

        return observation

    def additional_command(self):
        pass

    def apply_rl_actions(self, rl_actions):
        for index, veh_id in enumerate(self.rl_ids):
            action = rl_actions[index]
            if self.fail_safe == 'instantaneous':
                safe_action = self.vehicles[veh_id]['controller'].get_safe_action_instantaneous(self, action)
            elif self.fail_safe == 'eugene':
                safe_action = self.vehicles[veh_id]['controller'].get_safe_action(self, action)
            else:
                safe_action = action
            self.apply_action(veh_id, action=safe_action)

    def apply_action(self, veh_id, action):
        """
        :param veh_id: {string}
        :param action: as specified by a controller or rllab, usually a scalar value
        """
        raise NotImplementedError

    def getState(self):
        """
        returns the state of the simulation, dependent on the experiment/environment
        :return:
        """
        raise NotImplementedError

    @property
    def action_space(self):
        """
        Returns an action space object
        """
        raise NotImplementedError

    @property
    def observation_space(self):
        """
        Returns an observation space object
        """
        raise NotImplementedError

    def compute_reward(self, state, actions):
        """Reward function for RL.
        
        Arguments:
            state {Array-type} -- State of all the vehicles in the simulation
        """
        raise NotImplementedError

    def render(self):
        """Description of the state for when verbose mode is on."""
        raise NotImplementedError

    def terminate(self):
        """Closes the TraCI I/O connection. Should be done at end of every experiment.
        Must be in Environment because the environment opens the TraCI connection.
        """
        self.traci_connection.close()

    def close(self):
        self.terminate()
