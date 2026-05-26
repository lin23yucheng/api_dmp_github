"""
机器设备对外接口
"""
import os
import time
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiMachineFeign:
    def __init__(self, client: ApiClient):
        self.client = client

    # 查询所有机器
    def all_machine(self, spaceId):
        url = f"{env}/feign/machine/{spaceId}"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 查询机器设备加密相关信息
    def get_machine_encrypt(self, cloudDeviceNo):
        url = f"{env}/feign/machine/encrypt/{cloudDeviceNo}"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response
