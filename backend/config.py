import os
from dotenv import load_dotenv

load_dotenv()

WB_API_KEY: str = os.getenv("WB_API_KEY", "")
USE_MOCK: bool = not WB_API_KEY or WB_API_KEY == "your_wildberries_api_key_here"
