"""
base — SQLAlchemy ORM 声明式基类
================================
文件用途：
    提供所有ORM模型共享的 declarative Base 对象。
    所有 models/ 目录下的 ORM 模型类均继承此 Base。

对应技术规格章节：
    §1.7 数据库表结构设计（所有业务表的ORM映射基础）

使用方式：
    from prog.models.base import Base
    class Product:
        __tablename__ = "products"
        # ...

说明：
    使用 SQLAlchemy 2.0 风格的 DeclarativeBase，
    兼容 Mapped/mapped_column 类型注解写法。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类

    所有ORM模型继承此类以获得表映射能力。
    使用方式：class Product(Base): ...
    """
    pass
