import subprocess
import base64
import time
import re
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional
import os
import configparser
from openai import OpenAI
import httpx

"""
微信半视觉自动回复机器人（ADB + UI 层级）
- 监听：用 uiautomator dump 抓取当前界面 XML，解析最后一条对方消息（依据左侧气泡）
- 回复：用 ADB Keyboard 发送 Unicode 文本，再点击“发送”按钮

使用前请校准：
1) 请把目标微信会话置顶并打开，使其保持前台；或设置 CONFIG["CHAT_ENTRY"] 让脚本自动点进去。
2) 改好 CONFIG["SEND_BTN"] 为你机型上“发送按钮”的像素坐标（先用 adb shell input tap 试探）。
3) 先运行一次 set_adb_keyboard() 切到 ADBKeyboard 输入法。
4) 仅在你自己的设备上，并在所有相关人知情同意的前提下使用。
"""

# ======================== 配置区域 ========================
CONFIG = {
    # 必填：发送按钮坐标（像素）。示例值需按你的设备改！
    "SEND_BTN": (960, 2260),
    # 可选：会话列表首行坐标，用于进入置顶会话（不需要可设为 None）
    "CHAT_ENTRY": (200, 380),   # 或 None
    # 轮询间隔（秒）
    "POLL_INTERVAL": 0.1,
    # 只对这些联系人/群名生效（None 表示对所有）。需要的话实现 current_chat_title() 做匹配。
    "WHITELIST": None,  # 例如：["小王", "项目群"]
    "OPENAI_MODEL": "gpt-4o-mini",
    "CHANGED_BALANCE": 20000,
    "DEMAND_BALANCE": 28600,
    "TIME_BALANCE": 0,
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "",
    "DEFAULT_TOKEN": "",
    "MAX_TOKENS": 200,
    "MENTIONS": [],
}

inChat = False

# ========================================================
def load_config(path: str = "config.ini"):
    if not os.path.exists(path):
        return
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        cfg = parser[parser.default_section]

        tuple_keys = {"SEND_BTN", "CHAT_ENTRY"}
        float_keys = {"POLL_INTERVAL"}
        int_keys = {"DEMAND_BALANCE", "TIME_BALANCE", "CHANGED_BALANCE", "MAX_TOKENS"}
        list_keys = {"WHITELIST", "MENTIONS"}

        for k, v in cfg.items():
            key = k.upper()
            val = v.strip()
            if not val or key not in CONFIG:
                continue
            if key in tuple_keys:
                if val.lower() == "none":
                    CONFIG[key] = None
                    continue
                try:
                    CONFIG[key] = tuple(int(x) for x in val.split(","))
                except ValueError:
                    print(f"[CONFIG] 无效坐标 {key}: {val}")
            elif key in float_keys:
                CONFIG[key] = float(val)
            elif key in int_keys:
                CONFIG[key] = int(val)
            elif key in list_keys:
                CONFIG[key] = [x.strip() for x in val.split(",") if x.strip()]
            else:
                CONFIG[key] = val

    except Exception as e:
        print("[CONFIG] 读取配置失败:", e)


def save_config(path: str = "config.ini"):
    parser = configparser.ConfigParser()
    try:
        parser[parser.default_section] = {}
        for k, v in CONFIG.items():
            if isinstance(v, tuple):
                parser[parser.default_section][k] = ",".join(str(x) for x in v)
            elif isinstance(v, list):
                parser[parser.default_section][k] = ",".join(v)
            else:
                parser[parser.default_section][k] = str(v)
        with open(path, "w", encoding="utf-8") as f:
            parser.write(f)
        print(f"[CONFIG] 已写入当前配置到 {path}")
    except Exception as e:
        print("[CONFIG] 写入失败:", e)


# -------------------- ADB 工具封装 --------------------
def adb(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True).decode(errors="ignore")
    except subprocess.CalledProcessError as e:
        print("[ADB] 命令失败:", e)
        return ""


def tap(x: int, y: int):
    adb(f"adb shell input tap {int(x)} {int(y)}")


def swipe(x1, y1, x2, y2, dur_ms=200):
    adb(f"adb shell input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(dur_ms)}")


def set_adb_keyboard():
    adb("adb shell ime enable com.android.adbkeyboard/.AdbIME")
    adb("adb shell ime set com.android.adbkeyboard/.AdbIME")
    print("[IME] 已切换到 ADBKeyboard")


def reset_keyboard(ime_id: str):
    """切回系统键盘，ime_id 可通过 adb shell ime list -s 查询"""
    adb(f"adb shell ime set {ime_id}")


def send_text(text: str):
    """通过 ADBKeyboard 发送 Unicode 文本（推荐 Base64）"""
    b64 = base64.b64encode(text.encode("utf-8")).decode()
    adb(f"adb shell am broadcast -a ADB_INPUT_B64 --es msg {b64}")


# -------------------- UI 抓取与解析 --------------------
def is_windows() -> bool:
    import os
    return os.name == "nt"


def dump_ui(xml_path: str = "/sdcard/view.xml") -> Optional[ET.Element]:
    adb(f"adb shell uiautomator dump {xml_path} 2>/dev/null")
    adb(f"adb pull {xml_path} . >nul 2>&1" if is_windows() else f"adb pull {xml_path} . >/dev/null 2>&1")
    try:
        return ET.parse(xml_path.split("/")[-1]).getroot()
    except Exception as e:
        print("[UI] 解析失败:", e)
        return None


def parse_bounds(b: str) -> Tuple[int, int, int, int]:
    # 形如 [x1,y1][x2,y2]
    nums = re.findall(r"[0-9]+", b or "0 0 0 0")
    x1, y1, x2, y2 = map(int, nums[:4]) if len(nums) >= 4 else (0, 0, 0, 0)
    return x1, y1, x2, y2


def extract_text_nodes(root: ET.Element) -> List[Tuple[int, int, int, int, str]]:
    """提取所有 TextView 文本，返回 (x1,y1,x2,y2,text) 列表"""
    items = []
    for node in root.iter("node"):
        cls = (node.get("class") or "")
        if not cls.endswith("TextView"):
            continue
        text = (node.get("text") or "").strip()
        if not text:
            continue
        # 过滤时间戳（如 12:30 / 下午 3:21），可按需扩展
        if re.match(r"^[0-9]{1,2}:[0-9]{2}$", text):
            continue
        b = node.get("bounds") or ""
        x1, y1, x2, y2 = parse_bounds(b)
        items.append((x1, y1, x2, y2, text))
    return items


def screen_width(items: List[Tuple[int, int, int, int, str]]) -> int:
    return max((x2 for (_, _, x2, _, _) in items), default=0)


def last_incoming_message(root: ET.Element) -> Optional[str]:
    """左侧气泡（x1 < 屏宽/2）≈ 对方消息；返回最后一条文本"""
    items = extract_text_nodes(root)
    if not items:
        return None
    W = screen_width(items)
    if W <= 0:
        return None
    msgs = [(y1, x1, text) for (x1, y1, x2, y2, text) in items if y1 > 300]
    msgs.sort()  # 从上到下
    incoming = [t for (y1, x1, t) in msgs if x1 < W * 0.5]
    return incoming[-10:] if incoming else None


def current_chat_title(root: ET.Element) -> Optional[str]:
    """可选：解析当前聊天窗口标题（便于做白名单）"""
    cands = []
    mentions = CONFIG.get('MENTIONS') or []
    for node in root.iter("node"):
        cls = (node.get("class") or "")
        text = (node.get("text") or "").strip()
        x = parse_bounds(node.get("bounds") or "0,0,0,0")[0]
        y = parse_bounds(node.get("bounds") or "0,0,0,0")[1]
        if cls.endswith("TextView") and text and y < 250:
            cands.append((y, text))
        global inChat
        if ('有人@我' in text or any(m in text for m in mentions)) and not inChat:
            tap(x, y)
            inChat = True
    cands.sort()
    return cands[0][1] if cands else None


# -------------------- 业务策略与发送 --------------------
def gpt_reply(msg: str) -> str:
    try:
        base_url = CONFIG['OPENAI_BASE_URL']
        api_key = CONFIG['OPENAI_API_KEY']
        default_token = CONFIG['DEFAULT_TOKEN']
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=httpx.Client(
                base_url=base_url,
                follow_redirects=True,
            ),
        )
        payload = [
                       {"role": "system", "content": default_token},
                       {"role": "user", "content": msg}
                   ]
        model = CONFIG['OPENAI_MODEL']
        resp = client.chat.completions.create(
            model=model,
            messages=payload,
            max_tokens=int(CONFIG.get('MAX_TOKENS', 200)),
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print("[GPT] 调用失败:", e)
        return "GPT调用失败，请稍后再试~"


def gen_reply(msg: str) -> Optional[str]:
    try:
        title = current_chat_title(dump_ui())
        s = msg.strip()
        mentions = CONFIG.get('MENTIONS') or []
        if any(m in s for m in mentions):
            if "help" in s:
                return "help菜单：\n1、@我与我互动\n"
            if s.startswith('gpt'):
                return gpt_reply(s)
            if not s:
                return None
            if title == "豆包家族˶ 'ᵕ' ੭(3)":
                if "查询金库" in s or "金库查询" in s:
                    return f"【豆包家族大金库】\n💌活期余额：{CONFIG['DEMAND_BALANCE']}元\n💌定期余额：{CONFIG['TIME_BALANCE']}元"
                if s.startswith('金库'):
                    try:
                        m = re.match(r".*(活期|定期)(-?\d+)(.*)", s)
                        if not m:
                            return "输入格式错误，请用 '存钱活期1000工资' 这样的格式。"
                        account_type, money_str, reason = m.groups()
                        money = int(money_str)
                        reason = reason.strip() if reason else "未注明"
                        if "活期" in account_type:
                            CONFIG['DEMAND_BALANCE'] = int(CONFIG['DEMAND_BALANCE']) + money
                        elif "定期" in account_type:
                            CONFIG['TIME_BALANCE'] = int(CONFIG['TIME_BALANCE']) + money
                        save_config()
                        return f"【豆包家族大金库】\n📩{'➕' if money >= 0 else '➖'}{abs(money)}元（{reason}）\n💌活期余额：{CONFIG['DEMAND_BALANCE']}元\n💌定期余额：{CONFIG['TIME_BALANCE']}元"
                    except Exception as e:
                        return f"存钱操作失败: {e}"
            return gpt_reply(s)
        else:
            return None
    except Exception as e:
        print("[gen_reply] 异常:", e)
        return None


def send_and_click(text: str):
    entry = CONFIG.get('CHAT_ENTRY')
    if entry is None:
        print("无需点击聊天入口")
    else:
        x, y = entry
        tap(x, y)
        time.sleep(1)
    send_text(text)
    time.sleep(0.5)
    sx, sy = CONFIG["SEND_BTN"]
    if not sx or not sy:
        raise RuntimeError("请在 CONFIG['SEND_BTN'] 中设置发送按钮坐标")
    tap(sx, sy)


# -------------------- 可选：导航到会话 --------------------
def enter_top_chat():
    entry = CONFIG.get("CHAT_ENTRY")
    if entry:
        x, y = entry
        tap(x, y)
        time.sleep(0.5)


# -------------------- 主循环 --------------------
def main_loop():
    set_adb_keyboard()
    time.sleep(0.5)
    last_hash = None

    # 可选：确保进入会话
    # enter_top_chat()

    while True:
        count = 0
        root = dump_ui()
        if root is None:
            time.sleep(CONFIG["POLL_INTERVAL"])
            continue

        title = current_chat_title(root)
        # wl = CONFIG.get("WHITELIST")
        # if (wl is not None) and title and (title not in wl):
        #     time.sleep(CONFIG["POLL_INTERVAL"])
        #     continue

        msg = last_incoming_message(root)
        mentions = CONFIG.get('MENTIONS') or []
        lastmsg = None
        if msg:
            lastmsg = next((x for x in reversed(msg) if any(m in x for m in mentions)), None)
        # print(msg)
        global inChat
        if msg:
            h = hash((title, lastmsg))
            if h != last_hash:
                print(f"[MSG] @{title or '?'}: {lastmsg}")
                reply = gen_reply(lastmsg)
                if reply:
                    send_and_click("以下内容为GPT自动回复：\n"+reply)
                    print(f"[SEND] {reply}")
                    time.sleep(1)
                    # tap(95, 175)
                    inChat = False
                last_hash = h
        if count > 10:
            tap(95, 175)
            inChat = False
            count = 0
        count += 1
        time.sleep(CONFIG["POLL_INTERVAL"])


if __name__ == "__main__":
    load_config()
    main_loop()
