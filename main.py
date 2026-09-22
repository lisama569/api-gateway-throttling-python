"""服务入口：``uvicorn main:app``。"""

from gateway.app import create_app

app = create_app()
