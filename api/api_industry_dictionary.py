"""
行业字典表管理接口
"""
import os
import time
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiIndustryDictionary:
    def __init__(self, client: ApiClient):
        self.client = client

    # 查询全部行业字典
    def select_all(self):
        url = f"{env}/industrydictionary/selectAll"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response
