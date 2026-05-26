"""
初始化接口
"""
import os
import time
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiRefresh:
    def __init__(self, client: ApiClient):
        self.client = client

    # 初始化空间、产品、用户
    def refresh_all(self):
        url = f"{env}/refresh/all"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response
