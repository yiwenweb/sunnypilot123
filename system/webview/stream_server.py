#!/usr/bin/env python3
"""
C3 实时视频流服务
在手机浏览器上查看 C3 摄像头画面

两种模式自动切换:
1. openpilot 运行时: 从 livestream H264 编码流读取（低延迟）
2. openpilot 未运行时: 从 VisionIpc 读取 YUV 原始帧（需要 camerad）

使用方法:
  python system/webview/stream_server.py
  手机浏览器访问 http://<C3_IP>:8099
"""

import time
import threading
import subprocess
import signal
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

# 配置
HOST = "0.0.0.0"
PORT = 8099
JPEG_QUALITY = 60
TARGET_FPS = 15


HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>C3 Live View</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #000; color: #eee;
    font-family: -apple-system, sans-serif;
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; overflow: hidden;
  }
  .video-container {
    width: 100%; height: 100vh; position: relative;
    background: #000;
  }
  .video-container img {
    width: 100%; height: 100%; display: block;
    position: absolute; top: 0; left: 0;
    object-fit: contain;
  }
  .video-container .placeholder {
    position: relative; width: 100%; height: 100vh;
  }
  .status {
    position: absolute; top: 6px; right: 8px; z-index: 10;
    background: rgba(0,0,0,0.5); padding: 2px 8px;
    border-radius: 4px; font-size: 11px;
  }
  .fps {
    position: absolute; top: 6px; right: 70px; z-index: 10;
    background: rgba(0,0,0,0.5); padding: 2px 6px;
    border-radius: 4px; font-size: 11px; color: #aaa;
  }
  /* 摄像头切换按钮 — 右下角小按钮 */
  .controls {
    position: absolute; bottom: 6px; right: 6px; z-index: 10;
    display: flex; gap: 4px;
  }
  .cam-btn {
    padding: 4px 10px; background: rgba(0,0,0,0.5); color: #aaa;
    border: 1px solid rgba(255,255,255,0.2); border-radius: 12px;
    font-size: 11px; cursor: pointer;
  }
  .cam-btn.active { color: #fff; border-color: rgba(255,255,255,0.5); }
</style>
</head>
<body>
  <div class="video-container" id="container">
    <div class="placeholder"></div>
    <div class="status" id="status" style="color:#ff0">CONNECTING</div>
    <div class="fps" id="fps"></div>
    <div class="controls">
      <button class="cam-btn active" onclick="switchCam('road', this)">前方</button>
      <button class="cam-btn" onclick="switchCam('wideRoad', this)">广角</button>
      <button class="cam-btn" onclick="switchCam('driver', this)">驾驶员</button>
    </div>
  </div>
  <script>
    var currentCam = 'road';
    var statusEl = document.getElementById('status');
    var fpsEl = document.getElementById('fps');
    var container = document.getElementById('container');
    var imgA = new Image();
    var imgB = new Image();
    var useA = true;
    var running = true;
    var frameCount = 0;
    var fpsCounter = 0;
    var lastFpsTime = Date.now();

    imgA.style.cssText = 'width:100%;height:100%;display:block;position:absolute;top:0;left:0;object-fit:contain;';
    imgB.style.cssText = 'width:100%;height:100%;display:block;position:absolute;top:0;left:0;object-fit:contain;';
    container.appendChild(imgA);
    container.appendChild(imgB);

    function loadNext() {
      if (!running) return;
      var active = useA ? imgA : imgB;
      var standby = useA ? imgB : imgA;
      var url = '/snapshot?cam=' + currentCam + '&t=' + Date.now();

      standby.onload = function() {
        standby.style.zIndex = 2;
        active.style.zIndex = 1;
        useA = !useA;
        frameCount++;
        fpsCounter++;
        statusEl.textContent = 'LIVE';
        statusEl.style.color = '#0f0';
        setTimeout(loadNext, 50);
      };
      standby.onerror = function() {
        statusEl.textContent = 'OFFLINE';
        statusEl.style.color = '#f44';
        setTimeout(loadNext, 2000);
      };
      standby.src = url;
    }

    function switchCam(cam, btn) {
      currentCam = cam;
      frameCount = 0;
      document.querySelectorAll('.cam-btn').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
    }

    // FPS 计算
    setInterval(function() {
      var now = Date.now();
      var elapsed = (now - lastFpsTime) / 1000;
      if (elapsed > 0) {
        var fps = Math.round(fpsCounter / elapsed);
        fpsEl.textContent = fps + 'fps';
      }
      fpsCounter = 0;
      lastFpsTime = now;
    }, 2000);

    loadNext();
  </script>
</body>
</html>"""


# ============================================================
# 摄像头数据源
# ============================================================

LIVESTREAM_SOCKS = {
  "road": "livestreamRoadEncodeData",
  "wideRoad": "livestreamWideRoadEncodeData",
  "driver": "livestreamDriverEncodeData",
}

VIPC_STREAMS = {
  "road": "VISION_STREAM_ROAD",
  "wideRoad": "VISION_STREAM_WIDE_ROAD",
  "driver": "VISION_STREAM_DRIVER",
}


class CameraStreamer:
  """自动检测数据源，从摄像头获取 JPEG 帧"""

  def __init__(self, camera_type="road"):
    self.camera_type = camera_type
    self._running = False
    self._thread = None
    self._latest_jpeg = b""
    self._lock = threading.Lock()
    self._mode = "unknown"  # "livestream", "vipc", "none"
    self._frame_count = 0
    self._start_time = time.time()

  def start(self):
    if self._running:
      return
    self._running = True
    self._thread = threading.Thread(target=self._run, daemon=True)
    self._thread.start()

  def stop(self):
    self._running = False

  @property
  def jpeg(self):
    with self._lock:
      return self._latest_jpeg

  @property
  def status(self):
    elapsed = time.time() - self._start_time
    fps = self._frame_count / max(elapsed, 1)
    return f"模式: {self._mode} | 帧数: {self._frame_count} | FPS: {fps:.1f}"

  def _set_jpeg(self, data):
    with self._lock:
      self._latest_jpeg = data
    self._frame_count += 1

  def _run(self):
    """主循环：先尝试 livestream，失败则尝试 VisionIpc"""
    # 尝试 livestream 模式（openpilot 运行时）
    if self._try_livestream():
      return

    # 尝试 VisionIpc 模式（camerad 运行时）
    if self._try_vipc():
      return

    # 都不行，等待重试
    self._mode = "waiting"
    while self._running:
      time.sleep(3)
      if self._try_livestream():
        return
      if self._try_vipc():
        return

  def _try_livestream(self):
    """从 livestream H264 编码流读取"""
    import cereal.messaging as messaging

    sock_name = LIVESTREAM_SOCKS.get(self.camera_type)
    if not sock_name:
      return False

    sock = messaging.sub_sock(sock_name, conflate=True)

    # 等待几秒看有没有数据
    for _ in range(30):
      if not self._running:
        return False
      msg = messaging.recv_one_or_none(sock)
      if msg is not None:
        self._mode = "livestream"
        self._run_livestream(sock, msg)
        return True
      time.sleep(0.1)

    return False

  def _run_livestream(self, sock, first_msg):
    """livestream 模式主循环：H264 -> ffmpeg -> JPEG"""
    import cereal.messaging as messaging

    ffmpeg_proc = subprocess.Popen(
      ["ffmpeg", "-probesize", "32", "-flags", "low_delay",
       "-f", "h264", "-i", "pipe:0",
       "-vf", "scale=640:-1", "-q:v", "5",
       "-f", "image2pipe", "-vcodec", "mjpeg", "-an", "pipe:1"],
      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL, bufsize=0,
    )

    # JPEG 读取线程
    def read_jpegs():
      SOI, EOI = b'\xff\xd8', b'\xff\xd9'
      buf = b""
      while self._running and ffmpeg_proc.poll() is None:
        try:
          chunk = ffmpeg_proc.stdout.read(4096)
          if not chunk:
            break
          buf += chunk
          while True:
            si = buf.find(SOI)
            if si == -1:
              buf = b""
              break
            ei = buf.find(EOI, si + 2)
            if ei == -1:
              buf = buf[si:]
              break
            self._set_jpeg(buf[si:ei + 2])
            buf = buf[ei + 2:]
        except Exception:
          break

    reader = threading.Thread(target=read_jpegs, daemon=True)
    reader.start()

    # 喂 H264 数据
    try:
      evta = getattr(first_msg, first_msg.which())
      ffmpeg_proc.stdin.write(evta.header + evta.data)
    except Exception:
      pass

    while self._running:
      msg = messaging.recv_one_or_none(sock)
      if msg is None:
        time.sleep(0.005)
        continue
      try:
        evta = getattr(msg, msg.which())
        ffmpeg_proc.stdin.write(evta.header + evta.data)
        ffmpeg_proc.stdin.flush()
      except (BrokenPipeError, OSError):
        break
      except Exception:
        time.sleep(0.01)

    ffmpeg_proc.kill()

  def _try_vipc(self):
    """从 VisionIpc 读取 YUV 帧"""
    try:
      from msgq.visionipc import VisionIpcClient, VisionStreamType
    except ImportError:
      return False

    stream_map = {
      "road": VisionStreamType.VISION_STREAM_ROAD,
      "wideRoad": VisionStreamType.VISION_STREAM_WIDE_ROAD,
      "driver": VisionStreamType.VISION_STREAM_DRIVER,
    }

    stream_type = stream_map.get(self.camera_type)
    if stream_type is None:
      return False

    client = VisionIpcClient("camerad", stream_type, True)

    # 尝试连接
    if not client.connect(False):
      # camerad 可能没跑，等一下再试
      time.sleep(0.5)
      if not client.connect(False):
        return False

    self._mode = "vipc"
    self._run_vipc(client)
    return True

  def _run_vipc(self, client):
    """VisionIpc 模式主循环：YUV -> ffmpeg -> JPEG（高性能）"""
    frame_interval = 1.0 / TARGET_FPS

    # 先获取一帧确定分辨率
    buf = None
    for _ in range(50):
      if not self._running:
        return
      buf = client.recv()
      if buf is not None and buf.data is not None and len(buf.data) > 0:
        break
      time.sleep(0.1)

    if buf is None:
      return

    w, h = buf.width, buf.height
    # C3 的 YUV 数据是 NV12 格式（Y平面 + UV交错平面）
    # 计算缩放后的宽度，保持比例
    out_w = min(w, 640)
    out_h = int(h * out_w / w)
    # ffmpeg 要求偶数
    out_w = out_w - (out_w % 2)
    out_h = out_h - (out_h % 2)

    print(f"[vipc] {self.camera_type}: {w}x{h} -> {out_w}x{out_h}, stride={buf.stride}")

    # 启动 ffmpeg: 读 NV12 原始帧，输出 MJPEG
    ffmpeg_proc = subprocess.Popen(
      ["ffmpeg", "-hide_banner", "-loglevel", "error",
       "-f", "rawvideo", "-pixel_format", "nv12",
       "-video_size", f"{buf.stride}x{h}",
       "-framerate", str(TARGET_FPS),
       "-i", "pipe:0",
       "-vf", f"crop={w}:{h}:0:0,scale={out_w}:{out_h}",
       "-q:v", "8",
       "-f", "image2pipe", "-vcodec", "mjpeg", "-an", "pipe:1"],
      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
      stderr=subprocess.PIPE, bufsize=0,
    )

    # JPEG 读取线程
    def read_jpegs():
      SOI, EOI = b'\xff\xd8', b'\xff\xd9'
      jpegbuf = b""
      while self._running and ffmpeg_proc.poll() is None:
        try:
          chunk = ffmpeg_proc.stdout.read(8192)
          if not chunk:
            break
          jpegbuf += chunk
          while True:
            si = jpegbuf.find(SOI)
            if si == -1:
              jpegbuf = b""
              break
            ei = jpegbuf.find(EOI, si + 2)
            if ei == -1:
              jpegbuf = jpegbuf[si:]
              break
            self._set_jpeg(jpegbuf[si:ei + 2])
            jpegbuf = jpegbuf[ei + 2:]
        except Exception:
          break

    reader = threading.Thread(target=read_jpegs, daemon=True)
    reader.start()

    # 喂第一帧
    try:
      ffmpeg_proc.stdin.write(bytes(buf.data))
    except Exception:
      pass

    while self._running:
      t0 = time.time()
      try:
        buf = client.recv()
        if buf is None or buf.data is None or len(buf.data) == 0:
          time.sleep(0.02)
          continue
        ffmpeg_proc.stdin.write(bytes(buf.data))
        ffmpeg_proc.stdin.flush()
      except (BrokenPipeError, OSError):
        break
      except Exception as e:
        print(f"[vipc] 帧处理错误: {e}")
        time.sleep(0.1)
        continue

      # 帧率控制
      elapsed = time.time() - t0
      if elapsed < frame_interval:
        time.sleep(frame_interval - elapsed)

    try:
      ffmpeg_proc.kill()
    except Exception:
      pass


# ============================================================
# 全局 streamer 管理
# ============================================================

_streamers = {}
_streamers_lock = threading.Lock()


def get_streamer(cam_type="road"):
  """获取或创建指定摄像头的 streamer"""
  with _streamers_lock:
    if cam_type not in _streamers:
      s = CameraStreamer(cam_type)
      s.start()
      _streamers[cam_type] = s
    return _streamers[cam_type]


def stop_all_streamers():
  with _streamers_lock:
    for s in _streamers.values():
      s.stop()
    _streamers.clear()


# ============================================================
# HTTP 服务
# ============================================================

class StreamHandler(BaseHTTPRequestHandler):
  """处理 HTTP 请求"""

  def log_message(self, format, *args):
    # 减少日志噪音
    pass

  def do_GET(self):
    path = self.path.split("?")[0]
    params = {}
    if "?" in self.path:
      for p in self.path.split("?")[1].split("&"):
        if "=" in p:
          k, v = p.split("=", 1)
          params[k] = v

    if path == "/":
      self._serve_html()
    elif path == "/stream":
      cam = params.get("cam", "road")
      self._serve_mjpeg(cam)
    elif path == "/snapshot":
      cam = params.get("cam", "road")
      self._serve_snapshot(cam)
    elif path == "/status":
      self._serve_status()
    else:
      self.send_error(404)

  def _serve_html(self):
    data = HTML_PAGE.encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def _serve_mjpeg(self, cam_type):
    """MJPEG 推流"""
    streamer = get_streamer(cam_type)
    boundary = b"--frame"

    self.send_response(200)
    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    self.send_header("Pragma", "no-cache")
    self.end_headers()

    interval = 1.0 / TARGET_FPS
    last_jpeg = b""

    while True:
      try:
        jpeg = streamer.jpeg
        if not jpeg:
          time.sleep(0.1)
          continue
        if jpeg is last_jpeg:
          time.sleep(0.02)
          continue
        last_jpeg = jpeg

        self.wfile.write(boundary + b"\r\n")
        self.wfile.write(b"Content-Type: image/jpeg\r\n")
        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n".encode())
        self.wfile.write(b"\r\n")
        self.wfile.write(jpeg)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

        time.sleep(interval)
      except (BrokenPipeError, ConnectionResetError, OSError):
        break

  def _serve_snapshot(self, cam_type):
    """单帧 JPEG"""
    streamer = get_streamer(cam_type)
    jpeg = streamer.jpeg
    if not jpeg:
      self.send_error(503, "No frame available")
      return
    self.send_response(200)
    self.send_header("Content-Type", "image/jpeg")
    self.send_header("Content-Length", str(len(jpeg)))
    self.end_headers()
    self.wfile.write(jpeg)

  def _serve_status(self):
    """状态信息"""
    lines = []
    with _streamers_lock:
      for cam, s in _streamers.items():
        lines.append(f"{cam}: {s.status}")
    if not lines:
      lines.append("无活跃摄像头")
    text = " | ".join(lines)
    data = text.encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "text/plain; charset=utf-8")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


class ThreadedHTTPServer(HTTPServer):
  """支持多客户端同时连接"""
  allow_reuse_address = True
  daemon_threads = True

  def server_bind(self):
    import socket
    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    super().server_bind()

  def process_request(self, request, client_address):
    t = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
    t.start()

  def process_request_thread(self, request, client_address):
    try:
      self.finish_request(request, client_address)
    except Exception:
      self.handle_error(request, client_address)
    finally:
      self.shutdown_request(request)


# ============================================================
# 主入口
# ============================================================

def main():
  print(f"[stream_server] 启动视频流服务 http://{HOST}:{PORT}")
  print(f"[stream_server] 支持模式: livestream (openpilot运行时) / vipc (仅camerad运行时)")

  server = ThreadedHTTPServer((HOST, PORT), StreamHandler)

  def shutdown(sig, frame):
    print("\n[stream_server] 正在关闭...")
    stop_all_streamers()
    server.shutdown()
    sys.exit(0)

  signal.signal(signal.SIGINT, shutdown)
  signal.signal(signal.SIGTERM, shutdown)

  try:
    server.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    stop_all_streamers()
    server.server_close()
    print("[stream_server] 已关闭")


if __name__ == "__main__":
  main()
