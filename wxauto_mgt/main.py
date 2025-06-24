#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
WxAuto管理程序入口文件
"""

import asyncio
import os
import signal
import sys
import time
import platform
from pathlib import Path

# 添加项目根目录到Python路径
ROOT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(str(ROOT_DIR.parent))

import qasync
from PySide6.QtWidgets import QApplication
import logging

from wxauto_mgt.ui.main_window import MainWindow
from wxauto_mgt.data.db_manager import db_manager
from wxauto_mgt.data.config_store import config_store
from wxauto_mgt.core.api_client import instance_manager
from wxauto_mgt.core.message_listener import message_listener
from wxauto_mgt.core.message_delivery_service import message_delivery_service
from wxauto_mgt.core.message_sender import message_sender
from wxauto_mgt.utils.logging import setup_logging, logger

def handle_exception(exc_type, exc_value, exc_traceback):
    """
    处理未捕获的异常

    Args:
        exc_type: 异常类型
        exc_value: 异常值
        exc_traceback: 异常追踪
    """
    # 忽略KeyboardInterrupt异常
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    # 打印完整的异常信息到控制台
    import traceback
    print("="*80)
    print("发生未捕获的异常:")
    print(f"异常类型: {exc_type.__name__}")
    print(f"异常值: {exc_value}")
    print("-"*80)
    print("异常堆栈:")
    traceback.print_tb(exc_traceback)
    print("="*80)

    logger.critical("未捕获的异常", exc_info=(exc_type, exc_value, exc_traceback))

    # 如果是严重错误，尝试安全关闭
    if exc_type.__name__ in ['SegmentationFault', 'SystemError', 'MemoryError']:
        logger.critical(f"检测到严重错误 {exc_type.__name__}，尝试安全关闭程序")
        try:
            # 尝试清理资源
            import asyncio
            loop = asyncio.get_event_loop()
            if loop and not loop.is_closed():
                loop.run_until_complete(cleanup_services())
        except:
            pass  # 忽略清理过程中的错误

        # 强制退出
        import os
        os._exit(1)

def setup_signal_handlers():
    """设置信号处理器"""
    try:
        # 在Unix系统上设置SIGSEGV处理器
        if hasattr(signal, 'SIGSEGV'):
            def segfault_handler(signum, frame):
                logger.critical("检测到段错误(SIGSEGV)，程序即将退出")
                print("检测到段错误(SIGSEGV)，程序即将退出")
                try:
                    # 尝试清理资源
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop and not loop.is_closed():
                        loop.run_until_complete(cleanup_services())
                except:
                    pass
                import os
                os._exit(1)

            signal.signal(signal.SIGSEGV, segfault_handler)
            logger.info("已设置SIGSEGV信号处理器")
    except Exception as e:
        logger.warning(f"设置信号处理器失败: {e}")

async def init_services():
    """初始化各服务"""
    try:
        # 获取应用程序的基础路径
        # 如果是打包后的可执行文件，使用可执行文件所在目录
        # 如果是开发环境，使用项目根目录
        if getattr(sys, 'frozen', False):
            # 打包环境 - 使用可执行文件所在目录
            base_dir = os.path.dirname(sys.executable)
        else:
            # 开发环境 - 使用项目根目录
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

        # 确保data目录存在
        data_dir = os.path.join(base_dir, 'data')
        os.makedirs(data_dir, exist_ok=True)

        # 确保logs目录存在
        log_dir = os.path.join(data_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)

        # 确保downloads目录存在
        downloads_dir = os.path.join(data_dir, 'downloads')
        os.makedirs(downloads_dir, exist_ok=True)

        # 检查目录权限
        try:
            # 测试写入权限
            test_files = {
                data_dir: os.path.join(data_dir, '.test_write'),
                log_dir: os.path.join(log_dir, '.test_write'),
                downloads_dir: os.path.join(downloads_dir, '.test_write')
            }

            for dir_path, test_file in test_files.items():
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                print(f"目录 {dir_path} 写入权限正常")
        except Exception as e:
            print(f"目录权限检查失败: {e}")

        # 在打包环境下，确保目录有完全权限
        if getattr(sys, 'frozen', False):
            try:
                import stat
                # 设置目录权限为完全读写
                for dir_path in [data_dir, log_dir, downloads_dir]:
                    if platform.system() == "Windows":
                        # Windows下使用icacls命令设置权限
                        import subprocess
                        subprocess.run(['icacls', dir_path, '/grant', 'Everyone:(OI)(CI)F'],
                                      shell=True, check=False)
                    else:
                        # Unix系统使用chmod
                        os.chmod(dir_path,
                                stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)  # 777权限
                print("已设置目录完全权限")
            except Exception as e:
                print(f"设置目录权限失败: {e}")

        # 初始化日志
        setup_logging(log_dir, console_level="DEBUG", file_level="DEBUG")

        # 初始化数据库
        db_path = os.path.join(data_dir, 'wxauto_mgt.db')
        await db_manager.initialize(db_path)

        # 等待一下确保表创建完成
        await asyncio.sleep(0.1)

        # 加载实例配置
        logger.info("正在加载实例配置...")
        try:
            # 检查表是否存在
            check_sql = "SELECT name FROM sqlite_master WHERE type='table' AND name='instances'"
            table_exists = await db_manager.fetchone(check_sql)

            if not table_exists:
                logger.warning("实例表不存在，可能是首次运行")
                return True

            instances = await db_manager.fetchall(
                "SELECT * FROM instances WHERE enabled = 1"
            )
            logger.info(f"从数据库中获取到 {len(instances)} 个实例")

            # 初始化实例
            for instance in instances:
                try:
                    instance_id = instance.get("instance_id")
                    if not instance_id:
                        logger.error(f"实例配置错误: 缺少 instance_id")
                        continue

                    base_url = instance.get("base_url")
                    if not base_url:
                        logger.error(f"实例配置错误: {instance_id} 缺少 base_url")
                        continue

                    api_key = instance.get("api_key")
                    timeout = instance.get("timeout", 30)
                    name = instance.get("name", "未命名")

                    logger.info(f"正在加载实例: {instance_id} ({name})")
                    instance_manager.add_instance(instance_id, base_url, api_key, timeout)
                    logger.info(f"已加载实例: {instance_id}")
                except Exception as e:
                    logger.error(f"加载实例失败: {str(e)}")
                    continue
        except Exception as e:
            logger.error(f"获取实例配置失败: {str(e)}")
            return False

        # 初始化消息监听
        try:
            # 设置消息监听服务默认为启用状态
            logger.info("正在启动消息监听...")
            await message_listener.start()
            logger.info("消息监听服务已启动")
        except Exception as e:
            logger.error(f"初始化消息监听失败: {str(e)}")
            # 不中断启动流程

        # 初始化消息投递服务
        try:
            # 初始化消息投递服务
            logger.info("正在初始化消息投递服务...")
            await message_delivery_service.initialize()

            # 启动消息投递服务
            logger.info("正在启动消息投递服务...")
            await message_delivery_service.start()
            logger.info("消息投递服务已启动")
        except Exception as e:
            logger.error(f"初始化消息投递服务失败: {str(e)}")
            # 不中断启动流程

        logger.info("服务初始化完成")
        return True
    except Exception as e:
        logger.error(f"服务初始化失败: {str(e)}")
        return False

async def cleanup_services():
    """清理各服务"""
    try:
        # 停止消息投递服务
        if message_delivery_service:
            logger.info("正在停止消息投递服务...")
            await message_delivery_service.stop()

        # 停止消息监听
        if message_listener:
            logger.info("正在停止消息监听...")
            await message_listener.stop()

        # 关闭所有API客户端
        if instance_manager:
            logger.info("正在关闭API客户端...")
            await instance_manager.close_all()

        # 关闭数据库连接
        if db_manager:
            logger.info("正在关闭数据库连接...")
            await db_manager.close()

        logger.info("服务清理完成")
        return True
    except Exception as e:
        logger.error(f"服务清理失败: {str(e)}")
        return False

def main():
    """主程序入口"""
    try:
        # 设置未捕获异常处理器
        sys.excepthook = handle_exception

        # 设置信号处理器
        setup_signal_handlers()

        # 初始化Qt应用
        app = QApplication.instance()
        if not app:
            app = QApplication(sys.argv)
            app.setApplicationName("WxAuto管理工具")

        # 创建事件循环
        loop = qasync.QEventLoop(app)
        asyncio.set_event_loop(loop)

        # 初始化服务
        if not loop.run_until_complete(init_services()):
            logger.error("服务初始化失败")
            return 1

        # 创建主窗口
        window = MainWindow()
        window.show()

        # 设置信号处理
        # Windows系统不支持add_signal_handler，使用平台兼容的方式
        try:
            loop.add_signal_handler(signal.SIGINT, loop.stop)
        except NotImplementedError:
            # Windows平台上忽略此错误
            logger.debug("当前平台不支持add_signal_handler，跳过信号处理设置")

        # 运行事件循环
        with loop:
            logger.info("程序已启动")
            loop.run_forever()

        return 0

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logger.error(f"程序启动失败: {str(e)}\n{error_details}")
        print(f"程序启动失败详细信息: {str(e)}\n{error_details}")
        return 1
    finally:
        # 执行清理
        if 'loop' in locals():
            loop.run_until_complete(cleanup_services())

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n程序已终止")
        sys.exit(0)
    except Exception as e:
        print(f"程序启动失败: {e}")
        sys.exit(1)