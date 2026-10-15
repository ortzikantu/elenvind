"""控制台彩色输出：只用 ANSI 转义序列，不引入依赖。"""

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"

def _format(color: str, tag: str, msg: str) -> str:
    return f"{BOLD}\033[92mELENVIND: {color}[{tag}]{RESET} {msg}"


def _emit(line: str) -> None:
    """立即写出一行并冲刷。

    `flush=True` 不是装饰：stdout 被重定向到文件/管道（systemd journal、`> log`）
    时是**块缓冲**的，而 Gunicorn 退出路径会走 `os._exit()`，缓冲区不会落盘 ——
    实测表现为"启动横幅和 INFO 行整个消失"。启动信息恰恰是排障时最需要的，
    因此这里逐行冲刷。
    """
    print(line, flush=True)


def success(msg: str):
    _emit(_format(GREEN, "SUCCESS", msg))

def error(msg: str):
    _emit(_format(RED, "ERROR", msg))

def warning(msg: str):
    _emit(_format(YELLOW, "WARNING", msg))

def info(msg: str):
    _emit(_format(BLUE, "INFO", msg))

def banner(msg: str):
    """打印醒目的加粗标题横幅"""
    _emit(f"{CYAN}{BOLD}{msg}{RESET}")
