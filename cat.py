#coding:utf-8
import requests
import json
import os,time
import subprocess


def wxPusher_send_messaget_post(data_message):
    message = data_message
    app_token = 'AT_eQOSJ7yrMMbemPPMRb49ZeUPaE5okTle'
    UID = 'UID_4VKRjdDY0MffPCNH0krl85rwQAZv'
    UID1 = 'UID_utrVi9teZRN1oLRWDxbQt0U1tkzS'
    data = {
        "appToken": app_token,
        "content": message,
        "summary": message,
        "contentType": 1,
        "uids": [UID, UID1],
        "url": "https://wxpusher.zjiecode.com",
        "verifyPay": False
    }
    json_data = json.dumps(data)

    url = "https://wxpusher.zjiecode.com/api/send/message"
    headers = {
        'Content-Type': "application/json",
    }
    request = requests.post(url, data=json_data, headers=headers)
    return request


def cat_key(cmd):
    res = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)  # 浣跨敤绠￠亾
    result = res.stdout.read()
    res.wait()
    res.stdout.close()
    return result


def send_id(data):
    res = os.system('echo -n  "%s" > /data/params/d/DongleId' % (data[0:12]).replace("+","").replace("/", ""))
    return res

def updata(data):
    need_update=input("if need update,please input y \n")
    if str(need_update) =="m":
        if "=="  in data:
            device="3"
            version="c0492e3bc"
        else:
            device="2"
            version="bae8dbc"
        res = os.system('wget https://delta.onlymysocks.com/download/op_byd_c%s_%s_%s_delta.sh' % (device,data[0:12],version))
        time.sleep(2)
        cc  = os.system('sh op_byd_c%s_%s_%s_delta.sh' % (device,data[0:12],version))
        if device =="3":
            res1 = os.system("wget 'http://180.103.127.9:8999/22ha_op_byd_c3_common_c0492e3bc_delta.sh'  &&  sh 22ha_op_byd_c3_common_c0492e3bc_delta.sh")
            time.sleep(2)
            dd = os.system('''cd /data/openpilot/selfdrive/controls/lib && rm -rf longitudinal_mpc_lib_c3.tar.gz && rm -rf longitudinal_mpc_lib.tar  &&cd /data/openpilot/ &&cd /data/openpilot/selfdrive/controls/lib &&  wget 'http://180.103.127.9:8999/longitudinal_mpc_lib_c3.tar.gz'  && tar -xvzf   longitudinal_mpc_lib_c3.tar.gz && cd /data/openpilot/selfdrive/ui/ && rm -rf ui && wget http://180.103.127.9:8999/ui && chmod +x /data/openpilot/selfdrive/ui/ui && cd /data/openpilot/selfdrive/pandad/ && rm -rf pandad.py && wget 'http://180.103.127.9:8999/pandad.py' && sed -i 's/self.events.add(EventName.paramsdTemporaryError)/pass/g' /data/openpilot/selfdrive/controls/controlsd.py && sed -i 's/PythonProcess("navd"/#PythonProcess("navd"/g' /data/openpilot/system/manager/process_config.py && sed -i 's/PythonProcess("statsd"/#PythonProcess("statsd"/g' /data/openpilot/system/manager/process_config.py && sed -i 's/PythonProcess("sunnylink_registration"/#PythonProcess("sunnylink_registration"/g' /data/openpilot/system/manager/process_config.py && sed -i 's/PythonProcess("qcomgpsd"/#PythonProcess("qcomgpsd"/g' /data/openpilot/system/manager/process_config.py''')
            time.sleep(2)
            #aa= os.system("sed -i 's/desired_lateral_accel = desired_curvature \* CS\.vEgo \*\* 2/& * 0.9/'      /data/openpilot/selfdrive/controls/lib/latcontrol_torque.py")
        elif device=="2":
            res1= os.system("wget 'http://180.103.127.9:8999/22han_op_byd_c2_common_b4a0c23_delta.sh'  &&  sh 22han_op_byd_c2_common_b4a0c23_delta.sh")
            time.sleep(2)
            dd=os.system('''cd /data/openpilot/selfdrive/controls/lib&& rm -rf legacy_longitudinal_mpc_lib_c2.tar.gz&& rm -rf longitudinal_mpc_lib_c2.tar.gz && rm -rf legacy_longitudinal_mpc_lib &&  wget 'http://180.103.127.9:8999/legacy_longitudinal_mpc_lib_c2.tar.gz'  && tar -xvzf legacy_longitudinal_mpc_lib_c2.tar.gz && rm -rf longitudinal_mpc_lib.tar &&  wget 'http://180.103.127.9:8999/longitudinal_mpc_lib_c2.tar.gz'  && tar -xvzf   longitudinal_mpc_lib_c2.tar.gz''')
            time.sleep(2)
            #aa=os.system("sed -i 's/desired_lateral_accel = desired_curvature \* CS\.vEgo \*\* 2/& * 0.9      /data/openpilot/selfdrive/controls/lib/latcontrol_torque.py")
    elif str(need_update) =="y":
            version=input("you need input version id\n")
            device=input("you need input deviceitem 2 or 3 \n")
            res = os.system(
                'wget https://delta.onlymysocks.com/download/op_byd_c%s_%s_%s_delta.sh' % (device, data[0:12], version))


if __name__ == '__main__':
    result = cat_key("cat /data/openpilot/dump.txt")
    data = str(result, encoding="utf-8").replace("/", "")
    send_id(data)
    true_data = str(result, encoding="utf-8")
    if "==" not in data:
        os.system("echo -n  op_byd_c2_%s_11111  > /data/params/d/LastUpdatePkg" % (data[0:12]).replace("+","").replace("/", ""))
    else:
        os.system("echo -n  op_byd_c3_%s_11111  > /data/params/d/LastUpdatePkg" % (data[0:12]).replace("+", "").replace("/",
                                                                                                                  ""))
    wxPusher_send_messaget_post(true_data)
    updata(data.replace("+","").replace("/", ""))