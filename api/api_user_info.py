"""
用户信息管理接口
"""
import os
import time
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiUserInfo:
    def __init__(self, client: ApiClient):
        self.client = client

    # 查询空间下的成员列表
    def query_space_users(self, spaceManageId=None, pageIndex=1, pageSize=10):
        url = f"{env}/userinfo/querySpaceUsers"
        payload = {
            "data": {"spaceManageId": spaceManageId},
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 通过SystemId查询空间下的成员列表
    def query_space_users_by_system_id(self, other_system_id, spaceManageId=None, pageIndex=1, pageSize=10):
        url = f"{env}/userinfo/querySpaceUsersBySystemId"
        payload = {
            "data": {"spaceManageId": spaceManageId},
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        headers = {"other-system-id": other_system_id}
        response = self.client.post_with_retry(url, json=payload, headers=headers)
        return response

    # 手机号或用户名或姓名模糊查询用户
    def query_by_phone_or_username(self, phone=None, spaceManageId=None, pageIndex=1, pageSize=10):
        url = f"{env}/userinfo/queryByPhone"
        payload = {
            "data": {"phone": phone, "spaceManageId": spaceManageId},
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 根据一批用户id集合查询用户集合
    def query_by_ids(self, userIds):
        url = f"{env}/userinfo/queryByIds"
        payload = {"userIds": userIds}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 根据角色id查询对应的角色名称
    def get_user_name_map(self, userIds):
        url = f"{env}/userinfo/getUserNameMap"
        payload = {"userIds": userIds}
        response = self.client.post_with_retry(url, json=payload)
        return response
