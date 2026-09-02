"""
部署环境检查脚本（独立运行）
==============================
集中管理部署前的环境检查：运行期依赖自检/自动安装、本地外部服务探测、
配置校验报告。可单独运行，也可被 run_server.py 复用（避免重复代码）。

用法：
    python prog/scripts/deploy_check.py                 # 全量检查（缺失依赖自动安装）
    python prog/scripts/deploy_check.py --no-install    # 仅检查，不自动安装
    python prog/scripts/deploy_check.py --env prod      # 正式模式：依赖安装失败即报错

退出码：
    0 = 全部就绪（依赖齐全）
    1 = 存在缺失（依赖/服务/配置任一未就绪）
"""

import argparse
import importlib.util
import os
import socket
import subprocess
import sys
from typing import List

# 路径引导：支持 `python prog/scripts/deploy_check.py` 直接运行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 运行期必需依赖（pip 包名, import 名；import 名与包名不一致时显式标注）
REQUIRED_DEPENDENCIES = [
    ("flask", "flask"),
    ("flask-cors", "flask_cors"),
    ("waitress", "waitress"),
    ("sqlalchemy", "sqlalchemy"),
    ("psycopg2-binary", "psycopg2"),
    ("redis", "redis"),
    ("openai", "openai"),
    ("pymilvus", "pymilvus"),
    ("minio", "minio"),
    ("python-docx", "docx"),
    ("openpyxl", "openpyxl"),
    ("pdfplumber", "pdfplumber"),
    ("pypdf", "pypdf"),
    ("PyJWT", "jwt"),
    ("bcrypt", "bcrypt"),
]

# 本地部署外部服务探测点（host, port, 名称）
LOCAL_SERVICES = [
    ("127.0.0.1", 5432, "PostgreSQL"),
    ("127.0.0.1", 6379, "Redis"),
    ("127.0.0.1", 9000, "MinIO"),
    ("127.0.0.1", 19530, "Milvus"),
]


def check_dependencies() -> List[str]:
    """检测缺失的运行期必需依赖（按 import 名探测），返回缺失的 pip 包名列表"""
    missing = []
    for pip_name, import_name in REQUIRED_DEPENDENCIES:
        if importlib.util.find_spec(import_name) is None:
            missing.append(pip_name)
    return missing


def install_dependencies(missing: List[str]) -> bool:
    """自动安装缺失依赖（pip install），失败时打印手动命令并返回 False"""
    print(f"[INFO] 尝试自动安装缺失依赖: {', '.join(missing)} ...")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", *missing],
            capture_output=True, text=True)
        if proc.returncode == 0:
            print("[INFO] 依赖安装完成")
            return True
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        print(f"[WARN] pip 安装失败（exit={proc.returncode}）: {' | '.join(tail)}")
    except Exception as e:
        print(f"[WARN] 自动安装依赖异常: {e}")
    print(f"[INFO] 请手动执行: python -m pip install {' '.join(missing)}")
    return False


def probe_services() -> List[str]:
    """socket 探测本地服务，返回不可达的服务名称列表"""
    down = []
    for host, port, name in LOCAL_SERVICES:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect((host, port))
            except OSError:
                down.append(name)
    return down


def probe_and_hint_services() -> None:
    """本地模式下探测外部服务，缺失仅提示（不阻断，允许内存降级）"""
    down = probe_services()
    if not down:
        print("[INFO] 本地服务探测：PostgreSQL/Redis/MinIO/Milvus 均可达")
        return
    print(f"[WARN] 本地服务未检测到: {', '.join(down)}")
    print("      相关功能将降级为内存模式（配置落库/向量检索/缓存/文件存储不可用）。")
    print("      Windows 一键部署: .\\prog\\deploy-dev-windows.ps1 -StartServices")
    print("      Linux Docker:     docker compose -f prog/docker-compose.yml up -d")


def print_validation_report() -> None:
    """打印配置校验报告（依赖 ConfigLoader/.env 加载）"""
    try:
        from prog.config.config_loader import ConfigLoader
        from prog.config.config_validator import ConfigValidator
        loader = ConfigLoader.get_instance()
        loader.load_config(force=True)
        print(ConfigValidator(loader).get_validation_report())
    except Exception as e:
        print(f"[WARN] 配置校验报告生成失败: {e}")


def main() -> int:
    """独立检查入口。返回退出码（0=就绪，1=存在缺失）。"""
    parser = argparse.ArgumentParser(
        description="AI工厂管家部署环境检查：依赖自检/自动安装 + 服务探测 + 配置校验")
    parser.add_argument("--no-install", action="store_true",
                        help="仅检查，不自动安装缺失依赖")
    parser.add_argument("--env", choices=["dev", "prod"], default="dev",
                        help="环境规则（默认dev：安装失败仅提示；prod：安装失败即报错）")
    args = parser.parse_args()

    exit_code = 0

    # 1. 依赖自检 + 自动安装
    missing = check_dependencies()
    if missing:
        print(f"[WARN] 缺失运行依赖({len(missing)}): {', '.join(missing)}")
        exit_code = 1
        if not args.no_install and install_dependencies(missing):
            missing = check_dependencies()
            if not missing:
                exit_code = 0
        if missing and args.env == "prod":
            print("[ERROR] 正式模式：依赖安装失败，环境未就绪")

    # 2. 本地服务探测（缺失仅提示，但计入未就绪）
    down = probe_services()
    if down:
        print(f"[WARN] 本地服务未检测到: {', '.join(down)}")
        print("      相关功能将降级为内存模式（配置落库/向量检索/缓存/文件存储不可用）。")
        print("      Windows 一键部署: .\\prog\\deploy-dev-windows.ps1 -StartServices")
        print("      Linux Docker:     docker compose -f prog/docker-compose.yml up -d")
        exit_code = 1
    else:
        print("[INFO] 本地服务探测：PostgreSQL/Redis/MinIO/Milvus 均可达")

    # 3. 配置校验报告（存在错误计入未就绪）
    try:
        from prog.config.config_loader import ConfigLoader
        from prog.config.config_validator import ConfigValidator
        loader = ConfigLoader.get_instance()
        loader.load_config(force=True)
        report_errors = ConfigValidator(loader).validate_all()
        print(ConfigValidator(loader).get_validation_report())
        if report_errors:
            exit_code = 1
    except Exception as e:
        print(f"[WARN] 配置校验报告生成失败: {e}")

    print("-" * 50)
    if exit_code == 0:
        print("[OK] 部署环境检查完成，全部就绪")
    else:
        print("[ERROR] 部署环境未就绪，请先修复后重试（缺失项见上方报告）")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
