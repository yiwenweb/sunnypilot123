#!/usr/bin/env python3
"""
快速诊断 CP搭子 连接状态
用法: python system/test_cplink.py
"""
import socket
import json
import time

print("=" * 50)
print("CP搭子 连接诊断工具")
print("=" * 50)

# 1. 检查 UDP 7706 是否能收到数据
print("\n[1] 监听 UDP 7706，等待 CP搭子 数据...")
print("    请确保手机和 C3 在同一 WiFi，CP搭子 已打开")
print("    等待 10 秒...\n")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
  sock.bind(('0.0.0.0', 7706))
except OSError as e:
  print(f"    ⚠ 端口 7706 被占用（navi_bridge 可能已在运行）: {e}")
  print("    这是正常的，说明 navi_bridge 进程已启动")
  print("\n[2] 改用检查 cereal 消息...")

  try:
    from cereal import messaging
    sm = messaging.SubMaster(['liveMapDataSP'])
    print("    等待 liveMapDataSP 消息（5秒）...\n")

    for i in range(50):
      sm.update(100)
      if sm.updated['liveMapDataSP']:
        d = sm['liveMapDataSP']
        print(f"    ✅ 收到 liveMapDataSP!")
        print(f"    限速: {d.speedLimit:.1f} m/s ({d.speedLimit * 3.6:.0f} km/h)")
        print(f"    限速有效: {d.speedLimitValid}")
        print(f"    前方限速: {d.speedLimitAhead:.1f} m/s, 距离: {d.speedLimitAheadDistance:.0f}m")
        print(f"    路名: {d.currentRoadName}")
        print(f"    GPS: {d.lastGpsLatitude:.6f}, {d.lastGpsLongitude:.6f}")
        print(f"    时间戳: {d.lastGpsTimestamp}")
        print(f"    数据类型: {d.dataType}")
        print(f"\n    ✅ CP搭子 → navi_bridge → liveMapDataSP 链路正常!")
        break
    else:
      print("    ❌ 5秒内未收到 liveMapDataSP 消息")
      print("    可能原因: navi_bridge 未运行，或 CP搭子 未发送数据")
  except Exception as e:
    print(f"    cereal 检查失败: {e}")

  sock.close()
  exit()

sock.settimeout(10.0)
try:
  data, addr = sock.recvfrom(4096)
  j = json.loads(data.decode('utf-8'))
  print(f"    ✅ 收到 CP搭子 数据! 来自: {addr[0]}:{addr[1]}")
  print(f"    道路限速: {j.get('nRoadLimitSpeed', '无')} km/h")
  print(f"    路名: {j.get('szPosRoadName', '无')}")
  print(f"    GPS: {j.get('vpPosPointLat', 0)}, {j.get('vpPosPointLon', 0)}")
  print(f"    测速类型: {j.get('nSdiType', -1)}")
  print(f"    转弯距离: {j.get('nTBTDist', 0)}m, 类型: {j.get('nTBTTurnType', 0)}")
  print(f"\n    ✅ CP搭子 连接正常!")
except socket.timeout:
  print("    ❌ 10秒内未收到任何数据")
  print("    请检查:")
  print("    - 手机和 C3 是否在同一 WiFi")
  print("    - CP搭子 app 是否已打开并运行")
  print("    - CP搭子 是否已设置 C3 的 IP 地址")
except Exception as e:
  print(f"    ❌ 错误: {e}")
finally:
  sock.close()

# 2. 检查 webrtcd 端口
print("\n[3] 检查视频服务端口...")
for port, name in [(5001, "webrtcd"), (7000, "cplink_server")]:
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.settimeout(2)
  try:
    s.connect(('127.0.0.1', port))
    print(f"    ✅ {name} (端口 {port}) 正在运行")
  except:
    print(f"    ❌ {name} (端口 {port}) 未运行")
  finally:
    s.close()

print("\n" + "=" * 50)
