import numpy as np
import math
from cereal import log
from common.numpy_fast import interp
from common.params import Params
from common.realtime import sec_since_boot
from selfdrive.config import Conversions as CV
from selfdrive.controls.lib.lane_planner import TRAJECTORY_SIZE
from selfdrive.controls.lib.drive_helpers import V_CRUISE_MAX


_MIN_V = 5.6  # Do not operate under 20km/h
_MIN_V_SOLUTION = 8.33  # m/s, Minimum speed to provide as solution.

_ENTERING_PRED_LAT_ACC_TH = 1.3  # Predicted Lat Acc threshold to trigger entering turn state.
_ABORT_ENTERING_PRED_LAT_ACC_TH = 1.1  # Predicted Lat Acc threshold to abort entering state if speed drops.

_TURNING_LAT_ACC_TH = 1.5  # Lat Acc threshold to trigger turning turn state.

_LEAVING_LAT_ACC_TH = 1.3  # Lat Acc threshold to trigger leaving turn state.
_FINISH_LAT_ACC_TH = 1.1  # Lat Acc threshold to trigger end of turn cycle.

_EVAL_STEP = 5.  # mts. Resolution of the curvature evaluation.
_EVAL_START = 20.  # mts. Distance ahead where to start evaluating vision curvature.
_EVAL_LENGHT = 150.  # mts. Distance ahead where to stop evaluating vision curvature.
_EVAL_RANGE = np.arange(_EVAL_START, _EVAL_LENGHT, _EVAL_STEP)

_A_LAT_REG_MAX = 2.  # Maximum lateral acceleration

# Lookup table for the minimum smooth deceleration during the ENTERING state
# depending on the actual maximum absolute lateral acceleration predicted on the turn ahead.
_ENTERING_SMOOTH_DECEL_V = [-0.3, -1.]  # min decel value allowed on ENTERING state
_ENTERING_SMOOTH_DECEL_BP = [1., 3]  # absolute value of lat acc ahead

# Lookup table for the minimum acceleration for the TURNING state
# depending on the current lateral acceleration of the vehicle.
_MIN_TURNING_ACC_V = [-0.1, -0.2, -0.4]  # acc value
_MIN_TURNING_ACC_BP = [1., 2., 3.]  # absolute value of current lat acc

_MIN_LANE_PROB = 0.6  # Minimum lanes probability to allow curvature prediction based on lanes.

_DEBUG = False


def _debug(msg):
  if not _DEBUG:
    return
  print(msg)


VisionTurnControllerState = log.LongitudinalPlan.VisionTurnControllerState


def eval_curvature(poly, x_vals):
  """
  This function returns a vector with the curvature based on path defined by `poly`
  evaluated on distance vector `x_vals`
  """
  # https://en.wikipedia.org/wiki/Curvature#  Local_expressions
  def curvature(x):
    a = abs(2 * poly[1] + 6 * poly[0] * x) / (1 + (3 * poly[0] * x**2 + 2 * poly[1] * x + poly[2])**2)**(1.5)
    return a

  return np.vectorize(curvature)(x_vals)


def eval_lat_acc(v_ego, x_curv):
  """
  This function returns a vector with the lateral acceleration based
  for the provided speed `v_ego` evaluated over curvature vector `x_curv`
  """

  def lat_acc(curv):
    a = v_ego**2 * curv
    return a

  return np.vectorize(lat_acc)(x_curv)


def _description_for_state(turn_controller_state):
  if turn_controller_state == VisionTurnControllerState.disabled:
    return 'DISABLED'
  if turn_controller_state == VisionTurnControllerState.entering:
    return 'ENTERING'
  if turn_controller_state == VisionTurnControllerState.turning:
    return 'TURNING'
  if turn_controller_state == VisionTurnControllerState.leaving:
    return 'LEAVING'


class VisionTurnController():
  def __init__(self, CP):
    self._params = Params()
    self._CP = CP
    self._op_enabled = False
    self._gas_pressed = False
    self._is_enabled = self._params.get_bool("TurnVisionControl")
    self._last_params_update = 0.0
    self._v_cruise_setpoint = 0.0
    self._v_ego = 0.0
    self._state = VisionTurnControllerState.disabled

    self._reset()

  @property
  def v_turn(self):
    return float(self._v_turn) if self.state != VisionTurnControllerState.disabled else self._v_cruise_setpoint

  @property
  def acc_limits(self):
    return self._acc_limits

  @property
  def state(self):
    return self._state

  @property
  def is_active(self):
    return self._state != VisionTurnControllerState.disabled

  @state.setter
  def state(self, value):
    if value != self._state:
      _debug(f'TVC: TurnVisionController state: {_description_for_state(value)}')
      if value == VisionTurnControllerState.disabled:
        self._reset()
    self._state = value

  def _reset(self):
    self._current_lat_acc = 0.0
    self._max_v_for_current_curvature = 0.0
    self._max_pred_lat_acc = 0.0
    self._v_overshoot_distance = 200.0
    self._v_overshoot = 0.0
    self._lat_acc_overshoot_ahead = False

  def _update_params(self):
    time = sec_since_boot()
    if time > self._last_params_update + 5.0:
      self._is_enabled = self._params.get_bool("TurnVisionControl")
      self._last_params_update = time

  def _update_calculations(self, sm):
    # Get path polynomial aproximation for curvature estimation from model data.
    path_poly = None
    model_data = sm['modelV2'] if sm.valid.get('modelV2', False) else None
    lat_planner_data = sm['lateralPlan'] if sm.valid.get('lateralPlan', False) else None

    # 1. When the probability of lanes is good enough, compute polynomial from lanes as they are way more stable
    # on current mode than drving path.
    if model_data is not None and len(model_data.laneLines) == 4 and len(model_data.laneLines[0].t) == TRAJECTORY_SIZE:
      ll_x = model_data.laneLines[1].x  # left and right ll x is the same
      lll_y = np.array(model_data.laneLines[1].y)
      rll_y = np.array(model_data.laneLines[2].y)
      l_prob = model_data.laneLineProbs[1]
      r_prob = model_data.laneLineProbs[2]
      lll_std = model_data.laneLineStds[1]
      rll_std = model_data.laneLineStds[2]

      # Reduce reliance on lanelines that are too far apart or will be in a few seconds
      width_pts = rll_y - lll_y
      prob_mods = []
      for t_check in [0.0, 1.5, 3.0]:
        width_at_t = interp(t_check * (self._v_ego + 7), ll_x, width_pts)
        prob_mods.append(interp(width_at_t, [4.0, 5.0], [1.0, 0.0]))
      mod = min(prob_mods)
      l_prob *= mod
      r_prob *= mod

      # Reduce reliance on uncertain lanelines
      l_std_mod = interp(lll_std, [.15, .3], [1.0, 0.0])
      r_std_mod = interp(rll_std, [.15, .3], [1.0, 0.0])
      l_prob *= l_std_mod
      r_prob *= r_std_mod

      # Find path from lanes as the average center lane only if min probability on both lanes is above threshold.
      if l_prob > _MIN_LANE_PROB and r_prob > _MIN_LANE_PROB:
        c_y = width_pts / 2 + lll_y
        path_poly = np.polyfit(ll_x, c_y, 3)

    # 2. If not polynomial derived from lanes, then derive it from compensated driving path with lanes as
    # provided by `lateralPlanner`.
    if path_poly is None and lat_planner_data is not None and len(lat_planner_data.dPathWLinesX) > 0 \
       and lat_planner_data.dPathWLinesX[0] > 0:
      path_poly = np.polyfit(lat_planner_data.dPathWLinesX, lat_planner_data.dPathWLinesY, 3)

    # 3. If no polynomial derived from lanes or driving path, then provide a straight line poly.
    if path_poly is None:
      path_poly = np.array([0., 0., 0., 0.])

    current_curvature = abs(
        sm['carState'].steeringAngleDeg * CV.DEG_TO_RAD / (self._CP.steerRatio * self._CP.wheelbase))
    self._current_lat_acc = current_curvature * self._v_ego**2
    self._max_v_for_current_curvature = math.sqrt(_A_LAT_REG_MAX / current_curvature) if current_curvature > 0 \
        else V_CRUISE_MAX * CV.KPH_TO_MS

    pred_curvatures = eval_curvature(path_poly, _EVAL_RANGE)
    max_pred_curvature = np.amax(pred_curvatures)
    self._max_pred_lat_acc = self._v_ego**2 * max_pred_curvature

    max_curvature_for_vego = _A_LAT_REG_MAX / max(self._v_ego, 0.1)**2
    lat_acc_overshoot_idxs = np.nonzero(pred_curvatures >= max_curvature_for_vego)[0]
    self._lat_acc_overshoot_ahead = len(lat_acc_overshoot_idxs) > 0

    if self._lat_acc_overshoot_ahead:
      self._v_overshoot = min(math.sqrt(_A_LAT_REG_MAX / max_pred_curvature), self._v_cruise_setpoint)
      self._v_overshoot_distance = max(lat_acc_overshoot_idxs[0] * _EVAL_STEP + _EVAL_START, _EVAL_STEP)
      _debug(f'TVC: High LatAcc. Dist: {self._v_overshoot_distance:.2f}, v: {self._v_overshoot * CV.MS_TO_KPH:.2f}')

  def _state_transition(self):
    # In any case, if system is disabled or the feature is disabeld or gas is pressed, disable.
    if not self._op_enabled or not self._is_enabled or self._gas_pressed:
      self.state = VisionTurnControllerState.disabled
      return

    # DISABLED
    if self.state == VisionTurnControllerState.disabled:
      # Do not enter a turn control cycle if speed is low.
      if self._v_ego <= _MIN_V:
        pass
      # If substantial lateral acceleration is predicted ahead, then move to Entering turn state.
      elif self._max_pred_lat_acc >= _ENTERING_PRED_LAT_ACC_TH:
        self.state = VisionTurnControllerState.entering
    # ENTERING
    elif self.state == VisionTurnControllerState.entering:
      # Transition to Turning if current lateral acceleration is over the threshold.
      if self._current_lat_acc >= _TURNING_LAT_ACC_TH:
        self.state = VisionTurnControllerState.turning
      # Abort if the predicted lateral acceleration drops
      elif self._max_pred_lat_acc < _ABORT_ENTERING_PRED_LAT_ACC_TH:
        self.state = VisionTurnControllerState.disabled
    # TURNING
    elif self.state == VisionTurnControllerState.turning:
      # Transition to Leaving if current lateral acceleration drops drops below threshold.
      if self._current_lat_acc <= _LEAVING_LAT_ACC_TH:
        self.state = VisionTurnControllerState.leaving
    # LEAVING
    elif self.state == VisionTurnControllerState.leaving:
      # Transition back to Turning if current lateral acceleration goes back over the threshold.
      if self._current_lat_acc >= _TURNING_LAT_ACC_TH:
        self.state = VisionTurnControllerState.turning
      # Finish if current lateral acceleration goes below threshold.
      elif self._current_lat_acc < _FINISH_LAT_ACC_TH:
        self.state = VisionTurnControllerState.disabled

  def _update_solution(self):
    # Calculate acceleration limits and turn speed based on turn state.
    acc_limits = self._acc_limits
    v_turn = self._v_cruise_setpoint

    # DISABLED
    if self.state == VisionTurnControllerState.disabled:
      pass
    # ENTERING
    elif self.state == VisionTurnControllerState.entering:
      min_acc = interp(self._max_pred_lat_acc, _ENTERING_SMOOTH_DECEL_BP, _ENTERING_SMOOTH_DECEL_V)
      if not self._lat_acc_overshoot_ahead:
        # when not overshooting, calculate v_turn as the speed at the prediction horizon when following
        # the smooth deceleration.
        try:
          v_turn = math.sqrt(self._v_ego**2 + 2 * min_acc * _EVAL_RANGE[-1])
        except ValueError:
          v_turn = _MIN_V_SOLUTION
      else:
        # when overshooting, target the overshoot speed and adapt limits to allow braking as to achieve
        # the overshoot speed at the overshoot distance.
        v_turn = self._v_overshoot
        min_acc = min((self._v_overshoot**2 - self._v_ego**2) / (2 * self._v_overshoot_distance), min_acc)
      acc_limits[0] = min(min_acc, acc_limits[0])
      _debug(f'TVC Entering: Overshooting: {self._lat_acc_overshoot_ahead}')
      _debug(f'    Decel: {acc_limits[0]:.2f}, target v: {v_turn * CV.MS_TO_KPH}')
    # TURNING
    elif self.state == VisionTurnControllerState.turning:
      v_turn = min(self._v_ego, self._max_v_for_current_curvature)
      acc_limits[0] = interp(self._current_lat_acc, _MIN_TURNING_ACC_BP, _MIN_TURNING_ACC_V)
    # LEAVING
    elif self.state == VisionTurnControllerState.leaving:
      v_turn = min(self._v_ego, self._v_cruise_setpoint)

    # update solution values.
    self._v_turn = max(v_turn, _MIN_V_SOLUTION)
    self._acc_limits = acc_limits

  def update(self, enabled, v_ego, a_ego, v_cruise_setpoint, acc_limits, sm):
    self._op_enabled = enabled
    self._gas_pressed = sm['carState'].gasPressed
    self._v_ego = v_ego
    self._a_ego = a_ego
    self._v_cruise_setpoint = v_cruise_setpoint
    self._acc_limits = acc_limits

    self._update_params()
    self._update_calculations(sm)
    self._state_transition()
    self._update_solution()
