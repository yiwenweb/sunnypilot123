#!/usr/bin/env python3
# 简易版客户端授权验证文件

import os
import json
import base64
import hashlib
import hmac
import time
import threading
from datetime import datetime

class AuthClient:
    """
    简易版客户端授权验证系统
    用于验证设备是否有权使用增强型控制器
    """
    
    def __init__(self):
        self.version = "1.0"
        self.auth_file_path = "/data/params/auth_code.key"
        self.device_id_path = "/data/params/d/DongleId"
        self.auth_cache_path = "/data/params/auth_status.cache"
        self.auth_status = {
            "is_authorized": False,
            "features": [],
            "device_id": None,
            "expires": 0,
            "last_check": 0
        }
        self.launch_file_hash = None
        self.cache_lock = threading.Lock()
        
        # 安全密钥 - 在实际应用中应存储在安全位置
        self.master_key = "op_enhanced_controller_master_key_v1"
        
        # 预期的launch_openpilot.sh文件哈希值
        self.expected_launch_hash = None  # 将在运行时从授权码中获取或设置
        
        # 初始化设备ID和授权状态
        self._init_auth_status()
    
    def _init_auth_status(self):
        """初始化授权状态"""
        # 获取设备ID
        self.auth_status["device_id"] = self._get_device_id()
        
        # 尝试从缓存加载授权状态
        self._load_cache()
        
        # 验证授权
        self.verify_authorization()
    
    def _get_device_id(self):
        """获取设备ID"""
        try:
            if os.path.exists(self.device_id_path):
                with open(self.device_id_path, 'r') as f:
                    return f.read().strip()
            else:
                # 如果DongleId文件不存在，生成随机ID
                random_id = hashlib.md5(str(time.time()).encode()).hexdigest()
                return random_id
        except Exception as e:
            print(f"获取设备ID失败: {str(e)}")
            # 出错时返回随机ID
            return hashlib.md5(str(time.time()).encode()).hexdigest()
    
    def _load_cache(self):
        """从缓存加载授权状态"""
        try:
            if os.path.exists(self.auth_cache_path):
                with self.cache_lock:
                    with open(self.auth_cache_path, 'r') as f:
                        cache_data = json.load(f)
                        self.auth_status.update(cache_data)
        except Exception as e:
            print(f"加载授权缓存失败: {str(e)}")
    
    def _save_cache(self):
        """保存授权状态到缓存"""
        try:
            with self.cache_lock:
                with open(self.auth_cache_path, 'w') as f:
                    json.dump(self.auth_status, f)
        except Exception as e:
            print(f"保存授权缓存失败: {str(e)}")
    
    def _decrypt_auth_code(self, encrypted_data, key):
        """解密授权码"""
        try:
            # 派生HMAC密钥
            hmac_key = hashlib.sha256(key + b"hmac").digest()
            
            # 解析加密数据
            if isinstance(encrypted_data, str):
                encrypted_json = json.loads(encrypted_data)
            else:
                encrypted_json = json.loads(encrypted_data.decode('utf-8'))
            
            # 获取加密参数
            iv = base64.b64decode(encrypted_json.get("iv"))
            data = base64.b64decode(encrypted_json.get("data"))
            tag = base64.b64decode(encrypted_json.get("tag"))
            
            # 验证HMAC
            computed_tag = hmac.new(hmac_key, data, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(computed_tag, tag):
                raise ValueError("认证标签验证失败")
            
            # 解密数据
            decrypted = bytearray(len(data))
            key_array = bytearray(key)
            
            for i in range(len(data)):
                key_byte = key_array[i % len(key_array)]
                iv_byte = iv[i % len(iv)]
                decrypted[i] = data[i] ^ key_byte ^ iv_byte
            
            return bytes(decrypted)
        
        except Exception as e:
            raise ValueError(f"解密失败: {str(e)}")
    
    def verify_launch_file(self, launch_file_path="/data/openpilot/launch_openpilot.sh"):
        """验证启动文件完整性"""
        try:
            if self.expected_launch_hash is None:
                return True  # 如果没有预期哈希值，默认通过验证
                
            with open(launch_file_path, 'rb') as f:
                content = f.read()
            
            # 计算文件哈希
            file_hash = hashlib.sha256(content).hexdigest()
            
            # 验证哈希值是否匹配
            return file_hash == self.expected_launch_hash
            
        except Exception as e:
            print(f"验证启动文件失败: {str(e)}")
            return False
    
    def verify_authorization(self, force=False):
        """
        验证设备是否有授权
        
        参数:
            force: 是否强制重新验证，忽略缓存
        """
        current_time = int(time.time())
        
        # 如果距离上次验证不到1小时且非强制验证，直接返回缓存结果
        if not force and (current_time - self.auth_status["last_check"]) < 3600:
            return self.auth_status["is_authorized"]
        
        try:
            # 检查授权文件是否存在
            if not os.path.exists(self.auth_file_path):
                print(f"授权文件不存在: {self.auth_file_path}")
                self.auth_status["is_authorized"] = False
                self.auth_status["last_check"] = current_time
                self._save_cache()
                return False
            
            # 读取授权文件
            with open(self.auth_file_path, 'r') as f:
                auth_code = f.read().strip()
            print(f"读取到的授权码: {auth_code[:50]}...")  # 只打印前50个字符
            
            # 解码授权数据
            try:
                auth_data = json.loads(base64.b64decode(auth_code))
                print(f"解码后的授权数据: {json.dumps(auth_data, indent=2)}")
            except Exception as e:
                print(f"授权数据解码失败: {str(e)}")
                raise
            
            salt = base64.b64decode(auth_data.get("salt"))
            encrypted_auth = auth_data.get("auth")
            
            # 生成解密密钥
            key = hashlib.pbkdf2_hmac(
                'sha256', 
                self.master_key.encode(), 
                salt, 
                100000  # 迭代次数，需要与加密时相同
            )
            
            # 解密授权数据
            try:
                decrypted_auth = self._decrypt_auth_code(encrypted_auth, key)
                auth_info = json.loads(decrypted_auth)
                print(f"解密后的授权信息: {json.dumps(auth_info, indent=2)}")
            except Exception as e:
                print(f"授权数据解密失败: {str(e)}")
                raise
            
            # 验证授权信息
            auth_hash = auth_info.pop("hash", None)
            auth_json = json.dumps(auth_info, sort_keys=True)
            computed_hash = hashlib.sha256(auth_json.encode()).hexdigest()
            
            print(f"授权哈希验证: 预期={auth_hash}, 计算={computed_hash}")
            if auth_hash != computed_hash:
                raise ValueError("授权数据哈希验证失败")
            
            # 验证设备ID是否匹配
            print(f"设备ID验证: 预期={auth_info['device_id']}, 当前={self.auth_status['device_id']}")
            if auth_info["device_id"] != self.auth_status["device_id"]:
                raise ValueError("设备ID不匹配")
            
            # 验证授权是否过期
            print(f"授权过期时间: {datetime.fromtimestamp(auth_info['expires']).strftime('%Y-%m-%d %H:%M:%S')}")
            if auth_info["expires"] < current_time:
                raise ValueError("授权已过期")
            
            # 验证启动文件完整性
            if not self.verify_launch_file():
                raise ValueError("启动文件已被修改")
            
            # 更新授权状态
            self.auth_status["is_authorized"] = True
            self.auth_status["features"] = auth_info.get("features", [])
            self.auth_status["expires"] = auth_info["expires"]
            self.auth_status["last_check"] = current_time
            
            self._save_cache()
            return True
            
        except Exception as e:
            print(f"验证授权失败: {str(e)}")
            self.auth_status["is_authorized"] = False
            self.auth_status["last_check"] = current_time
            self._save_cache()
            return False
    
    def is_feature_authorized(self, feature_name):
        """
        检查特定功能是否授权
        
        参数:
            feature_name: 功能名称，例如 "enhanced_lat" 或 "enhanced_long"
        """
        # 验证授权状态
        if not self.verify_authorization():
            return False
        
        # 检查功能是否在授权列表中
        return feature_name in self.auth_status["features"]
    
    def get_device_id(self):
        """获取当前设备ID"""
        return self.auth_status["device_id"]
    
    def get_expiry_date(self):
        """获取授权过期日期"""
        if self.auth_status["expires"] > 0:
            return datetime.fromtimestamp(self.auth_status["expires"]).strftime("%Y-%m-%d %H:%M:%S")
        return "未授权"

# 创建全局单例实例
_auth_client = None

def get_auth_client():
    """获取授权客户端实例（单例模式）"""
    global _auth_client
    if _auth_client is None:
        _auth_client = AuthClient()
    return _auth_client

if __name__ == "__main__":
    # 创建授权客户端实例
    auth_client = get_auth_client()
    
    # 打印设备ID
    print(f"设备ID: {auth_client.get_device_id()}")
    
    # 强制重新验证授权
    is_authorized = auth_client.verify_authorization(force=True)
    
    # 打印授权状态
    print(f"授权状态: {'已授权' if is_authorized else '未授权'}")
    print(f"过期时间: {auth_client.get_expiry_date()}")
    
    # 如果授权失败，打印更多调试信息
    if not is_authorized:
        print("\n授权验证失败，请检查以下信息：")
        print("1. 授权文件是否存在")
        print("2. 设备ID是否匹配")
        print("3. 授权是否过期")
        print("4. 启动文件是否被修改") 
