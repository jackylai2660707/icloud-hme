#!/usr/bin/env python3
"""
iCloud Hide My Email — 定时自动创建调度器
============================================
挂在服务器上持续运行：
  - 启动后立即开始创建，一直创建到 iCloud 上限
  - 每到一个整点 (XX:00) 再次自动触发一轮
  - 每轮一直创建到上限为止，然后休眠等待下一个整点

用法:
    python scheduler.py                          # 前台运行 (从Chrome提取cookie)
    python scheduler.py --cookies cookies.json   # 使用手动导出的cookie
    python scheduler.py -d                       # 后台运行 (守护进程, Windows用)
    python scheduler.py --interval 30            # 每30分钟一轮 (默认60分钟整点)

信号:
    Ctrl+C  优雅退出 (会等当前轮次完成)
    SIGTERM 同上

日志:
    自动写入 logs/ 目录，按日期滚动
    结果写入 results/ 目录，按时间戳命名
"""

import sys
import os
import json
import time
import signal
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List

# 确保可以导入同目录的 icloud_hme
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from icloud_hme import ICloudHME, extract_chrome_cookies

# ============================================================
# 配置
# ============================================================

LOG_DIR = HERE / "logs"
RESULT_DIR = HERE / "results"
STATE_FILE = HERE / "scheduler_state.json"

# iCloud HME 已知上限相关错误关键词
LIMIT_KEYWORDS = [
    "limit", "exceeded", "maximum", "too many",
    "无法创建", "已达上限", "超过限制", "quota",
    "cannot create", "unavailable", "try again later",
    "too many", "rate limit", "429",
]


# ============================================================
# 日志设置
# ============================================================

def setup_logging(verbose: bool = True) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    log_date = datetime.now().strftime("%Y%m%d")
    log_file = LOG_DIR / f"scheduler_{log_date}.log"

    logger = logging.getLogger("icloud_scheduler")
    logger.setLevel(logging.DEBUG)

    # 文件 handler — 详细日志
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # 控制台 handler — 简洁输出
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    ))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ============================================================
# 状态持久化
# ============================================================

def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"total_created": 0, "rounds": [], "last_error": None}


def save_state(state: Dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ============================================================
# 上限检测
# ============================================================

def is_limit_error(error: str) -> bool:
    """判断错误是否由 iCloud 上限/配额触发"""
    lower = error.lower()
    return any(kw in lower for kw in LIMIT_KEYWORDS)


# ============================================================
# 核心: 一轮创建 (一直创建到上限)
# ============================================================

class CreateRound:
    """一轮创建的结果"""

    def __init__(self):
        self.start_time = datetime.now()
        self.end_time: Optional[datetime] = None
        self.created: List[str] = []        # 成功创建的邮箱
        self.errors: List[Dict] = []        # 失败记录
        self.hit_limit = False              # 是否触达上限
        self.fatal_error: Optional[str] = None  # 致命错误 (如cookie过期)


def run_one_round(client: ICloudHME, logger: logging.Logger, label: str = "") -> CreateRound:
    """
    执行一轮创建：一直创建到遇到上限错误为止。
    返回 CreateRound 包含本轮所有结果。
    """
    round_result = CreateRound()
    consecutive_errors = 0
    max_consecutive = 5  # 连续失败 N 次也视为上限/故障

    logger.info(f"══════════ 新一轮开始 ══════════")
    logger.info(f"开始创建，直到触发上限...")

    idx = 1
    while True:
        try:
            alias_label = label or f"Batch {datetime.now().strftime('%m%d%H')}-{idx}"
            result = client.create_alias(label=alias_label, max_retries=3)
            email = result.get("email", "")
            if email:
                round_result.created.append(email)
                consecutive_errors = 0
                logger.info(f"  ✅ [{len(round_result.created)}] {email}")
            else:
                err_msg = "create_alias 返回空邮箱"
                round_result.errors.append({"attempt": idx, "error": err_msg})
                consecutive_errors += 1
                logger.warning(f"  ⚠️  [{idx}] {err_msg}")

        except Exception as e:
            err_str = str(e)
            round_result.errors.append({"attempt": idx, "error": err_str})
            consecutive_errors += 1

            if is_limit_error(err_str):
                logger.info(f"  🛑 触达上限: {err_str[:120]}")
                round_result.hit_limit = True
                break

            # 致命错误: cookie 过期 / 未开通 iCloud+
            if any(kw in err_str.lower() for kw in ["401", "403", "cookie", "session", "validate", "未开通"]):
                logger.error(f"  💀 致命错误: {err_str[:200]}")
                round_result.fatal_error = err_str
                break

            if consecutive_errors >= max_consecutive:
                logger.warning(f"  ⚠️  连续失败 {consecutive_errors} 次，本轮暂停")
                round_result.hit_limit = True
                break

            logger.warning(f"  ⚠️  [{idx}] 失败: {err_str[:100]}")
            time.sleep(2)  # 失败后短暂等待

        idx += 1

    round_result.end_time = datetime.now()
    duration = (round_result.end_time - round_result.start_time).total_seconds()

    logger.info(
        f"本轮结束: 创建 {len(round_result.created)} 个, "
        f"失败 {len(round_result.errors)} 次, "
        f"耗时 {duration:.0f}s, "
        f"状态: {'触达上限' if round_result.hit_limit else '致命错误' if round_result.fatal_error else '正常结束'}"
    )
    return round_result


# ============================================================
# 结果导出
# ============================================================

def save_round_result(round_result: CreateRound, logger: logging.Logger):
    """保存本轮结果到 JSON 文件"""
    ts = round_result.start_time.strftime("%Y%m%d_%H%M%S")
    result_file = RESULT_DIR / f"round_{ts}.json"
    data = {
        "start_time": round_result.start_time.isoformat(),
        "end_time": round_result.end_time.isoformat() if round_result.end_time else None,
        "created_count": len(round_result.created),
        "created": round_result.created,
        "errors": round_result.errors,
        "hit_limit": round_result.hit_limit,
        "fatal_error": round_result.fatal_error,
    }
    result_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug(f"结果已保存: {result_file.name}")


def save_latest_emails(round_result: CreateRound, logger: logging.Logger):
    """追加新创建的邮箱到 latest.txt"""
    if round_result.created:
        latest_file = RESULT_DIR / "latest_emails.txt"
        with open(str(latest_file), "a", encoding="utf-8") as f:
            for email in round_result.created:
                f.write(f"{email}\n")
        logger.debug(f"已追加 {len(round_result.created)} 个邮箱到 latest_emails.txt")


# ============================================================
# 等待到下一个触发点
# ============================================================

def wait_until_next_hour(logger: logging.Logger):
    """计算并休眠到下一个整点 (固定目标，避免边界漂移)"""
    target = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    logger.info(f"下一轮触发: {target.strftime('%H:%M:%S')} (等待 {(target - datetime.now()).total_seconds()/60:.1f} 分钟)")
    logger.info(f"休眠中... (Ctrl+C 退出)")

    while True:
        rem = (target - datetime.now()).total_seconds()
        if rem <= 0:
            break
        time.sleep(min(rem, 30))


# ============================================================
# 调度器主循环
# ============================================================

class Scheduler:
    """iCloud HME 定时调度器"""

    def __init__(
        self,
        cookies: Dict[str, str],
        host: str = "icloud.com",
        label_prefix: str = "",
        verbose: bool = True,
    ):
        self.cookies = cookies
        self.host = host
        self.label_prefix = label_prefix
        self.verbose = verbose
        self.logger = setup_logging(verbose)
        self._running = True
        self._state = load_state()

        # 注册信号处理
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self.logger.info(f"收到退出信号 (signal={signum})，等当前轮次完成后退出...")
        self._running = False

    def _make_client(self) -> ICloudHME:
        return ICloudHME(self.cookies, host=self.host, verbose=self.verbose)

    def run(self):
        """主循环"""
        self.logger.info("=" * 55)
        self.logger.info("iCloud HME 定时调度器 启动")
        self.logger.info(f"累计已创建: {self._state.get('total_created', 0)} 个")
        self.logger.info(f"触发模式: 每整点自动一轮，每轮创建到上限")
        self.logger.info(f"日志目录: {LOG_DIR}")
        self.logger.info(f"结果目录: {RESULT_DIR}")
        self.logger.info("=" * 55)

        round_num = 0

        while self._running:
            round_num += 1
            now = datetime.now()
            label = f"{self.label_prefix}R{round_num} {now.strftime('%m%d%H%M')}" if self.label_prefix else f"Round {round_num} {now.strftime('%m%d%H%M')}"

            # 执行一轮
            client = self._make_client()
            round_result = run_one_round(client, self.logger, label=label)

            # 保存结果
            save_round_result(round_result, self.logger)
            save_latest_emails(round_result, self.logger)

            # 更新状态
            self._state["total_created"] = self._state.get("total_created", 0) + len(round_result.created)
            self._state["rounds"].append({
                "round": round_num,
                "time": now.isoformat(),
                "created": len(round_result.created),
                "hit_limit": round_result.hit_limit,
            })
            # 只保留最近 200 轮记录
            if len(self._state["rounds"]) > 200:
                self._state["rounds"] = self._state["rounds"][-200:]
            self._state["last_error"] = round_result.fatal_error
            save_state(self._state)

            # 致命错误 → 退出
            if round_result.fatal_error:
                self.logger.error(f"致命错误，调度器退出: {round_result.fatal_error[:200]}")
                self._running = False
                break

            if not self._running:
                break

            # 等待到下一个整点
            wait_until_next_hour(self.logger)

        self.logger.info(f"调度器已停止。累计创建: {self._state.get('total_created', 0)} 个")
        save_state(self._state)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="iCloud HME 定时调度器 — 每整点自动创建隐私邮箱到上限",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cookies", "-c", type=str, help="cookies.json 路径 (默认从Chrome自动提取)")
    parser.add_argument("--host", type=str, default="icloud.com",
                       choices=["icloud.com", "icloud.com.cn"])
    parser.add_argument("--label", type=str, default="", help="别名标签前缀")
    parser.add_argument("--quiet", "-q", action="store_true", help="减少控制台输出")
    parser.add_argument("--daemon", "-d", action="store_true", help="后台运行 (Windows 不支持)")

    args = parser.parse_args()

    # 加载 cookies
    if args.cookies:
        if not os.path.isfile(args.cookies):
            print(f"[!] Cookie 文件不存在: {args.cookies}")
            sys.exit(1)
        with open(args.cookies, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        print(f"[+] 从文件加载 {len(cookies)} 个 cookie")
    else:
        print("[*] 从 Chrome 自动提取 iCloud cookies...")
        try:
            cookies = extract_chrome_cookies()
        except RuntimeError as e:
            print(f"[!] {e}")
            sys.exit(1)
        if not cookies:
            print("[!] 未提取到 iCloud cookies，请先登录 icloud.com")
            sys.exit(1)
        print(f"[+] 已提取 {len(cookies)} 个 cookie")

    # 后台运行 (仅 Linux/macOS)
    if args.daemon:
        if sys.platform == "win32":
            print("[!] Windows 不支持 --daemon，请使用 pythonw 或 NSSM 注册服务")
            sys.exit(1)
        # fork 守护进程
        pid = os.fork()
        if pid > 0:
            print(f"[+] 守护进程已启动 (PID={pid})")
            sys.exit(0)
        # 子进程
        os.setsid()
        os.umask(0)

    # 启动调度器
    scheduler = Scheduler(
        cookies=cookies,
        host=args.host,
        label_prefix=args.label,
        verbose=not args.quiet,
    )

    try:
        scheduler.run()
    except KeyboardInterrupt:
        print("\n[!] 已中断")


if __name__ == "__main__":
    main()
