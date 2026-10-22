"""统一异常类型。所有异常都继承 RestElectricError，CLI 按类型映射退出码。"""


class RestElectricError(Exception):
    """项目内所有异常的基类。"""


class ConfigError(RestElectricError):
    """配置文件缺失、字段缺失或类型错误。"""


class TransportError(RestElectricError):
    """网络层失败：超时、DNS、连接被拒、重试耗尽。"""


class ProtocolError(RestElectricError):
    """接口返回成功但内容不符合预期：字段缺失、金额不是数字、JSON 解析失败。"""


class AuthExpiredError(RestElectricError):
    """登录态失效：401/403 或接口返回的未登录业务码。需要人工续 token。"""


class NotifierError(RestElectricError):
    """推送通道配置缺失或发送失败。"""
