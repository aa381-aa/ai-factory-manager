"""
scripts — 工具脚本模块
======================
包含数据库初始化、部署环境检查、生产服务器启动等脚本。

脚本列表：
    - init_db.py: 数据库初始化（执行SQL + 创建Milvus Collection）
    - deploy_check.py: 部署环境检查（依赖自检/自动安装 + 服务探测 + 配置校验）
    - run_server.py: 生产服务器启动（Waitress/Gunicorn）
"""
