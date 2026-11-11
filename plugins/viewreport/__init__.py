# -*- coding: utf-8 -*-
"""
观影报告推送 (ViewReport)
=======================
一个零外部素材依赖的 MoviePilot 插件，用于把 Emby 的观影排行以纯文字榜单推送到
微信 / Telegram 等外部渠道（NotificationType.Plugin）或站内信；也可切换为图片报告。

相比社区常见的 EmbyReporter，本插件的改进：
  1. 默认推送纯文字榜单，不依赖图片外链是否可访问，微信/TG/站内信都能直接读到内容。
  2. 不依赖 res/ 目录里的背景图、掩膜、字体文件 —— 可选的图片模式由 PIL 代码生成渐变，
     字体在运行时自动探测系统中文字体，缺失也只是降级而非崩溃。
  3. 任何取数 / 排版 / 推送异常都会写进插件日志，并通过站内信（systemmessage）
     通知，不再被调度器静默吞掉，便于排查。

前置条件：Emby 已安装官方 Playback Reporting 插件（提供 user_usage_stats 数据源）。
"""
import os
import threading
import traceback
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageChops
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from cacheout import Cache

from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

cache = Cache()


class ViewReport(_PluginBase):
    # ---- 插件元信息 ----
    plugin_name = "观影报告推送"
    plugin_desc = "文字按 Emby 账号分节；图片为所有账号汇总的总榜（电影+剧集合并）。默认纯文字，可选单张长图（点击直接打开原图，渠道内可直接显示）。需 Emby 安装 Playback Reporting 插件。"
    plugin_icon = "Pydiocells_A.png"
    plugin_version = "2.0.3"
    plugin_author = "WorkBuddy"
    author_url = "https://github.com/"
    plugin_config_prefix = "viewreport_"
    plugin_order = 31
    auth_level = 1

    # ---- 私有属性 ----
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: Optional[str] = None
    _days: int = 7
    _cnt: int = 10
    _type: Optional[str] = "Web"
    _mp_host: Optional[str] = None
    _show_time: bool = True
    _black_library: Optional[str] = None
    _emby_host: Optional[str] = None
    _emby_api_key: Optional[str] = None
    _font_path: Optional[str] = None
    _mediaservers: List[str] = []
    # 报告形式：Text=纯文字（默认） / Image=图片 / Both=文字+图片
    _format: str = "Text"

    _scheduler: Optional[BackgroundScheduler] = None
    mediaserver_helper = None

    # Emby user_usage_stats 查询类型
    PLAYBACK_REPORTING_TYPE_MOVIE = "ItemName"
    PLAYBACK_REPORTING_TYPE_TVSHOWS = "substr(ItemName,0, instr(ItemName, ' - '))"

    # 运行时连接信息
    _EMBY_HOST = None
    _EMBY_APIKEY = None
    _EMBY_USER = None
    # 字体探测缓存
    _cjk_font: Optional[str] = None
    _font_scanned: bool = False

    # ------------------------------------------------------------------ #
    # 插件生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict = None):
        self.stop_service()
        # 配置可能变更（尤其是手动指定的 font_path），重置字体探测缓存
        self._cjk_font = None
        self._font_scanned = False
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._days = config.get("days") or 7
            self._cnt = config.get("cnt") or 10
            self._type = config.get("type") or "Web"
            self._mp_host = config.get("mp_host")
            self._show_time = config.get("show_time", True)
            self._black_library = config.get("black_library")
            self._emby_host = config.get("emby_host")
            self._emby_api_key = config.get("emby_api_key")
            self._font_path = config.get("font_path")
            self._mediaservers = config.get("mediaservers") or []
            self._format = config.get("format") or "Text"

            if self._enabled or self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                if self._onlyonce:
                    logger.info("观影报告推送服务启动，立即运行一次")
                    # 用独立后台线程执行：插件重载 / 配置回写可能触发 init_plugin 重入，
                    # 进而调用 stop_service() 清掉 APScheduler 的 date job，导致"立即运行"丢失。
                    threading.Thread(target=self.__report, name="ViewReport-Once",
                                     daemon=True).start()
                    self._onlyonce = False
                    self.__update_config()

                if self._cron:
                    try:
                        self._scheduler.add_job(
                            func=self.__report,
                            trigger=CronTrigger.from_crontab(self._cron),
                            name="观影报告推送")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        self.systemmessage.put(f"观影报告推送 执行周期配置错误：{err}")

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        return []

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "days": self._days,
            "cnt": self._cnt,
            "type": self._type,
            "mp_host": self._mp_host,
            "show_time": self._show_time,
            "black_library": self._black_library,
            "emby_host": self._emby_host,
            "emby_api_key": self._emby_api_key,
            "font_path": self._font_path,
            "mediaservers": self._mediaservers,
            "format": self._format,
        })

    # ------------------------------------------------------------------ #
    # 配置表单
    # ------------------------------------------------------------------ #
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = [{"title": item.value, "value": item.name} for item in NotificationType]
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VCronField', 'props': {
                                    'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式，留空自动'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {
                                    'model': 'days', 'label': '报告天数', 'placeholder': '向前统计天数，默认7'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {
                                    'model': 'cnt', 'label': '排行数量', 'placeholder': '每类取前N条，默认10'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {
                                    'model': 'mp_host', 'label': 'MoviePilot域名', 'placeholder': '图片模式必填，需公网可访问（企业微信要能打开），如 http://域名:端口 ，末尾不带/'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VSelect', 'props': {
                                    'model': 'type', 'label': '消息类型', 'items': MsgTypeOptions,
                                    'placeholder': '决定消息归类；渠道由「通知设置」里各渠道的开关决定'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VSelect', 'props': {
                                    'model': 'show_time', 'label': '显示观看时长',
                                    'items': [{'title': '是', 'value': True}, {'title': '否', 'value': False}]}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {
                                    'model': 'font_path', 'label': '中文字体路径(可选)',
                                    'placeholder': '留空自动探测，如 /config/NotoSansCJK.ttc'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {
                                    'model': 'black_library', 'label': '黑名单媒体库', 'placeholder': '多个名称用英文逗号分隔'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {
                                    'model': 'emby_host', 'label': '自定义Emby Host', 'placeholder': 'IP:PORT，留空读环境变量'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {
                                    'model': 'emby_api_key', 'label': '自定义Emby ApiKey'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12},
                                'content': [{'component': 'VSelect', 'props': {
                                    'multiple': True, 'chips': True, 'clearable': True, 'model': 'mediaservers',
                                    'label': '媒体服务器',
                                    'items': [{"title": cfg.name, "value": cfg.name}
                                              for cfg in self.mediaserver_helper.get_configs().values()
                                              if cfg.type == "emby"]}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12},
                                'content': [{'component': 'VSelect', 'props': {
                                    'model': 'format', 'label': '报告形式',
                                    'items': [
                                        {'title': '纯文字（推荐，直接可读，不生成图片）', 'value': 'Text'},
                                        {'title': '图片', 'value': 'Image'},
                                        {'title': '文字 + 图片', 'value': 'Both'},
                                    ]}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12},
                                'content': [{'component': 'VAlert', 'props': {
                                    'type': 'info', 'variant': 'tonal',
                                    'text': '按 Emby 账号分类，无观影记录的账号不显示。「纯文字」= 每台服务器一条文字；'
                                            '「图片」= 每个账号一张长图；「文字+图片」= 一张长图 + 该账号文字榜单放在图片说明里，'
                                            '只发一条消息、不重复标题。图片使用公网外链（企业微信可直接内联显示），'
                                            '并预热 MP 图片缓存保证 Telegram 也能发图；点击链接打开原图可放大。'}}]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "days": 7,
            "cnt": 10,
            "type": "Web",
            "mp_host": "",
            "show_time": True,
            "black_library": "",
            "emby_host": "",
            "emby_api_key": "",
            "font_path": "",
            "mediaservers": [],
            "format": "Text",
        }

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def __report(self):
        """生成并推送观影报告（外层已做异常兜底，确保可见）"""
        try:
            fmt_cfg = str(self._format or "Text").strip().lower()
            # 只有图片模式才需要 mp_host 拼图片外链；纯文字模式不依赖它
            if fmt_cfg in ("image", "both") and not self._mp_host:
                logger.error("图片模式需配置 MoviePilot域名(mp_host)，无法生成图片外链")
                self.systemmessage.put("观影报告推送：图片模式需先配置 MoviePilot域名（或改用纯文字模式）。")
                return
            if not self._type:
                self._type = "Web"

            emby_servers = self.mediaserver_helper.get_services(
                name_filters=self._mediaservers, type_filter="emby")
            if not emby_servers:
                logger.error("未配置 Emby 媒体服务器")
                self.systemmessage.put("观影报告推送：未检测到已配置的 Emby 媒体服务器。")
                return

            for si, (emby_name, emby_server) in enumerate(emby_servers.items(), start=1):
                logger.info(f"开始处理媒体服务器 {emby_name}")
                self._EMBY_HOST = self._emby_host or emby_server.config.config.get("host")
                self._EMBY_APIKEY = self._emby_api_key or emby_server.config.config.get("apikey")
                self._EMBY_USER = emby_server.instance.get_user()
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

                ts = datetime.now().strftime("%Y%m%d%H%M%S")

                # 日期：只写具体日期，不再用「统计区间 …（近N天）」这种区间描述
                now = datetime.now(pytz.timezone("Asia/Shanghai"))
                if int(self._days) == 1:
                    yesterday = now - timedelta(days=1)
                    week = "一二三四五六日"[yesterday.weekday()]
                    # 短日期：去掉年份，仅「M月DD日 星期X」，更紧凑
                    date_str = f"{yesterday.month}月{yesterday.day:02d}日 星期{week}"
                else:
                    first = (now - timedelta(days=int(self._days))).strftime("%Y年%m月%d日")
                    last = (now - timedelta(days=1)).strftime("%Y年%m月%d日")
                    date_str = f"{first} ~ {last}"

                # 取数：SQL 按 UserId + 片名分组，Python 里再按账号各取 TopN
                ok_m, movies = self.get_report(
                    types=self.PLAYBACK_REPORTING_TYPE_MOVIE, days=int(self._days), limit=int(self._cnt))
                ok_t, tvshows = self.get_report(
                    types=self.PLAYBACK_REPORTING_TYPE_TVSHOWS, days=int(self._days), limit=int(self._cnt))
                movies = movies if ok_m else []
                tvshows = tvshows if ok_t else []
                logger.info(f"{emby_name} 取到电影 {len(movies)} 行 / 剧集 {len(tvshows)} 行")

                # 消息类型：兼容成员名 / 中文值 / 历史配置里的渠道名
                mtype = self._resolve_mtype()

                fmt = str(self._format or "Text").strip().lower()
                if fmt not in ("text", "image", "both"):
                    fmt = "text"
                # 整台 Emby 服务器只发一条消息：
                #   text  -> 纯文字（内部仍按账号分节，保持现状）
                #   image -> 全部账号汇总成的一张总榜长图
                #   both  -> 同一条消息里：汇总总榜图片 + 按账号分节的完整文字
                want_image = fmt in ("image", "both")

                # 按 Emby 账号归类（文字报告用）；无观影记录的账号不会出现
                user_names = self._get_emby_users()
                groups = self._group_by_user(movies, tvshows, user_names, int(self._cnt))
                if not groups:
                    logger.info(f"{emby_name} 无观影数据，跳过推送")
                    continue
                logger.info(f"{emby_name} 有观影记录的账号 {len(groups)} 个："
                            f"{[g['name'] for g in groups]}")

                # 推送标题统一为「昨日观影报告」，不带 Emby 账号名 / 媒体服务器名
                msg_title = "📊 昨日观影报告"

                # 纯文字报告：整台服务器一条消息，内部按账号分节；正文不含标题（避免与消息标题重复）
                if fmt == "text":
                    text = self._make_text_report(emby_name, date_str, groups)
                    if text:
                        self._send(title=msg_title, mtype=mtype, text=text)
                        logger.info(f"{emby_name} 文字观影报告推送成功")
                        logger.info("文字报告内容：\n" + text)
                    else:
                        logger.error(f"{emby_name} 文字观影报告生成失败")

                if not want_image:
                    continue

                # ---- 图片模式：所有账号数据汇总成一张「总榜」长图 ----
                # 同名影片跨账号合并播放次数与时长，图片里不体现任何单个账号
                all_items = self._merge_all_users(groups, int(self._cnt))
                if not all_items:
                    logger.info(f"{emby_name} 汇总后无榜单数据，跳过图片")
                    continue
                logger.info(f"{emby_name} 汇总总榜 {len(all_items)} 部："
                            f"{[it[3] for it in all_items]}")
                sections = [("观影榜", all_items)]
                filename = f"report_s{si}.jpg"
                # 图片内主标题不带 emoji：CJK 字体缺少 emoji 字形会渲染成方框
                path = self._make_image(sections, "昨日观影报告", date_str,
                                        filename, ts)
                if not path:
                    logger.error(f"{emby_name} 汇总总榜长图生成失败")
                    continue
                public_url = self._mp_host.rstrip("/") + f"/{filename}?_timestamp={ts}"
                image_url = self._pick_image_url(filename, ts, public_url, path)
                # 文字保持现状：按账号分节的完整榜单（both 时去掉与标题重复的汇总行）
                if fmt == "both":
                    caption = self._make_text_report(emby_name, date_str, groups,
                                                     show_count=False)
                else:
                    caption = f"共 {len(all_items)} 部作品\n{date_str}"
                if caption:
                    logger.info("文字报告内容：\n" + caption)
                # link 指向图片本身：点击直接打开原图，不再跳 MoviePilot 插件页
                self._send(title=msg_title, mtype=mtype, text=caption,
                           image=image_url, link=public_url)
                logger.info(f"{emby_name} 汇总总榜推送成功 image={image_url} link={public_url}")

        except Exception as e:
            logger.error(f"观影报告推送执行异常：{e}")
            logger.error(traceback.format_exc())
            try:
                self.systemmessage.put(f"观影报告推送执行异常：{e}")
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 纯文字报告（默认形式，不依赖图片外链 / 字体）
    # ------------------------------------------------------------------ #
    def _make_text_report(self, emby_name: str, date_str: str, groups: list,
                          show_count: bool = True) -> Optional[str]:
        """按 Emby 账号分节排版纯文字榜单。

        正文不重复消息标题：渠道会把 title 和 text 拼在一起显示，
        若正文再写一遍「近N天观影报告」就会出现两行重复标题。
        """
        try:
            if not groups:
                return None
            # 缩进只用 2 个空格，避免被 Telegram MarkdownV2 当成代码块
            lines = [date_str]
            if show_count:
                lines.append(f"共 {len(groups)} 位账号有观影记录")
            lines.append("")

            def section(icon: str, label: str, items: list):
                if not items:
                    return
                lines.append(f"{icon} {label} TOP{len(items)}")
                total_count, total_sec = 0, 0
                for idx, it in enumerate(items):
                    try:
                        _uid, _iid, _itype, name, count, duration = it
                    except Exception:
                        name, count, duration = str(it), 0, 0
                    name = " ".join(str(name or "未知").split())
                    if len(name) > 48:
                        name = name[:47] + "…"
                    try:
                        count = int(count or 0)
                    except Exception:
                        count = 0
                    try:
                        duration = int(float(duration or 0))
                    except Exception:
                        duration = 0
                    total_count += count
                    total_sec += duration
                    lines.append(f"{idx + 1}. {name}")
                    stat = f"  播放 {count} 次"
                    if self._show_time and duration:
                        try:
                            stat += f" · 时长 {StringUtils.str_secends(duration)}"
                        except Exception:
                            pass
                    lines.append(stat)
                if total_sec:
                    try:
                        lines.append(f"  小计：播放 {total_count} 次 · 总时长 "
                                     f"{StringUtils.str_secends(total_sec)}")
                    except Exception:
                        pass
                lines.append("")

            for grp in groups:
                lines.append(f"── 👤 {grp['name']} ──")
                # 电影与剧集不再分段，统一成一个观影排行
                section("🏆", "观影排行", grp["items"])
            lines.append("由 MoviePilot · 观影报告推送 生成")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"生成文字报告失败：{e}")
            logger.error(traceback.format_exc())
            return None

    # ------------------------------------------------------------------ #
    # 账号信息 / 分组 / 图片地址
    # ------------------------------------------------------------------ #
    def _get_emby_users(self) -> dict:
        """取 Emby 账号 Id -> 名称 映射；取不到时报告里显示 ID 前缀。"""
        if not self._EMBY_HOST:
            return {}
        urls = [
            f"{self._EMBY_HOST}emby/Users/Query?api_key={self._EMBY_APIKEY}",
            f"{self._EMBY_HOST}emby/Users?api_key={self._EMBY_APIKEY}",
        ]
        for url in urls:
            try:
                resp = RequestUtils().get_res(url=url)
                if resp is None or resp.status_code != 200:
                    continue
                data = resp.json()
                items = data.get("Items") if isinstance(data, dict) else data
                mapping = {}
                for it in items or []:
                    uid, name = it.get("Id"), it.get("Name")
                    if uid and name:
                        mapping[str(uid)] = str(name)
                if mapping:
                    logger.info(f"Emby 账号列表：{list(mapping.values())}")
                    return mapping
            except Exception as e:
                logger.warning(f"获取 Emby 账号列表失败：{e}")
        logger.warning("未取到 Emby 账号名称，报告中将显示用户ID前缀")
        return {}

    @staticmethod
    def _group_by_user(movies: list, tvshows: list, user_names: dict, limit: int) -> list:
        """按 UserId 分组；电影与剧集合并成一份统一榜单，各账号取 Top limit。

        合并后按播放总时长降序统一排名，不再分别生成「电影榜 / 剧集榜」。
        没有任何观影记录的账号不会出现。
        """
        groups = {}

        def dur(row):
            try:
                return int(float(row[5] or 0))
            except Exception:
                return 0

        def feed(rows):
            for row in rows or []:
                try:
                    uid = str(row[0] or "unknown")
                except Exception:
                    uid = "unknown"
                groups.setdefault(uid, {"id": uid, "items": []})["items"].append(row)

        feed(movies)
        feed(tvshows)
        out = []
        for uid, grp in groups.items():
            items = sorted(grp["items"], key=dur, reverse=True)[:max(1, limit)]
            if not items:
                continue
            grp["items"] = items
            grp["total"] = sum(dur(r) for r in items)
            grp["name"] = user_names.get(uid) or (
                "未知账号" if uid == "unknown" else f"用户 {uid[:8]}")
            out.append(grp)
        out.sort(key=lambda x: -x["total"])
        return out

    @staticmethod
    def _merge_all_users(groups: list, limit: int) -> list:
        """把全部 Emby 账号的观影记录汇总成一份「总榜」，图片专用。

        同名影片（含同一剧集的不同集）跨账号合并播放次数与时长，按总时长降序
        取 Top limit；结果里不含任何账号信息，封面沿用首次出现的条目。
        文字报告不使用这个方法，仍按账号分节（保持现状）。
        """
        merged = {}
        for grp in groups or []:
            for row in grp.get("items") or []:
                try:
                    uid, iid, itype, name, count, duration = row
                except Exception:
                    continue
                key = " ".join(str(name or "未知").split()) or "未知"
                try:
                    count = int(count or 0)
                except Exception:
                    count = 0
                try:
                    duration = int(float(duration or 0))
                except Exception:
                    duration = 0
                if key not in merged:
                    merged[key] = [uid, iid, itype, key, 0, 0]
                merged[key][4] += count
                merged[key][5] += duration
        return sorted(merged.values(), key=lambda r: r[5], reverse=True)[:max(1, limit)]

    def _resolve_mtype(self):
        """把配置里的「推送方式」解析成 NotificationType 成员。

        NotificationType 只有 资源下载/整理入库/订阅/站点/媒体服务器/手动处理/
        插件/智能体/其它 九类，**没有 Web**（Web 是 MessageChannel 渠道名）。
        旧配置写成 Web / Telegram / Wechat 等渠道名时统一归到「插件」，避免回退到
        list(NotificationType)[0] = 「资源下载」这种误导性分类。
        """
        raw = str(self._type or "").strip()
        cand = getattr(NotificationType, raw, None) if raw else None
        if isinstance(cand, NotificationType):
            return cand
        for item in NotificationType:
            if raw == item.value or raw.lower() == item.name.lower():
                return item
        fallback = getattr(NotificationType, "Plugin", None) or list(NotificationType)[0]
        if raw:
            logger.info(f"推送方式「{raw}」不是有效消息类型，已按「{fallback.value}」推送")
        return fallback

    def _prime_image_cache(self, public_url: str, local_path: str) -> bool:
        """把刚生成的图片字节写进 MP 的 ImageHelper 图片缓存。

        Telegram 发图前会调用 ImageHelper().fetch_image(image)（默认 use_cache=True）：
        只要同一 URL 的缓存里有内容，就直接返回字节、完全不发网络请求，也就不会
        踩到 settings.PROXY 回连失败。因此 image 可以放心使用公网地址，让企业微信
        news.picurl / 群机器人 markdown 也能内联显示图片。
        """
        try:
            from app.helper.image import ImageHelper
            helper = ImageHelper()
        except Exception as e:
            logger.warning(f"无法加载 ImageHelper，跳过图片缓存预热：{e}")
            return False

        # 1) 离线写入：图片文件就在本地，不需要任何网络请求
        wrote = False
        try:
            data = Path(local_path).read_bytes()
            if data and data[:2] == b"\xff\xd8":
                cache_path = helper._prepare_cache_path(public_url)
                helper.file_cache.set(cache_path, data, region="images")
                wrote = True
        except Exception as e:
            logger.warning(f"离线写入图片缓存失败：{e}")

        # 2) 用与 Telegram 完全相同的调用自检（命中缓存即代表发图链路通）
        if wrote:
            try:
                check = helper.fetch_image(public_url)
                if check and check[:2] == b"\xff\xd8":
                    logger.info(f"图片渠道缓存已就绪，Telegram 发图链路自检通过：{public_url}")
                    return True
            except Exception as e:
                logger.warning(f"图片缓存自检异常：{e}")

        # 3) 兜底：无代理下载一次，fetch_image 内部会把结果写回同一缓存
        try:
            helper.fetch_image(public_url, proxy=False, use_cache=False)
            check = helper.fetch_image(public_url)
            if check and check[:2] == b"\xff\xd8":
                logger.info(f"图片渠道缓存已由网络预热补全：{public_url}")
                return True
        except Exception as e:
            logger.warning(f"图片缓存网络预热异常：{e}")
        logger.warning("图片缓存预热失败，Telegram 可能退化为纯文本；"
                       "企业微信等外链渠道不受影响")
        return False

    def _pick_image_url(self, filename: str, ts: str, public_url: str,
                        local_path: str = None) -> str:
        """返回给消息渠道使用的 image 地址 —— 固定为公网 URL。

        两类渠道对 image 的要求看似冲突：
        - 企业微信应用消息把它当 news.picurl、群机器人当 markdown 图片外链，由
          客户端自行加载，必须是公网可访问地址。v1.4 改用 127.0.0.1 后客户端
          解析不到，消息里就没图，只剩点 link 进去才能看到。
        - Telegram 由 MP 容器先下载再以二进制发送；公网域名会挂 settings.PROXY，
          代理回连 NAS 失败会退化成「纯文本 + 查看详情」。

        解法：image 用公网 URL，并在发送前把图片字节写入 ImageHelper 缓存
        （_prime_image_cache）；TG 命中缓存、不走网络，两边同时成立。
        """
        if local_path:
            self._prime_image_cache(public_url, local_path)
        return public_url

    def _send(self, title: str, mtype, text: str = None,
              image: str = None, link: str = None):
        """直接投递 Notification，绕开 _PluginBase.post_message 的默认链接。

        基类在 link 为空时会自动填 MoviePilot 插件页地址，Telegram 会把它渲染成
        「查看详情」并跳回 MP。这里让 link 为 None 时消息根本没有这个链接；
        图片消息则显式把 link 指向原图，点击直接打开可放大的图片。
        """
        try:
            from app.schemas import Notification
            self.chain.post_message(Notification(
                title=title, text=text, image=image, link=link, mtype=mtype))
        except Exception as e:
            logger.warning(f"直接投递消息失败，回退基础通道：{e}")
            # 降级路径不再把 link 塞成 MoviePilot 首页（会渲染成「查看详情」跳回 MP）
            self.post_message(title=title, text=text, image=image, mtype=mtype,
                              link=link)

    # ------------------------------------------------------------------ #
    # 图片生成（可选模式：纯 PIL，无外部素材）
    # ------------------------------------------------------------------ #
    def _make_image(self, sections, section_title, date_str, filename, ts,
                    emby_name: str = ""):
        """毛玻璃版式（v2.0.0）：把「全部账号汇总总榜」画进一张 480 宽长图。

        统一毛玻璃背景 + 玻璃卡片/列表；保留「昨日观影报告」大标题与金竖条，
        去掉「观影排行 TOP10」标签栏；日期沿用调用方传入的具体日期；右上统计块
        缩小并双向居中；数字默认使用独立拉丁字体（容器有则用，否则沿用中文字形）；
        无时钟图标；底图取 Top1 真实封面做模糊壁纸。
        sections = [(段标题, rows), ...]，rows 元素为
        [user_id, item_id, item_type, name, count, duration]
        """
        try:
            from PIL import ImageFilter, ImageChops
            RESAMPLE = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS

            WHITE = (247, 250, 255, 255)
            SOFT = (218, 232, 250, 255)
            MUTED = (168, 188, 222, 255)
            GOLD = (247, 203, 112, 255)
            SILVER = (216, 226, 242, 255)
            BRONZE = (218, 162, 112, 255)
            PURPLE = (196, 188, 255, 255)
            RANK_COLORS = [GOLD, SILVER, BRONZE]
            RANK_EDGE = [(247, 203, 112, 110), (216, 226, 242, 90), (218, 162, 112, 100)]
            EDGE = (255, 255, 255, 62)

            # 字体：中文用主字体（思源黑体，容器内缺失则降级探测）；数字优先独立拉丁字体
            def F(s):
                return self._get_font(s)

            def Fn(s):
                return self._get_num_font(s)

            W, PAD = 480, 22
            header_h, footer_h = 106, 42
            section_gap = 14
            gap, card_w = 14, 106
            poster_h, info_h = int(card_w * 1.5), 70
            card_h = poster_h + info_h
            row_h, row_gap = 60, 8

            valid = [(lb, list(rw)) for lb, rw in sections if rw]
            if not valid:
                return None
            all_rows = [it for _lb, rw in valid for it in rw]

            def cell(it):
                try:
                    uid, iid, itype, name, count, duration = it
                except Exception:
                    uid, iid, itype, name, count, duration = None, None, "Movie", str(it), 0, 0
                try:
                    count = int(count or 0)
                except Exception:
                    count = 0
                try:
                    duration = int(float(duration or 0))
                except Exception:
                    duration = 0
                return uid, iid, itype, " ".join(str(name or "未知").split()), count, duration

            parsed = [cell(it) for it in all_rows]
            total = len(parsed)
            total_dur = sum(p[5] for p in parsed)
            max_dur = max([p[5] for p in parsed] + [1])
            top = parsed[:3]
            rest = parsed[3:]

            H = header_h + (card_h + 14 if top else 0) + len(rest) * (row_h + row_gap) \
                + section_gap + footer_h

            # ---- 底图：取 Top1 真实封面做模糊壁纸 ----
            hero = None
            if top:
                try:
                    hero = self._cover(top[0][0], top[0][1], top[0][2], W, H, 0)
                except Exception:
                    hero = None
            if hero is not None:
                hero = hero.convert("RGBA")

            def build_bg():
                grad = Image.new("RGBA", (W, H))
                gd = ImageDraw.Draw(grad)
                top_c, bot_c = (16, 22, 42), (7, 10, 22)
                for y in range(H):
                    t = y / max(H - 1, 1)
                    gd.line([(0, y), (W, y)],
                            fill=tuple(int(top_c[i] + (bot_c[i] - top_c[i]) * t) for i in range(3)) + (255,))
                bg = grad
                if hero is not None:
                    try:
                        cover = ImageOps.fit(hero, (W, H), method=RESAMPLE,
                                             centering=(0.5, 0.35)).filter(ImageFilter.GaussianBlur(40))
                        bg = Image.blend(bg, cover, 0.22)
                    except Exception:
                        pass
                glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                g = ImageDraw.Draw(glow)
                g.ellipse([-120, -90, 260, 300], fill=(255, 190, 96, 58))
                g.ellipse([W - 230, H - 330, W + 170, H + 90], fill=(96, 132, 255, 66))
                g.ellipse([W - 270, -150, W + 110, 220], fill=(168, 110, 240, 46))
                try:
                    bg = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(70)))
                except Exception:
                    pass
                bg = Image.alpha_composite(bg, Image.new("RGBA", (W, H), (6, 9, 20, 84)))
                return bg

            bg = build_bg()
            ink = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(ink)

            def glass(box, radius=16, tint=(255, 255, 255, 22), blur=16, edge=EDGE):
                x0, y0, x1, y1 = [int(v) for v in box]
                w, h = x1 - x0, y1 - y0
                if w <= 2 or h <= 2:
                    return
                region = bg.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur))
                region = Image.alpha_composite(region, Image.new("RGBA", (w, h), tint))
                hi = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                hd = ImageDraw.Draw(hi)
                for yy in range(min(h, 34)):
                    hd.line([(0, yy), (w, yy)], fill=(255, 255, 255, int(26 * (1 - yy / 34))))
                region = Image.alpha_composite(region, hi)
                mask = Image.new("L", (w, h), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
                region.putalpha(ImageChops.multiply(region.getchannel("A"), mask))
                bg.alpha_composite(region, (x0, y0))
                if edge:
                    e = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                    ImageDraw.Draw(e).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                                                        outline=edge, width=1)
                    bg.alpha_composite(e, (x0, y0))

            def paste_cover(img, x, y, w, h, radius=9):
                m = Image.new("L", (w, h), 0)
                ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
                bg.paste(img, (x, y), m)
                e = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                ImageDraw.Draw(e).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                                                    outline=(255, 255, 255, 66), width=1)
                bg.alpha_composite(e, (x, y))

            def gradient_bar(x1, y, x2, h, c1=(247, 203, 112), c2=(255, 231, 160)):
                w = max(1, x2 - x1)
                layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                for xx in range(w):
                    t = xx / max(1, w - 1)
                    ld.line([(xx, 0), (xx, h)],
                            fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)) + (255,))
                m = Image.new("L", (w, h), 0)
                ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=4, fill=255)
                layer.putalpha(m)
                ink.alpha_composite(layer, (x1, y))

            def mix_width(text, f_text, f_digit, sp=0):
                return sum(self._text_w(d, ch, f_digit if ch.isdigit() else f_text)
                           for ch in text) + sp * max(0, len(text) - 1)

            def draw_rich(x, y, text, f_text, f_digit, fill_text, fill_digit,
                          anchor="la", sp=0):
                widths = [self._text_w(d, ch, f_digit if ch.isdigit() else f_text) for ch in text]
                total = sum(widths) + sp * max(0, len(text) - 1)
                if anchor == "ra":
                    x -= total
                elif anchor == "mm":
                    x -= total / 2
                cx = x
                for ch, w in zip(text, widths):
                    if ch.isdigit():
                        f, fill = f_digit, fill_digit
                    else:
                        f, fill = f_text, fill_text
                    d.text((cx, y), ch, font=f, fill=fill, anchor="la")
                    cx += w + sp
                return total

            def draw_rich_mm(cx, cy, text, f_text, f_digit, fill_text, fill_digit, sp=0):
                bbox = f_text.getbbox("M")
                offset = (bbox[1] + bbox[3]) / 2
                return draw_rich(cx, cy - offset, text, f_text, f_digit,
                                 fill_text, fill_digit, anchor="mm", sp=sp)

            def cover_rgba(uid, iid, itype, w, h, r):
                cov = self._cover(uid, iid, itype, w, h, r)
                if cov is None:
                    return None
                img = cov.convert("RGBA")
                m = Image.new("L", (w, h), 0)
                ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
                img.putalpha(m)
                return img

            # ---- 右上统计：玻璃小胶囊，文字在块内双向居中（已缩小）----
            f_stat = F(12)
            f_digit_stat = Fn(13)
            stat1 = f"共 {total} 部"
            stat2 = f"累计 {self._fmt_hm(total_dur)}"
            stat_w = max(mix_width(stat1, f_stat, f_digit_stat, 0.4),
                         mix_width(stat2, f_stat, f_digit_stat, 0.4))
            pad_x, line_h, pad_y = 18, 19, 9
            bw = stat_w + pad_x * 2
            bh = line_h * 2 + pad_y * 2
            bx1 = W - PAD - bw
            by1 = 14
            glass([bx1, by1, bx1 + bw, by1 + bh], radius=16,
                  tint=(255, 255, 255, 36), blur=10, edge=(255, 255, 255, 70))
            cx_stat = bx1 + bw / 2
            gstart = by1 + pad_y + line_h / 2
            draw_rich_mm(cx_stat, gstart, stat1, f_stat, f_digit_stat, SOFT, GOLD, sp=0.4)
            draw_rich_mm(cx_stat, gstart + line_h, stat2, f_stat, f_digit_stat, SOFT, GOLD, sp=0.4)

            # ---- 标题区：发光金竖条 + 大标题（无 TOP10 标签栏）----
            d.rounded_rectangle([PAD - 4, 26, PAD + 9, 72], radius=6, fill=(247, 203, 112, 34))
            d.rounded_rectangle([PAD, 30, PAD + 5, 68], radius=3, fill=GOLD)
            d.text((PAD + 16, 24), section_title, font=F(30), fill=WHITE)
            d.text((PAD + 16, 73), date_str, font=F(13), fill=SOFT)

            y = header_h
            # ---- 前三名：玻璃卡片 + 清晰海报 ----
            if top:
                n = len(top)
                span = n * card_w + (n - 1) * gap
                x0 = (W - span) // 2
                for j, (uid, iid, itype, name, count, duration) in enumerate(top):
                    x = x0 + j * (card_w + gap)
                    glass([x - 5, y - 5, x + card_w + 5, y + card_h],
                          radius=14, tint=(255, 255, 255, 30), blur=14, edge=(255, 255, 255, 58))
                    cov = cover_rgba(uid, iid, itype, card_w, poster_h, 9)
                    if cov is not None:
                        paste_cover(cov, x, y, card_w, poster_h, 9)
                    else:
                        d.rounded_rectangle([x, y, x + card_w, y + poster_h], radius=9,
                                            fill=(21, 29, 51, 255))
                    by_badge = y + poster_h - 28
                    glass([x + 6, by_badge, x + 58, by_badge + 22], radius=11,
                          tint=(255, 255, 255, 40), blur=10, edge=RANK_EDGE[j])
                    d.text((x + 32, by_badge + 10), self._cn_rank(j), font=F(11),
                           fill=RANK_COLORS[j], anchor="mm")
                    # 三行文字整体上移、收紧行距：用 draw_rich_mm 做真正的水平+垂直居中，
                    # 避免旧 draw_rich(anchor="mm") 只居中水平、y 实为文字顶部导致贴底溢出。
                    by = y + poster_h + 7
                    dtxt = self._fmt_hm(duration)
                    draw_rich_mm(x + card_w / 2, by + 10, dtxt, F(14), Fn(15),
                                 SOFT, (226, 238, 255, 255), sp=0.4)
                    title_txt = self._clip(name, F(14), card_w)
                    draw_rich_mm(x + card_w / 2, by + 27, title_txt, F(14), F(14),
                                 WHITE, WHITE, sp=0.3)
                    count_txt = f"{count} 次播放"
                    draw_rich_mm(x + card_w / 2, by + 43, count_txt, F(11), Fn(12),
                                 MUTED, MUTED, sp=0.5)
                y += card_h + 14

            # ---- 第 4 名起：玻璃列表行 ----
            bar_w, bar_x2 = 92, W - PAD - 12
            bar_x1 = bar_x2 - bar_w
            for j, (uid, iid, itype, name, count, duration) in enumerate(rest):
                idx = j + 3
                ry = y + j * (row_h + row_gap)
                glass([PAD, ry, W - PAD, ry + row_h], radius=14,
                      tint=(255, 255, 255, 22), blur=12, edge=(255, 255, 255, 46))
                glass([PAD + 8, ry + 15, PAD + 76, ry + 43], radius=14,
                      tint=(255, 255, 255, 26), blur=10, edge=(196, 188, 255, 62))
                d.text((PAD + 42, ry + 28), self._cn_rank(idx), font=F(13),
                       fill=PURPLE, anchor="mm")
                th = cover_rgba(uid, iid, itype, 30, 45, 6)
                if th is not None:
                    paste_cover(th, PAD + 84, ry + 7, 30, 45, 6)
                else:
                    d.rounded_rectangle([PAD + 84, ry + 7, PAD + 114, ry + 52], radius=6,
                                        fill=(21, 29, 51, 255))
                nx = PAD + 124
                d.text((nx, ry + 8), self._clip(name, F(16), bar_x1 - nx - 12),
                       font=F(16), fill=WHITE)
                d.text((nx, ry + 32), f"{count} 次播放", font=F(12), fill=MUTED)
                d.rounded_rectangle([bar_x1, ry + 14, bar_x2, ry + 22], radius=4,
                                    fill=(255, 255, 255, 34))
                fw = max(8, int(bar_w * ((duration / max_dur) ** 0.5)))
                gradient_bar(bar_x1, ry + 14, bar_x1 + fw, 8)
                dtxt = self._fmt_hm(duration)
                draw_rich(bar_x2, ry + 30, dtxt, F(13), Fn(13), SOFT,
                          (226, 238, 255, 255), anchor="ra", sp=0.4)

            # ---- 页脚 ----
            fy = H - footer_h + 12
            d.text((PAD, fy), "数据来自 Emby Playback Reporting", font=F(11),
                   fill=(138, 158, 194, 235))
            d.text((W - PAD, fy), datetime.now().strftime("%H:%M"), font=F(11),
                   fill=(138, 158, 194, 235), anchor="ra")

            out = f"/public/{filename}"
            if Path(out).exists():
                Path(out).unlink()
            Image.alpha_composite(bg, ink).convert("RGB").save(out, quality=93, subsampling=0)
            logger.info(f"已生成长图 {out}（{W}x{H}）")
            return out
        except Exception as e:
            logger.error(f"生成报告图片失败：{e}")
            logger.error(traceback.format_exc())
            return None

    # ------------------------------------------------------------------ #
    # 绘图辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt_hm(seconds) -> str:
        """秒 -> 「18 时 17 分」；不足一小时只显示「49 分」。"""
        try:
            s = int(float(seconds or 0))
        except Exception:
            s = 0
        h, m = s // 3600, s % 3600 // 60
        return f"{h} 时 {m} 分" if h else f"{m} 分"

    @staticmethod
    def _cn_rank(idx: int) -> str:
        """0 基排名 -> 「第一名」…「第十名」。"""
        digits = "零一二三四五六七八九十"
        n = idx + 1
        if n <= 10:
            return f"第{digits[n]}名"
        if n < 20:
            return f"第十{digits[n - 10]}名"
        return f"第{n}名"

    @staticmethod
    def _text_w(draw, text: str, font) -> float:
        """量取文字宽度，兼容不同 Pillow 版本。"""
        try:
            return draw.textlength(text, font=font)
        except Exception:
            try:
                return font.getlength(text)
            except Exception:
                return len(text) * 12

    def _clip(self, text: str, font, max_w: int) -> str:
        """单行截断，超出加省略号（中文按字符宽度计算）。"""
        text = " ".join(str(text or "").split())
        try:
            if font.getlength(text) <= max_w:
                return text
            while text and font.getlength(text + "…") > max_w:
                text = text[:-1]
            return text + "…"
        except Exception:
            return text[:20]

    def _cover(self, user_id, item_id, item_type, w: int, h: int, radius: int):
        """取 Emby 封面并居中裁成指定尺寸 + 圆角；取不到返回 None。"""
        try:
            cover_id = item_id
            if item_type and str(item_type) != "Movie":
                sid = self._get_series_id(user_id, item_id)
                if sid:
                    cover_id = sid
            if not cover_id:
                return None
            ok, data = self.primary(cover_id)
            if not ok or not data:
                return None
            img = Image.open(BytesIO(data)).convert("RGB")
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS
            try:
                img = ImageOps.fit(img, (w, h), method=resample, centering=(0.5, 0.4))
            except TypeError:  # 老版 Pillow 不支持 centering
                img = ImageOps.fit(img, (w, h), method=resample)
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                                   radius=radius, fill=255)
            out = Image.new("RGB", (w, h), (21, 29, 51))
            out.paste(img, (0, 0), mask)
            return out
        except Exception:
            return None

    @staticmethod
    def _rank_color(idx: int):
        palette = [(231, 76, 60), (241, 196, 15), (52, 152, 219),
                   (155, 89, 182), (46, 204, 113), (230, 126, 34)]
        return palette[idx % len(palette)]

    def _wrap(self, text: str, font, max_w: int) -> List[str]:
        """按字符宽度折行（适配中文），最多两行，超出截断加省略号。"""
        lines, cur = [], ""
        for ch in text:
            if font.getlength(cur + ch) <= max_w:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
                if len(lines) >= 2:
                    break
        if cur:
            lines.append(cur)
        if len(lines) >= 2 and len(text) > sum(len(x) for x in lines):
            lines[1] = lines[1][:-1] + "…"
        return lines or [text]

    @staticmethod
    def _font_supports_cjk(path: str) -> bool:
        """判断字体是否真正包含中文字形，避免误选 emoji / 符号字体导致方块。"""
        # 方案 A：fontTools 精确读取 cmap（最可靠）
        try:
            from fontTools.ttLib import TTCollection, TTFont
            fonts, coll = [], None
            try:
                if path.lower().endswith(".ttc"):
                    coll = TTCollection(path, lazy=True)
                    fonts = list(coll.fonts)
                else:
                    fonts = [TTFont(path, lazy=True)]
                for ft in fonts:
                    cmap = ft.getBestCmap() or {}
                    # 「一」「中」「观」任一存在即认为支持简体中文
                    if any(cp in cmap for cp in (0x4E00, 0x4E2D, 0x89C2)):
                        return True
                return False
            finally:
                for ft in fonts:
                    try:
                        ft.close()
                    except Exception:
                        pass
                if coll is not None:
                    try:
                        coll.close()
                    except Exception:
                        pass
        except Exception:
            pass
        # 方案 B：PIL 渲染对比法 —— 「观」与私用区缺字符一致说明渲染成了豆腐块
        try:
            font = ImageFont.truetype(path, 32)
            img_cn = Image.new("L", (64, 64), 0)
            ImageDraw.Draw(img_cn).text((2, 2), "观", font=font, fill=255)
            img_tofu = Image.new("L", (64, 64), 0)
            ImageDraw.Draw(img_tofu).text((2, 2), "\uE000", font=font, fill=255)
            return img_cn.tobytes() != img_tofu.tobytes()
        except Exception:
            return False

    def _find_cjk_font(self) -> Optional[str]:
        if self._cjk_font:
            return self._cjk_font
        if self._font_scanned:
            return None
        if self._font_path and os.path.exists(self._font_path):
            self._cjk_font = self._font_path
            logger.info(f"观影报告推送 使用手动指定字体：{self._cjk_font}")
            return self._cjk_font

        # 优先使用文泉驿（容器内确定存在、中文观感稳健），避免误选 emoji/日文/泰文字体
        for _p in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",):
            if os.path.exists(_p) and self._font_supports_cjk(_p):
                self._cjk_font = _p
                logger.info(f"观影报告推送 使用默认中文字体：{_p}")
                return _p

        search_dirs = ["/usr/share/fonts", "/usr/local/share/fonts", "/app/fonts",
                       "/config/fonts", os.path.expanduser("~/.fonts")]
        try:
            # 其他插件的 res/ 目录里常自带中文字体，一并纳入候选
            search_dirs.append(str(settings.ROOT_PATH / "app" / "plugins"))
        except Exception:
            pass
        # 轮廓型中文字体，观感最佳，优先选用（文泉驿 / 思源 / 微软雅黑 / 苹方等）
        best = ("wqy", "wenquanyi", "notosanscjk", "notosanssc", "notoserifcjk",
                "sourcehan", "source han", "droidsansfallback", "simhei", "simsun",
                "msyh", "pingfang", "sarasa", "lxgw", "cjk", "heiti", "songti",
                "fangsong", "dengxian")
        weak = ("noto", "han", "chinese", "sc", "cn", "hei", "song", "kai", "ming")
        # GNU Unifont 是点阵字体，虽含中文但锯齿明显，仅作为最后兜底
        last_resort = ("unifont",)
        # emoji / 彩色 / 符号字体不含中文字形，必须排除
        blocked = ("emoji", "color", "symbol", "math", "icon", "dingbat", "awesome")
        candidates, seen = [], set()
        for d in search_dirs:
            if not d or not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                for f in files:
                    low = f.lower()
                    if not low.endswith((".ttf", ".ttc", ".otf")):
                        continue
                    if any(b in low for b in blocked):
                        continue
                    # 明确非中文的字体族（日文 IPA / 泰文 Tlwg / 老挝 / 缅甸 / 阿拉伯 /
                    # 希腊 / 希伯来等）即使含少量 CJK 也不作为中文渲染字体，避免方块
                    if any(s in low for s in ("ipa", "jpn", "jp", "thai", "tlwg",
                                             "loma", "khmer", "lao", "burmese",
                                             "tamil", "deva", "arab", "hebrew", "greek")):
                        continue
                    path = os.path.join(root, f)
                    if path in seen:
                        continue
                    seen.add(path)
                    if any(s in low for s in best):
                        score = 3
                    elif any(r in low for r in last_resort):
                        score = -1
                    elif any(w in low for w in weak):
                        score = 1
                    else:
                        score = 0
                    candidates.append((-score, path))
        candidates.sort()
        self._font_scanned = True
        logger.info(f"观影报告推送 扫描到字体候选 {len(candidates)} 个："
                    f"{[p for _, p in candidates]}")
        for _score, path in candidates:
            if self._font_supports_cjk(path):
                self._cjk_font = path
                logger.info(f"观影报告推送 使用中文字体：{self._cjk_font}")
                return self._cjk_font
        logger.warning("观影报告推送 未找到支持中文的字体，标题中文将显示为方块。"
                       "请放入一个中文字体（如 DroidSansFallbackFull.ttf / NotoSansCJK-Regular.ttc / msyh.ttc）"
                       "并在设置中填写 font_path 指定其绝对路径。")
        return None

    def _get_font(self, size: int):
        path = self._primary_font()
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    def _primary_font(self):
        # 已解析过则复用（避免重复日志/重复探测）
        cached = getattr(self, "_primary_font_resolved", None)
        if cached:
            return cached
        path = None
        # 1) 手动指定
        if self._font_path and os.path.exists(self._font_path):
            path = self._font_path
            logger.info(f"观影报告推送 使用手动指定字体：{path}")
        # 2) 自动探测（优先 wqy-zenhei，含降级）
        if not path:
            path = self._find_cjk_font()
        self._primary_font_resolved = path
        logger.info(f"观影报告推送 最终中文字体：{path}")
        return path

    def _get_num_font(self, size: int):
        path = self._number_font()
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return self._get_font(size)

    def _number_font(self):
        # 优先用独立的、字形清爽的拉丁/数字字体（与中文区分，更有层次）
        if getattr(self, "_number_font_resolved", False):
            return getattr(self, "_number_font_path", None)
        self._number_font_resolved = True
        cands = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/TTF/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                 "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                 "/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf",
                 "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
                 "/usr/share/fonts/truetype/open-sans/OpenSans-Regular.ttf",
                 "/usr/share/fonts/truetype/arimo/Arimo-Regular.ttf")
        for c in cands:
            if os.path.exists(c):
                self._number_font_path = c
                logger.info(f"观影报告推送 使用数字字体：{c}")
                return c
        self._number_font_path = None
        logger.info("观影报告推送 未找到独立数字字体，数字沿用中文字体")
        return None  # 回退到中文字体（含拉丁字形）

    # ------------------------------------------------------------------ #
    # Emby 取数 / 取封面（沿用成熟实现）
    # ------------------------------------------------------------------ #
    @cache.memoize(ttl=300)
    def _get_series_id(self, user_id, item_id):
        try:
            ok, data = self.items(user_id, item_id)
            if ok and data:
                return data.get("SeriesId")
        except Exception:
            pass
        return None

    @cache.memoize(ttl=600)
    def primary(self, item_id, width=480, height=720, quality=90, ret_url=False):
        try:
            url = self._EMBY_HOST + f"/emby/Items/{item_id}/Images/Primary?" \
                                    f"maxHeight={height}&maxWidth={width}&quality={quality}"
            if ret_url:
                return url
            resp = RequestUtils().get_res(url=url)
            if resp.status_code not in (200, 204):
                return False, "Emby 服务器连接失败!"
            return True, resp.content
        except Exception as e:
            logger.error(f"获取封面失败：{e}")
            return False, "Emby 服务器连接失败!"

    @cache.memoize(ttl=300)
    def items(self, user_id, item_id):
        try:
            url = f"{self._EMBY_HOST}/emby/Users/{user_id}/Items/{item_id}?api_key={self._EMBY_APIKEY}"
            resp = RequestUtils().get_res(url=url)
            if resp.status_code not in (200, 204):
                return False, "Emby 服务器连接失败!"
            return True, resp.json()
        except Exception:
            return False, "Emby 服务器连接失败!"

    def get_report(self, days, types=None, user_id=None, limit=10):
        if not types:
            types = self.PLAYBACK_REPORTING_TYPE_MOVIE
        # 让查询窗口与报告上显示的具体日期一致：days=1 时严格只取「昨天」一整天，
        # 不再把今天的数据混进昨日报告；days>1 时取 [N天前, 昨天] 共 N 天。
        now = datetime.now(pytz.timezone("Asia/Shanghai"))
        if int(days) == 1:
            day = now - timedelta(days=1)
            start_time = day.strftime("%Y-%m-%d 00:00:00")
            end_time = day.strftime("%Y-%m-%d 23:59:59")
        else:
            start_time = (now - timedelta(days=int(days))).strftime("%Y-%m-%d 00:00:00")
            end_time = (now - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        sql = "SELECT UserId, ItemId, ItemType, "
        sql += types + " AS name, "
        sql += "COUNT(1) AS play_count, "
        sql += "SUM(PlayDuration - PauseDuration) AS total_duration "
        sql += "FROM PlaybackActivity "
        sql += f"WHERE ItemType = '{'Movie' if types == self.PLAYBACK_REPORTING_TYPE_MOVIE else 'Episode'}' "
        sql += f"AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}' "
        sql += "AND UserId not IN (select UserId from UserList) "
        if user_id:
            sql += f"AND UserId = '{user_id}' "
        # 按「账号 + 片名」分组：否则多个账号看同一部片会被合并成一条，无法按账号分类。
        # 这里先多取一些，再在 Python 里按账号各取 Top limit。
        sql += "GROUP BY UserId, name "
        sql += "ORDER BY total_duration DESC "
        sql += "LIMIT 1000"

        url = f"{self._EMBY_HOST}/emby/user_usage_stats/submit_custom_query?api_key={self._EMBY_APIKEY}"
        data = {"CustomQueryString": sql, "ReplaceUserId": False}
        try:
            resp = RequestUtils().post_res(url=url, data=data)
            if resp.status_code not in (200, 204):
                return False, "Emby 服务器连接失败!"
            ret = resp.json()
            if len(ret.get("colums", [])) == 0:
                return False, ret.get("message", "无数据")
            return True, ret["results"]
        except Exception as e:
            logger.error(f"查询观影数据失败：{e}")
            return False, str(e)
