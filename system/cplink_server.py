#!/usr/bin/env python3
"""
cplink_server.py — CP搭子兼容服务器

模拟 carrotpilot 的 carrot_server 关键端点，让 CP搭子 app 能连接并查看 C3 视频。
监听端口 7000，将 /stream 请求代理到 webrtcd (端口 5001)。

CP搭子 app 连接流程：
  1. 发现 C3 设备（同一 WiFi）
  2. 连接 http://<C3_IP>:7000
  3. POST /stream 发起 WebRTC 视频请求 → 代理到 webrtcd:5001/stream
  4. WebSocket /ws/carstate 获取车辆状态
"""

import json
import time
import asyncio
import logging

from aiohttp import web, ClientSession
from cereal import messaging, car

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
  """将 CP搭子 的 WebRTC 请求代理到 webrtcd"""
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
  """车辆状态 WebSocket，CP搭子 用来显示 HUD 信息"""
  ws = web.WebSocketResponse(heartbeat=20)
  await ws.prepare(request)

  sm = messaging.SubMaster(['carState', 'deviceState', 'peripheralState'])
  params = Params() if HAS_PARAMS else None
  mem_params = None
  if HAS_PARAMS:
    try:
      mem_params = Params("/dev/shm/params")
    except Exception:
      pass

  try:
    while True:
      sm.update(0)
      now = time.time()

      v_ego = None
      v_cruise = None
      gear = "P"
      cpu_temp_c = None

      if sm.alive.get('carState'):
        CS = sm['carState']
        v_ego = CS.vEgoCluster
        # vCruiseCluster 在 openpilot 中单位是 km/h（已经是仪表盘单位）
        v_cruise = CS.vCruiseCluster
        gs = CS.gearShifter
        step = CS.gearStep
        gear_map = {
          GearShifter.park: "P", GearShifter.drive: "D",
          GearShifter.neutral: "N", GearShifter.reverse: "R",
          GearShifter.sport: "S", GearShifter.low: "L",
        }
        gear = gear_map.get(gs, "D")
        if gs == GearShifter.drive and step > 0:
          gear = str(step)
      else:
        # carState 不可用时尝试等待
        await asyncio.sleep(0.5)
        sm.update(0)
        if sm.alive.get('carState'):
          CS = sm['carState']
          v_ego = CS.vEgoCluster
          v_cruise = CS.vCruiseCluster
          gs = CS.gearShifter
          gear_map = {
            GearShifter.park: "P", GearShifter.drive: "D",
            GearShifter.neutral: "N", GearShifter.reverse: "R",
            GearShifter.sport: "S", GearShifter.low: "L",
          }
          gear = gear_map.get(gs, "D")

      if sm.alive.get('deviceState'):
        ds = sm['deviceState']
        c = ds.cpuTempC
        if c and len(c) > 0:
          cpu_temp_c = float(max(c))

      tf_gap = 2
      if params:
        try:
          tf_gap = int(params.get_int("LongitudinalPersonality") or 0) + 1
        except Exception:
          pass

      # 读取红绿灯数据（navi_bridge 写入的共享内存）
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
            if tl_status == 1:
              tlight_str = "red"
            elif tl_status == 2:
              tlight_str = "green"
            elif tl_status == 3:
              tlight_str = "yellow"
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
        "vEgo": v_ego if v_ego is not None else 0.0,
        "vSetKph": v_cruise if v_cruise is not None else 0.0,
        "gear": gear,
        "gpsOk": True,
        "cpuTempC": cpu_temp_c,
        "memPct": None,
        "diskPct": None,
        "diskLabel": "DISK",
        "tfGap": tf_gap,
        "tfBars": tf_gap,
        "driveMode": {"name": "Normal", "kind": "normal"},
        "tlight": tlight_str,
        "tlightCountdown": tlight_countdown,
        "redDot": False,
        "temp": None,
        "speedLimitKph": navi_speed_limit if navi_speed_limit > 0 else None,
        "speedLimitOver": False,
        "naviRoad": navi_road,
        "naviRemainDist": navi_remain_dist,
        "naviRemainTime": navi_remain_time,
        "apm": " ",
      }

      await ws.send_str(json.dumps(payload))
      await asyncio.sleep(0.1)  # 10Hz
  except Exception:
    pass

  try:
    await ws.close()
  except Exception:
    pass
  return ws


async def handle_index(request: web.Request) -> web.Response:
  return web.Response(text="cplink_server running", content_type="text/plain")


async def on_startup(app: web.Application):
  app["http"] = ClientSession()

async def on_cleanup(app: web.Application):
  sess = app.get("http")
  if sess:
    await sess.close()


def main():
  logging.basicConfig(level=logging.WARNING)

  app = web.Application(middlewares=[cors_middleware])
  app.on_startup.append(on_startup)
  app.on_cleanup.append(on_cleanup)

  app.router.add_get("/", handle_index)
  app.router.add_post("/stream", proxy_stream)
  app.router.add_get("/ws/carstate", ws_carstate)

  # 使用 reuse_address 和 reuse_port 避免端口冲突
  web.run_app(app, host="0.0.0.0", port=7000, reuse_address=True)


if __name__ == "__main__":
  main()
