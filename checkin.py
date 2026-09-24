import random
import sys
import time

import requests
import os
from datetime import date
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict
from pypushdeer import PushDeer
from logging_config import init_logger


# 对齐真实浏览器的 User-Agent（GLaDOS 现按请求特征做反自动化/设备绑定校验）
# 默认 macOS Chrome，可用环境变量 GLADOS_USER_AGENT 覆盖为登录该账号时浏览器的 UA
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
USER_AGENT = os.environ.get("GLADOS_USER_AGENT", DEFAULT_USER_AGENT)


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    FAILURE = -2


class ExchangePlan(Enum):
    """兑换计划"""

    PLAN100 = "plan100"
    PLAN200 = "plan200"
    PLAN500 = "plan500"
    NONE = "none"


class APIEndpoint(Enum):
    """API端点"""

    CHECKIN = "/api/user/checkin"
    STATUS = "/api/user/status"
    POINTS = "/api/user/points"
    EXCHANGE = "/api/user/exchange"


class LogEmoji:
    """日志 Emoji 常量"""

    SUCCESS = "✅"
    FAIL = "❌"
    REPEAT = "🔄"
    CHECKIN = "🎫"
    STATUS = "📊"
    POINTS = "💰"
    EXCHANGE = "🎁"
    START = "🚀"
    END = "🏁"
    COOKIE = "🍪"
    DOMAIN = "🌐"
    WARNING = "⚠️ "
    ERROR = "🔴"
    INFO = "ℹ️ "


def log_method(func):
    """日志装饰器"""

    def wrapper(self, *args, **kwargs):
        method_name = func.__name__
        emoji_map = {
            "checkin": LogEmoji.CHECKIN,
            "get_status": LogEmoji.STATUS,
            "get_points": LogEmoji.POINTS,
            "exchange": LogEmoji.EXCHANGE,
        }
        emoji = emoji_map.get(method_name, LogEmoji.INFO)
        try:
            result = func(self, *args, **kwargs)
            return result
        except Exception as e:
            logger.error(f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] {LogEmoji.ERROR} {method_name} 执行失败: {e}")

            DEFAULT_ERRORS = {
                "checkin": {"status": "签到失败", "points": "0", "message": ""},
                "get_status": ("None 天", -2),
                "get_points": ("None 积分", 0),
                "exchange": "",
            }

            if method_name in DEFAULT_ERRORS:
                error_template = DEFAULT_ERRORS[method_name]
                if isinstance(error_template, dict):
                    error_result = error_template.copy()
                    error_result["message"] = f"执行失败: {e}"
                    return error_result
                return error_template
            raise

    return wrapper


class Config:
    """应用配置"""

    ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"
    ENV_EXCHANGE_PLANS = "GLADOS_EXCHANGE_PLANS"
    ENV_EXCHANGE_INTERVAL = "GLADOS_EXCHANGE_INTERVAL"
    ENV_VERBOSE = "GLADOS_VERBOSE"

    """默认兑换计划"""
    DEFAULT_EXCHANGE_PLAN = "plan500"

    """默认兑换间隔（天），非天天尝试兑换"""
    DEFAULT_EXCHANGE_INTERVAL = 3

    """默认是否输出详细响应"""
    DEFAULT_VERBOSE = False

    """GLaDOS 域名（同一账号体系，作为故障转移链，优先第一个）"""
    GLADOS_DOMAINS = [
        "glados.cloud",
        "glados.one",
        "glados.rocks",
        "glados.network",
        "glados.space",
    ]

    """Railgun 域名（独立账号体系）"""
    RAILGUN_DOMAINS = ["railgun.info"]

    """多 Cookie 之间签到的最大随机间隔（秒）"""
    COOKIE_SLEEP_MAX = 15

    """全部域名"""
    DOMAINS = GLADOS_DOMAINS + RAILGUN_DOMAINS

    """兑换计划列表"""
    EXCHANGE_PLANS = {
        ExchangePlan.PLAN100.value: 100,
        ExchangePlan.PLAN200.value: 200,
        ExchangePlan.PLAN500.value: 500,
        ExchangePlan.NONE.value: 0,
    }

    def __init__(self):
        self.push_key: str = ""
        self.cookies_list: List[str] = []
        self.exchange_plan: str = self.DEFAULT_EXCHANGE_PLAN
        self.exchange_plans: List[str] = []
        self.exchange_interval: int = self.DEFAULT_EXCHANGE_INTERVAL
        self.exchange_due: bool = False
        self.verbose: bool = self.DEFAULT_VERBOSE
        self._load_config()

    def _load_config(self) -> None:
        """加载配置"""
        push_key_env: Optional[str] = os.environ.get(self.ENV_PUSH_KEY)
        raw_cookies_env: Optional[str] = os.environ.get(self.ENV_COOKIES)
        exchange_plan_env: Optional[str] = os.environ.get(self.ENV_EXCHANGE_PLAN)
        exchange_plans_env: Optional[str] = os.environ.get(self.ENV_EXCHANGE_PLANS)
        exchange_interval_env: Optional[str] = os.environ.get(self.ENV_EXCHANGE_INTERVAL)
        verbose_env: Optional[str] = os.environ.get(self.ENV_VERBOSE)

        if not push_key_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_PUSH_KEY}' 未设置。")
            self.push_key = ""
        else:
            self.push_key = push_key_env

        if not raw_cookies_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_COOKIES}' 未设置。")
            self.cookies_list = []
        else:
            self.cookies_list = [cookie.strip() for cookie in raw_cookies_env.split("&") if cookie.strip()]
            if not self.cookies_list:
                raise ValueError(f"环境变量 '{self.ENV_COOKIES}' 已设置，但未包含任何有效的 Cookie。")

        if not exchange_plan_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 未设置，将使用默认兑换计划 {self.DEFAULT_EXCHANGE_PLAN}。")
            self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN
        else:
            if exchange_plan_env in self.EXCHANGE_PLANS:
                self.exchange_plan = exchange_plan_env
                logger.info(f"{LogEmoji.SUCCESS} 使用指定的兑换计划: {self.exchange_plan}")
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 的值 '{exchange_plan_env}' 无效，将使用默认兑换计划 {self.DEFAULT_EXCHANGE_PLAN}。")
                self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN

        # 按账号配置兑换计划：& 分隔，与 GLADOS_COOKIES 中的 Cookie 按顺序一一对应
        if not exchange_plans_env:
            logger.info(f"{LogEmoji.INFO} 环境变量 '{self.ENV_EXCHANGE_PLANS}' 未设置，所有账号使用统一兑换计划 {self.exchange_plan}。")
        else:
            raw_plans = exchange_plans_env.split("&")
            self.exchange_plans = []
            for idx, raw_plan in enumerate(raw_plans, 1):
                plan = raw_plan.strip()
                if plan in self.EXCHANGE_PLANS:
                    self.exchange_plans.append(plan)
                else:
                    logger.warning(
                        f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLANS}' 第 {idx} 个账号的值 '{raw_plan}' 无效，"
                        f"该账号将使用全局兑换计划 {self.exchange_plan}。"
                    )
                    self.exchange_plans.append(self.exchange_plan)
            if len(self.exchange_plans) > len(self.cookies_list):
                logger.warning(
                    f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLANS}' 的账号数 ({len(self.exchange_plans)}) "
                    f"多于 Cookie 数 ({len(self.cookies_list)})，多余部分将被忽略。"
                )
            elif self.exchange_plans and len(self.exchange_plans) < len(self.cookies_list):
                logger.warning(
                    f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLANS}' 的账号数 ({len(self.exchange_plans)}) "
                    f"少于 Cookie 数 ({len(self.cookies_list)})，缺失的账号将使用全局兑换计划 {self.exchange_plan}。"
                )
            if self.exchange_plans:
                logger.info(
                    f"{LogEmoji.INFO} 各账号兑换计划: "
                    + ", ".join(f"#{i + 1}={p}" for i, p in enumerate(self.exchange_plans))
                    + f"（其余账号使用 {self.exchange_plan}）"
                )

        logger.info(f"{LogEmoji.INFO} 共加载了 {len(self.cookies_list)} 个 Cookie 用于签到。")
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_PUSH_KEY} {'已设置' if push_key_env else '未设置'}。")
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_EXCHANGE_PLAN}: {self.exchange_plan}。")

        if exchange_interval_env is not None:
            try:
                interval = int(exchange_interval_env)
                if interval >= 1:
                    self.exchange_interval = interval
                    logger.info(f"{LogEmoji.SUCCESS} 使用指定的兑换间隔: {self.exchange_interval} 天")
                else:
                    logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_INTERVAL}' 必须 >= 1，将使用默认间隔 {self.DEFAULT_EXCHANGE_INTERVAL} 天。")
            except ValueError:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_INTERVAL}' 的值 '{exchange_interval_env}' 无效，将使用默认间隔 {self.DEFAULT_EXCHANGE_INTERVAL} 天。")

        logger.info(f"{LogEmoji.INFO} 当前兑换间隔: {self.exchange_interval} 天。")

        if verbose_env is not None:
            verbose_env_lower = verbose_env.lower()
            if verbose_env_lower in ["true", "1", "yes", "y"]:
                self.verbose = True
            elif verbose_env_lower in ["false", "0", "no", "n"]:
                self.verbose = False
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_VERBOSE}' 的值 '{verbose_env}' 无效，将使用默认值 {self.DEFAULT_VERBOSE}。")

        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_VERBOSE}: {self.verbose}。")

    def get_exchange_plan(self, cookie_idx: int) -> str:
        """获取指定账号（1 起始）的兑换计划，未单独配置时回落全局计划"""
        if 1 <= cookie_idx <= len(self.exchange_plans):
            return self.exchange_plans[cookie_idx - 1]
        return self.exchange_plan

    def is_exchange_due(self) -> bool:
        """判断今天是否轮到尝试兑换（每 N 天一次，基于日期序号取模）"""
        return date.today().toordinal() % self.exchange_interval == 0


class API:
    """API 调用"""

    CHECKIN_URL = APIEndpoint.CHECKIN.value
    STATUS_URL = APIEndpoint.STATUS.value
    POINTS_URL = APIEndpoint.POINTS.value
    EXCHANGE_URL = APIEndpoint.EXCHANGE.value

    def __init__(self, domain: str, cookie_index: int = 0, verbose: bool = False):
        self.domain: str = domain
        self.cookie_index: int = cookie_index
        self.verbose: bool = verbose
        self.headers: Dict[str, str] = self._get_headers()
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def close(self) -> None:
        """关闭 session"""
        if hasattr(self, "session"):
            try:
                self.session.close()
            except Exception as e:
                logger.error(f"{LogEmoji.ERROR} 关闭 session 时发生错误: {e}")

    def __enter__(self):
        """进入上下文管理器"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器"""
        self.close()
        return False

    def _get_headers(self) -> Dict[str, str]:
        """获取请求头（对齐真实浏览器同源 XHR 的请求特征）"""
        return {
            "origin": f"https://{self.domain}",
            "referer": f"https://{self.domain}/console/checkin",
            "user-agent": USER_AGENT,
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

    def _log(self, level: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        log_message = f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] {emoji} {message}"

        if force or self.verbose:
            if level == "info":
                logger.info(log_message)
            elif level == "warning":
                logger.warning(log_message)
            elif level == "error":
                logger.error(log_message)

    def _get_full_url(self, path: str) -> str:
        """获取完整 URL"""
        return f"https://{self.domain}{path}"

    def _make_request(self, url: str, method: str, data: Optional[Dict] = None, cookies: str = "") -> Optional[requests.Response]:
        """发送 HTTP 请求"""
        session_headers = self.headers.copy()
        session_headers["cookie"] = cookies

        try:
            if method.upper() == "POST":
                response = self.session.post(url, headers=session_headers, data=data, timeout=(60, 120))
            elif method.upper() == "GET":
                response = self.session.get(url, headers=session_headers, timeout=(60, 120))
            else:
                self._log("error", LogEmoji.ERROR, f"不支持的 HTTP 方法: {method}", force=True)
                return None

            if not response.ok:
                self._log("warning", LogEmoji.WARNING, f"向 {url} 发起的请求失败，状态码 {response.status_code}。响应内容: {response.text}", force=True)
                return None
            return response
        except requests.exceptions.RequestException as e:
            self._log("error", LogEmoji.ERROR, f"向 {url} 发起请求时发生网络错误: {e}", force=True)
            return None

    def _get_checkin_data(self) -> Dict[str, str]:
        """获取签到数据"""
        return {"token": self.domain}

    @log_method
    def checkin(self, cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """执行签到"""
        url = self._get_full_url(self.CHECKIN_URL)
        checkin_data = self._get_checkin_data()
        response = self._make_request(url, "POST", checkin_data, cookies)

        result = {
            "status": "签到失败",
            "points": "0",
            "message": "",
            "code": CheckinStatus.FAILURE,
        }

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "无消息字段")
            points = str(data.get("points", 0))

            if code == CheckinStatus.SUCCESS.value:
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points}, message : {message} }}")
                result["code"] = CheckinStatus.SUCCESS
                result["status"] = "签到成功"
                result["points"] = points
                result["message"] = message
            elif code == CheckinStatus.REPEAT.value:
                self._log("info", LogEmoji.REPEAT, f"{{ code : {code}, message : {message} }}", force=True)
                result["code"] = CheckinStatus.REPEAT
                result["status"] = "重复签到"
                result["points"] = "0"
                result["message"] = message
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
                result["code"] = CheckinStatus.FAILURE
                result["status"] = "签到失败"
                result["points"] = "0"
                result["message"] = message
        else:
            self._log("warning", LogEmoji.WARNING, "签到失败", force=True)
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["message"] = "网络请求失败"

        return result

    @log_method
    def get_status(self, cookies: str, probe: bool = False) -> Tuple[str, int]:
        """获取状态（probe=True 时用于 cookie 体系探测，输出中性日志）"""

        url = self._get_full_url(self.STATUS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            left_days = data.get("data", {}).get("leftDays", None)

            if left_days is not None:
                left_days_int = int(float(left_days))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, leftDays : {left_days_int} 天}}")
                return f"{left_days_int} 天", code
            else:
                if probe:
                    self._log("info", LogEmoji.INFO, f"{{ code : {code}, leftDays : {left_days} 天}} (探测: 非本体系)", force=True)
                else:
                    self._log("info", LogEmoji.FAIL, f"{{ code : {code}, leftDays : {left_days} 天}}", force=True)
                return "None 天", code
        else:
            self._log("warning", LogEmoji.WARNING, "获取状态失败", force=True)
            return "None 天", -2

    @log_method
    def get_points(self, cookies: str) -> Tuple[str, int, str]:
        """获取积分（含连续签到天数）"""
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            points = data.get("points", None)
            streak = data.get("streak", None)
            streak_str = str(streak) if streak is not None else "-"

            if points is not None:
                points_int = int(float(points))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points_int} 积分, streak : {streak_str} }}")
                points_str = f"{points_int} 积分"
                points_num = points_int
                return points_str, points_num, streak_str
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, points : {points} 积分}}", force=True)
                return "None 积分", 0, streak_str
        else:
            self._log("warning", LogEmoji.WARNING, "获取积分失败", force=True)
            return "None 积分", 0, "-"

    @log_method
    def exchange(self, cookies: str, plan: str, required_points: int) -> str:
        """执行兑换"""
        url = self._get_full_url(self.EXCHANGE_URL)
        response = self._make_request(url, "POST", {"planType": plan}, cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "未知错误")

            if code == 0:
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, message : {message} }}")
                return f"兑换成功: {plan}"
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
                return f"兑换失败: {message}"
        else:
            self._log("warning", LogEmoji.WARNING, "兑换失败", force=True)
            return "兑换失败"


@dataclass()
class CheckinResult:
    """签到结果"""

    cookie_index: int
    domain: str
    status: str = "签到失败"
    points: str = "0"
    days: str = "None"
    points_total: str = "None"
    streak: str = "-"
    exchange: str = "未兑换"
    code: CheckinStatus = CheckinStatus.FAILURE  # 0: 成功, 1: 重复, -2: 失败

    def to_dict(self) -> Dict[str, Union[str, CheckinStatus]]:
        result_dict = asdict(self)
        return result_dict


class PushService:
    """推送服务"""

    def __init__(self, config: Config):
        self.config = config

    def send(self, title: str, content: str) -> bool:
        """发送推送"""
        if not self.config.push_key:
            logger.info(f"{LogEmoji.WARNING} 未设置推送密钥，跳过推送通知。")
            return False

        try:
            pushdeer = PushDeer(pushkey=self.config.push_key)
            pushdeer.send_text(title, desp=content)
            logger.info(f"{LogEmoji.SUCCESS} 推送通知发送成功。")
            return True
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} 发送推送通知失败: {e}")
            return False


class Checker:
    """签到"""

    def __init__(self, config: Config):
        self.config = config
        self.results = []

    def _log(self, cookie_idx: int, domain: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        if self.config.verbose or force:
            logger.info(f"{LogEmoji.COOKIE}[{cookie_idx}] {LogEmoji.DOMAIN}[{domain}] {emoji} {message}")

    def _probe_cookie(self, cookie: str, cookie_idx: int) -> Tuple[bool, bool]:
        """探测 cookie 属于哪个账号体系：GLaDOS 域名返回 code=0，Railgun 返回 No permission"""
        is_glados, is_railgun = False, False

        with API(self.config.GLADOS_DOMAINS[0], cookie_idx, verbose=self.config.verbose) as api:
            _, code = api.get_status(cookie, probe=True)
            is_glados = code == 0
            self._log(cookie_idx, self.config.GLADOS_DOMAINS[0], LogEmoji.STATUS,
                      f"探测 cookie 体系: GLaDOS -> {'命中' if is_glados else '未命中'}", force=True)

        with API(self.config.RAILGUN_DOMAINS[0], cookie_idx, verbose=self.config.verbose) as api:
            _, code = api.get_status(cookie, probe=True)
            is_railgun = code == 0
            self._log(cookie_idx, self.config.RAILGUN_DOMAINS[0], LogEmoji.STATUS,
                      f"探测 cookie 体系: Railgun -> {'命中' if is_railgun else '未命中'}", force=True)

        return is_glados, is_railgun

    def checkin_all(self):
        """执行所有签到任务（先探测 cookie 所属体系；GLaDOS 域名作为故障转移链）"""
        cookie_count = len(self.config.cookies_list)
        logger.info(
            f"{LogEmoji.INFO} 共 {cookie_count} 个 Cookie, "
            f"GLaDOS 域名 {len(self.config.GLADOS_DOMAINS)} 个, Railgun 域名 {len(self.config.RAILGUN_DOMAINS)} 个"
        )

        for cookie_idx, cookie in enumerate(self.config.cookies_list, 1):
            if cookie_idx > 1:
                delay = random.uniform(0, self.config.COOKIE_SLEEP_MAX)
                logger.info(f"{LogEmoji.INFO} 随机等待 {delay:.1f} 秒后处理下一个 Cookie...")
                time.sleep(delay)

            logger.info(f"{LogEmoji.START} ========== 开始处理 Cookie {cookie_idx} ==========")

            is_glados, is_railgun = self._probe_cookie(cookie, cookie_idx)
            if is_glados:
                domains = self.config.GLADOS_DOMAINS
                sys_name = "GLaDOS"
            elif is_railgun:
                domains = self.config.RAILGUN_DOMAINS
                sys_name = "Railgun"
            else:
                domains = self.config.DOMAINS
                sys_name = "未知(全部尝试)"
            self._log(cookie_idx, "-", LogEmoji.INFO, f"该 Cookie 归属: {sys_name}", force=True)

            # GLaDOS 域名作为故障转移链：签到成功/重复即停，避免多余的重复与失败噪音
            result = None
            for domain in domains:
                self._log(cookie_idx, domain, LogEmoji.INFO, f"尝试签到于 {domain}", force=True)
                result = self._checkin_on_domain(cookie, cookie_idx, domain)
                if result.code in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT):
                    self._log(cookie_idx, domain, LogEmoji.INFO, "该域名已受理签到，停止故障转移", force=True)
                    break

            if result is not None:
                self.results.append(result)
                result_message = f"结果: {result.status} @ {result.domain}"
                if result.code == CheckinStatus.SUCCESS:
                    if self.config.verbose:
                        result_message = f"结果: {result.status}, 获得 {result.points} 积分, 剩余 {result.days}, 总 {result.points_total}, {result.exchange}"
                    self._log(cookie_idx, result.domain, LogEmoji.SUCCESS, result_message, force=True)
                else:
                    self._log(cookie_idx, result.domain, LogEmoji.WARNING, result_message, force=True)

    def _checkin_on_domain(self, cookie: str, cookie_idx: int, domain: str) -> CheckinResult:
        result = CheckinResult(cookie_idx, domain)

        with API(domain, cookie_idx, verbose=self.config.verbose) as api:
            # 1. 获取状态
            self._log(cookie_idx, domain, LogEmoji.STATUS, "查询剩余天数")
            days_str, status_code = api.get_status(cookie)
            result.days = days_str

            # 2. 签到
            self._log(cookie_idx, domain, LogEmoji.CHECKIN, "执行签到")
            checkin_result = api.checkin(cookie)
            result.status = checkin_result["status"]
            result.code = checkin_result.get("code", CheckinStatus.FAILURE)
            result.points = str(checkin_result.get("points", "0"))

            # 3. 获取积分
            self._log(cookie_idx, domain, LogEmoji.POINTS, "查询总积分")
            points_str, _, streak_str = api.get_points(cookie)
            result.points_total = points_str
            result.streak = streak_str

            # 4. 兑换（仅当签到被受理时执行，且每 N 天尝试一次，避免天天调用兑换接口）
            exchange_plan = self.config.get_exchange_plan(cookie_idx)
            if result.code not in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT):
                result.exchange = "未兑换(签到失败)"
            elif exchange_plan == ExchangePlan.NONE.value:
                result.exchange = "未兑换(已关闭)"
                self._log(
                    cookie_idx,
                    domain,
                    LogEmoji.EXCHANGE,
                    "自动兑换已关闭 (none)，跳过兑换",
                    force=True,
                )
            elif not self.config.exchange_due:
                result.exchange = "未兑换(未到期)"
                self._log(
                    cookie_idx,
                    domain,
                    LogEmoji.EXCHANGE,
                    f"跳过兑换 {exchange_plan}（今日非兑换日，间隔 {self.config.exchange_interval} 天）",
                    force=True,
                )
            else:
                required_points = self.config.EXCHANGE_PLANS.get(exchange_plan, 500)
                self._log(
                    cookie_idx,
                    domain,
                    LogEmoji.EXCHANGE,
                    f"开始兑换 {exchange_plan} (需要 {required_points} 积分)",
                )
                result.exchange = api.exchange(cookie, exchange_plan, required_points)

        return result

    def get_results(self) -> List[Dict[str, str]]:
        """获取所有结果"""
        return [result.to_dict() for result in self.results]

    def format_results(self) -> Tuple[str, str, str]:
        """格式化结果"""
        results = sorted(self.get_results(), key=lambda r: r["cookie_index"])

        success_count = sum(1 for r in results if r["code"] == CheckinStatus.SUCCESS)
        repeat_count = sum(1 for r in results if r["code"] == CheckinStatus.REPEAT)
        fail_count = sum(1 for r in results if r["code"] == CheckinStatus.FAILURE)

        title = f"GLaDOS 签到, 成功{success_count}, 失败{fail_count}, 重复{repeat_count}"

        send_content_lines = []
        log_content_lines = []
        for i, res in enumerate(results, 1):
            line1 = f"#{i} 本次:{res['points']} 总积分:{res['points_total']} | {res['status']}"
            line2 = f"    连续:{res['streak']} 剩余:{res['days']} | {res['exchange']}"
            send_content_lines.append(line1)
            send_content_lines.append(line2)
            log_content_lines.append(f"{LogEmoji.COOKIE}[{res['cookie_index']}] {line1}")
            log_content_lines.append(f"{LogEmoji.COOKIE}[{res['cookie_index']}] {line2}")

        content = "\n".join(send_content_lines)
        log_content = "\n".join(log_content_lines)
        return title, content, log_content


# 初始化日志
logger = init_logger()


def main():
    """主函数"""
    config = None
    exit_code = 0
    try:
        # 1. 加载配置
        logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
        config = Config()
        config.exchange_due = config.is_exchange_due()
        logger.info(f"{LogEmoji.INFO} 今日是否轮到兑换: {'是' if config.exchange_due else '否'}")

        if not config.cookies_list:
            logger.error(f"{LogEmoji.ERROR} 未找到有效的 Cookie, 退出程序。")
            title, content = "# 未找到 cookies!", ""
            exit_code = 1
        else:
            # 2. 执行签到
            logger.info(f"{LogEmoji.START} 步骤 2: 执行签到")
            checker = Checker(config)
            checker.checkin_all()

            # 3. 格式化结果
            logger.info(f"{LogEmoji.START} 步骤 3: 格式化结果")
            title, content, log_content = checker.format_results()
            logger.info(f"{LogEmoji.END}========== 签到总结 ==========")
            logger.info(f"{LogEmoji.INFO} {title}")
            for _line in log_content.split("\n"):
                if _line.strip():
                    logger.info(f"{LogEmoji.INFO} {_line}")

            # 全部 Cookie 均签到失败时以非零状态退出，让 CI 失败，避免"绿色但实际失效"
            accepted_count = sum(
                1 for result in checker.get_results()
                if result["code"] in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT)
            )
            if accepted_count == 0:
                logger.error(
                    f"{LogEmoji.ERROR} 全部 {len(config.cookies_list)} 个 Cookie 签到均失败，"
                    f"请检查 Cookie 是否已失效。"
                )
                exit_code = 1

    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 主程序执行过程中发生未预期的错误: {e}")
        title, content, log_content = "# 脚本执行出错", str(e), str(e)
        exit_code = 1

    # 4. 发送推送
    logger.info(f"{LogEmoji.START} 步骤 4: 发送推送")
    if config is not None:
        push_service = PushService(config)
        push_service.send(title, content)
    else:
        logger.warning(f"{LogEmoji.WARNING} 配置加载失败，跳过推送。")
    logger.info(f"{LogEmoji.END} 签到完成")

    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
