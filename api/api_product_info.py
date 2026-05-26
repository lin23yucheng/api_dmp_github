"""
产品信息管理接口
"""
import os
import time
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiProductInfo:
    def __init__(self, client: ApiClient):
        self.client = client

    # 修改产品
    def update(self, spaceManageId, productInfoId, showName, spaceId=None):
        url = f"{env}/productinfo/update"
        payload = {
            "spaceManageId": spaceManageId,
            "spaceId": spaceId,
            "productInfoId": productInfoId,
            "showName": showName,
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 产品统计数量查询
    def statistics_num(self, spaceManageId, productInfoId=None):
        url = f"{env}/productinfo/statisticsNum"
        payload = {"spaceManageId": spaceManageId, "productInfoId": productInfoId}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 产品下拉
    def select(self, spaceManageId, ignoreProductInfoIdList=None, pageIndex=1, pageSize=10):
        url = f"{env}/productinfo/select"
        payload = {
            "data": {
                "spaceManageId": spaceManageId,
                "ignoreProductInfoIdList": ignoreProductInfoIdList or [],
            },
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 分页列表
    def page(self, spaceManageId, source=None, queryText=None, pageIndex=1, pageSize=10):
        url = f"{env}/productinfo/page"
        payload = {
            "data": {"spaceManageId": spaceManageId, "source": source, "queryText": queryText},
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 删除产品
    def delete(self, productInfoId):
        url = f"{env}/productinfo/delete/{productInfoId}"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 新增产品
    def add(self, spaceManageId, productCode, showName, referenceProductCode=None, copyLabel=None):
        url = f"{env}/productinfo/add"
        payload = {
            "spaceManageId": spaceManageId,
            "productCode": productCode,
            "showName": showName,
            "referenceProductCode": referenceProductCode,
            "copyLabel": copyLabel,
        }
        response = self.client.post_with_retry(url, json=payload)
        return response
