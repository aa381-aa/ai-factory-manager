"""
config 模块 - AI工厂管家配置层

本模块负责：
1. 通过三层变量加载机制读取并合并配置（系统默认 → deployment_config.json → 环境变量）
2. 提供 Flask 应用运行所需的设置（SECRET_KEY、JWT、日志、CORS 等）

对应技术规格：§1.8.8 统一部署配置、§A.0 三层变量加载机制
"""
