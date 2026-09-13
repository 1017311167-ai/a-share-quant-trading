"""消息推送模块：邮件 + 企业微信机器人

统一 Notifier 类，回测完成 / 交易信号 / 风控告警 三种场景一键推送：
    - 邮件：基于 smtplib，支持 HTML 正文和附件（如回测报告 Excel）
    - 企业微信机器人：基于 Webhook，支持 text / markdown 两种消息格式
所有密钥从项目根目录的 .env 读取（先复制 .env.example 为 .env 再填写），
代码里不出现任何真实密码。推送失败只记录日志、返回错误信息，绝不中断主程序。

用法:
    from notification.notifier import Notifier

    notifier = Notifier()
    notifier.send_trade_signal({"股票代码": "600519", "股票名称": "贵州茅台",
                                "信号": "买入", "价格": 1400.0})
    notifier.send_backtest_finished(results_df, attachments=["回测报告.xlsx"])
    notifier.send_risk_alert("最大回撤超过 25%，建议减仓")

运行方式（本地测试，模拟服务器，不会真的发出任何消息）:
    python notification/notifier.py
"""

import html
import logging
import os
import smtplib
import sys
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

# 保证直接运行本文件时能找到项目根目录（读取 .env 用）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("notification")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
                        datefmt="%H:%M:%S")

# 渠道内部名 -> 中文显示名
CHANNEL_NAMES = {"email": "邮件", "wecom": "企业微信"}

# 渠道别名（调用时写中文英文都可以）
CHANNEL_ALIASES = {"email": "email", "mail": "email", "邮件": "email",
                   "wecom": "wecom", "wechat": "wecom", "企业微信": "wecom"}

# 企业微信机器人消息长度上限（字节）
WECOM_TEXT_LIMIT = 2048
WECOM_MARKDOWN_LIMIT = 4096

# A股习惯配色：买入红、卖出绿（用于邮件 HTML 正文）
BUY_COLOR = "#d03b3b"
SELL_COLOR = "#0ca30c"

# 常见附件的 MIME 子类型
_SUBTYPES = {".xlsx": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             ".xls": "vnd.ms-excel", ".pdf": "pdf", ".csv": "csv",
             ".png": "png", ".jpg": "jpeg", ".txt": "plain"}


def _project_root() -> Path:
    """项目根目录（notification/ 的上一级）"""
    return Path(__file__).resolve().parent.parent


def _env_bool(key: str, default: bool = False) -> bool:
    """读布尔型环境变量：true/1/yes/on 为真，其余为假；空值用默认值"""
    v = os.getenv(key, "").strip().lower()
    if not v:
        return default
    return v in ("true", "1", "yes", "on")


def _env_int(key: str, default: int) -> int:
    """读整数型环境变量：解析失败回退默认值"""
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def load_config() -> dict:
    """从项目根目录的 .env 读取推送配置（密钥不硬编码在代码里）"""
    load_dotenv(_project_root() / ".env")   # 开发环境：项目根目录
    load_dotenv(Path.cwd() / ".env")        # 打包成 exe 后：exe 同目录（已存在的变量不会被覆盖）
    return {
        "email": {
            "enabled": _env_bool("NOTIFY_EMAIL", True),       # 渠道开关
            "host": os.getenv("SMTP_HOST", ""),               # SMTP 服务器
            "port": _env_int("SMTP_PORT", 465),               # 端口
            "ssl": _env_bool("SMTP_SSL", True),               # 是否 SSL 直连
            "user": os.getenv("SMTP_USER", ""),               # 发件邮箱
            "password": os.getenv("SMTP_PASSWORD", ""),       # SMTP 授权码
            "to": os.getenv("MAIL_TO", ""),                   # 收件人（逗号分隔）
        },
        "wecom": {
            "enabled": _env_bool("NOTIFY_WECOM", True),       # 渠道开关
            "webhook": os.getenv("WECOM_WEBHOOK", ""),        # 机器人 Webhook 地址
        },
    }


def _email_ready(cfg: dict) -> bool:
    return bool(cfg["host"] and cfg["user"] and cfg["password"] and cfg["to"])


def _wecom_ready(cfg: dict) -> bool:
    return bool(cfg["webhook"])


def _truncate_bytes(text: str, limit: int) -> str:
    """按字节数截断文本（中文 UTF-8 占 3 字节），避免截出半个汉字"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = "\n……（内容过长已截断）"
    return raw[: limit - len(suffix.encode("utf-8"))].decode("utf-8", "ignore") + suffix


def _dict_to_html_table(d: dict, color: str | None = None) -> str:
    """字典转简单 HTML 表格（邮件正文用）；color 给值上色"""
    style = f" style='color:{color};'" if color else ""
    rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td>"
        f"<td{style}><b>{html.escape(str(v))}</b></td></tr>"
        for k, v in d.items())
    return "<table border='0' cellpadding='4' style='border-collapse:collapse'>" + rows + "</table>"


class Notifier:
    """统一消息推送器：邮件 + 企业微信机器人，可多渠道同时推送

    参数:
        config:   推送配置字典。默认 None = 自动从 .env 读取（load_config()）；
                  测试时可以传入假配置，避免真的发消息
        channels: 默认推送渠道。None = 按 .env 里的开关自动选择；
                  也可以写 "all"、["邮件", "企业微信"] 等
    """

    def __init__(self, config: dict | None = None, channels=None):
        self.config = config if config is not None else load_config()
        if channels is not None:
            self.default_channels = self._resolve_channels(channels)
        else:
            self.default_channels = self._enabled_channels()

    # ---------- 渠道管理 ----------

    def _enabled_channels(self) -> list:
        """按 .env 开关 + 配置完整性，自动选择可用渠道"""
        out = []
        email = self.config["email"]
        if email["enabled"]:
            if _email_ready(email):
                out.append("email")
            else:
                logger.warning("邮件渠道已开启，但 .env 配置不完整"
                               "（需要 SMTP_HOST/SMTP_USER/SMTP_PASSWORD/MAIL_TO），已跳过")
        wecom = self.config["wecom"]
        if wecom["enabled"]:
            if _wecom_ready(wecom):
                out.append("wecom")
            else:
                logger.warning("企业微信渠道已开启，但 .env 未配置 WECOM_WEBHOOK，已跳过")
        return out

    @staticmethod
    def _resolve_channels(channels) -> list:
        """渠道参数转内部名列表；支持 "all"、单个字符串、列表"""
        if channels is None:
            return []
        if isinstance(channels, str):
            channels = list(CHANNEL_NAMES) if channels == "all" else [channels]
        else:
            channels = list(channels)
            if "all" in channels:
                channels = list(CHANNEL_NAMES)
        out = []
        for c in channels:
            key = CHANNEL_ALIASES.get(c, c)
            if key not in CHANNEL_NAMES:
                raise ValueError(f"未知推送渠道：{c!r}，可选：{list(CHANNEL_NAMES.values())}"
                                 f" / {list(CHANNEL_NAMES)} / 'all'")
            if key not in out:
                out.append(key)
        return out

    @staticmethod
    def _safe(channel: str, fn, *args, **kwargs):
        """执行一次推送：失败只记日志并返回错误信息，绝不抛出异常"""
        try:
            fn(*args, **kwargs)
            return True
        except Exception as e:
            logger.error(f"{channel} 推送失败：{type(e).__name__}: {e}")
            return f"{type(e).__name__}: {e}"

    # ---------- 邮件渠道 ----------

    def _build_email_message(self, subject: str, text_body: str = "",
                             html_body: str | None = None,
                             attachments: list | None = None) -> MIMEMultipart:
        """拼装邮件：主题 + 纯文本 + HTML + 附件（附件自动识别类型）"""
        cfg = self.config["email"]
        msg = MIMEMultipart()
        msg["From"] = cfg["user"]
        msg["To"] = cfg["to"]
        msg["Subject"] = Header(subject, "utf-8")

        if html_body:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(text_body or "", "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)
        else:
            msg.attach(MIMEText(text_body, "plain", "utf-8"))

        for path in attachments or []:
            p = Path(path)
            if not p.is_file():
                raise FileNotFoundError(f"附件不存在：{p}")
            part = MIMEApplication(p.read_bytes(),
                                  _subtype=_SUBTYPES.get(p.suffix.lower(), "octet-stream"))
            part.add_header("Content-Disposition", "attachment",
                            filename=("utf-8", "", p.name))
            msg.attach(part)
        return msg

    def _send_email(self, subject: str, text_body: str = "",
                    html_body: str | None = None,
                    attachments: list | None = None):
        """用 smtplib 发送邮件（SSL 直连或 STARTTLS 自动选择）"""
        cfg = self.config["email"]
        msg = self._build_email_message(subject, text_body, html_body, attachments)
        recipients = [addr.strip() for addr in cfg["to"].split(",") if addr.strip()]
        if cfg["ssl"]:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15) as server:
                server.login(cfg["user"], cfg["password"])
                server.sendmail(cfg["user"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
                server.starttls()
                server.login(cfg["user"], cfg["password"])
                server.sendmail(cfg["user"], recipients, msg.as_string())
        logger.info(f"邮件已发送：{subject} -> {cfg['to']}")

    # ---------- 企业微信渠道 ----------

    @staticmethod
    def _build_wecom_payload(content: str, msgtype: str = "text") -> dict:
        """拼装企业微信机器人消息体；超长自动截断"""
        if msgtype == "text":
            limit = WECOM_TEXT_LIMIT
        elif msgtype == "markdown":
            limit = WECOM_MARKDOWN_LIMIT
        else:
            raise ValueError(f"企业微信不支持的消息类型：{msgtype!r}，可选 text / markdown")
        return {"msgtype": msgtype, msgtype: {"content": _truncate_bytes(content, limit)}}

    def _send_wecom(self, content: str, msgtype: str = "text"):
        """往机器人 Webhook 发消息；errcode 非 0 视为失败"""
        payload = self._build_wecom_payload(content, msgtype)
        resp = requests.post(self.config["wecom"]["webhook"], json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"企业微信返回错误 {data.get('errcode')}：{data.get('errmsg')}")
        logger.info(f"企业微信已发送（{msgtype}）")

    # ---------- 统一发送入口 ----------

    def send(self, subject: str, content: str, *, html_body: str | None = None,
             msgtype: str = "text", attachments: list | None = None,
             channels=None) -> dict:
        """多渠道同时推送（一般直接调用下面三个业务方法）

        参数:
            subject:     邮件标题（企业微信正文里不重复标题）
            content:     纯文本正文
            html_body:   邮件 HTML 版正文（None 时自动把纯文本包一层 <pre>）
            msgtype:     企业微信消息类型：text / markdown
            attachments: 邮件附件路径列表（企业微信机器人不支持附件）
            channels:    本次渠道：None 用默认、'all' 全部、或 ["邮件","企业微信"] 等

        返回:
            {'邮件': True, '企业微信': '错误信息'}
            True = 成功；字符串 = 失败原因（已记日志，不会中断主程序）
        """
        targets = self.default_channels if channels is None else self._resolve_channels(channels)
        if not targets:
            logger.warning("没有可用的推送渠道（请检查 .env 配置），本次通知未发送")
            return {}
        results = {}
        for channel in targets:
            name = CHANNEL_NAMES[channel]
            if channel == "email":
                if not _email_ready(self.config["email"]):
                    results[name] = "未配置：请在 .env 填写 SMTP_HOST/SMTP_USER/SMTP_PASSWORD/MAIL_TO"
                    logger.warning(f"{name} 未配置，跳过")
                    continue
                body = html_body if html_body is not None else f"<pre>{html.escape(content)}</pre>"
                results[name] = self._safe(name, self._send_email,
                                           subject, content, body, attachments or [])
            else:
                if not _wecom_ready(self.config["wecom"]):
                    results[name] = "未配置：请在 .env 填写 WECOM_WEBHOOK"
                    logger.warning(f"{name} 未配置，跳过")
                    continue
                results[name] = self._safe(name, self._send_wecom, content, msgtype)
        return results

    # ---------- 业务通知 ----------

    def send_backtest_finished(self, result_summary, *,
                               attachments: list | None = None,
                               channels=None) -> dict:
        """批量回测 / 参数寻优完成通知

        参数:
            result_summary: 结果汇总，支持两种：
                            - dict：如 {"任务类型": "批量回测", "股票代码": "600519",
                              "策略名称": "双均线", "夏普比率": 1.23, ...}
                            - DataFrame：汇总表（邮件转 HTML 表格，企业微信转纯文本）
            attachments:    邮件附件（如导出的 Excel 报告路径列表）
        """
        if hasattr(result_summary, "to_html"):  # DataFrame
            text = result_summary.to_string(index=False)
            body_html = result_summary.to_html(index=False, border=0)
            subject = "【回测完成】结果汇总"
        elif isinstance(result_summary, dict):
            task = str(result_summary.get("任务类型", "回测任务"))
            code = str(result_summary.get("股票代码", ""))
            strat = str(result_summary.get("策略名称", ""))
            subject = f"【回测完成】{task} {code} {strat}".rstrip()
            text = "\n".join(f"{k}：{v}" for k, v in result_summary.items())
            body_html = _dict_to_html_table(result_summary)
        else:
            subject = "【回测完成】"
            text = str(result_summary)
            body_html = f"<pre>{html.escape(text)}</pre>"
        return self.send(subject, text, html_body=body_html,
                         attachments=attachments, channels=channels)

    def send_trade_signal(self, signal_info: dict, *, channels=None) -> dict:
        """交易信号触发通知

        参数:
            signal_info: dict，常用键：股票代码 / 股票名称 / 信号（买入、卖出）/
                         价格 / 时间 / 策略 / 原因
                         （邮件 HTML 里买入标红、卖出标绿）
        """
        if not isinstance(signal_info, dict):
            signal_info = {"信号": str(signal_info)}
        code = signal_info.get("股票代码", "?")
        name = signal_info.get("股票名称", "")
        action = signal_info.get("信号", signal_info.get("信号类型", "?"))
        subject = f"【交易信号】{code} {name} {action}"
        text = "\n".join(f"{k}：{v}" for k, v in signal_info.items())
        color = (BUY_COLOR if "买" in str(action)
                 else SELL_COLOR if "卖" in str(action) else None)
        return self.send(subject, text,
                         html_body=_dict_to_html_table(signal_info, color),
                         channels=channels)

    def send_risk_alert(self, alert_msg, *, channels=None) -> dict:
        """风控告警通知

        参数:
            alert_msg: 字符串（告警内容）或 dict（如
                       {"风险类型": "最大回撤", "当前回撤": "25%", "建议": "减仓"}）
            企业微信用 markdown 格式加粗展示，邮件用红色正文
        """
        if isinstance(alert_msg, dict):
            text = "\n".join(f"{k}：{v}" for k, v in alert_msg.items())
            # 企业微信正文用 markdown（加粗 + 标题），邮件 HTML 用纯文本标红
            content = "## ⚠️ 风控告警\n\n" + "\n".join(f"**{k}**：{v}"
                                                      for k, v in alert_msg.items())
        else:
            text = str(alert_msg)
            content = "## ⚠️ 风控告警\n\n" + text
        return self.send("【风控告警】", content,
                         html_body=f"<div style='color:{BUY_COLOR}'><pre>{html.escape(text)}</pre></div>",
                         msgtype="markdown", channels=channels)


def run_test():
    """本地测试：用「模拟服务器」验证两个渠道，不会真的发出任何消息

    运行方式（在项目根目录下）：
        python notification/notifier.py
    """
    import tempfile
    from email import message_from_string, policy
    from unittest import mock

    print("===== 消息推送模块测试开始 =====")

    def fake_email_config():
        """只有邮件配置（企业微信关闭）"""
        return {"email": {"enabled": True, "host": "smtp.qq.com", "port": 465,
                          "ssl": True, "user": "test@qq.com", "password": "fake-code",
                          "to": "receiver@qq.com"},
                "wecom": {"enabled": False, "webhook": ""}}

    def fake_wecom_config():
        """只有企业微信配置（邮件关闭）"""
        return {"email": {"enabled": False, "host": "", "port": 465, "ssl": True,
                          "user": "", "password": "", "to": ""},
                "wecom": {"enabled": True,
                          "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake"}}

    def fake_full_config():
        """两个渠道都配置好"""
        c = fake_email_config()
        c["wecom"]["enabled"] = True
        c["wecom"]["webhook"] = fake_wecom_config()["wecom"]["webhook"]
        return c

    # 1. 配置读取：开关解析 + 非法值回退
    with mock.patch.dict(os.environ, {"NOTIFY_EMAIL": "false", "NOTIFY_WECOM": "0",
                                      "SMTP_SSL": "true", "SMTP_PORT": "abc"}):
        assert _env_bool("NOTIFY_EMAIL", True) is False
        assert _env_bool("NOTIFY_WECOM", True) is False
        assert _env_bool("SMTP_SSL", True) is True
        assert _env_int("SMTP_PORT", 465) == 465  # 非法端口值回退默认
    cfg = load_config()
    assert set(cfg) == {"email", "wecom"}
    assert set(cfg["email"]) == {"enabled", "host", "port", "ssl", "user", "password", "to"}
    print("  ✓ 配置读取：.env 开关解析正确，非法值自动回退默认")

    # 2. 邮件消息结构：主题 + 纯文本 + HTML + Excel 附件
    n = Notifier(config=fake_email_config())
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        f.write(b"fake-excel-bytes")
        tmp = f.name
    try:
        msg = n._build_email_message("回测报告", "纯文本正文", "<b>HTML正文</b>", [tmp])
        assert msg["Subject"] == "回测报告"
        raw = msg.as_string()
        # 注意：含中文的正文会被 base64 编码，要解码后检查内容
        parts = {p.get_content_type(): p for p in msg.walk()}
        assert "text/html" in parts and "text/plain" in parts
        assert "<b>HTML正文</b>" in parts["text/html"].get_payload(decode=True).decode("utf-8")
        assert "纯文本正文" in parts["text/plain"].get_payload(decode=True).decode("utf-8")
        assert "Content-Disposition: attachment" in raw
        assert os.path.basename(tmp) in raw
        assert "spreadsheetml" in raw  # xlsx 附件识别出 Excel 类型
    finally:
        os.unlink(tmp)
    print("  ✓ 邮件消息结构：主题 + 纯文本 + HTML + Excel 附件 齐全")

    # 3. 邮件发送：模拟 SMTP 成功发出；连接失败只返回错误信息不中断
    with mock.patch("smtplib.SMTP_SSL") as m_smtp:
        r = n.send("【测试】主题", "正文", channels=["邮件"])
        assert r["邮件"] is True
        m_smtp.assert_called_once_with("smtp.qq.com", 465, timeout=15)
        server = m_smtp.return_value.__enter__.return_value
        server.login.assert_called_once_with("test@qq.com", "fake-code")
        # 主题是 RFC2047 编码的，解析时用 policy.default 才能取到解码值
        parsed = message_from_string(server.sendmail.call_args[0][2], policy=policy.default)
        assert parsed["Subject"] == "【测试】主题"
    with mock.patch("smtplib.SMTP_SSL", side_effect=ConnectionRefusedError("服务器拒绝连接")):
        r = n.send("主题", "正文", channels=["邮件"])
        assert isinstance(r["邮件"], str) and "ConnectionRefusedError" in r["邮件"]
    print("  ✓ 邮件发送：模拟 SMTP 成功发出；连接失败只返回错误信息，不抛异常")

    # 4. 企业微信消息体：text / markdown + 超长截断
    w = Notifier(config=fake_wecom_config())
    assert w._build_wecom_payload("你好", "text") == \
        {"msgtype": "text", "text": {"content": "你好"}}
    p = w._build_wecom_payload("**加粗**", "markdown")
    assert p["msgtype"] == "markdown" and p["markdown"]["content"] == "**加粗**"
    long_text = "参数寻优中" * 800  # 4800 字节，超过 text 上限 2048
    pl = w._build_wecom_payload(long_text, "text")
    assert len(pl["text"]["content"].encode("utf-8")) <= WECOM_TEXT_LIMIT
    assert "已截断" in pl["text"]["content"]
    print("  ✓ 企业微信消息体：text/markdown 正确，超长自动按字节截断")

    # 5. 企业微信推送：成功 / 业务错误 / 网络异常
    ok_resp = mock.MagicMock()
    ok_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
    with mock.patch("requests.post", return_value=ok_resp) as m_post:
        r = w.send("主题", "茅台触发买入信号", channels=["企业微信"])
        assert r["企业微信"] is True
        m_post.assert_called_once()
        assert m_post.call_args.kwargs["json"]["text"]["content"] == "茅台触发买入信号"
    bad_resp = mock.MagicMock()
    bad_resp.json.return_value = {"errcode": 93000, "errmsg": "invalid webhook key"}
    with mock.patch("requests.post", return_value=bad_resp):
        r = w.send("主题", "正文", channels=["企业微信"])
        assert isinstance(r["企业微信"], str) and "93000" in r["企业微信"]
    with mock.patch("requests.post", side_effect=requests.RequestException("网络超时")):
        r = w.send("主题", "正文", channels=["企业微信"])
        assert isinstance(r["企业微信"], str) and "网络超时" in r["企业微信"]
    print("  ✓ 企业微信推送：成功/业务错误/网络异常都按预期处理，失败不中断")

    # 6. 多渠道同时推送 + 未配置/关闭的渠道自动跳过
    full = Notifier(config=fake_full_config())
    with mock.patch("smtplib.SMTP_SSL"), mock.patch("requests.post", return_value=ok_resp):
        r = full.send("主题", "正文", channels="all")
        assert r == {"邮件": True, "企业微信": True}
    with mock.patch("smtplib.SMTP_SSL"):  # n 没配 webhook，邮件也要模拟，避免真实联网
        r = n.send("主题", "正文", channels="all")
    assert r["邮件"] is True and "未配置" in r["企业微信"]
    empty = Notifier(config={"email": {**fake_email_config()["email"], "enabled": False},
                             "wecom": {**fake_wecom_config()["wecom"], "enabled": False}})
    assert empty.send("主题", "正文") == {}  # 渠道全关：不推送也不报错
    print("  ✓ 多渠道同时推送：邮件+企业微信可同时发出，未配置/已关闭的渠道自动跳过")

    # 7. 三个业务方法：回测完成 / 交易信号 / 风控告警
    with mock.patch("requests.post", return_value=ok_resp) as m_post:
        w.send_backtest_finished({"任务类型": "参数寻优", "股票代码": "600519",
                                  "策略名称": "双均线", "最优夏普比率": 1.23})
        c1 = m_post.call_args.kwargs["json"]["text"]["content"]
        assert "最优夏普比率" in c1 and "600519" in c1

        w.send_trade_signal({"股票代码": "600519", "股票名称": "贵州茅台",
                             "信号": "买入", "价格": 1400.0})
        c2 = m_post.call_args.kwargs["json"]["text"]["content"]
        assert "买入" in c2 and "贵州茅台" in c2

        w.send_risk_alert({"风险类型": "最大回撤", "当前回撤": "25.6%", "建议": "减仓"})
        last = m_post.call_args.kwargs["json"]
        assert last["msgtype"] == "markdown" and "⚠️ 风控告警" in last["markdown"]["content"]
        assert "**风险类型**：最大回撤" in last["markdown"]["content"]
    import pandas as pd
    with mock.patch("requests.post", return_value=ok_resp):
        df = pd.DataFrame([{"股票代码": "600519", "夏普比率": 1.2}])
        assert w.send_backtest_finished(df)["企业微信"] is True  # DataFrame 汇总表也能发
    with mock.patch("smtplib.SMTP_SSL") as m_smtp:
        n.send_trade_signal({"股票代码": "600519", "股票名称": "贵州茅台",
                             "信号": "买入", "价格": 1400.0})
        raw = m_smtp.return_value.__enter__.return_value.sendmail.call_args[0][2]
        parsed = message_from_string(raw, policy=policy.default)
        html_part = next(p for p in parsed.walk() if p.get_content_type() == "text/html")
        body = html_part.get_payload(decode=True).decode("utf-8")
        assert BUY_COLOR in body and "600519" in body  # 买入信号邮件标红
        assert SELL_COLOR in _dict_to_html_table({"信号": "卖出"}, SELL_COLOR)
    print("  ✓ 三个业务方法：回测完成/交易信号/风控告警 内容正确（买入红/卖出绿/markdown加粗）")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
