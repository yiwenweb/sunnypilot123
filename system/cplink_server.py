#!/usr/bin/env python3
"""
cplink_server.py — CP搭子兼容服务器

监听端口 7000，将 /stream 请求代理到 webrtcd (端口 5001)。
WebSocket /ws/carstate 推送车辆状态给 SP搭子 app HUD。

方案：彻底放弃 SubMaster，直接用 sub_sock + non_blocking receive 轮询。
sub_sock 在 main() 中创建（fork 后新 context 已就绪），
asyncio 定时任务中直接调用 sock.receive(non_blocking=True)，无需线程。
"""

import json
import os
import time
import asyncio
import logging

from aiohttp import web, ClientSession
from cereal import messaging, log, car

GearShifter = car.CarState.GearShifter

HAS_PARAMS = False
Params = None
try:
  from openpilot.common.params import Params as _Params
  Params = _Params
  HAS_PARAMS = True
except Exception:
  pass

WEBRTCD_URL = "http://127.0.0.1:5001/stream"
logger = logging.getLogger("cplink")

# 全局共享数据
cereal_data = {
  "v_ego": 0.0, "v_cruise_kph": 0.0, "gear": "P", "tf_gap": 2,
  "cpu_temp_c": None,
  "cs_alive": False, "ctrl_alive": False, "cc_alive": False, "ds_alive": False,
  "debug": {},
}

# 原始 socket（main() 中创建）
socks = {}
last_recv = {}
poll_stats = {"recv_count": {}, "parse_ok": {}, "parse_fail": {}, "last_error": {}}


ALIVE_TIMEOUT = {
  'carState': 0.5,       # 100Hz → 10ms, 超时 500ms
  'controlsState': 0.5,  # 100Hz
  'carControl': 0.5,     # 100Hz
  'deviceState': 10.0,   # 2Hz → 500ms, 超时 10s
}


def parse_capnp(service, raw_bytes):
  """解析 capnp 消息，返回 (event, service_data) 或 None"""
  try:
    evt = messaging.log_from_bytes(raw_bytes)
    return evt, getattr(evt, service)
  except Exception as e:
    poll_stats["last_error"][service] = f"{type(e).__name__}: {e}"
    return None


def poll_sockets():
  """非阻塞轮询所有 socket，更新 cereal_data"""
  global cereal_data
  now = time.monotonic()

  raw_data = {}
  for svc, sock in socks.items():
    # conflate=True，只取最新一条
    dat = sock.receive(non_blocking=True)
    if dat is not None:
      last_recv[svc] = now
      poll_stats["recv_count"][svc] = poll_stats["recv_count"].get(svc, 0) + 1
      result = parse_capnp(svc, dat)
      if result:
        raw_data[svc] = result  # (event, service_data)
        poll_stats["parse_ok"][svc] = poll_stats["parse_ok"].get(svc, 0) + 1
      else:
        poll_stats["parse_fail"][svc] = poll_stats["parse_fail"].get(svc, 0) + 1

  # 判断 alive
  cs_alive = (now - last_recv.get('carState', 0)) < ALIVE_TIMEOUT['carState'] and 'carState' in raw_data or \
             (now - last_recv.get('carState', 0)) < ALIVE_TIMEOUT['carState'] and last_recv.get('carState', 0) > 0
  ctrl_alive = (now - last_recv.get('controlsState', 0)) < ALIVE_TIMEOUT['controlsState'] and last_recv.get('controlsState', 0) > 0
  cc_alive = (now - last_recv.get('carControl', 0)) < ALIVE_TIMEOUT['carControl'] and last_recv.get('carControl', 0) > 0
  ds_alive = (now - last_recv.get('deviceState', 0)) < ALIVE_TIMEOUT['deviceState'] and last_recv.get('deviceState', 0) > 0

  v_ego = cereal_data["v_ego"]
  v_cruise_kph = cereal_data["v_cruise_kph"]
  gear = cereal_data["gear"]
  tf_gap = cereal_data["tf_gap"]
  cpu_temp_c = cereal_data["cpu_temp_c"]
  debug = cereal_data.get("debug", {})

  # carState
  if 'carState' in raw_data:
    _, CS = raw_data['carState']
    v_ego = CS.vEgoCluster if CS.vEgoCluster > 0.1 else CS.vEgo

    gs = CS.gearShifter
    gear_map = {
      GearShifter.park: "P", GearShifter.drive: "D",
      GearShifter.neutral: "N", GearShifter.reverse: "R",
      GearShifter.sport: "S", GearShifter.low: "L",
      GearShifter.unknown: "?",
    }
    # 也用字符串做 key，兼容 capnp reader 返回字符串的情况
    gear_map_str = {
      "park": "P", "drive": "D", "neutral": "N", "reverse": "R",
      "sport": "S", "low": "L", "unknown": "?",
    }
    gear = gear_map.get(gs, None) or gear_map_str.get(str(gs), "?")
    try:
      if str(gs) in ("drive",) and hasattr(CS, 'gearStep'):
        step = CS.gearStep
        if step > 0:
          gear = str(step)
    except Exception:
      pass

    try:
      gap_raw = int(CS.gapAdjustCruiseTr)
      if 1 <= gap_raw <= 4:
        tf_gap = gap_raw
      debug["gapAdjustCruiseTr"] = gap_raw
    except Exception:
      debug["gapAdjustCruiseTr"] = "N/A"

    try:
      if CS.cruiseState.enabled and CS.cruiseState.speed > 0.1:
        v_cruise_kph = CS.cruiseState.speed * 3.6
    except Exception:
      pass

    debug = {
      "vEgo": round(float(CS.vEgo), 3),
      "vEgoCluster": round(float(CS.vEgoCluster), 3),
      "gearShifter": str(gs),
      "gear_resolved": gear,
    }

  # controlsState
  if 'controlsState' in raw_data:
    try:
      _, ctrl = raw_data['controlsState']
      raw_cruise = ctrl.vCruise
      if 1 < raw_cruise < 250:
        v_cruise_kph = raw_cruise
      debug["vCruise"] = round(raw_cruise, 1)
    except Exception:
      pass

  # carControl — 跟车距离
  if 'carControl' in raw_data:
    try:
      _, cc = raw_data['carControl']
      bars = int(cc.hudControl.leadDistanceBars)
      debug["leadDistanceBars"] = bars
      if 1 <= bars <= 4:
        tf_gap = bars
    except Exception as e:
      debug["leadDistanceBars_error"] = str(e)

  if tf_gap < 1 or tf_gap > 4:
    tf_gap = 2

  # deviceState
  if 'deviceState' in raw_data:
    try:
      _, ds = raw_data['deviceState']
      c = ds.cpuTempC
      if c and len(c) > 0:
        cpu_temp_c = float(max(c))
    except Exception:
      pass

  cereal_data = {
    "v_ego": v_ego, "v_cruise_kph": v_cruise_kph,
    "gear": gear, "tf_gap": tf_gap, "cpu_temp_c": cpu_temp_c,
    "cs_alive": cs_alive, "ctrl_alive": ctrl_alive,
    "cc_alive": cc_alive, "ds_alive": ds_alive,
    "debug": debug,
    "last_recv": {k: round(now - v, 2) if v > 0 else -1 for k, v in last_recv.items()},
  }


async def cereal_loop(app):
  """asyncio 定时任务：直接非阻塞轮询 socket，无需线程"""
  log_counter = 0
  while True:
    try:
      poll_sockets()

      log_counter += 1
      if log_counter >= 200:  # ~20秒打一次日志
        log_counter = 0
        d = cereal_data
        logger.warning(
          "HUD: speed=%.1f cruise=%.0f gear=%s gap=%d | alive: cs=%s ctrl=%s cc=%s ds=%s | recv_ago: %s",
          d["v_ego"] * 3.6, d["v_cruise_kph"], d["gear"], d["tf_gap"],
          d["cs_alive"], d["ctrl_alive"], d["cc_alive"], d["ds_alive"],
          d.get("last_recv", {}),
        )
    except asyncio.CancelledError:
      break
    except Exception as e:
      logger.error("cereal_loop 异常: %s", e)

    await asyncio.sleep(0.05)  # 20Hz 轮询


@web.middleware
async def cors_middleware(request, handler):
  if request.method == 'OPTIONS':
    resp = web.Response(status=200)
  else:
    resp = await handler(request)
  resp.headers['Access-Control-Allow-Origin'] = '*'
  resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
  resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
  return resp


async def proxy_stream(request: web.Request) -> web.Response:
  body = await request.read()
  ct = request.headers.get("Content-Type", "application/json")
  sess: ClientSession = request.app["http"]
  try:
    async with sess.post(WEBRTCD_URL, data=body, headers={"Content-Type": ct}) as resp:
      resp_body = await resp.read()
      out = web.Response(body=resp_body, status=resp.status)
      rct = resp.headers.get("Content-Type")
      if rct:
        out.headers["Content-Type"] = rct
      out.headers["Access-Control-Allow-Origin"] = "*"
      return out
  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=502)


async def ws_carstate(request: web.Request) -> web.WebSocketResponse:
  ws = web.WebSocketResponse(heartbeat=20)
  await ws.prepare(request)
  mem_params = None
  if HAS_PARAMS:
    try:
      mem_params = Params("/dev/shm/params")
    except Exception:
      pass

  try:
    while True:
      now = time.time()
      d = cereal_data

      tlight_str = "off"
      tlight_countdown = 0
      navi_road = ""
      navi_speed_limit = 0
      navi_remain_dist = 0
      navi_remain_time = 0
      if mem_params:
        try:
          tl_raw = mem_params.get("NaviTrafficLight", encoding='utf8')
          if tl_raw:
            tl = json.loads(tl_raw)
            tl_status = tl.get("status", 0)
            tlight_countdown = tl.get("countdown", 0)
            if tl_status == 1: tlight_str = "red"
            elif tl_status == 2: tlight_str = "green"
            elif tl_status == 3: tlight_str = "yellow"
        except Exception:
          pass
        try:
          ni_raw = mem_params.get("NaviInfo", encoding='utf8')
          if ni_raw:
            ni = json.loads(ni_raw)
            navi_road = ni.get("roadName", "")
            navi_speed_limit = ni.get("speedLimit", 0)
            navi_remain_dist = ni.get("remainDist", 0)
            navi_remain_time = ni.get("remainTime", 0)
        except Exception:
          pass

      payload = {
        "ts": now,
        "vEgo": d["v_ego"], "vSetKph": d["v_cruise_kph"],
        "gear": d["gear"], "gpsOk": True,
        "cpuTempC": d["cpu_temp_c"],
        "memPct": None, "diskPct": None, "diskLabel": "DISK",
        "tfGap": d["tf_gap"], "tfBars": d["tf_gap"],
        "driveMode": {"name": "Normal", "kind": "normal"},
        "tlight": tlight_str, "tlightCountdown": tlight_countdown,
        "redDot": False, "temp": None,
        "speedLimitKph": navi_speed_limit if navi_speed_limit > 0 else None,
        "speedLimitOver": False,
        "naviRoad": navi_road,
        "naviRemainDist": navi_remain_dist,
        "naviRemainTime": navi_remain_time,
        "apm": " ",
      }
      await ws.send_str(json.dumps(payload))
      await asyncio.sleep(0.1)
  except Exception as e:
    logger.warning("ws_carstate 异常: %s", e)
  try:
    await ws.close()
  except Exception:
    pass
  return ws


async def handle_index(request: web.Request) -> web.Response:
  return web.Response(text="cplink_server running (sub_sock mode)", content_type="text/plain")


async def handle_debug(request: web.Request) -> web.Response:
  now = time.monotonic()
  d = cereal_data
  info = {
    "ts": time.time(),
    "mode": "sub_sock (no SubMaster)",
    "cereal_alive": {
      "carState": d["cs_alive"], "controlsState": d["ctrl_alive"],
      "carControl": d["cc_alive"], "deviceState": d["ds_alive"],
    },
    "hud_values": {
      "speed_kph": round(d["v_ego"] * 3.6, 1),
      "cruise_kph": round(d["v_cruise_kph"], 1),
      "gear": d["gear"], "gap": d["tf_gap"],
    },
    "raw_fields": d["debug"],
    "last_recv_ago": d.get("last_recv", {}),
    "env": {
      "OPENPILOT_PREFIX": os.environ.get("OPENPILOT_PREFIX", "(not set)"),
      "cwd": os.getcwd(), "pid": os.getpid(),
    },
  }

  # 即时测试：在当前上下文中创建一个临时 sub_sock 尝试接收 carState
  try:
    test_sock = messaging.sub_sock('carState', timeout=500)
    test_dat = test_sock.receive(non_blocking=False)
    if test_dat:
      info["instant_carState_test"] = {
        "received": True,
        "bytes": len(test_dat),
      }
      # 尝试解析并 dump 关键字段
      try:
        evt = messaging.log_from_bytes(test_dat)
        cs = evt.carState
        info["instant_carState_fields"] = {
          "vEgo": round(float(cs.vEgo), 3),
          "vEgoCluster": round(float(cs.vEgoCluster), 3),
          "gearShifter": str(cs.gearShifter),
          "cruiseState.speed": round(float(cs.cruiseState.speed), 3),
          "cruiseState.enabled": bool(cs.cruiseState.enabled),
          "which": evt.which(),
        }
        # 尝试读取可能不存在的字段
        try:
          info["instant_carState_fields"]["gapAdjustCruiseTr"] = float(cs.gapAdjustCruiseTr)
        except Exception as e2:
          info["instant_carState_fields"]["gapAdjustCruiseTr_error"] = str(e2)
        try:
          info["instant_carState_fields"]["gearStep"] = int(cs.gearStep)
        except Exception as e2:
          info["instant_carState_fields"]["gearStep_error"] = str(e2)
      except Exception as e:
        info["instant_carState_parse_error"] = f"{type(e).__name__}: {e}"
    else:
      info["instant_carState_test"] = {"received": False, "bytes": 0}
  except Exception as e:
    info["instant_carState_test"] = {"error": str(e)}

  info["poll_stats"] = poll_stats

  try:
    p = Params() if HAS_PARAMS else None
    if p:
      info["params"] = {
        "IsOnroad": p.get_bool("IsOnroad"),
        "IsOffroad": p.get_bool("IsOffroad"),
      }
  except Exception:
    pass
  return web.json_response(info, dumps=lambda x: json.dumps(x, ensure_ascii=False, indent=2))


async def on_startup(app: web.Application):
  app["http"] = ClientSession()
  # 启动 cereal 轮询循环
  app["cereal_task"] = asyncio.ensure_future(cereal_loop(app))
  logger.warning("cereal_loop 已启动 (sub_sock 模式)")


async def on_cleanup(app: web.Application):
  task = app.get("cereal_task")
  if task:
    task.cancel()
    try:
      await task
    except asyncio.CancelledError:
      pass
  sess = app.get("http")
  if sess:
    await sess.close()


def main():
  logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(name)s %(levelname)s %(message)s')
  logger.warning("cplink_server 启动 (sub_sock 模式)，端口 7000")

  # 在 main() 中创建所有 sub_sock
  # launcher() 已经执行了 messaging.context = messaging.Context()
  # 所以这里的 context 是 fork 后的新 context，与其他进程的 msgq 通信正常
  services = ['carState', 'controlsState', 'carControl', 'deviceState']
  for svc in services:
    socks[svc] = messaging.sub_sock(svc, conflate=True)
    last_recv[svc] = 0
    logger.warning("sub_sock 已创建: %s (port=%d)", svc, messaging.SERVICE_LIST[svc].port)

  app = web.Application(middlewares=[cors_middleware])
  app.on_startup.append(on_startup)
  app.on_cleanup.append(on_cleanup)
  app.router.add_get("/", handle_index)
  app.router.add_post("/stream", proxy_stream)
  app.router.add_get("/ws/carstate", ws_carstate)
  app.router.add_get("/debug", handle_debug)
  web.run_app(app, host="0.0.0.0", port=7000, reuse_address=True)


if __name__ == "__main__":
  main()
