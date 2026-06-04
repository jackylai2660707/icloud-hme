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

from account_manager import AccountManager

# ============================================================
# 配置
# ============================================================

LOG_DIR = HERE / "logs"
RESULT_DIR = HERE / "results"
STATE_FILE = HERE / "scheduler_state.json"


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
# 核心: 一轮创建 (一直创建到上限)
# ============================================================

class CreateRound:
    """一轮创建的结果 (多账号)"""

    def __init__(self):
        self.start_time = datetime.now()
        self.end_time: Optional[datetime] = None
        self.created: List[str] = []               # 成功创建的邮箱
        self.created_by_account: Dict[str, int] = {}  # 按账号统计
        self.errors: List[Dict] = []               # 失败记录
        self.hit_limit = False                     # 是否触达上限
        self.fatal_error: Optional[str] = None     # 致命错误


# iCloud HME 已知上限相关错误关键词
LIMIT_KEYWORDS = [
    "limit", "exceeded", "maximum", "too many",
    "无法创建", "已达上限", "超过限制", "quota",
    "cannot create", "unavailable", "try again later",
    "too many", "rate limit", "429",
]


def is_limit_error(error: str) -> bool:
    """判断错误是否由 iCloud 上限/配额触发"""
    lower = error.lower()
    return any(kw in lower for kw in LIMIT_KEYWORDS)


def run_one_round(mgr: AccountManager, logger: logging.Logger,
                  label: str = "", interval_sec: float = 3.0) -> CreateRound:
    """
    执行一轮创建：每个活跃账号随机创建 3-5 个别名。
    返回 CreateRound 包含本轮所有结果。
    """
    import random as _random

    round_result = CreateRound()
    accounts = mgr.list_accounts()
    active_accounts = [a for a in accounts if a.get("status") == "active"]

    if not active_accounts:
        logger.warning("没有活跃账号，本轮跳过")
        round_result.end_time = datetime.now()
        return round_result

    logger.info(f"══════════ 新一轮开始 ({len(active_accounts)} 个账号, 随机 3-5 个/账号) ══════════")

    for i, account in enumerate(active_accounts):
        acc_id = account["id"]
        acc_name = account.get("name", acc_id)
        acc_email = account.get("real_email", "?")
        target_count = _random.randint(3, 5)

        logger.info(f"—— [{i+1}/{len(active_accounts)}] {acc_name} ({acc_email}) 目标 {target_count} ——")

        created = 0
        errors = 0

        while created < target_count and errors < 3:
            try:
                results = mgr.create_aliases_for_account(
                    acc_id, count=1,
                    label=label or f"{acc_name} {datetime.now().strftime('%m%d%H%M')}-{created+1}",
                )
                if results and len(results) > 0:
                    r = results[0]
                    if r.get("ok") and r.get("email"):
                        email = r["email"]
                        created += 1
                        round_result.created.append(email)
                        round_result.created_by_account[acc_id] = \
                            round_result.created_by_account.get(acc_id, 0) + 1
                        errors = 0
                        logger.info(f"  ✅ ({created}/{target_count}) {email}")
                        time.sleep(_random.uniform(10, 30))
                    else:
                        err_msg = r.get("error", "未知错误")
                        round_result.errors.append({"account": acc_id, "error": err_msg})
                        errors += 1
                        if is_limit_error(err_msg):
                            logger.info(f"  🛑 触达上限: {err_msg[:80]}")
                            break
                        logger.warning(f"  ⚠️  {err_msg[:80]}")
                else:
                    errors += 1
            except Exception as e:
                err_str = str(e)
                round_result.errors.append({"account": acc_id, "error": err_str})
                errors += 1
                if is_limit_error(err_str):
                    logger.info(f"  🛑 触达上限: {err_str[:80]}")
                    break
                if any(kw in err_str.lower() for kw in
                       ["401", "403", "cookie", "session", "validate", "未开通"]):
                    logger.error(f"  💀 账号 {acc_name} 致命错误: {err_str[:200]}")
                    mgr.update_account(acc_id, status="error", last_error=err_str[:300])
                    round_result.fatal_error = err_str
                    break
                logger.warning(f"  ⚠️  {err_str[:80]}")

        if i < len(active_accounts) - 1:
            time.sleep(_random.uniform(120, 300))

    round_result.hit_limit = any(
        is_limit_error(e.get("error", ""))
        for e in round_result.errors
    )
    round_result.end_time = datetime.now()
    duration = (round_result.end_time - round_result.start_time).total_seconds()

    summary = ", ".join(
        f"{mgr.accounts[aid].get('name', aid)[:12]}: {n}"
        for aid, n in round_result.created_by_account.items()
    ) if round_result.created_by_account else "0"

    logger.info(
        f"本轮结束: 创建 {len(round_result.created)} 个 ({summary}), "
        f"失败 {len(round_result.errors)} 次, 耗时 {duration:.0f}s"
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


# ============================================================
# 等待到下一个触发点
# ============================================================

def wait_interval(logger: logging.Logger, seconds: float = 3600):
    """休眠指定秒数 (默认 1 小时)，可中断退出。"""
    target = datetime.now() + timedelta(seconds=seconds)
    logger.info(f"下一轮触发: {target.strftime('%H:%M:%S')} (等待 {seconds/60:.0f} 分钟)")
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
    """iCloud HME 定时调度器 (多账号)"""

    def __init__(
        self,
        mgr: AccountManager,
        label_prefix: str = "",
        interval_sec: float = 3.0,
        verbose: bool = True,
    ):
        self.mgr = mgr
        self.label_prefix = label_prefix
        self.interval_sec = interval_sec
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

    def run(self):
        """主循环"""
        summary = self.mgr.get_summary()
        self.logger.info("=" * 55)
        self.logger.info("iCloud HME 多账号定时调度器 启动")
        self.logger.info(f"账号数: {summary['account_count']} "
                         f"(活跃: {summary['active_accounts']}, "
                         f"异常: {summary['error_accounts']})")
        self.logger.info(f"累计已创建: {self._state.get('total_created', 0)} 个")
        self.logger.info(f"触发模式: BJ 7-20h, 间隔 60-90min, 每账号 3-5 个")
        self.logger.info(f"账号间间隔: {self.interval_sec}s")
        self.logger.info(f"日志目录: {LOG_DIR}")
        self.logger.info(f"结果目录: {RESULT_DIR}")
        self.logger.info("=" * 55)

        round_num = 0

        while self._running:
            round_num += 1
            now = datetime.now()
            label = (f"{self.label_prefix}R{round_num} {now.strftime('%m%d%H%M')}"
                     if self.label_prefix else f"R{round_num} {now.strftime('%m%d%H%M')}")

            # 执行一轮（遍历所有活跃账号）
            round_result = run_one_round(
                self.mgr, self.logger, label=label,
                interval_sec=self.interval_sec,
            )

            # 保存结果
            save_round_result(round_result, self.logger)

            # 更新状态
            self._state["total_created"] = (
                self._state.get("total_created", 0) + len(round_result.created)
            )
            self._state["rounds"].append({
                "round": round_num,
                "time": now.isoformat(),
                "created": len(round_result.created),
                "by_account": round_result.created_by_account,
                "hit_limit": round_result.hit_limit,
            })
            if len(self._state["rounds"]) > 200:
                self._state["rounds"] = self._state["rounds"][-200:]
            self._state["last_error"] = round_result.fatal_error
            save_state(self._state)

            # 致命错误 → 退出
            if round_result.fatal_error:
                self.logger.error(
                    f"致命错误，调度器退出: {round_result.fatal_error[:200]}"
                )
                self._running = False
                break

            if not self._running:
                break

            # 等待 1 小时
            wait_interval(self.logger, 3600)

        self.logger.info(
            f"调度器已停止。累计创建: {self._state.get('total_created', 0)} 个"
        )
        save_state(self._state)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="iCloud HME 多账号定时调度器 — 每整点遍历所有账号创建到上限",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--interval", "-i", type=float, default=3.0,
                       help="账号间间隔秒数 (默认 3.0)")
    parser.add_argument("--label", type=str, default="", help="别名标签前缀")
    parser.add_argument("--quiet", "-q", action="store_true", help="减少控制台输出")
    parser.add_argument("--daemon", "-d", action="store_true", help="后台运行 (Windows 不支持)")

    args = parser.parse_args()

    # 使用 AccountManager
    mgr = AccountManager()
    summary = mgr.get_summary()
    if summary["active_accounts"] == 0:
        print("[!] 没有活跃账号。请先通过 Web UI 添加账号，或确保 accounts.json 中有活跃账号。")
        sys.exit(1)

    print(f"[+] 加载 {summary['account_count']} 个账号 "
          f"(活跃: {summary['active_accounts']}, 异常: {summary['error_accounts']})")

    # 后台运行 (仅 Linux/macOS)
    if args.daemon:
        if sys.platform == "win32":
            print("[!] Windows 不支持 --daemon，请使用 pythonw 或 NSSM 注册服务")
            sys.exit(1)
        pid = os.fork()
        if pid > 0:
            print(f"[+] 守护进程已启动 (PID={pid})")
            sys.exit(0)
        os.setsid()
        os.umask(0)

    # 启动调度器
    scheduler = Scheduler(
        mgr=mgr,
        label_prefix=args.label,
        interval_sec=args.interval,
        verbose=not args.quiet,
    )

    try:
        scheduler.run()
    except KeyboardInterrupt:
        print("\n[!] 已中断")


if __name__ == "__main__":
    main()