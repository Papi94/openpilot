from collections import namedtuple
from cereal import car
from common.realtime import DT_CTRL
from selfdrive.controls.lib.drive_helpers import rate_limit
from common.numpy_fast import clip, interp
from selfdrive.car import create_gas_command
from selfdrive.car.honda import hondacan
from selfdrive.car.honda.values import OLD_NIDEC_LONG_CONTROL, CruiseButtons, CAR, VISUAL_HUD, HONDA_BOSCH, CarControllerParams
from opendbc.can.packer import CANPacker

VisualAlert = car.CarControl.HUDControl.VisualAlert


#TODO not clear this does anything useful
def actuator_hystereses(brake, braking, brake_steady, v_ego, car_fingerprint):
  # hyst params
  brake_hyst_on = 0.02     # to activate brakes exceed this value
  brake_hyst_off = 0.005                     # to deactivate brakes below this value
  brake_hyst_gap = 0.01                      # don't change brake command for small oscillations within this value

  #*** hysteresis logic to avoid brake blinking. go above 0.1 to trigger
  if (brake < brake_hyst_on and not braking) or brake < brake_hyst_off:
    brake = 0.
  braking = brake > 0.

  # for small brake oscillations within brake_hyst_gap, don't change the brake command
  if brake == 0.:
    brake_steady = 0.
  elif brake > brake_steady + brake_hyst_gap:
    brake_steady = brake - brake_hyst_gap
  elif brake < brake_steady - brake_hyst_gap:
    brake_steady = brake + brake_hyst_gap
  brake = brake_steady

  if (car_fingerprint in (CAR.ACURA_ILX, CAR.CRV, CAR.CRV_EU)) and brake > 0.0:
    brake += 0.15

  return brake, braking, brake_steady


def brake_pump_hysteresis(apply_brake, apply_brake_last, last_pump_ts, ts):
  pump_on = False

  # reset pump timer if:
  # - there is an increment in brake request
  # - we are applying steady state brakes and we haven't been running the pump
  #   for more than 20s (to prevent pressure bleeding)
  if apply_brake > apply_brake_last or (ts - last_pump_ts > 20. and apply_brake > 0):
    last_pump_ts = ts

  # once the pump is on, run it for at least 0.2s
  if ts - last_pump_ts < 0.2 and apply_brake > 0:
    pump_on = True

  return pump_on, last_pump_ts


def process_hud_alert(hud_alert):
  # initialize to no alert
  fcw_display = 0
  steer_required = 0
  acc_alert = 0

  # priority is: FCW, steer required, all others
  if hud_alert == VisualAlert.fcw:
    fcw_display = VISUAL_HUD[hud_alert.raw]
  elif hud_alert in [VisualAlert.steerRequired, VisualAlert.ldw]:
    steer_required = VISUAL_HUD[hud_alert.raw]
  else:
    acc_alert = VISUAL_HUD[hud_alert.raw]

  return fcw_display, steer_required, acc_alert


HUDData = namedtuple("HUDData",
                     ["pcm_accel", "v_cruise", "car",
                     "lanes", "fcw", "acc_alert", "steer_required"])


class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.braking = False
    self.brake_steady = 0.
    self.brake_last = 0.
    self.apply_brake_last = 0
    self.last_pump_ts = 0.
    self.packer = CANPacker(dbc_name)
    self.new_radar_config = False

    self.params = CarControllerParams(CP)

  def update(self, enabled, CS, frame, actuators,
             pcm_speed, pcm_override, pcm_cancel_cmd, pcm_accel,
             hud_v_cruise, hud_show_lanes, hud_show_car, hud_alert):

    P = self.params

    # *** apply brake hysteresis ***
    brake, self.braking, self.brake_steady = actuator_hystereses(actuators.brake, self.braking, self.brake_steady, CS.out.vEgo, CS.CP.carFingerprint)

    # *** no output if not enabled ***
    if not enabled and CS.out.cruiseState.enabled:
      # send pcm acc cancel cmd if drive is disabled but pcm is still on, or if the system can't be activated
      pcm_cancel_cmd = True

    # Never send cancel command if we never enter cruise state (no cruise if pedal)
    # Cancel cmd causes brakes to release at a standstill causing grinding
    pcm_cancel_cmd = pcm_cancel_cmd and CS.CP.pcmCruise

    # *** rate limit after the enable check ***
    self.brake_last = rate_limit(brake, self.brake_last, -2., DT_CTRL)

    # vehicle hud display, wait for one update from 10Hz 0x304 msg
    if hud_show_lanes:
      hud_lanes = 1
    else:
      hud_lanes = 0

    if enabled:
      if hud_show_car:
        hud_car = 2
      else:
        hud_car = 1
    else:
      hud_car = 0

    fcw_display, steer_required, acc_alert = process_hud_alert(hud_alert)


    # **** process the car messages ****

    # steer torque is converted back to CAN reference (positive when steering right)
    apply_steer = int(interp(-actuators.steer * P.STEER_MAX, P.STEER_LOOKUP_BP, P.STEER_LOOKUP_V))

    lkas_active = enabled and not CS.steer_not_allowed

    # Send CAN commands.
    can_sends = []

    # tester present - w/ no response (keeps radar disabled)
    if CS.CP.carFingerprint in HONDA_BOSCH and CS.CP.openpilotLongitudinalControl:
      if (frame % 10) == 0:
        can_sends.append((0x18DAB0F1, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", 1))

    # Send steering command.
    idx = frame % 4
    can_sends.append(hondacan.create_steering_control(self.packer, apply_steer,
      lkas_active, CS.CP.carFingerprint, idx, CS.CP.openpilotLongitudinalControl))


    accel = actuators.gas - actuators.brake

    # TODO: pass in LoC.long_control_state and use that to decide starting/stoppping
    stopping = accel < 0 and CS.out.vEgo < 0.3
    starting = accel > 0 and CS.out.vEgo < 0.3

    # Prevent rolling backwards
    accel = -1.0 if stopping else accel
    if CS.CP.carFingerprint in HONDA_BOSCH:
      apply_accel = interp(accel, P.BOSCH_ACCEL_LOOKUP_BP, P.BOSCH_ACCEL_LOOKUP_V)
    else:
      apply_accel = interp(accel, P.NIDEC_ACCEL_LOOKUP_BP, P.NIDEC_ACCEL_LOOKUP_V)

    # wind brake from air resistance decel at high speed
    wind_brake = interp(CS.out.vEgo, [0.0, 2.3, 35.0], [0.0, 0.0, 0.15])
    if CS.CP.carFingerprint in OLD_NIDEC_LONG_CONTROL:
      #pcm_speed = pcm_speed
      pcm_accel = int(clip(pcm_accel, 0, 1) * 0xc6)
    else:
      max_accel = interp(CS.out.vEgo, P.NIDEC_MAX_ACCEL_BP, P.NIDEC_MAX_ACCEL_V)
      pcm_accel = int(clip(apply_accel/max_accel, 0.0, 1.0) * 0xc6)
      pcm_speed_BP = [-wind_brake,
                      -wind_brake*(3/4),
                      0.0]
      pcm_speed_V = [0.0,
                     CS.out.vEgo + apply_accel/2.0 - 2.0,
                     CS.out.vEgo + apply_accel/2.0 + 2.0]
      pcm_speed = interp(accel, pcm_speed_BP, pcm_speed_V)

    if not CS.CP.openpilotLongitudinalControl:
      if (frame % 2) == 0:
        idx = frame // 2
        can_sends.append(hondacan.create_bosch_supplemental_1(self.packer, CS.CP.carFingerprint, idx))
      # If using stock ACC, spam cancel command to kill gas when OP disengages.
      if pcm_cancel_cmd:
        can_sends.append(hondacan.spam_buttons_command(self.packer, CruiseButtons.CANCEL, idx, CS.CP.carFingerprint))
      elif CS.out.cruiseState.standstill:
        can_sends.append(hondacan.spam_buttons_command(self.packer, CruiseButtons.RES_ACCEL, idx, CS.CP.carFingerprint))

    else:
      # Send gas and brake commands.
      if (frame % 2) == 0:
        idx = frame // 2
        ts = frame * DT_CTRL

        if CS.CP.carFingerprint in HONDA_BOSCH:
          apply_gas = interp(accel, P.BOSCH_GAS_LOOKUP_BP, P.BOSCH_GAS_LOOKUP_V)
          can_sends.extend(hondacan.create_acc_commands(self.packer, enabled, apply_accel, apply_gas, idx, stopping, starting, CS.CP.carFingerprint))

        else:
          apply_brake = clip(self.brake_last - wind_brake, 0.0, 1.0)
          apply_brake = int(clip(apply_brake * P.BRAKE_MAX, 0, P.BRAKE_MAX - 1))
          pump_on, self.last_pump_ts = brake_pump_hysteresis(apply_brake, self.apply_brake_last, self.last_pump_ts, ts)
          can_sends.append(hondacan.create_brake_command(self.packer, apply_brake, pump_on,
            pcm_override, pcm_cancel_cmd, fcw_display, idx, CS.CP.carFingerprint, CS.stock_brake))
          self.apply_brake_last = apply_brake

          if CS.CP.enableGasInterceptor:
            # way too aggressive at low speed without this
            gas_mult = interp(CS.out.vEgo, [0., 10.], [0.4, 1.0])
            # send exactly zero if apply_gas is zero. Interceptor will send the max between read value and apply_gas.
            # This prevents unexpected pedal range rescaling
            apply_gas = clip(gas_mult * actuators.gas, 0., 1.)
            can_sends.append(create_gas_command(self.packer, apply_gas, idx))

    hud = HUDData(int(pcm_accel), int(round(hud_v_cruise)), hud_car,
                  hud_lanes, fcw_display, acc_alert, steer_required)

    # Send dashboard UI commands.
    if (frame % 10) == 0:
      idx = (frame//10) % 4
      can_sends.extend(hondacan.create_ui_commands(self.packer, pcm_speed, hud, CS.CP.carFingerprint, CS.is_metric, idx, CS.CP.openpilotLongitudinalControl, CS.stock_hud))

    return can_sends
