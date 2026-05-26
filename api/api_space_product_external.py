"""
空间产品对外接口
"""
import os
import time
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiSpaceProductFeign:
    def __init__(self, client: ApiClient):
        self.client = client

    # 空间分页
    def space_page(self, name=None, userId=None, spaceStatus=None, spaceType=None, spaceIds=None,
                   pageIndex=1, pageSize=10):
        url = f"{env}/feign/space/page"
        payload = {
            "data": {
                "name": name,
                "userId": userId,
                "spaceStatus": spaceStatus,
                "spaceType": spaceType,
                "spaceIds": spaceIds or [],
            },
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 空间查询
    def space_list(self, name=None, userId=None, spaceStatus=None, spaceType=None, spaceIds=None):
        url = f"{env}/feign/space/list"
        payload = {
            "name": name,
            "userId": userId,
            "spaceStatus": spaceStatus,
            "spaceType": spaceType,
            "spaceIds": spaceIds or [],
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 产品查询
    def product_list(self, spaceManageId):
        url = f"{env}/feign/product/list"
        payload = {"spaceManageId": spaceManageId}
        response = self.client.post_with_retry(url, json=payload)
        return response
