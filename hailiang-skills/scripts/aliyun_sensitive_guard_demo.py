# coding=utf-8
# python version >= 3.6
from alibabacloud_green20220302.client import Client
from alibabacloud_green20220302 import models
from alibabacloud_tea_openapi.models import Config
import json
import os

config = Config(
    # 阿里云账号AccessKey拥有所有API的访问权限，建议您使用RAM用户进行API访问或日常运维。
    # 强烈建议不要把AccessKey ID和AccessKey Secret保存到工程代码里，否则可能导致AccessKey泄露，威胁您账号下所有资源的安全。
    # 常见获取环境变量方式：
    # 获取RAM用户AccessKey ID：os.environ['ALIBABA_CLOUD_ACCESS_KEY_ID']
    # 获取RAM用户AccessKey Secret：os.environ['ALIBABA_CLOUD_ACCESS_KEY_SECRET']
    access_key_id=os.environ['ALIBABA_CLOUD_ACCESS_KEY_ID'],
    access_key_secret=os.environ['ALIBABA_CLOUD_ACCESS_KEY_SECRET'],
    # 连接超时时间 单位毫秒(ms)
    connect_timeout=10000,
    # 读超时时间 单位毫秒(ms)
    read_timeout=3000,
    region_id='cn-hangzhou',
    endpoint='green-cip.cn-hangzhou.aliyuncs.com'
)

clt = Client(config)

serviceParameters = {
    'content': '张三的电话是19550176155，男，身材魁梧，喜好游泳, 这个用户住在杭州'
}
textModerationPlusRequest = models.TextModerationPlusRequest(
    # 检测类型
    # AI 输入内容安全检测（query_security_check）
    # AI 生成内容安全检测（response_security_check）
    service='response_security_check',
    service_parameters=json.dumps(serviceParameters)
)

try:
    response = clt.text_moderation_plus(textModerationPlusRequest)
    if response.status_code == 200:
        # 调用成功
        result = response.body
        print('response success. result:{}'.format(result))
    else:
        print('response not success. status:{} ,result:{}'.format(response.status_code, response))
except Exception as err:
    print(err)
