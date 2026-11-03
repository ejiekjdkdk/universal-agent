"""启动只监听本机回环地址的工作台。"""
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv(Path(__file__).with_name(".env"))
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1",
                port=int(os.getenv("AGENT_PORT", "8765")), proxy_headers=False)
