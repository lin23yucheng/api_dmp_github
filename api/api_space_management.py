"""
空间管理管理接口
"""
import os
import time
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiSpaceManage:
    def __init__(self, client: ApiClient):
        self.client = client

    # 修改空间管理
    def update_space(self, spaceName, spaceCnName, spaceType, industryId, spaceExplain, spaceId):
        url = f"{env}/spacemanage/update"
        payload = {
            "spaceName": spaceName,
            "spaceCnName": spaceCnName,
            "spaceType": spaceType,
            "industryId": industryId,
            "spaceExplain": spaceExplain,
            "spaceId": spaceId,
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 查询项目类型统计
    def statistics_type(self, spaceStatus=None):
        url = f"{env}/spacemanage/statistics/type"
        payload = {"spaceStatus": spaceStatus}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 查询近12个月项目开展趋势
    def statistics_time(self, spaceType=None):
        url = f"{env}/spacemanage/statistics/time"
        payload = {"spaceType": spaceType}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 查询行业分布
    def statistics_industry(self):
        url = f"{env}/spacemanage/statistics/industry"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 查询最近项目
    def statistics_active(self):
        url = f"{env}/spacemanage/statistics/active"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 通过用户编号获取空间ID列表
    def select_ids_by_user_id(self, spaceName=None, spaceType=None, showUnsettle=None, showSelf=None, userId=None):
        url = f"{env}/spacemanage/selectIdsByUserId"
        payload = {
            "spaceName": spaceName,
            "spaceType": spaceType,
            "showUnsettle": showUnsettle,
            "showSelf": showSelf,
            "userId": userId,
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 重启
    def restart(self, spaceManageId):
        url = f"{env}/spacemanage/restart/{spaceManageId}"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 分页查询
    def page(self, spaceName=None, spaceType=None, spaceStatus=None, pageIndex=1, pageSize=8):
        url = f"{env}/spacemanage/page"
        payload = {
            "data": {"spaceName": spaceName, "spaceType": spaceType, "spaceStatus": spaceStatus},
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 通过用户编号分页查询
    def page_by_user_id(self, spaceName=None, spaceType=None, showUnsettle=None, showSelf=None, userId=None,
                        pageIndex=1, pageSize=10):
        url = f"{env}/spacemanage/pageByUserId"
        payload = {
            "data": {
                "spaceName": spaceName,
                "spaceType": spaceType,
                "showUnsettle": showUnsettle,
                "showSelf": showSelf,
                "userId": userId,
            },
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 结项
    def finish(self, spaceManageId):
        url = f"{env}/spacemanage/finish/{spaceManageId}"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 项目详情
    def detail(self, spaceManageId):
        url = f"{env}/spacemanage/detail/{spaceManageId}"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 删除空间成员
    def del_space_user(self, spaceManageId, userId=None, userIds=None):
        url = f"{env}/spacemanage/delSpaceuser"
        payload = {"spaceManageId": spaceManageId, "userId": userId, "userIds": userIds or []}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 新增空间管理
    def add(self, spaceName, spaceCnName, spaceType, industryId, spaceExplain=None):
        url = f"{env}/spacemanage/add"
        payload = {
            "spaceName": spaceName,
            "spaceCnName": spaceCnName,
            "spaceType": spaceType,
            "industryId": industryId,
            "spaceExplain": spaceExplain,
        }
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 添加空间成员
    def add_space_user(self, spaceManageId, userIds):
        url = f"{env}/spacemanage/addSpaceUser"
        payload = {"spaceManageId": spaceManageId, "userIds": userIds}
        response = self.client.post_with_retry(url, json=payload)
        return response
