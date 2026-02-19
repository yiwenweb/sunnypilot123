#!/usr/bin/env python3
# 横向控制库初始化文件

import os
import sys
import importlib.util
import base64
import json
import hashlib
import hmac

# 导入标准控制器
from openpilot.selfdrive.controls.lib.lateral_mpc_lib.lat_mpc import LateralMpc

# 定义加密控制器的路径
ENHANCED_LAT_MPC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enhanced_lat_mpc.py.enc")

# 安全密钥 - 必须与加密时相同
ENCRYPTION_KEY = "op_enhanced_controller_lat_key_v1"

def _decrypt_controller(encrypted_file_path, key_password):
    """解密控制器代码"""
    try:
        # 读取加密文件
        with open(encrypted_file_path, 'r') as f:
            encrypted_data = json.load(f)
        
        # 获取盐值和加密数据
        salt = base64.b64decode(encrypted_data.get("salt"))
        encrypted_json = encrypted_data.get("data")
        
        # 派生密钥
        key = hashlib.pbkdf2_hmac('sha256', key_password.encode(), salt, 100000)
        
        # 解析加密JSON
        encrypted_obj = json.loads(encrypted_json)
        iv = base64.b64decode(encrypted_obj.get("iv"))
        data = base64.b64decode(encrypted_obj.get("data"))
        tag = base64.b64decode(encrypted_obj.get("tag"))
        
        # 派生HMAC密钥
        hmac_key = hashlib.sha256(key + b"hmac").digest()
        
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
        print(f"解密控制器失败: {str(e)}")
        return None

def _verify_auth_status():
    """验证授权状态"""
    try:
        # 检查环境变量
        if os.environ.get("OP_ENHANCED_CONTROLLER") == "1":
            return True
            
        # 如果环境变量未设置，尝试导入授权客户端
        auth_module_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                                       "auth_client.py")
        
        if os.path.exists(auth_module_path):
            spec = importlib.util.spec_from_file_location("auth_client", auth_module_path)
            auth_client_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(auth_client_module)
            
            # 获取授权客户端实例
            auth_client = auth_client_module.get_auth_client()
            
            # 验证是否有横向增强控制器的授权
            return auth_client.is_feature_authorized("enhanced_lat")
        
        return False
    
    except Exception as e:
        print(f"验证授权状态失败: {str(e)}")
        return False

def _load_enhanced_controller():
    """加载增强型控制器"""
    try:
        if not os.path.exists(ENHANCED_LAT_MPC_PATH):
            print("增强型横向控制器文件不存在")
            return None
            
        # 解密控制器代码
        decrypted_code = _decrypt_controller(ENHANCED_LAT_MPC_PATH, ENCRYPTION_KEY)
        if decrypted_code is None:
            return None
            
        # 动态加载解密后的代码
        module_name = "enhanced_lat_mpc"
        spec = importlib.util.spec_from_loader(module_name, loader=None)
        module = importlib.util.module_from_spec(spec)
        
        # 执行解密后的代码
        exec(decrypted_code, module.__dict__)
        
        # 返回控制器类
        return module.EnhancedLateralController
        
    except Exception as e:
        print(f"加载增强型控制器失败: {str(e)}")
        return None

# 根据授权状态选择控制器
if _verify_auth_status():
    EnhancedController = _load_enhanced_controller()
    if EnhancedController is not None:
        # 创建增强型控制器实例
        enhanced_controller_instance = EnhancedController()
        
        # 创建横向控制器包装类，实现标准接口并使用增强型功能
        class LatMPC(LateralMpc):
            """
            横向控制器包装类
            如果增强型控制器可用且系统验证通过，使用增强型控制器
            否则回退到标准控制器
            """
            def __init__(self, x0=None):
                # 初始化标准控制器作为备用
                super().__init__(x0)
                # 保存增强型控制器实例
                self.enhanced = enhanced_controller_instance
                print("已初始化横向增强型控制器")
            
            def update(self, state, path):
                """更新控制器状态"""
                # 尝试使用增强型控制器
                enhanced_result = self.enhanced.update(state, path)
                
                # 如果增强型控制器返回None（验证失败）或发生异常，使用标准控制器
                if enhanced_result is None:
                    # 使用标准控制器
                    return super().update(state, path)
                
                return enhanced_result
        
        print("启用增强型横向控制器")
    else:
        print("加载增强型横向控制器失败，使用标准控制器")
        # 使用标准控制器
        LatMPC = LateralMpc
else:
    print("未授权使用增强型横向控制器，使用标准控制器")
    # 使用标准控制器
    LatMPC = LateralMpc
