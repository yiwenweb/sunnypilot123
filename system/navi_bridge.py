#!/usr/bin/env python3
"""
navi_bridge.py — CP搭子 (CPlink) → sunnypilot123 桥接器

监听 UDP 7706 端口，接收 CP搭子 app 发来的高德导航 JSON 数据，
转换为 sunnypilot123 已有的 liveMapDataSP cereal 消息，
让现有的 SpeedLimitController (SLC) 框架直接使用。

当没有 CP搭子 数据时，回退读取 OSM 本地数据（MapSpeedLimit 等 Params），
确保 mapd 的离线地图数据仍然可用。

注意：mapd_manager 检测到 navi_bridge 存在时会跳过 liveMapDataSP 发布，
由 navi_bridge 统一负责发布，避免 MultiplePublishersError。

功能：
  - 道路限速控制 (nRoadLimitSpeed → speedLimit)
  - 测速摄像头提前减速 (nSdiSpeedLimit/nSdiDist → speedLimitAhead)
  - 区间测速控制 (nSdiBlockSpeed/nSdiBlockDist → speedLimitAhead)
  - GPS 坐标更新 (vpPosPointLat/Lon → LastGPSPosition)
  - 道路名称显示 (szPosRoadName → currentRoadName)
  - 弯道提前减速 (nTBTDist/nTBTTurnType → turnSpeedLimit)
  - OSM 离线数据回退（无 CP搭子 数据时使用 mapd 本地数据）

部署：
  scp system/navi_bridge.py comma@<C3_IP>:/data/openpilot/system/navi_bridge.py
"""

import json
import socket
import time
import threading
import platform

from cereal import messaging, custom
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.conversions import Conversions as CV
from openpilot.common.swaglog import cloudlog


# CP搭子 SDI 类型定义（测速相机类型）
# 高德 CAMERA_TYPE: 0=测速, 1=监控, 2=闯红灯, 3=违章拍照, 4=公交车道,
#                   5=区间测速起点, 6=区间测速终点, 7=应急车道, 8=非机动车道, 12=其他
SDI_CAMERA_TYPES = {0, 1, 2, 3, 4, 7, 8, 12}  # 普通测速摄像头（需要减速的类型）
SDI_SECTION_TYPES = {5, 6}  # 区间测速（起点/终点）

# TBT 转弯类型 → 建议速度 (m/s)
# 高德 ICON 值定义：
#   2=左转, 3=右转, 4=左前方, 5=右前方, 6=左后方, 7=右后方
#   8=左转掉头, 9=直行, 10=到达目的地, 11=进入环岛, 12=驶出环岛
#   13=到达途经点, 14=进入匝道/辅路, 15=驶出匝道/辅路, 16=到达收费站
TBT_TURN_SPEEDS = {
  0: 0,     # 无/直行 → 不限速
  2: 50 * CV.KPH_TO_MS,   # 左转
  3: 50 * CV.KPH_TO_MS,   # 右转
  4: 40 * CV.KPH_TO_MS,   # 左前方转弯
  5: 40 * CV.KPH_TO_MS,   # 右前方转弯
  6: 25 * CV.KPH_TO_MS,   # 左后方转弯
  7: 25 * CV.KPH_TO_MS,   # 右后方转弯
  8: 20 * CV.KPH_TO_MS,   # 掉头
  9: 0,                    # 直行 → 不限速
  10: 20 * CV.KPH_TO_MS,  # 到达目的地 → 减速
  11: 30 * CV.KPH_TO_MS,  # 进入环岛
  12: 30 * CV.KPH_TO_MS,  # 驶出环岛
  13: 20 * CV.KPH_TO_MS,  # 到达途经点 → 减速
  14: 40 * CV.KPH_TO_MS,  # 进入匝道/辅路 → 40 km/h
  15: 40 * CV.KPH_TO_MS,  # 驶出匝道/辅路 → 40 km/h
  16: 20 * CV.KPH_TO_MS,  # 到达收费站 → 20 km/h
}


class NaviBridge:
  def __init__(self):
    self.pm = messaging.PubMaster(['liveMapDataSP'])
    self.params = Params()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params

    # UDP 接收
    self.udp_port = 7706
    self.last_recv_time = 0.0
    self.lock = threading.Lock()

    # CP搭子 导航数据
    self.road_limit_speed = 0
    self.sdi_type = -1
    self.sdi_speed_limit = 0
    self.sdi_dist = 0.0
    self.sdi_block_type = -1
    self.sdi_block_speed = 0
    self.sdi_block_dist = 0.0
    self.latitude = 0.0
    self.longitude = 0.0
    self.bearing = 0.0
    self.road_name = ""
    self.road_cate = 0
    self.tbt_dist = 0.0
    self.tbt_turn_type = 0
    self.go_pos_dist = 0
    self.go_pos_time = 0
    self.traffic_light = 0       # 0=无, 1=红, 2=绿, 3=黄
    self.traffic_light_sec = 0   # 倒计时秒数

    # 限速骤降保护状态
    self._prev_speed_limit = 0.0  # 上一次发布的限速 (m/s)
    self._NAVI_MIN_SPEED = 30 * CV.KPH_TO_MS       # 最低限速 30 km/h
    self._NAVI_MAX_DROP_RATE = 20 * CV.KPH_TO_MS    # 每秒最大降速 20 km/h

  def udp_listener_thread(self):
    """后台线程：监听 UDP 7706 端口，接收 CP搭子 JSON"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', self.udp_port))
    sock.settimeout(5.0)
    cloudlog.info(f"NaviBridge: listening on UDP port {self.udp_port}")

    while True:
      try:
        data, addr = sock.recvfrom(4096)
        json_str = data.decode('utf-8', errors='ignore')
        json_obj = json.loads(json_str)
        with self.lock:
          self.parse_cplink_json(json_obj)
          self.last_recv_time = time.monotonic()
      except socket.timeout:
        continue
      except json.JSONDecodeError:
        cloudlog.warning("NaviBridge: invalid JSON received")
      except Exception as e:
        cloudlog.error(f"NaviBridge: UDP error: {e}")
        time.sleep(1.0)

  def parse_cplink_json(self, j):
    """解析 CP搭子 JSON 数据包"""
    self.road_limit_speed = int(j.get('nRoadLimitSpeed', 0))
    self.sdi_type = int(j.get('nSdiType', -1))
    self.sdi_speed_limit = int(j.get('nSdiSpeedLimit', 0))
    self.sdi_dist = float(j.get('nSdiDist', 0))
    self.sdi_block_type = int(j.get('nSdiBlockType', -1))
    self.sdi_block_speed = int(j.get('nSdiBlockSpeed', 0))
    self.sdi_block_dist = float(j.get('nSdiBlockDist', 0))
    self.latitude = float(j.get('vpPosPointLat', 0))
    self.longitude = float(j.get('vpPosPointLon', 0))
    self.bearing = float(j.get('nPosAngle', 0))
    self.road_name = str(j.get('szPosRoadName', ''))
    self.road_cate = int(j.get('roadcate', 0))
    self.tbt_dist = float(j.get('nTBTDist', 0))
    self.tbt_turn_type = int(j.get('nTBTTurnType', 0))
    self.go_pos_dist = int(j.get('nGoPosDist', 0))
    self.go_pos_time = int(j.get('nGoPosTime', 0))
    self.traffic_light = int(j.get('nTrafficLight', 0))
    self.traffic_light_sec = int(j.get('nTrafficLightSec', 0))

  @property
  def has_cplink_data(self):
    """CP搭子 数据是否新鲜（5秒内收到过）"""
    return (time.monotonic() - self.last_recv_time) < 5.0 if self.last_recv_time > 0 else False

  def get_speed_limit(self):
    """获取当前道路限速 (m/s)，带骤降保护和最低限速"""
    if self.road_limit_speed > 0:
      raw_limit = self.road_limit_speed * CV.KPH_TO_MS

      # 安全防护 1：最低限速 30 km/h，防止高速上误刹停
      raw_limit = max(raw_limit, self._NAVI_MIN_SPEED)

      # 安全防护 2：限速骤降保护（每秒最多降 20 km/h）
      # 10Hz 发布，每帧间隔 0.1s
      if self._prev_speed_limit > 0:
        max_drop = self._NAVI_MAX_DROP_RATE * 0.1  # 每帧最大降幅
        if raw_limit < self._prev_speed_limit - max_drop:
          raw_limit = self._prev_speed_limit - max_drop

      self._prev_speed_limit = raw_limit
      return raw_limit

    # 无限速时重置
    self._prev_speed_limit = 0.0
    return 0.0

  def get_ahead_speed_limit(self):
    """获取前方测速点/区间测速限速 (speed_ms, distance_m)"""
    if self.sdi_block_type in SDI_SECTION_TYPES and self.sdi_block_speed > 0:
      return max(self.sdi_block_speed * CV.KPH_TO_MS, self._NAVI_MIN_SPEED), self.sdi_block_dist
    if self.sdi_type in SDI_CAMERA_TYPES and self.sdi_speed_limit > 0:
      return max(self.sdi_speed_limit * CV.KPH_TO_MS, self._NAVI_MIN_SPEED), self.sdi_dist
    return 0.0, 0.0

  def get_turn_speed_limit(self):
    """根据 TBT 转弯信息计算建议速度 (speed_ms, distance_m)"""
    if self.tbt_dist <= 0 or self.tbt_turn_type == 0:
      return 0.0, 0.0
    turn_speed = TBT_TURN_SPEEDS.get(self.tbt_turn_type, 0)
    if turn_speed <= 0:
      return 0.0, 0.0
    return turn_speed, self.tbt_dist

  def get_osm_fallback_data(self):
    """从 OSM 本地数据（mapd 写入的 Params）获取限速信息作为回退"""
    speed_limit = 0.0
    road_name = ""
    next_speed_limit = 0.0
    next_speed_limit_dist = 0.0

    try:
      sl = self.mem_params.get("MapSpeedLimit", encoding='utf8')
      if sl:
        speed_limit = float(sl)
    except Exception:
      pass

    try:
      rn = self.mem_params.get("RoadName", encoding='utf8')
      if rn:
        road_name = rn
    except Exception:
      pass

    try:
      nsl_str = self.mem_params.get("NextMapSpeedLimit", encoding='utf8')
      if nsl_str:
        nsl = json.loads(nsl_str)
        next_speed_limit = float(nsl.get('speedlimit', 0))
        # 距离需要 GPS 坐标计算，这里简化处理
        next_speed_limit_dist = 0.0
    except Exception:
      pass

    return speed_limit, road_name, next_speed_limit, next_speed_limit_dist

  def update_gps_position(self):
    """更新 GPS 位置到共享内存"""
    if self.latitude == 0.0 and self.longitude == 0.0:
      return
    gps_data = json.dumps({
      "latitude": self.latitude,
      "longitude": self.longitude,
      "bearing": self.bearing,
    })
    try:
      self.mem_params.put("LastGPSPosition", gps_data)
    except Exception:
      pass

  def update_traffic_light(self):
    """更新红绿灯数据到共享内存，供 cplink_server 推送给 app HUD"""
    try:
      tl_data = json.dumps({
        "status": self.traffic_light,   # 0=无, 1=红, 2=绿, 3=黄
        "countdown": self.traffic_light_sec,
      })
      self.mem_params.put("NaviTrafficLight", tl_data)
    except Exception:
      pass

  def update_navi_info(self):
    """更新导航信息到共享内存，供 cplink_server 推送给 app HUD"""
    try:
      info = json.dumps({
        "roadName": self.road_name,
        "speedLimit": self.road_limit_speed,
        "remainDist": self.go_pos_dist,
        "remainTime": self.go_pos_time,
      })
      self.mem_params.put("NaviInfo", info)
    except Exception:
      pass

  def publish_live_map_data_sp(self):
    """构建并发布 liveMapDataSP cereal 消息

    优先使用 CP搭子 实时数据，无数据时回退到 OSM 离线数据
    """
    with self.lock:
      cplink_fresh = self.has_cplink_data

      if cplink_fresh:
        # 使用 CP搭子 实时数据
        speed_limit = self.get_speed_limit()
        ahead_speed, ahead_dist = self.get_ahead_speed_limit()
        turn_speed, turn_dist = self.get_turn_speed_limit()
        road_name = self.road_name
        lat = self.latitude
        lon = self.longitude
        bearing = self.bearing
        data_type = custom.LiveMapDataSP.DataType.online
      else:
        # 回退到 OSM 离线数据
        speed_limit, road_name, ahead_speed, ahead_dist = self.get_osm_fallback_data()
        turn_speed, turn_dist = 0.0, 0.0
        lat, lon, bearing = 0.0, 0.0, 0.0
        data_type = custom.LiveMapDataSP.DataType.offline

    msg = messaging.new_message('liveMapDataSP')
    msg.valid = cplink_fresh or speed_limit > 0

    d = msg.liveMapDataSP
    d.lastGpsTimestamp = int(time.time() * 1000)
    d.lastGpsLatitude = lat
    d.lastGpsLongitude = lon
    d.lastGpsBearingDeg = bearing
    d.lastGpsSpeed = 0.0
    d.lastGpsAccuracy = 1.0
    d.lastGpsBearingAccuracyDeg = 1.0

    d.speedLimitValid = speed_limit > 0
    d.speedLimit = speed_limit

    d.speedLimitAheadValid = ahead_speed > 0
    d.speedLimitAhead = ahead_speed
    d.speedLimitAheadDistance = ahead_dist

    d.turnSpeedLimitValid = turn_speed > 0
    d.turnSpeedLimit = turn_speed
    d.turnSpeedLimitEndDistance = turn_dist
    d.turnSpeedLimitSign = 0

    d.currentRoadName = road_name
    d.dataType = data_type

    self.pm.send('liveMapDataSP', msg)

  def run(self):
    """主循环：启动 UDP 监听线程，10Hz 发布 cereal 消息"""
    t = threading.Thread(target=self.udp_listener_thread, daemon=True)
    t.start()
    cloudlog.info("NaviBridge: started (unified liveMapDataSP publisher, replaces mapd_manager publish)")

    if not self.mem_params.get("LastGPSPosition"):
      self.mem_params.put("LastGPSPosition", "{}")

    rk = Ratekeeper(10, print_delay_threshold=None)

    while True:
      self.publish_live_map_data_sp()
      if self.has_cplink_data:
        self.update_gps_position()
        self.update_traffic_light()
        self.update_navi_info()
      rk.keep_time()


def main():
  bridge = NaviBridge()
  bridge.run()


if __name__ == "__main__":
  main()
